#!/usr/bin/env python3
"""GPS HUD - Real-time speed display on 480x480 framebuffer.

Renders GPS speed, speed limit sign, and satellite status directly
to /dev/fb0 using struct.pack. No third-party dependencies.

Target: Luckfox Pico Ultra (ARM, Buildroot Linux, Python 3.11)
Framebuffer: 480x480, 32bpp XRGB8888 (byte order: B, G, R, X)
GPS: /dev/ttyS4, 9600 baud, NMEA protocol
"""

import datetime
import os
import sys
import time
import fcntl
import termios
import signal
import traceback

from framebuffer import Framebuffer, FB_DEV, FB_W, FB_H
from hud_renderer import (
    render_hud, HUDState,
    COL_BG,
)
from ipc_writer import (
    NPU_DETECT_FILE, NPU_POLL_INTERVAL,
    SPEED_IPC_FILE, DISPLAY_MODE_IPC, ISP_CONFIG_IPC_FILE,
    write_speed_ipc, write_npu_enable_ipc, write_isp_config_ipc,
    write_gps_ipc, write_heartbeat, write_display_mode_ipc,
)

# Fusion source identifiers -- module-level so DetectionState methods can
# reference them. Re-imported from speed_db inside main() if available;
# the fallback strings mirror speed_db.SOURCE_* exactly.
try:
    from speed_db import (
        SOURCE_DB, SOURCE_NPU,
        SOURCE_DB_LOW_CONFIDENCE, SOURCE_DEFAULT,
    )
except ImportError:
    SOURCE_DB = "DB"
    SOURCE_NPU = "NPU"
    SOURCE_DB_LOW_CONFIDENCE = "DB_LOW_CONFIDENCE"
    SOURCE_DEFAULT = "DEFAULT"

# ---------------------------------------------------------------------------
# App version
# ---------------------------------------------------------------------------
# Default APP_VERSION when /root/version.txt isn't present (e.g. a fresh
# device that hasn't been through an OTA update yet, or a dev workstation
# running hud_live.py directly). _resolve_version() below overrides it
# from /root/version.txt if available so the dashboard shows the bundle
# version actually deployed, not whatever string we hardcoded last.
APP_VERSION_FALLBACK = "0.0.0-dev"
_VERSION_FILE = "/root/version.txt"


def _resolve_version():
    try:
        with open(_VERSION_FILE) as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    return APP_VERSION_FALLBACK


APP_VERSION = _resolve_version()

# ---------------------------------------------------------------------------
# Region system -- GPS auto-detect for DB switching (UI always English)
# ---------------------------------------------------------------------------

# Per-region database paths and defaults
_REGION_DATA = {
    "au": {
        "default_limit": 100,
        "zones_db": "/root/data/speed_zones.db",
        "cameras_db": "/root/data/speed_cameras.db",
        "valid_speeds": {30, 40, 50, 60, 70, 80, 90, 100, 110},
    },
    "cn": {
        "default_limit": 120,
        "zones_db": "/root/data/speed_zones_cn.db",
        "cameras_db": "/root/data/speed_cameras_cn.db",
        "valid_speeds": {20, 30, 40, 50, 60, 70, 80, 100, 110, 120},
    },
}

# Geographic bounding boxes for auto-detection (WGS-84)
_REGION_BOUNDS = {
    "cn": {"lat": (18.0, 54.0), "lon": (73.0, 135.0)},
    "au": {"lat": (-44.0, -10.0), "lon": (113.0, 154.0)},
}


class RegionManager:
    """GPS-based automatic region detection for speed database switching.

    On first GPS fix, determines the region from coordinates and switches
    the database accordingly. Re-checks every 60s in case the device
    is relocated (e.g., shipped between countries).

    Falls back to HUD_REGION env var or "au" if GPS is unavailable.
    UI text is always English regardless of region.
    """

    CHECK_INTERVAL = 60.0  # seconds between geo-checks

    def __init__(self):
        initial = os.environ.get("HUD_REGION",
                                 os.environ.get("HUD_LOCALE", "au"))
        if initial not in _REGION_DATA:
            initial = "au"
        self.region = initial
        self._auto_detected = False
        self._last_check = 0.0

    @property
    def default_limit(self):
        return _REGION_DATA[self.region]["default_limit"]

    @property
    def zones_db(self):
        return _REGION_DATA[self.region]["zones_db"]

    @property
    def cameras_db(self):
        return _REGION_DATA[self.region]["cameras_db"]

    def detect_region(self, lat, lon):
        """Determine region from GPS coordinates.

        Returns region key ("au", "cn") or None if outside known regions.
        """
        for region, bounds in _REGION_BOUNDS.items():
            if (bounds["lat"][0] <= lat <= bounds["lat"][1] and
                    bounds["lon"][0] <= lon <= bounds["lon"][1]):
                return region
        return None

    def update(self, lat, lon, now=None):
        """Check GPS position and switch region if needed.

        Args:
            lat, lon: current GPS coordinates (WGS-84)
            now: current time (default: time.time())

        Returns:
            True if region changed (caller should reload DB), False otherwise
        """
        if now is None:
            now = time.time()

        # Rate-limit checks (skip if checked recently)
        if now - self._last_check < self.CHECK_INTERVAL:
            return False
        self._last_check = now

        # Skip invalid coordinates
        if lat == 0.0 and lon == 0.0:
            return False

        new_region = self.detect_region(lat, lon)
        if new_region is None:
            return False  # unknown region, keep current

        if new_region != self.region:
            old = self.region
            self.region = new_region
            self._auto_detected = True
            print(f"\n[region] GPS auto-switch: {old} -> {new_region} "
                  f"(lat={lat:.2f}, lon={lon:.2f})")
            return True  # caller should reload speed DB

        if not self._auto_detected:
            self._auto_detected = True
            print(f"[region] GPS confirmed: {self.region}")

        return False


# Global region manager instance
region_mgr = RegionManager()

# ---------------------------------------------------------------------------
# Serial port (raw termios, from gps_reader.py)
# ---------------------------------------------------------------------------

def open_serial(device="/dev/ttyS4", baudrate=9600):
    """Open serial port using raw termios (no pyserial dependency)."""
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)

    baud_map = {9600: termios.B9600, 115200: termios.B115200}
    speed = baud_map.get(baudrate, termios.B9600)
    attrs[4] = speed  # ispeed
    attrs[5] = speed  # ospeed

    attrs[0] = 0  # iflag
    attrs[1] = 0  # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0  # lflag
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1  # 100ms read timeout

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return fd


def open_serial_retry(device="/dev/ttyS4", baudrate=9600,
                      retries=3, retry_delay=2):
    """Open serial with retry for boot-time race conditions.

    3x2s cap: longer waits stall the HUD render loop after restart.
    If the port doesn't come back in 6s we let the HUD draw without GPS.
    """
    for attempt in range(1, retries + 1):
        try:
            return open_serial(device, baudrate)
        except OSError as e:
            print(f"[GPS] Open {device} failed "
                  f"(attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(retry_delay)
    print(f"[GPS] WARNING: cannot open {device} after {retries} attempts, "
          f"running without GPS")
    return -1

# ---------------------------------------------------------------------------
# NMEA parsing (from gps_reader.py)
# ---------------------------------------------------------------------------

def nmea_checksum(sentence):
    if "*" not in sentence:
        return False
    data, checksum = sentence.rsplit("*", 1)
    if data.startswith("$"):
        data = data[1:]
    calc = 0
    for c in data:
        calc ^= ord(c)
    try:
        return calc == int(checksum.strip(), 16)
    except ValueError:
        return False


def _nmea_to_decimal(raw_val, direction):
    """Convert NMEA lat/lon (ddmm.mmmm) to decimal degrees."""
    if not raw_val or not direction:
        return None
    try:
        raw = float(raw_val)
    except ValueError:
        return None
    degrees = int(raw / 100)
    minutes = raw - (degrees * 100)
    decimal = degrees + minutes / 60.0
    if direction in ("S", "W"):
        decimal = -decimal
    return decimal


def parse_gprmc(parts):
    if len(parts) < 10:
        return None
    result = {"type": "RMC", "valid": parts[2] == "A"}
    if not result["valid"]:
        return result
    # UTC time (parts[1]=hhmmss.ss) and date (parts[9]=ddmmyy) are valid
    # *only* when status is 'A'; the device has no RTC, so this is our
    # sole source of truth for wall-clock time. Used by sun.py.
    try:
        t = parts[1]
        if len(t) >= 6:
            result["utc_hour"] = int(t[0:2])
            result["utc_min"] = int(t[2:4])
            result["utc_sec"] = int(float(t[4:]))
    except (ValueError, IndexError):
        pass
    try:
        d = parts[9]
        if len(d) >= 6:
            # NMEA date is ddmmyy with two-digit year -- assume 21st century.
            result["utc_day"] = int(d[0:2])
            result["utc_month"] = int(d[2:4])
            result["utc_year"] = 2000 + int(d[4:6])
    except (ValueError, IndexError):
        pass
    # Latitude (parts[3]=ddmm.mmmm, parts[4]=N/S)
    lat = _nmea_to_decimal(parts[3], parts[4])
    if lat is not None:
        result["lat"] = lat
    # Longitude (parts[5]=dddmm.mmmm, parts[6]=E/W)
    lon = _nmea_to_decimal(parts[5], parts[6])
    if lon is not None:
        result["lon"] = lon
    if parts[7]:
        try:
            result["speed_kmh"] = float(parts[7]) * 1.852
        except ValueError:
            pass
    if parts[8]:
        try:
            result["heading"] = float(parts[8])
        except ValueError:
            pass
    return result


def parse_gpgga(parts):
    if len(parts) < 10:
        return None
    result = {"type": "GGA"}
    try:
        result["fix_quality"] = int(parts[6])
    except (ValueError, IndexError):
        result["fix_quality"] = 0
    try:
        result["satellites"] = int(parts[7])
    except (ValueError, IndexError):
        result["satellites"] = 0
    return result

# ---------------------------------------------------------------------------
# GPS state
# ---------------------------------------------------------------------------

class GPSState:
    def __init__(self):
        self.valid = False
        self.lat = 0.0
        self.lon = 0.0
        self.speed_kmh = 0.0
        self.heading = 0.0
        self.satellites = 0
        self.fix_quality = 0
        self.last_update = 0
        # UTC wall-clock from the most recent valid RMC; primary input to
        # the day/night detector. None until we get a fix.
        self.utc = None  # datetime.datetime in UTC

    def update(self, parsed):
        if parsed is None:
            return
        t = parsed.get("type", "")
        if t == "RMC":
            self.valid = parsed.get("valid", False)
            if self.valid:
                if "lat" in parsed:
                    self.lat = parsed["lat"]
                if "lon" in parsed:
                    self.lon = parsed["lon"]
                if "speed_kmh" in parsed:
                    self.speed_kmh = parsed["speed_kmh"]
                if "heading" in parsed:
                    self.heading = parsed["heading"]
                # UTC clock: stitch date + time if both present.
                if all(k in parsed for k in
                       ("utc_year", "utc_month", "utc_day",
                        "utc_hour", "utc_min", "utc_sec")):
                    try:
                        self.utc = datetime.datetime(
                            parsed["utc_year"], parsed["utc_month"], parsed["utc_day"],
                            parsed["utc_hour"], parsed["utc_min"], parsed["utc_sec"],
                            tzinfo=datetime.timezone.utc)
                    except ValueError:
                        pass  # invalid date components
                self.last_update = time.time()
        elif t == "GGA":
            self.fix_quality = parsed.get("fix_quality", 0)
            self.satellites = parsed.get("satellites", 0)

# ---------------------------------------------------------------------------
# NPU detection state (IPC with C inference process)
# ---------------------------------------------------------------------------

class DetectionState:
    """Hold latest NPU detection results read from IPC file.

    The C inference process (rknn_detect) writes detection results to
    /tmp/ai_hud_detect in a simple key=value format:

        speed_limit=60
        camera=1
        confidence=0.85
        timestamp=1234567890

    This class polls that file periodically and updates state.
    """

    def __init__(self, filepath=NPU_DETECT_FILE):
        self.filepath = filepath
        # Two-tier state separation:
        #   raw_npu_*       -- latest NPU output from IPC (per-frame ground truth)
        #   speed_limit/... -- fused output for display (consumes raw + DB)
        # Conflating these caused a feedback loop: fusion was fed its own
        # previous output as "new NPU input", letting a single real detection
        # accumulate enough votes via stale repeats to trigger an override.
        self.raw_npu_speed_limit = 0
        self.raw_npu_confidence = 0.0
        self.raw_npu_camera = False

        self.speed_limit = region_mgr.default_limit  # post-fusion display value
        self.camera_detected = False                  # post-fusion display value
        self.confidence = 0.0                          # mirrors raw_npu_confidence
        # Renderer paints "--" while this is True. Defaults True so the
        # first frame (before any GPS fix) shows "--" not region default.
        self.limit_low_confidence = True
        # Three-state display machine consumed by hud_renderer:
        #   no_data    -- never had a trusted signal (or coasting timed out)
        #   confirmed  -- fusion is currently producing SOURCE_DB / NPU
        #   coasting   -- lost the signal but within COASTING_TIMEOUT_S we
        #                 still show the last-confirmed number, visually
        #                 dimmed so the driver can tell it's not live.
        # Pattern follows the Toyota patent (US 10,152,883) "speed display
        # disappear" model adapted with an explicit visual-degradation
        # tier to avoid the Tesla-style stuck-on-stale failure mode.
        self.display_state = "no_data"
        self.last_confirmed_limit = 0
        self.last_confirmed_time = 0.0
        self.last_poll = 0
        self._last_mtime = 0
        self.npu_enabled = True  # user toggle for live detection

    # Coasting hold time after losing the fusion signal. 60s is well
    # under the Tesla "5 miles ~ 4 min @ 100 km/h" hold but long enough
    # to bridge typical NPU detection gaps on unmapped backroads.
    COASTING_TIMEOUT_S = 60.0

    # Stale-IPC gate threshold. Must cover the slowest adaptive
    # inference cadence in hud_ipc.h (5000 ms at 0-5 km/h) plus headroom
    # for an in-flight inference, but stay short enough that a real
    # C-side hang surfaces within the fusion 8s window.
    NPU_STALE_THRESHOLD_S = 8.0

    def poll(self):
        """Read detection file if changed. Updates only raw_npu_*; the
        fused display fields are owned by SpeedFusion.

        When the file's mtime is more than NPU_STALE_THRESHOLD_S old the
        C side is presumed dead/hung -- we zero raw_npu_* and return
        without re-reading, otherwise the cached phantom value would
        keep SpeedFusion voting forever.
        """
        if not self.npu_enabled:
            return
        now = time.time()
        if now - self.last_poll < NPU_POLL_INTERVAL:
            return
        self.last_poll = now

        try:
            st = os.stat(self.filepath)
            if (self._last_mtime > 0 and
                    now - st.st_mtime >= self.NPU_STALE_THRESHOLD_S):
                if (self.raw_npu_speed_limit != 0 or
                        self.raw_npu_camera or
                        self.raw_npu_confidence != 0.0):
                    # Log only on the transition so a long C-side outage
                    # doesn't flood ai_hud.log.
                    print(f"[NPU] IPC stale "
                          f"({now - st.st_mtime:.1f}s since last write) "
                          f"-- clearing raw_npu_* to stop phantom voting")
                self.raw_npu_speed_limit = 0
                self.raw_npu_confidence = 0.0
                self.raw_npu_camera = False
                self.confidence = 0.0
                return

            if st.st_mtime == self._last_mtime:
                return  # file unchanged
            self._last_mtime = st.st_mtime

            with open(self.filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    if key == "speed_limit":
                        try:
                            detected = int(val)
                            # Filter by region: ignore speeds not used locally
                            valid = _REGION_DATA.get(
                                region_mgr.region, {}
                            ).get("valid_speeds")
                            if detected == 0 or valid is None or detected in valid:
                                self.raw_npu_speed_limit = detected
                            else:
                                # Out-of-region detection: treat as no detection.
                                self.raw_npu_speed_limit = 0
                        except ValueError:
                            pass
                    elif key == "camera":
                        self.raw_npu_camera = val.strip() == "1"
                    elif key == "confidence":
                        try:
                            self.raw_npu_confidence = float(val)
                        except ValueError:
                            pass

            # Mirror raw confidence to legacy field for any external reader.
            self.confidence = self.raw_npu_confidence
        except (OSError, IOError):
            pass  # file doesn't exist yet -- NPU not running

    def set_npu_enabled(self, enabled):
        """Toggle NPU inference on/off. Writes IPC file for C thread."""
        self.npu_enabled = bool(enabled)
        write_npu_enable_ipc(self.npu_enabled)
        if not self.npu_enabled:
            # Clear stale NPU results when disabled
            self.raw_npu_speed_limit = 0
            self.raw_npu_confidence = 0.0
            self.raw_npu_camera = False
            self.speed_limit = region_mgr.default_limit
            self.camera_detected = False
            self.confidence = 0.0

    def update_display_state(self, source, fused_limit, now=None):
        """Advance the display state machine after each fusion update.

        Called from the main loop right after speed_fusion.update().
        Owns three derived fields the renderer consumes:
          self.display_state   -- "no_data" | "confirmed" | "coasting"
          self.speed_limit     -- numeric value to show (held during coast)
          self.limit_low_confidence -- True when renderer should paint "--"

        Coasting holds the last confirmed number for COASTING_TIMEOUT_S
        seconds after losing a SOURCE_DB / SOURCE_NPU signal; the
        renderer dims the digits during coasting so the driver can tell
        the value isn't live.
        """
        if now is None:
            now = time.time()
        has_signal = source in (SOURCE_DB, SOURCE_NPU)

        if has_signal:
            self.display_state = "confirmed"
            self.last_confirmed_limit = fused_limit
            self.last_confirmed_time = now
            self.speed_limit = fused_limit
            self.limit_low_confidence = False
            return

        # Signal absent. Eligible to coast if we have a recent confirmed
        # value and we're still inside the hold window.
        if (self.last_confirmed_limit > 0 and
                now - self.last_confirmed_time < self.COASTING_TIMEOUT_S):
            self.display_state = "coasting"
            self.speed_limit = self.last_confirmed_limit
            self.limit_low_confidence = False
            return

        # No signal AND no recent confirmed value -- fall back to "--".
        self.display_state = "no_data"
        self.speed_limit = fused_limit  # default_limit; only for over-limit math
        self.limit_low_confidence = True

    @property
    def active(self):
        """True if NPU has ever written results."""
        return self._last_mtime > 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS4"
    baudrate = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

    # --- SIGTERM handler for graceful shutdown ---
    _shutdown_requested = False

    def _sigterm_handler(signum, frame):
        nonlocal _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _sigterm_handler)

    print(f"HUD Live - GPS: {device} @ {baudrate} baud")
    print(f"Framebuffer: {FB_DEV} ({FB_W}x{FB_H})")
    print(f"Region: {region_mgr.region} (auto-detect from GPS enabled)")
    print(f"Default speed limit: {region_mgr.default_limit} km/h")
    print(f"NPU detection IPC: {NPU_DETECT_FILE}")

    # --- Load persistent config ---
    config = None
    try:
        from config_manager import ConfigManager, PARAM_DEFS
        config = ConfigManager()
        print(f"Config: loaded from {config.path}")

        # One-shot migration of pre-2026-05-19 fusion defaults. The original
        # values let too many false positives through (most notably
        # npu_confidence_no_db=0.60 < BOX_THRESH=0.65, so the fusion guard
        # was a no-op in regions without GPS DB coverage). If the user has
        # never overridden a param it'll still equal the OLD default; bump
        # it to the new default. User-overridden values are left alone.
        _OLD_FUSION_DEFAULTS = {
            ("fusion", "npu_confidence_min"):   0.70,
            ("fusion", "npu_confidence_no_db"): 0.60,
            ("fusion", "npu_vote_required"):    3,
            ("fusion", "npu_override_timeout"): 15.0,
        }
        migrated = []
        for (section, key), old in _OLD_FUSION_DEFAULTS.items():
            typ, new_default, _vmin, _vmax = PARAM_DEFS[(section, key)]
            if typ is float:
                cur = config.get_float(section, key)
                stale = abs(cur - old) < 1e-6
            else:
                cur = config.get_int(section, key)
                stale = cur == old
            if stale and cur != new_default:
                config.set(section, key, new_default)
                migrated.append(f"{section}.{key}: {old}->{new_default}")
        if migrated:
            config.save()
            print("[migrate] Retuned fusion defaults to reduce false positives:")
            for line in migrated:
                print(f"[migrate]   {line}")
    except Exception as e:
        print(f"Config: not available ({e}), using defaults")

    # --- Load offline speed database (optional) ---
    # DB module is imported once; instances are recreated on region switch.
    SpeedDB_cls = None
    SpeedFusion_cls = None
    fuse_camera_warning_fn = None
    try:
        from speed_db import SpeedDB as SpeedDB_cls
        from speed_db import SpeedFusion as SpeedFusion_cls
        from speed_db import fuse_camera_warning as fuse_camera_warning_fn
    except ImportError:
        print("Speed DB: module not available, using NPU only")

    def load_speed_db():
        """Load speed DB for current region. Returns (speed_db, speed_fusion)."""
        if SpeedDB_cls is None:
            return None, None
        zones_path = region_mgr.zones_db
        cameras_path = region_mgr.cameras_db
        zones_ok = os.path.isfile(zones_path)
        cameras_ok = os.path.isfile(cameras_path)
        db = None
        if zones_ok or cameras_ok:
            db = SpeedDB_cls(
                zones_path=zones_path if zones_ok else None,
                cameras_path=cameras_path if cameras_ok else None,
            )
            # Format build_epoch as a date for human-readable log + dashboard.
            # 0 = legacy v1 file without build_epoch -- show "unknown".
            if db.build_epoch:
                built = datetime.datetime.utcfromtimestamp(
                    db.build_epoch).strftime("%Y-%m-%d")
            else:
                built = "unknown"
            print(f"Speed DB [{region_mgr.region.upper()}]: "
                  f"{db.zone_count} zones, {db.camera_count} cameras, "
                  f"built {built}")
        else:
            print(f"Speed DB [{region_mgr.region.upper()}]: "
                  f"no database files found, using NPU only")
        fusion_kwargs = {}
        if config:
            fusion_kwargs = config.get_fusion_params()
        fusion = SpeedFusion_cls(
            default_limit=region_mgr.default_limit, **fusion_kwargs)
        return db, fusion

    speed_db, speed_fusion = load_speed_db()

    print("Press Ctrl+C to stop\n")

    fb = Framebuffer(FB_DEV)
    if not fb.available:
        print("[FB] FATAL: framebuffer not available, cannot render HUD")
        # Still try to run (process watchdog will restart us)

    # Early paint: replace splash with baseline HUD before any blocking I/O
    # (serial up to 6s) so the screen isn't stuck.
    gps = GPSState()
    detect = DetectionState()
    hud_state = HUDState()
    try:
        render_hud(fb, gps, hud_state, detect, region_mgr.default_limit)
    except Exception as _e:
        print(f"[BOOT] early HUD paint failed: {_e}")
        traceback.print_exc()

    gps_fd = open_serial_retry(device, baudrate)

    # Production defaults -- not user-configurable. CAM mode + NPU-off
    # only exist for developer debugging via direct edits.
    npu_initial = True
    display_mode = "hud"
    if config:
        saved_region = config.get_str("settings", "region")
        if saved_region and saved_region != region_mgr.region:
            region_mgr.region = saved_region
            speed_db, speed_fusion = load_speed_db()
    mirror_enabled = False
    if config:
        mirror_enabled = bool(config.get_int("settings", "mirror_display"))
    fb.mirror = mirror_enabled and display_mode != "cam"

    write_npu_enable_ipc(npu_initial)
    detect.npu_enabled = npu_initial
    write_display_mode_ipc(display_mode)
    # Boot fallback for night_mode: use the last known auto-decision so the
    # ISP isn't briefly miscalibrated before the first GPS fix. The
    # auto-switcher rewrites this once GPS gives us a UTC time + position.
    night_mode_initial = config.get_int("settings", "night_mode") if config else 0
    write_isp_config_ipc(night_mode_initial)
    print(f"Initial state: mirror={'ON' if mirror_enabled else 'OFF'}, "
          f"night={'ON' if night_mode_initial else 'OFF'} (will auto-update), "
          f"region={region_mgr.region} (will auto-update)")

    # Day/night auto-switch state -- declared early so the dashboard's
    # status snapshot (built inside the web server below) can read it
    # even if the first GPS fix hasn't arrived yet.
    auto_night_state = {
        "current": bool(night_mode_initial),
        "votes": 0,
        "pending": None,
    }

    nmea_buf = b""
    # hud_state constructed earlier for the boot-time pre-serial paint.

    # --- Shared config-change callbacks ---
    # Defined here so the web config server can dispatch the same actions
    # when a setting changes.
    def _on_npu_toggle(enabled):
        write_npu_enable_ipc(enabled)
        detect.npu_enabled = enabled

    def _on_region_change(new_region):
        nonlocal speed_db, speed_fusion
        region_mgr.region = new_region
        speed_db, speed_fusion = load_speed_db()
        hud_state.base_layer = None

    def _on_fusion_reload():
        nonlocal speed_fusion
        if SpeedFusion_cls and config:
            fusion_kwargs = config.get_fusion_params()
            speed_fusion = SpeedFusion_cls(
                default_limit=region_mgr.default_limit,
                **fusion_kwargs)

    def _on_display_mode_change(mode):
        nonlocal display_mode
        display_mode = mode
        write_display_mode_ipc(mode)
        # Mirror only applies to HUD mode
        fb.mirror = mirror_enabled and mode != "cam"
        if mode == "hud":
            # Switching back to HUD: force full redraw
            hud_state.base_layer = None

    def _on_mirror_change(enabled):
        nonlocal mirror_enabled
        mirror_enabled = enabled
        fb.mirror = enabled and display_mode != "cam"
        # Force base layer rebuild so cached layer matches new orientation
        hud_state.base_layer = None

    def _on_night_mode_change(enabled):
        write_isp_config_ipc(enabled)
        # Persist the latest auto-decision so the next boot starts in the
        # right mode instead of flashing the wrong ISP tone for ~5s.
        if config:
            try:
                config.set("settings", "night_mode", 1 if enabled else 0)
                config.save()
            except Exception:
                pass
        print(f"[ISP] Night mode: {'ON' if enabled else 'OFF'}")

    # --- Web config server (optional, graceful fallback) ---
    # Provides a PC-side HTML UI over USB via `adb forward tcp:8080 tcp:8080`.
    # Replaces the removed GT911 touch settings UI (firmware-stall bug).
    web_server = None
    if config is not None:
        try:
            from web_config import WebConfigServer

            def _build_status():
                """Live status snapshot for the dashboard /api/state."""
                age = int(time.time() - gps.last_update) if gps.last_update else None
                # Cap reported age so a 17h uptime without GPS doesn't render
                # as "62000s ago" -- looks like a bug.
                if age is not None and age > 999:
                    age = 999
                last_det = None
                npu_lim = getattr(detect, "raw_npu_speed_limit", 0)
                if npu_lim:
                    last_det = f"{npu_lim} km/h"
                db_info = None
                if speed_db is not None:
                    parts = []
                    if speed_db.zone_count:
                        parts.append(f"{speed_db.zone_count:,} zones")
                    if speed_db.camera_count:
                        parts.append(f"{speed_db.camera_count:,} cameras")
                    db_info = ", ".join(parts) if parts else None
                db_built = None
                if speed_db is not None and speed_db.build_epoch:
                    db_built = datetime.datetime.utcfromtimestamp(
                        speed_db.build_epoch).strftime("%Y-%m-%d")
                return {
                    "gps_valid":  gps.valid,
                    "gps_sats":   gps.satellites,
                    "gps_age_s":  age,
                    "speed_limit": detect.speed_limit,
                    "speed_limit_source": getattr(detect, "speed_limit_source", None),
                    "npu_running": detect.npu_enabled,
                    "last_detection": last_det,
                    "night_mode": bool(auto_night_state["current"]),
                    "db_info":    db_info,
                    "db_built":   db_built,
                }

            web_server = WebConfigServer(
                config=config,
                region_mgr=region_mgr,
                app_version=APP_VERSION,
                status_fn=_build_status,
                callbacks={
                    "on_mirror_change": _on_mirror_change,
                    "on_display_mode_change": _on_display_mode_change,
                },
            )
            if not web_server.start_in_thread():
                web_server = None
        except ImportError:
            print("Web config: module not available")
        except Exception as e:
            print(f"Web config: init failed ({e})")

    parsers = {
        "RMC": parse_gprmc,
        "GGA": parse_gpgga,
    }

    # Helper: render HUD with current default_speed_limit
    def _render_hud():
        render_hud(fb, gps, hud_state, detect, region_mgr.default_limit)

    # Initial render (before any GPS data - builds base layer cache)
    _render_hud()

    # Signal C binary (pip_render_thread) that HUD is ready for PiP overlay.
    # Must be AFTER initial render so splash is replaced before camera appears.
    try:
        with open("/tmp/ai_hud_ready", "w") as f:
            f.write("1\n")
    except OSError:
        pass

    # --- Night-mode auto-switch state ---
    # Drives day/night purely from GPS UTC time + (lat, lon); recomputed on
    # every RMC fix (~1 Hz) but only writes the IPC when the decision
    # actually changes, so the C-side ISP isn't woken up every second.
    try:
        from sun import is_night_now
    except ImportError:
        is_night_now = None
        print("[NIGHT] sun module unavailable, day/night auto disabled")

    # 3 consecutive same-direction GPS samples before committing -- guards
    # against the brief window after a hot start when GPS UTC can be wrong.
    AUTO_NIGHT_VOTE_THRESHOLD = 3

    def update_night_auto():
        if is_night_now is None or not gps.valid or gps.utc is None:
            return
        decision = is_night_now(gps.utc, gps.lat, gps.lon)
        if decision is None:
            return
        if decision == auto_night_state["current"]:
            auto_night_state["votes"] = 0
            auto_night_state["pending"] = None
            return
        if auto_night_state["pending"] != decision:
            auto_night_state["pending"] = decision
            auto_night_state["votes"] = 1
            return
        auto_night_state["votes"] += 1
        if auto_night_state["votes"] >= AUTO_NIGHT_VOTE_THRESHOLD:
            auto_night_state["current"] = decision
            auto_night_state["pending"] = None
            auto_night_state["votes"] = 0
            _on_night_mode_change(decision)
            print(f"[NIGHT] auto-switched to "
                  f"{'NIGHT' if decision else 'DAY'} "
                  f"(GPS UTC={gps.utc.strftime('%H:%M')} "
                  f"@ {gps.lat:.2f},{gps.lon:.2f})")

    try:
        while not _shutdown_requested:
          try:
            # --- GPS read (skip if GPS unavailable) ---
            if gps_fd < 0:
                time.sleep(0.1)
                detect.poll()
                write_heartbeat()
                # Still render HUD without GPS data
                if display_mode != "cam":
                    _render_hud()
                continue

            try:
                data = os.read(gps_fd, 256)
            except OSError:
                time.sleep(0.1)
                continue

            if not data:
                continue

            nmea_buf += data
            if len(nmea_buf) > 4096:
                nmea_buf = nmea_buf[-2048:]
            rmc_seen = False

            while b"\n" in nmea_buf:
                line, nmea_buf = nmea_buf.split(b"\n", 1)
                sentence = line.decode("ascii", errors="replace").strip()

                if not sentence.startswith("$"):
                    continue
                if not nmea_checksum(sentence):
                    continue

                parts = sentence.split("*")[0].split(",")
                msg_type = parts[0]

                for key, parser in parsers.items():
                    if msg_type.endswith(key):
                        result = parser(parts)
                        gps.update(result)
                        break

                if msg_type.endswith("RMC"):
                    rmc_seen = True

            # Refresh display once per GPS cycle (on RMC)
            if rmc_seen:
                detect.poll()  # check for NPU detection updates
                # Day/night auto-switch piggybacks on RMC cadence (~1 Hz),
                # vote-debounced so transient GPS noise can't flap the ISP.
                update_night_auto()

                # Write speed to IPC for C adaptive inference rate
                spd_ipc = gps.speed_kmh if gps.valid else 0.0
                write_speed_ipc(spd_ipc)
                write_heartbeat()

                # Write GPS coords to IPC for C frame capture metadata
                if gps.valid:
                    write_gps_ipc(gps.lat, gps.lon)

                # --- GPS-based region auto-detection ---
                if gps.valid and gps.lat != 0.0:
                    region_changed = region_mgr.update(
                        gps.lat, gps.lon)
                    if region_changed:
                        print(f"\n[REGION] Switched to "
                              f"{region_mgr.region.upper()}")
                        speed_db, speed_fusion = load_speed_db()
                        hud_state.base_layer = None  # force redraw

                # --- Fuse speed DB + NPU results ---
                if speed_fusion and gps.valid and gps.lat != 0.0:
                    db_limit = 0
                    db_confidence = None
                    db_cameras = []
                    if speed_db:
                        db_result = speed_db.query(
                            gps.lat, gps.lon, gps.heading,
                            camera_radius_m=speed_fusion.camera_alert_radius)
                        db_limit = db_result.speed_limit
                        db_cameras = db_result.cameras
                        # v3 DBs carry per-record confidence. v1/v2 DBs
                        # surface CONFIDENCE_UNKNOWN here, which fusion
                        # treats as single-source -- the conservative
                        # path. New keyword arg, defaults to None when
                        # SpeedFusion is the older API.
                        db_confidence = getattr(
                            db_result, "speed_confidence", None)

                    # Pass raw_npu_* (not detect.speed_limit) so fusion
                    # always sees the per-frame NPU truth, never its own
                    # previous output -- conflation caused stale-vote bug.
                    fused = speed_fusion.update(
                        db_limit,
                        detect.raw_npu_speed_limit,
                        detect.raw_npu_confidence,
                        db_confidence=db_confidence)
                    # Display-state machine owns detect.speed_limit /
                    # detect.limit_low_confidence from here on; it
                    # decides whether to coast on the last confirmed
                    # value or fall back to "--".
                    detect.update_display_state(speed_fusion.source, fused)

                    # Fuse camera warning: DB proximity OR NPU detection
                    if fuse_camera_warning_fn is not None:
                        show_cam, cam_dist, cam_src = fuse_camera_warning_fn(
                            db_cameras,
                            detect.raw_npu_camera)
                        detect.camera_detected = show_cam
                elif speed_fusion and not gps.valid:
                    # GPS lost -- feed db_limit=0 so the NPU window keeps
                    # voting (tunnel scenario). Do not reset() the fusion
                    # or the in-tunnel votes are discarded.
                    fused = speed_fusion.update(
                        0,
                        detect.raw_npu_speed_limit,
                        detect.raw_npu_confidence,
                        db_confidence=None)
                    detect.update_display_state(speed_fusion.source, fused)

                # Render: CAM mode lets C binary draw full-screen camera
                # (no Python overlay); HUD mode renders the full HUD.
                if display_mode != "cam":
                    _render_hud()

                spd = gps.speed_kmh if gps.valid else 0.0
                status = "FIX" if gps.valid else "---"
                lim = detect.speed_limit
                cam_info = ""
                if detect.camera_detected:
                    cam_info = " CAM"
                pos = ""
                if gps.valid and gps.lat != 0.0:
                    pos = f" ({gps.lat:.4f},{gps.lon:.4f})"
                print(f"\r[{status}] {spd:5.1f} km/h | LIM:{lim} | "
                      f"SAT:{gps.satellites}{cam_info}{pos}",
                      end="", flush=True)

          except KeyboardInterrupt:
            raise  # propagate to outer handler
          except Exception as e:
            # Catch-all: log and continue instead of crashing
            print(f"\n[ERROR] Main loop exception: {e}", flush=True)
            time.sleep(1)  # prevent error storm

    except KeyboardInterrupt:
        print("\n\nShutting down HUD...")
    finally:
        print("\n[SHUTDOWN] Cleaning up...")
        # Stop web config server (daemon thread, but close socket cleanly)
        if web_server is not None:
            try:
                web_server.stop()
            except Exception:
                pass
        # Clear screen to black on exit
        if fb.available:
            try:
                fb.clear((0, 0, 0))
                fb.flush()
                fb.close()
            except Exception:
                pass
        if gps_fd >= 0:
            try:
                os.close(gps_fd)
            except OSError:
                pass
        # Clean up IPC files
        for ipc_f in (SPEED_IPC_FILE, DISPLAY_MODE_IPC,
                      ISP_CONFIG_IPC_FILE, "/tmp/ai_hud_ready"):
            try:
                os.unlink(ipc_f)
            except OSError:
                pass
        print("Done.")


if __name__ == "__main__":
    main()

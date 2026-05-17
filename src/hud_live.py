#!/usr/bin/env python3
"""GPS HUD - Real-time speed display on 480x480 framebuffer.

Renders GPS speed, speed limit sign, and satellite status directly
to /dev/fb0 using struct.pack. No third-party dependencies.

Target: Luckfox Pico Ultra (ARM, Buildroot Linux, Python 3.11)
Framebuffer: 480x480, 32bpp XRGB8888 (byte order: B, G, R, X)
GPS: /dev/ttyS4, 9600 baud, NMEA protocol
"""

import os
import sys
import time
import fcntl
import termios
import signal

from framebuffer import Framebuffer, FB_DEV, FB_W, FB_H
from hud_renderer import (
    render_hud, HUDState, draw_menu_icon,
    COL_BG, COL_DIM,
)
from ipc_writer import (
    NPU_DETECT_FILE, NPU_POLL_INTERVAL,
    SPEED_IPC_FILE, DISPLAY_MODE_IPC, ISP_CONFIG_IPC_FILE,
    write_speed_ipc, write_npu_enable_ipc, write_isp_config_ipc,
    write_gps_ipc, write_heartbeat, write_display_mode_ipc,
)

# ---------------------------------------------------------------------------
# App version
# ---------------------------------------------------------------------------
APP_VERSION = "1.0.0"

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
    attrs[6][termios.VTIME] = 1  # 100ms timeout (fast loop for touch polling)

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return fd


def open_serial_retry(device="/dev/ttyS4", baudrate=9600,
                      retries=10, retry_delay=2):
    """Open serial with retry for boot-time race conditions."""
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
        self.speed_limit = region_mgr.default_limit
        self.camera_detected = False
        self.confidence = 0.0
        self.last_poll = 0
        self._last_mtime = 0
        self.npu_enabled = True  # user toggle for live detection

    def poll(self):
        """Read detection file if changed. Called from main loop."""
        if not self.npu_enabled:
            return  # NPU disabled, skip polling
        now = time.time()
        if now - self.last_poll < NPU_POLL_INTERVAL:
            return
        self.last_poll = now

        try:
            st = os.stat(self.filepath)
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
                                self.speed_limit = detected
                            # else: ignore (e.g., 20km/h detected in AU)
                        except ValueError:
                            pass
                    elif key == "camera":
                        self.camera_detected = val.strip() == "1"
                    elif key == "confidence":
                        try:
                            self.confidence = float(val)
                        except ValueError:
                            pass
        except (OSError, IOError):
            pass  # file doesn't exist yet -- NPU not running

    def set_npu_enabled(self, enabled):
        """Toggle NPU inference on/off. Writes IPC file for C thread."""
        self.npu_enabled = bool(enabled)
        write_npu_enable_ipc(self.npu_enabled)
        if not self.npu_enabled:
            # Clear stale NPU results when disabled
            self.speed_limit = region_mgr.default_limit
            self.camera_detected = False
            self.confidence = 0.0

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
        from config_manager import ConfigManager
        config = ConfigManager()
        print(f"Config: loaded from {config.path}")
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
            print(f"Speed DB [{region_mgr.region.upper()}]: "
                  f"{db.zone_count} zones, {db.camera_count} cameras")
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

    gps_fd = open_serial_retry(device, baudrate)
    gps = GPSState()
    detect = DetectionState()

    # Read initial state from config (respects persisted settings)
    npu_initial = True
    display_mode = "hud"
    if config:
        npu_initial = bool(config.get_int("settings", "npu_enabled"))
        display_mode = config.get_str("settings", "display_mode")
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
    night_mode_initial = config.get_int("settings", "night_mode") if config else 0
    write_isp_config_ipc(night_mode_initial)
    print(f"Initial state: NPU={'ON' if npu_initial else 'OFF'}, "
          f"display={display_mode}, mirror={'ON' if mirror_enabled else 'OFF'}, "
          f"night={'ON' if night_mode_initial else 'OFF'}, "
          f"region={region_mgr.region}")

    nmea_buf = b""
    hud_state = HUDState()

    # --- Touch input + Settings UI (optional, graceful fallback) ---
    touch = None
    settings_ui = None
    try:
        from touch_input import TouchInput
        from settings_ui import SettingsUI
        touch = TouchInput()
        if touch.available:
            settings_ui = SettingsUI(fb, config, region_mgr, app_version=APP_VERSION)

            # Wire up callbacks
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
                print(f"[ISP] Night mode: {'ON' if enabled else 'OFF'}")

            settings_ui.on_npu_toggle = _on_npu_toggle
            settings_ui.on_region_change = _on_region_change
            settings_ui.on_fusion_reload = _on_fusion_reload
            settings_ui.on_display_mode_change = _on_display_mode_change
            settings_ui.on_mirror_change = _on_mirror_change
            settings_ui.on_night_mode_change = _on_night_mode_change
            print(f"Touch: enabled (GT911 @ 0x{touch._addr:02X})")
        else:
            touch = None
            print("Touch: no device found, settings UI disabled")
    except ImportError:
        print("Touch: modules not available, settings UI disabled")
    except Exception as e:
        print(f"Touch: init failed ({e}), settings UI disabled")

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

    settings_close_time = 0  # debounce: prevent accidental re-open

    try:
        while not _shutdown_requested:
          try:
            # --- Poll touch events (non-blocking) ---
            if touch and settings_ui:
                was_active = settings_ui.active
                try:
                    for ev in touch.poll():
                        # Mirror touch x-coordinate when display is mirrored,
                        # so tap targets match the visually flipped layout.
                        # Settings UI disables mirror during render, but the
                        # touch zone must still be mirrored when settings is
                        # not active (HUD is mirrored on screen).
                        if mirror_enabled and not settings_ui.active:
                            ev.x = FB_W - 1 - ev.x
                        if settings_ui.active:
                            try:
                                settings_ui.handle_touch(ev)
                            except Exception as e:
                                print(f"\n[SETTINGS] handle_touch error: {e}")
                        else:
                            # Debounce: skip taps shortly after settings closed
                            if settings_close_time and (
                                    time.time() - settings_close_time < 0.5):
                                continue
                            # Tap top-left corner (0,0)-(80,80) -> open settings
                            if (ev.gesture == "tap"
                                    and ev.x < 80 and ev.y < 80):
                                settings_ui.activate()
                except Exception as e:
                    print(f"\n[TOUCH] poll error: {e}")
                # Settings inactivity timeout (auto-close if touch dies)
                if settings_ui.active and settings_ui.check_timeout():
                    was_active = True  # force display restore below

                # Exiting settings: immediately restore display
                if was_active and not settings_ui.active:
                    print("[settings] closed, restoring display")
                    settings_close_time = time.time()
                    if config:
                        display_mode = config.get_str("settings", "display_mode")
                    if display_mode == "cam":
                        # Full clear removes settings UI; C binary
                        # resumes camera on the next frame.
                        fb.clear(COL_BG)
                        draw_menu_icon(fb, 18, 18, COL_DIM)
                        fb.flush()
                    else:
                        # Immediate HUD render (don't wait for GPS cycle)
                        hud_state.base_layer = None
                        _render_hud()

            # --- GPS read (skip if GPS unavailable) ---
            if gps_fd < 0:
                time.sleep(0.1)
                detect.poll()
                write_heartbeat()
                # Still render HUD without GPS data
                if not (settings_ui and settings_ui.active):
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
                    db_cameras = []
                    if speed_db:
                        db_result = speed_db.query(
                            gps.lat, gps.lon, gps.heading,
                            camera_radius_m=speed_fusion.camera_alert_radius)
                        db_limit = db_result.speed_limit
                        db_cameras = db_result.cameras

                    # Fuse speed limit via state machine:
                    # DB is baseline, NPU can only lower (construction),
                    # requires 3 consecutive high-confidence detections.
                    detect.speed_limit = speed_fusion.update(
                        db_limit,
                        detect.speed_limit,
                        detect.confidence)

                    # Fuse camera warning: DB proximity OR NPU detection
                    if fuse_camera_warning_fn is not None:
                        show_cam, cam_dist, cam_src = fuse_camera_warning_fn(
                            db_cameras,
                            detect.camera_detected)
                        detect.camera_detected = show_cam
                elif speed_fusion and not gps.valid:
                    # Lost GPS fix -- reset fusion state
                    speed_fusion.reset()

                # Render: settings overlay > CAM mode gear > full HUD
                if settings_ui and settings_ui.active:
                    pass  # settings_ui renders itself on touch events
                elif display_mode == "cam":
                    # CAM mode: C binary renders full-screen camera.
                    # Only draw gear icon in the reserved top-left corner.
                    fb.fill_rect(0, 0, 40, 40, COL_BG)
                    draw_menu_icon(fb, 18, 18, COL_DIM)
                    fb.flush_rect(0, 0, 40, 40)
                else:
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
        # Close touch input
        if touch:
            try:
                touch.close()
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

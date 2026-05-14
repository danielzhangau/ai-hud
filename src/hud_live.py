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
import struct
import fcntl
import termios
import math
import signal
import errno

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
FB_DEV = "/dev/fb0"
FB_W = 480
FB_H = 480
FB_BPP = 4  # bytes per pixel (XRGB8888)
FB_STRIDE = 1920  # bytes per row = 480 * 4

# Colors (R, G, B) - matching mockup v3
COL_BG = (8, 10, 15)
COL_WHITE = (245, 248, 255)
COL_RED = (255, 55, 60)
COL_DIM = (55, 60, 75)
COL_GREEN = (50, 220, 120)
COL_ARC_BLUE = (60, 140, 255)
COL_ARC_RED = (255, 55, 60)
COL_GAUGE_BG = (30, 34, 45)
COL_LIMIT_BG = (252, 252, 255)
COL_LIMIT_RING = (215, 40, 40)
COL_LIMIT_TEXT = (20, 20, 25)

# NPU detection IPC file -- C inference process writes results here
NPU_DETECT_FILE = "/tmp/ai_hud_detect"
NPU_POLL_INTERVAL = 0.5  # seconds between file reads

# Speed IPC file -- Python writes speed for C adaptive inference rate
SPEED_IPC_FILE = "/tmp/ai_hud_speed"
SPEED_IPC_TMP = "/tmp/ai_hud_speed.tmp"

# NPU enable/disable IPC file -- Python writes, C inference thread reads
NPU_ENABLE_FILE = "/tmp/ai_hud_npu_enable"
NPU_ENABLE_TMP = "/tmp/ai_hud_npu_enable.tmp"

# GPS coordinates IPC file -- Python writes, C frame capture reads
GPS_IPC_FILE = "/tmp/ai_hud_gps"
GPS_IPC_TMP = "/tmp/ai_hud_gps.tmp"

# Display mode IPC file -- Python writes, C pip_render_thread reads
# "cam" = full-screen camera, absent/empty = HUD mode (default)
DISPLAY_MODE_IPC = "/tmp/ai_hud_display_mode"

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
        import time as _time
        if now is None:
            now = _time.time()

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

DEFAULT_SPEED_LIMIT = region_mgr.default_limit

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

def write_speed_ipc(speed_kmh):
    """Write current vehicle speed to IPC file for C inference thread.

    The C inference thread reads this to adjust its detection frequency:
    faster speed -> more frequent inference.
    """
    try:
        with open(SPEED_IPC_TMP, "w") as f:
            f.write(f"{speed_kmh:.1f}\n")
        os.rename(SPEED_IPC_TMP, SPEED_IPC_FILE)
    except OSError:
        pass  # non-critical, C side falls back to default rate


def write_npu_enable_ipc(enabled):
    """Write NPU enable/disable toggle to IPC file for C inference thread.

    When disabled, the C inference thread skips NPU inference entirely,
    falling back to pure database-driven speed limits.
    """
    try:
        with open(NPU_ENABLE_TMP, "w") as f:
            f.write("1\n" if enabled else "0\n")
        os.rename(NPU_ENABLE_TMP, NPU_ENABLE_FILE)
    except OSError:
        pass


def write_gps_ipc(lat, lon):
    """Write GPS coordinates to IPC file for C frame capture metadata.

    The C frame capture module reads lat/lon to tag saved frames
    with location data for offline labeling.
    """
    try:
        with open(GPS_IPC_TMP, "w") as f:
            f.write(f"lat={lat:.6f}\n")
            f.write(f"lon={lon:.6f}\n")
        os.rename(GPS_IPC_TMP, GPS_IPC_FILE)
    except OSError:
        pass


def write_display_mode_ipc(mode):
    """Write display mode IPC for C pip_render_thread.

    Args:
        mode: "cam" for full-screen camera, "hud" to stop camera rendering.
    """
    if mode == "cam":
        try:
            with open(DISPLAY_MODE_IPC, "w") as f:
                f.write("cam\n")
        except OSError:
            pass
    else:
        # HUD mode: remove IPC file (C defaults to HUD when absent)
        try:
            os.unlink(DISPLAY_MODE_IPC)
        except OSError:
            pass


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
# Bitmap font - digits 0-9, letters, symbols
# Each glyph is a list of strings where '#' = pixel on, '.' = pixel off.
# Large font: ~16x24 base, scaled up to ~40x60 for speed display.
# Small font: used at 1x (~16x24) or scaled to 20x30.
# ---------------------------------------------------------------------------

# 16-wide, 24-tall monospaced glyphs for 0-9 and a few extras
_GLYPHS = {
    '0': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
    ],
    '1': [
        "...##...........",
        "..###...........",
        ".####...........",
        "#..##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        ".######.........",
        ".######.........",
        "................",
    ],
    '2': [
        "..#####.........",
        ".#######........",
        "###..###........",
        "##....##........",
        "......##........",
        "......##........",
        "......##........",
        ".....##.........",
        ".....##.........",
        "....##..........",
        "....##..........",
        "...##...........",
        "...##...........",
        "..##............",
        "..##............",
        ".##.............",
        ".##.............",
        "##..............",
        "##..............",
        "##..............",
        "########........",
        "########........",
        "########........",
        "................",
    ],
    '3': [
        "..#####.........",
        ".#######........",
        "###..###........",
        "##....##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "...####.........",
        "...####.........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".#######........",
        "..#####.........",
        "................",
        "................",
    ],
    '4': [
        ".....##.........",
        "....###.........",
        "....###.........",
        "...####.........",
        "...#.##.........",
        "..##.##.........",
        "..##.##.........",
        ".##..##.........",
        ".##..##.........",
        "##...##.........",
        "##...##.........",
        "########........",
        "########........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        ".....##.........",
        "................",
        "................",
    ],
    '5': [
        "########........",
        "########........",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "#######.........",
        "########........",
        "##...###........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
    ],
    '6': [
        "...####.........",
        "..#####.........",
        ".###..##........",
        ".##...##........",
        "##..............",
        "##..............",
        "##..............",
        "##.####.........",
        "#######.........",
        "####.###........",
        "###...##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
    ],
    '7': [
        "########........",
        "########........",
        "......##........",
        ".....##.........",
        ".....##.........",
        "....##..........",
        "....##..........",
        "...##...........",
        "...##...........",
        "...##...........",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        "................",
        "................",
    ],
    '8': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
    ],
    '9': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".#######........",
        "..#####.........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "......##........",
        "##...##.........",
        "###.###.........",
        ".#####..........",
        "..###...........",
        "................",
        "................",
        "................",
    ],
    'k': [
        "##..............",
        "##..............",
        "##...##.........",
        "##..##..........",
        "##.##...........",
        "####............",
        "####............",
        "#####...........",
        "##.##...........",
        "##..##..........",
        "##...##.........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'm': [
        "................",
        "................",
        "#.##.##.........",
        "########........",
        "##.##.##........",
        "##.##.##........",
        "##.##.##........",
        "##.##.##........",
        "##.##.##........",
        "##.##.##........",
        "##.##.##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '/': [
        "......##........",
        ".....##.........",
        ".....##.........",
        "....##..........",
        "....##..........",
        "...##...........",
        "...##...........",
        "..##............",
        "..##............",
        ".##.............",
        ".##.............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'h': [
        "##..............",
        "##..............",
        "##..............",
        "##.####.........",
        "#######.........",
        "###..##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'S': [
        "..#####.........",
        ".#######........",
        "###...##........",
        "###.............",
        ".####...........",
        "..#####.........",
        "....####........",
        ".....###........",
        "##...###........",
        "###..##.........",
        ".#######........",
        "..#####.........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'A': [
        "...##...........",
        "..####..........",
        "..####..........",
        ".##..##.........",
        ".##..##.........",
        "##....##........",
        "##....##........",
        "########........",
        "########........",
        "##....##........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'T': [
        "########........",
        "########........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    ' ': [
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '.': [
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '-': [
        "................",
        "................",
        "................",
        "................",
        "................",
        "######..........",
        "######..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    # --- Additional uppercase letters ---
    'B': [
        "######..........",
        "#######.........",
        "##...###........",
        "##....##........",
        "##...###........",
        "#######.........",
        "#######.........",
        "##...###........",
        "##....##........",
        "##...###........",
        "#######.........",
        "######..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'C': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'D': [
        "#####...........",
        "######..........",
        "##..###.........",
        "##...###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##...###........",
        "##..###.........",
        "######..........",
        "#####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'E': [
        "########........",
        "########........",
        "##..............",
        "##..............",
        "######..........",
        "######..........",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "########........",
        "########........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'F': [
        "########........",
        "########........",
        "##..............",
        "##..............",
        "######..........",
        "######..........",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'G': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##..............",
        "##..............",
        "##..####........",
        "##..####........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'H': [
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "########........",
        "########........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'I': [
        "########........",
        "########........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "########........",
        "########........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'L': [
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "########........",
        "########........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'M': [
        "##....##........",
        "###..###........",
        "########........",
        "##.##.##........",
        "##.##.##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'N': [
        "##....##........",
        "###...##........",
        "####..##........",
        "##.##.##........",
        "##.##.##........",
        "##..####........",
        "##..####........",
        "##...###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'O': [
        "..####..........",
        ".######.........",
        "###..###........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'P': [
        "######..........",
        "#######.........",
        "##...###........",
        "##....##........",
        "##...###........",
        "#######.........",
        "######..........",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'R': [
        "######..........",
        "#######.........",
        "##...###........",
        "##....##........",
        "##...###........",
        "#######.........",
        "######..........",
        "##.##...........",
        "##..##..........",
        "##...##.........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'U': [
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "###..###........",
        ".######.........",
        "..####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'V': [
        "##....##........",
        "##....##........",
        "##....##........",
        ".##..##.........",
        ".##..##.........",
        ".##..##.........",
        "..####..........",
        "..####..........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'W': [
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##.##.##........",
        "##.##.##........",
        "########........",
        "###..###........",
        "###..###........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'X': [
        "##....##........",
        "##....##........",
        ".##..##.........",
        "..####..........",
        "...##...........",
        "...##...........",
        "..####..........",
        ".##..##.........",
        "##....##........",
        "##....##........",
        "##....##........",
        "##....##........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'Y': [
        "##....##........",
        "##....##........",
        ".##..##.........",
        ".##..##.........",
        "..####..........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "...##...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    # --- Lowercase letters ---
    'a': [
        "................",
        "................",
        ".#####..........",
        "######..........",
        ".....##.........",
        ".######.........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        ".######.........",
        ".##.###.........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'c': [
        "................",
        "................",
        ".####...........",
        "######..........",
        "##..##..........",
        "##..............",
        "##..............",
        "##..............",
        "##..##..........",
        "######..........",
        ".####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'd': [
        "....##..........",
        "....##..........",
        ".#####..........",
        "######..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "######..........",
        ".#####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'e': [
        "................",
        "................",
        ".####...........",
        "######..........",
        "##..##..........",
        "######..........",
        "######..........",
        "##..............",
        "##..##..........",
        "######..........",
        ".####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'f': [
        "..###...........",
        ".####...........",
        ".##.............",
        ".##.............",
        "#####...........",
        "#####...........",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'g': [
        "................",
        "................",
        ".#####..........",
        "######..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "######..........",
        ".#####..........",
        "....##..........",
        "##..##..........",
        ".####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'i': [
        "..##............",
        "................",
        ".###............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "######..........",
        "######..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'l': [
        ".###............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "######..........",
        "######..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'n': [
        "................",
        "................",
        "#.####..........",
        "######..........",
        "###..##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "##...##.........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'o': [
        "................",
        "................",
        ".####...........",
        "######..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "######..........",
        ".####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'p': [
        "................",
        "................",
        "#.####..........",
        "######..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "######..........",
        "#####...........",
        "##..............",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'r': [
        "................",
        "................",
        "##.###..........",
        "######..........",
        "###.............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    's': [
        "................",
        "................",
        ".####...........",
        "######..........",
        "##..............",
        ".####...........",
        "..####..........",
        "....##..........",
        "##..##..........",
        "######..........",
        ".####...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    't': [
        ".##.............",
        ".##.............",
        "#####...........",
        "#####...........",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".##.............",
        ".####...........",
        "..###...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'u': [
        "................",
        "................",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "######..........",
        ".#####..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'v': [
        "................",
        "................",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        ".####...........",
        ".####...........",
        "..##............",
        "..##............",
        "..##............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    'y': [
        "................",
        "................",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        "##..##..........",
        ".####...........",
        "..###...........",
        "...##...........",
        "...##...........",
        "####............",
        "###.............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    # --- Symbols ---
    '(': [
        "..##............",
        ".##.............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        "##..............",
        ".##.............",
        "..##............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    ')': [
        "##..............",
        ".##.............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        ".##.............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '+': [
        "................",
        "................",
        "...##...........",
        "...##...........",
        "...##...........",
        "########........",
        "########........",
        "...##...........",
        "...##...........",
        "...##...........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '<': [
        "................",
        "....##..........",
        "...##...........",
        "..##............",
        ".##.............",
        "##..............",
        "##..............",
        ".##.............",
        "..##............",
        "...##...........",
        "....##..........",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '>': [
        "................",
        "##..............",
        ".##.............",
        "..##............",
        "...##...........",
        "....##..........",
        "....##..........",
        "...##...........",
        "..##............",
        ".##.............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    ':': [
        "................",
        "................",
        "................",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "##..............",
        "##..............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
    '|': [
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "..##............",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
        "................",
    ],
}

# Glyph dimensions (base)
GLYPH_W = 16
GLYPH_H = 24


def _parse_glyph(rows):
    """Convert glyph string rows to a list of (row_index, col_index) tuples."""
    pixels = []
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == '#':
                pixels.append((r, c))
    return pixels


# Pre-parse all glyphs
_PARSED = {ch: _parse_glyph(rows) for ch, rows in _GLYPHS.items()}


def _compute_glyph_widths():
    """Pre-compute advance widths for all parsed glyphs."""
    widths = {}
    for ch, pixels in _PARSED.items():
        if ch.isdigit() or ch.isupper():
            widths[ch] = 8  # fixed monospace width
        elif not pixels:
            widths[ch] = GLYPH_W // 2
        else:
            widths[ch] = max(c for _, c in pixels) + 1
    return widths


_GLYPH_WIDTHS = _compute_glyph_widths()


def _glyph_actual_width(ch):
    """Return advance width for a character (pre-computed lookup)."""
    return _GLYPH_WIDTHS.get(ch, GLYPH_W // 2)


# ---------------------------------------------------------------------------
# Framebuffer renderer
# ---------------------------------------------------------------------------

class Framebuffer:
    """Direct framebuffer access with pixel-level drawing primitives."""

    # Pixel color cache: avoids repeated struct.pack for the same color tuple
    _pixel_cache = {}

    @classmethod
    def _pack_pixel(cls, color):
        """Pack (R, G, B) tuple to 4-byte XRGB. Cached per unique color."""
        p = cls._pixel_cache.get(color)
        if p is None:
            r, g, b = color
            p = struct.pack('BBBB', b, g, r, 0)
            cls._pixel_cache[color] = p
        return p

    def __init__(self, device=FB_DEV, retries=10, retry_delay=2):
        self.fd = -1
        self.buf = bytearray(FB_STRIDE * FB_H)
        for attempt in range(1, retries + 1):
            try:
                self.fd = os.open(device, os.O_RDWR)
                break
            except OSError as e:
                print(f"[FB] Open {device} failed (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    time.sleep(retry_delay)
        if self.fd < 0:
            print(f"[FB] FATAL: cannot open {device} after {retries} attempts")

    @property
    def available(self):
        """Whether framebuffer was successfully opened."""
        return self.fd >= 0

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def clear(self, color=COL_BG):
        """Fill entire buffer with a color."""
        pixel = self._pack_pixel(color)
        row = pixel * FB_W
        for y in range(FB_H):
            offset = y * FB_STRIDE
            self.buf[offset:offset + FB_STRIDE] = row

    def set_pixel(self, x, y, color):
        """Set a single pixel. color is (R, G, B)."""
        if 0 <= x < FB_W and 0 <= y < FB_H:
            offset = y * FB_STRIDE + x * FB_BPP
            self.buf[offset] = color[2]      # B
            self.buf[offset + 1] = color[1]  # G
            self.buf[offset + 2] = color[0]  # R
            # offset+3 = X (padding), leave as 0

    def fill_rect(self, x, y, w, h, color):
        """Fill a rectangle."""
        pixel = self._pack_pixel(color)
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(FB_W, x + w)
        y1 = min(FB_H, y + h)
        row_bytes = pixel * (x1 - x0)
        for py in range(y0, y1):
            offset = py * FB_STRIDE + x0 * FB_BPP
            self.buf[offset:offset + len(row_bytes)] = row_bytes

    def draw_rect(self, x, y, w, h, color):
        """Draw a 1px rectangle outline."""
        self.fill_rect(x, y, w, 1, color)            # top
        self.fill_rect(x, y + h - 1, w, 1, color)    # bottom
        self.fill_rect(x, y, 1, h, color)             # left
        self.fill_rect(x + w - 1, y, 1, h, color)    # right

    def draw_circle(self, cx, cy, radius, color, thickness=2):
        """Draw a circle outline using midpoint algorithm."""
        for t in range(-thickness // 2, (thickness + 1) // 2):
            r = radius + t
            if r < 0:
                continue
            x = 0
            y = r
            d = 1 - r
            while x <= y:
                for sx, sy in [(x, y), (y, x), (-x, y), (-y, x),
                                (x, -y), (y, -x), (-x, -y), (-y, -x)]:
                    self.set_pixel(cx + sx, cy + sy, color)
                x += 1
                if d < 0:
                    d += 2 * x + 1
                else:
                    y -= 1
                    d += 2 * (x - y) + 1

    def fill_circle(self, cx, cy, radius, color):
        """Fill a solid circle."""
        pixel = self._pack_pixel(color)
        r2 = radius * radius
        for dy in range(-radius, radius + 1):
            py = cy + dy
            if py < 0 or py >= FB_H:
                continue
            # Half-width at this row
            dx = int(math.sqrt(max(0, r2 - dy * dy)))
            x0 = max(0, cx - dx)
            x1 = min(FB_W, cx + dx + 1)
            if x0 >= x1:
                continue
            row_bytes = pixel * (x1 - x0)
            offset = py * FB_STRIDE + x0 * FB_BPP
            self.buf[offset:offset + len(row_bytes)] = row_bytes

    def draw_thick_arc(self, cx, cy, radius, start_deg, end_deg, color, width=10):
        """Draw a thick arc using optimized scanline ring traversal.

        Angles in degrees, measured clockwise from 3-o'clock (standard).
        start_deg=150, end_deg=390 gives a bottom-open arc like the mockup.
        Width is the arc thickness in pixels.

        Optimization strategy: for each scanline row, compute the two
        horizontal spans of the ring (left segment and right segment),
        then only check angles for those pixels. This avoids scanning
        the entire bounding box.
        """
        pixel = self._pack_pixel(color)
        buf = self.buf

        TWO_PI = 2 * math.pi
        s_norm = math.radians(start_deg) % TWO_PI
        e_norm = math.radians(end_deg) % TWO_PI
        wraps = e_norm <= s_norm  # arc crosses 0-degree line

        r_inner = max(0, radius - width // 2)
        r_outer = radius + (width - width // 2)
        ri2 = r_inner * r_inner
        ro2 = r_outer * r_outer

        atan2 = math.atan2
        sqrt = math.sqrt
        bpp = FB_BPP
        stride = FB_STRIDE
        fb_w = FB_W
        fb_h = FB_H

        for dy in range(-r_outer, r_outer + 1):
            py = cy + dy
            if py < 0 or py >= fb_h:
                continue
            dy2 = dy * dy
            if dy2 > ro2:
                continue

            # Outer circle horizontal extent at this row
            dx_outer = int(sqrt(ro2 - dy2))

            # Inner circle horizontal extent (if row intersects inner circle)
            if dy2 < ri2:
                dx_inner = int(sqrt(ri2 - dy2))
                # Ring has two horizontal segments:
                #   left:  [cx - dx_outer, cx - dx_inner]
                #   right: [cx + dx_inner, cx + dx_outer]
                segments = [
                    (max(0, cx - dx_outer), min(fb_w - 1, cx - dx_inner)),
                    (max(0, cx + dx_inner), min(fb_w - 1, cx + dx_outer)),
                ]
            else:
                # Row is above/below inner circle, entire outer span is ring
                segments = [
                    (max(0, cx - dx_outer), min(fb_w - 1, cx + dx_outer)),
                ]

            row_off = py * stride
            for seg_x0, seg_x1 in segments:
                if seg_x0 > seg_x1:
                    continue
                for px in range(seg_x0, seg_x1 + 1):
                    dx = px - cx
                    # Verify ring membership (handles rounding at edges)
                    d2 = dx * dx + dy2
                    if d2 < ri2 or d2 > ro2:
                        continue
                    # Angle check
                    angle = atan2(dy, dx) % TWO_PI
                    if wraps:
                        if angle < s_norm and angle > e_norm:
                            continue
                    else:
                        if angle < s_norm or angle > e_norm:
                            continue
                    off = row_off + px * bpp
                    buf[off:off + 4] = pixel

    def draw_text(self, text, x, y, color, scale=1):
        """Render text at (x, y) with given color and scale.

        Returns the total width drawn (for centering calculations).
        Optimized: uses row-span batching instead of per-pixel set_pixel.
        """
        pixel = self._pack_pixel(color)
        buf = self.buf
        stride = FB_STRIDE
        bpp = FB_BPP
        fb_w = FB_W
        fb_h = FB_H

        cx = x
        for ch in text:
            pixels = _PARSED.get(ch)
            if pixels is None:
                cx += (GLYPH_W // 2 + 2) * scale
                continue
            if scale == 1:
                # Fast path: single pixel per glyph pixel
                for gr, gc in pixels:
                    px = cx + gc
                    py = y + gr
                    if 0 <= px < fb_w and 0 <= py < fb_h:
                        off = py * stride + px * bpp
                        buf[off:off + 4] = pixel
            else:
                # Group glyph pixels by row to enable row-span writes
                # Build horizontal spans per scaled row
                row_spans = {}
                for gr, gc in pixels:
                    for sy in range(scale):
                        py = y + gr * scale + sy
                        if py < 0 or py >= fb_h:
                            continue
                        px_start = cx + gc * scale
                        px_end = px_start + scale
                        if py not in row_spans:
                            row_spans[py] = []
                        row_spans[py].append((px_start, px_end))

                for py, spans in row_spans.items():
                    # Sort and merge overlapping/adjacent spans
                    spans.sort()
                    merged = [spans[0]]
                    for s, e in spans[1:]:
                        if s <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                        else:
                            merged.append((s, e))
                    row_off = py * stride
                    for s, e in merged:
                        s_clamp = max(0, s)
                        e_clamp = min(fb_w, e)
                        if s_clamp >= e_clamp:
                            continue
                        n = e_clamp - s_clamp
                        off = row_off + s_clamp * bpp
                        buf[off:off + n * bpp] = pixel * n

            aw = _glyph_actual_width(ch)
            cx += (aw + 2) * scale
        return cx - x

    def measure_text(self, text, scale=1):
        """Calculate pixel width of text without drawing."""
        w = 0
        for ch in text:
            aw = _glyph_actual_width(ch)
            w += (aw + 2) * scale
        # Remove trailing gap
        if text:
            w -= 2 * scale
        return w

    def flush(self):
        """Write buffer to framebuffer device with partial-write handling."""
        if self.fd < 0:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            mv = memoryview(self.buf)
            total = len(self.buf)
            written = 0
            while written < total:
                try:
                    n = os.write(self.fd, mv[written:])
                    if n <= 0:
                        break
                    written += n
                except OSError as e:
                    if e.errno == errno.EINTR:
                        continue
                    break  # non-EINTR error, skip this frame
        except OSError:
            pass  # lseek failed, skip this frame

    def flush_rect(self, x, y, w, h):
        """Write only a rectangular region to framebuffer device.

        Used in CAM mode where C binary renders most of the screen via mmap
        and Python only needs to update the gear icon area without overwriting
        the camera frame.
        """
        if self.fd < 0:
            return
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(FB_W, x + w)
        y1 = min(FB_H, y + h)
        mv = memoryview(self.buf)
        try:
            for row in range(y0, y1):
                offset = row * FB_STRIDE + x0 * FB_BPP
                length = (x1 - x0) * FB_BPP
                os.lseek(self.fd, offset, os.SEEK_SET)
                os.write(self.fd, mv[offset:offset + length])
        except OSError:
            pass  # skip partial rect flush on error

# ---------------------------------------------------------------------------
# HUD rendering with caching
# ---------------------------------------------------------------------------

# Arc gauge constants (shared between cache builder and renderer)
ARC_CX = FB_W // 2
ARC_CY = FB_H // 2
ARC_R = 200
ARC_WIDTH = 10
ARC_START = 150   # degrees
ARC_END_FULL = 390  # degrees
ARC_SPAN = ARC_END_FULL - ARC_START  # 240 degrees
MAX_SPEED = 160

# Limit sign constants
SIGN_R = 52
SIGN_CX = FB_W - 65
SIGN_CY = FB_H // 2 + 10



def _build_base_layer(fb, speed_limit=DEFAULT_SPEED_LIMIT):
    """Pre-render the static background layer: bg + arc track + limit sign.

    Call once at startup or when speed_limit changes.  Returns a snapshot
    (bytearray copy) of the buffer that can be blitted back each frame
    instead of redrawing.
    """
    fb.clear(COL_BG)

    # Arc gauge background track
    fb.draw_thick_arc(ARC_CX, ARC_CY, ARC_R, ARC_START, ARC_END_FULL,
                      COL_GAUGE_BG, width=ARC_WIDTH)

    # Speed limit sign - red ring + white fill + number
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R, COL_LIMIT_RING)
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R - 5, COL_LIMIT_BG)

    limit_str = str(speed_limit)
    limit_scale = 2
    limit_tw = fb.measure_text(limit_str, limit_scale)
    limit_th = GLYPH_H * limit_scale
    limit_tx = SIGN_CX - limit_tw // 2
    limit_ty = SIGN_CY - limit_th // 2
    fb.draw_text(limit_str, limit_tx, limit_ty, COL_LIMIT_TEXT, limit_scale)

    # Thin separator line above satellite info
    fb.fill_rect(20, FB_H - 55, FB_W - 40, 1, COL_DIM)

    return bytearray(fb.buf)


class HUDState:
    """Track previous frame state for delta rendering."""
    __slots__ = ('speed_int', 'over_limit', 'valid', 'satellites',
                 'fix_quality', 'base_layer', 'speed_limit',
                 'camera_detected', 'camera_dist')

    def __init__(self):
        self.speed_int = -1
        self.over_limit = False
        self.valid = False
        self.satellites = -1
        self.fix_quality = -1
        self.base_layer = None
        self.speed_limit = -1
        self.camera_detected = False
        self.camera_dist = -1  # meters to nearest camera, -1 = none


def _draw_menu_icon(fb, cx, cy, color):
    """Draw a hamburger menu icon (three horizontal lines).

    16px wide, three 2px-tall lines spaced 6px apart, centered at (cx, cy).
    Universal 'open menu/settings' symbol, pixel-perfect on bitmap display.
    """
    lw, lh = 16, 2
    x0 = cx - lw // 2
    fb.fill_rect(x0, cy - 7, lw, lh, color)
    fb.fill_rect(x0, cy - 1, lw, lh, color)
    fb.fill_rect(x0, cy + 5, lw, lh, color)


def render_hud(fb, gps, state=None, detect=None):
    """Render the full HUD frame - mockup v3 style.

    If *state* is provided (HUDState), uses cached base layer and skips
    rendering when nothing changed.  On first call state.base_layer is
    built automatically.

    If *detect* is provided (DetectionState), speed limit and camera
    warning are updated dynamically from NPU inference results.

    Layout (matching mockup v3):
    - Arc gauge: centered, radius 200, 150deg-390deg
    - Speed number: huge (scale=6), centered in arc
    - Limit sign: right side, radius 52 (dynamic from NPU)
    - km/h label: below speed number
    - Camera warning badge: top center (when detected)
    - Satellite info + status dot: bottom left
    """

    speed = gps.speed_kmh if gps.valid else 0.0
    speed_int = int(round(speed))

    # Dynamic speed limit from NPU detection
    current_limit = detect.speed_limit if detect else DEFAULT_SPEED_LIMIT
    camera = detect.camera_detected if detect else False

    over_limit = speed_int > current_limit

    # --- Delta check: skip full redraw if nothing changed ---
    if state is not None:
        limit_changed = state.speed_limit != current_limit
        if (state.speed_int == speed_int and
                state.over_limit == over_limit and
                state.valid == gps.valid and
                state.satellites == gps.satellites and
                state.fix_quality == gps.fix_quality and
                state.camera_detected == camera and
                not limit_changed):
            return  # nothing changed, skip render

        # Rebuild base layer if speed limit changed (sign needs redraw)
        if limit_changed or state.base_layer is None:
            state.base_layer = _build_base_layer(fb, current_limit)
        fb.buf[:] = state.base_layer

        state.speed_int = speed_int
        state.over_limit = over_limit
        state.valid = gps.valid
        state.satellites = gps.satellites
        state.fix_quality = gps.fix_quality
        state.speed_limit = current_limit
        state.camera_detected = camera
    else:
        # No state tracking -- full render every frame
        fb.clear(COL_BG)
        fb.draw_thick_arc(ARC_CX, ARC_CY, ARC_R, ARC_START, ARC_END_FULL,
                          COL_GAUGE_BG, width=ARC_WIDTH)
        fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R, COL_LIMIT_RING)
        fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R - 5, COL_LIMIT_BG)
        limit_str = str(current_limit)
        limit_scale = 2
        limit_tw = fb.measure_text(limit_str, limit_scale)
        limit_th = GLYPH_H * limit_scale
        fb.draw_text(limit_str, SIGN_CX - limit_tw // 2,
                     SIGN_CY - limit_th // 2, COL_LIMIT_TEXT, limit_scale)
        fb.fill_rect(20, FB_H - 55, FB_W - 40, 1, COL_DIM)

    # --- Colors based on state ---
    speed_color = COL_RED if over_limit else COL_WHITE
    arc_color = COL_ARC_RED if over_limit else COL_ARC_BLUE

    # --- Active arc (proportional to speed) ---
    pct = min(speed_int / MAX_SPEED, 1.0) if MAX_SPEED > 0 else 0.0
    if pct > 0.005:
        active_end = ARC_START + pct * ARC_SPAN
        fb.draw_thick_arc(ARC_CX, ARC_CY, ARC_R, ARC_START, active_end,
                          arc_color, width=ARC_WIDTH)

        # Small filled dot at the tip of the active arc
        tip_rad = math.radians(active_end)
        tip_x = int(ARC_CX + ARC_R * math.cos(tip_rad))
        tip_y = int(ARC_CY + ARC_R * math.sin(tip_rad))
        fb.fill_circle(tip_x, tip_y, 6, arc_color)

    # --- Re-draw limit sign on top of arc (arc overlaps sign at 349-378 deg) ---
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R, COL_LIMIT_RING)
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R - 5, COL_LIMIT_BG)
    _limit_str = str(current_limit)
    _limit_tw = fb.measure_text(_limit_str, 2)
    fb.draw_text(_limit_str, SIGN_CX - _limit_tw // 2,
                 SIGN_CY - GLYPH_H, COL_LIMIT_TEXT, 2)

    # --- Speed number (huge, centered, shifted slightly left) ---
    speed_str = str(speed_int)
    speed_scale = 6  # 16*6=96px wide per glyph, 24*6=144px tall
    speed_w = fb.measure_text(speed_str, speed_scale)
    speed_h = GLYPH_H * speed_scale
    speed_x = ARC_CX - speed_w // 2 - 15
    speed_y = ARC_CY - speed_h // 2
    fb.draw_text(speed_str, speed_x, speed_y, speed_color, speed_scale)

    # --- "km/h" label below speed ---
    unit_str = "km/h"
    unit_scale = 1
    unit_w = fb.measure_text(unit_str, unit_scale)
    unit_x = speed_x + (speed_w - unit_w) // 2
    unit_y = speed_y + speed_h + 6
    fb.draw_text(unit_str, unit_x, unit_y, COL_DIM, unit_scale)

    # --- "NO SAT" hint when no fix ---
    if not gps.valid:
        hint_str = "--"
        hint_scale = 3
        hint_w = fb.measure_text(hint_str, hint_scale)
        hint_x = ARC_CX - hint_w // 2 - 15
        hint_y = speed_y - GLYPH_H * hint_scale - 8
        fb.draw_text(hint_str, hint_x, hint_y, COL_DIM, hint_scale)

    # --- Alert bar (bottom) ---
    show_alert = over_limit or camera
    if show_alert:
        bar_h = 44
        bar_y = FB_H - bar_h - 68
        bar_x = 24
        bar_w = FB_W - bar_x * 2

        # Choose alert color and text based on situation
        if camera and over_limit:
            alert_color = COL_RED
            alert_text = "SLOW DOWN"
        elif over_limit:
            alert_color = COL_RED
            alert_text = "OVER SPEED"
        else:
            alert_color = (255, 185, 35)  # amber
            alert_text = "CAMERA AHEAD"

        # Dark background
        bar_bg = (alert_color[0] // 6, alert_color[1] // 6, alert_color[2] // 6)
        fb.fill_rect(bar_x, bar_y, bar_w, bar_h, bar_bg)
        # Border rectangle
        fb.fill_rect(bar_x, bar_y, bar_w, 2, alert_color)
        fb.fill_rect(bar_x, bar_y + bar_h - 2, bar_w, 2, alert_color)
        fb.fill_rect(bar_x, bar_y, 2, bar_h, alert_color)
        fb.fill_rect(bar_x + bar_w - 2, bar_y, 2, bar_h, alert_color)
        # Alert text centered
        alert_tw = fb.measure_text(alert_text, 1)
        alert_tx = bar_x + (bar_w - alert_tw) // 2
        alert_ty = bar_y + (bar_h - GLYPH_H) // 2
        fb.draw_text(alert_text, alert_tx, alert_ty, alert_color, 1)

    # --- Camera warning badge (top center, when NPU detects camera) ---
    if camera:
        cam_color = COL_RED if over_limit else (255, 185, 35)  # amber
        cam_bg = (cam_color[0] // 5, cam_color[1] // 5, cam_color[2] // 5)
        badge_w, badge_h = 160, 36
        badge_x = ARC_CX - badge_w // 2
        badge_y = 82
        # Dark background pill
        fb.fill_rect(badge_x, badge_y, badge_w, badge_h, cam_bg)
        # Border (top, bottom, left, right)
        fb.fill_rect(badge_x, badge_y, badge_w, 2, cam_color)
        fb.fill_rect(badge_x, badge_y + badge_h - 2, badge_w, 2, cam_color)
        fb.fill_rect(badge_x, badge_y, 2, badge_h, cam_color)
        fb.fill_rect(badge_x + badge_w - 2, badge_y, 2, badge_h, cam_color)
        # Camera label text centered in badge
        cam_text = "CAMERA"
        cam_tw = fb.measure_text(cam_text, 1)
        cam_tx = badge_x + (badge_w - cam_tw) // 2
        cam_ty = badge_y + (badge_h - GLYPH_H) // 2
        fb.draw_text(cam_text, cam_tx, cam_ty, cam_color, 1)

    # --- Satellite info (bottom left) ---
    sat_str = "SAT " + str(gps.satellites)
    sat_scale = 1
    sat_w = fb.measure_text(sat_str, sat_scale)
    sat_x = 20
    sat_y = FB_H - 40

    sat_color = COL_GREEN if gps.fix_quality > 0 else COL_DIM
    fb.draw_text(sat_str, sat_x, sat_y, sat_color, sat_scale)

    # GPS status dot
    dot_x = sat_x + sat_w + 12
    dot_y = sat_y + GLYPH_H // 2
    dot_color = COL_GREEN if gps.valid else COL_RED
    fb.fill_circle(dot_x, dot_y, 4, dot_color)

    # --- Settings gear icon (top-left tap zone indicator) ---
    _draw_menu_icon(fb, 18, 18, COL_DIM)

    fb.flush()

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
    write_npu_enable_ipc(npu_initial)
    detect.npu_enabled = npu_initial
    write_display_mode_ipc(display_mode)
    print(f"Initial state: NPU={'ON' if npu_initial else 'OFF'}, "
          f"display={display_mode}, region={region_mgr.region}")

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
            settings_ui = SettingsUI(fb, config, region_mgr)

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
                if mode == "hud":
                    # Switching back to HUD: force full redraw
                    hud_state.base_layer = None

            settings_ui.on_npu_toggle = _on_npu_toggle
            settings_ui.on_region_change = _on_region_change
            settings_ui.on_fusion_reload = _on_fusion_reload
            settings_ui.on_display_mode_change = _on_display_mode_change
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

    # Initial render (before any GPS data - builds base layer cache)
    render_hud(fb, gps, hud_state, detect)

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
                        _draw_menu_icon(fb, 18, 18, COL_DIM)
                        fb.flush()
                    else:
                        # Immediate HUD render (don't wait for GPS cycle)
                        hud_state.base_layer = None
                        render_hud(fb, gps, hud_state, detect)

            # --- GPS read (skip if GPS unavailable) ---
            if gps_fd < 0:
                time.sleep(0.1)
                detect.poll()
                # Still render HUD without GPS data
                if not (settings_ui and settings_ui.active):
                    if display_mode != "cam":
                        render_hud(fb, gps, hud_state, detect)
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
                    _draw_menu_icon(fb, 18, 18, COL_DIM)
                    fb.flush_rect(0, 0, 40, 40)
                else:
                    render_hud(fb, gps, hud_state, detect)

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
        for ipc_f in (SPEED_IPC_FILE, DISPLAY_MODE_IPC, "/tmp/ai_hud_ready"):
            try:
                os.unlink(ipc_f)
            except OSError:
                pass
        print("Done.")


if __name__ == "__main__":
    main()

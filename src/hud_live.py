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

DEFAULT_SPEED_LIMIT = 100  # fallback limit when no sign detected (km/h)

# NPU detection IPC file -- C inference process writes results here
NPU_DETECT_FILE = "/tmp/ai_hud_detect"
NPU_POLL_INTERVAL = 0.5  # seconds between file reads

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
    attrs[6][termios.VTIME] = 10  # 1s timeout

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return fd

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


def parse_gprmc(parts):
    if len(parts) < 10:
        return None
    result = {"type": "RMC", "valid": parts[2] == "A"}
    if not result["valid"]:
        return result
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
        self.speed_limit = DEFAULT_SPEED_LIMIT
        self.camera_detected = False
        self.confidence = 0.0
        self.last_poll = 0
        self._last_mtime = 0

    def poll(self):
        """Read detection file if changed. Called from main loop."""
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
                            self.speed_limit = int(val)
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
        "....##..........",
        "...###..........",
        "..####..........",
        ".#.###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        "...###..........",
        ".########.......",
        ".########.......",
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
        "......##........",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        "......##........",
        "##...##.........",
        "##..###.........",
        ".#####..........",
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
        "..#####.........",
        ".#######........",
        "###..###........",
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
        "###..###........",
        ".#######........",
        "..#####.........",
        "................",
        "................",
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
        "..####.##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        ".......##.......",
        "##....##........",
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
        "#.###.###.......",
        "###.###.##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
        "##..##..##......",
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
        "###..###........",
        "##....##........",
        "##..............",
        "###.............",
        ".####...........",
        "..#####.........",
        "....####........",
        "......###.......",
        ".......##.......",
        "##....##........",
        "###..###........",
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
    ],
    'A': [
        "....##..........",
        "...####.........",
        "...####.........",
        "..##..##........",
        "..##..##........",
        ".##....##.......",
        ".##....##.......",
        ".########.......",
        ".########.......",
        "##......##......",
        "##......##......",
        "##......##......",
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
        "##########......",
        "##########......",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
        "....##..........",
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


def _glyph_actual_width(ch):
    """Return the actual right-most used column + 1 for tighter spacing."""
    pixels = _PARSED.get(ch)
    if not pixels:
        return GLYPH_W // 2  # space
    max_c = max(c for _, c in pixels)
    return max_c + 1


# ---------------------------------------------------------------------------
# Framebuffer renderer
# ---------------------------------------------------------------------------

class Framebuffer:
    """Direct framebuffer access with pixel-level drawing primitives."""

    def __init__(self, device=FB_DEV):
        self.fd = os.open(device, os.O_RDWR)
        self.buf = bytearray(FB_STRIDE * FB_H)

    def close(self):
        os.close(self.fd)

    def clear(self, color=COL_BG):
        """Fill entire buffer with a color."""
        r, g, b = color
        pixel = struct.pack('BBBB', b, g, r, 0)
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
        r, g, b = color
        pixel = struct.pack('BBBB', b, g, r, 0)
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(FB_W, x + w)
        y1 = min(FB_H, y + h)
        row_bytes = pixel * (x1 - x0)
        for py in range(y0, y1):
            offset = py * FB_STRIDE + x0 * FB_BPP
            self.buf[offset:offset + len(row_bytes)] = row_bytes

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
        r, g, b = color
        pixel = struct.pack('BBBB', b, g, r, 0)
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
        r, g, b = color
        pixel = struct.pack('BBBB', b, g, r, 0)
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
        r, g, b = color
        pixel = struct.pack('BBBB', b, g, r, 0)
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
        """Write buffer to framebuffer device."""
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, bytes(self.buf))

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
                 'camera_detected')

    def __init__(self):
        self.speed_int = -1
        self.over_limit = False
        self.valid = False
        self.satellites = -1
        self.fix_quality = -1
        self.base_layer = None
        self.speed_limit = -1
        self.camera_detected = False


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

    # --- Alert bar (bottom, context-sensitive) ---
    show_alert = over_limit or camera
    if show_alert:
        bar_h = 44
        bar_y = FB_H - bar_h - 68
        bar_x = 24
        bar_w = FB_W - 48

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
        # "CAMERA" text centered in badge
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

    fb.flush()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS4"
    baudrate = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

    print(f"HUD Live - GPS: {device} @ {baudrate} baud")
    print(f"Framebuffer: {FB_DEV} ({FB_W}x{FB_H})")
    print(f"Default speed limit: {DEFAULT_SPEED_LIMIT} km/h")
    print(f"NPU detection IPC: {NPU_DETECT_FILE}")
    print("Press Ctrl+C to stop\n")

    fb = Framebuffer(FB_DEV)
    gps_fd = open_serial(device, baudrate)
    gps = GPSState()
    detect = DetectionState()
    nmea_buf = b""
    hud_state = HUDState()

    parsers = {
        "RMC": parse_gprmc,
        "GGA": parse_gpgga,
    }

    # Initial render (before any GPS data - builds base layer cache)
    render_hud(fb, gps, hud_state, detect)

    try:
        while True:
            try:
                data = os.read(gps_fd, 256)
            except OSError:
                time.sleep(0.1)
                continue

            if not data:
                continue

            nmea_buf += data
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
                render_hud(fb, gps, hud_state, detect)
                spd = gps.speed_kmh if gps.valid else 0.0
                status = "FIX" if gps.valid else "---"
                lim = detect.speed_limit
                cam = " CAM" if detect.camera_detected else ""
                print(f"\r[{status}] {spd:5.1f} km/h | LIM:{lim} | SAT:{gps.satellites}{cam}",
                      end="", flush=True)

    except KeyboardInterrupt:
        print("\n\nShutting down HUD...")
    finally:
        # Clear screen to black on exit
        fb.clear((0, 0, 0))
        fb.flush()
        fb.close()
        os.close(gps_fd)
        print("Done.")


if __name__ == "__main__":
    main()

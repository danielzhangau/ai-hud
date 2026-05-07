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

# Colors (R, G, B)
COL_BG = (8, 10, 15)
COL_WHITE = (245, 248, 255)
COL_RED = (255, 55, 60)
COL_DIM = (80, 85, 95)
COL_GREEN = (50, 205, 100)
COL_LIMIT_BG = (255, 255, 255)
COL_LIMIT_RING = (220, 30, 35)
COL_LIMIT_TEXT = (30, 30, 30)

SPEED_LIMIT = 100  # hardcoded limit (km/h)

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

    def draw_text(self, text, x, y, color, scale=1):
        """Render text at (x, y) with given color and scale.

        Returns the total width drawn (for centering calculations).
        """
        cx = x
        for ch in text:
            pixels = _PARSED.get(ch)
            if pixels is None:
                # Unknown character, skip with space width
                cx += (GLYPH_W // 2 + 2) * scale
                continue
            for gr, gc in pixels:
                # Scale each glyph pixel into a scale x scale block
                for sy in range(scale):
                    for sx in range(scale):
                        self.set_pixel(cx + gc * scale + sx,
                                       y + gr * scale + sy, color)
            aw = _glyph_actual_width(ch)
            cx += (aw + 2) * scale  # 2px base gap between chars
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
# HUD rendering
# ---------------------------------------------------------------------------

def render_hud(fb, gps):
    """Render the full HUD frame."""
    fb.clear(COL_BG)

    speed = gps.speed_kmh if gps.valid else 0.0
    speed_int = int(round(speed))
    over_limit = speed_int > SPEED_LIMIT

    # -- Speed number (large, centered) --
    speed_str = str(speed_int)
    speed_scale = 3  # 16*3=48px wide base, 24*3=72px tall
    speed_w = fb.measure_text(speed_str, speed_scale)
    # Center horizontally (shifted slightly left to leave room for limit sign)
    speed_x = (FB_W - speed_w) // 2 - 40
    speed_y = (FB_H - GLYPH_H * speed_scale) // 2 - 20
    speed_color = COL_RED if over_limit else COL_WHITE
    fb.draw_text(speed_str, speed_x, speed_y, speed_color, speed_scale)

    # -- "km/h" label below speed --
    unit_str = "km/h"
    unit_scale = 1
    unit_w = fb.measure_text(unit_str, unit_scale)
    unit_x = speed_x + (speed_w - unit_w) // 2
    unit_y = speed_y + GLYPH_H * speed_scale + 8
    fb.draw_text(unit_str, unit_x, unit_y, COL_DIM, unit_scale)

    # -- Speed limit sign (right side) --
    # Circle with red ring and white fill, number inside
    limit_cx = 400
    limit_cy = FB_H // 2 - 10
    limit_r = 45

    fb.fill_circle(limit_cx, limit_cy, limit_r, COL_LIMIT_RING)
    fb.fill_circle(limit_cx, limit_cy, limit_r - 6, COL_LIMIT_BG)

    # Limit number centered in circle
    limit_str = str(SPEED_LIMIT)
    limit_scale = 2
    limit_tw = fb.measure_text(limit_str, limit_scale)
    limit_th = GLYPH_H * limit_scale
    limit_tx = limit_cx - limit_tw // 2
    limit_ty = limit_cy - limit_th // 2
    fb.draw_text(limit_str, limit_tx, limit_ty, COL_LIMIT_TEXT, limit_scale)

    # -- Satellite info (bottom area) --
    sat_str = "SAT " + str(gps.satellites)
    sat_scale = 1
    sat_w = fb.measure_text(sat_str, sat_scale)
    sat_x = 20
    sat_y = FB_H - 50

    # Color: green if fix, dim if no fix
    sat_color = COL_GREEN if gps.fix_quality > 0 else COL_DIM
    fb.draw_text(sat_str, sat_x, sat_y, sat_color, sat_scale)

    # -- GPS status indicator (small dot) --
    dot_x = sat_x + sat_w + 16
    dot_y = sat_y + GLYPH_H // 2
    dot_color = COL_GREEN if gps.valid else COL_RED
    fb.fill_circle(dot_x, dot_y, 5, dot_color)

    # -- "NO FIX" if invalid --
    if not gps.valid:
        nf_str = "--"
        # Already showing 0 speed; also show text hint
        hint_str = "NO SAT"
        hint_scale = 1
        hint_w = fb.measure_text(hint_str, hint_scale)
        hint_x = (FB_W - hint_w) // 2 - 40
        hint_y = speed_y - 30
        fb.draw_text(hint_str, hint_x, hint_y, COL_DIM, hint_scale)

    # -- Thin separator line --
    fb.fill_rect(20, FB_H - 65, FB_W - 40, 1, COL_DIM)

    fb.flush()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyS4"
    baudrate = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

    print(f"HUD Live - GPS: {device} @ {baudrate} baud")
    print(f"Framebuffer: {FB_DEV} ({FB_W}x{FB_H})")
    print(f"Speed limit: {SPEED_LIMIT} km/h")
    print("Press Ctrl+C to stop\n")

    fb = Framebuffer(FB_DEV)
    gps_fd = open_serial(device, baudrate)
    gps = GPSState()
    nmea_buf = b""

    parsers = {
        "RMC": parse_gprmc,
        "GGA": parse_gpgga,
    }

    # Initial render (before any GPS data)
    render_hud(fb, gps)

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
                render_hud(fb, gps)
                spd = gps.speed_kmh if gps.valid else 0.0
                status = "FIX" if gps.valid else "---"
                print(f"\r[{status}] {spd:5.1f} km/h | SAT: {gps.satellites}",
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

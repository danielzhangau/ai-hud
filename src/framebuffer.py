"""Framebuffer renderer - direct pixel drawing on /dev/fb0.

480x480 XRGB8888 framebuffer with bitmap font text rendering,
geometric primitives, and horizontal mirror support for HUD mode.

Target: Luckfox Pico Ultra (ARM Cortex-A7, Buildroot Linux)
"""

import os
import math
import time
import struct
import errno

from bitmap_font import _PARSED, GLYPH_W, GLYPH_H, glyph_actual_width

# ---------------------------------------------------------------------------
# Framebuffer constants
# ---------------------------------------------------------------------------

FB_DEV = "/dev/fb0"
FB_W = 480
FB_H = 480
FB_BPP = 4  # bytes per pixel (XRGB8888)
FB_STRIDE = 1920  # bytes per row = 480 * 4


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

    def __init__(self, device=FB_DEV, retries=3, retry_delay=1):
        # 3x1s caps boot delay to 3s; kernel releases fb0 as soon as the
        # previous process is fully gone, so longer retries just stall startup.
        self.fd = -1
        self.buf = bytearray(FB_STRIDE * FB_H)
        self.mirror = False  # horizontal flip for HUD windshield reflection
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

    def clear(self, color):
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

            aw = glyph_actual_width(ch)
            cx += (aw + 2) * scale
        return cx - x

    def measure_text(self, text, scale=1):
        """Calculate pixel width of text without drawing."""
        w = 0
        for ch in text:
            aw = glyph_actual_width(ch)
            w += (aw + 2) * scale
        # Remove trailing gap
        if text:
            w -= 2 * scale
        return w

    def flush(self):
        """Write buffer to framebuffer device with partial-write handling.

        When self.mirror is True, each row is horizontally flipped before
        writing. This is used for HUD windshield reflection mode.
        """
        if self.fd < 0:
            return
        data = self._mirror_buf() if self.mirror else self.buf
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            mv = memoryview(data)
            total = len(data)
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

    def _mirror_buf(self):
        """Return horizontally mirrored framebuffer data.

        Uses array.array to reverse 4-byte (XRGB8888) pixel groups per row
        via C-level slice operations. ~1-2ms on Cortex-A7 for 480x480.
        """
        import array
        arr = array.array('I')
        arr.frombytes(self.buf)
        for y in range(FB_H):
            s = y * FB_W
            arr[s:s + FB_W] = arr[s:s + FB_W][::-1]
        return arr.tobytes()

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

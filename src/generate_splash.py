#!/usr/bin/env python3
"""Generate HUD splash screen for 480x480 RGB LCD display.

Serves as both a display test image and project boot screen.
"""

import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 480, 480

BG = (8, 10, 15)
WHITE = (245, 248, 255)
BLUE = (60, 140, 255)
BLUE_DIM = (30, 70, 130)
DIM = (40, 44, 55)
ACCENT = (50, 220, 120)

FONT = "/System/Library/Fonts/Avenir Next Condensed.ttc"
FONT_R = "/System/Library/Fonts/HelveticaNeue.ttc"


def font(size, bold=False):
    try:
        idx = 4 if bold else 0
        return ImageFont.truetype(FONT, size, index=idx)
    except Exception:
        return ImageFont.load_default()


def font_r(size, bold=False):
    try:
        idx = 1 if bold else 0
        return ImageFont.truetype(FONT_R, size, index=idx)
    except Exception:
        return ImageFont.load_default()


def center_text(draw, cx, cy, text, fnt, color):
    bbox = draw.textbbox((0, 0), text, font=fnt)
    draw.text(
        (cx - (bbox[0] + bbox[2]) // 2, cy - (bbox[1] + bbox[3]) // 2),
        text, fill=color, font=fnt,
    )


def add_glow(img, cx, cy, radius, color, strength=0.35):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(radius, 0, -2):
        a = int(255 * strength * (1 - i / radius) ** 1.8)
        gd.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(*color, a))
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"))


def draw_arc(draw, cx, cy, r, start, end, color, width):
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=start, end=end, fill=color, width=width)


def main():
    import os
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    cx, cy = W // 2, H // 2

    # -- Background: subtle radial gradient --
    add_glow(img, cx, cy, 280, BLUE, 0.06)
    draw = ImageDraw.Draw(img)

    # -- Decorative arc rings --
    arc_cy = cy - 20 + H // 12
    # Outer ring (thin, dim)
    draw_arc(draw, cx, arc_cy, 210, 150, 390, DIM, 2)

    # Mid ring (styled like the HUD gauge)
    draw_arc(draw, cx, arc_cy, 180, 150, 390, BLUE_DIM, 4)

    # Animated-style partial arc (as if loading)
    draw_arc(draw, cx, arc_cy, 180, 150, 310, BLUE, 4)

    # Glow dot at arc tip
    arc_r = 180 - 2
    rad = math.radians(310)
    tx = cx + arc_r * math.cos(rad)
    ty = arc_cy + arc_r * math.sin(rad)
    add_glow(img, int(tx), int(ty), 25, BLUE, 0.5)
    draw = ImageDraw.Draw(img)
    draw.ellipse([tx - 4, ty - 4, tx + 4, ty + 4], fill=BLUE)

    # -- Project title --
    fnt_title = font(88, bold=True)
    center_text(draw, cx, cy - 15, "AI-HUD", fnt_title, WHITE)

    # -- Subtitle --
    fnt_sub = font_r(19, bold=True)
    center_text(draw, cx, cy + 55, "INTELLIGENT DRIVING ASSISTANT", fnt_sub, BLUE)

    # -- Bottom status dots (simulating system check) --
    dot_y = cy + 110 + H // 12
    labels = ["GPS", "CAM", "NPU", "LCD"]
    dot_spacing = 80
    start_x = cx - (len(labels) - 1) * dot_spacing // 2

    for i, label in enumerate(labels):
        dx = start_x + i * dot_spacing
        # Dot
        dot_color = ACCENT
        draw.ellipse([dx - 7, dot_y - 7, dx + 7, dot_y + 7], fill=dot_color)
        # Subtle glow
        add_glow(img, dx, dot_y, 20, dot_color, 0.3)
        draw = ImageDraw.Draw(img)
        # Label
        fnt_dot = font_r(18, bold=True)
        center_text(draw, dx, dot_y + 24, label, fnt_dot, (120, 128, 145))

    # -- Corner accents (matching HUD aesthetic) --
    ac = 30
    ac_color = (35, 40, 55)
    # Top-left
    draw.line([(12, 12), (12 + ac, 12)], fill=ac_color, width=2)
    draw.line([(12, 12), (12, 12 + ac)], fill=ac_color, width=2)
    # Top-right
    draw.line([(W - 12, 12), (W - 12 - ac, 12)], fill=ac_color, width=2)
    draw.line([(W - 12, 12), (W - 12, 12 + ac)], fill=ac_color, width=2)
    # Bottom-left
    draw.line([(12, H - 12), (12 + ac, H - 12)], fill=ac_color, width=2)
    draw.line([(12, H - 12), (12, H - 12 - ac)], fill=ac_color, width=2)
    # Bottom-right
    draw.line([(W - 12, H - 12), (W - 12 - ac, H - 12)], fill=ac_color, width=2)
    draw.line([(W - 12, H - 12), (W - 12, H - 12 - ac)], fill=ac_color, width=2)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mockups")
    os.makedirs(out, exist_ok=True)
    filename = f"{out}/splash_screen.png"
    img.save(filename, "PNG", quality=95)
    print(f"  -> {filename}")


if __name__ == "__main__":
    main()

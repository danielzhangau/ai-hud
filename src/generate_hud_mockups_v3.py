#!/usr/bin/env python3
"""HUD mockup v3 - Designed for real driving glanceability.

Design principle: 0.5 second glance through windshield reflection.
Only show what MATTERS. Speed huge, limit clear, alerts unmissable.
"""

import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 480, 480

BG = (8, 10, 15)
WHITE = (245, 248, 255)
RED = (255, 55, 60)
RED_DIM = (180, 35, 40)
AMBER = (255, 185, 35)
GREEN = (50, 220, 120)
GAUGE_BG = (30, 34, 45)
SIGN_RED = (215, 40, 40)
DIM = (55, 60, 75)

FONT = "/System/Library/Fonts/Avenir Next Condensed.ttc"
FONT_R = "/System/Library/Fonts/HelveticaNeue.ttc"


def f(size, bold=False):
    try:
        idx = 4 if bold else 0  # condensed bold vs regular
        return ImageFont.truetype(FONT, size, index=idx)
    except Exception:
        return ImageFont.load_default()


def fr(size, bold=False):
    try:
        idx = 1 if bold else 0
        return ImageFont.truetype(FONT_R, size, index=idx)
    except Exception:
        return ImageFont.load_default()


def center_text(draw, cx, cy, text, fnt, color):
    bbox = draw.textbbox((0, 0), text, font=fnt)
    # Use actual bbox midpoint for true visual centering
    draw.text(
        (cx - (bbox[0] + bbox[2]) // 2, cy - (bbox[1] + bbox[3]) // 2),
        text, fill=color, font=fnt,
    )


def draw_arc(draw, cx, cy, r, start, end, color, width):
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=start, end=end, fill=color, width=width)


def add_glow(img, cx, cy, radius, color, strength=0.35):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(radius, 0, -2):
        a = int(255 * strength * (1 - i / radius) ** 1.8)
        gd.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(*color, a))
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"))


def add_rect_glow(img, x1, y1, x2, y2, color, blur=20, strength=0.25):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([x1 - 10, y1 - 10, x2 + 10, y2 + 10], radius=20,
                          fill=(*color, int(255 * strength)))
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"))


def draw_speed_arc(img, draw, cx, cy, r, pct, color):
    """Minimal arc gauge - just the arc, no ticks."""
    start, end_full = 150, 390
    span = end_full - start

    # Track
    draw_arc(draw, cx, cy, r, start, end_full, GAUGE_BG, 10)

    # Active
    active_end = start + pct * span
    draw_arc(draw, cx, cy, r, start, active_end, color, 10)

    # Glow dot at tip (r - 5 to align with arc stroke center, width=10)
    rad = math.radians(active_end)
    arc_mid = r - 5
    tx = cx + arc_mid * math.cos(rad)
    ty = cy + arc_mid * math.sin(rad)
    add_glow(img, int(tx), int(ty), 30, color, 0.5)
    draw = ImageDraw.Draw(img)
    draw.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], fill=color)
    return draw


def draw_limit_sign(draw, cx, cy, r, text):
    """Big, clear Australian speed limit sign."""
    # Red outer ring
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SIGN_RED)
    # White inner
    ir = r - 5
    draw.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=(252, 252, 255))
    # Number - BIG
    fnt = f(int(r * 0.95), bold=True)
    center_text(draw, cx, cy - 2, text, fnt, (20, 20, 25))


def generate(filename, speed, limit, is_speeding=False, show_camera=False,
             camera_and_speeding=False):
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    main_color = RED if is_speeding else WHITE
    arc_color = RED if is_speeding else (60, 140, 255)
    max_speed = 160

    # -- Full-screen danger glow when speeding --
    if is_speeding:
        add_rect_glow(img, 0, 0, W, H, RED, blur=80, strength=0.08)
        draw = ImageDraw.Draw(img)

    # -- Arc gauge --
    arc_cx = W // 2
    arc_cy = H // 2
    arc_r = 200
    pct = min(speed / max_speed, 1.0)
    draw = draw_speed_arc(img, draw, arc_cx, arc_cy, arc_r, pct, arc_color)

    # -- SPEED (massive, center, shifted up to avoid overlap) --
    speed_str = str(speed)
    fnt_speed = f(160, bold=True)
    center_text(draw, arc_cx - 15, H // 2, speed_str, fnt_speed, main_color)

    # -- Speed limit sign (right side, clear of speed number) --
    sign_r = 52
    sign_cx = W - 65
    sign_cy = H // 2 + 10
    draw_limit_sign(draw, sign_cx, sign_cy, sign_r, str(limit))

    # -- Camera badge (top center, only when detected) --
    if show_camera:
        cam_color = RED if camera_and_speeding else AMBER

        # Glow behind badge
        add_glow(img, W // 2, 102, 60, cam_color, 0.25)
        draw = ImageDraw.Draw(img)

        # Pill badge - positioned below arc top, no overlap
        pw, ph = 160, 42
        bx = W // 2 - pw // 2
        by = 80
        draw.rounded_rectangle(
            [bx, by, bx + pw, by + ph], radius=21,
            fill=(cam_color[0] // 5, cam_color[1] // 5, cam_color[2] // 5),
            outline=cam_color, width=2,
        )

        # Camera icon
        ix = bx + 18
        iy = by + 12
        draw.rounded_rectangle([ix, iy, ix + 18, iy + 16], radius=3, fill=cam_color)
        draw.ellipse([ix + 5, iy + 3, ix + 14, iy + 12], fill=BG)
        draw.ellipse([ix + 7, iy + 5, ix + 12, iy + 10], fill=cam_color)

        # Label
        fnt_cam = fr(18, bold=True)
        draw.text((bx + 44, by + 10), "CAMERA", fill=cam_color, font=fnt_cam)

    # -- Alert bar (bottom, only when needed) --
    if is_speeding or show_camera:
        bar_h = 56
        bar_y = H - bar_h - 40

        if camera_and_speeding:
            alert_color = RED
            alert_text = "SLOW DOWN"
        elif is_speeding:
            alert_color = RED
            alert_text = "OVER SPEED"
        else:
            alert_color = AMBER
            alert_text = "CAMERA AHEAD"

        # Glow
        add_rect_glow(img, 20, bar_y, W - 20, bar_y + bar_h, alert_color, blur=25, strength=0.2)
        draw = ImageDraw.Draw(img)

        # Bar background
        draw.rounded_rectangle(
            [24, bar_y, W - 24, bar_y + bar_h], radius=14,
            fill=(alert_color[0] // 6, alert_color[1] // 6, alert_color[2] // 6),
            outline=alert_color, width=2,
        )

        # Alert text - BIG enough to read
        fnt_alert = f(28, bold=True)
        center_text(draw, W // 2, bar_y + bar_h // 2, alert_text, fnt_alert, alert_color)

    img.save(filename, "PNG", quality=95)
    print(f"  -> {filename}")


def main():
    import os
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mockups")
    os.makedirs(out, exist_ok=True)

    print("Generating HUD mockups v3 (glanceable design)...")

    # 1. Normal - clean, calm
    generate(f"{out}/hud_1_normal.png", speed=85, limit=100)

    # 2. Speeding - red, urgent
    generate(f"{out}/hud_2_speeding.png", speed=115, limit=100, is_speeding=True)

    # 3. Camera detected (not speeding)
    generate(f"{out}/hud_3_camera.png", speed=85, limit=80, show_camera=True)

    # 4. Camera + speeding (max danger)
    generate(f"{out}/hud_4_camera_speeding.png", speed=95, limit=80,
             is_speeding=True, show_camera=True, camera_and_speeding=True)

    print("Done!")


if __name__ == "__main__":
    main()

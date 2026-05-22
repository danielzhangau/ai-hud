#!/usr/bin/env python3
"""Render HUD mockup images for 480x480 canvas.

Generates PNG images showing the HUD display design,
matching the actual hud_live.py rendering layout (post-PiP removal).

Output (mockups/ directory):
  hud_1_normal.png          -- normal driving, under limit
  hud_2_speeding.png        -- over speed limit
  hud_3_camera.png          -- camera ahead, under limit
  hud_4_camera_speeding.png -- camera ahead + over limit

Usage:
  python tools/render_hud_mockup.py

Requirements: Pillow (pip install Pillow)
"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Layout constants (must match hud_live.py)
# ---------------------------------------------------------------------------
FB_W, FB_H = 480, 480
ARC_CX, ARC_CY = FB_W // 2, FB_H // 2
ARC_R = 200
ARC_WIDTH = 10
ARC_START = 150
ARC_END_FULL = 390
ARC_SPAN = ARC_END_FULL - ARC_START
MAX_SPEED = 160

SIGN_R = 52
SIGN_CX = FB_W - 65
SIGN_CY = FB_H // 2 + 10

# Colors (match hud_live.py dark theme)
COL_BG = (8, 10, 15)
COL_WHITE = (245, 248, 255)
COL_RED = (255, 55, 60)
COL_GREEN = (50, 220, 120)
COL_DIM = (55, 60, 75)
COL_ARC_BLUE = (60, 140, 255)
COL_ARC_RED = (255, 55, 60)
COL_GAUGE_BG = (30, 34, 45)
COL_LIMIT_RING = (215, 40, 40)
COL_LIMIT_BG = (252, 252, 255)
COL_LIMIT_TEXT = (20, 20, 25)
COL_AMBER = (255, 185, 35)

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def get_mono_font(size):
    """Get a monospace/digital-look font."""
    paths = [
        '/System/Library/Fonts/Menlo.ttc',
        '/System/Library/Fonts/Monaco.dfont',
        '/System/Library/Fonts/SFMono-Regular.otf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def draw_thick_arc(draw, cx, cy, r, start_deg, end_deg, color, width=10):
    """Draw a thick arc (Pillow arc with thick outline)."""
    bbox = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(bbox, start_deg, end_deg, fill=color, width=width)


def draw_speed_sign(draw, cx, cy, r, limit, fonts):
    """Draw the red-ring speed limit sign."""
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=COL_LIMIT_RING)
    draw.ellipse([cx-r+5, cy-r+5, cx+r-5, cy+r-5], fill=COL_LIMIT_BG)
    text = str(limit)
    font = fonts['limit']
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw//2 - bbox[0], cy - th//2 - bbox[1]),
              text, fill=COL_LIMIT_TEXT, font=font)


def draw_alert_bar(draw, text, color, fonts):
    """Draw the full-width alert bar at the bottom."""
    bar_x = 24
    bar_w = FB_W - bar_x * 2
    bar_h = 44
    bar_y = FB_H - bar_h - 68

    bg = (color[0]//6, color[1]//6, color[2]//6)
    draw.rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h], fill=bg)
    draw.rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h],
                   outline=color, width=2)
    font = fonts['alert']
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = bar_x + (bar_w - tw) // 2
    ty = bar_y + (bar_h - th) // 2
    draw.text((tx, ty), text, fill=color, font=font)


def draw_camera_badge(draw, text, color, fonts):
    """Draw the camera warning badge at top center."""
    badge_w, badge_h = 160, 36
    badge_x = ARC_CX - badge_w // 2
    badge_y = 82
    bg = (color[0]//5, color[1]//5, color[2]//5)
    draw.rectangle([badge_x, badge_y, badge_x+badge_w, badge_y+badge_h],
                   fill=bg)
    draw.rectangle([badge_x, badge_y, badge_x+badge_w, badge_y+badge_h],
                   outline=color, width=2)
    font = fonts['badge']
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = badge_x + (badge_w - tw) // 2
    ty = badge_y + (badge_h - th) // 2
    draw.text((tx, ty), text, fill=color, font=font)


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

def render_hud(speed, limit, over_limit, camera_ahead,
               satellites=8, gps_valid=True):
    """Render a complete HUD frame and return PIL Image."""

    img = Image.new('RGB', (FB_W, FB_H), COL_BG)
    draw = ImageDraw.Draw(img)

    fonts = {
        'speed': get_mono_font(120),
        'limit': get_mono_font(36),
        'unit': get_mono_font(14),
        'sat': get_mono_font(14),
        'alert': get_mono_font(18),
        'badge': get_mono_font(16),
    }

    # --- Background arc gauge ---
    draw_thick_arc(draw, ARC_CX, ARC_CY, ARC_R,
                   ARC_START, ARC_END_FULL, COL_GAUGE_BG, ARC_WIDTH)

    # --- Divider line (full width) ---
    draw.line([(20, FB_H - 55), (FB_W - 20, FB_H - 55)],
              fill=COL_DIM, width=1)

    # --- Speed limit sign ---
    draw_speed_sign(draw, SIGN_CX, SIGN_CY, SIGN_R, limit, fonts)

    # --- Active arc ---
    speed_color = COL_RED if over_limit else COL_WHITE
    arc_color = COL_ARC_RED if over_limit else COL_ARC_BLUE

    pct = min(speed / MAX_SPEED, 1.0)
    if pct > 0.005:
        active_end = ARC_START + pct * ARC_SPAN
        draw_thick_arc(draw, ARC_CX, ARC_CY, ARC_R,
                       ARC_START, active_end, arc_color, ARC_WIDTH)
        # Tip dot
        tip_rad = math.radians(active_end)
        tip_x = int(ARC_CX + ARC_R * math.cos(tip_rad))
        tip_y = int(ARC_CY + ARC_R * math.sin(tip_rad))
        draw.ellipse([tip_x-6, tip_y-6, tip_x+6, tip_y+6], fill=arc_color)

    # Re-draw sign on top (arc may overlap)
    draw_speed_sign(draw, SIGN_CX, SIGN_CY, SIGN_R, limit, fonts)

    # --- Speed number ---
    speed_str = str(speed)
    font_speed = fonts['speed']
    bbox = draw.textbbox((0, 0), speed_str, font=font_speed)
    sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    sx = ARC_CX - sw // 2 - 15
    sy = ARC_CY - sh // 2
    draw.text((sx - bbox[0], sy - bbox[1]), speed_str,
              fill=speed_color, font=font_speed)

    # --- km/h label ---
    font_unit = fonts['unit']
    ubbox = draw.textbbox((0, 0), "km/h", font=font_unit)
    uw = ubbox[2] - ubbox[0]
    ux = sx + (sw - uw) // 2
    uy = sy + sh + 6
    draw.text((ux, uy), "km/h", fill=COL_DIM, font=font_unit)

    # --- Alert bar (full width, no PiP cutoff) ---
    if over_limit or camera_ahead:
        if camera_ahead and over_limit:
            draw_alert_bar(draw, "SLOW DOWN", COL_RED, fonts)
        elif over_limit:
            draw_alert_bar(draw, "OVER SPEED", COL_RED, fonts)
        else:
            draw_alert_bar(draw, "CAMERA AHEAD", COL_AMBER, fonts)

    # --- Camera badge ---
    if camera_ahead:
        cam_color = COL_RED if over_limit else COL_AMBER
        draw_camera_badge(draw, "CAMERA", cam_color, fonts)

    # --- Satellite info ---
    sat_str = "SAT " + str(satellites)
    font_sat = fonts['sat']
    sat_color = COL_GREEN if gps_valid else COL_DIM
    draw.text((20, FB_H - 40), sat_str, fill=sat_color, font=font_sat)

    # GPS status dot
    sbbox = draw.textbbox((20, FB_H - 40), sat_str, font=font_sat)
    dot_x = sbbox[2] + 12
    dot_y = FB_H - 40 + (sbbox[3] - sbbox[1]) // 2
    dot_color = COL_GREEN if gps_valid else COL_RED
    draw.ellipse([dot_x-4, dot_y-4, dot_x+4, dot_y+4], fill=dot_color)

    return img


# ---------------------------------------------------------------------------
# Generate all mockup variants
# ---------------------------------------------------------------------------

def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'mockups')
    os.makedirs(out_dir, exist_ok=True)

    scenarios = [
        # (filename, speed, limit, over, camera, sats, gps)
        ('hud_1_normal.png',           85,  100, False, False, 10, True),
        ('hud_2_speeding.png',         115, 100, True,  False, 8,  True),
        ('hud_3_camera.png',           85,  80,  False, True,  8,  True),
        ('hud_4_camera_speeding.png',  95,  80,  True,  True,  8,  True),
    ]

    for fname, speed, limit, over, camera, sats, gps in scenarios:
        path = os.path.join(out_dir, fname)
        img = render_hud(speed, limit, over, camera, sats, gps)
        img.save(path)
        print(f"  {path}")

    print(f"\nGenerated {len(scenarios)} mockup images in {out_dir}/")


if __name__ == '__main__':
    main()

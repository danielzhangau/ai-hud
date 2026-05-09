#!/usr/bin/env python3
"""Render HUD mockup images for 480x480 canvas.

Generates PNG images showing the HUD display design,
matching the actual hud_live.py rendering layout.

Output:
  docs/hud_mockup_normal.png   -- normal driving
  docs/hud_mockup_over.png     -- over speed limit
  docs/hud_mockup_camera.png   -- camera ahead + over limit
  docs/hud_mockup_cam_ok.png   -- camera ahead, under limit

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

PIP_SIZE = 120
PIP_MARGIN = 10
PIP_X = FB_W - PIP_MARGIN - PIP_SIZE
PIP_Y = FB_H - PIP_MARGIN - PIP_SIZE

# Colors
COL_BG = (12, 12, 18)
COL_WHITE = (255, 255, 255)
COL_RED = (255, 60, 60)
COL_GREEN = (50, 220, 80)
COL_DIM = (80, 80, 100)
COL_ARC_BLUE = (40, 140, 255)
COL_ARC_RED = (255, 55, 60)
COL_GAUGE_BG = (35, 35, 50)
COL_LIMIT_RING = (220, 30, 30)
COL_LIMIT_BG = (240, 240, 245)
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
    # Red ring
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=COL_LIMIT_RING)
    # White inner
    draw.ellipse([cx-r+5, cy-r+5, cx+r-5, cy+r-5], fill=COL_LIMIT_BG)
    # Limit number
    text = str(limit)
    font = fonts['limit']
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw//2 - bbox[0], cy - th//2 - bbox[1]),
              text, fill=COL_LIMIT_TEXT, font=font)


def draw_pip_placeholder(draw, x, y, size):
    """Draw PiP camera view placeholder."""
    # Border
    draw.rectangle([x-1, y-1, x+size+1, y+size+1], outline=(60, 60, 80), width=2)
    # Dark fill simulating camera view
    draw.rectangle([x, y, x+size, y+size], fill=(20, 25, 30))
    # Road-like lines to simulate camera feed
    for i in range(3):
        ly = y + 40 + i * 25
        draw.line([(x+10, ly), (x+size-10, ly)], fill=(40, 50, 55), width=1)
    # Car-like shapes
    draw.rectangle([x+35, y+50, x+55, y+65], outline=(60, 80, 60), width=1)
    draw.rectangle([x+70, y+70, x+90, y+85], outline=(60, 80, 60), width=1)
    # "LIVE" label
    font_sm = get_mono_font(10)
    draw.text((x+4, y+4), "LIVE", fill=(200, 50, 50), font=font_sm)


def draw_alert_bar(draw, text, color, fonts, bar_x=24, bar_w=None):
    """Draw the alert bar at the bottom."""
    if bar_w is None:
        bar_w = PIP_X - bar_x - 10
    bar_h = 44
    bar_y = FB_H - bar_h - 68

    bg = (color[0]//6, color[1]//6, color[2]//6)
    draw.rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h], fill=bg)
    # Border
    draw.rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h],
                   outline=color, width=2)
    # Text centered
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
    draw.rectangle([badge_x, badge_y, badge_x+badge_w, badge_y+badge_h], fill=bg)
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

    # Load fonts
    fonts = {
        'speed': get_mono_font(120),
        'limit': get_mono_font(36),
        'unit': get_mono_font(14),
        'sat': get_mono_font(14),
        'alert': get_mono_font(18),
        'badge': get_mono_font(16),
        'hint': get_mono_font(12),
    }

    # --- Background arc gauge ---
    draw_thick_arc(draw, ARC_CX, ARC_CY, ARC_R,
                   ARC_START, ARC_END_FULL, COL_GAUGE_BG, ARC_WIDTH)

    # --- Divider line ---
    draw.line([(20, FB_H - 55), (PIP_X - 10, FB_H - 55)], fill=COL_DIM, width=1)

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

    # Re-draw sign on top (arc overlaps)
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

    # --- Alert bar ---
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

    # --- PiP placeholder ---
    draw_pip_placeholder(draw, PIP_X, PIP_Y, PIP_SIZE)

    return img


# ---------------------------------------------------------------------------
# Generate all mockup variants
# ---------------------------------------------------------------------------

def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'docs')
    os.makedirs(out_dir, exist_ok=True)

    scenarios = [
        # (filename, speed, limit, over, camera, sats, gps)
        ('hud_mockup_normal.png',   65,  100, False, False, 10, True),
        ('hud_mockup_over.png',     112, 100, True,  False, 8,  True),
        ('hud_mockup_camera.png',   108, 100, True,  True,  8,  True),
        ('hud_mockup_cam_ok.png',   85,  100, False, True,  7,  True),
    ]

    for fname, speed, limit, over, camera, sats, gps in scenarios:
        path = os.path.join(out_dir, fname)
        img = render_hud(speed, limit, over, camera, sats, gps)
        img.save(path)
        print(f"  {path}")

    print(f"\nGenerated {len(scenarios)} mockup images in {out_dir}/")


if __name__ == '__main__':
    main()

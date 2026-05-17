#!/usr/bin/env python3
"""Generate HUD mockup PNG images for the 4 driving scenarios.

Target: 480x480 pixels (matching the RGB LCD display)
Style: Dark background, high-contrast, automotive HUD aesthetic
"""

import math
from PIL import Image, ImageDraw, ImageFont

# -- Configuration --
W, H = 480, 480
BG_COLOR = (15, 15, 20)  # Near-black
WHITE = (255, 255, 255)
RED = (255, 50, 50)
AMBER = (255, 180, 30)
GREEN = (80, 220, 100)
GRAY = (100, 100, 110)
DIM_WHITE = (180, 180, 190)
DARK_GRAY = (40, 40, 50)
SPEED_LIMIT_RED = (220, 40, 40)

# Fonts
FONT_SPEED_LARGE = "/System/Library/Fonts/Avenir Next Condensed.ttc"
FONT_LABEL = "/System/Library/Fonts/Avenir Next.ttc"
FONT_MONO = "/System/Library/Fonts/Menlo.ttc"


def load_font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return ImageFont.load_default()


def draw_speed_limit_sign(draw, cx, cy, radius, limit_text, flashing=False):
    """Draw Australian-style speed limit sign (white circle with red border)."""
    border_color = SPEED_LIMIT_RED if not flashing else (255, 100, 100)
    border_width = 4

    # Outer red circle
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=None,
        outline=border_color,
        width=border_width,
    )
    # Inner white fill
    inner_r = radius - border_width
    draw.ellipse(
        [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
        fill=(255, 255, 255),
    )
    # Inner red ring
    ring_r = radius - 2
    draw.ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        fill=None,
        outline=SPEED_LIMIT_RED,
        width=5,
    )
    # White inner area
    fill_r = radius - 8
    draw.ellipse(
        [cx - fill_r, cy - fill_r, cx + fill_r, cy + fill_r],
        fill=(255, 255, 255),
    )

    # Speed number
    font = load_font(FONT_SPEED_LARGE, int(radius * 0.9), index=4)
    bbox = draw.textbbox((0, 0), limit_text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (cx - tw // 2, cy - th // 2 - 2),
        limit_text,
        fill=(30, 30, 30),
        font=font,
    )


def draw_camera_icon(draw, cx, cy, size, color):
    """Draw a simplified speed camera icon."""
    s = size
    # Camera body
    body_rect = [cx - s, cy - s * 0.5, cx + s * 0.6, cy + s * 0.7]
    draw.rounded_rectangle(body_rect, radius=4, fill=color)

    # Lens circle
    lens_cx = cx - s * 0.2
    lens_cy = cy + s * 0.1
    lens_r = s * 0.3
    draw.ellipse(
        [lens_cx - lens_r, lens_cy - lens_r, lens_cx + lens_r, lens_cy + lens_r],
        fill=BG_COLOR,
        outline=color,
        width=2,
    )
    # Inner lens
    inner_r = lens_r * 0.5
    draw.ellipse(
        [lens_cx - inner_r, lens_cy - inner_r, lens_cx + inner_r, lens_cy + inner_r],
        fill=color,
    )

    # Flash unit on top
    flash_rect = [cx - s * 0.3, cy - s * 0.8, cx + s * 0.1, cy - s * 0.5]
    draw.rounded_rectangle(flash_rect, radius=2, fill=color)

    # "CAMERA" label
    font = load_font(FONT_LABEL, 12)
    label = "CAMERA"
    bbox = draw.textbbox((0, 0), label, font=font)
    lw = bbox[2] - bbox[0]
    draw.text((cx - lw // 2, cy + s * 0.9), label, fill=color, font=font)


def draw_satellite_icon(draw, x, y, count, has_fix):
    """Draw satellite count indicator."""
    color = GREEN if has_fix else GRAY
    font = load_font(FONT_MONO, 14)
    text = f"SAT:{count}"
    draw.text((x, y), text, fill=color, font=font)

    # Small satellite symbol
    sx = x - 18
    sy = y + 2
    # Satellite body
    draw.rectangle([sx, sy + 2, sx + 8, sy + 8], fill=color)
    # Solar panels
    draw.line([(sx - 4, sy + 5), (sx, sy + 5)], fill=color, width=2)
    draw.line([(sx + 8, sy + 5), (sx + 12, sy + 5)], fill=color, width=2)


def draw_heading_compass(draw, cx, cy, heading, color):
    """Draw a small heading indicator."""
    r = 22
    # Circle
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=DARK_GRAY, width=1)

    # Cardinal directions
    font = load_font(FONT_MONO, 10)
    dirs = {"N": 0, "E": 90, "S": 180, "W": 270}
    for label, angle in dirs.items():
        rad = math.radians(angle - 90)
        tx = cx + (r + 10) * math.cos(rad)
        ty = cy + (r + 10) * math.sin(rad)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        c = DIM_WHITE if label != "N" else RED
        draw.text((tx - tw // 2, ty - th // 2), label, fill=c, font=font)

    # Heading arrow
    rad = math.radians(heading - 90)
    ex = cx + (r - 4) * math.cos(rad)
    ey = cy + (r - 4) * math.sin(rad)
    draw.line([(cx, cy), (ex, ey)], fill=color, width=2)
    # Arrow dot
    draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=color)


def draw_alert_bar(draw, y, text, color, bg_color=None):
    """Draw an alert banner at the bottom."""
    if bg_color is None:
        bg_color = (*color[:3], 40)
    # Semi-transparent bar
    bar_h = 36
    draw.rectangle([0, y, W, y + bar_h], fill=(color[0] // 4, color[1] // 4, color[2] // 4))
    # Border
    draw.line([(0, y), (W, y)], fill=color, width=2)
    # Text
    font = load_font(FONT_LABEL, 16, index=1)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(
        (W // 2 - tw // 2, y + (bar_h - 18) // 2),
        text,
        fill=color,
        font=font,
    )


def draw_status_bar(draw, time_str="12:34", satellites=8, has_fix=True):
    """Draw top status bar."""
    # Dim horizontal line
    draw.line([(10, 28), (W - 10, 28)], fill=DARK_GRAY, width=1)

    # Time
    font = load_font(FONT_MONO, 14)
    draw.text((W - 70, 8), time_str, fill=DIM_WHITE, font=font)

    # Satellite
    draw_satellite_icon(draw, 40, 8, satellites, has_fix)


def generate_hud(
    filename,
    speed,
    speed_color,
    limit,
    heading,
    alert_text=None,
    alert_color=None,
    show_camera=False,
    camera_color=AMBER,
    satellites=8,
    has_fix=True,
    limit_flashing=False,
):
    """Generate a single HUD mockup."""
    img = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Status bar
    draw_status_bar(draw, "14:32", satellites, has_fix)

    # -- Main speed display (center-left area) --
    speed_str = str(speed)
    font_speed = load_font(FONT_SPEED_LARGE, 120, index=4)
    font_unit = load_font(FONT_LABEL, 18)

    bbox = draw.textbbox((0, 0), speed_str, font=font_speed)
    sw = bbox[2] - bbox[0]
    sh = bbox[3] - bbox[1]

    speed_x = 40
    speed_y = 120

    draw.text((speed_x, speed_y), speed_str, fill=speed_color, font=font_speed)

    # Unit "km/h"
    unit_y = speed_y + sh + 5
    draw.text((speed_x + 5, unit_y), "km/h", fill=GRAY, font=font_unit)

    # -- Speed limit sign (right side) --
    sign_cx = W - 90
    sign_cy = 180
    sign_r = 48
    draw_speed_limit_sign(draw, sign_cx, sign_cy, sign_r, str(limit), limit_flashing)

    # "SPEED LIMIT" label above sign
    font_small = load_font(FONT_LABEL, 11)
    label = "SPEED LIMIT"
    bbox = draw.textbbox((0, 0), label, font=font_small)
    lw = bbox[2] - bbox[0]
    draw.text((sign_cx - lw // 2, sign_cy - sign_r - 18), label, fill=GRAY, font=font_small)

    # -- Camera icon (top right, if applicable) --
    if show_camera:
        draw_camera_icon(draw, W - 80, 70, 20, camera_color)

    # -- Heading compass (bottom right) --
    draw_heading_compass(draw, W - 70, H - 100, heading, DIM_WHITE)

    # Heading text
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(heading / 45) % 8
    heading_label = f"{directions[idx]} {heading:.0f}"
    font_heading = load_font(FONT_MONO, 13)
    bbox = draw.textbbox((0, 0), heading_label, font=font_heading)
    hw = bbox[2] - bbox[0]
    draw.text((W - 70 - hw // 2, H - 65), heading_label, fill=DIM_WHITE, font=font_heading)

    # -- Bottom info bar --
    draw.line([(10, H - 45), (W - 10, H - 45)], fill=DARK_GRAY, width=1)

    font_info = load_font(FONT_MONO, 12)
    if has_fix:
        info = "GPS: ACTIVE"
        draw.text((15, H - 38), info, fill=GREEN, font=font_info)
    else:
        info = "GPS: SEARCHING..."
        draw.text((15, H - 38), info, fill=AMBER, font=font_info)

    # -- Alert bar (if applicable) --
    if alert_text and alert_color:
        draw_alert_bar(draw, H - 82, alert_text, alert_color)

    # -- Decorative elements --
    # Corner accents
    accent_len = 20
    accent_color = (50, 50, 60)
    # Top-left
    draw.line([(5, 5), (5 + accent_len, 5)], fill=accent_color, width=1)
    draw.line([(5, 5), (5, 5 + accent_len)], fill=accent_color, width=1)
    # Top-right
    draw.line([(W - 5, 5), (W - 5 - accent_len, 5)], fill=accent_color, width=1)
    draw.line([(W - 5, 5), (W - 5, 5 + accent_len)], fill=accent_color, width=1)
    # Bottom-left
    draw.line([(5, H - 5), (5 + accent_len, H - 5)], fill=accent_color, width=1)
    draw.line([(5, H - 5), (5, H - 5 - accent_len)], fill=accent_color, width=1)
    # Bottom-right
    draw.line([(W - 5, H - 5), (W - 5 - accent_len, H - 5)], fill=accent_color, width=1)
    draw.line([(W - 5, H - 5), (W - 5, H - 5 - accent_len)], fill=accent_color, width=1)

    img.save(filename, "PNG")
    print(f"Saved: {filename}")


def main():
    output_dir = "/Users/boshengzhang/Desktop/hub/mockups"
    import os
    os.makedirs(output_dir, exist_ok=True)

    # Scene 1: Normal driving (not speeding)
    generate_hud(
        f"{output_dir}/hud_1_normal.png",
        speed=85,
        speed_color=WHITE,
        limit=100,
        heading=45,
        satellites=8,
    )

    # Scene 2: Speeding alert
    generate_hud(
        f"{output_dir}/hud_2_speeding.png",
        speed=115,
        speed_color=RED,
        limit=100,
        heading=45,
        alert_text="!! SPEED WARNING - REDUCE SPEED !!",
        alert_color=RED,
        limit_flashing=True,
        satellites=8,
    )

    # Scene 3: Speed camera detected (not speeding)
    generate_hud(
        f"{output_dir}/hud_3_camera.png",
        speed=85,
        speed_color=WHITE,
        limit=80,
        heading=190,
        alert_text="SPEED CAMERA AHEAD",
        alert_color=AMBER,
        show_camera=True,
        camera_color=AMBER,
        satellites=6,
    )

    # Scene 4: Camera + speeding (highest danger)
    generate_hud(
        f"{output_dir}/hud_4_camera_speeding.png",
        speed=95,
        speed_color=RED,
        limit=80,
        heading=190,
        alert_text="!! CAMERA + SPEEDING - SLOW DOWN NOW !!",
        alert_color=RED,
        show_camera=True,
        camera_color=RED,
        limit_flashing=True,
        satellites=6,
    )

    print(f"\nAll mockups saved to: {output_dir}/")


if __name__ == "__main__":
    main()

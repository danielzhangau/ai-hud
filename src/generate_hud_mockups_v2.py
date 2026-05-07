#!/usr/bin/env python3
"""Generate modern HUD mockup PNGs - 2026 automotive UI aesthetic.

Design language: Arc gauges, glassmorphism, glow effects, clean typography.
Inspired by Tesla/Rivian/Apple CarPlay modern dashboards.
"""

import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 480, 480

# -- Modern color palette --
BG_DARK = (10, 12, 18)
CARD_BG = (22, 26, 35)
CARD_BORDER = (45, 50, 65)
TEXT_PRIMARY = (240, 242, 250)
TEXT_SECONDARY = (120, 128, 145)
TEXT_DIM = (70, 75, 90)
ACCENT_BLUE = (60, 140, 255)
ACCENT_GREEN = (50, 215, 130)
ACCENT_RED = (255, 65, 75)
ACCENT_AMBER = (255, 185, 40)
ACCENT_RED_GLOW = (255, 40, 60)
ACCENT_AMBER_GLOW = (255, 170, 20)
GAUGE_TRACK = (35, 40, 55)
SIGN_RED = (220, 45, 45)

FONT_MAIN = "/System/Library/Fonts/HelveticaNeue.ttc"
FONT_CONDENSED = "/System/Library/Fonts/Avenir Next Condensed.ttc"
FONT_MONO = "/System/Library/Fonts/SFNSMono.ttf"


def font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return ImageFont.load_default()


def draw_arc(draw, cx, cy, radius, start_deg, end_deg, color, width=4):
    """Draw an arc segment."""
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.arc(bbox, start=start_deg, end=end_deg, fill=color, width=width)


def draw_glow_circle(img, cx, cy, radius, color, intensity=0.4):
    """Draw a soft glow around a point."""
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(radius, 0, -1):
        alpha = int(255 * intensity * (1 - i / radius) ** 2)
        c = (*color, alpha)
        gd.ellipse([cx - i, cy - i, cx + i, cy + i], fill=c)
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 3))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"))


def draw_glow_rect(img, x1, y1, x2, y2, color, blur=15):
    """Draw a glowing rectangle overlay."""
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([x1, y1, x2, y2], radius=12, fill=(*color, 30))
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    base = img.convert("RGBA")
    img.paste(Image.alpha_composite(base, glow).convert("RGB"))


def draw_rounded_card(draw, x1, y1, x2, y2, radius=14):
    """Draw a glassmorphic card background."""
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=CARD_BG, outline=CARD_BORDER, width=1)


def draw_speed_arc(img, draw, cx, cy, radius, speed, max_speed, color, glow_color):
    """Draw the main speed arc gauge."""
    arc_start = 135
    arc_end = 405
    arc_range = arc_end - arc_start

    # Background track
    draw_arc(draw, cx, cy, radius, arc_start, arc_end, GAUGE_TRACK, width=8)

    # Tick marks
    for i in range(0, max_speed + 1, 20):
        angle = arc_start + (i / max_speed) * arc_range
        rad = math.radians(angle)
        inner = radius - 14
        outer = radius - 4
        x1 = cx + inner * math.cos(rad)
        y1 = cy + inner * math.sin(rad)
        x2 = cx + outer * math.cos(rad)
        y2 = cy + outer * math.sin(rad)
        tick_color = TEXT_DIM if i <= speed else (30, 33, 42)
        draw.line([(x1, y1), (x2, y2)], fill=tick_color, width=2)

    # Active arc
    progress = min(speed / max_speed, 1.0)
    active_end = arc_start + progress * arc_range
    draw_arc(draw, cx, cy, radius, arc_start, active_end, color, width=8)

    # Glow at the arc tip
    tip_rad = math.radians(active_end)
    tip_x = cx + radius * math.cos(tip_rad)
    tip_y = cy + radius * math.sin(tip_rad)
    draw_glow_circle(img, int(tip_x), int(tip_y), 25, glow_color, 0.5)

    # Bright dot at tip
    draw.ellipse([tip_x - 5, tip_y - 5, tip_x + 5, tip_y + 5], fill=color)


def draw_speed_text(draw, cx, cy, speed, color):
    """Draw the big speed number in the center of the arc."""
    speed_str = str(speed)
    f_speed = font(FONT_CONDENSED, 88, index=4)
    f_unit = font(FONT_MAIN, 14, index=1)

    bbox = draw.textbbox((0, 0), speed_str, font=f_speed)
    sw = bbox[2] - bbox[0]
    sh = bbox[3] - bbox[1]
    draw.text((cx - sw // 2, cy - sh // 2 - 10), speed_str, fill=color, font=f_speed)

    # "km/h" unit
    ubbox = draw.textbbox((0, 0), "km/h", font=f_unit)
    uw = ubbox[2] - ubbox[0]
    draw.text((cx - uw // 2, cy + sh // 2 - 12), "km/h", fill=TEXT_SECONDARY, font=f_unit)


def draw_limit_sign(draw, cx, cy, r, limit_str):
    """Modern Australian speed limit sign."""
    # Outer red ring
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SIGN_RED)
    # Inner white
    inner = r - 4
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=(250, 250, 252))
    # Number
    f = font(FONT_CONDENSED, int(r * 0.85), index=4)
    bbox = draw.textbbox((0, 0), limit_str, font=f)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((cx - tw // 2, cy - th // 2 - 1), limit_str, fill=(25, 25, 30), font=f)


def draw_info_pill(draw, cx, cy, label, value, color):
    """Draw a small pill-shaped info element."""
    f_label = font(FONT_MAIN, 10, index=1)
    f_value = font(FONT_MONO, 13)

    # Label
    lbbox = draw.textbbox((0, 0), label, font=f_label)
    lw = lbbox[2] - lbbox[0]
    draw.text((cx - lw // 2, cy - 10), label, fill=TEXT_DIM, font=f_label)

    # Value
    vbbox = draw.textbbox((0, 0), value, font=f_value)
    vw = vbbox[2] - vbbox[0]
    draw.text((cx - vw // 2, cy + 4), value, fill=color, font=f_value)


def draw_camera_badge(img, draw, x, y, color, glow=True):
    """Modern camera detection badge."""
    # Glow
    if glow:
        draw_glow_circle(img, x + 16, y + 14, 30, color, 0.3)
        draw = ImageDraw.Draw(img)

    # Pill background
    pw, ph = 95, 30
    draw.rounded_rectangle(
        [x, y, x + pw, y + ph], radius=15,
        fill=(color[0] // 6, color[1] // 6, color[2] // 6),
        outline=color, width=1,
    )

    # Camera icon (simplified)
    ix = x + 12
    iy = y + 9
    # Body
    draw.rounded_rectangle([ix, iy, ix + 14, iy + 11], radius=2, fill=color)
    # Lens
    draw.ellipse([ix + 4, iy + 2, ix + 11, iy + 9], fill=(color[0] // 6, color[1] // 6, color[2] // 6))
    draw.ellipse([ix + 6, iy + 4, ix + 9, iy + 7], fill=color)

    # Text
    f = font(FONT_MAIN, 11, index=1)
    draw.text((x + 30, y + 8), "CAMERA", fill=color, font=f)

    return draw


def draw_alert_banner(img, draw, y, text, color):
    """Modern alert banner with glow."""
    # Glow behind banner
    draw_glow_rect(img, 15, y - 5, W - 15, y + 40, color, blur=20)
    draw = ImageDraw.Draw(img)

    # Banner
    draw.rounded_rectangle(
        [20, y, W - 20, y + 36], radius=10,
        fill=(color[0] // 8, color[1] // 8, color[2] // 8),
        outline=(*color, 180), width=1,
    )

    # Pulsing dot
    draw.ellipse([32, y + 13, 42, y + 23], fill=color)

    # Text
    f = font(FONT_MAIN, 13, index=1)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    draw.text((W // 2 - tw // 2 + 8, y + 9), text, fill=color, font=f)

    return draw


def generate_hud_v2(
    filename,
    speed,
    limit,
    heading_deg,
    heading_str,
    is_speeding=False,
    show_camera=False,
    alert_text=None,
    alert_color=None,
    satellites=8,
):
    """Generate a single modern HUD mockup."""
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    # -- Color setup --
    speed_color = ACCENT_RED if is_speeding else TEXT_PRIMARY
    glow_color = ACCENT_RED_GLOW if is_speeding else ACCENT_BLUE
    arc_color = ACCENT_RED if is_speeding else ACCENT_BLUE

    # -- Speed arc gauge (center) --
    arc_cx, arc_cy = W // 2 - 20, H // 2 - 30
    arc_r = 155
    draw_speed_arc(img, draw, arc_cx, arc_cy, arc_r, speed, 160, arc_color, glow_color)
    draw = ImageDraw.Draw(img)

    # Speed text in center
    draw_speed_text(draw, arc_cx, arc_cy, speed, speed_color)

    # -- Speed limit sign (right of arc) --
    sign_x = W - 62
    sign_y = H // 2 - 55
    draw_limit_sign(draw, sign_x, sign_y, 35, str(limit))

    # "LIMIT" label
    f_tiny = font(FONT_MAIN, 9, index=1)
    lbbox = draw.textbbox((0, 0), "LIMIT", font=f_tiny)
    lw = lbbox[2] - lbbox[0]
    draw.text((sign_x - lw // 2, sign_y + 40), "LIMIT", fill=TEXT_DIM, font=f_tiny)

    # -- Camera badge (top area) --
    if show_camera:
        cam_color = ACCENT_RED if is_speeding else ACCENT_AMBER
        draw = draw_camera_badge(img, draw, W // 2 - 47, 18, cam_color)

    # -- Top status bar --
    f_status = font(FONT_MONO, 11)

    # Time
    draw.text((W - 52, 8), "14:32", fill=TEXT_SECONDARY, font=f_status)

    # Satellite indicator
    sat_color = ACCENT_GREEN if satellites >= 4 else ACCENT_AMBER
    draw.ellipse([18, 12, 24, 18], fill=sat_color)
    draw.text((28, 7), f"{satellites} SAT", fill=TEXT_SECONDARY, font=f_status)

    # -- Bottom info cards --
    card_y = H - 90
    card_h = 52

    # Heading card
    draw_rounded_card(draw, 18, card_y, 155, card_y + card_h)
    draw_info_pill(draw, 86, card_y + 10, "HEADING", f"{heading_str} {heading_deg}", TEXT_PRIMARY)

    # Altitude/GPS card
    draw_rounded_card(draw, 165, card_y, 310, card_y + card_h)
    draw_info_pill(draw, 237, card_y + 10, "GPS STATUS", "ACTIVE", ACCENT_GREEN)

    # Speed delta card
    delta = speed - limit
    if delta > 0:
        delta_str = f"+{delta}"
        delta_color = ACCENT_RED
    else:
        delta_str = f"{delta}"
        delta_color = ACCENT_GREEN
    draw_rounded_card(draw, 320, card_y, W - 18, card_y + card_h)
    draw_info_pill(draw, (320 + W - 18) // 2, card_y + 10, "DELTA", delta_str, delta_color)

    # -- Alert banner --
    if alert_text and alert_color:
        draw = draw_alert_banner(img, draw, H - 148, alert_text, alert_color)

    # -- Subtle top/bottom edge lines --
    draw.line([(30, H - 30), (W - 30, H - 30)], fill=(30, 35, 48), width=1)

    img.save(filename, "PNG", quality=95)
    print(f"  -> {filename}")


def main():
    import os
    out = "/Users/boshengzhang/Desktop/hub/mockups"
    os.makedirs(out, exist_ok=True)

    print("Generating modern HUD mockups...")

    # Scene 1: Normal
    generate_hud_v2(
        f"{out}/hud_1_normal.png",
        speed=85, limit=100, heading_deg=45, heading_str="NE",
        satellites=8,
    )

    # Scene 2: Speeding
    generate_hud_v2(
        f"{out}/hud_2_speeding.png",
        speed=115, limit=100, heading_deg=45, heading_str="NE",
        is_speeding=True,
        alert_text="REDUCE SPEED", alert_color=ACCENT_RED,
        satellites=8,
    )

    # Scene 3: Camera detected
    generate_hud_v2(
        f"{out}/hud_3_camera.png",
        speed=85, limit=80, heading_deg=190, heading_str="S",
        show_camera=True,
        alert_text="SPEED CAMERA AHEAD", alert_color=ACCENT_AMBER,
        satellites=6,
    )

    # Scene 4: Camera + speeding
    generate_hud_v2(
        f"{out}/hud_4_camera_speeding.png",
        speed=95, limit=80, heading_deg=190, heading_str="S",
        is_speeding=True, show_camera=True,
        alert_text="CAMERA AHEAD - SLOW DOWN", alert_color=ACCENT_RED,
        satellites=6,
    )

    print(f"\nDone! Files in: {out}/")


if __name__ == "__main__":
    main()

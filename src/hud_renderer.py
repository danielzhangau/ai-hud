"""HUD rendering - speed gauge, limit sign, alerts, satellite info.

Renders the full HUD frame onto a Framebuffer instance using cached
base layers for efficient delta rendering on ARM Cortex-A7.
"""

import math

from bitmap_font import GLYPH_H
from framebuffer import FB_W, FB_H

# ---------------------------------------------------------------------------
# Colors (R, G, B) - matching mockup v3
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Arc gauge constants
# ---------------------------------------------------------------------------

ARC_CX = FB_W // 2
ARC_CY = FB_H // 2
ARC_R = 200
ARC_WIDTH = 10
ARC_START = 150   # degrees
ARC_END_FULL = 390  # degrees
ARC_SPAN = ARC_END_FULL - ARC_START  # 240 degrees
MAX_SPEED = 160

# ---------------------------------------------------------------------------
# Limit sign constants
# ---------------------------------------------------------------------------

SIGN_R = 52
SIGN_CX = FB_W - 65
SIGN_CY = FB_H // 2 + 10


def _build_base_layer(fb, speed_limit, low_confidence=False):
    """Pre-render the static background layer: bg + arc track + limit sign.

    Call once at startup or when speed_limit changes.  Returns a snapshot
    (bytearray copy) of the buffer that can be blitted back each frame
    instead of redrawing.

    When `low_confidence` is True we keep the sign chrome but render
    "--" instead of a number -- the DB hit was single-source / OSM-only
    and the project's "宁可不报" policy forbids displaying a value the
    cross-verify pipeline didn't elevate to OFFICIAL_ONLY or higher.
    """
    fb.clear(COL_BG)

    # Arc gauge background track
    fb.draw_thick_arc(ARC_CX, ARC_CY, ARC_R, ARC_START, ARC_END_FULL,
                      COL_GAUGE_BG, width=ARC_WIDTH)

    # Speed limit sign - red ring + white fill + number
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R, COL_LIMIT_RING)
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R - 5, COL_LIMIT_BG)

    limit_str = "--" if low_confidence else str(speed_limit)
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
                 'camera_detected', 'limit_low_confidence')

    def __init__(self):
        self.speed_int = -1
        self.over_limit = False
        self.valid = False
        self.satellites = -1
        self.fix_quality = -1
        self.base_layer = None
        self.speed_limit = -1
        self.camera_detected = False
        # Tracks the renderer-visible confidence flag so a toggle between
        # "show number" and "show --" forces a base-layer rebuild even
        # when the numeric limit hasn't changed.
        self.limit_low_confidence = False


def render_hud(fb, gps, state, detect, default_speed_limit):
    """Render the full HUD frame - mockup v3 style.

    Uses cached base layer via *state* (HUDState) and skips rendering
    when nothing changed.

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
    current_limit = detect.speed_limit if detect else default_speed_limit
    camera = detect.camera_detected if detect else False
    # low_conf = True means we don't trust the numeric limit enough to
    # surface it. Triggered by DB_LOW_CONFIDENCE (single-source OSM),
    # SOURCE_DEFAULT (no DB hit + no NPU detection) or GPS unfixed.
    # Falls back to False so callers without DetectionState still work.
    low_conf = bool(getattr(detect, "limit_low_confidence", False)
                    if detect else False)

    # Over-limit hint suppressed when low_conf -- if we're showing "--"
    # we cannot honestly claim the driver is over any limit. Otherwise
    # render speed numeric against current_limit (DB or NPU vote).
    over_limit = (not low_conf) and speed_int > current_limit

    # --- Delta check: skip full redraw if nothing changed ---
    limit_changed = (state.speed_limit != current_limit
                     or state.limit_low_confidence != low_conf)
    needs_rebuild = limit_changed or state.base_layer is None
    if (not needs_rebuild and
            state.speed_int == speed_int and
            state.over_limit == over_limit and
            state.valid == gps.valid and
            state.satellites == gps.satellites and
            state.fix_quality == gps.fix_quality and
            state.camera_detected == camera):
        return  # nothing changed, skip render

    # Rebuild base layer if speed limit changed or invalidated
    if needs_rebuild:
        state.base_layer = _build_base_layer(fb, current_limit,
                                             low_confidence=low_conf)
    fb.buf[:] = state.base_layer

    state.speed_int = speed_int
    state.over_limit = over_limit
    state.valid = gps.valid
    state.satellites = gps.satellites
    state.fix_quality = gps.fix_quality
    state.speed_limit = current_limit
    state.camera_detected = camera
    state.limit_low_confidence = low_conf

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
    # Must mirror _build_base_layer's "--" rule, otherwise this pass would
    # paint the numeric default back over the "--" the base layer drew.
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R, COL_LIMIT_RING)
    fb.fill_circle(SIGN_CX, SIGN_CY, SIGN_R - 5, COL_LIMIT_BG)
    _limit_str = "--" if low_conf else str(current_limit)
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

    fb.flush()

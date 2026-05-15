"""Settings UI overlay for ai-hud, rendered to framebuffer via touch.

Full-screen overlay that replaces HUD when active. Uses the same
Framebuffer class from hud_live.py for all drawing.

Layout (480x480):
  - Top bar (0-48):   title + back/close buttons with accent underline
  - Items (52-430):   list of settings with description subtitles
  - Status bar (440-480): version and region

Pages:
  - "main":   display mode, mirror toggle, NPU toggle, region, fusion entry
  - "fusion": NPU fusion parameters with +/- controls and descriptions

No third-party dependencies.
"""

import os
import time

# ---------------------------------------------------------------------------
# Colors (dark theme, refined)
# ---------------------------------------------------------------------------

COL_BG = (8, 10, 15)
COL_HEADER = (14, 17, 24)
COL_ROW_A = (12, 15, 21)
COL_ROW_B = (16, 19, 27)
COL_SEP = (28, 32, 42)
COL_WHITE = (235, 240, 252)
COL_DIM = (80, 88, 108)
COL_DESC = (65, 72, 92)
COL_ACCENT = (55, 135, 255)
COL_GREEN = (45, 210, 110)
COL_RED = (245, 55, 60)
COL_TOGGLE_ON = (40, 190, 100)
COL_TOGGLE_OFF = (50, 55, 68)
COL_TOGGLE_KNOB = (230, 235, 245)
COL_BTN = (30, 35, 48)
COL_BTN_BORDER = (45, 50, 65)
COL_BTN_DANGER = (100, 25, 30)
COL_BTN_DANGER_BORDER = (160, 40, 45)

# Layout constants
SCREEN_W = 480
SCREEN_H = 480
TOP_BAR_H = 48
ACCENT_LINE_Y = TOP_BAR_H - 2
CONTENT_Y = TOP_BAR_H + 6
STATUS_Y = 442
LABEL_X = 24
VALUE_X = 310
MAIN_ROW_H = 60
FUSION_ROW_H = 66


class SettingsUI:
    """Touch-driven settings overlay."""

    def __init__(self, fb, config, region_mgr, app_version="0.0.0"):
        """Initialize settings UI.

        Args:
            fb: Framebuffer instance (from hud_live.py)
            config: ConfigManager instance
            region_mgr: RegionManager instance
            app_version: Version string displayed in status bar
        """
        self.fb = fb
        self.config = config
        self.region_mgr = region_mgr
        self.app_version = app_version

        self.active = False
        self.page = "main"

        # Callbacks set by hud_live.py integration
        self.on_npu_toggle = None          # fn(enabled: bool)
        self.on_region_change = None       # fn(region: str)
        self.on_fusion_reload = None       # fn()
        self.on_display_mode_change = None # fn(mode: str)  "hud" or "cam"
        self.on_mirror_change = None      # fn(enabled: bool)
        self.on_night_mode_change = None  # fn(enabled: bool)

        # Page definitions
        self._main_items = [
            {"key": "display_mode",   "label": "Display",        "type": "choice",
             "choices": ["hud", "cam"], "display": {"hud": "HUD", "cam": "CAM"}},
            {"key": "mirror_display", "label": "Mirror",         "type": "toggle"},
            {"key": "npu_enabled",    "label": "NPU Detection",  "type": "toggle"},
            {"key": "night_mode",     "label": "Night Mode",      "type": "toggle"},
            {"key": "region",         "label": "Region",          "type": "choice",
             "choices": ["au", "cn"], "display": {"au": "AU", "cn": "CN"}},
            {"key": "_fusion",        "label": "Fusion Params",   "type": "submenu"},
        ]

        self._fusion_items = [
            {"key": "npu_confidence_min",   "label": "NPU Confidence",
             "desc": "min trust level (has data)",
             "type": "float", "min": 0.10, "max": 1.00, "step": 0.05, "fmt": ".2f"},
            {"key": "npu_confidence_no_db", "label": "NPU Confidence",
             "desc": "min trust level (no data)",
             "type": "float", "min": 0.10, "max": 1.00, "step": 0.05, "fmt": ".2f"},
            {"key": "npu_vote_required",    "label": "Confirm Count",
             "desc": "consecutive detections needed",
             "type": "int",   "min": 1,    "max": 10,   "step": 1,    "fmt": "d"},
            {"key": "npu_override_timeout", "label": "NPU Timeout",
             "desc": "seconds until override resets",
             "type": "float", "min": 5.0,  "max": 120.0, "step": 5.0, "fmt": ".0f"},
            {"key": "camera_alert_radius",  "label": "Camera Range",
             "desc": "alert radius in meters",
             "type": "int",   "min": 100,  "max": 2000, "step": 100,  "fmt": "d"},
            {"key": "_reset",               "label": "Reset Defaults",
             "type": "button_danger"},
        ]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    # IPC file to pause C-side camera rendering while settings is visible
    _PIP_HIDE_IPC = "/tmp/ai_hud_pip_hide"

    _INACTIVITY_TIMEOUT = 30  # seconds: auto-close if no touch

    def activate(self):
        """Enter settings overlay."""
        self.active = True
        self.page = "main"
        self._last_interaction = time.time()
        # Signal C binary to pause camera rendering
        try:
            with open(self._PIP_HIDE_IPC, "w") as f:
                f.write("1")
        except OSError:
            pass
        self.render()

    def deactivate(self):
        """Exit settings, return to HUD."""
        self.active = False
        # Resume camera rendering
        try:
            os.unlink(self._PIP_HIDE_IPC)
        except OSError:
            pass

    def check_timeout(self):
        """Auto-close settings after inactivity. Call from main loop."""
        if not self.active:
            return False
        if time.time() - self._last_interaction > self._INACTIVITY_TIMEOUT:
            print("[SETTINGS] inactivity timeout, auto-closing")
            self.deactivate()
            return True  # caller should restore display
        return False

    def handle_touch(self, event):
        """Process a TouchEvent. Returns True if event was consumed."""
        if not self.active:
            return False
        self._last_interaction = time.time()

        if event.gesture != "tap":
            if event.gesture == "swipe_right" and self.page == "fusion":
                self.page = "main"
                self.render()
                return True
            return True  # consume but ignore other gestures

        x, y = event.x, event.y

        # Close button (top-right corner)
        if y < TOP_BAR_H and x > SCREEN_W - 60:
            self.deactivate()
            return True

        # Back button (top-left, fusion page only)
        if y < TOP_BAR_H and x < 60 and self.page == "fusion":
            self.page = "main"
            self.render()
            return True

        # Content area
        if y < CONTENT_Y or y > STATUS_Y:
            return True  # tap in non-interactive area

        row_h = FUSION_ROW_H if self.page == "fusion" else MAIN_ROW_H
        row_index = (y - CONTENT_Y) // row_h
        items = self._main_items if self.page == "main" else self._fusion_items

        if row_index < 0 or row_index >= len(items):
            return True

        item = items[row_index]
        self._handle_item_tap(item, x)
        self.render()
        return True

    def render(self):
        """Full-screen render of settings page.

        Settings UI is always rendered without mirror -- the user interacts
        with the screen directly (not through windshield reflection), so
        text and touch coordinates must stay in normal orientation.
        """
        fb = self.fb
        saved_mirror = fb.mirror
        fb.mirror = False

        fb.clear(COL_BG)

        if self.page == "main":
            self._render_top_bar("SETTINGS", show_back=False)
            self._render_main_items()
        elif self.page == "fusion":
            self._render_top_bar("FUSION PARAMS", show_back=True)
            self._render_fusion_items()

        # Status bar
        region = self.region_mgr.region.upper()
        fb.draw_text(f"v{self.app_version} | {region}", LABEL_X, STATUS_Y + 8, COL_DIM, scale=1)

        fb.flush()
        fb.mirror = saved_mirror

    # -----------------------------------------------------------------------
    # Top bar
    # -----------------------------------------------------------------------

    def _render_top_bar(self, title, show_back=False):
        """Draw header with title, accent underline, and close button."""
        fb = self.fb
        fb.fill_rect(0, 0, SCREEN_W, TOP_BAR_H, COL_HEADER)

        # Accent underline (2px)
        fb.fill_rect(0, ACCENT_LINE_Y, SCREEN_W, 2, COL_ACCENT)

        # Back arrow (fusion page only)
        if show_back:
            fb.draw_text("<", 16, 14, COL_ACCENT, scale=2)

        # Title centered or left-aligned
        title_x = 56 if show_back else LABEL_X
        fb.draw_text(title, title_x, 14, COL_WHITE, scale=2)

        # Close button: circle with X
        cx, cy = SCREEN_W - 30, TOP_BAR_H // 2
        fb.fill_circle(cx, cy, 14, COL_BTN)
        fb.draw_text("X", cx - 5, cy - 6, COL_RED, scale=1)

    # -----------------------------------------------------------------------
    # Main page rendering
    # -----------------------------------------------------------------------

    def _render_main_items(self):
        """Render main settings items."""
        fb = self.fb
        for i, item in enumerate(self._main_items):
            row_y = CONTENT_Y + i * MAIN_ROW_H

            # Row background (subtle alternation)
            bg = COL_ROW_A if i % 2 == 0 else COL_ROW_B
            fb.fill_rect(0, row_y, SCREEN_W, MAIN_ROW_H, bg)

            # Bottom separator
            fb.fill_rect(LABEL_X, row_y + MAIN_ROW_H - 1,
                         SCREEN_W - LABEL_X * 2, 1, COL_SEP)

            # Label (vertically centered)
            label_y = row_y + (MAIN_ROW_H - 12) // 2
            fb.draw_text(item["label"], LABEL_X, label_y, COL_WHITE, scale=1)

            # Value widget
            itype = item["type"]
            if itype == "toggle":
                self._render_toggle(item, "settings", row_y, MAIN_ROW_H)
            elif itype == "choice":
                self._render_choice(item, "settings", row_y, MAIN_ROW_H)
            elif itype == "submenu":
                arrow_y = row_y + (MAIN_ROW_H - 12) // 2
                fb.draw_text(">", SCREEN_W - 40, arrow_y, COL_ACCENT, scale=1)

    # -----------------------------------------------------------------------
    # Fusion page rendering (two-line rows with descriptions)
    # -----------------------------------------------------------------------

    def _render_fusion_items(self):
        """Render fusion parameter items with descriptions."""
        fb = self.fb
        for i, item in enumerate(self._fusion_items):
            row_y = CONTENT_Y + i * FUSION_ROW_H

            # Row background
            bg = COL_ROW_A if i % 2 == 0 else COL_ROW_B
            fb.fill_rect(0, row_y, SCREEN_W, FUSION_ROW_H, bg)

            # Bottom separator
            fb.fill_rect(LABEL_X, row_y + FUSION_ROW_H - 1,
                         SCREEN_W - LABEL_X * 2, 1, COL_SEP)

            itype = item["type"]

            if itype == "button_danger":
                self._render_button(item, row_y, FUSION_ROW_H)
                continue

            # Line 1: Label (left) + value controls (right)
            line1_y = row_y + 10
            fb.draw_text(item["label"], LABEL_X, line1_y, COL_WHITE, scale=1)

            # Line 2: Description
            desc = item.get("desc", "")
            if desc:
                line2_y = row_y + 30
                fb.draw_text(desc, LABEL_X + 8, line2_y, COL_DESC, scale=1)

            # Value controls
            if itype in ("int", "float"):
                self._render_numeric(item, "fusion", row_y, FUSION_ROW_H)

    # -----------------------------------------------------------------------
    # Widget renderers
    # -----------------------------------------------------------------------

    def _render_toggle(self, item, section, row_y, row_h):
        """Draw iOS-style pill toggle switch."""
        fb = self.fb
        val = self.config.get_int(section, item["key"])

        # Pill dimensions
        pill_w, pill_h = 56, 28
        pill_x = VALUE_X + 20
        pill_y = row_y + (row_h - pill_h) // 2
        knob_r = 10

        pill_color = COL_TOGGLE_ON if val else COL_TOGGLE_OFF
        knob_cx = (pill_x + pill_w - pill_h // 2) if val else (pill_x + pill_h // 2)
        pill_cy = pill_y + pill_h // 2

        # Pill body + rounded ends
        fb.fill_rect(pill_x, pill_y, pill_w, pill_h, pill_color)
        fb.fill_circle(pill_x + pill_h // 2, pill_cy, pill_h // 2, pill_color)
        fb.fill_circle(pill_x + pill_w - pill_h // 2, pill_cy, pill_h // 2, pill_color)
        # Knob
        fb.fill_circle(knob_cx, pill_cy, knob_r, COL_TOGGLE_KNOB)

    def _render_choice(self, item, section, row_y, row_h):
        """Draw choice button with border accent."""
        fb = self.fb
        raw = self.config.get_str(section, item["key"])
        display_map = item.get("display", {})
        display = display_map.get(raw, raw.upper())

        btn_w, btn_h = 64, 28
        btn_x = VALUE_X + 16
        btn_y = row_y + (row_h - btn_h) // 2

        # Outlined button
        fb.fill_rect(btn_x, btn_y, btn_w, btn_h, COL_BTN)
        fb.draw_rect(btn_x, btn_y, btn_w, btn_h, COL_ACCENT)

        # Centered text
        text_w = len(display) * 8  # approximate at scale=1
        text_x = btn_x + (btn_w - text_w) // 2
        text_y = row_y + (row_h - 12) // 2
        fb.draw_text(display, text_x, text_y, COL_ACCENT, scale=1)

    def _render_numeric(self, item, section, row_y, row_h):
        """Draw numeric value with styled [-] [value] [+] controls."""
        fb = self.fb
        key = item["key"]
        fmt = item.get("fmt", "")

        if item["type"] == "float":
            val = self.config.get_float(section, key)
        else:
            val = self.config.get_int(section, key)

        text = f"{val:{fmt}}" if fmt else str(val)

        # Control layout (aligned to right side)
        ctrl_y = row_y + 6
        btn_size = 28

        # [-] button
        minus_x = VALUE_X
        fb.fill_rect(minus_x, ctrl_y, btn_size, btn_size, COL_BTN)
        fb.draw_rect(minus_x, ctrl_y, btn_size, btn_size, COL_BTN_BORDER)
        fb.draw_text("-", minus_x + 10, ctrl_y + 8, COL_RED, scale=1)

        # Value text (centered between buttons)
        val_x = minus_x + btn_size + 8
        fb.draw_text(text, val_x, ctrl_y + 8, COL_WHITE, scale=1)

        # [+] button
        plus_x = VALUE_X + 120
        fb.fill_rect(plus_x, ctrl_y, btn_size, btn_size, COL_BTN)
        fb.draw_rect(plus_x, ctrl_y, btn_size, btn_size, COL_BTN_BORDER)
        fb.draw_text("+", plus_x + 10, ctrl_y + 8, COL_GREEN, scale=1)

    def _render_button(self, item, row_y, row_h):
        """Draw a full-width action button with border."""
        fb = self.fb
        btn_x = LABEL_X
        btn_y = row_y + (row_h - 34) // 2
        btn_w = SCREEN_W - LABEL_X * 2
        btn_h = 34

        fb.fill_rect(btn_x, btn_y, btn_w, btn_h, COL_BTN_DANGER)
        fb.draw_rect(btn_x, btn_y, btn_w, btn_h, COL_BTN_DANGER_BORDER)

        # Centered text
        text_w = len(item["label"]) * 8
        text_x = btn_x + (btn_w - text_w) // 2
        text_y = btn_y + (btn_h - 12) // 2 + 1
        fb.draw_text(item["label"], text_x, text_y, COL_WHITE, scale=1)

    # -----------------------------------------------------------------------
    # Interaction handlers
    # -----------------------------------------------------------------------

    def _handle_item_tap(self, item, tap_x):
        """Process a tap on a settings item."""
        itype = item["type"]
        key = item["key"]

        if itype == "toggle":
            cur = self.config.get_int("settings", key)
            new_val = 0 if cur else 1
            self.config.set("settings", key, new_val)
            self.config.save()
            if key == "npu_enabled" and self.on_npu_toggle:
                self.on_npu_toggle(bool(new_val))
            elif key == "night_mode" and self.on_night_mode_change:
                self.on_night_mode_change(bool(new_val))
            elif key == "mirror_display" and self.on_mirror_change:
                self.on_mirror_change(bool(new_val))

        elif itype == "choice":
            choices = item["choices"]
            cur = self.config.get_str("settings", key)
            idx = choices.index(cur) if cur in choices else 0
            new_val = choices[(idx + 1) % len(choices)]
            self.config.set("settings", key, new_val)
            self.config.save()
            if key == "region" and self.on_region_change:
                self.on_region_change(new_val)
            elif key == "display_mode" and self.on_display_mode_change:
                self.on_display_mode_change(new_val)

        elif itype == "submenu":
            if key == "_fusion":
                self.page = "fusion"

        elif itype in ("int", "float"):
            # Determine if tap hit [-] or [+]
            section = "fusion"
            step = item["step"]
            if tap_x < VALUE_X + 28:
                # [-] button
                if item["type"] == "float":
                    val = self.config.get_float(section, key) - step
                else:
                    val = self.config.get_int(section, key) - step
                val = max(item["min"], val)
            elif tap_x > VALUE_X + 110:
                # [+] button
                if item["type"] == "float":
                    val = self.config.get_float(section, key) + step
                else:
                    val = self.config.get_int(section, key) + step
                val = min(item["max"], val)
            else:
                return  # tap on value text, ignore

            if item["type"] == "float":
                val = round(val, 2)
            else:
                val = int(val)
            self.config.set(section, key, val)
            self.config.save()
            if self.on_fusion_reload:
                self.on_fusion_reload()

        elif itype == "button_danger":
            if key == "_reset":
                self.config.reset_section("fusion")
                self.config.save()
                if self.on_fusion_reload:
                    self.on_fusion_reload()

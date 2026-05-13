"""Settings UI overlay for ai-hud, rendered to framebuffer via touch.

Full-screen overlay that replaces HUD when active. Uses the same
Framebuffer class from hud_live.py for all drawing.

Layout (480x480):
  - Top bar (0-50):   title + close button
  - Items (60-410):   list of settings, each row ~60px
  - Status bar (430-480): device info

Pages:
  - "main":   NPU toggle, region, fusion entry
  - "fusion": all NPU fusion parameters with +/- controls

No third-party dependencies.
"""

import os

# ---------------------------------------------------------------------------
# Colors (consistent with HUD dark theme)
# ---------------------------------------------------------------------------

COL_BG = (8, 10, 15)
COL_PANEL = (20, 24, 32)
COL_ROW_ALT = (16, 19, 26)
COL_WHITE = (245, 248, 255)
COL_DIM = (100, 110, 130)
COL_ACCENT = (60, 140, 255)
COL_GREEN = (50, 220, 120)
COL_RED = (255, 55, 60)
COL_TOGGLE_ON = (50, 220, 120)
COL_TOGGLE_OFF = (80, 85, 100)
COL_SLIDER_BG = (40, 44, 55)
COL_SLIDER_FILL = (60, 140, 255)
COL_BTN = (35, 40, 55)
COL_BTN_DANGER = (120, 30, 35)

# Layout constants
ROW_HEIGHT = 58
TOP_BAR_H = 52
CONTENT_Y = TOP_BAR_H + 4
STATUS_Y = 432
LABEL_X = 20
VALUE_X = 320
ROW_PAD = 8
SCREEN_W = 480
SCREEN_H = 480


class SettingsUI:
    """Touch-driven settings overlay."""

    def __init__(self, fb, config, region_mgr):
        """Initialize settings UI.

        Args:
            fb: Framebuffer instance (from hud_live.py)
            config: ConfigManager instance
            region_mgr: RegionManager instance
        """
        self.fb = fb
        self.config = config
        self.region_mgr = region_mgr

        self.active = False
        self.page = "main"

        # Callbacks set by hud_live.py integration
        self.on_npu_toggle = None          # fn(enabled: bool)
        self.on_region_change = None       # fn(region: str)
        self.on_fusion_reload = None       # fn()
        self.on_display_mode_change = None # fn(mode: str)  "hud" or "cam"

        # Page definitions
        self._main_items = [
            {"key": "display_mode", "label": "Display",         "type": "choice",
             "choices": ["hud", "cam"], "display": {"hud": "HUD", "cam": "CAM"}},
            {"key": "npu_enabled",  "label": "NPU Detection",  "type": "toggle"},
            {"key": "region",       "label": "Region",          "type": "choice",
             "choices": ["au", "cn"], "display": {"au": "AU", "cn": "CN"}},
            {"key": "_fusion",      "label": "Fusion Params",   "type": "submenu"},
        ]

        self._fusion_items = [
            {"key": "npu_confidence_min",   "label": "Conf (DB)",
             "type": "float", "min": 0.10, "max": 1.00, "step": 0.05, "fmt": ".2f"},
            {"key": "npu_confidence_no_db", "label": "Conf (no DB)",
             "type": "float", "min": 0.10, "max": 1.00, "step": 0.05, "fmt": ".2f"},
            {"key": "npu_vote_required",    "label": "Vote Count",
             "type": "int",   "min": 1,    "max": 10,   "step": 1,    "fmt": "d"},
            {"key": "npu_override_timeout", "label": "Timeout (s)",
             "type": "float", "min": 5.0,  "max": 120.0, "step": 5.0, "fmt": ".0f"},
            {"key": "camera_alert_radius",  "label": "Cam Alert (m)",
             "type": "int",   "min": 100,  "max": 2000, "step": 100,  "fmt": "d"},
            {"key": "camera_warn_radius",   "label": "Cam Warn (m)",
             "type": "int",   "min": 50,   "max": 1000, "step": 50,   "fmt": "d"},
            {"key": "_reset",               "label": "Reset Defaults",
             "type": "button_danger"},
        ]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    # IPC file to pause C-side PiP rendering while settings is visible
    _PIP_HIDE_IPC = "/tmp/ai_hud_pip_hide"

    def activate(self):
        """Enter settings overlay."""
        self.active = True
        self.page = "main"
        # Signal C binary to pause PiP camera overlay
        try:
            with open(self._PIP_HIDE_IPC, "w") as f:
                f.write("1")
        except OSError:
            pass
        self.render()

    def deactivate(self):
        """Exit settings, return to HUD."""
        self.active = False
        # Resume PiP camera overlay
        try:
            os.unlink(self._PIP_HIDE_IPC)
        except OSError:
            pass

    def handle_touch(self, event):
        """Process a TouchEvent. Returns True if event was consumed."""
        if not self.active:
            return False

        if event.gesture != "tap":
            # Swipe left on fusion page -> back to main
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

        row_index = (y - CONTENT_Y) // ROW_HEIGHT
        items = self._main_items if self.page == "main" else self._fusion_items

        if row_index < 0 or row_index >= len(items):
            return True

        item = items[row_index]
        self._handle_item_tap(item, x)
        self.render()
        return True

    def render(self):
        """Full-screen render of settings page."""
        fb = self.fb
        fb.clear(COL_BG)

        if self.page == "main":
            self._render_top_bar("SETTINGS", show_back=False)
            self._render_items(self._main_items, "settings")
        elif self.page == "fusion":
            self._render_top_bar("FUSION PARAMS", show_back=True)
            self._render_items(self._fusion_items, "fusion")

        # Status bar
        region = self.region_mgr.region.upper()
        fb.draw_text(f"v0.2 | {region}", LABEL_X, STATUS_Y + 10, COL_DIM, scale=1)

        fb.flush()

    # -----------------------------------------------------------------------
    # Rendering helpers
    # -----------------------------------------------------------------------

    def _render_top_bar(self, title, show_back=False):
        """Draw top bar with title and close button."""
        fb = self.fb
        fb.fill_rect(0, 0, SCREEN_W, TOP_BAR_H, COL_PANEL)

        # Back arrow (fusion page only)
        if show_back:
            fb.draw_text("<", 16, 14, COL_ACCENT, scale=2)

        # Title
        title_x = 50 if show_back else LABEL_X
        fb.draw_text(title, title_x, 16, COL_WHITE, scale=2)

        # Close button [X]
        fb.draw_text("X", SCREEN_W - 34, 16, COL_RED, scale=2)

    def _render_items(self, items, section):
        """Render a list of setting items."""
        fb = self.fb
        for i, item in enumerate(items):
            row_y = CONTENT_Y + i * ROW_HEIGHT

            # Alternating row background
            bg = COL_ROW_ALT if i % 2 == 0 else COL_BG
            fb.fill_rect(0, row_y, SCREEN_W, ROW_HEIGHT, bg)

            # Separator line
            fb.fill_rect(LABEL_X, row_y + ROW_HEIGHT - 1,
                         SCREEN_W - LABEL_X * 2, 1, COL_PANEL)

            # Label
            label_y = row_y + (ROW_HEIGHT - 16) // 2
            fb.draw_text(item["label"], LABEL_X, label_y, COL_WHITE, scale=1)

            # Value widget
            itype = item["type"]
            if itype == "toggle":
                self._render_toggle(item, section, row_y)
            elif itype == "choice":
                self._render_choice(item, section, row_y)
            elif itype == "submenu":
                fb.draw_text(">", SCREEN_W - 40, label_y, COL_ACCENT, scale=1)
            elif itype in ("int", "float"):
                self._render_numeric(item, section, row_y)
            elif itype == "button_danger":
                self._render_button(item, row_y, COL_BTN_DANGER)

    def _render_toggle(self, item, section, row_y):
        """Draw ON/OFF toggle indicator."""
        fb = self.fb
        val = self.config.get_int(section, item["key"])
        text = "ON" if val else "OFF"
        color = COL_TOGGLE_ON if val else COL_TOGGLE_OFF
        text_y = row_y + (ROW_HEIGHT - 16) // 2
        # Draw pill background
        pill_x = VALUE_X
        pill_y = row_y + (ROW_HEIGHT - 28) // 2
        fb.fill_rect(pill_x, pill_y, 70, 28, color)
        fb.draw_text(text, pill_x + 12, text_y, COL_WHITE, scale=1)

    def _render_choice(self, item, section, row_y):
        """Draw current choice value."""
        fb = self.fb
        raw = self.config.get_str(section, item["key"])
        display_map = item.get("display", {})
        display = display_map.get(raw, raw.upper())
        text_y = row_y + (ROW_HEIGHT - 16) // 2
        fb.fill_rect(VALUE_X, row_y + (ROW_HEIGHT - 28) // 2, 70, 28, COL_BTN)
        fb.draw_text(display, VALUE_X + 12, text_y, COL_ACCENT, scale=1)

    def _render_numeric(self, item, section, row_y):
        """Draw numeric value with [-] [value] [+] controls."""
        fb = self.fb
        key = item["key"]
        fmt = item.get("fmt", "")

        if item["type"] == "float":
            val = self.config.get_float(section, key)
        else:
            val = self.config.get_int(section, key)

        text = f"{val:{fmt}}" if fmt else str(val)
        text_y = row_y + (ROW_HEIGHT - 16) // 2

        # [-] button
        btn_y = row_y + (ROW_HEIGHT - 28) // 2
        fb.fill_rect(VALUE_X, btn_y, 36, 28, COL_BTN)
        fb.draw_text("-", VALUE_X + 12, text_y, COL_RED, scale=1)

        # Value
        fb.draw_text(text, VALUE_X + 46, text_y, COL_WHITE, scale=1)

        # [+] button
        plus_x = VALUE_X + 110
        fb.fill_rect(plus_x, btn_y, 36, 28, COL_BTN)
        fb.draw_text("+", plus_x + 12, text_y, COL_GREEN, scale=1)

    def _render_button(self, item, row_y, bg_color):
        """Draw a full-width action button."""
        fb = self.fb
        btn_x = LABEL_X
        btn_y = row_y + (ROW_HEIGHT - 34) // 2
        btn_w = SCREEN_W - LABEL_X * 2
        btn_h = 34
        fb.fill_rect(btn_x, btn_y, btn_w, btn_h, bg_color)
        # Center text
        text_w = len(item["label"]) * 8
        text_x = btn_x + (btn_w - text_w) // 2
        text_y = btn_y + (btn_h - 16) // 2 + 1
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
            if tap_x < VALUE_X + 36:
                # [-] button
                if item["type"] == "float":
                    val = self.config.get_float(section, key) - step
                else:
                    val = self.config.get_int(section, key) - step
                val = max(item["min"], val)
            elif tap_x > VALUE_X + 100:
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

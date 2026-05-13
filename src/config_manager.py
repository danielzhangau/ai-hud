"""Persistent configuration manager for ai-hud device.

INI format stored at /root/ai_hud.conf (or override via constructor).
Uses Python standard library only (configparser).

Atomic write: writes to .tmp then renames, safe against power loss.

Usage:
    cfg = ConfigManager()           # loads or creates default config
    val = cfg.get_float("fusion", "npu_confidence_min")
    cfg.set("fusion", "npu_vote_required", "5")
    cfg.save()
"""

import configparser
import os
import tempfile

# Default config path on device
DEFAULT_CONFIG_PATH = "/root/ai_hud.conf"

# ---------------------------------------------------------------------------
# Parameter definitions: (section, key) -> (type, default, min, max)
# ---------------------------------------------------------------------------

PARAM_DEFS = {
    # Fusion parameters
    ("fusion", "npu_confidence_min"):   (float, 0.70, 0.10, 1.00),
    ("fusion", "npu_confidence_no_db"): (float, 0.60, 0.10, 1.00),
    ("fusion", "npu_vote_required"):    (int,   3,    1,    10),
    ("fusion", "npu_override_timeout"): (float, 15.0, 5.0,  120.0),
    ("fusion", "camera_alert_radius"):  (int,   800,  100,  2000),
    ("fusion", "camera_warn_radius"):   (int,   400,  50,   1000),

    # Settings
    ("settings", "npu_enabled"):   (int,   1,    0,    1),
    ("settings", "region"):        (str,   "au", None, None),
    ("settings", "brightness"):    (int,   100,  10,   100),
    ("settings", "display_mode"):  (str,   "hud", None, None),
}

# Valid string choices
_STR_CHOICES = {
    ("settings", "region"):       ("au", "cn"),
    ("settings", "display_mode"): ("hud", "cam"),
}


class ConfigManager:
    """Read/write INI config with type validation and range clamping."""

    def __init__(self, path=None):
        self.path = path or DEFAULT_CONFIG_PATH
        self._cp = configparser.ConfigParser()
        self._dirty = False

        if os.path.isfile(self.path):
            self._cp.read(self.path)
            self._ensure_defaults()
        else:
            self._write_defaults()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get_str(self, section, key):
        """Get string value."""
        return self._cp.get(section, key,
                            fallback=str(self._default(section, key)))

    def get_int(self, section, key):
        """Get integer value, clamped to valid range."""
        raw = self.get_str(section, key)
        pdef = PARAM_DEFS.get((section, key))
        try:
            val = int(raw)
        except (ValueError, TypeError):
            val = pdef[1] if pdef else 0
        if pdef and pdef[0] in (int, float) and pdef[2] is not None:
            val = max(pdef[2], min(pdef[3], val))
        return val

    def get_float(self, section, key):
        """Get float value, clamped to valid range."""
        raw = self.get_str(section, key)
        pdef = PARAM_DEFS.get((section, key))
        try:
            val = float(raw)
        except (ValueError, TypeError):
            val = pdef[1] if pdef else 0.0
        if pdef and pdef[0] in (int, float) and pdef[2] is not None:
            val = max(pdef[2], min(pdef[3], val))
        return val

    def set(self, section, key, value):
        """Set a parameter value (validates and clamps)."""
        pdef = PARAM_DEFS.get((section, key))
        if pdef is None:
            # Unknown param, store as-is
            if not self._cp.has_section(section):
                self._cp.add_section(section)
            self._cp.set(section, key, str(value))
            self._dirty = True
            return

        typ, default, vmin, vmax = pdef

        if typ == str:
            value = str(value)
            choices = _STR_CHOICES.get((section, key))
            if choices and value not in choices:
                return  # reject invalid choice silently
        elif typ == int:
            try:
                value = int(value)
            except (ValueError, TypeError):
                return
            if vmin is not None:
                value = max(vmin, min(vmax, value))
        elif typ == float:
            try:
                value = float(value)
            except (ValueError, TypeError):
                return
            if vmin is not None:
                value = max(vmin, min(vmax, value))

        if not self._cp.has_section(section):
            self._cp.add_section(section)
        self._cp.set(section, key, str(value))
        self._dirty = True

    def save(self):
        """Atomic write: write to .tmp then rename."""
        if not self._dirty:
            return

        dir_name = os.path.dirname(self.path) or "."
        try:
            os.makedirs(dir_name, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_name, prefix=".ai_hud_conf_")
            with os.fdopen(fd, "w") as f:
                self._cp.write(f)
            os.replace(tmp_path, self.path)
            self._dirty = False
        except OSError as e:
            print(f"[config] WARNING: failed to save {self.path}: {e}")
            # Try to clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def get_fusion_params(self):
        """Return dict of all fusion parameters for SpeedFusion constructor."""
        return {
            "confidence_min":     self.get_float("fusion", "npu_confidence_min"),
            "confidence_no_db":   self.get_float("fusion", "npu_confidence_no_db"),
            "vote_required":      self.get_int("fusion", "npu_vote_required"),
            "override_timeout":   self.get_float("fusion", "npu_override_timeout"),
            "camera_alert_radius": self.get_int("fusion", "camera_alert_radius"),
            "camera_warn_radius": self.get_int("fusion", "camera_warn_radius"),
        }

    def reset_section(self, section):
        """Reset all parameters in a section to defaults."""
        for (sec, key), (_, default, _, _) in PARAM_DEFS.items():
            if sec == section:
                self.set(sec, key, default)

    def reset_all(self):
        """Reset entire config to defaults."""
        for (sec, key), (_, default, _, _) in PARAM_DEFS.items():
            self.set(sec, key, default)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _default(self, section, key):
        """Get default value for a parameter."""
        pdef = PARAM_DEFS.get((section, key))
        return pdef[1] if pdef else ""

    def _ensure_defaults(self):
        """Add missing parameters with defaults (preserves existing values)."""
        changed = False
        for (section, key), (_, default, _, _) in PARAM_DEFS.items():
            if not self._cp.has_section(section):
                self._cp.add_section(section)
                changed = True
            if not self._cp.has_option(section, key):
                self._cp.set(section, key, str(default))
                changed = True
        if changed:
            self._dirty = True
            self.save()

    def _write_defaults(self):
        """Create config file with all default values."""
        for (section, key), (_, default, _, _) in PARAM_DEFS.items():
            if not self._cp.has_section(section):
                self._cp.add_section(section)
            self._cp.set(section, key, str(default))
        self._dirty = True
        self.save()

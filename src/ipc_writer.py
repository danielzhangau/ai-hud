"""IPC file writers for C <-> Python communication.

All IPC uses atomic write (write to .tmp, then rename) to prevent
partial reads. Each function is fire-and-forget with OSError suppression.
"""

import os
import time

# ---------------------------------------------------------------------------
# IPC file paths (must match hud_ipc.h on the C side)
# ---------------------------------------------------------------------------

# C -> Python: NPU detection results
NPU_DETECT_FILE = "/tmp/ai_hud_detect"
NPU_POLL_INTERVAL = 0.5  # seconds between file reads

# Python -> C: vehicle speed for adaptive inference rate
SPEED_IPC_FILE = "/tmp/ai_hud_speed"
SPEED_IPC_TMP = "/tmp/ai_hud_speed.tmp"

# Python -> C: NPU enable/disable toggle
NPU_ENABLE_FILE = "/tmp/ai_hud_npu_enable"
NPU_ENABLE_TMP = "/tmp/ai_hud_npu_enable.tmp"

# Python -> C: GPS coordinates for frame capture metadata
GPS_IPC_FILE = "/tmp/ai_hud_gps"
GPS_IPC_TMP = "/tmp/ai_hud_gps.tmp"

# Python -> C: display mode ("cam" = camera, absent = HUD)
DISPLAY_MODE_IPC = "/tmp/ai_hud_display_mode"
DISPLAY_MODE_IPC_TMP = "/tmp/ai_hud_display_mode.tmp"

# Python -> C: ISP night mode configuration
ISP_CONFIG_IPC_FILE = "/tmp/ai_hud_isp_config"
ISP_CONFIG_IPC_TMP = "/tmp/ai_hud_isp_config.tmp"

# Python -> system: heartbeat for watchdog
HEARTBEAT_IPC_FILE = "/tmp/ai_hud_heartbeat"
HEARTBEAT_IPC_TMP = "/tmp/ai_hud_heartbeat.tmp"
HEARTBEAT_INTERVAL_S = 5  # Write heartbeat at most every N seconds


# ---------------------------------------------------------------------------
# IPC write functions
# ---------------------------------------------------------------------------

def write_speed_ipc(speed_kmh):
    """Write current vehicle speed to IPC file for C inference thread.

    The C inference thread reads this to adjust its detection frequency:
    faster speed -> more frequent inference.
    """
    try:
        with open(SPEED_IPC_TMP, "w") as f:
            f.write(f"{speed_kmh:.1f}\n")
        os.rename(SPEED_IPC_TMP, SPEED_IPC_FILE)
    except OSError:
        pass  # non-critical, C side falls back to default rate


def write_npu_enable_ipc(enabled):
    """Write NPU enable/disable toggle to IPC file for C inference thread.

    When disabled, the C inference thread skips NPU inference entirely,
    falling back to pure database-driven speed limits.
    """
    try:
        with open(NPU_ENABLE_TMP, "w") as f:
            f.write("1\n" if enabled else "0\n")
        os.rename(NPU_ENABLE_TMP, NPU_ENABLE_FILE)
    except OSError:
        pass


def write_isp_config_ipc(night_mode):
    """Write ISP configuration to IPC file for C isp_control module.

    C reads this periodically and applies AE/DRC/NR changes:
      night_mode=1 -> higher gain, stronger DRC + temporal NR
      night_mode=0 -> normal daytime settings
    """
    try:
        with open(ISP_CONFIG_IPC_TMP, "w") as f:
            f.write(f"night_mode={1 if night_mode else 0}\n")
        os.rename(ISP_CONFIG_IPC_TMP, ISP_CONFIG_IPC_FILE)
    except OSError:
        pass


def write_gps_ipc(lat, lon):
    """Write GPS coordinates to IPC file for C frame capture metadata.

    The C frame capture module reads lat/lon to tag saved frames
    with location data for offline labeling.
    """
    try:
        with open(GPS_IPC_TMP, "w") as f:
            f.write(f"lat={lat:.6f}\n")
            f.write(f"lon={lon:.6f}\n")
        os.rename(GPS_IPC_TMP, GPS_IPC_FILE)
    except OSError:
        pass


_last_heartbeat_time = 0.0


def write_heartbeat():
    """Write epoch timestamp to heartbeat IPC file.

    Called from the main loop. Throttled to at most once per
    HEARTBEAT_INTERVAL_S seconds to avoid unnecessary I/O.
    System watchdog in S99_ai_hud monitors this file to detect
    application-level hangs and trigger reboot if stale.
    """
    global _last_heartbeat_time
    now = time.monotonic()
    if now - _last_heartbeat_time < HEARTBEAT_INTERVAL_S:
        return
    _last_heartbeat_time = now
    try:
        with open(HEARTBEAT_IPC_TMP, "w") as f:
            f.write(f"{int(time.time())}\n")
        os.rename(HEARTBEAT_IPC_TMP, HEARTBEAT_IPC_FILE)
    except OSError:
        pass


def write_display_mode_ipc(mode):
    """Write display mode IPC for C pip_render_thread.

    Args:
        mode: "cam" for full-screen camera, "hud" to stop camera rendering.
    """
    if mode == "cam":
        try:
            with open(DISPLAY_MODE_IPC_TMP, "w") as f:
                f.write("cam\n")
            os.rename(DISPLAY_MODE_IPC_TMP, DISPLAY_MODE_IPC)
        except OSError:
            pass
    else:
        # HUD mode: remove IPC file (C defaults to HUD when absent)
        try:
            os.unlink(DISPLAY_MODE_IPC)
        except OSError:
            pass

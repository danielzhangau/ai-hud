#!/bin/bash
# AI-HUD Config launcher (macOS .app entry point).
#
# Double-clicking the .app runs this script. It:
#   1. Bundled-adb starts a forward (host 8080 -> device 8080)
#   2. Probes the device HTTP server
#   3. Opens the config UI in the default browser
#
# Designed for end-users with zero technical background -- one double-click,
# no terminal, no homebrew, no Android Platform Tools install needed.

set -eu

# Resolve the bundled adb. The script lives at
# AI-HUD Config.app/Contents/MacOS/launch -- adb is alongside it.
DIR="$(cd "$(dirname "$0")" && pwd)"
ADB="$DIR/adb"
# The device serves HTTP on port 80 (no port in URL when accessed via
# the future ai-hud.local mDNS path). We forward to a host-side 8080 to
# avoid clashing with anything on the user's :80, and to keep the URL
# trivially memorable.
HOST_PORT=8080
DEVICE_PORT=80
URL="http://localhost:${HOST_PORT}"

# Native macOS dialogs so the user gets feedback even though the UI is just a
# shell-script-in-an-app. `osascript` is built into every Mac since 10.5.
notify() {
    osascript -e "display dialog \"$1\" with title \"AI-HUD Config\" \
        buttons {\"OK\"} default button \"OK\" with icon ${2:-note}" \
        >/dev/null 2>&1 || true
}

if [ ! -x "$ADB" ]; then
    notify "Bundled adb is missing. Please reinstall AI-HUD Config." stop
    exit 1
fi

# Before doing anything else, sniff USB. Three valid states exist for the
# device + this launcher to do something useful:
#
#   * ADB    -- device runs Linux normally; we want the OTA + dashboard
#               path (the rest of this script).
#   * MaskROM/Loader -- customer just held BOOT and replugged; they want a
#               firmware flash. flash_firmware.py handles it.
#   * none   -- nothing plugged in; show the "plug it in" dialog.
#
# We detect via ioreg (no admin prompt, fast) rather than running adb /
# upgrade_tool blindly: each of those tools would otherwise pop its own
# dialog box for a device that isn't there.
RK_STATE=""
if [ -f "$DIR/flash_firmware.py" ] && command -v python3 >/dev/null 2>&1; then
    RK_STATE=$(python3 -c '
import sys
sys.path.insert(0, "'"$DIR"'")
from flash_firmware import detect_rockchip_state
print(detect_rockchip_state())
' 2>/dev/null || echo "")
fi

case "$RK_STATE" in
    maskrom|loader)
        UPGRADE_TOOL="$DIR/upgrade_tool"
        if [ ! -x "$UPGRADE_TOOL" ]; then
            notify "Firmware-flash mode detected, but upgrade_tool is missing
from the app bundle. Please reinstall AI-HUD Config." stop
            exit 5
        fi
        python3 "$DIR/flash_firmware.py" --upgrade-tool "$UPGRADE_TOOL" || true
        # After flashing the device reboots into normal Linux. We don't
        # try to fall through to the OTA path here -- there's a ~30s
        # boot window and the user will double-click again anyway.
        exit 0
        ;;
esac

# Start the local adb daemon explicitly. If a Homebrew / Android-Studio adb
# is already running on a *different* protocol version it will refuse to talk
# to our bundled one; in that case we kill the running server and retake it.
if ! "$ADB" devices >/dev/null 2>&1; then
    "$ADB" kill-server >/dev/null 2>&1 || true
    "$ADB" start-server >/dev/null 2>&1 || true
fi

# Look for a connected device. Output looks like:
#     List of devices attached
#     fd673a469f61593a	device
if ! "$ADB" devices | awk 'NR>1 && $2=="device" {found=1} END{exit !found}'; then
    notify "No AI-HUD device detected.

Please plug the device in via USB, wait a few seconds for it to boot,
then double-click this app again.

If the device is in firmware-flash mode (BOOT button held while plugging
in), make sure the cable is a data cable and not a power-only one." caution
    exit 2
fi

# Bind the forward (idempotent: rebinding overrides a stale rule).
if ! "$ADB" forward "tcp:${HOST_PORT}" "tcp:${DEVICE_PORT}" >/dev/null 2>&1; then
    notify "adb forward failed. Is another instance using port ${HOST_PORT}?" stop
    exit 3
fi

# Probe the device HTTP server with a short timeout. The device runs the web
# server inside hud_live.py; if hud_live.py is restarting we want to wait a
# couple of seconds, but bail out cleanly if it's really not there.
PROBE_OK=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf -o /dev/null --max-time 1 "${URL}/api/state" 2>/dev/null; then
        PROBE_OK=1
        break
    fi
    sleep 0.5
done

if [ "$PROBE_OK" -ne 1 ]; then
    notify "Device is connected but the configuration server is not
responding. Try power-cycling the device, then double-click this app again." caution
    exit 4
fi

# Best-effort OTA check. The updater compares /root/version.txt to the
# latest GitHub Release tag; if outdated AND the user accepts the
# prompt, it pulls the bundle and pushes the new code. Failures here
# never block opening the dashboard -- the user sees whatever's
# currently installed and the next launch can retry.
UPDATER="$DIR/updater.py"
if [ -f "$UPDATER" ] && command -v python3 >/dev/null 2>&1; then
    python3 "$UPDATER" --adb "$ADB" --script-dir "$DIR" || true
fi

# If the updater triggered a reboot the original adb forward got torn
# down (the device disconnected from USB during reboot). Re-bind it.
# Idempotent if it's still alive (2026-05-22).
"$ADB" forward "tcp:${HOST_PORT}" "tcp:${DEVICE_PORT}" >/dev/null 2>&1 || true

# Re-probe the device HTTP server before opening the browser. After an
# OTA reboot hud_live.py takes 10-20 seconds to come back; without this
# wait `open ${URL}` hits a connection-refused page in the browser.
# Window: up to 30 seconds (longer than the bare _wait_for_device the
# updater already did, to cover hud_live.py's Python import + ISP init).
for _ in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 1 "${URL}/api/state" 2>/dev/null; then
        break
    fi
    sleep 1
done

# Open the default browser. -g keeps the existing focused window from
# losing focus (the launcher itself shouldn't steal it).
open "${URL}"

#!/usr/bin/env bash
# Factory-provision an AI-HUD device so the customer can plug it into any
# car or computer and immediately have HUD + USB-drive-with-launcher.
#
# Assumes:
#   - Device is already on a recent firmware (reboot button + adb working).
#     If you need to flash firmware first, run flash_and_deploy.sh; this
#     script only handles the "stuff that goes on top of the rootfs".
#   - Mac is set up with adb (the launcher's bundled one is fine), the
#     launcher built, and tools/build_launcher_disk.sh has produced
#     dist/launcher.img.
#
# What it does, in order:
#   1. Build the launcher disk image if missing.
#   2. Push the launcher.img to /userdata/ (the only persistent partition
#      that survives an OTA rootfs reflash).
#   3. Push every Python module, init script, NPU model, speed DB, and
#      the ai-hud binary if present.
#   4. Write the product version to /root/version.txt so dashboard +
#      OTA updater both see a real (non-0.0.0) starting point.
#   5. Clear stale .pyc caches.
#   6. Reboot so S50usbdevice picks up UMS + S99_ai_hud restarts cleanly.
#
# Usage:
#   ./tools/provision.sh                          # uses VERSION from latest git tag
#   VERSION=0.1.0 ./tools/provision.sh
#   ADB=/path/to/adb ./tools/provision.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ADB="${ADB:-adb}"
LAUNCHER_IMG="${REPO_ROOT}/dist/launcher.img"

VERSION="${VERSION:-}"
if [ -z "$VERSION" ]; then
    # Derive from the latest git tag (vX.Y.Z -> X.Y.Z). Fall back to 0.0.0
    # if the repo isn't tagged yet -- the dashboard will show "0.0.0"
    # and the launcher OTA path will then offer to update to the latest
    # released version.
    VERSION="$(git -C "$REPO_ROOT" describe --tags --abbrev=0 2>/dev/null \
               | sed 's/^v//' \
               || echo "0.0.0")"
fi
echo "==> Provisioning device for version $VERSION"

# --- 1. Ensure launcher disk image exists ----------------------------------
if [ ! -f "$LAUNCHER_IMG" ]; then
    echo "==> dist/launcher.img missing -- building it"
    bash "$SCRIPT_DIR/build_launcher_disk.sh"
fi
[ -f "$LAUNCHER_IMG" ] || { echo "ERROR: launcher.img build failed"; exit 1; }

# --- 2. Push launcher.img -> /userdata (32 MB; ~5 s over USB 2.0) ---------
# Path tied to S50usbdevice's UMS_BLOCK variable. /userdata is on a
# separate eMMC partition that survives an OTA rootfs reflash, so the
# customer never loses access to their virtual launcher disk.
echo "==> Pushing $LAUNCHER_IMG -> /userdata/launcher.img"
"$ADB" shell 'mkdir -p /userdata'
"$ADB" push "$LAUNCHER_IMG" /userdata/launcher.img

# --- 3. Push the rest of the device-side code ------------------------------
# Mirrors flash_and_deploy.sh layout but doesn't reboot until the very end.
echo "==> Pushing Python modules to /root/"
for f in hud_live.py web_config.py config_manager.py speed_db.py sun.py \
         usb_netd.py touch_input.py settings_ui.py gps_reader.py ; do
    "$ADB" push "${REPO_ROOT}/src/$f" "/root/$f"
done

echo "==> Pushing init scripts"
"$ADB" push "${REPO_ROOT}/scripts/S50usbdevice"      /etc/init.d/S50usbdevice
"$ADB" push "${REPO_ROOT}/scripts/S99_ai_hud"        /etc/init.d/S99_ai_hud
"$ADB" push "${REPO_ROOT}/scripts/S99usbnetd"        /etc/init.d/S99usbnetd
"$ADB" push "${REPO_ROOT}/scripts/S01_ai_hud_splash" /etc/init.d/S01_ai_hud_splash
"$ADB" shell 'chmod +x /etc/init.d/S50usbdevice /etc/init.d/S99_ai_hud \
                       /etc/init.d/S99usbnetd /etc/init.d/S01_ai_hud_splash'

echo "==> Pushing speed databases"
"$ADB" shell 'mkdir -p /root/data'
for db in speed_zones.db speed_cameras.db speed_zones_cn.db speed_cameras_cn.db ; do
    [ -f "${REPO_ROOT}/data/$db" ] \
        && "$ADB" push "${REPO_ROOT}/data/$db" "/root/data/$db" \
        || echo "   (skipping missing $db)"
done

# Optional binaries: ai-hud comes from the firmware image rather than git,
# and the model is large. Push them if a copy exists locally.
if [ -f "${REPO_ROOT}/build/ai-hud" ]; then
    echo "==> Pushing ai-hud binary"
    "$ADB" push "${REPO_ROOT}/build/ai-hud" /root/ai-hud
    "$ADB" shell 'chmod +x /root/ai-hud'
fi
if [ -f "${REPO_ROOT}/models/speed_signs_rv1106.rknn" ]; then
    echo "==> Pushing NPU model"
    "$ADB" shell 'mkdir -p /root/model'
    "$ADB" push "${REPO_ROOT}/models/speed_signs_rv1106.rknn" \
                "/root/model/speed_signs_rv1106.rknn"
fi
if [ -f "${REPO_ROOT}/mockups/splash_raw.bin" ]; then
    "$ADB" push "${REPO_ROOT}/mockups/splash_raw.bin" /root/splash_raw.bin
fi

# --- 4. Stamp version.txt --------------------------------------------------
echo "==> Stamping /root/version.txt = $VERSION"
"$ADB" shell "echo '$VERSION' > /root/version.txt"

# --- 5. Clear stale .pyc ---------------------------------------------------
echo "==> Clearing stale Python bytecode"
"$ADB" shell 'find /root -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; \
              find /root -name "*.pyc" -exec rm {} \; 2>/dev/null; true'

# --- 6. Reboot so UMS function binds to the new launcher.img ---------------
# S50usbdevice's UDC binding only happens once at boot, so editing the file
# alone doesn't expose the new mass-storage function -- we need a reboot.
echo "==> Rebooting device..."
"$ADB" shell 'reboot' || true

echo
echo "==> Waiting for device to come back online (~30 s)"
for i in $(seq 1 60); do
    sleep 2
    if "$ADB" shell true >/dev/null 2>&1; then
        echo "    device back after $((i*2)) s"
        break
    fi
done

# --- 7. Verify ------------------------------------------------------------
sleep 5
echo
echo "==> Verification"
"$ADB" shell 'echo "version.txt:   $(cat /root/version.txt 2>/dev/null)"
              echo "launcher.img:  $(ls -la /userdata/launcher.img 2>/dev/null | awk "{print \$5}")"
              echo "ums function:  $(ls /sys/kernel/config/usb_gadget/rockchip/configs/b.1/ 2>/dev/null | grep -i mass_storage || echo none)"
              echo "ai-hud:        $(pidof ai-hud >/dev/null && echo running || echo NOT RUNNING)"
              echo "hud_live.py:   $(pidof python3 >/dev/null && echo running || echo NOT RUNNING)"'

echo
echo "==> Done. Customer plug-in checklist:"
echo "  - On a Mac, AIHUD volume should appear in Finder shortly"
echo "  - Drag AI-HUD Config.app from there to Applications"
echo "  - Right-click → Open (once) to bypass Gatekeeper"
echo "  - Subsequent double-clicks Just Work"

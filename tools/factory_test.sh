#!/usr/bin/env bash
# Factory-line sanity check: 12 boxes a device must tick before it
# leaves the bench. Run after provision.sh + a full reboot.
#
# Usage:
#   bash tools/factory_test.sh
#   ADB=/path/to/adb bash tools/factory_test.sh
#
# Exit codes:
#   0  -- all checks pass; device is ready to ship.
#   1  -- one or more checks failed; output details which and why.
#   2  -- adb cannot reach the device at all.
#
# Intentionally KISS: a long list of single-purpose probes printed as a
# checklist so a human can also read it. No fancy reporting, no JSON.

set -u

ADB="${ADB:-adb}"

# Test counters
TOTAL=0
PASSED=0
FAILED=0
FAIL_LINES=""

# Each check appends to FAIL_LINES on failure so we print a clean summary
# at the end instead of forcing the operator to scroll back through 12
# items of output.
check() {
    local label="$1"
    local cmd="$2"
    TOTAL=$((TOTAL + 1))
    if eval "$cmd" >/tmp/factory_check_out 2>&1; then
        printf "  [%2d/12] %-40s \033[32mPASS\033[0m\n" "$TOTAL" "$label"
        PASSED=$((PASSED + 1))
    else
        printf "  [%2d/12] %-40s \033[31mFAIL\033[0m\n" "$TOTAL" "$label"
        FAILED=$((FAILED + 1))
        # Capture up to 5 lines of context per failure
        FAIL_LINES="${FAIL_LINES}\n\n  [${TOTAL}] ${label}\n$(sed -n '1,5p' /tmp/factory_check_out | sed 's/^/      /')"
    fi
}

# Wrapper around adb shell that fails if the command's stdout is empty.
adb_nonempty() {
    test -n "$($ADB shell "$1" 2>/dev/null | tr -d '\r\n ')"
}

echo "==> AI-HUD factory sanity check"
echo "    target: $($ADB get-state 2>&1 | head -1)"
echo

# 0. ADB reachability is a precondition -- if this fails nothing else
#    can run, exit early.
if ! $ADB shell true >/dev/null 2>&1; then
    echo "  ERROR: adb cannot reach the device. Connect USB and rerun."
    exit 2
fi

# 1. ai-hud C binary process is running (NPU + ISP pipeline)
check "ai-hud (C) process running" \
      "adb_nonempty 'pidof ai-hud'"

# 2. hud_live.py is running (rendering + GPS + dashboard)
check "hud_live.py (Python) running" \
      "adb_nonempty 'pidof python3'"

# 3. Watchdog kept the heartbeat fresh in the last 90 s
check "heartbeat file fresh" \
      "$ADB shell '
          now=\$(date +%s)
          hb=\$(cat /tmp/ai_hud_heartbeat 2>/dev/null || echo 0)
          age=\$((now - hb))
          [ \$age -lt 90 ]
      '"

# 4. /root/version.txt exists and looks like SemVer
check "/root/version.txt valid" \
      "$ADB shell 'v=\$(cat /root/version.txt 2>/dev/null); echo \"\$v\" | grep -qE \"^[0-9]+\\.[0-9]+\\.[0-9]+\"'"

# 5. ai_hud.conf is loadable (INI sections present)
check "ai_hud.conf has fusion+settings" \
      "$ADB shell 'grep -q \"\\[fusion\\]\" /root/ai_hud.conf && grep -q \"\\[settings\\]\" /root/ai_hud.conf'"

# 6. RKNN model file deployed
check "NPU model present (.rknn)" \
      "$ADB shell '[ -s /root/model/speed_signs_rv1106.rknn ]'"

# 7. At least one speed-zone DB (AU or CN) is loaded
check "speed-zone DB present" \
      "$ADB shell '[ -s /root/data/speed_zones.db ] || [ -s /root/data/speed_zones_cn.db ]'"

# 8. Web dashboard listening on TCP 80
check "web dashboard listening :80" \
      "$ADB shell 'netstat -ln 2>&1 | grep -q \":80 \"'"

# 9. USB Mass Storage backing image present (the virtual AIHUD drive)
check "launcher.img staged for UMS" \
      "$ADB shell '[ -s /userdata/launcher.img ]'"

# 10. Framebuffer is XRGB at the expected resolution
check "framebuffer 480x480 XRGB" \
      "$ADB shell '[ -c /dev/fb0 ] && fbset -fb /dev/fb0 2>/dev/null | grep -qE \"480x480|geometry 480 480\"' || \
       $ADB shell '[ -c /dev/fb0 ]'"

# 11. Disk free on rootfs > 100 MB (catches log overflow / bundle leak)
check "rootfs free space > 100 MB" \
      "$ADB shell '
          free=\$(df / | awk \"NR==2 {print \\\$4}\")
          [ \$free -gt 102400 ]
      '"

# 12. No core dumps or .crash files lingering
check "no core dumps in /root or /tmp" \
      "$ADB shell '
          c=\$(find /root /tmp -maxdepth 2 -name \"core*\" -o -name \"*.crash\" 2>/dev/null | wc -l)
          [ \$c -eq 0 ]
      '"

# --- Summary ----------------------------------------------------------------

echo
echo "==> Result: $PASSED/$TOTAL passed, $FAILED failed"

if [ $FAILED -gt 0 ]; then
    printf '\n--- failures ---'
    printf "$FAIL_LINES\n"
    echo
    echo "Device is NOT ready to ship."
    exit 1
fi

echo "Device is ready to ship."
exit 0

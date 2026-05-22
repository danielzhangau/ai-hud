#!/bin/sh
# Buildroot post-build hook for ai-hud.
#
# Buildroot calls every script registered in BR2_ROOTFS_POST_BUILD_SCRIPT
# with the target rootfs directory as $1, after all packages are installed
# but BEFORE the final filesystem image (rootfs.img / squashfs / ext4) is
# packed. That's the only correct point to drop runtime secrets into
# /etc: any earlier and Buildroot might re-extract a package over it;
# any later and the image is already sealed.
#
# What we inject
# --------------
# /etc/ai_hud_db_secret -- 64 hex chars (or whatever the operator set),
# 0600, will be owned root:root once Buildroot's filesystem stage runs.
# speed_db.py reads this file at startup to verify HMAC-SHA256 signatures
# on speed_zones.db / speed_cameras.db. Without it the device falls back
# to db_signer.py's dev key, which is public and unsafe in production.
#
# Where the secret comes from
# ---------------------------
# AI_HUD_DB_SECRET env var:
#   * GitHub Actions: passed via `env:` in sdk-build.yml from the repo
#     Actions secret of the same name.
#   * Local dev:      export it before running build.sh, or omit to fall
#                     back to the dev key string -- intentionally weak
#                     so a developer can sign a local DB and roundtrip
#                     it through a flashed dev device without GH access.
#
# Why a plain file (not e.g. embedded in a kernel module)
# -------------------------------------------------------
# - Trivially rotated: re-flash with a fresh value, no recompile.
# - Trivially audited: `ls -l /etc/ai_hud_db_secret` on the device.
# - Permission gate already enforced by Linux (0600 + root ownership).

set -eu

TARGET="${1:-}"
if [ -z "$TARGET" ] || [ ! -d "$TARGET" ]; then
    echo "[post_build] ERROR: target directory missing (got '$TARGET')" >&2
    exit 1
fi

SECRET="${AI_HUD_DB_SECRET:-ai-hud-dev-only}"
if [ "$SECRET" = "ai-hud-dev-only" ]; then
    # Loud warning but not fatal -- a local developer building a dev
    # image without exporting the env should still get a working
    # binary, just one that the production OTA pipeline would reject
    # because it was signed with a public key.
    echo "[post_build] WARN: AI_HUD_DB_SECRET unset; baking dev fallback into rootfs." >&2
    echo "[post_build] WARN: This image will NOT verify against production-signed DBs." >&2
fi

install -d -m 0755 "$TARGET/etc"
# `printf %s` (not echo) so we never append a stray newline -- the
# device-side `read_min_epoch` / `load_key` trim trailing whitespace,
# but the HMAC key derivation hashes raw bytes, so any newline would
# silently produce a different 32-byte key from what GitHub Actions
# used to sign.
printf '%s' "$SECRET" > "$TARGET/etc/ai_hud_db_secret"
chmod 0600 "$TARGET/etc/ai_hud_db_secret"

bytes=$(wc -c < "$TARGET/etc/ai_hud_db_secret")
echo "[post_build] wrote /etc/ai_hud_db_secret ($bytes bytes, mode 0600)"

# ---------------------------------------------------------------------------
# Recovery wiring: watchdog feeder + sysrq enable + S99usb0config bashism fix
# ---------------------------------------------------------------------------
# The kernel-side enables (CONFIG_WATCHDOG, CONFIG_MAGIC_SYSRQ, CONFIG_PSTORE)
# are patched into the defconfig by sdk-build.yml. The bits below land the
# *userspace* glue into /etc so a fresh image actually uses them on boot.
# See memory/watchdog-reboot-hang.md for the incident that drove this.
install -d -m 0755 "$TARGET/etc/init.d"

# ---- S97_watchdog: feed /dev/watchdog every 15s, kernel timeout 60s ----
# busybox ships a `watchdog` applet that does this in C with proper
# WDIOC_KEEPALIVE ioctls. We start it via start-stop-daemon so the daemon
# itself is supervised by init.  If for any reason userspace stops feeding,
# the kernel resets the SoC within HW timeout -- the only path that
# recovers from a wedged software `reboot -f`.
cat > "$TARGET/etc/init.d/S97_watchdog" << 'EOF'
#!/bin/sh
# Hardware watchdog feeder (busybox watchdog applet).
# Timer: -T 60s kernel timeout, -t 15s feed interval (4x safety margin).
#
# Shutdown path: rcK runs `S97_watchdog stop` before swapoff/umount/reboot.
# busybox watchdog's SIGTERM handler writes the magic "V" to /dev/watchdog
# before exit, which disables the kernel HW timer -- so the rest of rcK
# can take as long as it needs without racing the 60s kernel deadline.
# This script adds a belt-and-suspenders standalone magic-close AFTER the
# daemon has released the device, in case the busybox version in use
# happens not to honour magic-close (older or stripped builds).

DEV=/dev/watchdog0
PIDFILE=/var/run/watchdog.pid

start() {
    [ -c "$DEV" ] || { echo "[wdt] $DEV missing, hardware watchdog unavailable"; exit 0; }
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "[wdt] already running PID=$(cat "$PIDFILE")"
        return 0
    fi
    /usr/sbin/watchdog -t 15 -T 60 "$DEV" &
    echo $! > "$PIDFILE"
    echo "[wdt] started PID=$(cat "$PIDFILE") (60s HW timeout, 15s feed)"
}

stop() {
    if [ -f "$PIDFILE" ]; then
        _pid=$(cat "$PIDFILE")
        kill "$_pid" 2>/dev/null
        # Wait for daemon to release /dev/watchdog0; without this the
        # belt-and-suspenders open below hits EBUSY.
        for _i in 1 2 3 4 5; do
            kill -0 "$_pid" 2>/dev/null || break
            sleep 0.2
        done
        rm -f "$PIDFILE"
    fi
    # Belt-and-suspenders magic close. If busybox already wrote V on
    # SIGTERM this re-open/close is a no-op (kernel timer already parked).
    # If busybox didn't, opening /dev/watchdog restarts the timer briefly
    # then writing V parks it again before close.
    if [ -c "$DEV" ]; then
        printf 'V' > "$DEV" 2>/dev/null || true
    fi
    echo "[wdt] stopped, HW timer parked"
}

case "$1" in
    start) start ;;
    stop)  stop  ;;
    restart) stop; start ;;
    *) echo "Usage: $0 {start|stop|restart}"; exit 1 ;;
esac
EOF
chmod 0755 "$TARGET/etc/init.d/S97_watchdog"
echo "[post_build] wrote /etc/init.d/S97_watchdog"

# ---- S97_enable_sysrq: turn on /proc/sys/kernel/sysrq early ----
# Default-enable is set in defconfig (CONFIG_MAGIC_SYSRQ_DEFAULT_ENABLE=0x1)
# but only kernel param sysrq_always_enabled=1 makes it survive bootup
# without procfs writes. Belt-and-suspenders: also write the procfs node so
# the value is observable regardless of build flag interpretation.
cat > "$TARGET/etc/init.d/S97_enable_sysrq" << 'EOF'
#!/bin/sh
case "$1" in
    start)
        [ -w /proc/sys/kernel/sysrq ] && echo 1 > /proc/sys/kernel/sysrq
        ;;
    stop|restart) ;;
esac
exit 0
EOF
chmod 0755 "$TARGET/etc/init.d/S97_enable_sysrq"
echo "[post_build] wrote /etc/init.d/S97_enable_sysrq"

# ---- S99usb0config bashism fix ----
# Vendor SDK uses `[[ ... && ... ]]` which is a bash builtin; on busybox sh
# this can spin forever instead of exiting on MAX_RETRIES. Replace with
# POSIX `[ ... ] && [ ... ]`.  Idempotent.
USBCFG="$TARGET/etc/init.d/S99usb0config"
if [ -f "$USBCFG" ]; then
    if grep -q '\[\[.*&&.*\]\]' "$USBCFG"; then
        # No `sed -i` -- BSD sed needs an extra "" arg, GNU doesn't.
        # Round-tripping through a tmp file is portable.
        _tmp=$(mktemp "${USBCFG}.XXXXXX")
        sed 's/\[\[[[:space:]]*"$current_ip"[[:space:]]*!=[[:space:]]*"$TARGET_IP"[[:space:]]*&&[[:space:]]*$retries[[:space:]]*-lt[[:space:]]*$MAX_RETRIES[[:space:]]*\]\]/[ "$current_ip" != "$TARGET_IP" ] \&\& [ "$retries" -lt "$MAX_RETRIES" ]/' "$USBCFG" > "$_tmp"
        mv "$_tmp" "$USBCFG"
        chmod 0755 "$USBCFG"
        echo "[post_build] patched S99usb0config bashism"
    else
        echo "[post_build] S99usb0config bashism already patched (no-op)"
    fi
else
    echo "[post_build] S99usb0config not present, skipping"
fi

# ---- Reserved-memory node for ramoops is provided by kernel/DTS once
# CONFIG_PSTORE_RAM is on. The /sys/fs/pstore mount appears automatically
# on boot. No userspace glue needed here.

echo "[post_build] recovery wiring installed"

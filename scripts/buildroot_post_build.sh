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

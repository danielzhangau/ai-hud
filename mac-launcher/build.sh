#!/bin/bash
# Build "AI-HUD Config.app" -- a macOS .app bundle that wraps adb and opens
# the on-device web UI in the browser.
#
# Run from anywhere:  ./mac-launcher/build.sh
# Output:             mac-launcher/dist/AI-HUD Config.app
#
# Bundles Google's official Android Platform Tools `adb` binary (Universal
# Binary, ~5 MB) so users don't need to install anything else.

set -euo pipefail

# Where this script lives. We use realpath dance because macOS bash 3.2 has
# no `readlink -f`.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
DIST_DIR="$SCRIPT_DIR/dist"
APP_NAME="AI-HUD Config"
APP_DIR="$DIST_DIR/$APP_NAME.app"
CACHE_DIR="$SCRIPT_DIR/.cache"

# Official Apple-Silicon-compatible Universal adb. URL is the canonical
# Google CDN endpoint; sha256 verified after download.
PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 missing"; exit 1; }
}

require curl
require unzip
require plutil

echo "==> Cleaning dist..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR" "$CACHE_DIR"

# --- Fetch adb ---------------------------------------------------------------
ADB_ZIP="$CACHE_DIR/platform-tools-darwin.zip"
ADB_BIN="$CACHE_DIR/platform-tools/adb"

if [ ! -x "$ADB_BIN" ]; then
    echo "==> Downloading Android Platform Tools..."
    curl -L --fail --progress-bar -o "$ADB_ZIP" "$PLATFORM_TOOLS_URL"
    rm -rf "$CACHE_DIR/platform-tools"
    unzip -q "$ADB_ZIP" -d "$CACHE_DIR"
fi

[ -x "$ADB_BIN" ] || { echo "ERROR: adb not found after extract"; exit 2; }
echo "    adb: $(file "$ADB_BIN" | sed 's|.*: ||')"

# --- Assemble .app bundle ----------------------------------------------------
echo "==> Assembling $APP_NAME.app..."
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cp "$SRC_DIR/Info.plist"   "$APP_DIR/Contents/Info.plist"
cp "$SRC_DIR/launch.sh"    "$APP_DIR/Contents/MacOS/launch"
cp "$ADB_BIN"              "$APP_DIR/Contents/MacOS/adb"
# OTA updater + mirror config -- the launch script invokes these to
# offer a one-click bundle install when the device is on an older tag.
cp "$SRC_DIR/updater.py"   "$APP_DIR/Contents/MacOS/updater.py"
cp "$SRC_DIR/mirrors.conf" "$APP_DIR/Contents/MacOS/mirrors.conf"
# Platform tools needs a couple of co-located libs (e.g. libc++.dylib) on
# some macOS versions -- they live next to adb in platform-tools/. Copy any
# .dylib siblings just in case.
for dylib in "$CACHE_DIR/platform-tools/"*.dylib; do
    [ -f "$dylib" ] && cp "$dylib" "$APP_DIR/Contents/MacOS/"
done

chmod +x "$APP_DIR/Contents/MacOS/launch" "$APP_DIR/Contents/MacOS/adb"

# Validate the plist (catches typos that would prevent Finder from
# recognizing the bundle).
plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null

# --- Ad-hoc codesign ---------------------------------------------------------
# On Apple Silicon, unsigned binaries inside an app bundle can be killed by
# Gatekeeper. An ad-hoc signature (`-`) doesn't establish identity but does
# satisfy the kernel's "must be signed" check, so the app launches without
# the user having to disable SIP.
if command -v codesign >/dev/null 2>&1; then
    echo "==> Ad-hoc codesigning..."
    codesign --force --deep --sign - "$APP_DIR" 2>&1 | tail -3 || true
fi

# --- Build distributable zip ------------------------------------------------
# Ships the .app together with the Chinese end-user instructions so the
# recipient gets everything needed in one download.
echo "==> Packaging distributable zip..."
cp "$SCRIPT_DIR/HOW-TO-OPEN.md" "$DIST_DIR/HOW-TO-OPEN.md"
DIST_ZIP="$DIST_DIR/AI-HUD Config.zip"
rm -f "$DIST_ZIP"
( cd "$DIST_DIR" && zip -ryq "AI-HUD Config.zip" "AI-HUD Config.app" "HOW-TO-OPEN.md" )

echo
echo "==> Done."
echo "    Bundle:        $APP_DIR ($(du -sh "$APP_DIR" | awk '{print $1}'))"
echo "    Distributable: $DIST_ZIP ($(du -sh "$DIST_ZIP" | awk '{print $1}'))"
echo
echo "Try locally:  open '$APP_DIR'"
echo "To share:     send 'AI-HUD Config.zip' to the end user"

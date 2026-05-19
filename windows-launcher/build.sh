#!/usr/bin/env bash
# Pack the Windows launcher into a distributable zip. Runs on macOS or
# Linux dev hosts (we're not building on Windows -- the launcher itself
# is just PowerShell + .bat + adb.exe, no Windows-specific tooling).
#
# Output:
#   windows-launcher/dist/AI-HUD Config (Windows).zip
#
# Customer extracts the zip, double-clicks "Run AI-HUD Config.bat", done.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
DIST_DIR="$SCRIPT_DIR/dist"
CACHE_DIR="$SCRIPT_DIR/.cache"
STAGE_DIR="$DIST_DIR/AI-HUD Config"   # what the customer sees inside the zip

# Google's Android Platform Tools for Windows: zipped, ~10 MB, contains
# adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll which we need together (adb
# refuses to start without the matching DLLs on Windows).
PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools-latest-windows.zip"

require() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 missing"; exit 1; }; }
require curl
require unzip
require zip

echo "==> Cleaning dist..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR" "$CACHE_DIR" "$STAGE_DIR"

# --- Fetch Windows adb -------------------------------------------------------
ADB_ZIP="$CACHE_DIR/platform-tools-windows.zip"
ADB_DIR="$CACHE_DIR/platform-tools"

if [ ! -f "$ADB_DIR/adb.exe" ]; then
    echo "==> Downloading Windows Platform Tools (adb.exe + DLLs)..."
    # Google CDN occasionally drops the connection mid-stream from CN
    # networks; --retry + --retry-all-errors + -C - lets us resume the
    # partially-downloaded file instead of starting over.
    curl -L --fail --progress-bar \
         --retry 5 --retry-delay 4 --retry-all-errors \
         --connect-timeout 15 --max-time 300 \
         -C - \
         -o "$ADB_ZIP" "$PLATFORM_TOOLS_URL"
    rm -rf "$ADB_DIR"
    unzip -q "$ADB_ZIP" -d "$CACHE_DIR"
fi
[ -f "$ADB_DIR/adb.exe" ] || { echo "ERROR: adb.exe not found"; exit 2; }

# --- Stage launcher files ---------------------------------------------------
echo "==> Staging launcher files..."
# .bat: must keep CRLF line endings or some Windows shells choke.
# (The file was written from macOS with LF; converting here is safer
# than asking devs to maintain CRLF in the repo.)
awk '{ sub(/\r$/, ""); printf "%s\r\n", $0 }' \
    "$SRC_DIR/Run AI-HUD Config.bat" > "$STAGE_DIR/Run AI-HUD Config.bat"
# .ps1 files can stay LF -- PowerShell handles either, and keeping LF
# means our git diff stays sane.
cp "$SRC_DIR/launcher.ps1"     "$STAGE_DIR/launcher.ps1"
cp "$SRC_DIR/updater.ps1"      "$STAGE_DIR/updater.ps1"
cp "$SRC_DIR/mirrors.conf"     "$STAGE_DIR/mirrors.conf"
# Bundled tools
cp "$ADB_DIR/adb.exe"          "$STAGE_DIR/adb.exe"
cp "$ADB_DIR/AdbWinApi.dll"    "$STAGE_DIR/AdbWinApi.dll"
cp "$ADB_DIR/AdbWinUsbApi.dll" "$STAGE_DIR/AdbWinUsbApi.dll"

# --- Zip it -----------------------------------------------------------------
DIST_ZIP="$DIST_DIR/AI-HUD Config (Windows).zip"
echo "==> Packaging $DIST_ZIP..."
# -X drops macOS extended attribute records; -r recurse; -q quiet.
( cd "$DIST_DIR" && zip -rXq "$(basename "$DIST_ZIP")" "AI-HUD Config" )

# --- Report -----------------------------------------------------------------
SIZE="$(du -h "$DIST_ZIP" | awk '{print $1}')"
echo
echo "==> Done."
echo "    Output: $DIST_ZIP ($SIZE)"
echo
echo "Distribute by giving the user this zip. On their Windows box:"
echo "  1. Extract it"
echo "  2. Plug AI-HUD device in"
echo "  3. Double-click 'Run AI-HUD Config.bat'"
echo "  4. If SmartScreen warns, click 'More info' -> 'Run anyway'"

#!/usr/bin/env bash
# Open the AI-HUD web configuration UI in the default browser.
#
# Once the device is plugged in, the on-board DHCP + mDNS daemons make
# http://ai-hud.local/ reachable directly -- no adb required. This script
# tries that first. As a developer fallback it also supports adb forward
# (e.g. when the USB Ethernet gadget isn't recognized by the host).
#
# Usage:
#   ./scripts/web_config.sh                # auto: mDNS first, then adb
#   ./scripts/web_config.sh --adb          # force adb forward path
#   PORT=80 ./scripts/web_config.sh        # override device port

set -euo pipefail

PORT="${PORT:-80}"
MDNS_URL="http://ai-hud.local${PORT:+:${PORT/#80/}}"
MDNS_URL="${MDNS_URL%:}"      # strip trailing ':' when PORT=80

force_adb=0
if [ "${1:-}" = "--adb" ]; then
  force_adb=1
fi

open_url() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$url"
  else
    echo "[info] open this URL manually: $url"
  fi
}

# --- Path A: USB Ethernet + mDNS -------------------------------------------
if [ "$force_adb" -eq 0 ]; then
  echo -n "[info] probing ${MDNS_URL}/api/state ... "
  if curl -sf -o /dev/null --max-time 2 "${MDNS_URL}/api/state"; then
    echo "ok"
    open_url "${MDNS_URL}"
    exit 0
  fi
  echo "no response -- falling back to adb"
fi

# --- Path B: adb forward fallback ------------------------------------------
if ! command -v adb >/dev/null 2>&1; then
  echo "[error] adb not found in PATH (needed for fallback)" >&2
  echo "[hint]  install Android Platform Tools or check USB Ethernet is up" >&2
  exit 1
fi

if ! adb get-state >/dev/null 2>&1; then
  echo "[error] no adb device attached" >&2
  exit 1
fi

LOCAL_PORT=8080
echo "[info] adb forward tcp:${LOCAL_PORT} tcp:${PORT}"
adb forward tcp:"${LOCAL_PORT}" tcp:"${PORT}"

FALLBACK_URL="http://localhost:${LOCAL_PORT}"
echo -n "[info] probing ${FALLBACK_URL}/api/state ... "
if curl -sf -o /dev/null --max-time 2 "${FALLBACK_URL}/api/state"; then
  echo "ok"
else
  echo "no response"
  echo "[hint] is hud_live.py running? Check: adb shell 'ps | grep hud_live'" >&2
  exit 2
fi

open_url "${FALLBACK_URL}"

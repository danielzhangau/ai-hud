"""AI-HUD firmware flasher (macOS launcher side).

Invoked by launch.sh when ioreg shows the device in MaskROM (Rockchip
VID 0x2207, PID 0x110b) or Loader (PID 0x110a) state -- i.e. the
customer just held BOOT and replugged USB.

Pulls the latest firmware (`update.img`) from the GitHub Release that
matches the current launcher version, downloads it once and caches in
~/Library/Caches/AI-HUD/, asks the user for admin rights via osascript
(upgrade_tool needs raw USB access), and runs upgrade_tool UF.

Stdlib only.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


GITHUB_REPO = "danielzhangau/ai-hud"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

ROCKCHIP_VID = 8711           # 0x2207
PID_ADB     = 25              # 0x0019 -- normal Linux/ADB mode
# MaskROM and Loader appear under slightly different PIDs depending on
# the Rockchip SoC generation. RV1106 specifically uses 0x110c for
# MaskROM (confirmed on hardware 2026-05-19). Earlier docs sometimes
# show 0x110b which is RK29-era. We accept both ranges as flash-mode.
PIDS_LOADER  = (4362,)               # 0x110a
PIDS_MASKROM = (4363, 4364)          # 0x110b, 0x110c

CACHE_DIR = os.path.expanduser("~/Library/Caches/AI-HUD")


# --- macOS dialogs ----------------------------------------------------------

def _osascript(script):
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=900)
        return r.returncode, r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""


def notify(msg, icon="note"):
    """Modal dialog with an OK button. icon: note, caution, stop."""
    _osascript(
        f'display dialog "{msg}" with title "AI-HUD Firmware" '
        f'buttons {{"OK"}} default button "OK" with icon {icon}'
    )


def confirm(msg, default_button="Flash"):
    rc, out = _osascript(
        f'display dialog "{msg}" with title "AI-HUD Firmware" '
        f'buttons {{"Cancel", "{default_button}"}} '
        f'default button "{default_button}" with icon note'
    )
    return default_button in out


# --- USB-state detection ----------------------------------------------------

def detect_rockchip_state():
    """Return one of: 'adb', 'maskrom', 'loader', 'none', 'unknown:<pid>'.

    Walks `ioreg -p IOUSB -l` and looks for any USB device whose idVendor
    field matches Rockchip (0x2207). We match the numeric VID rather than
    the "rockchip" vendor-name string because MaskROM-mode devices ship
    string descriptors all zero -- only the numeric VID/PID survive.
    Confirmed 2026-05-19 on RV1106 in MaskROM: iManufacturer=0, iProduct=0,
    no "kUSBVendorString" field exists, but idVendor=8711 is still there.

    If several Rockchip devices are attached at once we pick the most
    "we should flash this" state: MaskROM beats Loader beats ADB.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "none"

    # Walk each IOUSBHostDevice block looking for our VID. ioreg formats
    # one device per `+-o IOUSBHostDevice@...` block; idVendor/idProduct
    # appear inside. We don't assume any specific ordering of fields
    # within a block, so we collect them and only emit a (vid, pid) pair
    # when we leave the block.
    products = []
    cur_vid = None
    cur_pid = None
    for line in out.splitlines():
        if "IOUSBHostDevice" in line and "+-o" in line:
            # New device block -- flush previous
            if cur_vid == ROCKCHIP_VID and cur_pid is not None:
                products.append(cur_pid)
            cur_vid = None
            cur_pid = None
            continue
        m = re.search(r'"idVendor"\s*=\s*(\d+)', line)
        if m:
            cur_vid = int(m.group(1))
            continue
        m = re.search(r'"idProduct"\s*=\s*(\d+)', line)
        if m:
            cur_pid = int(m.group(1))
    # Final flush at EOF
    if cur_vid == ROCKCHIP_VID and cur_pid is not None:
        products.append(cur_pid)

    if not products:
        return "none"
    if any(p in PIDS_MASKROM for p in products):
        return "maskrom"
    if any(p in PIDS_LOADER for p in products):
        return "loader"
    if PID_ADB in products:
        return "adb"
    return f"unknown:{products[0]}"


# --- GitHub Release lookup --------------------------------------------------

def _http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-hud-flasher"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _find_firmware_asset(release_json):
    """Return (filename, download_url, expected_sha256) for the update.img."""
    sums = {}
    sums_url = None
    img_name = None
    img_url = None
    for a in release_json.get("assets", []):
        n = a.get("name", "")
        if n == "update.img":
            img_name = n
            img_url = a.get("browser_download_url")
        elif n == "SHA256SUMS":
            sums_url = a.get("browser_download_url")
    expected_sha = None
    if img_name and sums_url:
        try:
            req = urllib.request.Request(
                sums_url, headers={"User-Agent": "ai-hud-flasher"})
            with urllib.request.urlopen(req, timeout=8) as r:
                for line in r.read().decode("utf-8", errors="ignore").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].endswith(img_name):
                        expected_sha = parts[0]
                        break
        except (urllib.error.URLError, TimeoutError):
            pass
    return img_name, img_url, expected_sha


def _sha256_file(path, progress=None):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    seen = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            seen += len(chunk)
            if progress:
                progress(seen, size)
    return h.hexdigest()


def _download_with_progress(url, dest, label, expected_sha=None):
    """Stream `url` to `dest`. Prints percent to stdout so the launch
    script can show progress in a Terminal-like context. Returns True
    on success / False on error (errors logged but not raised)."""
    req = urllib.request.Request(url, headers={"User-Agent": "ai-hud-flasher"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            seen = 0
            last_pct = -1
            with open(dest, "wb") as out:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    seen += len(chunk)
                    if total:
                        pct = int(seen * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            print(f"  {label}: {pct}% ({seen // 1024 // 1024} MB)",
                                  flush=True)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  download failed: {e}")
        return False

    if expected_sha:
        actual = _sha256_file(dest)
        if actual != expected_sha:
            print(f"  checksum mismatch: expected {expected_sha}, got {actual}")
            return False
        print(f"  checksum ok ({expected_sha[:8]}...)")
    return True


# --- Flash orchestration ----------------------------------------------------

def _run_upgrade_tool(upgrade_tool, image_path):
    """Run `upgrade_tool UF <img>` via sudo (osascript admin privileges).

    upgrade_tool needs raw USB access -- /dev/bulk endpoints -- which on
    macOS means root. osascript "with administrator privileges" hands
    the standard macOS auth dialog to the user.
    """
    # Build the shell command. Quote both paths because they're inside
    # the .app bundle which has spaces ("AI-HUD Config.app").
    cmd = f"'{upgrade_tool}' UF '{image_path}'"
    # We run sh -c so stdout/stderr both stream to the (hidden) terminal
    # that osascript runs in; we won't see them, but upgrade_tool's
    # progress goes to that buffer. The user sees a separate macOS
    # progress dialog (below) while this runs.
    script = (
        f'do shell script "{cmd} 2>&1" '
        f'with administrator privileges'
    )
    print(f"  invoking upgrade_tool (admin password prompt may appear)...")
    rc, out = _osascript(script)
    if rc != 0:
        # User cancelled the admin dialog, or upgrade_tool failed.
        return False, out
    return True, out


def flash(args):
    state = detect_rockchip_state()
    print(f"[flasher] device state: {state}")
    if state not in ("maskrom", "loader"):
        notify("The device is not in firmware-flash mode.\n\n"
               "To enter it: unplug USB, hold the BOOT button on the "
               "board, plug USB back in, release BOOT after 2 seconds, "
               "then run this app again.", icon="caution")
        return 1

    # Confirm with the user -- firmware flashing is destructive and takes
    # several minutes. Note we explicitly mention the reboot.
    if not confirm(
        "AI-HUD device detected in firmware-flash mode.\n\n"
        "Flashing firmware:\n"
        "  - downloads ~450 MB (if not cached)\n"
        "  - replaces the kernel and root filesystem\n"
        "  - reboots the device\n\n"
        "Total time: 5-10 minutes. Do not unplug USB during this.\n\n"
        "Continue?"
    ):
        print("[flasher] user cancelled")
        return 0

    # Look up the latest GitHub Release and locate update.img.
    print("[flasher] fetching latest release info...")
    try:
        rel = _http_get_json(GITHUB_API)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        notify(f"Couldn't fetch the latest release from GitHub:\n{e}\n\n"
               "Check your internet connection and try again.",
               icon="stop")
        return 2

    version = rel.get("tag_name", "")
    img_name, img_url, expected_sha = _find_firmware_asset(rel)
    if not img_url:
        notify(f"No update.img found in release {version}.\n\n"
               "The release might not include firmware yet -- contact "
               "your distributor.", icon="stop")
        return 2

    # Cache key by version + sha so re-flashing the same release skips
    # the download. Different releases get different cache entries.
    os.makedirs(CACHE_DIR, exist_ok=True)
    short_sha = (expected_sha or "nosum")[:12]
    cached = os.path.join(CACHE_DIR, f"update-{version}-{short_sha}.img")

    if os.path.isfile(cached):
        # Re-verify cached file in case the user mucked with it.
        if expected_sha:
            print(f"[flasher] verifying cached {cached}")
            if _sha256_file(cached) == expected_sha:
                print("[flasher] cache hit (sha verified)")
            else:
                print("[flasher] cache file corrupt, re-downloading")
                os.unlink(cached)
        else:
            print("[flasher] cache hit (no checksum available)")

    if not os.path.isfile(cached):
        # Test mode: when AI_HUD_OTA_MIRROR_FIRST=1 try localhost:8000/update.img
        # before GitHub. Lets a developer point a local `python3 -m http.server`
        # at a pre-downloaded firmware image and skip the 478 MB CDN pull on
        # every iteration. Production users never set the env var.
        urls_to_try = []
        if os.environ.get("AI_HUD_OTA_MIRROR_FIRST"):
            urls_to_try.append("http://localhost:8000/" + img_name)
        urls_to_try.append(img_url)

        downloaded = False
        for u in urls_to_try:
            print(f"[flasher] downloading {img_name} ({u})...")
            if _download_with_progress(u, cached, "downloading", expected_sha):
                downloaded = True
                break
            print(f"[flasher]   source failed: {u}")

        if not downloaded:
            notify("Firmware download failed.\n\n"
                   "Check your internet connection and try again.",
                   icon="stop")
            try:
                os.unlink(cached)
            except OSError:
                pass
            return 2

    # Tell the user the long-running step is starting. osascript dialogs
    # are modal, so we don't pre-show "flashing in progress" -- the
    # admin-prompt itself signals to the user that the next step needs
    # their password.
    print("[flasher] starting upgrade_tool (this takes 3-5 min)")
    ok, out = _run_upgrade_tool(args.upgrade_tool, cached)
    print(f"[flasher] upgrade_tool finished, rc={'ok' if ok else 'fail'}")
    print(out[-1000:] if out else "")

    if not ok:
        notify("Firmware flashing failed.\n\n"
               "Possible causes:\n"
               "  - You cancelled the password prompt\n"
               "  - USB cable was unplugged mid-flash\n"
               "  - Device fell out of MaskROM mode\n\n"
               "To retry: hold BOOT, replug USB, and run this app again.",
               icon="stop")
        return 3

    notify(f"Firmware {version} flashed successfully.\n\n"
           "The device will reboot in a moment. Once it's back online "
           "the launcher will offer to push the matching code update.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upgrade-tool", required=True,
                    help="Path to the bundled upgrade_tool binary")
    args = ap.parse_args()
    return flash(args)


if __name__ == "__main__":
    sys.exit(main() or 0)

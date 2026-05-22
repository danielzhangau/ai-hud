"""AI-HUD over-the-air updater (macOS launcher side).

Invoked by launch.sh after the device is reachable over adb. Compares
the device's /root/version.txt to the latest GitHub Release tag and, if
older, downloads the matching update-bundle-v<X.Y.Z>.zip and pushes its
contents to the device.

Stdlib-only. macOS bundles Python 3 since Monterey -- no extra runtime
needs to ship inside the .app.

Exit codes:
   0 -- nothing to do (up to date, user skipped, or device offline)
   2 -- update attempted but failed; user has been notified
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile


GITHUB_REPO = "danielzhangau/ai-hud"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Optional mirror config -- launcher ships a mirrors.conf next to this
# script. Each non-empty, non-comment line is a base URL the launcher
# falls back to when GitHub is unreachable. Bundle filename is appended
# to each entry. Lets a Chinese-mainland user point at a Gitee /
# self-hosted CDN copy without rebuilding the .app.
def _load_mirror_urls(script_dir):
    cfg = os.path.join(script_dir, "mirrors.conf")
    urls = []
    try:
        with open(cfg) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                urls.append(line.rstrip("/") + "/")
    except OSError:
        pass
    return urls


# --- Native macOS dialogs ---------------------------------------------------

def _osascript(*args):
    try:
        return subprocess.run(
            ["osascript"] + list(args),
            capture_output=True, text=True, timeout=120,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def notify(msg, icon="note"):
    """Information dialog with an OK button. Best-effort -- silent on error."""
    _osascript(
        "-e",
        f'display dialog "{msg}" with title "AI-HUD Updater" '
        f'buttons {{"OK"}} default button "OK" with icon {icon}',
    )


def progress_notify(msg):
    """Non-modal banner notification (macOS Notification Center). Doesn't
    block the updater flow and doesn't require an OK click. Used so the
    user sees forward progress during the ~30 second download+push+reboot
    window instead of staring at a frozen launcher (P1, 2026-05-22).

    Silent on error: if the user denied notification permission to
    osascript, this is a no-op rather than a hard failure.
    """
    # Escape double quotes in the message body so the AppleScript
    # string literal stays well-formed.
    safe = msg.replace('"', '\\"')
    _osascript(
        "-e",
        f'display notification "{safe}" with title "AI-HUD Updater"',
    )


def confirm_update(current, latest):
    """Ask the user to authorize the update; returns True iff they accept."""
    out = _osascript(
        "-e",
        f'display dialog "A new AI-HUD version is available.\\n\\n'
        f'Current: {current}\\nNew: {latest}\\n\\nUpdate now? (about 30 seconds)" '
        f'with title "AI-HUD Updater" '
        f'buttons {{"Skip", "Update"}} default button "Update" with icon note',
    )
    return "Update" in out


# --- adb helpers ------------------------------------------------------------

def _adb(adb_path, *args, capture=True, timeout=30):
    cmd = [adb_path] + list(args)
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    r = subprocess.run(cmd, timeout=timeout)
    return r.returncode, "", ""


def _wait_for_device(adb_path, max_wait_s=60):
    """Block until adb sees a connected device or the timeout elapses.

    `adb wait-for-device` would do this in one call, but it has no
    timeout and would hang the launcher forever if the user yanked the
    cable mid-reboot. So we poll instead.
    """
    import time
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        rc, out, _ = _adb(adb_path, "devices", timeout=4)
        for line in out.splitlines()[1:]:
            if line.endswith("\tdevice"):
                # Give init.d another second to bring up hud_live.
                time.sleep(2)
                return True
        time.sleep(1)
    return False


def _device_version(adb_path):
    rc, out, _ = _adb(adb_path, "shell", "cat /root/version.txt 2>/dev/null")
    if rc == 0:
        v = out.strip().split("\n", 1)[0].strip()
        if v:
            return v
    return "0.0.0"  # treat missing as ancient so we offer to update


def _wait_for_web_server(adb_path, timeout_s=30):
    """After reboot, wait until hud_live.py's web server answers on
    device :80. _wait_for_device returns as soon as adb sees the
    device -- that's just the ADB gadget, not hud_live.py which takes
    another 10-20 seconds to import its modules and bind :80.

    Polling via `adb shell wget` rather than via the host-side adb
    forward, because the forward may have been torn down by the reboot
    and isn't re-bound until launch.sh runs after the updater returns
    (P2, 2026-05-22).
    """
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rc, out, _ = _adb(
            adb_path, "shell",
            "wget -qO- --timeout=1 http://127.0.0.1/api/state 2>/dev/null "
            "| head -c 1",
            timeout=3,
        )
        if rc == 0 and out.strip():
            return True
        time.sleep(1)
    return False


def _write_device_update_status(adb_path, current, latest, available, url=None):
    """Write a small JSON sidecar at /tmp/ai_hud_update_status.json on the
    device so hud_live.py's dashboard can show a "new version" banner.

    Called after every GitHub probe -- including when the user declines
    the upgrade or when nothing newer is available. The device's
    /api/state surfaces the file; if it's missing the banner stays
    hidden (graceful degrade).

    Best-effort: if adb push fails we just log and return -- the banner
    will simply be stale until the next launcher run.
    """
    import time
    payload = json.dumps({
        "current":          current,
        "latest":           latest,
        "update_available": bool(available),
        "checked_at":       int(time.time()),
        "url":              url,
    })
    # adb shell quoting is unreliable for JSON (curly braces, quotes);
    # write to a host tempfile then `adb push`.
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        rc, _, err = _adb(adb_path, "push", tmp_path,
                          "/tmp/ai_hud_update_status.json", timeout=8)
        if rc != 0:
            print(f"[updater] could not write update-status sidecar: {err}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# --- HTTP helpers -----------------------------------------------------------

def _http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-hud-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _http_download(url, dest, timeout=60):
    """Stream a URL to a local path. Raises on HTTP errors / timeouts."""
    req = urllib.request.Request(url, headers={"User-Agent": "ai-hud-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        with open(dest, "wb") as out:
            shutil.copyfileobj(r, out)


def _find_bundle_asset(release_json):
    """Find the update-bundle zip asset URL inside a Releases API payload."""
    for a in release_json.get("assets", []):
        n = a.get("name", "")
        if n.startswith("update-bundle-") and n.endswith(".zip"):
            return n, a.get("browser_download_url")
    return None, None


def _semver_tuple(v):
    """Best-effort tuple form so comparisons handle '0.1.0' vs '0.10.0'."""
    parts = v.lstrip("v").split("-")[0].split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        # If the string isn't a clean SemVer, fall back to string compare.
        return (-1,)


def _version_gap_too_large(current, latest):
    """Refuse OTA when the version gap is wide enough that OS-layer
    changes risk leaving the device in an unrecoverable state.

    The OTA bundle only touches application layer: /root/*.py, the
    ai-hud binary, init scripts, databases. It assumes the kernel and
    rootfs are compatible. Across a single minor bump (0.1.x -> 0.2.x)
    that's safe. Across two minor bumps or a major bump the new init
    scripts (S50usbdevice / S99usbnetd) may reference configfs paths
    or kernel modules the older rootfs/kernel doesn't expose, which
    can break the USB gadget and leave a device without ADB.

    Note 2026-05-22: an observed 0.1.0 -> 0.3.0 OTA looked bricked --
    USB went silent for ~30 min, display stayed black. Eventually it
    recovered: the boot delay was slow first-boot init (speed-DB
    HMAC verify on 99 MB of zones, ISP calibration, NPU model load)
    rather than a true brick. But the user-visible state was
    indistinguishable from a hard failure during that window. Even if
    the device usually recovers, "looks bricked for 30 min" is a bad
    UX for an end user who'll think it's dead and try to force-reset.
    Combined with the real risk of true bricking on older kernels,
    conservatism wins -- refuse the OTA and route the user to the
    full firmware-flash path instead.

    Heuristic: refuse when minor gap > 1, or major differs at all.
    Returns False on parse failure so we don't block legitimate updates
    due to an unrecognised version string.
    """
    def _major_minor(v):
        parts = v.lstrip("v").split("-")[0].split(".")
        try:
            return (int(parts[0]),
                    int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return None
    c = _major_minor(current)
    l = _major_minor(latest)
    if c is None or l is None:
        return False
    if c[0] != l[0]:
        return True
    return (l[1] - c[1]) > 1


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --- The actual update orchestration ---------------------------------------

def perform_update(adb_path, bundle_path):
    """Unpack the bundle, push each file, verify checksums, run post-deploy."""
    workdir = tempfile.mkdtemp(prefix="ai_hud_bundle_")
    try:
        with zipfile.ZipFile(bundle_path) as z:
            z.extractall(workdir)
        with open(os.path.join(workdir, "manifest.json")) as f:
            manifest = json.load(f)

        if manifest.get("schema") != 1:
            raise RuntimeError(
                f"unknown manifest schema: {manifest.get('schema')}")

        total = len(manifest["files"])
        for i, entry in enumerate(manifest["files"], 1):
            local = os.path.join(workdir, entry["src"])
            dest = entry["dest"]
            mode = entry.get("mode", "0644")

            if not os.path.isfile(local):
                raise RuntimeError(f"missing file in bundle: {entry['src']}")
            actual = _sha256(local)
            if actual != entry["sha256"]:
                raise RuntimeError(
                    f"checksum mismatch for {entry['src']}: "
                    f"manifest={entry['sha256']} actual={actual}")

            # Ensure dest dir exists on device before push.
            dest_dir = os.path.dirname(dest)
            if dest_dir and dest_dir != "/":
                _adb(adb_path, "shell", f"mkdir -p {dest_dir}")

            print(f"  [{i}/{total}] {dest}")
            rc, _, err = _adb(adb_path, "push", local, dest, timeout=60)
            if rc != 0:
                raise RuntimeError(f"adb push failed for {dest}: {err}")
            _adb(adb_path, "shell", f"chmod {mode} {dest}")

        # post_deploy commands run sequentially -- failures here are
        # warnings (e.g. a stale .pyc cleanup that finds no .pyc to clean
        # exits non-zero on some busybox builds), not fatal.
        triggers_reboot = False
        for cmd in manifest.get("post_deploy", []):
            print(f"  post-deploy: {cmd}")
            is_reboot = cmd.strip() == "reboot"
            if is_reboot:
                triggers_reboot = True
            try:
                # `reboot` is fire-and-forget -- adb-shell exits non-zero
                # the moment the kernel pulls the rug. We ignore the
                # return code and rely on _wait_for_device below.
                _adb(adb_path, "shell", cmd,
                     timeout=4 if is_reboot else 10)
            except subprocess.TimeoutExpired:
                if not is_reboot:
                    print(f"  post-deploy: {cmd!r} timed out (proceeding)")

        # If post_deploy issued a reboot, wait for the device to come
        # back online so the caller can re-establish the adb forward
        # without an extra retry from the user.
        if triggers_reboot:
            print("  waiting for device to come back from reboot...")
            _wait_for_device(adb_path, max_wait_s=60)

        return manifest["version"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", required=True, help="Path to the bundled adb")
    ap.add_argument("--script-dir", required=True,
                    help="Directory containing mirrors.conf")
    args = ap.parse_args()

    # 1. Read device's current version
    current = _device_version(args.adb)
    print(f"[updater] device version: {current}")

    # 2. Probe GitHub for the latest release
    try:
        release = _http_get_json(GITHUB_API)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        # Update check is best-effort; if it can't reach GitHub we don't
        # block the user from opening the dashboard.
        print(f"[updater] GitHub probe failed: {e}")
        return 0

    latest = release.get("tag_name", "").lstrip("v")
    if not latest:
        print("[updater] no tag_name in release response")
        return 0
    print(f"[updater] latest release: {latest}")

    available = _semver_tuple(latest) > _semver_tuple(current)
    # Write update-status sidecar regardless of whether we'll apply --
    # the dashboard banner needs to know about pending versions even if
    # the user just dismissed our dialog. Done before the dialog so a
    # cancelled dialog still leaves the banner up.
    _write_device_update_status(
        args.adb, current=current, latest=latest,
        available=available, url=release.get("html_url"),
    )

    if not available:
        print("[updater] device is up to date")
        return 0

    # 2a. Guardrail: refuse OTA across wide version gaps (P0, 2026-05-22)
    if _version_gap_too_large(current, latest):
        print(f"[updater] version gap too large: {current} -> {latest}, "
              f"refusing OTA")
        notify(
            f"This update spans too many versions (v{current} -> v{latest}).\\n\\n"
            f"OTA can only safely apply small step upgrades — the kernel "
            f"and USB gadget on your device may not be compatible with the "
            f"newer application code, and pushing it could brick the device.\\n\\n"
            f"To upgrade across this many versions, use firmware-flash mode:\\n"
            f"  1. Unplug USB\\n"
            f"  2. Hold the BOOT button on the device\\n"
            f"  3. Plug USB back in, release BOOT after 2 seconds\\n"
            f"  4. Run this app again — it'll flash the full firmware",
            "caution",
        )
        return 0

    # 3. Ask the user
    if not confirm_update(current, latest):
        print("[updater] user skipped")
        return 0

    # 4. Find bundle asset URL + mirror fallbacks
    name, gh_url = _find_bundle_asset(release)
    if not name:
        notify("Update available but the bundle asset is missing on GitHub. "
               "Try again later.", "caution")
        return 2

    mirror_urls = [base + name for base in _load_mirror_urls(args.script_dir)]
    gh_urls = [gh_url] if gh_url else []
    # Test mode: when AI_HUD_OTA_MIRROR_FIRST=1 (set via launchctl setenv on
    # macOS) we prepend a localhost mirror and try mirrors before GitHub.
    # Lets a developer reuse a cached bundle from a local `python3 -m http.server`
    # instead of redownloading 37 MB from GitHub's CDN on every iteration.
    # Production users never set the env var -- behaviour is unchanged.
    if os.environ.get("AI_HUD_OTA_MIRROR_FIRST"):
        test_url = "http://localhost:8000/" + name
        urls = [test_url] + mirror_urls + gh_urls
    else:
        urls = gh_urls + mirror_urls
    if not urls:
        notify("No download URLs configured.", "stop")
        return 2

    # 5. Download (with mirror fallback)
    progress_notify(f"Downloading update v{latest}...")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        bundle_path = tmp.name
    try:
        downloaded = False
        for url in urls:
            print(f"[updater] downloading {url}")
            try:
                _http_download(url, bundle_path, timeout=60)
                downloaded = True
                break
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"[updater]   failed: {e}")
        if not downloaded:
            notify("Download failed from all sources. "
                   "Check your internet connection.", "stop")
            return 2

        # 6. Push to device
        progress_notify("Installing files on device...")
        try:
            applied = perform_update(args.adb, bundle_path)
        except Exception as e:
            notify(f"Update failed mid-deploy: {e}\n\n"
                   f"The device may be in an inconsistent state. "
                   f"Try running the launcher again.", "stop")
            return 2

        # 7. Verify device actually rebooted with the new version (P3)
        # _wait_for_device is already called inside perform_update for the
        # reboot, but it returns as soon as adb sees the device -- before
        # hud_live.py + web server come up. Wait for the web server too
        # (P2), then re-read version.txt to confirm the upgrade landed.
        progress_notify("Waiting for device to finish booting...")
        web_ok = _wait_for_web_server(args.adb, timeout_s=30)
        new_ver = _device_version(args.adb)
        if _semver_tuple(new_ver) < _semver_tuple(latest):
            notify(
                f"Update attempted but device still reports v{new_ver} "
                f"(expected v{latest}).\n\n"
                f"Some files may not have applied correctly. Check "
                f"/var/log/ai_hud.log on device.",
                "caution",
            )
            return 2
        if not web_ok:
            notify(
                f"Update to v{applied} applied, but the device web "
                f"server hasn't come back yet.\n\n"
                f"Wait a minute and double-click this app again to "
                f"open the dashboard.",
                "caution",
            )
            return 0

        notify(f"Update to v{applied} complete.\n\n"
               f"The dashboard will reload in a moment.")
        print(f"[updater] update applied: v{applied} (verified)")
        return 0
    finally:
        try:
            os.unlink(bundle_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)

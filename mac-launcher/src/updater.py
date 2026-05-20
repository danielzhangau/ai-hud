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

    urls = [gh_url] if gh_url else []
    for base in _load_mirror_urls(args.script_dir):
        urls.append(base + name)
    if not urls:
        notify("No download URLs configured.", "stop")
        return 2

    # 5. Download (with mirror fallback)
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
        try:
            applied = perform_update(args.adb, bundle_path)
        except Exception as e:
            notify(f"Update failed mid-deploy: {e}\n\n"
                   f"The device may be in an inconsistent state. "
                   f"Try running the launcher again.", "stop")
            return 2

        notify(f"Update to v{applied} complete.\n\n"
               f"The dashboard will reload in a moment.")
        print(f"[updater] update applied: v{applied}")
        return 0
    finally:
        try:
            os.unlink(bundle_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)

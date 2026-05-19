"""Pack the device-deployable artifacts into a single update bundle zip.

Spec: docs/update-bundle.md.

Run from repo root. Inputs are assumed to already exist:
    src/*.py             -- Python source
    data/*.db            -- offline speed databases
    scripts/S99* S50* S01*  -- init scripts
    models/*.rknn        -- AI model (optional)
    build/ai-hud         -- compiled C binary (optional; warn if missing)

Output:
    dist/update-bundle-v<VERSION>.zip
    dist/update-bundle-v<VERSION>.sha256

Usage:
    python3 tools/build_update_bundle.py --version 0.1.0
    python3 tools/build_update_bundle.py --version 0.1.0 --skip-model

CI uses this from .github/workflows/release.yml after the firmware build
finishes; locally it's runnable too for testing OTA against a dev device.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile


MANIFEST_SCHEMA = 1

# Map of repo-relative source path -> device destination. Order is
# intentional: post-deploy hooks at the bottom run after every file
# has landed, so the on-device version.txt rewrites only happen if
# every previous push succeeded.
FILE_MAP = [
    # Python source (every .py from src/ except the touch UI which is
    # dead on devices with broken GT911 -- we still ship it for the day
    # a working module replaces the broken one).
    ("src/hud_live.py",       "/root/hud_live.py",         "0644"),
    ("src/web_config.py",     "/root/web_config.py",       "0644"),
    ("src/config_manager.py", "/root/config_manager.py",   "0644"),
    ("src/speed_db.py",       "/root/speed_db.py",         "0644"),
    ("src/sun.py",            "/root/sun.py",              "0644"),
    ("src/usb_netd.py",       "/root/usb_netd.py",         "0644"),
    ("src/touch_input.py",    "/root/touch_input.py",      "0644"),
    ("src/settings_ui.py",    "/root/settings_ui.py",      "0644"),
    ("src/gps_reader.py",     "/root/gps_reader.py",       "0644"),

    # Offline speed / camera databases. AU + CN; both regions ship so
    # the same firmware can be deployed in either market without rebuild.
    ("data/speed_zones.db",     "/root/data/speed_zones.db",     "0644"),
    ("data/speed_cameras.db",   "/root/data/speed_cameras.db",   "0644"),
    ("data/speed_zones_cn.db",  "/root/data/speed_zones_cn.db",  "0644"),
    ("data/speed_cameras_cn.db","/root/data/speed_cameras_cn.db","0644"),

    # init.d scripts -- need +x. The launcher reapplies mode after push
    # because adb sometimes drops it.
    ("scripts/S99_ai_hud",         "/etc/init.d/S99_ai_hud",         "0755"),
    ("scripts/S99usbnetd",         "/etc/init.d/S99usbnetd",         "0755"),
    ("scripts/S50usbdevice",       "/etc/init.d/S50usbdevice",       "0755"),
    ("scripts/S01_ai_hud_splash",  "/etc/init.d/S01_ai_hud_splash",  "0755"),
]

# Optional files: skipped silently if missing (warning emitted).
OPTIONAL_FILE_MAP = [
    ("build/ai-hud",                       "/root/ai-hud",                              "0755"),
    ("models/speed_signs_rv1106.rknn",     "/root/model/speed_signs_rv1106.rknn",       "0644"),
    ("mockups/splash_raw.bin",             "/root/splash_raw.bin",                      "0644"),
    # The virtual USB-drive image. /userdata is on a persistent partition
    # so a reboot keeps the previous one; we overwrite during OTA so the
    # customer's "USB drive" always contains the latest launcher. The
    # post_deploy reboot below also re-binds mass_storage to the fresh
    # file, otherwise the host would still see the stale contents.
    ("dist/launcher.img",                  "/userdata/launcher.img",                    "0644"),
]

POST_DEPLOY = [
    # Clear stale .pyc -- device has no RTC and stat-based bytecode
    # freshness checks can be wrong.
    "find /root -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true",
    "find /root -name '*.pyc' -exec rm {} \\; 2>/dev/null; true",
    # Reboot rather than service-restart. S99_ai_hud spawns watchdog
    # subshells with `&`, and adb-shell semantics SIGHUP everything in
    # the session when the shell exits -- even nohup'd children get
    # killed (verified 2026-05-19). A full reboot is also necessary
    # whenever S50usbdevice changes the USB gadget descriptors, which
    # only take effect at configfs bind time. The updater detects the
    # adb disconnect and waits for the device to come back online.
    #
    # Plain `reboot` -- the kernel takes over immediately, adb drops the
    # session, and the updater's _wait_for_device() then polls until the
    # device boots back up. Verified 2026-05-19: device returns within
    # ~25s with all services running.
    "reboot",
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                    help="Product version (e.g. '0.1.0', no v prefix)")
    ap.add_argument("--repo-root", default=os.getcwd(),
                    help="Repository root (default: cwd)")
    ap.add_argument("--out-dir", default="dist",
                    help="Output directory for the bundle zip")
    ap.add_argument("--skip-binary", action="store_true",
                    help="Skip the ai-hud binary even if present (for dev tests)")
    ap.add_argument("--skip-model", action="store_true",
                    help="Skip the RKNN model even if present (for dev tests)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo_root)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    bundle_name = f"update-bundle-v{args.version}"
    out_zip = os.path.join(out_dir, f"{bundle_name}.zip")

    workdir = tempfile.mkdtemp(prefix="bundle_")
    try:
        files = []

        # version.txt is generated inline.
        version_path = os.path.join(workdir, "version.txt")
        with open(version_path, "w") as f:
            f.write(args.version + "\n")
        files.append({
            "src": "version.txt", "dest": "/root/version.txt",
            "mode": "0644", "sha256": sha256_file(version_path),
        })

        all_entries = list(FILE_MAP)
        for src, dest, mode in OPTIONAL_FILE_MAP:
            if "ai-hud" in src and args.skip_binary:
                print(f"[skip] {src} (--skip-binary)")
                continue
            if ".rknn" in src and args.skip_model:
                print(f"[skip] {src} (--skip-model)")
                continue
            all_entries.append((src, dest, mode))

        for src_rel, dest, mode in all_entries:
            src_abs = os.path.join(repo, src_rel)
            if not os.path.isfile(src_abs):
                print(f"[warn] missing: {src_rel} (skipping)")
                continue
            # Stage into workdir mirroring the bundle layout, e.g.
            # python/hud_live.py.
            layout_rel = _bundle_layout_path(src_rel)
            staged = os.path.join(workdir, layout_rel)
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            shutil.copy2(src_abs, staged)
            files.append({
                "src": layout_rel.replace(os.sep, "/"),
                "dest": dest, "mode": mode,
                "sha256": sha256_file(staged),
            })

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": args.version,
            "built": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
            "post_deploy": POST_DEPLOY,
        }
        with open(os.path.join(workdir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, names in os.walk(workdir):
                for n in names:
                    abs_path = os.path.join(root, n)
                    arc = os.path.relpath(abs_path, workdir)
                    z.write(abs_path, arc)

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    sha = sha256_file(out_zip)
    sha_path = os.path.join(out_dir, f"{bundle_name}.sha256")
    with open(sha_path, "w") as f:
        f.write(f"{sha}  {bundle_name}.zip\n")

    size_mb = os.path.getsize(out_zip) / (1024 * 1024)
    print(f"\n  Bundle: {out_zip}")
    print(f"  Size:   {size_mb:.2f} MB")
    print(f"  SHA256: {sha}")
    print(f"  Files:  {len(files)}")


def _bundle_layout_path(src_rel):
    """Translate a repo-relative source path to its location in the bundle."""
    if src_rel.startswith("src/"):
        return os.path.join("python", os.path.basename(src_rel))
    if src_rel.startswith("data/"):
        return os.path.join("data", os.path.basename(src_rel))
    if src_rel.startswith("scripts/"):
        return os.path.join("scripts", os.path.basename(src_rel))
    if src_rel.startswith("build/"):
        return os.path.join("binaries", os.path.basename(src_rel))
    if src_rel.startswith("models/"):
        return os.path.join("models", os.path.basename(src_rel))
    if src_rel.startswith("mockups/"):
        return os.path.join("assets", os.path.basename(src_rel))
    if src_rel.startswith("dist/"):
        # Anything in dist/ is a build artifact; flatten into assets/.
        return os.path.join("assets", os.path.basename(src_rel))
    return os.path.basename(src_rel)


if __name__ == "__main__":
    sys.exit(main() or 0)

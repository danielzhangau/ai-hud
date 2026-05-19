# AI-HUD Update Bundle Format

The OTA update path for the device. A "bundle" is a single zip published as
a GitHub Release asset and pulled down by the macOS launcher when it
detects the device is on an older version.

Firmware (`boot.img` / `rootfs.img`) is **not** in the bundle -- it
requires MaskROM-mode flashing which the launcher can't do over USB
without physical BOOT-button press. Bundles handle the other 80% of
updates: Python code, the `ai-hud` binary, speed databases, init.d
scripts, and (when applicable) the RKNN model.

## Naming

```
update-bundle-v0.1.0.zip
```

One per release tag. The macOS launcher pulls this asset from the
release matching the latest tag.

## Layout

```
update-bundle-v0.1.0/
├── manifest.json
├── version.txt
├── python/
│   ├── hud_live.py
│   ├── web_config.py
│   ├── config_manager.py
│   ├── speed_db.py
│   ├── sun.py
│   ├── usb_netd.py
│   └── ... (every .py from src/)
├── binaries/
│   └── ai-hud
├── data/
│   ├── speed_zones.db
│   ├── speed_cameras.db
│   ├── speed_zones_cn.db
│   └── speed_cameras_cn.db
├── scripts/
│   ├── S99_ai_hud
│   ├── S99usbnetd
│   ├── S50usbdevice
│   └── S01_ai_hud_splash
└── models/
    └── speed_signs_rv1106.rknn
```

## manifest.json

The single source of truth for what gets pushed where. The launcher
reads this, verifies every file's sha256, then `adb push`es each entry
to its `dest`. Post-deploy actions run last to restart services and
write the new version.txt on the device.

```json
{
  "schema": 1,
  "version": "0.1.0",
  "built": "2026-05-19T10:00:00Z",
  "files": [
    {
      "src": "python/hud_live.py",
      "dest": "/root/hud_live.py",
      "mode": "0644",
      "sha256": "abc123..."
    },
    {
      "src": "binaries/ai-hud",
      "dest": "/root/ai-hud",
      "mode": "0755",
      "sha256": "def456..."
    },
    {
      "src": "scripts/S99_ai_hud",
      "dest": "/etc/init.d/S99_ai_hud",
      "mode": "0755",
      "sha256": "..."
    },
    {
      "src": "version.txt",
      "dest": "/root/version.txt",
      "mode": "0644",
      "sha256": "..."
    }
  ],
  "post_deploy": [
    "find /root -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true",
    "find /root -name '*.pyc' -exec rm {} \\; 2>/dev/null; true",
    "/etc/init.d/S99_ai_hud restart",
    "/etc/init.d/S99usbnetd restart"
  ]
}
```

Fields:

- `schema` -- format version of *this manifest*, not the product
  version. Bump if we ever rename or restructure fields. Loaders should
  refuse unknown schemas instead of silently misinterpreting.
- `version` -- the product release tag (`v` prefix stripped).
- `built` -- ISO-8601 UTC build time.
- `files[].src` -- path inside the zip.
- `files[].dest` -- absolute path on the device.
- `files[].mode` -- octal string, applied via `adb shell chmod` after
  push so executables don't lose their +x bit when copied through adb.
- `files[].sha256` -- hex digest. Launcher verifies after push by
  running `sha256sum` on the device and comparing.
- `post_deploy` -- shell commands run sequentially on the device after
  all files have been pushed and verified.

## version.txt

Single line containing the SemVer version of the bundle (no `v`
prefix), e.g.:

```
0.1.0
```

- Written to `/root/version.txt` as part of the bundle deploy.
- Read by hud_live.py at startup and surfaced in the dashboard
  alongside the firmware version.
- Used by the launcher to decide whether an update is needed
  (compare against the latest tag from GitHub Releases).

## Mirror fallback

The launcher downloads in this priority order:

1. **GitHub Release asset** -- primary path; signed URL via GitHub API.
2. **Gitee mirror** (if configured) -- `mac-launcher/mirrors.conf`
   contains an optional `gitee_base_url` that prepends to
   `update-bundle-vX.Y.Z.zip`.

The launcher shows a clear error dialog if both fail, so the user
knows whether the problem is network or update-bundle availability.

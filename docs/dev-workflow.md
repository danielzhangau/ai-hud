# Developer Workflow

How code goes from "I have an idea" to "the customer has it installed".

## The big picture

```
local edit  →  commit  →  push to main  →  tag vX.Y.Z  →  GitHub Actions
                                                              │
                                                              ▼
                                                      GitHub Release
                                                              │
                                                              ▼
                                              customer's launcher (auto)
```

Day-to-day work doesn't need any of the release machinery -- you push
to `main`, the next time you cut a tag, the changes ship.

## Branch model

Trunk-based on `main`. No long-lived feature branches in this repo.
CI on `main` is what guards quality (sdk-build runs, app-build runs).
Tags are how we cut releases.

## Conventional commits

We use Conventional Commit prefixes so `git-cliff` can group the
changelog automatically:

| Prefix | Goes into changelog as |
|---|---|
| `feat:` | 🚀 Features |
| `fix:` | 🐛 Bug Fixes |
| `perf:` | ⚡ Performance |
| `refactor:` | ♻️ Refactor |
| `docs:` | 📚 Documentation |
| `test:` | 🧪 Tests |
| `build:` | 📦 Build System |
| `ci:` | 🤖 CI / CD |
| `chore:` | 🧹 Chores |

Scope tags are encouraged: `feat(usb): ...`, `fix(detection): ...`.
The cliff config is in `cliff.toml` if you want to add a new group.

## Daily loop (no release)

```bash
# Edit some Python on host
$EDITOR src/hud_live.py

# Push to a device for live testing -- no build step for Python
adb push src/hud_live.py /root/hud_live.py
adb shell 'pkill -f hud_live.py; sleep 1; \
           start-stop-daemon -S -b -m -p /var/run/ai_hud_py.pid \
             -x /usr/bin/python3 -- -u /root/hud_live.py /dev/ttyS4 9600'

# Watch the result
adb shell 'tail -f /var/log/ai_hud.log'

# Commit when happy
git add src/hud_live.py
git commit -m "fix(hud): correct speed-limit fallback when GPS is invalid"
git push origin main
```

For C code changes (in `src/*.c`), rebuild with the lightweight
toolchain first (see `README.md → Building`), then `adb push build/ai-hud`.

## Provisioning a fresh device

`tools/provision.sh` pushes everything in one step:

```bash
# Make sure mac-launcher and the .img exist
( cd mac-launcher && ./build.sh )
( cd windows-launcher && ./build.sh )      # optional, recommended
bash tools/build_launcher_disk.sh

# Now push it all
VERSION=0.1.0 bash tools/provision.sh
```

Does, in order:

1. Build / reuse `dist/launcher.img` (the virtual USB drive)
2. `adb push` the image to `/userdata/launcher.img`
3. `adb push` all Python modules, init scripts, databases, model, and
   `ai-hud` binary
4. Write `/root/version.txt`
5. Clear `.pyc` caches
6. Reboot the device
7. Verify everything came back up

After this, the device behaves exactly like one a customer would
receive: HUD on power-on, USB drive on plug-in, OTA-aware.

## Cutting a release

A release is just a SemVer git tag pushed to `main`:

```bash
git tag v0.2.0
git push origin v0.2.0
```

That triggers `.github/workflows/release.yml`:

1. **`sdk-build.yml`** (reusable, called from release.yml) -- builds
   the Luckfox firmware in CI. Patches `CONFIG_USB_CONFIGFS_NCM=y`
   into the kernel defconfig along the way. ~1h44m.
2. **Build the OTA bundle** -- `tools/build_update_bundle.py` zips
   all device-side artifacts with a `manifest.json` + sha256 per file.
3. **Generate the changelog** -- `git-cliff --latest --strip header`
   produces release notes from commits since the previous tag.
4. **Publish the release** -- `softprops/action-gh-release@v2` creates
   the GitHub Release with everything attached:
   - `update.img`, `boot.img`, `rootfs.img`, ... (firmware partitions)
   - `update-bundle-vX.Y.Z.zip` + `.sha256` (OTA payload)
   - `speed_zones*.db`, `speed_cameras*.db` (offline databases)
   - `SHA256SUMS` (single file with all hashes)

After CI is green, customer launchers worldwide start offering the
update at next double-click.

### Pre-release vs production

The release workflow auto-marks releases as **prerelease** when:
- Tag starts with `v0.` (still in alpha territory), or
- Tag contains a hyphen (e.g. `v1.0.0-rc1`)

When you ship `v1.0.0` without a suffix, it lands as a real release.

## SDK reflashing (rare)

Only needed when the kernel itself or rootfs layout changes (new
USB function, init script wired into rootfs, etc.).

Currently bundles + provisioning don't include `update.img` -- a
firmware change requires:

1. Bump tag, push it, wait for CI -- get the new `update.img` from
   the release.
2. Customer (or developer) puts device in MaskROM, runs
   `flash_firmware.py` (via launcher) to flash.
3. Customer (or developer) double-clicks launcher again to bring
   business code current via OTA.

See `docs/firmware-update.md` for the customer-facing flow.

## Monthly DB refresh

`.github/workflows/db-refresh.yml` runs on cron @ 03:00 UTC the 1st
of every month. It pulls the latest OSM speed-limit + camera data for
AU and CN, rebuilds the `.db` files, computes a diff, and opens a
PR titled `data: monthly OSM refresh`.

Review the diff -- a 30%+ size delta in either region usually means
OSM had vandalism or partial outage that month. If the numbers look
sane, merge.

The PR pushes the new `.db` files to `main`; they'll ship with the
next tagged release.

## Local launcher iteration

Both launchers are just shell + scripts (no native compile), so the
edit-test loop is fast:

```bash
# macOS
$EDITOR mac-launcher/src/launch.sh
( cd mac-launcher && ./build.sh )
open mac-launcher/dist/AI-HUD\ Config.app

# Windows -- need a Win box (or VM) to test
$EDITOR windows-launcher/src/launcher.ps1
( cd windows-launcher && ./build.sh )
# Copy windows-launcher/dist/AI-HUD\ Config\ \(Windows\).zip to the
# Windows machine, extract, double-click .bat
```

Both `build.sh` scripts ad-hoc sign / package and emit a single zip
ready to ship.

## CI structure

| Workflow | Trigger | Purpose |
|---|---|---|
| `app-build.yml` | push to main | Smoke-test the C build |
| `sdk-build.yml` | manual / reused | Full Luckfox firmware build |
| `release.yml` | `v*.*.*` tag push | Cut a release |
| `db-refresh.yml` | cron 1st of month | Refresh OSM databases |
| `model-convert.yml` | manual | Convert trained ONNX → RKNN |

All shared `actions/*` and `softprops/*` are pinned to commit SHAs
(see comments next to each `uses:`). Dependabot rotates them weekly.

## Things to remember

- **Don't commit secrets / API keys.** GitHub scanning will catch
  most patterns but check.
- **Don't push `dist/` or `mac-launcher/dist/` artifacts** -- they're
  in `.gitignore`. The release workflow rebuilds them from source.
- **Don't manually rewrite `data/*.db`.** Let the monthly PR pick up
  upstream OSM changes; one-off edits get clobbered.
- **Don't bump version.txt by hand on the device.** OTA is the source
  of truth.
- **The device has no RTC.** `find -newer` and `.pyc` cache
  invalidation can lie. The provision script and OTA both clear
  `__pycache__` to work around this.

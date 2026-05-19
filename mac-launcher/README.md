# AI-HUD Config Launcher (macOS)

A zero-install macOS `.app` that opens the on-device configuration UI when
the AI-HUD device is plugged in over USB.

This is the **Phase 1** fallback: it works on every existing AI-HUD device
without re-flashing firmware. Once the device runs a CDC-NCM-enabled
kernel (Phase 2), users will be able to browse to `http://ai-hud.local/`
directly with no launcher at all.

## What it does

```
Double-click "AI-HUD Config.app"
        │
        ├── checks USB device with bundled adb
        ├── runs adb forward tcp:8080 tcp:8080
        ├── probes http://localhost:8080/api/state
        └── opens default browser at http://localhost:8080
```

End-user experience: one double-click, no terminal, no Homebrew, no
Android Platform Tools install.

## Build

```bash
./mac-launcher/build.sh
```

Output: `mac-launcher/dist/AI-HUD Config.app` (~6 MB).

Requires `curl`, `unzip`, and `plutil` -- all built into macOS.

## Distribute

```bash
cd mac-launcher/dist
zip -r 'AI-HUD Config.app.zip' 'AI-HUD Config.app'
```

Send the zip. Recipient:

1. Unzip.
2. **Right-click `AI-HUD Config.app` → Open** (first launch only).
   This is the standard macOS Gatekeeper bypass for ad-hoc-signed apps.
3. Subsequent launches: double-click as normal.

## Layout

```
mac-launcher/
├── build.sh                 # one-shot build script
├── src/
│   ├── launch.sh            # entry-point shell script
│   └── Info.plist           # bundle metadata (background app, no Dock icon)
└── dist/                    # build output (gitignored)
    └── AI-HUD Config.app/
```

## Why a shell-only launcher?

Considered and rejected:

- **PyInstaller / py2app** -- pulls in 50+ MB of Python runtime to do what
  five lines of shell achieve.
- **Tauri / Electron** -- 100+ MB binary for a launcher that exits in 2 s.
- **Native Swift app** -- requires Xcode, signing identity, and a CI matrix
  none of which the project wants to maintain right now.

The shell launcher is 6 MB, signs ad-hoc on any Mac, and has zero runtime
dependencies. When Phase 2 lands the launcher becomes obsolete -- so
investing in heavier tooling now would be wasted.

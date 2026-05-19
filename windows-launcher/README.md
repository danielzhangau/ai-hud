# AI-HUD Config Launcher (Windows)

PowerShell-based counterpart to `mac-launcher/`. Same product behaviour:
double-click, get a browser window with the device dashboard.

## What it covers vs. mac

| Capability | macOS | Windows |
|---|---|---|
| Browse + change settings | yes | yes |
| OTA bundle updates | yes | yes |
| Firmware (kernel/rootfs) flash | yes | not yet -- do it on a Mac |

Firmware flashing requires the Rockchip USB driver on Windows; that
breaks the "double-click only" promise so we send the user to a Mac
for that rare case (it's typically a quarterly task).

## Build

```bash
./windows-launcher/build.sh
```

Output: `windows-launcher/dist/AI-HUD Config (Windows).zip` (~10 MB).

Builds on macOS / Linux -- the launcher itself is `.ps1` + `.bat` + a
bundled Windows `adb.exe`, no Windows tooling needed to assemble.

## End-user flow

1. Extract zip
2. Plug AI-HUD device in via USB
3. Double-click `Run AI-HUD Config.bat`
4. If Windows SmartScreen warns ("Windows protected your PC"):
   click *More info* → *Run anyway* once. macOS Gatekeeper equivalent.
5. Browser opens the dashboard.

## Layout

```
windows-launcher/
├── build.sh                # mac/linux builder; downloads adb.exe, zips up
├── src/
│   ├── Run AI-HUD Config.bat   # entry, kicks off PowerShell
│   ├── launcher.ps1            # main: detect USB state, route
│   ├── updater.ps1             # OTA pull from GitHub releases
│   └── mirrors.conf            # optional Gitee / OSS mirror URLs
└── dist/                       # gitignored; build output
```

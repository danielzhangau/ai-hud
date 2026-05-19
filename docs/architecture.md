# AI-HUD Architecture Overview

End-to-end map of every moving part: from the customer's car, to the
Mac/Windows configurator, to the GitHub CI that builds firmware.

## Three independent layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 1: DEVICE (Luckfox Pico Ultra RV1106G3)                       │
│  Always-on. Runs HUD without any external help.                      │
│                                                                      │
│   ┌─────────────────────── ai-hud (C) ────────────────────────────┐ │
│   │ SC3336 → ISP → VI ──┬── CHN0 2304x1296 → VPSS → /dev/fb0 PiP │ │
│   │                     └── CHN1  640x640  → RKNN NPU inference  │ │
│   └──────────────────────────────┬─────────────────────────────────┘ │
│                                  │ /tmp/ai_hud_detect (IPC)          │
│   ┌──────────────── hud_live.py (Python) ───────────────────────┐  │
│   │ GPS NMEA → GPSState → SpeedDB lookup (AU/CN OSM)            │  │
│   │            └── sun.py auto day/night via GPS UTC + lat/lon  │  │
│   │ SpeedFusion (DB ∪ NPU, vote-debounced)                      │  │
│   │ Framebuffer renderer → /dev/fb0                              │  │
│   │ web_config.WebConfigServer  →  TCP 0.0.0.0:80               │  │
│   └──────────────────────────────────────────────────────────────┘ │
│                                                                      │
│   USB gadget composite (configfs):                                   │
│     ├── ffs.adb          (developer access + OTA channel)            │
│     ├── mass_storage.0   (virtual USB drive → /userdata/launcher.img)│
│     └── ncm.gs0 *        (USB Ethernet, future -- requires firmware  │
│                           with CONFIG_USB_CONFIGFS_NCM=y)            │
└──────────────────────────────────────────────────────────────────────┘
                                  ↑
                                  │ USB-C cable
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 2: HOST (customer's Mac or Windows PC)                        │
│  Only present when configuring -- car-running HUD doesn't need this. │
│                                                                      │
│   On plug-in, the OS sees three things at once:                      │
│     1. ADB device   (VID 0x2207, PID 0x0019)                         │
│     2. AIHUD volume (mass_storage backing /userdata/launcher.img)    │
│     3. USB Ethernet (future, after NCM firmware)                     │
│                                                                      │
│   ┌─── AI-HUD Config.app (macOS) ────────────────────────────────┐ │
│   │ launch.sh                                                    │ │
│   │   │  ioreg → detect adb / maskrom / loader / none            │ │
│   │   ├── adb path  → adb forward + updater.py + open browser    │ │
│   │   └── maskrom path → flash_firmware.py                       │ │
│   │ updater.py  →  GitHub Releases API → SHA-verified bundle     │ │
│   │ flash_firmware.py → osascript admin → upgrade_tool UF        │ │
│   └──────────────────────────────────────────────────────────────┘ │
│                                                                      │
│   ┌─── AI-HUD Config (Windows) ──────────────────────────────────┐ │
│   │ Run AI-HUD Config.bat → launcher.ps1                         │ │
│   │   Get-PnpDevice → same routing                               │ │
│   │ updater.ps1: PowerShell port of updater.py                   │ │
│   │ (firmware-flash path: tells user to use a Mac for now)       │ │
│   └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
                                  ↑
                                  │ HTTPS
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 3: CI / RELEASE (GitHub Actions)                              │
│  Triggered by developer pushing tags / on schedule.                  │
│                                                                      │
│   .github/workflows/                                                 │
│     ├── sdk-build.yml   reusable: Luckfox SDK → firmware images      │
│     ├── release.yml     triggered by  v*.*.*  tag                    │
│     │                     ├── calls sdk-build                        │
│     │                     ├── stages firmware + databases            │
│     │                     ├── runs tools/build_update_bundle.py      │
│     │                     ├── git-cliff changelog from commits       │
│     │                     └── softprops/action-gh-release → publish  │
│     ├── db-refresh.yml  monthly cron: rebuild OSM dbs, open PR       │
│     └── app-build.yml   develop-time: lightweight C build            │
│                                                                      │
│   Every release ships:                                               │
│     update.img / boot.img / rootfs.img      ← firmware               │
│     speed_zones*.db / speed_cameras*.db     ← offline databases      │
│     update-bundle-vX.Y.Z.zip                ← OTA payload            │
│     SHA256SUMS                              ← integrity              │
└──────────────────────────────────────────────────────────────────────┘
```

## Crossing the layer boundaries

### Device ↔ Host (USB)

| Channel | Backed by | Purpose |
|---|---|---|
| **ADB** | `ffs.adb` USB gadget function | Push files (OTA), shell access, port forward |
| **Mass Storage** | `mass_storage.0` + `/userdata/launcher.img` | Self-distributes the launcher zip |
| **USB Ethernet** *(future)* | `ncm.gs0` (CDC NCM) | Browser-direct `http://ai-hud.local/` |
| **Dashboard** | `web_config.py` on TCP :80, reached via adb forward | Read-only status + mirror toggle |

The launcher uses **only ADB** for everything today; the Mass Storage
function is there so the customer doesn't have to download the launcher
through any other channel. NCM unlocks browser-direct access, but the
launcher path keeps working unchanged whether or not NCM is present.

### Host ↔ CI (HTTPS)

| Channel | Used by | Purpose |
|---|---|---|
| **GitHub Releases API** | updater.py / updater.ps1 | Latest version probe + bundle download |
| **mirrors.conf** *(optional)* | both updaters | Gitee / OSS fallback for users on slow GitHub |

### Device ↔ CI (none)

The device never talks to GitHub directly. It has no Wi-Fi (Pico Ultra
non-W) and no internet path; updates always flow through a Host PC.

## Update flows

### Day-to-day code/DB updates (OTA bundle)

```
developer: git tag v0.2.0; git push origin v0.2.0
           │
           ▼
CI: sdk-build → firmware ─┐
    build_update_bundle.py ┤
    git-cliff changelog    ├──► GitHub Release v0.2.0
    action-gh-release  ────┘     (firmware + bundle + .db + SHA256SUMS)

customer (Mac): double-click .app
                │ adb shell cat /root/version.txt → 0.1.0
                │ GitHub /releases/latest → 0.2.0
                │ dialog "Update? (about 30 s)"
                │ download update-bundle-v0.2.0.zip
                │ adb push each file + chmod
                │ adb shell reboot
                │ wait_for_device
                │ open browser
                ▼
device: reboot, new code running, /root/version.txt = 0.2.0
```

### Firmware updates (rare, mac only)

```
customer: unplug USB, hold BOOT, replug, release BOOT
          │  (device now in MaskROM, PID 0x110b)
          ▼
double-click AI-HUD Config.app
          │  ioreg sees PID 0x110b → flash_firmware.py
          │  dialog "Flash firmware? (5-10 min, do not unplug)"
          │  osascript "do shell script... with administrator privileges"
          │  upgrade_tool UF update.img
          ▼
device: full reflash, auto-reboot into new kernel/rootfs
          │
          ▼
customer: double-click again
          │  detects 0.0.0 vs 0.2.0 → OTA bundle push (above)
          ▼
device fully on v0.2.0
```

## Why this shape

A few decisions worth recording so they're not silently re-litigated:

- **C + Python split**: The C side owns hard real-time (camera ISP,
  NPU inference, framebuffer DMA). Python owns parsing and policy
  (GPS, fusion, day/night). File-based IPC on tmpfs is intentionally
  primitive: zero protocol versioning headaches at the cost of a
  ~1 ms write/poll latency, which is fine for a 1 Hz GPS cycle.
- **Stateful device, stateless host**: All product state lives on the
  device (config, DBs, version). The launcher only exists to push
  updates and proxy a browser; if you throw away the launcher
  install and grab a fresh zip, nothing is lost.
- **Two USB functions, not network**: We could have run mDNS-only
  with no USB Mass Storage; we could have asked customers to
  download the launcher externally. Self-distributing it via Mass
  Storage removes one whole class of "where did I put the zip"
  support tickets.
- **No-launcher fallback path exists**: Once NCM firmware lands,
  `http://ai-hud.local/` works in any browser without the launcher.
  The launcher remains useful (OTA / firmware flash) but the day-1
  dependency disappears.

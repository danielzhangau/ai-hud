# AI-HUD -- Real-time Speed Sign Detection HUD

A standalone heads-up display that detects speed limit signs via on-device NPU inference, fuses with GPS-based offline speed/camera databases, and renders a real-time HUD on a 480x480 display. Designed for AU/CN dual-region driving with BOM under $50.

## Hardware

| Component | Spec |
|-----------|------|
| SoC | Luckfox Pico Ultra -- RV1106G3, Cortex-A7 @ 1.2 GHz, 256 MB DDR3, 0.5 TOPS NPU |
| Camera | SC3336 3MP MIPI CSI |
| Display | 480x480 DPI, `/dev/fb0` XRGB8888 (32 bpp, stride 1920) |
| Touch | GT911 capacitive, I2C (`/dev/i2c-3`, addr 0x5D) |
| GPS | EBYTE E108-GN03D (AT6558R), BDS + GPS + GLONASS, `/dev/ttyS4` 9600 bps |
| OS | Buildroot Linux (uclibc) |

## Architecture

```
                          Luckfox Pico Ultra (RV1106G3)
 ┌──────────────────────────────────────────────────────────────────┐
 │                                                                  │
 │  ┌───────────────────── ai-hud (C) ──────────────────────────┐  │
 │  │                                                            │  │
 │  │  SC3336 -> ISP -> VI ──┬── CHN0 2304x1296 -> VPSS 480x480 │  │
 │  │                        │       (mainpath)     -> PiP/fb0   │  │
 │  │                        │                                   │  │
 │  │                        └── CHN1 640x640 -> NPU inference   │  │
 │  │                             (selfpath)     -> IPC file     │  │
 │  └────────────────────────────────┬───────────────────────────┘  │
 │                                   │                              │
 │                          /tmp/ai_hud_detect (C -> Python)        │
 │                          /tmp/ai_hud_speed  (Python -> C)        │
 │                          /tmp/ai_hud_npu_enable (Python -> C)    │
 │                                   │                              │
 │  ┌───────────── hud_live.py (Python) ────────────────────────┐  │
 │  │                                                            │  │
 │  │  GPS NMEA -> speed/position ──┬── HUD render -> /dev/fb0  │  │
 │  │                               ├── speed_db lookup          │  │
 │  │                               ├── NPU result fusion        │  │
 │  │                               ├── RegionManager (AU/CN)    │  │
 │  │                               └── sun.py: day/night auto   │  │
 │  │                                                            │  │
 │  │  web_config.py -> TCP 0.0.0.0:80 (Dashboard + Setup)       │  │
 │  └────────────────────────────────────────────────────────────┘  │
 │                                                                  │
 │  USB gadget composite:                                           │
 │    ├── ADB function       (developer + OTA channel)              │
 │    ├── Mass Storage       (/userdata/launcher.img → virtual USB) │
 │    └── CDC NCM (future)   (USB Ethernet → http://ai-hud.local/)  │
 └──────────────────────────────────────────────────────────────────┘
```

**Dual-process design:** The C binary owns the camera/NPU pipeline; Python handles GPS parsing, HUD rendering, fusion, and the dashboard web server. They communicate via atomic file-based IPC on tmpfs.

**Adaptive inference:** Python writes GPS speed to `/tmp/ai_hud_speed`; the C inference thread adjusts its rate (5 s at idle, 200 ms at highway speed) to balance power and responsiveness.

**Speed limit fusion:** The offline GPS database is the primary source. When NPU also detects a sign, the lower of the two values is used (conservative safety).

## Project Structure

```
ai-hud/
├── src/                          # Device-side code (C + Python)
│   ├── camera_display.c          #   VI -> VPSS -> VO/PiP, main C entry
│   ├── rknn_detect.c/.h          #   RKNN NPU inference + adaptive rate
│   ├── postprocess.c/.h          #   YOLOv5 INT8 decode + NMS
│   ├── isp_control.c/.h          #   RKAIQ ISP auto-exposure / night tuning
│   ├── frame_capture.c/.h        #   On-device frame capture for training
│   ├── hud_ipc.h                 #   Bidirectional file-based IPC
│   ├── hud_live.py               #   HUD renderer + GPS + fusion
│   ├── speed_db.py               #   Offline OSM speed limit + camera lookup
│   ├── sun.py                    #   NOAA day/night calc from GPS UTC
│   ├── config_manager.py         #   Persistent INI config + thread lock
│   ├── web_config.py             #   Dashboard web server (port 80)
│   ├── usb_netd.py               #   DHCP + mDNS for USB Ethernet path
│   ├── settings_ui.py            #   Touch overlay (legacy, GT911 broken)
│   ├── touch_input.py            #   GT911 I2C driver (legacy)
│   └── gps_reader.py             #   NMEA sentence parser
├── scripts/                      # Device init.d
│   ├── S01_ai_hud_splash         #   Splash before HUD comes up
│   ├── S50usbdevice              #   USB gadget composite config
│   ├── S99_ai_hud                #   Main watchdog (ai-hud + hud_live.py)
│   └── S99usbnetd                #   DHCP + mDNS daemon launcher
├── data/                         # Offline databases (regenerated monthly via CI)
│   └── speed_zones*.db / speed_cameras*.db    # AU + CN
├── tools/                        # Build / provision helpers
│   ├── prepare_speed_db.py       #   Rebuild dbs from OSM (used by CI cron)
│   ├── build_update_bundle.py    #   Package OTA bundles
│   ├── build_launcher_disk.sh    #   Build the 64 MB virtual-USB FAT32 image
│   ├── provision.sh              #   One-shot factory deploy
│   └── speed_db_config.yaml      #   Per-region OSM source config
├── mac-launcher/                 # Customer-side macOS launcher (.app)
│   └── src/{launch.sh, updater.py, flash_firmware.py, ...}
├── windows-launcher/             # Customer-side Windows launcher (.bat + .ps1)
│   └── src/{launcher.ps1, updater.ps1, ...}
├── docs/                         # Project documentation
│   ├── architecture.md           #   Three-layer overview (device/host/CI)
│   ├── customer-journey.md       #   End-user perspective
│   ├── dev-workflow.md           #   build / tag / release loop
│   ├── firmware-update.md        #   MaskROM flash detailed procedure
│   ├── update-bundle.md          #   OTA bundle format spec
│   └── hardware-reference.md     #   Luckfox Pico Ultra raw notes
├── training/                     # ML training pipeline (offline)
├── models/                       # RKNN conversion tooling
├── enclosure/                    # 3D-printable case (SCAD + STL)
├── cmake/                        # Cross-compile toolchain config
├── docker/                       # Luckfox SDK build environment
├── mockups/                      # HUD design mockups
├── .github/workflows/            # CI pipelines (see "CI/CD Workflows" below)
├── cliff.toml                    # git-cliff changelog template
├── CMakeLists.txt                # C build system
└── AI-Powered-HUD-Project-Plan.md # Living roadmap / Phase status
```

## AI Model

| Item | Value |
|------|-------|
| Architecture | YOLOv5n (airockchip fork) |
| Input | 640 x 640 RGB |
| Quantization | INT8 (RKNN) |
| Classes | 11 -- speed signs 20/30/40/50/60/70/80/90/100/110/120 km/h |
| Dataset | MTSD (Mapillary Traffic Sign Detection), 3,905 images |
| Training | Google Colab T4 or local Mac MPS |
| Conversion | ONNX -> RKNN via rknn-toolkit2 (x86 Linux only) |

Speed cameras are detected solely via the GPS database (not vision), providing more reliable coverage.

Region filtering is handled at the Python layer: `hud_live.py` maintains `valid_speeds` per region and ignores detections outside the local speed set.

## Building

### Prerequisites

- CMake >= 3.10
- [Rockchip cross-compilation toolchain](https://github.com/deerpi/arm-rockchip830-linux-uclibcgnueabihf)
- [RKMPI headers and libraries](https://github.com/LuckfoxTECH/luckfox_pico_rkmpi_example)

### Mode A: Lightweight Build (recommended)

Uses the standalone toolchain + RKMPI example repo. This is what CI uses.

```bash
mkdir -p build && cd build
cmake .. \
  -DCMAKE_TOOLCHAIN_FILE=../cmake/rv1106-toolchain.cmake \
  -DTOOLCHAIN_DIR=/path/to/arm-rockchip830-linux-uclibcgnueabihf \
  -DRKMPI_DIR=/path/to/luckfox_pico_rkmpi_example
make -j$(nproc)
```

### Mode B: Full SDK Build

Uses the complete Luckfox SDK sysroot (from `sdk-build` workflow or local SDK clone).

```bash
export LUCKFOX_SDK_PATH=/path/to/luckfox-pico
cmake .. \
  -DCMAKE_TOOLCHAIN_FILE=../cmake/rv1106-toolchain.cmake \
  -DTOOLCHAIN_DIR=$LUCKFOX_SDK_PATH/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf \
  -DRKMPI_DIR=/path/to/luckfox_pico_rkmpi_example
make -j$(nproc)
```

### Build Variants

| Flag | Description |
|------|-------------|
| (default) | 11-class universal speed sign model |
| `-DCOCO_TEST=ON` | 80-class COCO model for NPU pipeline testing |

## Deploying

### Push to Device

```bash
# C binary (from CI artifact or local build)
adb push build/ai-hud /root/ai-hud

# Python files
adb push src/hud_live.py /root/hud_live.py
adb push src/settings_ui.py /root/settings_ui.py
adb push src/touch_input.py /root/touch_input.py
adb push src/config_manager.py /root/config_manager.py
adb push src/web_config.py /root/web_config.py
adb push src/speed_db.py /root/speed_db.py
adb push src/gps_reader.py /root/gps_reader.py

# Diagnostic tools (optional, for field debugging)
adb push tools/measure_false_positives.py /root/measure_false_positives.py

# RKNN model
adb push models/speed_signs_rv1106.rknn /root/model/speed_signs_rv1106.rknn

# Speed databases
adb push data/speed_zones.db /root/data/speed_zones.db
adb push data/speed_cameras.db /root/data/speed_cameras.db

# Init scripts
adb push scripts/S01_ai_hud_splash /etc/init.d/S01_ai_hud_splash
adb push scripts/S99_ai_hud /etc/init.d/S99_ai_hud

# Splash screen
adb push mockups/splash_raw.bin /root/splash_raw.bin
```

### Restart Services

```bash
# Clear stale bytecode (device has no RTC)
adb shell 'find /root -name "*.pyc" -delete'
adb shell 'find /root -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null'

# Restart
adb shell '/etc/init.d/S99_ai_hud restart'
```

### Verify

```bash
adb shell 'ps | grep -E "ai-hud|hud_live"'
adb shell 'cat /tmp/ai_hud_detect'       # NPU detection results
adb shell 'cat /var/log/ai_hud.log'       # Python HUD log
adb shell 'cat /var/log/ai_hud_c.log'     # C binary log
```

## Customer-side launcher

End users don't run `adb` themselves -- they double-click a tiny app
that does it for them. The device exposes itself as a USB drive named
**AIHUD** containing the launcher for both macOS and Windows, so the
customer never has to download anything.

```
Plug device into PC
   │
   ▼
Finder / Explorer auto-mounts "AIHUD" (~67 MB FAT32)
   │
   ▼
Drag AI-HUD Config.app (mac) or unzip For Windows/... (win)
   │
   ▼
Double-click → browser opens → AI-HUD Dashboard
   │
   ▼ (if outdated)
   Dialog: "Update to vX.Y.Z? (~30 s)" → click Update → done
```

What the launcher actually does on each launch:

1. Detects USB state via ioreg (mac) / Get-PnpDevice (Windows) -- routes
   between ADB / MaskROM / no-device branches.
2. Runs `adb forward 8080 → 80` so the device's web server is reachable
   at `http://localhost:8080`.
3. Probes the device, compares `/root/version.txt` to GitHub's latest
   release tag, prompts the user if there's an update.
4. If accepted, downloads `update-bundle-vX.Y.Z.zip`, SHA-verifies every
   file in its manifest, pushes to the device, triggers a reboot.
5. Opens the default browser at the dashboard URL.

See `docs/customer-journey.md` for the end-user-facing walkthrough,
and `docs/architecture.md` for the full three-layer diagram.

### Building / shipping the launcher

```bash
# macOS launcher (~20 MB .app)
( cd mac-launcher && ./build.sh )

# Windows launcher (~4 MB .zip)
( cd windows-launcher && ./build.sh )

# Pack both into the AIHUD virtual USB drive image
bash tools/build_launcher_disk.sh

# One-shot factory provision: push code + DB + launcher.img + version
VERSION=0.1.0 bash tools/provision.sh
```

### Developer fallback (no launcher)

For local debugging without going through the customer flow:

```bash
adb forward tcp:8080 tcp:80
open http://localhost:8080
```

The dashboard server itself binds `0.0.0.0:80` on the device so it's
reachable through any path that gets you to port 80 -- adb forward,
the launcher, or (once NCM firmware lands) the USB Ethernet link
straight to `http://ai-hud.local/`.

## CI/CD Workflows

Six GitHub Actions workflows, each with a distinct purpose. All third-party
actions are SHA-pinned and tracked by Dependabot.

| Workflow | File | Trigger | Purpose |
|----------|------|---------|---------|
| **App Build** | `app-build.yml` | Push to `src/**`, `CMakeLists.txt`, `cmake/**` | Cross-compile C binary for RV1106 |
| **Build HUD** | `build.yml` | Push to `main` | Generate Python HUD mockup images |
| **Model Convert** | `model-convert.yml` | Manual dispatch | Convert ONNX model to RKNN INT8 |
| **SDK Build** | `sdk-build.yml` | Manual dispatch / called by release | Full Luckfox SDK firmware build (~1h44m) |
| **Release** | `release.yml` | `v*.*.*` tag push | Build firmware, package OTA bundle, generate changelog via git-cliff, publish GitHub Release |
| **DB Refresh** | `db-refresh.yml` | Cron 03:00 UTC on 1st of month | Rebuild AU/CN speed databases from OSM, open auto-PR |

Detailed lifecycle:

- **A release** (`git tag v0.2.0 && git push origin v0.2.0`) chains the
  full pipeline: SDK build → firmware + bundle → changelog → GitHub
  Release with `update.img`, `update-bundle-v0.2.0.zip`, `*.db`, and
  `SHA256SUMS`. From there it's automatically discoverable by the
  customer launcher.
- **A database refresh** opens a PR for human review (OSM data is
  occasionally vandalised; a 30%+ region size delta usually means
  something went wrong upstream). The PR's body contains pre/post
  zone-count diffs.

See `docs/dev-workflow.md` for the day-to-day developer loop and the
release procedure.

### App Build (Primary CI)

The main development workflow. Automatically triggers when C source code changes. Produces three stripped ARM binaries as artifacts:

- `ai-hud-rv1106` -- Production build (11-class universal model)
- `ai-hud-rv1106-cn` -- CN region variant
- `ai-hud-rv1106-coco-test` -- 80-class COCO test build

Toolchain: `deerpi/arm-rockchip830-linux-uclibcgnueabihf` + `LuckfoxTECH/luckfox_pico_rkmpi_example`. Typical build time: ~18 seconds.

### Build HUD (Mockups)

Generates HUD mockup images for design review. Runs `generate_hud_mockups_v3.py` and `generate_splash.py`, uploads PNG artifacts.

### Model Convert (Manual)

Converts a YOLOv5 ONNX model to RKNN INT8 format using `rknn-toolkit2`. Supports `yolov5n` and `yolov5s_relu` architectures, targeting `rv1106` or `rv1103` platforms. Uses 20 COCO calibration images for INT8 quantization.

### SDK Build (Manual)

Builds the complete Luckfox SDK firmware image from source. Takes ~2 hours. Produces a full firmware image for flashing to device EMMC.

## Boot Sequence

```
Power on
  -> U-Boot (no display output -- no DPI driver)
  -> Kernel DRM init (renders built-in Luckfox logo to /dev/fb0)
  -> Backlight turns on at ~t=3.4s
  -> S01_ai_hud_splash (kill backlight -> write splash -> restore backlight)
  -> ... other init.d scripts ...
  -> S99_ai_hud start:
       1. Kill rkipc / display-blocking processes
       2. Clear stale .pyc bytecode (no RTC)
       3. Show splash (refresh)
       4. Launch watchdog for ai-hud (C binary)
       5. Wait 2s for camera pipeline init
       6. Launch watchdog for hud_live.py (Python HUD)
```

Both processes run under watchdog loops with automatic respawn (3-second delay after crash). Logs rotate at 1 MB.

## IPC Protocol

All IPC uses atomic file writes on tmpfs (`/tmp/`). Write protocol: write to `.tmp` file, then `rename()` for atomicity.

| File | Direction | Content |
|------|-----------|---------|
| `/tmp/ai_hud_detect` | C -> Python | `speed_limit=60\nconfidence=0.85\ntimestamp=...` |
| `/tmp/ai_hud_speed` | Python -> C | GPS speed in km/h (float) |
| `/tmp/ai_hud_npu_enable` | Python -> C | `1` (enabled) or `0` (disabled) |
| `/tmp/ai_hud_display_mode` | Python -> C | `cam` (camera) or `hud` (default) |
| `/tmp/ai_hud_pip_hide` | Python -> C | Existence = hide PiP overlay |
| `/tmp/ai_hud_gps` | Internal | GPS fix status |
| `/tmp/ai_hud_ready` | C -> Python | Existence = camera pipeline ready |

## Training

### Google Colab (recommended)

Open `training/train_colab.ipynb` in Google Colab with a T4 GPU runtime. The notebook handles:

1. Dataset extraction and validation
2. YOLOv5n training (airockchip fork for RKNN compatibility)
3. Export to ONNX
4. RKNN INT8 conversion with calibration

### Local Training

```bash
cd training
./train_local.sh
```

Supports Mac MPS (Apple Silicon) and NVIDIA CUDA. RKNN conversion still requires x86 Linux (use Colab or the `model-convert` workflow).

## GPS Wiring

E108-GN03D SH1.0 6-pin connector to Luckfox Pico Ultra front-left header:

| Pin | Wire | Signal | Board Pin |
|-----|------|--------|-----------|
| 1 | White | ON_OFF | NC (internal pull-up) |
| 2 | Blue | 1PPS | NC (not used) |
| 3 | Green | GND | GND (5th from top) |
| 4 | Yellow | TXD | UART4_RX GPIO1_B0 [40] (4th from top) |
| 5 | Black | RXD | UART4_TX GPIO1_B1 [41] (6th from top) |
| 6 | Red | VCC | 3V3 (1st from top) |

## License

Private repository. All rights reserved.

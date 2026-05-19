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
 │  │                               └── RegionManager (AU/CN)    │  │
 │  │                                                            │  │
 │  │  GT911 touch -> settings_ui.py -> config overlay           │  │
 │  └────────────────────────────────────────────────────────────┘  │
 └──────────────────────────────────────────────────────────────────┘
```

**Dual-process design:** The C binary owns the camera/NPU pipeline; Python handles GPS parsing, HUD rendering, and touch UI. They communicate via atomic file-based IPC on tmpfs.

**Adaptive inference:** Python writes GPS speed to `/tmp/ai_hud_speed`; the C inference thread adjusts its rate (5 s at idle, 200 ms at highway speed) to balance power and responsiveness.

**Speed limit fusion:** The offline GPS database is the primary source. When NPU also detects a sign, the lower of the two values is used (conservative safety).

## Project Structure

```
ai-hud/
├── src/
│   ├── camera_display.c      # VI -> VPSS -> VO/PiP, main entry
│   ├── rknn_detect.c/.h      # RKNN NPU inference thread + adaptive rate
│   ├── postprocess.c/.h      # YOLOv5 INT8 decode + NMS
│   ├── frame_capture.c/.h    # On-device frame capture for model iteration
│   ├── hud_ipc.h             # Bidirectional file-based IPC (C <-> Python)
│   ├── hud_live.py           # HUD renderer, GPS, RegionManager, bitmap font
│   ├── speed_db.py           # Offline speed limit + camera spatial lookup
│   ├── config_manager.py     # Persistent JSON config with atomic writes
│   ├── web_config.py         # PC-side config UI over USB (adb forward 8080)
│   ├── settings_ui.py        # Touch settings overlay (dark theme)
│   ├── touch_input.py        # GT911 I2C userspace driver with stall recovery
│   ├── gps_reader.py         # NMEA sentence parser
│   ├── generate_splash.py    # Splash screen raw binary generator
│   └── generate_hud_mockups*.py  # Mockup image generators
├── scripts/
│   ├── S01_ai_hud_splash     # Earliest init: hide kernel logo, show splash
│   ├── S99_ai_hud            # Main init: watchdog loops for both processes
│   └── web_config.sh         # One-shot: adb forward + open browser to config UI
├── data/
│   ├── speed_zones.db        # AU: 171K zones from OSM (2.7 MB)
│   ├── speed_zones_cn.db     # CN: 20K zones from OSM (322 KB)
│   ├── speed_cameras.db      # AU: 1,363 cameras (21 KB)
│   └── speed_cameras_cn.db   # CN: 53 cameras (0.8 KB)
├── tools/
│   ├── prepare_speed_db.py   # Build speed databases from OSM data
│   ├── speed_db_config.yaml  # Region config for DB builder
│   ├── render_hud_mockup.py  # Render HUD mockups from state
│   └── convert_captures.py   # Convert on-device frame captures
├── training/
│   ├── train_colab.ipynb     # Training + RKNN conversion (Colab T4)
│   ├── train_local.sh        # Local training (Mac MPS / CUDA)
│   └── download_wheels.py    # Offline rknn-toolkit2 wheel downloader
├── models/
│   └── convert_to_rknn.py    # Standalone ONNX -> RKNN converter
├── cmake/
│   └── rv1106-toolchain.cmake  # Cross-compilation toolchain file
├── docker/
│   └── Dockerfile            # Luckfox SDK build environment
├── mockups/                  # Generated HUD mockup images
├── .github/workflows/        # CI/CD pipelines (see below)
└── CMakeLists.txt            # Build system (Mode A / Mode B)
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

## Configuration UI (PC over USB)

When the GT911 touchscreen is unavailable (e.g. hardware failure), all
settings are still reachable through a browser on the development PC. The
HUD process embeds a small HTTP server on `127.0.0.1:8080`, which is exposed
to the PC via `adb forward`.

One-shot launch (recommended):

```bash
./scripts/web_config.sh
```

The script runs `adb forward tcp:8080 tcp:8080`, probes the device, and opens
`http://localhost:8080` in the default browser.

Manual equivalent:

```bash
adb forward tcp:8080 tcp:8080
open http://localhost:8080      # macOS
xdg-open http://localhost:8080  # Linux
```

Exposes all `settings` and `fusion` parameters from `ai_hud.conf` with the
same callbacks the touch UI uses, so changes are applied live (NPU on/off,
display mode, mirror, night mode, region, fusion thresholds) and persisted
atomically. The server is bound to `127.0.0.1` only and is reachable solely
through the USB-mediated `adb forward` tunnel.

## CI/CD Workflows

Four GitHub Actions workflows, each with a distinct purpose:

| Workflow | File | Trigger | Purpose |
|----------|------|---------|---------|
| **App Build** | `app-build.yml` | Push to `src/**`, `CMakeLists.txt`, `cmake/**` | Cross-compile C binary for RV1106 |
| **Build HUD** | `build.yml` | Push to `main` | Generate Python HUD mockup images |
| **Model Convert** | `model-convert.yml` | Manual dispatch | Convert ONNX model to RKNN INT8 |
| **SDK Build** | `sdk-build.yml` | Manual dispatch | Full Luckfox SDK firmware build |

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

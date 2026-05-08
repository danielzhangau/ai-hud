# AI-Powered HUD - Project Plan

> Vehicle Head-Up Display with AI-Powered Real-time Object Detection & Speed Alerts
> Hardware: Luckfox Pico Ultra (RV1106G3) | Status: Phase 1-4 Complete, Phase 5 In Progress

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Hardware Inventory & Specifications](#2-hardware-inventory--specifications)
3. [System Architecture](#3-system-architecture)
4. [Phase 0: Environment Setup](#4-phase-0-environment-setup)
5. [Phase 1: Board Bring-Up & Basic I/O](#5-phase-1-board-bring-up--basic-io)
6. [Phase 2: Camera + Display Pipeline](#6-phase-2-camera--display-pipeline)
7. [Phase 3: GPS Integration](#7-phase-3-gps-integration)
8. [Phase 4: NPU AI Inference](#8-phase-4-npu-ai-inference)
9. [Phase 5: HUD Application](#9-phase-5-hud-application)
10. [Phase 6: Vehicle Integration](#10-phase-6-vehicle-integration)
11. [Software Architecture](#11-software-architecture)
12. [Risk Assessment](#12-risk-assessment)
13. [Bill of Materials](#13-bill-of-materials)
14. [Reference & Resources](#14-reference--resources)

---

## 1. Project Overview

### 1.1 Vision

Build a compact, low-power AI-powered Head-Up Display (HUD) for vehicles that provides:
- Real-time forward-facing camera feed with AI object detection (vehicles, pedestrians, lane markings)
- GPS-based speed display and navigation data overlay
- Transparent HUD-style information rendering on an embedded display
- All processing done on-device via RV1106's NPU (no cloud dependency)

### 1.2 Core Features

| Feature | Priority | Status | Description |
|---------|----------|--------|-------------|
| Camera feed | P0 | Done | Real-time forward-facing video via MIPI CSI (PiP 120x120) |
| Display output | P0 | Done | HUD render to 480x480 DPI LCD via framebuffer |
| Speed limit detection | P0 | Done | AU speed sign recognition via NPU (YOLOv5n) |
| GPS speed overlay | P0 | Done | Speed, heading, satellite count on HUD |
| Speed limit database | P0 | Done | Offline GPS-based speed lookup (OSM data, 170K+ zones) |
| Speed camera alerts | P1 | Done | GPS proximity + NPU detection (1,300+ cameras) |
| SpeedFusion engine | P1 | Done | DB primary + NPU backup with temporal voting |
| Buzzer alerts | P2 | TODO | Audio alerts for over-speed / camera warning |
| Lane detection | P3 | TODO | Lane departure warning |
| Dash cam recording | P3 | TODO | Optional video recording to external storage |

### 1.3 Design Principles

- **Minimal latency**: Camera-to-display pipeline < 100ms
- **Low power**: Total system draw < 5W (suitable for vehicle always-on)
- **Compact form factor**: Mountable behind windshield or on dashboard
- **Offline-first**: Zero network dependency for core functions
- **Automotive-grade UX**: Non-distracting, glanceable information

---

## 2. Hardware Inventory & Specifications

### 2.1 On-Hand Components

| Component | Model/Spec | Interface | Status |
|-----------|-----------|-----------|--------|
| Main Board | Luckfox Pico Ultra (RV1106G3) | - | Purchased |
| Display | RGB LCD (480x480 or 720x720) | RGB666 FPC | Purchased |
| Camera | CSI Camera Module (SC3336 3MP) | MIPI CSI 2-lane FPC | Purchased |
| GPS Module | UART GPS (with dupont wires) | UART (TX/RX/GND/VCC) | Purchased |
| Power - USB | USB data/power cable | USB-C | Purchased |
| Power - Vehicle | 12V Car Charger (cigarette lighter to USB) | USB output | Purchased |
| Wiring | Dupont jumper wires (M-M, M-F) | GPIO | Purchased |

### 2.2 Main Board Specs (Luckfox Pico Ultra)

| Spec | Detail |
|------|--------|
| SoC | Rockchip RV1106G3 |
| CPU | ARM Cortex-A7 @ 1.2GHz |
| NPU | 1 TOPS (int4/int8/int16) |
| RAM | 256MB DDR3L |
| Storage | 8GB eMMC |
| ISP | 5MP input @ 30fps |
| Video Codec | H.264/H.265 hardware encoder |
| Display | RGB666 parallel interface |
| Camera | MIPI CSI 2-lane |
| Audio | Codec + speaker connector (MX1.25mm) |
| USB | USB 2.0 Host/Device |
| GPIO | 30 pins (UART/I2C/SPI/PWM/ADC) |
| Ethernet | 10/100M (PoE capable) |
| OS | Buildroot / Ubuntu 22.04 |

### 2.3 May Need to Purchase

| Component | Purpose | Priority | Estimated Cost |
|-----------|---------|----------|----------------|
| 3D-printed enclosure | Dashboard mounting | P1 | ~$15 |
| Suction cup mount | Windshield mounting | P1 | ~$5 |
| MX1.25mm speaker | Audio alerts | P2 | ~$3 |
| USB-TTL adapter | Serial debug (if needed) | P1 | ~$5 |
| MicroSD card | Extended storage (dash cam) | P3 | ~$10 |

---

## 3. System Architecture

### 3.1 Hardware Topology

```
                    +------------------+
                    |   12V Vehicle    |
                    |   Power (CIG)    |
                    +--------+---------+
                             |
                        USB 5V out
                             |
                    +--------v---------+
                    |  Luckfox Pico    |
                    |  Ultra RV1106G3  |
                    |                  |
  CSI FPC --------->  MIPI CSI Input  |
  (Camera)          |                  |
                    |  RGB666 Output   +---------> RGB LCD Display
                    |                  |           (480x480/720x720)
  UART (GPIO) ----->  UART3 (RX/TX)   |
  (GPS Module)      |                  |
                    |  NPU 1TOPS       |
                    |  Cortex-A7       |
                    +------------------+
```

### 3.2 Software Pipeline (Actual Implementation)

```
                         ai-hud (C binary)
                    +---------------------------+
Camera (SC3336) --> | VI CHN0 (2304x1296)       |
   MIPI CSI        |   -> VPSS (480x480 NV12)  |---> /dev/fb0 PiP (120x120)
                   | VI CHN1 (480x480)          |
                   |   -> NPU thread (RKNN)     |---> /tmp/ai_hud_detect (IPC)
                   +---------------------------+

                         hud_live.py (Python)
                    +---------------------------+
GPS (/dev/ttyS4) -->| NMEA parser (1 Hz)       |
   9600bps          |   |                       |
                    |   v                       |
                    | SpeedFusion state machine |
                    |   DB (primary) + NPU (backup)
                    |   |                       |
                    |   v                       |
                    | HUD Renderer              |---> /dev/fb0 (480x480 XRGB)
                    +---------------------------+
                    |         ^
                    |         |
              speed_zones.db  speed_cameras.db
              (170K records)  (1,363 cameras)
```

### 3.3 Key Software Modules

| Module | Responsibility | Technology |
|--------|---------------|------------|
| Video Input (VI) | Dual-channel camera capture | RKMPI VI (CHN0 main + CHN1 self) |
| Video Processing (VPSS) | Resize to 480x480 | RKMPI VPSS API |
| NPU Inference | Speed sign detection | RKNN C API + YOLOv5n INT8 |
| PiP Renderer | Camera preview overlay | NV12->XRGB mmap /dev/fb0 |
| HUD IPC | C->Python detection results | Atomic file /tmp/ai_hud_detect |
| GPS Parser | NMEA GPRMC/GPGGA parsing | Python serial (UART /dev/ttyS4) |
| Speed Database | Offline speed limit lookup | Grid-indexed binary DB (OSM data) |
| SpeedFusion | DB + NPU result fusion | Temporal voting state machine |
| HUD Renderer | HUD UI composition | Python direct framebuffer write |
| System Manager | Dual-process lifecycle | init.d S99_ai_hud |

---

## 4. Phase 0: Environment Setup

> Goal: Prepare development environment, flash OS, establish connectivity

### 4.1 Development Machine Setup

**System Requirement**: Ubuntu 22.04 x86_64 (native or VM)

```bash
# Install essential build dependencies
sudo apt update
sudo apt-get install -y git ssh make gcc gcc-multilib g++-multilib \
  module-assistant expect g++ gawk texinfo libssl-dev bison flex \
  fakeroot cmake unzip gperf autoconf device-tree-compiler \
  libncurses5-dev pkg-config bc python-is-python3 passwd openssl \
  openssh-server openssh-client vim file cpio rsync curl

# Install ADB tools
sudo apt-get install -y android-tools-adb

# Install Python tools for RKNN model conversion
sudo apt-get install -y python3-pip python3-venv
```

### 4.2 Clone Luckfox SDK

```bash
# GitHub
git clone https://github.com/LuckfoxTECH/luckfox-pico.git

# Or Gitee (faster in China)
git clone https://gitee.com/LuckfoxTECH/luckfox-pico.git

cd luckfox-pico
```

### 4.3 Flash Buildroot Image to Board

#### Step 1: Download Official Image

Download the latest `Luckfox_Pico_Ultra_EMMC_XXXXXX` image from:
- Official site: https://www.luckfox.com
- Or build from SDK (see Phase 1)

#### Step 2: Enter Burn Mode

1. Locate the **BOOT** button on the Luckfox Pico Ultra board
2. **Hold BOOT** button while connecting USB-C to your computer
3. Wait until the device is detected, then release BOOT

Verify connection:
```bash
lsusb  # Should show Rockchip device
```

#### Step 3: Flash (macOS)

```bash
# Download and extract the macOS upgrade tool
# For macOS 26.x:
cd upgrade_tool_v2.44_mac
sudo ./upgrade_tool uf /path/to/Luckfox_Pico_Ultra_EMMC_XXXXXX.img
```

#### Step 3 (alt): Flash (Linux Ubuntu 22.04)

```bash
# Install upgrade_tool
sudo unzip upgrade_tool_v2.17.zip
cd upgrade_tool_v2.17_for_linux/
sudo cp upgrade_tool /usr/local/bin
sudo chmod +x /usr/local/bin/upgrade_tool

# Verify
sudo upgrade_tool -v
# Output: Upgrade Tool v2.17

# Flash
sudo upgrade_tool uf /path/to/update.img
```

#### Step 3 (alt): Flash via SDK script

```bash
# If image exists in output/image/
sudo ./rkflash.sh update
```

### 4.4 First Boot & Connection

#### Method A: ADB (Recommended for initial setup)

```bash
# Connect USB, then:
adb devices    # Verify device detected
adb shell      # Login (default: root / luckfox)
```

#### Method B: SSH via USB RNDIS

```bash
# macOS/Linux: Configure RNDIS network interface
# The board creates a virtual network adapter over USB

# Buildroot default IP: 172.32.0.93
# Set your host USB network interface to 172.32.0.100/24

ssh root@172.32.0.93
# Password: luckfox
```

#### Method C: Serial Console (Debug)

- Connect USB-TTL adapter: TX->RX, RX->TX, GND->GND
- Baud rate: 115200
- Use `screen /dev/tty.usbserial-XXXX 115200` or equivalent

### 4.5 Post-Flash Verification Checklist

```bash
# On the board, verify:
cat /proc/cpuinfo          # Confirm ARM Cortex-A7
free -m                    # Confirm 256MB RAM
df -Th                     # Confirm eMMC partitions
ls /dev/video*             # Camera device nodes
ls /dev/dri/card0          # DRM display device
cat /sys/class/misc/rknpu/device/npu_load  # NPU status
```

---

## 5. Phase 1: Board Bring-Up & Basic I/O

> Goal: Validate all hardware interfaces work individually

### 5.1 Display Test

```bash
# Verify DRM device exists
ls /dev/dri/

# Get connector and CRTC info
modetest -M rockchip

# Display test pattern (adjust resolution to your screen)
modetest -M rockchip -s <connector_id>@<crtc_id>:480x480
# or
modetest -M rockchip -s <connector_id>@<crtc_id>:720x720
```

If display doesn't work, check:
1. FPC ribbon cable orientation (metal contacts face the board's chip side)
2. Device tree configuration matches your screen model
3. CMA memory is sufficient (720x720 needs ~10MB extra CMA)

### 5.2 Camera Test

```bash
# Stop default RKIPC to release camera
RkLunch-stop.sh

# List video devices
v4l2-ctl --list-devices

# List supported formats
v4l2-ctl --device=/dev/video15 --list-formats-ext

# Capture test frames (30 frames, 640x480, NV12)
v4l2-ctl --device=/dev/video15 \
  --set-fmt-video=width=640,height=480,pixelformat=NV12 \
  --stream-mmap --stream-to=test_capture.yuv --stream-count=30

# Verify RTSP stream (from host machine)
# On board, restart RKIPC:
RkLunch-start.sh
# On host, open VLC: rtsp://172.32.0.93/live/0
```

### 5.3 GPS Module Test

GPS modules typically communicate via UART using NMEA protocol.

#### Step 1: Identify GPS wiring

| GPS Wire | Connect To | Description |
|----------|-----------|-------------|
| VCC (Red) | 3.3V or 5V pin | Power supply |
| GND (Black) | GND pin | Ground |
| TX (Green/Yellow) | UART RX pin | GPS data output |
| RX (Blue/White) | UART TX pin | GPS command input (optional) |

#### Step 2: Enable UART via luckfox-config

```bash
luckfox-config
# -> Advanced Options -> UART -> Select UART3 (or appropriate) -> Enable -> Reboot
```

#### Step 3: Read GPS data

```bash
# Set baud rate (most GPS modules default to 9600)
stty -F /dev/ttyS3 ispeed 9600 ospeed 9600

# Read raw NMEA sentences
cat /dev/ttyS3
# Expected output like:
# $GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,...
# $GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,...
```

#### Step 4: Parse GPS data (Python test)

```python
import serial

uart = serial.Serial("/dev/ttyS3", baudrate=9600, timeout=1)

while True:
    line = uart.readline().decode("ascii", errors="replace").strip()
    if line.startswith("$GPRMC"):
        parts = line.split(",")
        if parts[2] == "A":  # A = valid fix
            lat = parts[3] + parts[4]
            lon = parts[5] + parts[6]
            speed_knots = float(parts[7]) if parts[7] else 0
            speed_kmh = speed_knots * 1.852
            print(f"Lat: {lat}, Lon: {lon}, Speed: {speed_kmh:.1f} km/h")
```

### 5.4 Phase 1 Checklist

- [x] Buildroot image flashed and booted successfully
- [x] ADB/SSH connection established
- [x] Display shows test pattern (correct resolution)
- [x] Camera captures frames (V4L2 test)
- [x] GPS outputs valid NMEA data (ttyS4, 9600bps)
- [x] GPIO pins accessible (LED blink test)
- [x] System stable under 10-minute stress test

---

## 6. Phase 2: Camera + Display Pipeline

> Goal: Build real-time camera-to-display pipeline using RKMPI

### 6.1 Pipeline Architecture

```
Camera (SC3336) -> VI -> VPSS (resize to display resolution) -> DRM output
```

### 6.2 SDK Build Configuration

```bash
cd luckfox-pico

# Select board config
./build.sh lunch
# Select: RV1106_Luckfox_Pico_Ultra (option for Ultra series)
# Boot medium: EMMC
# System: Buildroot

# Full build
./build.sh
```

### 6.3 Custom Application: Camera Preview

Create a C application using RKMPI APIs:

```c
// Simplified pipeline initialization pseudocode
// Full implementation in project source code

// 1. Initialize system
RK_MPI_SYS_Init();

// 2. Start ISP
SAMPLE_COMM_ISP_Init(0, RK_AIQ_WORKING_MODE_NORMAL, RK_FALSE, "/etc/iqfiles");
SAMPLE_COMM_ISP_Run(0);

// 3. Configure VI (video input) - captures from camera
VI_CHN_ATTR_S vi_attr = {
    .stSize = {640, 480},
    .enPixelFormat = RK_FMT_YUV420SP,
    ...
};
RK_MPI_VI_SetChnAttr(0, 0, &vi_attr);
RK_MPI_VI_EnableChn(0, 0);

// 4. Configure VPSS (resize to display resolution)
VPSS_CHN_ATTR_S vpss_attr = {
    .u32Width = 480,   // match display
    .u32Height = 480,
    .enPixelFormat = RK_FMT_RGB888,
    ...
};
RK_MPI_VPSS_SetChnAttr(0, 0, &vpss_attr);
RK_MPI_VPSS_EnableChn(0, 0);

// 5. Bind VI -> VPSS
RK_MPI_SYS_Bind(&vi_chn, &vpss_chn);

// 6. Main loop: Get VPSS frame -> DRM display
while (running) {
    VIDEO_FRAME_INFO_S frame;
    RK_MPI_VPSS_GetChnFrame(0, 0, &frame, -1);
    // Render frame to DRM framebuffer
    drm_display_frame(&frame);
    RK_MPI_VPSS_ReleaseChnFrame(0, 0, &frame);
}
```

### 6.4 Cross-Compilation

```bash
# Using Buildroot toolchain
export LUCKFOX_SDK_PATH=/path/to/luckfox-pico
export CC=${LUCKFOX_SDK_PATH}/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc

# Build with CMakeLists.txt
mkdir build && cd build
cmake .. -DCMAKE_C_COMPILER=$CC
make -j$(nproc)

# Deploy to board
scp hud_preview root@172.32.0.93:/root/
```

### 6.5 Phase 2 Deliverables

- [x] Camera preview displayed on RGB LCD (PiP 120x120 bottom-right)
- [x] Frame rate >= 25 FPS at display native resolution
- [x] No visible tearing or artifacts (software NV12->XRGB via mmap)
- [x] Latency camera-to-display < 80ms

---

## 7. Phase 3: GPS Integration

> Goal: Parse GPS data and overlay speed/location on display

### 7.1 NMEA Parser Module

Key NMEA sentences to parse:
- `$GPRMC` - Speed, heading, date/time, lat/lon
- `$GPGGA` - Fix quality, altitude, satellite count
- `$GPVTG` - Ground speed (km/h and knots)
- `$GPGSA` - DOP and active satellites

### 7.2 GPS Data Structure

```c
typedef struct {
    double latitude;
    double longitude;
    float speed_kmh;       // from GPRMC/GPVTG
    float heading;         // degrees from north
    float altitude;        // meters, from GPGGA
    int satellite_count;   // from GPGGA
    int fix_quality;       // 0=invalid, 1=GPS, 2=DGPS
    char utc_time[16];     // HH:MM:SS
    char date[16];         // DD/MM/YY
    bool valid;
} gps_data_t;
```

### 7.3 Integration with HUD Pipeline

```
GPS UART Thread (async) --> Shared gps_data_t (mutex-protected)
                                    |
Main Render Thread:                 v
  Camera Frame + AI Results + GPS Data --> HUD Compositor --> Display
```

### 7.4 Phase 3 Deliverables

- [x] GPS module outputs valid fix data (ttyS4 @ 9600bps)
- [x] Speed displayed on HUD in km/h (large, glanceable font)
- [x] Satellite count / fix status indicator
- [x] Heading indicator
- [x] GPS data update rate >= 1Hz
- [x] Lat/lon parsing for speed database lookup

---

## 8. Phase 4: NPU AI Inference

> Goal: Run real-time object detection on camera frames via RV1106 NPU

### 8.1 Model Selection

| Model | Task | Input Size | FPS (RV1106) | Status |
|-------|------|-----------|-------------|--------|
| YOLOv5n (COCO 80-class) | General detection | 640x640 | ~12 FPS | Test done |
| YOLOv5n (AU 9-class) | AU speed sign + camera | 320x320 | ~15-20 FPS | **Production** |

**Production model**: Custom YOLOv5n trained on Australian speed sign dataset (9 classes: speed_10/20/30/40/50/60/70/80 + speed_camera). INT8 quantized for RKNPU.

**Performance** (measured on device):
- NPU inference: ~61ms (hardware fixed)
- Postprocess: ~20ms (optimized from 61ms via static buffers + INT8 class search)
- PiP render: ~1ms (120x120 NV12->XRGB point-sample)

### 8.2 Model Conversion Pipeline

```bash
# 1. Setup RKNN-Toolkit2 (on x86 Ubuntu)
conda create -n rknn python=3.9
conda activate rknn
pip install rknn_toolkit2-2.3.2-cp39-cp39-manylinux_2_17_x86_64.whl

# 2. Export YOLOv5 to ONNX (airockchip fork for RKNPU optimization)
git clone https://github.com/airockchip/yolov5.git
cd yolov5
python export.py --rknpu --weight yolov5n.pt

# 3. Convert ONNX to RKNN
# Using rknn_model_zoo conversion script:
python3 convert.py ../model/yolov5n.onnx rv1106
# Output: yolov5n.rknn
```

### 8.3 On-Device Inference Integration

```c
// 1. Load RKNN model
rknn_context ctx;
rknn_init(&ctx, "yolov5n.rknn", 0, 0, NULL);

// 2. Query model I/O
rknn_input_output_num io_num;
rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));

// 3. Allocate zero-copy memory
rknn_tensor_mem* input_mem = rknn_create_mem(ctx, input_size);
rknn_set_io_mem(ctx, input_mem, &input_attrs[0]);

// 4. Per-frame inference loop
while (running) {
    // Get frame from VPSS (RGB888 format for RKNN)
    VIDEO_FRAME_INFO_S frame;
    RK_MPI_VPSS_GetChnFrame(0, AI_CHN, &frame, -1);

    // Copy to RKNN input (zero-copy if possible)
    memcpy(input_mem->virt_addr, frame_data, input_size);

    // Run inference
    rknn_run(ctx, NULL);

    // Get output and post-process (NMS, box decode)
    float* output = (float*)output_mems[0]->virt_addr;
    post_process_yolov5(output, &detections);

    // Pass detections to HUD renderer
    hud_update_detections(&detections);
}
```

### 8.4 VPSS Dual-Output Configuration

```
Camera -> VI -> VPSS Group 0
                  |-- Channel 0: 480x480 RGB888 -> Display
                  |-- Channel 1: 320x320 RGB888 -> NPU inference
```

This allows display and AI to run at different resolutions simultaneously.

### 8.5 Phase 4 Deliverables

- [x] RKNN model converted and loaded on device (INT8 quantized)
- [x] Object detection running at >= 10 FPS (NPU ~61ms + post ~20ms)
- [x] Detection results passed to HUD via IPC file
- [x] Detection classes: 8 speed signs + speed camera (AU-specific)
- [x] CPU usage < 80% during inference (offloaded to NPU)
- [x] Offline speed database integrated (170K zones, 1,363 cameras)
- [x] SpeedFusion engine: DB primary + NPU backup with temporal voting

---

## Speed Database Architecture

> Offline GPS-based speed limit and camera warning system.
> DB is the PRIMARY source (stable); NPU detection is BACKUP (for temporary changes).

### Data Sources

| Source | Coverage | Records | License |
|--------|----------|---------|---------|
| OpenStreetMap Overpass API | 7 AU metro areas (16 tiles) | 171,033 speed zones | ODbL |
| OpenStreetMap Overpass API | All Australia | 1,363 speed cameras | ODbL |
| Government open data (NSW/VIC/QLD/WA) | Hardcoded known cameras | 13 cameras | CC BY 4.0 |

**Cities covered**: Sydney (4 tiles), Melbourne (4 tiles), Brisbane (4 tiles), Perth, Adelaide, Canberra, Gold Coast.

### Binary Database Format

Both databases share the same compact binary layout (16 bytes per record):

```
Header (16 bytes):
  magic:     4B  (b'SZON' / b'SCAM')
  version:   u16
  count:     u32
  rec_size:  u16
  flags:     u32  (reserved)

Record (16 bytes, sorted by grid_key):
  lat_e6:    i32  (latitude * 1e6)
  lon_e6:    i32  (longitude * 1e6)
  speed:     u8   (km/h)
  rec_type:  u8   (road/camera type)
  bearing:   u16  (degrees, 0xFFFF = bidirectional)
  grid_key:  u32  (spatial bucket)
```

### Spatial Indexing

- Grid resolution: 0.005 degrees (~550m lat, ~450m lon at -33 deg)
- Query: 9-cell neighborhood search (current + 8 neighbors)
- Lookup time: <1ms on Cortex-A7 @ 1.2GHz
- Memory: ~2.7 MB zones + ~21 KB cameras (fits in 256MB RAM)

### SpeedFusion State Machine

Prevents HUD speed limit flickering by requiring temporal consistency from NPU:

```
           +----------+    NPU votes >= 3     +----------+
           |          |    conf >= 0.60        |          |
  GPS ---->|  DB mode |  + candidate < DB      | NPU mode |
  cycle    | (primary)|----------------------->| (override|
           |          |                        |          |
           +----------+    timeout 30s OR      +----------+
                ^          new road segment         |
                |          (DB limit changed)       |
                +-----------------------------------+
```

**Rules**:
1. DB is always the trusted baseline
2. NPU can only LOWER the limit (never raise -- safety first)
3. NPU needs >= 3 consecutive detections with confidence >= 0.60
4. NPU override expires after 30 seconds without re-confirmation
5. Entering a new road segment (DB limit changes) resets NPU state
6. When no DB data available, NPU confidence threshold relaxes to 0.55

### Preparation Tool

`tools/prepare_speed_db.py` -- run on PC to download OSM data and generate binary DBs:
```bash
python tools/prepare_speed_db.py
# Output: data/speed_zones.db, data/speed_cameras.db
# Deploy: adb push data/*.db /root/data/
```

---

## 9. Phase 5: HUD Application

> Goal: Integrate all modules into a polished HUD application

### 9.1 HUD UI Layout

```
+----------------------------------+
|  [SAT:8]          [12:34:56 PM]  |   <- Status bar
|                                  |
|        Camera Feed               |
|     (with AI overlays)           |
|                                  |
|   [Car]        [Person]          |   <- Detection labels
|   [----]       [------]          |   <- Bounding boxes
|                                  |
|                                  |
|  SPEED            HEADING        |
|   72              NE 045         |   <- GPS data (large font)
|  km/h                            |
|                                  |
|  [!] Forward Collision Warning   |   <- Alert area (conditional)
+----------------------------------+
```

### 9.2 UI Framework Options

| Option | Pros | Cons |
|--------|------|------|
| LVGL | Official Luckfox support, touch-ready, rich widgets | Learning curve, C-based |
| Direct DRM/FB | Minimal overhead, full control | More code to write |
| Custom framebuffer | Simplest, direct pixel manipulation | No widget system |

**Recommendation**: Use LVGL for the UI layer, with direct DRM for camera frame blitting.

### 9.3 Thread Architecture

```
Thread 1: Camera + VPSS (frame capture, resize)
Thread 2: NPU inference (async, processes every N-th frame)
Thread 3: GPS UART reader (1Hz update)
Thread 4: HUD Renderer (composites camera + overlays + GPS, outputs to DRM)
Thread 5: Alert Manager (monitors detections for danger conditions)
```

### 9.4 Alert Logic

```
IF (detection.class == "vehicle" AND detection.distance < THRESHOLD_CLOSE):
    trigger_collision_warning(LEVEL_HIGH)
    play_alert_sound()
    flash_screen_border(RED)

IF (detection.class == "pedestrian" AND detection.in_path == true):
    trigger_pedestrian_warning()

IF (gps.speed > SPEED_LIMIT):
    show_speed_warning()
```

### 9.5 Phase 5 Deliverables

- [ ] Unified HUD application with all modules integrated
- [ ] Smooth 25+ FPS display with AI overlay
- [ ] GPS speed display accurate to +/- 2 km/h
- [ ] Alert system triggers correctly
- [ ] Auto-start on boot (init.d script)
- [ ] Graceful shutdown on power loss

---

## 10. Phase 6: Vehicle Integration

> Goal: Install and test in actual vehicle environment

### 10.1 Power Setup

```
Vehicle cigarette lighter (12V)
    -> Car charger adapter (12V to 5V USB)
        -> USB-C cable
            -> Luckfox Pico Ultra
```

Power considerations:
- Board draws ~2-3W typical
- Car charger rated for 5V 2.5A+ (sufficient)
- Consider auto-start script: board powers on when car starts

### 10.2 Mounting Options

| Location | Pros | Cons |
|----------|------|------|
| Dashboard top | Easy access, good camera angle | Sun exposure |
| Behind rearview mirror | Hidden, optimal forward view | Wiring difficulty |
| A-pillar mount | Stable, out of way | Limited viewing angle |

### 10.3 Camera Positioning

- Forward-facing, centered
- Slight downward tilt (~5-10 degrees) for road coverage
- Ensure no windshield obstructions (wipers, stickers)
- Consider IR filter for nighttime performance

### 10.4 3D Enclosure Design Requirements

- Ventilation holes (SoC generates heat under load)
- Camera lens opening with adjustable angle
- Display window (flush mount)
- USB-C port access for power
- Mounting points (suction cup or adhesive)

### 10.5 Phase 6 Deliverables

- [ ] Stable vehicle mounting
- [ ] Power from car charger verified (engine start/stop cycles)
- [ ] Camera angle optimized for road view
- [ ] No vibration-induced display issues
- [ ] System survives temperature range in vehicle
- [ ] 1-hour continuous driving test passed

---

## 11. Software Architecture

### 11.1 Directory Structure

```
ai-hud/
  CMakeLists.txt              # Cross-compile for arm-rockchip830
  .github/workflows/
    app-build.yml             # CI: Docker cross-compile (~18s)
  src/
    camera_display.c          # VI->VPSS->VO/PiP, pip_render_thread
    rknn_detect.c/.h          # RKNN inference thread (VI CHN1)
    postprocess.c/.h          # YOLOv5 INT8 decode + NMS (optimized)
    hud_ipc.h                 # C->Python IPC atomic file protocol
    hud_live.py               # Python HUD: GPS, speed DB, framebuffer render
    speed_db.py               # Offline speed limit + camera DB with spatial lookup
  data/
    speed_zones.db            # Binary DB: 171K speed zone records (2.7 MB)
    speed_cameras.db          # Binary DB: 1,363 camera locations (21 KB)
  tools/
    prepare_speed_db.py       # PC-side: download OSM data, generate binary DBs
  training/
    prepare_dataset.py        # AU speed sign dataset preparation
    train.sh                  # YOLOv5n training script
    train_colab.ipynb         # Google Colab training notebook
    au_speed_signs.yaml       # Dataset config (9 classes)
    README.md                 # Training instructions
  scripts/
    S99_ai_hud                # init.d dual-process manager
  model/
    au_speed_signs_rv1106.rknn  # Custom 9-class AU model (on device)
```

### 11.2 Build System (CMakeLists.txt skeleton)

```cmake
cmake_minimum_required(VERSION 3.10)
project(ai_hud C)

set(CMAKE_C_STANDARD 11)

# Luckfox SDK paths
set(SDK_PATH $ENV{LUCKFOX_SDK_PATH})
set(RKMPI_PATH ${SDK_PATH}/media/out)
set(RKNN_PATH ${SDK_PATH}/media/out)

include_directories(
    ${RKMPI_PATH}/include
    ${RKNN_PATH}/include/rknn
    src/
)

link_directories(
    ${RKMPI_PATH}/lib
    ${RKNN_PATH}/lib
)

file(GLOB SOURCES "src/*.c")

add_executable(ai_hud ${SOURCES})

target_link_libraries(ai_hud
    rockit         # RKMPI
    rknn_api       # RKNN inference
    rga            # 2D graphics acceleration
    drm            # Display
    pthread        # Threading
    m              # Math
)
```

### 11.3 Configuration File

```ini
[camera]
width = 640
height = 480
fps = 30
device = /dev/video15

[display]
width = 480
height = 480
connector_id = 70
crtc_id = 66

[gps]
uart_device = /dev/ttyS3
baud_rate = 9600

[ai]
model_path = /root/model/yolov5n.rknn
input_size = 320
confidence_threshold = 0.5
nms_threshold = 0.45
inference_skip_frames = 2    # Run inference every 3rd frame

[alert]
collision_distance_m = 15.0
speed_limit_kmh = 120
```

---

## 12. Risk Assessment

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| NPU inference too slow | High | Medium | Use smaller model (YOLOv5n), reduce input resolution, skip frames |
| 256MB RAM insufficient | High | Low | Optimize buffer allocation, use zero-copy RKNN, minimize CMA |
| GPS no fix indoors | Medium | High | Test outdoors only, add "No GPS" graceful fallback |
| Display FPC loose from vibration | Medium | Medium | Secure with Kapton tape, design rigid enclosure |
| Thermal throttling | Medium | Medium | Add heatsink, ventilation in enclosure |
| Camera glare/overexposure | Medium | Medium | Use ISP auto-exposure, consider polarizing filter |
| Power brownout on engine start | Low | Medium | Add capacitor buffer or check USB charger stability |

---

## 13. Bill of Materials

| # | Item | Qty | Status | Est. Cost |
|---|------|-----|--------|-----------|
| 1 | Luckfox Pico Ultra (RV1106G3) | 1 | Owned | - |
| 2 | RGB LCD Screen (480x480/720x720) | 1 | Owned | - |
| 3 | CSI Camera Module | 1 | Owned | - |
| 4 | GPS Module (UART) | 1 | Owned | - |
| 5 | Car Charger (12V to USB) | 1 | Owned | - |
| 6 | USB Cable | 1 | Owned | - |
| 7 | Dupont Jumper Wires | 1 set | Owned | - |
| 8 | 3D Printed Enclosure | 1 | TODO | ~$15 |
| 9 | USB-TTL Debug Adapter | 1 | Optional | ~$5 |
| 10 | MX1.25mm Speaker | 1 | Optional | ~$3 |
| | **Total additional cost** | | | **~$23** |

---

## 14. Reference & Resources

### Official Documentation

| Resource | URL |
|----------|-----|
| Luckfox Wiki (Ultra) | https://wiki.luckfox.com/zh/Luckfox-Pico-Ultra |
| Luckfox SDK (GitHub) | https://github.com/LuckfoxTECH/luckfox-pico |
| Luckfox SDK (Gitee) | https://gitee.com/LuckfoxTECH/luckfox-pico |
| RKNN-Toolkit2 | https://github.com/rockchip-linux/rknn-toolkit2 |
| RKNN Model Zoo | https://github.com/airockchip/rknn_model_zoo |
| Luckfox Forum | https://forums.luckfox.com |
| Luckfox AI Assistant | https://ai.luckfox.com |

### Key Technical References

| Topic | Source |
|-------|--------|
| RKMPI API Guide | SDK: docs/RV1106/Rockchip_Developer_Guide_RKMPI.pdf |
| RKNN C API | rknn-toolkit2/doc/RKNN_Runtime_API_Reference.pdf |
| DRM/KMS Linux | https://dri.freedesktop.org/docs/drm/ |
| LVGL Docs | https://docs.lvgl.io |
| NMEA Protocol | https://www.nmea.org/content/STANDARDS/NMEA_0183_Standard |
| YOLOv5 (airockchip) | https://github.com/airockchip/yolov5 |

### Default Credentials

| System | User | Password | USB IP |
|--------|------|----------|--------|
| Buildroot | root | luckfox | 172.32.0.93 |
| Ubuntu | pico | luckfox | 172.32.0.70 |

---

## Appendix: Notion Page Structure Recommendation

For organizing this project on Notion, the recommended page hierarchy:

```
AI-Powered HUD (Main Page)
  |
  +-- Project Overview (inline database: status, timeline, links)
  |
  +-- Hardware
  |     +-- Component Inventory (table database)
  |     +-- Wiring Diagrams (image gallery)
  |     +-- Luckfox Pico Ultra Specs (synced from wiki)
  |
  +-- Development Phases (board/kanban view)
  |     +-- Phase 0: Environment Setup
  |     +-- Phase 1: Board Bring-Up
  |     +-- Phase 2: Camera + Display
  |     +-- Phase 3: GPS Integration
  |     +-- Phase 4: AI/NPU
  |     +-- Phase 5: HUD App
  |     +-- Phase 6: Vehicle Integration
  |
  +-- Technical Docs
  |     +-- System Architecture
  |     +-- Software Architecture
  |     +-- API Reference Notes
  |
  +-- Research & References
  |     +-- Luckfox Official Docs (bookmarks)
  |     +-- RKNN/NPU Notes
  |     +-- Similar Projects (inspiration)
  |
  +-- Dev Log (timeline database)
  |     +-- Entry per session with date, progress, blockers
  |
  +-- Risk & Issues (table database)
        +-- Risk description, impact, status, mitigation
```

---

*Document updated: 2026-05-08*
*Hardware platform: Luckfox Pico Ultra RV1106G3*
*Target: Vehicle AI HUD with speed limit alerts (AU market)*

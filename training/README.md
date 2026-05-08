# Australian Speed Signs -- Model Training

Train a YOLOv5n model for 9-class Australian speed sign detection,
optimized for deployment on Luckfox Pico Ultra (RV1106G3, 1 TOPS NPU).

## Target Classes

| ID | Class | Description |
|----|-------|-------------|
| 0 | speed_sign_30 | 30 km/h speed limit |
| 1 | speed_sign_40 | 40 km/h speed limit |
| 2 | speed_sign_50 | 50 km/h speed limit |
| 3 | speed_sign_60 | 60 km/h speed limit |
| 4 | speed_sign_70 | 70 km/h speed limit |
| 5 | speed_sign_80 | 80 km/h speed limit |
| 6 | speed_sign_100 | 100 km/h speed limit |
| 7 | speed_sign_110 | 110 km/h speed limit |
| 8 | speed_camera | Speed enforcement camera |

## Data Sources

| Source | Classes Covered | Images | Notes |
|--------|----------------|--------|-------|
| [ELEC5308 Australia Traffic Sign](https://universe.roboflow.com/elec5308-w8jl5/australia-traffic-sign) | AU speed limits | ~4.2k | Primary source, Australian signs |
| [GTSDB](https://benchmark.ini.rub.de/gtsdb_dataset.html) | 30/50/60/70/80/100 | 900 | German signs (similar appearance) |
| [Roboflow Speed Signs](https://universe.roboflow.com/search?q=class:%22speed+limit%22) | Various speed limits | varies | Supplemental |
| Custom local images | All 9 classes | user-provided | Recommended for speed_camera |

## Quick Start

### Option A: Google Colab (Recommended -- free GPU)

```bash
./train.sh --colab
```

This prints a ready-to-paste Colab notebook. Copy the output into
Google Colab cells (Runtime > GPU > T4).

### Option B: Local Training (requires NVIDIA GPU)

```bash
# 1. Install dependencies
pip install roboflow opencv-python tqdm pyyaml torch torchvision

# 2. Get a free Roboflow API key
#    https://app.roboflow.com/settings

# 3. Prepare dataset
python prepare_dataset.py --roboflow-key YOUR_API_KEY

# 4. Train
./train.sh --epochs 100
```

## Step-by-Step Guide

### 1. Prepare Dataset

```bash
cd training

# Download and merge all data sources
python prepare_dataset.py --roboflow-key YOUR_API_KEY

# Skip GTSDB if you only want Australian data
python prepare_dataset.py --roboflow-key YOUR_KEY --skip-gtsdb

# Add your own labeled images
python prepare_dataset.py --roboflow-key YOUR_KEY --local-dir /path/to/my/data
```

Output structure:
```
au_speed_dataset/
  data.yaml
  train/
    images/
    labels/
  valid/
    images/
    labels/
  test/
    images/
    labels/
```

### 2. Train Model

```bash
# Standard training (YOLOv5n, 320x320, 100 epochs)
./train.sh

# Custom parameters
./train.sh --epochs 200 --batch 16 --img 320

# Resume interrupted training
./train.sh --resume
```

Training uses COCO-pretrained YOLOv5n weights for transfer learning,
which significantly improves convergence on small datasets.

### 3. Export to ONNX

Training script auto-exports, but you can also do it manually:

```bash
cd third_party/airockchip_yolov5
python export.py \
    --weights runs/au_speed_signs/v1/weights/best.pt \
    --img-size 320 320 \
    --batch-size 1 \
    --rknpu \
    --include onnx
```

The `--rknpu` flag is **required** -- it removes post-processing subgraphs
that are incompatible with RKNN INT8 quantization.

### 4. Convert ONNX to RKNN

```bash
cd ../../models

# Prepare calibration images (20-100 representative images)
# List their paths in dataset.txt, one per line

python convert_to_rknn.py \
    --onnx au_speed_signs.onnx \
    --output au_speed_signs_rv1106.rknn \
    --dataset dataset.txt
```

Note: RKNN conversion requires Linux x86_64 and `rknn-toolkit2`.
Use Docker if on macOS:
```bash
cd ../docker
# See docker/README.md for RKNN conversion container
```

### 5. Deploy to Device

```bash
adb push au_speed_signs_rv1106.rknn /root/model/
adb shell "killall ai-hud; /root/ai-hud --model /root/model/au_speed_signs_rv1106.rknn"
```

## Adding Custom Data

### Speed Camera Images

The `speed_camera` class is underrepresented in public datasets.
To improve detection:

1. Collect images of Australian speed cameras (fixed pole cameras,
   mobile camera vans, average speed cameras)
2. Use [Roboflow](https://app.roboflow.com) to annotate bounding boxes
3. Export in YOLOv5 format with class ID `8` for speed_camera
4. Place in `custom_data/images/` and `custom_data/labels/`
5. Re-run: `python prepare_dataset.py --local-dir custom_data`

### Label Format (YOLO TXT)

Each image `foo.jpg` needs a corresponding `foo.txt` with one line per object:
```
<class_id> <cx> <cy> <width> <height>
```
All values normalized to [0, 1]. Example:
```
2 0.5123 0.3456 0.0812 0.1234
8 0.7890 0.6543 0.0456 0.0678
```

## Expected Performance

| Metric | COCO 80-class (current) | AU 9-class (target) |
|--------|------------------------|---------------------|
| NPU inference | ~61ms | ~61ms (hardware fixed) |
| Postprocess | ~20ms | <5ms (9 vs 80 classes) |
| Total latency | ~81ms | ~66ms |
| Model size | ~4MB | ~2MB |

## Troubleshooting

**Q: Roboflow download fails**
A: Check API key validity. Free tier allows 3 dataset downloads/month.
   Alternative: download manually from Roboflow web UI in YOLOv5 format.

**Q: Training loss not decreasing**
A: Check dataset quality. Use `--weights yolov5n.pt` for transfer learning.
   Increase epochs to 200+. Check for mislabeled images.

**Q: ONNX export error with --rknpu**
A: Must use airockchip/yolov5 fork, not the official ultralytics version.

**Q: RKNN conversion fails**
A: Ensure using rknn-toolkit2 >= 2.3.0 on Linux x86_64.
   Check calibration images exist and paths in dataset.txt are correct.

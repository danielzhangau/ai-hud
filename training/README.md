# Speed Sign Detection -- Model Training

Train a **universal** YOLOv5n model for the AI-Powered HUD project, targeting
deployment on Luckfox Pico Ultra (RV1106G3, 0.5 TOPS NPU, INT8).

Single model covers both **AU** (Australia) and **CN** (China) speed limits.
Region-specific filtering is handled at runtime in Python (`hud_live.py`).

## Universal Model -- 11 Classes

| ID | Class | AU | CN |
|---:|-------|:--:|:--:|
| 0 | speed_sign_20 | - | Y |
| 1 | speed_sign_30 | Y | Y |
| 2 | speed_sign_40 | Y | Y |
| 3 | speed_sign_50 | Y | Y |
| 4 | speed_sign_60 | Y | Y |
| 5 | speed_sign_70 | Y | Y |
| 6 | speed_sign_80 | Y | Y |
| 7 | speed_sign_90 | Y | - |
| 8 | speed_sign_100 | Y | Y |
| 9 | speed_sign_110 | Y | Y |
| 10 | speed_sign_120 | - | Y |

Speed camera detection uses GPS database (not vision).

## Directory Structure

```
training/
  README.md                          # This file
  train_colab.ipynb                   # Training notebook (Colab)
  train.sh                           # Local training helper script
  datasets/
    mtsd/                            # Mapillary Traffic Sign Dataset (primary)
      README.md                      #   Dataset documentation + statistics
      mtsd_prepare.py                #   MTSD -> YOLO conversion script
      speed_signs_dataset.tar.gz     #   Dataset archive (~3G, upload to Colab)
      annotations/                   #   41,909 JSON annotation files
      mtsd_fully_annotated_images.*.zip  # Image archives (5 files, ~42G)
```

## Data Sources

| Dataset | Images | BBox Style | Status |
|---------|--------|-----------|--------|
| MTSD (Mapillary) | 3,905 (filtered) | Sign face only (w/h ~0.99) | **Primary** |
| AU Roboflow + GTSDB | ~1,900 | Sign + sub-plate (w/h ~0.56) | Legacy |
| TT100K | ~2,000 (filtered) | Sign face only (w/h ~1.0) | Legacy |

### Annotation Incompatibility

AU Roboflow and MTSD have different bbox conventions and CANNOT be merged:
- **AU Roboflow**: BBox includes sign + supplementary plate (tall rectangular)
- **MTSD / TT100K**: BBox covers only circular sign face (near-square)

## Training Workflow

### Google Colab (Recommended -- free T4 GPU)

1. Open `train_colab.ipynb` in Colab
2. Upload `datasets/mtsd/speed_signs_dataset.tar.gz` (~3G)
3. Train: YOLOv5n, 640x640, 300 epochs (~2.5h on T4)
4. Download: `speed_signs_v2.pt`, `speed_signs_v2.onnx`, `speed_signs_v2_rv1106.rknn`

### Local Dataset Rebuild

```bash
cd datasets/mtsd
python mtsd_prepare.py --region universal --output speed_signs_dataset
```

## Export & Deploy

```bash
# Export ONNX (must use airockchip/yolov5 fork with --rknpu flag)
python export.py --weights best.pt --img-size 640 640 --rknpu --include onnx

# Convert to RKNN INT8 (see train_colab.ipynb cell 7-8)

# Deploy
adb push speed_signs_v2_rv1106.rknn /root/model/
adb push build/ai-hud /root/ai-hud
```

No region-specific build flags needed. `OBJ_CLASS_NUM=11` is the default.

## Troubleshooting

- **Training loss plateau**: Use `--weights yolov5n.pt` for transfer learning. Increase epochs.
- **ONNX export error with --rknpu**: Must use airockchip/yolov5 fork, not ultralytics.
- **RKNN conversion fails**: Ensure rknn-toolkit2 >= 2.3.0, use virtualenv to avoid dependency conflicts.
- **Pillow getsize error**: Notebooks include auto-patch. Alternatively `pip install "Pillow<10"`.

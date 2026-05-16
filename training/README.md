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

## Training Results

| Run | Date | Platform | Dataset | mAP@0.5 | mAP@0.5:0.95 | Status |
|-----|------|----------|---------|---------|---------------|--------|
| [v2_universal](runs/2026-05-17_v2_universal/) | 2026-05-17 | Kaggle T4 | v2 crop-augmented (8,061 train) | **0.943** | **0.746** | Deployed |
| v1_universal | 2026-05-14 | Colab T4 | MTSD raw (3,905 images) | 0.17 | -- | Failed (tiny bbox) |

## Directory Structure

```
training/
  README.md                        # This file
  train_kaggle.ipynb               # Kaggle training notebook (recommended)
  train_colab.ipynb                # Colab training notebook
  train_local.sh                   # Local training helper script (Mac M4 MPS)
  augment_crops.py                 # Two-tier crop augmentation strategy
  patch_yolov5_compat.py           # PyTorch/Pillow compatibility patches
  preview_labels.py                # Label visualization tool
  download_wheels.py               # RKNN toolkit offline wheel downloader
  download_wheels.sh               # Shell version of above
  runs/                            # Training results archive (timestamped)
    2026-05-17_v2_universal/       #   v2 results + charts
  datasets/                        # (gitignored, ~6GB)
    mtsd/                          #   Mapillary Traffic Sign Dataset
```

## Data Sources

| Dataset | Images | BBox Style | Status |
|---------|--------|-----------|--------|
| MTSD (Mapillary) | 3,905 (filtered) | Sign face only (w/h ~0.99) | **Primary** |
| AU Roboflow + GTSDB | ~1,900 | Sign + sub-plate (w/h ~0.56) | Legacy |
| TT100K | ~2,000 (filtered) | Sign face only (w/h ~1.0) | Legacy |

### Dataset v2: Crop Augmentation

v1 failed because 76.5% of bboxes were <6px at 640x640 (too tiny for YOLOv5n).

v2 uses `augment_crops.py` with a two-tier crop strategy:
- **Scene crops** (70%, pad 15-25x): median 31px@640, matches dashcam 30-60m viewing
- **Detail crops** (30%, pad 4-8x): median 97px@640, teaches digit classification
- **Class balancing**: ~800 samples per class
- **Result**: 8,061 train + 461 val images

### Annotation Incompatibility

AU Roboflow and MTSD have different bbox conventions and CANNOT be merged:
- **AU Roboflow**: BBox includes sign + supplementary plate (tall rectangular)
- **MTSD / TT100K**: BBox covers only circular sign face (near-square)

## Training Workflow

### Kaggle (Recommended -- free T4 GPU, 30h/week)

1. Open `train_kaggle.ipynb` in Kaggle
2. Add dataset: upload `datasets/mtsd/speed_signs_dataset.tar.gz` (~3G)
3. Train: YOLOv5n, 640x640, 300 epochs (~3h on T4)
4. RKNN conversion included in notebook (rknn-toolkit2 2.3.2)
5. Download from Output tab: `.pt`, `.onnx`, `.rknn`

### Google Colab (Alternative)

1. Open `train_colab.ipynb` in Colab
2. Upload dataset to Colab runtime or Google Drive
3. Same training config as Kaggle

### Local (Mac M4 MPS)

```bash
cd training
./train_local.sh
```

### Local Dataset Rebuild

```bash
cd datasets/mtsd
python mtsd_prepare.py --region universal --output speed_signs_dataset
python ../../augment_crops.py  # Apply two-tier crop augmentation
```

## Export & Deploy

```bash
# Export ONNX (must use airockchip/yolov5 fork with --rknpu flag)
python export.py --weights best.pt --img-size 640 640 --rknpu --include onnx

# Convert to RKNN INT8 (see train_kaggle.ipynb cell 7-8)

# Deploy
adb push speed_signs_rv1106.rknn /root/model/
adb push build/ai-hud /root/ai-hud
```

No region-specific build flags needed. `OBJ_CLASS_NUM=11` is the default.

## Troubleshooting

- **Training loss plateau**: Use `--weights yolov5n.pt` for transfer learning. Increase epochs.
- **Tiny bbox problem (v1)**: Use `augment_crops.py` to generate crop-augmented dataset.
- **ONNX export error with --rknpu**: Must use airockchip/yolov5 fork, not ultralytics.
- **RKNN conversion fails**: Ensure rknn-toolkit2 >= 2.3.0, use virtualenv to avoid dependency conflicts.
- **Pillow getsize error**: Notebooks include auto-patch. Alternatively `pip install "Pillow<10"`.
- **torch.cuda.amp deprecation**: `patch_yolov5_compat.py` auto-fixes autocast + GradScaler.

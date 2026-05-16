# Training Run: v2 Universal (2026-05-17)

## Summary

| Item | Value |
|------|-------|
| Date | 2026-05-17 |
| Platform | Kaggle (Tesla T4, 16GB VRAM) |
| Model | YOLOv5n @ 640x640 INT8 |
| Classes | 11 (speed_sign_20 ~ speed_sign_120) |
| Dataset | v2 crop-augmented, 8,061 train + 461 val |
| Epochs | 300 |
| Batch size | 32 |
| Image size | 640x640 |
| Base weights | yolov5n.pt (transfer learning) |
| Run name | `universal` |
| Notebook | `training/train_kaggle.ipynb` |

## Results

| Metric | Value |
|--------|-------|
| mAP@0.5 | **0.943** |
| mAP@0.5:0.95 | **0.746** |
| Precision | 0.935 |
| Recall | 0.884 |
| Best F1 | 0.91 @ conf=0.664 |

### Per-class Performance

| Class | Images | Instances | P | R | mAP@0.5 | mAP@0.5:0.95 |
|-------|--------|-----------|------|------|---------|---------------|
| speed_sign_20 | 570 | 27 | 0.959 | 0.857 | 0.929 | 0.757 |
| speed_sign_30 | 570 | 102 | 0.969 | 0.925 | 0.989 | 0.801 |
| speed_sign_40 | 570 | 129 | 0.925 | 0.907 | 0.968 | 0.760 |
| speed_sign_50 | 570 | 94 | 0.966 | 0.919 | 0.970 | 0.789 |
| speed_sign_60 | 570 | 84 | 0.884 | 0.726 | 0.897 | 0.628 |
| speed_sign_70 | 570 | 45 | 0.978 | 0.967 | 0.975 | 0.806 |
| speed_sign_80 | 570 | 75 | 0.778 | 0.893 | 0.864 | 0.654 |
| speed_sign_90 | 570 | 65 | 0.960 | 0.831 | 0.950 | 0.744 |
| speed_sign_100 | 570 | 41 | 0.914 | 0.776 | 0.874 | 0.662 |
| speed_sign_110 | 570 | 22 | 0.980 | 1.000 | 0.995 | 0.804 |
| speed_sign_120 | 570 | 13 | 0.976 | 0.923 | 0.956 | 0.805 |

### Weak Classes (acceptable with GPS fusion)

- **speed_sign_60**: mAP50=0.897, R=0.726 -- lowest recall
- **speed_sign_80**: mAP50=0.864, P=0.778 -- 14% confused with 60
- **speed_sign_100**: mAP50=0.874, R=0.776

### Confusion Patterns

- 80 vs 60: 14% misclassification (similar digit shapes)
- 80 vs background: 27% miss rate (highest)
- 90 vs background: 18% miss rate
- 40 vs background: 14% miss rate

## Dataset

- **Source**: MTSD (Mapillary Traffic Sign Dataset)
- **Augmentation**: `training/augment_crops.py` two-tier crop strategy
  - Scene crops (70%, pad 15-25x): median 31px@640, matches dashcam 30-60m
  - Detail crops (30%, pad 4-8x): median 97px@640, teaches digit classification
- **Bbox distribution**: 29% tiny (orig context) + 53% small (deploy scale) + 18% med/large
- **Class balancing**: ~800 samples per class

## Artifacts

| File | Size | Description |
|------|------|-------------|
| speed_signs_rv1106.rknn | 1.94 MB | RKNN INT8, deploy to `/root/model/` |
| speed_signs.pt | 3.74 MB | PyTorch weights |
| speed_signs.onnx | 7.00 MB | ONNX model |

## Comparison with v1

| | v1 (MTSD raw) | v2 (crop-augmented) |
|--|--------------|---------------------|
| mAP@0.5 | 0.17 | **0.943** |
| Dataset | 3,905 images (76.5% tiny bbox) | 8,061 train (balanced crops) |
| Root cause | Tiny bboxes <6px@640 | Two-tier crops -> median 31-97px |

## Files in This Directory

- `training_curve.png` -- Loss and metrics over 300 epochs
- `confusion_matrix.png` -- Per-class confusion analysis
- `PR_curve.png` -- Precision-Recall curves (all classes 0.941 mAP@0.5)
- `F1_curve.png` -- F1 vs confidence (optimal threshold 0.664)
- `training_sample.png` -- Dataset visualization (crop augmentation examples)

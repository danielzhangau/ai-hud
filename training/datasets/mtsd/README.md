# MTSD: Mapillary Traffic Sign Dataset

Global traffic sign dataset with 52,453 fully annotated images.

## Source

- [mapillary.com/dataset/trafficsign](https://www.mapillary.com/dataset/trafficsign)
- Paper: [ECCV 2020](https://arxiv.org/abs/1909.04422)
- License: Free for academic research

## Scale

- 52,453 images (train: 36,589 / val: 5,320 / test: 10,544)
- 206,386 annotations, 401 classes
- 6,237 speed limit annotations across g1/g2/g3/g6 groups

## Annotation Style

BBox covers circular sign face only (near-square, w/h ~0.99).
Vienna Convention style (g1 + g3) used for universal model.

## Appearance Groups

| Group | Style | Used |
|-------|-------|------|
| g1 | Vienna Convention (red circle, white bg) | Yes (5,244 annotations) |
| g3 | AU/NZ variant (visually same as g1) | Yes (396 annotations) |
| g2 | MUTCD (US white rectangle) | No -- incompatible style |
| g6 | Other variant | No -- too few (51) |

## Universal Model -- 11 Classes

Single model covering both AU and CN speed limits.

| ID | Class | AU | CN | g1+g3 Data |
|---:|-------|:--:|:--:|:----------:|
| 0 | speed_sign_20 | - | Y | 225 |
| 1 | speed_sign_30 | Y | Y | 792 |
| 2 | speed_sign_40 | Y | Y | 1,026 |
| 3 | speed_sign_50 | Y | Y | 663 |
| 4 | speed_sign_60 | Y | Y | 634 |
| 5 | speed_sign_70 | Y | Y | 319 |
| 6 | speed_sign_80 | Y | Y | 429 |
| 7 | speed_sign_90 | Y | - | 267 |
| 8 | speed_sign_100 | Y | Y | 280 |
| 9 | speed_sign_110 | Y | Y | 115 |
| 10 | speed_sign_120 | - | Y | 108 |
| | **TOTAL** | | | **4,858** |

### Built Dataset

| Metric | Value |
|--------|-------|
| Total images | 3,905 |
| Train / Val | 3,414 / 491 |
| Total annotations | 4,858 |
| Archive | `speed_signs_dataset.tar.gz` (~3G) |

## Files

```
mtsd/
  README.md                               # This file
  mtsd_prepare.py                         # MTSD -> YOLO conversion script
  speed_signs_dataset/                    # Built dataset (ready for training)
    data.yaml                             # YOLOv5 config (11 classes)
    train/images/*.jpg                    # 3,414 training images
    train/labels/*.txt                    # YOLO format labels
    val/images/*.jpg                      # 491 validation images
    val/labels/*.txt
  speed_signs_dataset.tar.gz              # Upload to Colab (~3G)
  annotations/                            # 41,909 JSON annotation files
    mtsd_v2_fully_annotated/
      annotations/*.json
      splits/{train,val,test}.txt
  mtsd_fully_annotated_images.*.zip       # Image archives (5 files, ~42G)
  md5_sums.txt                            # Checksums
```

## Usage

```bash
# Build universal dataset (11 classes, g1+g3)
python mtsd_prepare.py --region universal --output speed_signs_dataset
```

## Notes

MTSD labels follow the pattern `{category}--{sign-name}--{appearance-group}`,
e.g. `regulatory--maximum-speed-limit-60--g1`.

Excluded speed values (5/10/15/25/45) have insufficient data or are not
standard road speed limits in AU/CN.

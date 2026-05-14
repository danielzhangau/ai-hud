#!/usr/bin/env bash
# =============================================================
# Local YOLOv5n Training on Mac (Apple Silicon MPS)
# =============================================================
# Usage:  cd training && bash train_local.sh
#
# Prerequisites:
#   brew install python@3.11   (or any 3.9-3.12)
#   Dataset at: datasets/mtsd/speed_signs_dataset/
#
# Output:
#   yolov5/runs/speed_signs/universal/weights/best.pt   (PyTorch)
#   yolov5/runs/speed_signs/universal/weights/best.onnx  (ONNX, for RKNN conversion)
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATASET_DIR="$SCRIPT_DIR/datasets/mtsd/speed_signs_dataset"
VENV_DIR="$SCRIPT_DIR/.venv-yolo"
YOLOV5_DIR="$SCRIPT_DIR/yolov5"

RUN_NAME="universal"
PROJECT_NAME="speed_signs"
IMG_SIZE=640
BATCH_SIZE=32
EPOCHS=300
WORKERS=4  # M4 has plenty of cores

# -- Colors for output --
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# =============================================================
# 1. Verify dataset
# =============================================================
info "Checking dataset..."
[ -d "$DATASET_DIR/train/images" ] || error "Dataset not found at $DATASET_DIR"
TRAIN_COUNT=$(ls "$DATASET_DIR/train/images/" | wc -l | tr -d ' ')
VAL_COUNT=$(ls "$DATASET_DIR/val/images/" | wc -l | tr -d ' ')
info "  train: $TRAIN_COUNT images, val: $VAL_COUNT images"

# =============================================================
# 2. Setup Python venv
# =============================================================
if [ ! -d "$VENV_DIR" ]; then
    info "Creating Python venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
info "Python: $(python3 --version), venv: $VENV_DIR"

# =============================================================
# 3. Clone airockchip/yolov5 (RKNN-optimized fork)
# =============================================================
if [ ! -d "$YOLOV5_DIR" ]; then
    info "Cloning airockchip/yolov5..."
    git clone https://github.com/airockchip/yolov5.git "$YOLOV5_DIR"
fi

cd "$YOLOV5_DIR"

# Install dependencies
info "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q onnxscript  # Required for ONNX export with PyTorch >= 2.6

# =============================================================
# 4. Apply compatibility patches (torch.load + Pillow getsize)
# =============================================================
info "Applying compatibility patches..."
python3 "$SCRIPT_DIR/patch_yolov5_compat.py"

# =============================================================
# 6. Update data.yaml with local absolute paths
# =============================================================
info "Updating data.yaml paths..."
python3 - <<PYEOF
import yaml

data = {
    "train": "$DATASET_DIR/train/images",
    "val": "$DATASET_DIR/val/images",
    "nc": 11,
    "names": [
        "speed_sign_20",  "speed_sign_30",  "speed_sign_40",
        "speed_sign_50",  "speed_sign_60",  "speed_sign_70",
        "speed_sign_80",  "speed_sign_90",  "speed_sign_100",
        "speed_sign_110", "speed_sign_120",
    ],
}
with open("$DATASET_DIR/data.yaml", "w") as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
print("  data.yaml updated with local paths")
PYEOF

# =============================================================
# 7. Train
# =============================================================
info "Starting training on MPS (Apple Silicon)..."
info "  Model: YOLOv5n | Resolution: ${IMG_SIZE} | Epochs: ${EPOCHS} | Batch: ${BATCH_SIZE}"
echo ""

LAST_PT="runs/$PROJECT_NAME/$RUN_NAME/weights/last.pt"
if [ -f "$LAST_PT" ]; then
    info "Resuming from checkpoint: $LAST_PT"
    python3 train.py \
        --resume "$LAST_PT" \
        --batch-size "$BATCH_SIZE" \
        --workers "$WORKERS" \
        --device mps
else
    python3 train.py \
        --data "$DATASET_DIR/data.yaml" \
        --cfg yolov5n.yaml \
        --weights yolov5n.pt \
        --img "$IMG_SIZE" \
        --batch-size "$BATCH_SIZE" \
        --epochs "$EPOCHS" \
        --workers "$WORKERS" \
        --project "runs/$PROJECT_NAME" \
        --name "$RUN_NAME" \
        --exist-ok \
        --cache ram \
        --device mps
fi

# =============================================================
# 8. Export ONNX (for RKNN conversion)
# =============================================================
WEIGHTS="runs/$PROJECT_NAME/$RUN_NAME/weights/best.pt"
info "Exporting ONNX..."

python3 export.py \
    --weights "$WEIGHTS" \
    --img-size "$IMG_SIZE" "$IMG_SIZE" \
    --batch-size 1 \
    --rknpu \
    --include onnx

ONNX_PATH="${WEIGHTS%.pt}.onnx"
if [ -f "$ONNX_PATH" ]; then
    SIZE=$(du -h "$ONNX_PATH" | cut -f1)
    info "ONNX exported: $ONNX_PATH ($SIZE)"
else
    error "ONNX export failed!"
fi

# =============================================================
# 9. Summary
# =============================================================
echo ""
echo "=============================================="
info "Training complete!"
echo "=============================================="
echo "  PyTorch: $WEIGHTS"
echo "  ONNX:    $ONNX_PATH"
echo ""
echo "  Next step: RKNN conversion (requires x86 Linux)"
echo "  Upload the ONNX file to Colab and run convert_rknn notebook."
echo "=============================================="

#!/bin/bash
# ============================================================
# YOLOv5n Training Script -- Australian Speed Signs (9 classes)
#
# Requirements:
#   - Python 3.8+
#   - PyTorch >= 1.10 with CUDA (GPU recommended)
#   - airockchip/yolov5 fork (required for RKNN export)
#
# Usage:
#   ./train.sh                    # Full training
#   ./train.sh --resume           # Resume from last checkpoint
#   ./train.sh --epochs 50        # Custom epoch count
#   ./train.sh --colab            # Print Colab setup commands
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
YOLOV5_DIR="${PROJECT_DIR}/third_party/airockchip_yolov5"
DATASET_DIR="${SCRIPT_DIR}/au_speed_dataset"
DATASET_YAML="${DATASET_DIR}/data.yaml"

# Training hyperparameters
IMG_SIZE=320        # Match deployment input size
BATCH_SIZE=32       # Reduce if GPU OOM
EPOCHS=100
WORKERS=4
MODEL_CFG="yolov5n.yaml"    # Nano model for embedded deployment
WEIGHTS="yolov5n.pt"        # Pretrained COCO weights for transfer learning
PROJECT_NAME="au_speed_signs"

# ============================================================
# Parse arguments
# ============================================================

RESUME=""
COLAB_MODE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --resume)
            RESUME="--resume"
            shift
            ;;
        --epochs)
            EPOCHS="$2"
            shift 2
            ;;
        --batch)
            BATCH_SIZE="$2"
            shift 2
            ;;
        --img)
            IMG_SIZE="$2"
            shift 2
            ;;
        --colab)
            COLAB_MODE="1"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --resume         Resume from last checkpoint"
            echo "  --epochs N       Number of epochs (default: ${EPOCHS})"
            echo "  --batch N        Batch size (default: ${BATCH_SIZE})"
            echo "  --img N          Image size (default: ${IMG_SIZE})"
            echo "  --colab          Print Google Colab setup commands"
            echo "  --help           Show this help"
            exit 0
            ;;
        *)
            echo "[Error] Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================
# Google Colab mode -- print setup commands
# ============================================================

if [ -n "$COLAB_MODE" ]; then
    cat <<'COLAB_EOF'
# ============================================================
# Google Colab Training Setup
# Copy and paste the following cells into a Colab notebook.
# Runtime -> Change runtime type -> GPU (T4 is sufficient)
# ============================================================

# --- Cell 1: Clone repos and install dependencies ---
!git clone https://github.com/airockchip/yolov5.git
%cd yolov5
!pip install -r requirements.txt
!pip install roboflow

# --- Cell 2: Prepare dataset ---
# Upload prepare_dataset.py and au_speed_signs.yaml to Colab
# Or clone your repo:
# !git clone https://github.com/danielzhangau/ai-hud.git
# !cp ai-hud/training/prepare_dataset.py .
# !cp ai-hud/training/au_speed_signs.yaml .

!python prepare_dataset.py --roboflow-key YOUR_API_KEY --output ../au_speed_dataset

# --- Cell 3: Train ---
!python train.py \
    --data ../au_speed_dataset/data.yaml \
    --cfg yolov5n.yaml \
    --weights yolov5n.pt \
    --img 320 \
    --batch 32 \
    --epochs 100 \
    --project runs/au_speed_signs \
    --name v1

# --- Cell 4: Export ONNX for RKNN ---
!python export.py \
    --weights runs/au_speed_signs/v1/weights/best.pt \
    --img-size 320 320 \
    --batch-size 1 \
    --rknpu \
    --include onnx

# --- Cell 5: Download results ---
from google.colab import files
files.download('runs/au_speed_signs/v1/weights/best.pt')
files.download('runs/au_speed_signs/v1/weights/best.onnx')

COLAB_EOF
    exit 0
fi

# ============================================================
# Local training mode
# ============================================================

echo "=========================================="
echo " YOLOv5n Training -- AU Speed Signs"
echo "=========================================="

# Step 1: Check/clone airockchip/yolov5
if [ ! -d "${YOLOV5_DIR}" ]; then
    echo ""
    echo "[1/4] Cloning airockchip/yolov5..."
    mkdir -p "${PROJECT_DIR}/third_party"
    git clone https://github.com/airockchip/yolov5.git "${YOLOV5_DIR}"
    pip install -r "${YOLOV5_DIR}/requirements.txt"
else
    echo ""
    echo "[1/4] airockchip/yolov5 found at: ${YOLOV5_DIR}"
fi

# Step 2: Check dataset
if [ ! -f "${DATASET_YAML}" ]; then
    echo ""
    echo "[2/4] Dataset not found. Running prepare_dataset.py..."
    echo "      (You may need to provide a Roboflow API key)"
    python3 "${SCRIPT_DIR}/prepare_dataset.py" --output "${DATASET_DIR}"
fi

if [ ! -f "${DATASET_YAML}" ]; then
    echo "[Error] Dataset preparation failed. Check logs above."
    exit 1
fi

echo ""
echo "[2/4] Dataset: ${DATASET_YAML}"

# Step 3: Train
echo ""
echo "[3/4] Starting training..."
echo "      Model:   ${MODEL_CFG}"
echo "      Weights: ${WEIGHTS} (pretrained)"
echo "      Image:   ${IMG_SIZE}x${IMG_SIZE}"
echo "      Batch:   ${BATCH_SIZE}"
echo "      Epochs:  ${EPOCHS}"
echo ""

cd "${YOLOV5_DIR}"

python3 train.py \
    --data "${DATASET_YAML}" \
    --cfg "${MODEL_CFG}" \
    --weights "${WEIGHTS}" \
    --img "${IMG_SIZE}" \
    --batch-size "${BATCH_SIZE}" \
    --epochs "${EPOCHS}" \
    --workers "${WORKERS}" \
    --project "runs/${PROJECT_NAME}" \
    --name "v1" \
    --exist-ok \
    ${RESUME}

echo ""
echo "[3/4] Training complete!"

# Step 4: Export to ONNX with --rknpu flag
BEST_PT="runs/${PROJECT_NAME}/v1/weights/best.pt"
if [ -f "${BEST_PT}" ]; then
    echo ""
    echo "[4/4] Exporting to ONNX (RKNPU compatible)..."
    python3 export.py \
        --weights "${BEST_PT}" \
        --img-size "${IMG_SIZE}" "${IMG_SIZE}" \
        --batch-size 1 \
        --rknpu \
        --include onnx

    BEST_ONNX="runs/${PROJECT_NAME}/v1/weights/best.onnx"
    if [ -f "${BEST_ONNX}" ]; then
        # Copy to models directory
        cp "${BEST_ONNX}" "${PROJECT_DIR}/models/au_speed_signs.onnx"
        echo ""
        echo "=========================================="
        echo " Training & Export Complete!"
        echo "=========================================="
        echo "  Weights: ${BEST_PT}"
        echo "  ONNX:    ${PROJECT_DIR}/models/au_speed_signs.onnx"
        echo ""
        echo "  Next: Convert ONNX -> RKNN"
        echo "    cd ${PROJECT_DIR}/models"
        echo "    python convert_to_rknn.py --onnx au_speed_signs.onnx \\"
        echo "        --output au_speed_signs_rv1106.rknn"
        echo ""
        echo "  Then deploy to device:"
        echo "    adb push au_speed_signs_rv1106.rknn /root/model/"
    fi
else
    echo "[Error] Training output not found: ${BEST_PT}"
    exit 1
fi

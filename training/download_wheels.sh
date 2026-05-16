#!/usr/bin/env bash
# Download large wheel files with curl (supports resume on broken connections)
# Then pip install from local files
set -euo pipefail

WHEELS_DIR="$(cd "$(dirname "$0")" && pwd)/.wheels"
VENV_DIR="$(cd "$(dirname "$0")" && pwd)/.venv-yolo"
YOLOV5_DIR="$(cd "$(dirname "$0")" && pwd)/yolov5"
mkdir -p "$WHEELS_DIR"

source "$VENV_DIR/bin/activate"

# Robust download function: curl with resume + retry
dl() {
    local url="$1"
    local out="$WHEELS_DIR/$(basename "$url")"
    echo "Downloading $(basename "$url") ..."
    local attempt=0
    while [ $attempt -lt 20 ]; do
        if curl -fSL -C - --retry 3 --retry-delay 2 --connect-timeout 30 \
               --max-time 300 -o "$out" "$url" 2>&1; then
            echo "  OK: $(du -h "$out" | cut -f1)"
            return 0
        fi
        attempt=$((attempt + 1))
        echo "  Retry $attempt/20 (resuming)..."
        sleep 1
    done
    echo "  FAILED: $url"
    return 1
}

echo "=== Step 1: Download large wheels with curl (resume-capable) ==="

# PyTorch (arm64 macOS) - ~77MB
dl "https://files.pythonhosted.org/packages/6a/63/99f498b1b25e3cafd%3D/torch-2.7.0-cp312-none-macosx_11_0_arm64.whl" || true

# Actually, let pip resolve the correct URLs. Instead, let's use pip's
# download with a wrapper that retries the whole operation.

echo ""
echo "=== Step 2: pip install with small-batch strategy ==="
echo "Installing packages one group at a time to minimize download size per attempt..."

install_group() {
    local name="$1"
    shift
    echo ""
    echo "--- Installing: $name ---"
    local attempt=0
    while [ $attempt -lt 10 ]; do
        if pip install "$@" --timeout 120 2>&1 | tail -3; then
            echo "  $name: OK"
            return 0
        fi
        attempt=$((attempt + 1))
        echo "  $name: retry $attempt/10..."
        sleep 2
    done
    echo "  $name: FAILED after 10 attempts"
    return 1
}

# Install small packages first (all < 4MB, should work)
install_group "small-deps" \
    gitpython pyyaml requests tqdm psutil thop ipython pandas seaborn tensorboard

# Install medium packages one by one
install_group "numpy" numpy
install_group "Pillow" Pillow
install_group "matplotlib" matplotlib
install_group "scipy" scipy
install_group "opencv" opencv-python
install_group "torch" torch torchvision
install_group "onnxscript" onnxscript

echo ""
echo "=== Done ==="
pip list | grep -iE "(torch|matplotlib|numpy|opencv|pillow|scipy)"

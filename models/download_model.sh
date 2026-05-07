#!/bin/bash
# ============================================================
# 下载 YOLOv5n ONNX 模型 (airockchip 优化版)
#
# 来源: airockchip/rknn_model_zoo
# 模型说明:
#   该模型是 airockchip 针对 RKNN 平台优化过的版本,
#   与 ultralytics 官方原版不同 -- 裁剪了不适合量化的后处理子图,
#   例如输出从 [1,19200,85] 改为 [1,255,80,80] 形式,
#   后处理需要在应用层自行实现.
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="${SCRIPT_DIR}"

# ============================================================
# 方式一: 从 airockchip 文件服务器直接下载 (推荐)
# ============================================================

YOLOV5N_URL="https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/yolov5/yolov5n.onnx"
YOLOV5N_FILE="${MODEL_DIR}/yolov5n.onnx"

download_from_filez() {
    echo "=========================================="
    echo " 下载 YOLOv5n ONNX 模型 (airockchip 优化版)"
    echo "=========================================="

    if [ -f "${YOLOV5N_FILE}" ]; then
        echo "[信息] 模型文件已存在: ${YOLOV5N_FILE}"
        echo "[信息] 如需重新下载, 请先删除该文件"
        return 0
    fi

    echo "[信息] 下载地址: ${YOLOV5N_URL}"
    echo "[信息] 保存路径: ${YOLOV5N_FILE}"
    echo ""

    # 优先使用 wget, 备选 curl
    if command -v wget &> /dev/null; then
        wget -O "${YOLOV5N_FILE}" "${YOLOV5N_URL}"
    elif command -v curl &> /dev/null; then
        curl -L -o "${YOLOV5N_FILE}" "${YOLOV5N_URL}"
    else
        echo "[错误] 未找到 wget 或 curl, 请手动下载:"
        echo "  ${YOLOV5N_URL}"
        return 1
    fi

    if [ -f "${YOLOV5N_FILE}" ]; then
        FILE_SIZE=$(ls -lh "${YOLOV5N_FILE}" | awk '{print $5}')
        echo ""
        echo "[成功] 模型下载完成"
        echo "  文件: ${YOLOV5N_FILE}"
        echo "  大小: ${FILE_SIZE}"
    else
        echo "[错误] 下载失败"
        return 1
    fi
}

# ============================================================
# 方式二: 从 PyTorch 导出 ONNX (适用于自训练模型)
#
# 如果你有自己训练的 YOLOv5n 模型 (.pt), 需要使用
# airockchip 的 yolov5 fork 导出, 以确保输出格式兼容 RKNN.
#
# 步骤:
#   1. 克隆 airockchip/yolov5:
#      git clone https://github.com/airockchip/yolov5.git
#      cd yolov5
#      pip install -r requirements.txt
#
#   2. 使用 --rknpu 标志导出 ONNX:
#      python export.py --weights your_model.pt \
#                       --img-size 320 320 \
#                       --batch-size 1 \
#                       --rknpu \
#                       --include onnx
#
#   注意: 必须使用 --rknpu 标志!
#   该标志会裁剪不适合量化的后处理子图,
#   生成的 ONNX 模型才能被 rknn-toolkit2 正确转换.
#
#   3. 导出的 ONNX 文件在 yolov5/ 目录下,
#      将其复制到本目录即可使用 convert_to_rknn.py 转换.
# ============================================================

export_from_pytorch() {
    echo "=========================================="
    echo " 从 PyTorch 导出 ONNX (自训练模型)"
    echo "=========================================="

    PT_MODEL="${1:-best.pt}"

    if [ ! -f "${PT_MODEL}" ]; then
        echo "[错误] PyTorch 模型不存在: ${PT_MODEL}"
        echo "用法: $0 --export <your_model.pt>"
        return 1
    fi

    # 检查 airockchip/yolov5 是否已克隆
    YOLOV5_DIR="${MODEL_DIR}/../third_party/airockchip_yolov5"
    if [ ! -d "${YOLOV5_DIR}" ]; then
        echo "[信息] 克隆 airockchip/yolov5..."
        mkdir -p "${MODEL_DIR}/../third_party"
        git clone https://github.com/airockchip/yolov5.git "${YOLOV5_DIR}"
        pip install -r "${YOLOV5_DIR}/requirements.txt"
    fi

    echo "[信息] 使用 airockchip/yolov5 导出 ONNX..."
    echo "[信息] 输入模型: ${PT_MODEL}"
    echo "[信息] 输入尺寸: 320x320"

    cd "${YOLOV5_DIR}"
    python export.py \
        --weights "$(cd "${SCRIPT_DIR}" && realpath "${PT_MODEL}")" \
        --img-size 320 320 \
        --batch-size 1 \
        --rknpu \
        --include onnx

    echo "[成功] ONNX 导出完成, 请将生成的 .onnx 文件复制到 ${MODEL_DIR}/"
}

# ============================================================
# 主入口
# ============================================================

case "${1:-}" in
    --export)
        export_from_pytorch "${2:-}"
        ;;
    --help|-h)
        echo "用法:"
        echo "  $0              下载 airockchip 预训练 yolov5n.onnx"
        echo "  $0 --export <model.pt>  从 PyTorch 模型导出 ONNX"
        echo "  $0 --help       显示帮助信息"
        ;;
    *)
        download_from_filez
        ;;
esac

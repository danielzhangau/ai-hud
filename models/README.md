# YOLOv5n 模型转换 (ONNX -> RKNN)

将 YOLOv5n ONNX 模型转换为 RV1106 可用的 RKNN INT8 量化模型。

## 环境要求

- **操作系统**: Linux (x86_64 或 aarch64)
- **Python**: 3.8 - 3.12
- **rknn-toolkit2**: >= 2.3.0 (仅 PC 端转换使用, 非板端)

```bash
pip install rknn-toolkit2
```

> 注意: rknn-toolkit2 仅支持 Linux。如果使用 macOS/Windows，请在 Docker 容器中运行。

## 使用步骤

### 1. 下载预训练模型

```bash
cd models
chmod +x download_model.sh
./download_model.sh
```

模型来源: [airockchip/rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo)，已针对 RKNN 量化优化。

### 2. 准备量化校准数据集

创建 `dataset.txt` 文件，每行一个图片的绝对路径:

```
/path/to/calibration/image1.jpg
/path/to/calibration/image2.jpg
...
```

建议使用 20-100 张包含限速标志和测速摄像头的代表性图片。

### 3. 执行模型转换

```bash
python convert_to_rknn.py
```

常用参数:

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--onnx` | 输入 ONNX 模型路径 | `yolov5n.onnx` |
| `--output` | 输出 RKNN 模型路径 | `yolov5n_rv1106.rknn` |
| `--dataset` | 量化校准数据集 | `dataset.txt` |
| `--platform` | 目标平台 | `rv1106` |
| `--accuracy-analysis` | 执行量化精度分析 | 关闭 |
| `--verbose` | 详细日志 | 关闭 |

### 4. 部署到设备

将生成的 `.rknn` 文件通过 SCP 传输到 Luckfox Pico Ultra:

```bash
scp yolov5n_rv1106.rknn root@<device_ip>:/opt/ai-hud/models/
```

## 自训练模型导出

如果使用自己训练的 YOLOv5n 模型，需通过 [airockchip/yolov5](https://github.com/airockchip/yolov5) fork 导出:

```bash
./download_model.sh --export your_model.pt
```

必须使用 `--rknpu` 标志导出，以确保输出格式兼容 RKNN 量化。

## 技术细节

| 项目 | 值 |
|------|-----|
| 模型 | YOLOv5n (airockchip 优化版) |
| 输入尺寸 | 320 x 320 |
| 量化类型 | INT8 |
| 归一化 | mean=[0,0,0], std=[255,255,255] (即 pixel/255.0) |
| 目标平台 | rv1106 (Luckfox Pico Ultra, RV1106G3) |
| NPU 算力 | 1.0 TOPS |

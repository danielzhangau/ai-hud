# Speed Signs Model -- ONNX to RKNN Conversion

Convert YOLOv5n ONNX model to RKNN INT8 for RV1106 NPU deployment.

## Model Versions

| Version | Classes | Description |
|---------|---------|-------------|
| v2 | 11 (20-120 km/h) | Universal AU+CN, MTSD trained |
| v1 | 8-9 | Legacy AU/CN separate models |

## Output Files

Training produces three artifacts (named by `MODEL_VERSION` in notebook):

| File | Format | Usage |
|------|--------|-------|
| `speed_signs_v2.pt` | PyTorch | Archive / fine-tuning |
| `speed_signs_v2.onnx` | ONNX | Intermediate for RKNN conversion |
| `speed_signs_v2_rv1106.rknn` | RKNN INT8 | **Deploy to device** |

## Conversion

Conversion is handled automatically in `train_colab.ipynb` (cells 7-8).

For local conversion:

```bash
python convert_to_rknn.py
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--onnx` | Input ONNX model path | `yolov5n.onnx` |
| `--output` | Output RKNN model path | `yolov5n_rv1106.rknn` |
| `--dataset` | Calibration image list | `dataset.txt` |
| `--platform` | Target platform | `rv1106` |

Requires Linux x86_64 + `rknn-toolkit2 >= 2.3.0`.

## Deploy

```bash
adb push speed_signs_v2_rv1106.rknn /root/model/
adb push build/ai-hud /root/ai-hud
```

## Technical Details

| Item | Value |
|------|-------|
| Model | YOLOv5n (airockchip fork) |
| Input | 640 x 640 |
| Quantization | INT8 |
| Normalization | mean=[0,0,0], std=[255,255,255] |
| Target | rv1106 (RV1106G3, 0.5 TOPS NPU) |
| Classes | 11 (OBJ_CLASS_NUM=11) |

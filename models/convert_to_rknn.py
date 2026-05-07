#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLOv5n ONNX -> RKNN 模型转换脚本

目标硬件: Luckfox Pico Ultra (RV1106G3)
NPU: 1.0 TOPS, 仅支持 INT8 量化
模型输入: 320x320 RGB
用途: 限速标志识别 + 测速摄像头检测

参考:
  - airockchip/rknn_model_zoo: examples/yolov5/python/convert.py
  - airockchip/rknn-toolkit2: examples/onnx/yolov5/test.py

依赖: pip install rknn-toolkit2>=2.3.0
"""

import os
import sys
import argparse

from rknn.api import RKNN


# ============================================================
# 默认配置
# ============================================================

# ONNX 模型路径 (airockchip 优化版, 已裁剪不适合量化的后处理子图)
DEFAULT_ONNX_MODEL = os.path.join(os.path.dirname(__file__), 'yolov5n.onnx')

# 输出 RKNN 模型路径
DEFAULT_RKNN_MODEL = os.path.join(os.path.dirname(__file__), 'yolov5n_rv1106.rknn')

# 量化校准数据集文件 (每行一个图片路径, 建议 20-100 张代表性图片)
DEFAULT_DATASET = os.path.join(os.path.dirname(__file__), 'dataset.txt')

# 目标平台: rv1106 (适用于 RV1103/RV1106 系列)
TARGET_PLATFORM = 'rv1106'

# 输入尺寸
INPUT_SIZE = 320

# YOLOv5 标准预处理:
#   原始输入范围 0-255, 归一化到 0-1
#   mean_values=[[0,0,0]], std_values=[[255,255,255]]
#   等效于: output = (input - 0) / 255 = input / 255.0
MEAN_VALUES = [[0, 0, 0]]
STD_VALUES = [[255, 255, 255]]


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='将 YOLOv5n ONNX 模型转换为 RKNN 格式 (RV1106, INT8)')

    parser.add_argument('--onnx', type=str, default=DEFAULT_ONNX_MODEL,
                        help='输入 ONNX 模型路径 (默认: %(default)s)')
    parser.add_argument('--output', type=str, default=DEFAULT_RKNN_MODEL,
                        help='输出 RKNN 模型路径 (默认: %(default)s)')
    parser.add_argument('--dataset', type=str, default=DEFAULT_DATASET,
                        help='量化校准数据集文件 (默认: %(default)s)')
    parser.add_argument('--platform', type=str, default=TARGET_PLATFORM,
                        choices=['rv1103', 'rv1106', 'rk3562', 'rk3566',
                                 'rk3568', 'rk3576', 'rk3588'],
                        help='目标平台 (默认: %(default)s)')
    parser.add_argument('--no-quantize', action='store_true',
                        help='禁用量化 (RV1106 不支持此选项, 仅供调试)')
    parser.add_argument('--eval-perf', action='store_true',
                        help='转换后评估模型性能 (需要连接设备)')
    parser.add_argument('--accuracy-analysis', action='store_true',
                        help='执行量化精度分析 (对比量化前后输出差异)')
    parser.add_argument('--verbose', action='store_true',
                        help='打印详细日志')

    return parser.parse_args()


def check_dataset(dataset_path):
    """检查量化校准数据集是否存在且有效"""
    if not os.path.exists(dataset_path):
        print(f'[错误] 量化校准数据集文件不存在: {dataset_path}')
        print()
        print('请创建 dataset.txt 文件, 每行一个图片的绝对路径, 例如:')
        print('  /path/to/image1.jpg')
        print('  /path/to/image2.jpg')
        print()
        print('建议使用 20-100 张有代表性的图片 (包含限速标志和测速摄像头)')
        print('图片会被自动缩放到模型输入尺寸, 无需手动预处理')
        return False

    # 检查文件中的图片路径是否有效
    with open(dataset_path, 'r') as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    if len(lines) == 0:
        print(f'[错误] 量化校准数据集文件为空: {dataset_path}')
        return False

    missing = []
    for line in lines:
        if not os.path.exists(line):
            missing.append(line)

    if missing:
        print(f'[警告] 以下 {len(missing)} 个图片路径不存在:')
        for m in missing[:5]:
            print(f'  {m}')
        if len(missing) > 5:
            print(f'  ... 还有 {len(missing) - 5} 个')
        print()

    valid_count = len(lines) - len(missing)
    print(f'[信息] 校准数据集: {valid_count}/{len(lines)} 个有效图片')

    if valid_count == 0:
        print('[错误] 没有有效的校准图片')
        return False

    return True


def convert(args):
    """执行模型转换"""

    # ------------------------------------------------------------------
    # 步骤 0: 检查输入文件
    # ------------------------------------------------------------------
    if not os.path.exists(args.onnx):
        print(f'[错误] ONNX 模型不存在: {args.onnx}')
        print('请先运行 download_model.sh 下载模型')
        return -1

    do_quantization = not args.no_quantize

    # RV1106 强制要求 INT8 量化, 如果关闭量化会报错:
    # "Current target_platform(rv1106) not support do_quantization = False!"
    if args.platform in ('rv1103', 'rv1106') and not do_quantization:
        print(f'[警告] {args.platform} 平台强制要求 INT8 量化, 忽略 --no-quantize 参数')
        do_quantization = True

    if do_quantization and not check_dataset(args.dataset):
        return -1

    # ------------------------------------------------------------------
    # 步骤 1: 创建 RKNN 对象
    # ------------------------------------------------------------------
    rknn = RKNN(verbose=args.verbose)

    # ------------------------------------------------------------------
    # 步骤 2: 配置模型参数
    # ------------------------------------------------------------------
    # mean_values / std_values: YOLOv5 标准归一化
    #   输入为 0-255 的 uint8 图像
    #   归一化公式: output = (input - mean) / std = (input - 0) / 255
    #   等效于将像素值从 [0, 255] 映射到 [0.0, 1.0]
    # target_platform: 目标芯片平台
    print('--> 配置模型参数')
    rknn.config(
        mean_values=MEAN_VALUES,
        std_values=STD_VALUES,
        target_platform=args.platform,
    )
    print('    完成')

    # ------------------------------------------------------------------
    # 步骤 3: 加载 ONNX 模型
    # ------------------------------------------------------------------
    print(f'--> 加载 ONNX 模型: {args.onnx}')
    ret = rknn.load_onnx(model=args.onnx)
    if ret != 0:
        print('[错误] 加载 ONNX 模型失败!')
        rknn.release()
        return ret
    print('    完成')

    # ------------------------------------------------------------------
    # 步骤 4: 构建 RKNN 模型 (含 INT8 量化)
    # ------------------------------------------------------------------
    print(f'--> 构建 RKNN 模型 (量化: {do_quantization})')
    if do_quantization:
        print(f'    校准数据集: {args.dataset}')

    ret = rknn.build(
        do_quantization=do_quantization,
        dataset=args.dataset if do_quantization else None,
    )
    if ret != 0:
        print('[错误] 构建 RKNN 模型失败!')
        rknn.release()
        return ret
    print('    完成')

    # ------------------------------------------------------------------
    # 步骤 4.5 (可选): 量化精度分析
    # ------------------------------------------------------------------
    if args.accuracy_analysis and do_quantization:
        print('--> 执行量化精度分析...')
        # accuracy_analysis 会对比浮点模型和量化模型的输出差异
        # 输出 snapshot 目录包含每层的误差分析
        output_dir = os.path.join(os.path.dirname(args.output), 'accuracy_analysis')
        os.makedirs(output_dir, exist_ok=True)
        ret = rknn.accuracy_analysis(
            inputs=[args.dataset.split('\n')[0] if '\n' in args.dataset else None],
            output_dir=output_dir,
        )
        if ret != 0:
            print('[警告] 精度分析失败, 但不影响模型导出')
        else:
            print(f'    精度分析结果保存到: {output_dir}')

    # ------------------------------------------------------------------
    # 步骤 5: 导出 RKNN 模型
    # ------------------------------------------------------------------
    print(f'--> 导出 RKNN 模型: {args.output}')
    ret = rknn.export_rknn(args.output)
    if ret != 0:
        print('[错误] 导出 RKNN 模型失败!')
        rknn.release()
        return ret
    print('    完成')

    # ------------------------------------------------------------------
    # 步骤 6 (可选): 性能评估
    # ------------------------------------------------------------------
    if args.eval_perf:
        print('--> 评估模型性能 (需要连接目标设备)...')
        ret = rknn.init_runtime(target=args.platform)
        if ret != 0:
            print('[警告] 初始化运行环境失败, 跳过性能评估')
            print('       确保已通过 USB 连接目标设备')
        else:
            rknn.eval_perf()

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    rknn.release()

    # 打印模型文件大小
    model_size = os.path.getsize(args.output)
    print()
    print('=' * 50)
    print(f'转换完成!')
    print(f'  输入模型:   {args.onnx}')
    print(f'  输出模型:   {args.output}')
    print(f'  模型大小:   {model_size / 1024 / 1024:.2f} MB')
    print(f'  目标平台:   {args.platform}')
    print(f'  量化类型:   {"INT8" if do_quantization else "FP (未量化)"}')
    print(f'  输入尺寸:   {INPUT_SIZE}x{INPUT_SIZE}')
    print('=' * 50)

    return 0


if __name__ == '__main__':
    args = parse_args()
    ret = convert(args)
    sys.exit(ret)

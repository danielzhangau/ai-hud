#!/usr/bin/env python3
"""
convert_captures.py -- Convert on-device NV12 captures to labeled dataset.

Usage:
    # Pull captures from device
    adb pull /root/capture/ ~/captures/

    # Convert NV12 -> JPEG + generate YOLO label stubs
    python tools/convert_captures.py ~/captures/ --output ~/dataset_new/

    # With auto-split into train/val
    python tools/convert_captures.py ~/captures/ --output ~/dataset_new/ --split 0.8

The tool:
1. Reads .nv12 raw frames + .json metadata sidecars
2. Converts NV12 to JPEG images
3. Generates YOLO-format label stubs from NPU detection metadata
4. Optionally splits into train/val directories

Label stubs contain the NPU's detections as a starting point. They should
be reviewed and corrected in a labeling tool (CVAT, Label Studio, etc.)
before being used for training.
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:
    print("Required: pip install numpy Pillow")
    sys.exit(1)


def nv12_to_rgb(nv12_data, width, height):
    """Convert NV12 (YUV420SP) raw bytes to RGB numpy array."""
    y_size = width * height
    uv_size = y_size // 2

    if len(nv12_data) < y_size + uv_size:
        raise ValueError(
            f"NV12 data too small: {len(nv12_data)} < {y_size + uv_size}"
        )

    y = np.frombuffer(nv12_data[:y_size], dtype=np.uint8).reshape(height, width)
    uv = np.frombuffer(nv12_data[y_size:y_size + uv_size], dtype=np.uint8)
    uv = uv.reshape(height // 2, width)

    # Upsample UV to full resolution
    u = uv[:, 0::2].repeat(2, axis=0).repeat(2, axis=1).astype(np.int16)
    v = uv[:, 1::2].repeat(2, axis=0).repeat(2, axis=1).astype(np.int16)
    y = y.astype(np.int16)

    # YUV -> RGB (BT.601)
    r = np.clip(y + 1.402 * (v - 128), 0, 255).astype(np.uint8)
    g = np.clip(y - 0.344136 * (u - 128) - 0.714136 * (v - 128), 0, 255).astype(np.uint8)
    b = np.clip(y + 1.772 * (u - 128), 0, 255).astype(np.uint8)

    return np.stack([r, g, b], axis=2)


def bbox_to_yolo(box, img_w, img_h):
    """Convert [left, top, right, bottom] to YOLO format [cx, cy, w, h] normalized."""
    left, top, right, bottom = box
    cx = (left + right) / 2.0 / img_w
    cy = (top + bottom) / 2.0 / img_h
    w = (right - left) / img_w
    h = (bottom - top) / img_h
    return cx, cy, w, h


def convert_frame(nv12_path, json_path, output_dir, stem):
    """Convert a single NV12 frame + JSON metadata to JPEG + YOLO label."""
    # Read metadata
    with open(json_path, "r") as f:
        meta = json.load(f)

    width = meta["width"]
    height = meta["height"]

    # Convert NV12 -> JPEG
    with open(nv12_path, "rb") as f:
        nv12_data = f.read()

    rgb = nv12_to_rgb(nv12_data, width, height)
    img = Image.fromarray(rgb)

    img_dir = os.path.join(output_dir, "images")
    lbl_dir = os.path.join(output_dir, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)

    img_path = os.path.join(img_dir, f"{stem}.jpg")
    img.save(img_path, quality=90)

    # Generate YOLO label stub from detections
    lbl_path = os.path.join(lbl_dir, f"{stem}.txt")
    detections = meta.get("detections", [])
    with open(lbl_path, "w") as f:
        for det in detections:
            box = det.get("box", [0, 0, 0, 0])
            cx, cy, w, h = bbox_to_yolo(box, width, height)
            cls_id = det.get("class_id", 0)
            f.write(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

    return len(detections)


def main():
    parser = argparse.ArgumentParser(
        description="Convert on-device NV12 captures to labeled dataset"
    )
    parser.add_argument("input_dir", help="Directory with .nv12 + .json captures")
    parser.add_argument("--output", "-o", required=True,
                        help="Output dataset directory")
    parser.add_argument("--split", type=float, default=0,
                        help="Train ratio (e.g., 0.8 for 80/20 train/val split)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for split")
    args = parser.parse_args()

    input_dir = args.input_dir
    if not os.path.isdir(input_dir):
        print(f"Error: {input_dir} is not a directory")
        sys.exit(1)

    # Find all NV12 files with matching JSON sidecars
    nv12_files = sorted(Path(input_dir).glob("*.nv12"))
    pairs = []
    for nv12 in nv12_files:
        json_file = nv12.with_suffix(".json")
        if json_file.exists():
            pairs.append((str(nv12), str(json_file), nv12.stem))

    if not pairs:
        print(f"No .nv12 + .json pairs found in {input_dir}")
        sys.exit(1)

    print(f"Found {len(pairs)} capture pairs")

    # Determine output structure
    if args.split > 0:
        random.seed(args.seed)
        random.shuffle(pairs)
        split_idx = int(len(pairs) * args.split)
        train_pairs = pairs[:split_idx]
        val_pairs = pairs[split_idx:]

        print(f"Split: {len(train_pairs)} train, {len(val_pairs)} val")

        total_dets = 0
        for nv12_path, json_path, stem in train_pairs:
            n = convert_frame(nv12_path, json_path,
                              os.path.join(args.output, "train"), stem)
            total_dets += n

        for nv12_path, json_path, stem in val_pairs:
            n = convert_frame(nv12_path, json_path,
                              os.path.join(args.output, "val"), stem)
            total_dets += n
    else:
        total_dets = 0
        for nv12_path, json_path, stem in pairs:
            n = convert_frame(nv12_path, json_path, args.output, stem)
            total_dets += n

    print(f"\nConverted {len(pairs)} frames, {total_dets} detection stubs")
    print(f"Output: {args.output}")
    print()
    print("Next steps:")
    print("  1. Review label stubs in CVAT / Label Studio")
    print("  2. Correct bounding boxes and class labels")
    print("  3. Export as YOLO format")
    print("  4. Merge into training dataset and retrain")


if __name__ == "__main__":
    main()

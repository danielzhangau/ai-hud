#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chinese Speed Signs Dataset Preparation (TT100K)

Download and convert the Tsinghua-Tencent 100K (TT100K) dataset into a
YOLO-format dataset containing only the 8 speed-limit sign classes
relevant to the AI-HUD project.

Target classes (8):
  0: speed_sign_20   (TT100K: pl20)
  1: speed_sign_30   (TT100K: pl30)
  2: speed_sign_40   (TT100K: pl40)
  3: speed_sign_50   (TT100K: pl50)
  4: speed_sign_60   (TT100K: pl60)
  5: speed_sign_80   (TT100K: pl80)
  6: speed_sign_100  (TT100K: pl100)
  7: speed_sign_120  (TT100K: pl120)

Data source:
  TT100K -- http://cg.cs.tsinghua.edu.cn/traffic-sign/data_model_code/data.zip
  Format: JSON annotations + 2048x2048 JPG images

Requirements:
  pip install pyyaml tqdm

Usage:
  # Download + prepare (default):
  python cn_tt100k_prepare.py

  # Use existing data.zip or extracted directory:
  python cn_tt100k_prepare.py --data-zip /path/to/data.zip
  python cn_tt100k_prepare.py --data-dir /path/to/extracted/data

  # Custom output directory:
  python cn_tt100k_prepare.py --output /content/cn_speed_dataset
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# ============================================================
# Target class mapping: TT100K label -> (class_id, class_name)
# ============================================================

TT100K_LABEL_MAP = {
    "pl20":  0,
    "pl30":  1,
    "pl40":  2,
    "pl50":  3,
    "pl60":  4,
    "pl80":  5,
    "pl100": 6,
    "pl120": 7,
}

TARGET_CLASSES = [
    "speed_sign_20",   # 0
    "speed_sign_30",   # 1
    "speed_sign_40",   # 2
    "speed_sign_50",   # 3
    "speed_sign_60",   # 4
    "speed_sign_80",   # 5
    "speed_sign_100",  # 6
    "speed_sign_120",  # 7
]

# Classes with expected low instance counts -- warn the user
LOW_DATA_CLASSES = {"pl20", "pl120"}
LOW_DATA_THRESHOLD = 150


# ============================================================
# Download & extraction
# ============================================================

def download_tt100k(dest_dir):
    """Download TT100K data.zip from the official source."""
    url = "http://cg.cs.tsinghua.edu.cn/traffic-sign/data_model_code/data.zip"
    zip_path = os.path.join(dest_dir, "data.zip")

    if os.path.exists(zip_path):
        print(f"  data.zip already exists: {zip_path}")
        return zip_path

    print(f"  Downloading TT100K dataset...")
    print(f"  URL: {url}")
    print(f"  This may take a while (~1.7 GB)...")

    os.makedirs(dest_dir, exist_ok=True)

    # Try wget first (more reliable for large files), fall back to curl
    try:
        subprocess.run(
            ["wget", "-O", zip_path, "--progress=bar:force", url],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                ["curl", "-L", "-o", zip_path, "--progress-bar", url],
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("  [Error] Neither wget nor curl available.")
            print(f"  Please download manually: {url}")
            print(f"  Save to: {zip_path}")
            return None

    if os.path.exists(zip_path):
        size_mb = os.path.getsize(zip_path) / 1024 / 1024
        print(f"  Downloaded: {size_mb:.1f} MB")
        return zip_path

    return None


def extract_tt100k(zip_path, dest_dir):
    """Extract data.zip and return the path to the extracted data directory."""
    # After extraction, TT100K structure is: dest_dir/data/{train,test,other,annotations.json}
    data_dir = os.path.join(dest_dir, "data")

    if os.path.exists(os.path.join(data_dir, "annotations.json")):
        print(f"  Already extracted: {data_dir}")
        return data_dir

    print(f"  Extracting {zip_path}...")
    import zipfile
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

    if os.path.exists(os.path.join(data_dir, "annotations.json")):
        print(f"  Extracted to: {data_dir}")
        return data_dir

    # Some versions may extract directly into dest_dir
    if os.path.exists(os.path.join(dest_dir, "annotations.json")):
        print(f"  Extracted to: {dest_dir}")
        return dest_dir

    print("  [Error] Could not find annotations.json after extraction.")
    print(f"  Check contents of: {dest_dir}")
    return None


# ============================================================
# TT100K annotation parsing & YOLO conversion
# ============================================================

def parse_tt100k_annotations(data_dir):
    """
    Parse TT100K annotations.json and extract speed limit sign annotations.

    TT100K JSON structure:
      {
        "imgs": {
          "image_id": {
            "path": "train/12345.jpg",
            "objects": [
              {"category": "pl60", "bbox": {"xmin": x1, "ymin": y1, "xmax": x2, "ymax": y2}},
              ...
            ]
          },
          ...
        },
        "types": ["i1", "i2", ..., "pl20", "pl30", ...]
      }

    Returns:
      List of (image_id, image_path, split, annotations) tuples where
      annotations = [(class_id, x_center, y_center, width, height), ...]
      all in normalized coordinates.
    """
    ann_path = os.path.join(data_dir, "annotations.json")
    if not os.path.exists(ann_path):
        print(f"  [Error] annotations.json not found at: {ann_path}")
        return []

    print(f"  Loading annotations from: {ann_path}")
    with open(ann_path, "r") as f:
        data = json.load(f)

    imgs = data.get("imgs", {})
    print(f"  Total images in dataset: {len(imgs)}")

    # Show available speed-limit classes in the dataset
    all_types = data.get("types", [])
    speed_types = [t for t in all_types if t.startswith("pl")]
    print(f"  Speed limit classes in TT100K: {speed_types}")

    results = []
    class_stats = defaultdict(int)
    skipped_stats = defaultdict(int)

    for img_id, img_info in tqdm(imgs.items(), desc="  Parsing annotations"):
        objects = img_info.get("objects", [])
        if not objects:
            continue

        img_path = img_info.get("path", "")

        # Determine original split from path (train/ or test/)
        if img_path.startswith("train/"):
            orig_split = "train"
        elif img_path.startswith("test/"):
            orig_split = "test"
        else:
            orig_split = "other"

        # TT100K images are 2048x2048
        img_w, img_h = 2048, 2048

        annotations = []
        for obj in objects:
            category = obj.get("category", "")
            if category not in TT100K_LABEL_MAP:
                skipped_stats[category] += 1
                continue

            class_id = TT100K_LABEL_MAP[category]
            bbox = obj.get("bbox", {})
            xmin = bbox.get("xmin", 0)
            ymin = bbox.get("ymin", 0)
            xmax = bbox.get("xmax", 0)
            ymax = bbox.get("ymax", 0)

            # Validate bounding box
            if xmax <= xmin or ymax <= ymin:
                continue
            if xmin < 0 or ymin < 0 or xmax > img_w or ymax > img_h:
                # Clip to image bounds
                xmin = max(0, xmin)
                ymin = max(0, ymin)
                xmax = min(img_w, xmax)
                ymax = min(img_h, ymax)

            # Convert to YOLO format (normalized)
            x_center = ((xmin + xmax) / 2.0) / img_w
            y_center = ((ymin + ymax) / 2.0) / img_h
            width = (xmax - xmin) / img_w
            height = (ymax - ymin) / img_h

            annotations.append((class_id, x_center, y_center, width, height))
            class_stats[category] += 1

        if annotations:
            results.append((img_id, img_path, orig_split, annotations))

    print(f"\n  Images with target annotations: {len(results)}")
    print(f"  Annotation counts by TT100K label:")
    for label in sorted(TT100K_LABEL_MAP.keys()):
        count = class_stats.get(label, 0)
        target_name = TARGET_CLASSES[TT100K_LABEL_MAP[label]]
        warn = " [!!! LOW DATA]" if label in LOW_DATA_CLASSES and count < LOW_DATA_THRESHOLD else ""
        print(f"    {label:>6s} -> {target_name:<18s}: {count:>5d}{warn}")

    return results


def create_yolo_dataset(results, data_dir, output_dir, val_ratio=0.2, seed=42):
    """
    Create a YOLO-format dataset from parsed TT100K annotations.

    Splits the data 80/20 into train/val sets.
    Uses only the TT100K 'train' split images (test split has no annotations).
    """
    random.seed(seed)

    # Create output directories
    for split in ["train", "val"]:
        os.makedirs(os.path.join(output_dir, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, split, "labels"), exist_ok=True)

    # Filter to only annotated images (TT100K test split has no annotations)
    # In practice, all results should already have annotations
    annotated = [r for r in results if r[3]]

    # Shuffle and split
    random.shuffle(annotated)
    n_val = int(len(annotated) * val_ratio)
    val_set = annotated[:n_val]
    train_set = annotated[n_val:]

    split_data = {"train": train_set, "val": val_set}
    split_stats = {}

    for split_name, split_items in split_data.items():
        class_counts = defaultdict(int)
        copied = 0

        for img_id, img_path, orig_split, annotations in tqdm(
            split_items, desc=f"  Creating {split_name}"
        ):
            # Resolve source image path
            src_img = os.path.join(data_dir, img_path)
            if not os.path.exists(src_img):
                # Try alternative paths
                for alt in [
                    os.path.join(data_dir, "train", f"{img_id}.jpg"),
                    os.path.join(data_dir, "test", f"{img_id}.jpg"),
                    os.path.join(data_dir, "other", f"{img_id}.jpg"),
                ]:
                    if os.path.exists(alt):
                        src_img = alt
                        break
                else:
                    continue

            # Copy image
            dst_img = os.path.join(output_dir, split_name, "images", f"{img_id}.jpg")
            shutil.copy2(src_img, dst_img)

            # Write YOLO label file
            dst_label = os.path.join(output_dir, split_name, "labels", f"{img_id}.txt")
            with open(dst_label, "w") as f:
                for cls_id, cx, cy, w, h in annotations:
                    f.write(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
                    class_counts[cls_id] += 1

            copied += 1

        split_stats[split_name] = {
            "images": copied,
            "class_counts": dict(class_counts),
        }

    return split_stats


# ============================================================
# YAML generation
# ============================================================

def create_dataset_yaml(output_dir, yaml_path=None):
    """Create the YOLOv5 dataset configuration YAML file."""
    import yaml

    if yaml_path is None:
        yaml_path = os.path.join(output_dir, "data.yaml")

    abs_output = os.path.abspath(output_dir)
    data = {
        "train": os.path.join(abs_output, "train", "images"),
        "val": os.path.join(abs_output, "val", "images"),
        "nc": len(TARGET_CLASSES),
        "names": TARGET_CLASSES,
    }

    with open(yaml_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    print(f"\n  Dataset YAML written to: {yaml_path}")
    return yaml_path


# ============================================================
# Statistics & reporting
# ============================================================

def print_dataset_stats(split_stats):
    """Print detailed dataset statistics with warnings for low-data classes."""
    print("\n" + "=" * 65)
    print("  CN Speed Signs Dataset Statistics (TT100K)")
    print("=" * 65)

    # Aggregate class counts across splits
    total_counts = defaultdict(int)
    for split_name, stats in split_stats.items():
        for cls_id, count in stats.get("class_counts", {}).items():
            total_counts[cls_id] += count

    total_annotations = sum(total_counts.values())

    # Per-class stats
    print(f"\n  {'Class':<5s} {'Name':<20s} {'Train':>7s} {'Val':>7s} {'Total':>7s} {'%':>6s}  Distribution")
    print("  " + "-" * 75)
    for cls_id in range(len(TARGET_CLASSES)):
        name = TARGET_CLASSES[cls_id]
        train_count = split_stats.get("train", {}).get("class_counts", {}).get(cls_id, 0)
        val_count = split_stats.get("val", {}).get("class_counts", {}).get(cls_id, 0)
        total = train_count + val_count
        pct = (total / total_annotations * 100) if total_annotations > 0 else 0
        bar = "#" * min(total // 10, 30)
        print(f"  {cls_id:<5d} {name:<20s} {train_count:>7d} {val_count:>7d} {total:>7d} {pct:>5.1f}%  {bar}")

    print("  " + "-" * 75)
    total_train = sum(split_stats.get("train", {}).get("class_counts", {}).values())
    total_val = sum(split_stats.get("val", {}).get("class_counts", {}).values())
    print(f"  {'':5s} {'TOTAL':<20s} {total_train:>7d} {total_val:>7d} {total_annotations:>7d}")

    # Image counts per split
    print(f"\n  Split summary:")
    for split_name, stats in split_stats.items():
        print(f"    {split_name}: {stats['images']} images")

    # Warnings for low-data classes
    low_data = [
        (cls_id, TARGET_CLASSES[cls_id], total_counts.get(cls_id, 0))
        for cls_id in range(len(TARGET_CLASSES))
        if total_counts.get(cls_id, 0) < LOW_DATA_THRESHOLD
    ]
    if low_data:
        print(f"\n  [Warning] Low-data classes (< {LOW_DATA_THRESHOLD} annotations):")
        for cls_id, name, count in low_data:
            print(f"    - {name} (class {cls_id}): {count} annotations")
        print("    Consider data augmentation or additional data collection for these classes.")
        print("    Training with heavy augmentation (mosaic, mixup, hsv) is recommended.")

    print("=" * 65)


# ============================================================
# Main
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare Chinese speed signs dataset from TT100K for YOLOv5")

    parser.add_argument("--data-zip", type=str, default=None,
                        help="Path to existing TT100K data.zip (skip download)")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="Path to already-extracted TT100K data directory "
                             "(contains annotations.json)")
    parser.add_argument("--output", type=str, default="cn_speed_dataset",
                        help="Output dataset directory (default: cn_speed_dataset)")
    parser.add_argument("--downloads", type=str, default="downloads",
                        help="Directory for downloaded files (default: downloads)")
    parser.add_argument("--val-ratio", type=float, default=0.2,
                        help="Validation set ratio (default: 0.2)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--clean", action="store_true",
                        help="Remove existing output directory before starting")
    parser.add_argument("--yaml-output", type=str, default=None,
                        help="Path for the output YAML config file "
                             "(default: <output>/data.yaml)")

    return parser.parse_args()


def main():
    args = parse_args()

    output_dir = os.path.abspath(args.output)
    downloads_dir = os.path.abspath(args.downloads)

    print("=" * 65)
    print("  CN Speed Signs Dataset Preparation (TT100K)")
    print("=" * 65)
    print(f"  Output:    {output_dir}")
    print(f"  Val ratio: {args.val_ratio}")
    print(f"  Seed:      {args.seed}")
    print(f"  Classes:   {len(TARGET_CLASSES)}")
    for i, name in enumerate(TARGET_CLASSES):
        tt_label = [k for k, v in TT100K_LABEL_MAP.items() if v == i][0]
        print(f"    {i}: {name} (TT100K: {tt_label})")

    # Clean output if requested
    if args.clean and os.path.exists(output_dir):
        print(f"\n  Removing existing output: {output_dir}")
        shutil.rmtree(output_dir)

    # ------------------------------------------------------------------
    # Step 1: Get TT100K data
    # ------------------------------------------------------------------
    data_dir = None

    if args.data_dir:
        # Use already-extracted directory
        data_dir = os.path.abspath(args.data_dir)
        if not os.path.exists(os.path.join(data_dir, "annotations.json")):
            print(f"\n  [Error] annotations.json not found in: {data_dir}")
            sys.exit(1)
        print(f"\n  Using existing data directory: {data_dir}")

    elif args.data_zip:
        # Extract from provided zip
        zip_path = os.path.abspath(args.data_zip)
        if not os.path.exists(zip_path):
            print(f"\n  [Error] Zip file not found: {zip_path}")
            sys.exit(1)
        print(f"\n  Using existing zip: {zip_path}")
        data_dir = extract_tt100k(zip_path, downloads_dir)

    else:
        # Download and extract
        print(f"\n  Downloads directory: {downloads_dir}")
        zip_path = download_tt100k(downloads_dir)
        if zip_path is None:
            sys.exit(1)
        data_dir = extract_tt100k(zip_path, downloads_dir)

    if data_dir is None:
        print("\n  [Error] Failed to get TT100K data. Exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: Parse annotations and filter speed limit signs
    # ------------------------------------------------------------------
    print("\n  Step 2: Parsing TT100K annotations...")
    results = parse_tt100k_annotations(data_dir)

    if not results:
        print("\n  [Error] No speed limit annotations found. Exiting.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 3: Create YOLO-format dataset
    # ------------------------------------------------------------------
    print(f"\n  Step 3: Creating YOLO dataset at {output_dir}...")
    split_stats = create_yolo_dataset(
        results, data_dir, output_dir,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )

    # ------------------------------------------------------------------
    # Step 4: Generate dataset YAML
    # ------------------------------------------------------------------
    print("\n  Step 4: Generating dataset config...")
    create_dataset_yaml(output_dir, yaml_path=args.yaml_output)

    # ------------------------------------------------------------------
    # Step 5: Print statistics
    # ------------------------------------------------------------------
    print_dataset_stats(split_stats)

    # Final instructions
    total_images = sum(s["images"] for s in split_stats.values())
    print(f"\n  Dataset ready: {total_images} images")
    print(f"  Config: {os.path.join(output_dir, 'data.yaml')}")
    print("\n  Next steps:")
    print("    1. Review class distribution above")
    print("    2. Upload to Colab and run train_cn_colab.ipynb")
    print("    3. Or train locally:")
    print(f"       python train.py --data {os.path.join(output_dir, 'data.yaml')} \\")
    print("           --cfg yolov5n.yaml --img 480 --batch 32 --epochs 150")


if __name__ == "__main__":
    main()

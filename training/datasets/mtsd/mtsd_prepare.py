#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MTSD Speed Limit Dataset Preparation

Extract speed limit sign images from Mapillary Traffic Sign Dataset (MTSD)
and convert to YOLOv5 format for training.

Key features:
  - Only extracts images containing speed limit signs (not all 52K images)
  - Filters by appearance group (g1/g3 for AU, g1 for CN)
  - Extracts directly from zip files (no need to unzip 42G first)
  - Outputs standard YOLOv5 dataset structure

Usage:
  # Universal model (g1 + g3, 11 speed classes covering AU + CN)
  python mtsd_prepare.py --region universal --output /path/to/mtsd_universal_dataset

Requirements:
  pip install tqdm pyyaml
"""

import argparse
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# ============================================================
# Region-specific class mappings
# ============================================================

# Universal model: 11 classes covering both AU and CN speed limits
# Matches postprocess.c OBJ_CLASS_NUM=11 and hud_ipc.h SIGN_SPEEDS[]
UNIVERSAL_CLASSES = {
    0: "speed_sign_20",
    1: "speed_sign_30",
    2: "speed_sign_40",
    3: "speed_sign_50",
    4: "speed_sign_60",
    5: "speed_sign_70",
    6: "speed_sign_80",
    7: "speed_sign_90",
    8: "speed_sign_100",
    9: "speed_sign_110",
    10: "speed_sign_120",
}

# MTSD label -> speed value extraction
# Labels: "regulatory--maximum-speed-limit-{value}--{group}"
def parse_speed_value(label):
    """Extract speed value from MTSD label. Returns (value, group) or (None, None)."""
    if "maximum-speed-limit" not in label:
        return None, None
    # Skip "end-of-*", "led-*", "complementary--*", "truck-*"
    if "end-of" in label or "led-" in label or "truck-" in label:
        return None, None
    if label.startswith("complementary--"):
        return None, None

    parts = label.split("--")
    if len(parts) < 3:
        return None, None

    # Extract speed value: "maximum-speed-limit-60" -> 60
    sign_part = parts[1]  # e.g. "maximum-speed-limit-60"
    group = parts[2]      # e.g. "g1"

    try:
        value = int(sign_part.split("-")[-1])
        return value, group
    except (ValueError, IndexError):
        return None, None


def build_label_map(region):
    """Build mapping: (speed_value, group) -> target_class_id."""
    if region == "universal":
        # Universal model: all Vienna Convention style signs (g1 + g3)
        # Covers both AU and CN speed limits in one model
        classes = UNIVERSAL_CLASSES
        allowed_groups = {"g1", "g3"}
        speed_to_cls = {
            20: 0, 30: 1, 40: 2, 50: 3, 60: 4,
            70: 5, 80: 6, 90: 7, 100: 8, 110: 9, 120: 10,
        }
    else:
        raise ValueError(f"Unknown region: {region}. Use 'universal'.")

    return classes, allowed_groups, speed_to_cls


# ============================================================
# Annotation scanning
# ============================================================

def scan_annotations(ann_dir, allowed_groups, speed_to_cls):
    """
    Scan all MTSD annotations and collect speed limit sign info.

    Returns:
        image_annotations: dict of image_key -> list of (class_id, bbox_dict)
        stats: Counter of class_id -> annotation count
    """
    image_annotations = {}
    stats = defaultdict(int)
    skipped_groups = defaultdict(int)
    skipped_values = defaultdict(int)

    json_files = [f for f in os.listdir(ann_dir) if f.endswith(".json")]
    print(f"  Scanning {len(json_files)} annotation files...")

    for fname in tqdm(json_files, desc="  Scanning"):
        filepath = os.path.join(ann_dir, fname)
        with open(filepath) as f:
            data = json.load(f)

        # Skip panorama images (equirectangular projection, unusual aspect ratio)
        if data.get("ispano", False):
            continue

        img_w = data["width"]
        img_h = data["height"]
        image_key = fname[:-5]  # remove .json
        annotations = []

        for obj in data.get("objects", []):
            label = obj["label"]
            speed_val, group = parse_speed_value(label)

            if speed_val is None:
                continue

            # Filter by appearance group
            if allowed_groups is not None and group not in allowed_groups:
                skipped_groups[group] += 1
                continue

            # Filter by target speed values
            if speed_val not in speed_to_cls:
                skipped_values[speed_val] += 1
                continue

            cls_id = speed_to_cls[speed_val]
            bbox = obj["bbox"]

            # Skip cross-boundary bboxes (xmin > xmax, panorama edge wrapping)
            if bbox["xmin"] > bbox["xmax"]:
                continue

            # Skip occluded or out-of-frame signs
            props = obj.get("properties", {})
            if props.get("occluded", False) or props.get("out-of-frame", False):
                continue

            # Convert to YOLO format: normalized cx, cy, w, h
            bw = bbox["xmax"] - bbox["xmin"]
            bh = bbox["ymax"] - bbox["ymin"]
            cx = (bbox["xmin"] + bw / 2) / img_w
            cy = (bbox["ymin"] + bh / 2) / img_h
            nw = bw / img_w
            nh = bh / img_h

            # Sanity check: skip degenerate boxes
            if nw < 0.002 or nh < 0.002 or nw > 0.9 or nh > 0.9:
                continue

            annotations.append((cls_id, cx, cy, nw, nh))
            stats[cls_id] += 1

        if annotations:
            image_annotations[image_key] = annotations

    # Print skip summary
    if skipped_groups:
        print(f"  Skipped appearance groups: {dict(skipped_groups)}")
    if skipped_values:
        print(f"  Skipped speed values: {dict(skipped_values)}")

    return image_annotations, stats


# ============================================================
# Image extraction from zip
# ============================================================

def find_image_zips(mtsd_dir):
    """Find all MTSD image zip files in the directory."""
    zips = []
    for f in sorted(os.listdir(mtsd_dir)):
        if f.startswith("mtsd_fully_annotated_images.") and f.endswith(".zip"):
            zips.append(os.path.join(mtsd_dir, f))
    return zips


def build_zip_index(zip_paths):
    """
    Build an index: image_key -> (zip_path, entry_name).
    This avoids searching all zips for each image.
    """
    print(f"  Indexing {len(zip_paths)} zip files...")
    index = {}
    for zp in tqdm(zip_paths, desc="  Indexing zips"):
        with zipfile.ZipFile(zp, "r") as zf:
            for entry in zf.namelist():
                if entry.startswith("images/") and entry.endswith(".jpg"):
                    key = Path(entry).stem  # "images/abc.jpg" -> "abc"
                    index[key] = (zp, entry)
    print(f"  Indexed {len(index)} images across {len(zip_paths)} zips")
    return index


def extract_images(image_keys, zip_index, output_img_dir):
    """Extract specific images from zip files to output directory."""
    os.makedirs(output_img_dir, exist_ok=True)

    # Group by zip file for efficient extraction
    by_zip = defaultdict(list)
    missing = []
    for key in image_keys:
        if key in zip_index:
            zp, entry = zip_index[key]
            by_zip[zp].append((key, entry))
        else:
            missing.append(key)

    if missing:
        print(f"  [Warning] {len(missing)} images not found in any zip")

    extracted = 0
    for zp, entries in tqdm(by_zip.items(), desc="  Extracting"):
        with zipfile.ZipFile(zp, "r") as zf:
            for key, entry in entries:
                data = zf.read(entry)
                out_path = os.path.join(output_img_dir, f"{key}.jpg")
                with open(out_path, "wb") as f:
                    f.write(data)
                extracted += 1

    return extracted, len(missing)


# ============================================================
# Dataset output
# ============================================================

def write_labels(image_annotations, output_lbl_dir):
    """Write YOLO-format label files."""
    os.makedirs(output_lbl_dir, exist_ok=True)
    for key, anns in image_annotations.items():
        label_path = os.path.join(output_lbl_dir, f"{key}.txt")
        with open(label_path, "w") as f:
            for cls_id, cx, cy, w, h in anns:
                f.write(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")


def write_data_yaml(output_dir, classes):
    """Write YOLOv5 data.yaml config."""
    import yaml

    data = {
        "train": os.path.join(os.path.abspath(output_dir), "train", "images"),
        "val": os.path.join(os.path.abspath(output_dir), "val", "images"),
        "nc": len(classes),
        "names": list(classes.values()),
    }
    yaml_path = os.path.join(output_dir, "data.yaml")
    with open(yaml_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return yaml_path


def print_stats(stats, classes):
    """Print class distribution."""
    print("\n" + "=" * 55)
    print("  Class Distribution")
    print("=" * 55)
    total = sum(stats.values())
    for cls_id in sorted(classes.keys()):
        name = classes[cls_id]
        count = stats.get(cls_id, 0)
        pct = (count / total * 100) if total > 0 else 0
        bar = "#" * min(count // 10, 40)
        print(f"  {cls_id}: {name:<20s} {count:>5d} ({pct:>5.1f}%) {bar}")
    print(f"  {'TOTAL':<23s} {total:>5d}")
    print("=" * 55)


# ============================================================
# Main
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare MTSD speed limit dataset for YOLOv5 training")

    parser.add_argument("--region", type=str, default="universal",
                        choices=["universal"],
                        help="Target region: universal (11-class, AU+CN)")
    parser.add_argument("--output", type=str, required=True,
                        help="Output dataset directory")
    parser.add_argument("--mtsd-dir", type=str, default=None,
                        help="MTSD directory (default: same dir as this script)")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Skip image extraction (labels only, for testing)")
    parser.add_argument("--val-ratio", type=float, default=None,
                        help="Custom val ratio (default: use MTSD official splits)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for custom split")

    return parser.parse_args()


def main():
    args = parse_args()

    # Resolve MTSD directory
    if args.mtsd_dir:
        mtsd_dir = os.path.abspath(args.mtsd_dir)
    else:
        mtsd_dir = os.path.dirname(os.path.abspath(__file__))

    ann_base = os.path.join(mtsd_dir, "annotations", "mtsd_v2_fully_annotated")
    ann_dir = os.path.join(ann_base, "annotations")
    splits_dir = os.path.join(ann_base, "splits")

    if not os.path.isdir(ann_dir):
        print(f"[Error] Annotations not found: {ann_dir}")
        print("  Expected structure: mtsd/annotations/mtsd_v2_fully_annotated/annotations/*.json")
        sys.exit(1)

    output_dir = os.path.abspath(args.output)
    region = args.region

    print("=" * 55)
    print(f"  MTSD Speed Limit Dataset Preparation ({region.upper()})")
    print("=" * 55)
    print(f"  MTSD dir:  {mtsd_dir}")
    print(f"  Output:    {output_dir}")
    print(f"  Region:    {region}")

    # Build class mapping
    classes, allowed_groups, speed_to_cls = build_label_map(region)
    print(f"  Classes:   {len(classes)}")
    print(f"  Groups:    {allowed_groups or 'all'}")
    print(f"  Speeds:    {sorted(speed_to_cls.keys())}")

    # Step 1: Scan annotations
    print(f"\n[1/4] Scanning annotations...")
    image_annotations, stats = scan_annotations(ann_dir, allowed_groups, speed_to_cls)
    print(f"  Found {len(image_annotations)} images with speed limit signs")
    print_stats(stats, classes)

    if not image_annotations:
        print("\n[Error] No matching annotations found. Check region/group settings.")
        sys.exit(1)

    # Step 2: Split into train/val using MTSD official splits
    print(f"\n[2/4] Splitting dataset...")

    if args.val_ratio is not None:
        # Custom split
        import random
        random.seed(args.seed)
        all_keys = list(image_annotations.keys())
        random.shuffle(all_keys)
        n_val = int(len(all_keys) * args.val_ratio)
        val_keys = set(all_keys[:n_val])
        train_keys = set(all_keys[n_val:])
        print(f"  Custom split: {args.val_ratio:.0%} val")
    else:
        # Use MTSD official splits
        train_keys_all = set()
        val_keys_all = set()
        for split_file, key_set in [("train.txt", train_keys_all), ("val.txt", val_keys_all)]:
            split_path = os.path.join(splits_dir, split_file)
            if os.path.exists(split_path):
                with open(split_path) as f:
                    for line in f:
                        key_set.add(line.strip())

        # Intersect with our filtered images
        train_keys = set(image_annotations.keys()) & train_keys_all
        val_keys = set(image_annotations.keys()) & val_keys_all

        # Images in test split -> add to train (maximize training data)
        remaining = set(image_annotations.keys()) - train_keys - val_keys
        if remaining:
            train_keys |= remaining
            print(f"  {len(remaining)} test-split images added to train")

    print(f"  Train: {len(train_keys)} images")
    print(f"  Val:   {len(val_keys)} images")

    # Step 3: Write labels
    print(f"\n[3/4] Writing YOLO labels...")
    for split, keys in [("train", train_keys), ("val", val_keys)]:
        split_anns = {k: image_annotations[k] for k in keys}
        lbl_dir = os.path.join(output_dir, split, "labels")
        write_labels(split_anns, lbl_dir)
        print(f"  {split}: {len(split_anns)} label files written")

    # Step 4: Extract images from zips
    if args.skip_extract:
        print(f"\n[4/4] Skipping image extraction (--skip-extract)")
    else:
        print(f"\n[4/4] Extracting images from zip files...")
        zip_paths = find_image_zips(mtsd_dir)
        if not zip_paths:
            print(f"  [Error] No image zip files found in {mtsd_dir}")
            print("  Expected: mtsd_fully_annotated_images.*.zip")
            print("  Run with --skip-extract to generate labels only.")
            sys.exit(1)

        zip_index = build_zip_index(zip_paths)

        for split, keys in [("train", train_keys), ("val", val_keys)]:
            img_dir = os.path.join(output_dir, split, "images")
            extracted, missing = extract_images(keys, zip_index, img_dir)
            print(f"  {split}: {extracted} images extracted, {missing} missing")

    # Write data.yaml
    yaml_path = write_data_yaml(output_dir, classes)

    # Summary
    print("\n" + "=" * 55)
    print("  Dataset Ready")
    print("=" * 55)
    print(f"  Output:     {output_dir}")
    print(f"  Config:     {yaml_path}")
    print(f"  Train:      {len(train_keys)} images")
    print(f"  Val:        {len(val_keys)} images")
    print(f"  Classes:    {len(classes)}")
    print(f"  Region:     {region.upper()}")
    print(f"\n  Next steps:")
    print(f"    1. Tar for Colab upload:")
    print(f"       cd {os.path.dirname(output_dir)}")
    print(f"       tar -czf mtsd_{region}_dataset.tar.gz {os.path.basename(output_dir)}/")
    print(f"    2. Train in Colab or locally:")
    print(f"       python train.py --data {yaml_path} \\")
    print(f"           --cfg yolov5n.yaml --weights yolov5n.pt --img 640 --epochs 300")


if __name__ == "__main__":
    main()

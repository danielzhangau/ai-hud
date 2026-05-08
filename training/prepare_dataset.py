#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Australian Speed Signs Dataset Preparation

Merge multiple public datasets into a unified 9-class YOLOv5 dataset:
  0: speed_sign_30    4: speed_sign_70    8: speed_camera
  1: speed_sign_40    5: speed_sign_80
  2: speed_sign_50    6: speed_sign_100
  3: speed_sign_60    7: speed_sign_110

Data sources:
  1. Roboflow ELEC5308 "Australia Traffic Sign" (primary)
  2. GTSDB German Traffic Sign Detection Benchmark (supplement)
  3. Roboflow speed camera datasets (supplement)
  4. Local custom images (optional, user-provided)

Requirements:
  pip install roboflow opencv-python tqdm pyyaml

Usage:
  python prepare_dataset.py --roboflow-key YOUR_API_KEY
  python prepare_dataset.py --roboflow-key YOUR_API_KEY --skip-gtsdb
  python prepare_dataset.py --local-dir /path/to/custom/images
"""

import argparse
import os
import shutil
import random
import csv
from pathlib import Path
from collections import defaultdict

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# ============================================================
# Target class mapping
# ============================================================

TARGET_CLASSES = {
    0: "speed_sign_30",
    1: "speed_sign_40",
    2: "speed_sign_50",
    3: "speed_sign_60",
    4: "speed_sign_70",
    5: "speed_sign_80",
    6: "speed_sign_100",
    7: "speed_sign_110",
    8: "speed_camera",
}

# Reverse lookup: name -> target class id
TARGET_NAME_TO_ID = {v: k for k, v in TARGET_CLASSES.items()}

# ============================================================
# Source dataset class mappings
# ============================================================

# Roboflow ELEC5308 "Australia Traffic Sign" dataset
# Map source class names to our target class IDs
# (class names may vary by dataset version -- handle common variants)
ROBOFLOW_AU_CLASS_MAP = {
    # Exact matches (common naming patterns on Roboflow)
    "30 km/h Speed Limit": 0,
    "30 Speed Limit": 0,
    "30km": 0,
    "speed_30": 0,
    "Speed Limit 30": 0,
    "speed-limit-30": 0,
    "30": 0,

    "40 km/h Speed Limit": 1,
    "40 Speed Limit": 1,
    "40km": 1,
    "speed_40": 1,
    "Speed Limit 40": 1,
    "speed-limit-40": 1,
    "40": 1,

    "50 km/h Speed Limit": 2,
    "50 Speed Limit": 2,
    "50km": 2,
    "speed_50": 2,
    "Speed Limit 50": 2,
    "speed-limit-50": 2,
    "50": 2,

    "60 km/h Speed Limit": 3,
    "60 Speed Limit": 3,
    "60km": 3,
    "speed_60": 3,
    "Speed Limit 60": 3,
    "speed-limit-60": 3,
    "60": 3,

    "70 km/h Speed Limit": 4,
    "70 Speed Limit": 4,
    "70km": 4,
    "speed_70": 4,
    "Speed Limit 70": 4,
    "speed-limit-70": 4,
    "70": 4,

    "80 km/h Speed Limit": 5,
    "80 Speed Limit": 5,
    "80km": 5,
    "speed_80": 5,
    "Speed Limit 80": 5,
    "speed-limit-80": 5,
    "80": 5,

    "100 km/h Speed Limit": 6,
    "100 Speed Limit": 6,
    "100km": 6,
    "speed_100": 6,
    "Speed Limit 100": 6,
    "speed-limit-100": 6,
    "100": 6,

    "110 km/h Speed Limit": 7,
    "110 Speed Limit": 7,
    "110km": 7,
    "speed_110": 7,
    "Speed Limit 110": 7,
    "speed-limit-110": 7,
    "110": 7,

    # Speed camera variants
    "Speed-camera": 8,
    "Speed camera": 8,
    "speed_camera": 8,
    "speed-camera": 8,
    "SpeedCamera": 8,
    "Camera operation zone": 8,
}

# GTSDB class ID -> our target class ID
# GTSDB speed limit class IDs: 0=20, 1=30, 2=50, 3=60, 4=70, 5=80, 7=100, 8=120
# We skip 20 km/h (not common in AU) and 120 km/h (AU uses 110)
GTSDB_CLASS_MAP = {
    1: 0,   # GTSDB "30" -> speed_sign_30
    # GTSDB has no 40 km/h class
    2: 2,   # GTSDB "50" -> speed_sign_50
    3: 3,   # GTSDB "60" -> speed_sign_60
    4: 4,   # GTSDB "70" -> speed_sign_70
    5: 5,   # GTSDB "80" -> speed_sign_80
    7: 6,   # GTSDB "100" -> speed_sign_100
    # GTSDB "120" (id=8) -> skip (AU uses 110, not 120)
}


# ============================================================
# Roboflow download helpers
# ============================================================

def download_roboflow_dataset(api_key, workspace, project, version,
                              fmt="yolov5", dest_dir="downloads"):
    """Download a dataset from Roboflow Universe."""
    try:
        from roboflow import Roboflow
    except ImportError:
        print("[Error] roboflow package not installed. Run: pip install roboflow")
        return None

    rf = Roboflow(api_key=api_key)
    proj = rf.workspace(workspace).project(project)
    dataset = proj.version(version).download(fmt, location=os.path.join(dest_dir, project))
    return dataset.location


def download_au_traffic_signs(api_key, dest_dir="downloads"):
    """Download ELEC5308 Australia Traffic Sign dataset."""
    print("\n[1/3] Downloading Australia Traffic Sign dataset (ELEC5308)...")
    path = download_roboflow_dataset(
        api_key=api_key,
        workspace="elec5308-w8jl5",
        project="australia-traffic-sign",
        version=1,
        dest_dir=dest_dir,
    )
    if path:
        print(f"      Downloaded to: {path}")
    return path


def download_speed_camera_dataset(api_key, dest_dir="downloads"):
    """Download a dataset with speed camera annotations."""
    print("\n[2/3] Downloading speed camera dataset...")
    # Try JB's traffic sign dataset which has Speed-camera class
    # Fallback: camera detection dataset
    datasets_to_try = [
        ("custom-yolo-idfiy", "speed-signs-detection", 1),
    ]
    for ws, proj, ver in datasets_to_try:
        try:
            path = download_roboflow_dataset(
                api_key=api_key,
                workspace=ws,
                project=proj,
                version=ver,
                dest_dir=dest_dir,
            )
            if path:
                print(f"      Downloaded to: {path}")
                return path
        except Exception as e:
            print(f"      Failed to download {proj}: {e}")
            continue
    print("      [Warning] No speed camera dataset downloaded.")
    print("      You can add custom speed camera images later.")
    return None


# ============================================================
# GTSDB download & conversion
# ============================================================

def download_gtsdb(dest_dir="downloads"):
    """Download GTSDB from official source or Roboflow."""
    gtsdb_dir = os.path.join(dest_dir, "gtsdb")
    if os.path.exists(gtsdb_dir):
        print(f"\n[3/3] GTSDB already exists at: {gtsdb_dir}")
        return gtsdb_dir

    print("\n[3/3] Downloading GTSDB...")
    print("      Attempting Roboflow download...")

    # Try Roboflow version (already in YOLO format)
    try:
        from roboflow import Roboflow
        # Use a public GTSDB dataset on Roboflow (no API key needed for public)
        rf = Roboflow(api_key="a]")  # placeholder, Roboflow may allow public
        proj = rf.workspace("mohamed-traore-2ekkp").project(
            "gtsdb---german-traffic-sign-detection-benchmark"
        )
        dataset = proj.version(1).download("yolov5", location=gtsdb_dir)
        print(f"      Downloaded to: {dataset.location}")
        return dataset.location
    except Exception as e:
        print(f"      Roboflow download failed: {e}")

    # Fallback: manual download instructions
    print("\n      [Manual download required]")
    print("      Option A: Roboflow (YOLO format)")
    print("        https://universe.roboflow.com/mohamed-traore-2ekkp/"
          "gtsdb---german-traffic-sign-detection-benchmark")
    print("      Option B: Kaggle")
    print("        https://www.kaggle.com/datasets/safabouguezzi/"
          "german-traffic-sign-detection-benchmark-gtsdb")
    print("      Option C: Official")
    print("        https://benchmark.ini.rub.de/gtsdb_dataset.html")
    print(f"\n      Download and extract to: {gtsdb_dir}/")
    return None


def convert_gtsdb_csv_to_yolo(gtsdb_dir, output_dir):
    """
    Convert GTSDB CSV annotations to YOLO format.
    Only for the official GTSDB format (gt.txt CSV).
    Skip if data is already in YOLO format (from Roboflow).
    """
    gt_file = os.path.join(gtsdb_dir, "gt.txt")
    if not os.path.exists(gt_file):
        # Probably already in YOLO format (Roboflow download)
        return False

    print("      Converting GTSDB CSV -> YOLO format...")
    os.makedirs(os.path.join(output_dir, "images"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "labels"), exist_ok=True)

    # Parse gt.txt: filename;x1;y1;x2;y2;class_id
    annotations = defaultdict(list)
    with open(gt_file, "r") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) < 6:
                continue
            filename = row[0]
            x1, y1, x2, y2 = int(row[1]), int(row[2]), int(row[3]), int(row[4])
            class_id = int(row[5])
            annotations[filename].append((x1, y1, x2, y2, class_id))

    converted = 0
    for filename, boxes in annotations.items():
        img_path = os.path.join(gtsdb_dir, filename)
        if not os.path.exists(img_path):
            continue

        # Get image dimensions
        if HAS_CV2:
            img = cv2.imread(img_path)
            if img is None:
                continue
            h, w = img.shape[:2]
        else:
            # Fallback: assume 1360x800 (GTSDB standard size)
            w, h = 1360, 800

        yolo_lines = []
        for x1, y1, x2, y2, cls_id in boxes:
            if cls_id not in GTSDB_CLASS_MAP:
                continue
            target_id = GTSDB_CLASS_MAP[cls_id]
            # Convert to YOLO format: class cx cy w h (normalized)
            cx = ((x1 + x2) / 2.0) / w
            cy = ((y1 + y2) / 2.0) / h
            bw = (x2 - x1) / w
            bh = (y2 - y1) / h
            yolo_lines.append(f"{target_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")

        if yolo_lines:
            # Copy image
            dst_img = os.path.join(output_dir, "images", filename)
            shutil.copy2(img_path, dst_img)
            # Write label
            label_name = Path(filename).stem + ".txt"
            dst_label = os.path.join(output_dir, "labels", label_name)
            with open(dst_label, "w") as f:
                f.write("\n".join(yolo_lines) + "\n")
            converted += 1

    print(f"      Converted {converted} images with speed limit signs")
    return True


# ============================================================
# Dataset merging
# ============================================================

def read_yolo_dataset_yaml(dataset_dir):
    """Read data.yaml from a YOLO dataset directory."""
    import yaml
    yaml_path = os.path.join(dataset_dir, "data.yaml")
    if not os.path.exists(yaml_path):
        return None
    with open(yaml_path, "r") as f:
        return yaml.safe_load(f)


def build_class_map_from_yaml(yaml_data, source_map):
    """
    Build a mapping from source class index -> target class index,
    using class names from data.yaml and our source_map lookup table.
    """
    if yaml_data is None:
        return {}

    names = yaml_data.get("names", [])
    if isinstance(names, dict):
        names = [names[k] for k in sorted(names.keys())]

    mapping = {}
    for src_idx, name in enumerate(names):
        # Try exact match first, then case-insensitive, then partial
        target_id = source_map.get(name)
        if target_id is None:
            target_id = source_map.get(name.lower())
        if target_id is None:
            # Try partial matching for speed limit variants
            name_lower = name.lower().replace("-", " ").replace("_", " ")
            for key, val in source_map.items():
                if key.lower() in name_lower or name_lower in key.lower():
                    target_id = val
                    break
        if target_id is not None:
            mapping[src_idx] = target_id
            print(f"      class {src_idx} '{name}' -> {target_id} "
                  f"'{TARGET_CLASSES[target_id]}'")
        # else: skip this class (not relevant to our task)

    return mapping


def process_yolo_split(src_img_dir, src_lbl_dir, dst_img_dir, dst_lbl_dir,
                       class_map, prefix="", stats=None):
    """
    Copy images and remap labels from a YOLO-format source split.
    Only copies annotations that have classes in our class_map.
    """
    if not os.path.exists(src_img_dir) or not os.path.exists(src_lbl_dir):
        return 0

    os.makedirs(dst_img_dir, exist_ok=True)
    os.makedirs(dst_lbl_dir, exist_ok=True)

    copied = 0
    img_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    img_files = [f for f in os.listdir(src_img_dir)
                 if Path(f).suffix.lower() in img_extensions]

    for img_file in tqdm(img_files, desc=f"    {prefix}", leave=False):
        stem = Path(img_file).stem
        label_file = stem + ".txt"
        src_label = os.path.join(src_lbl_dir, label_file)

        if not os.path.exists(src_label):
            continue

        # Read and remap labels
        new_lines = []
        with open(src_label, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                src_cls = int(parts[0])
                if src_cls in class_map:
                    target_cls = class_map[src_cls]
                    new_lines.append(f"{target_cls} {' '.join(parts[1:])}")
                    if stats is not None:
                        stats[target_cls] += 1

        if not new_lines:
            continue

        # Copy image with prefix to avoid name collisions
        dst_name = f"{prefix}{img_file}" if prefix else img_file
        dst_label_name = f"{prefix}{stem}.txt" if prefix else label_file

        shutil.copy2(
            os.path.join(src_img_dir, img_file),
            os.path.join(dst_img_dir, dst_name),
        )
        with open(os.path.join(dst_lbl_dir, dst_label_name), "w") as f:
            f.write("\n".join(new_lines) + "\n")
        copied += 1

    return copied


def merge_dataset(source_dir, output_dir, class_map, prefix="", stats=None):
    """Merge a YOLO-format dataset into the unified output directory."""
    total = 0
    for split in ["train", "valid", "test"]:
        src_img = os.path.join(source_dir, split, "images")
        src_lbl = os.path.join(source_dir, split, "labels")
        dst_img = os.path.join(output_dir, split, "images")
        dst_lbl = os.path.join(output_dir, split, "labels")
        n = process_yolo_split(
            src_img, src_lbl, dst_img, dst_lbl, class_map, prefix, stats
        )
        if n > 0:
            print(f"      {split}: {n} images")
        total += n
    return total


# ============================================================
# Dataset statistics & validation
# ============================================================

def print_stats(stats, output_dir):
    """Print class distribution statistics."""
    print("\n" + "=" * 55)
    print("  Dataset Statistics")
    print("=" * 55)
    total = sum(stats.values())
    for cls_id in sorted(TARGET_CLASSES.keys()):
        name = TARGET_CLASSES[cls_id]
        count = stats.get(cls_id, 0)
        bar = "#" * min(count // 5, 40)
        pct = (count / total * 100) if total > 0 else 0
        print(f"  {cls_id}: {name:<20s} {count:>5d} ({pct:>5.1f}%) {bar}")
    print(f"  {'TOTAL':<23s} {total:>5d}")
    print("=" * 55)

    # Count images per split
    for split in ["train", "valid", "test"]:
        img_dir = os.path.join(output_dir, split, "images")
        if os.path.exists(img_dir):
            n = len(os.listdir(img_dir))
            print(f"  {split}: {n} images")

    # Warn about underrepresented classes
    min_count = 20
    weak_classes = [
        (cls_id, TARGET_CLASSES[cls_id], stats.get(cls_id, 0))
        for cls_id in TARGET_CLASSES
        if stats.get(cls_id, 0) < min_count
    ]
    if weak_classes:
        print(f"\n  [Warning] Underrepresented classes (< {min_count} annotations):")
        for cls_id, name, count in weak_classes:
            print(f"    - {name}: {count} annotations")
        print("    Consider adding more images for these classes.")
        print("    See README.md for instructions on adding custom data.")


def create_output_yaml(output_dir):
    """Create data.yaml in the output dataset directory."""
    import yaml

    yaml_data = {
        "train": os.path.join(os.path.abspath(output_dir), "train", "images"),
        "val": os.path.join(os.path.abspath(output_dir), "valid", "images"),
        "test": os.path.join(os.path.abspath(output_dir), "test", "images"),
        "nc": len(TARGET_CLASSES),
        "names": list(TARGET_CLASSES.values()),
    }

    yaml_path = os.path.join(output_dir, "data.yaml")
    with open(yaml_path, "w") as f:
        yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False)
    print(f"\n  Dataset config written to: {yaml_path}")
    return yaml_path


# ============================================================
# Local custom data integration
# ============================================================

def integrate_local_data(local_dir, output_dir, stats):
    """
    Integrate user-provided local images with YOLO-format annotations.

    Expected structure:
      local_dir/
        images/
          img001.jpg
          img002.png
        labels/
          img001.txt   (YOLO format, already using our 9-class IDs)
          img002.txt
    """
    img_dir = os.path.join(local_dir, "images")
    lbl_dir = os.path.join(local_dir, "labels")
    if not os.path.exists(img_dir):
        print(f"\n  [Skip] Local data dir not found: {img_dir}")
        return 0

    print(f"\n  Integrating local data from: {local_dir}")

    # Split 80/10/10
    img_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    images = [f for f in os.listdir(img_dir)
              if Path(f).suffix.lower() in img_extensions]
    random.shuffle(images)

    n = len(images)
    n_train = int(n * 0.8)
    n_valid = int(n * 0.1)
    splits = {
        "train": images[:n_train],
        "valid": images[n_train:n_train + n_valid],
        "test": images[n_train + n_valid:],
    }

    total = 0
    for split, split_images in splits.items():
        dst_img = os.path.join(output_dir, split, "images")
        dst_lbl = os.path.join(output_dir, split, "labels")
        os.makedirs(dst_img, exist_ok=True)
        os.makedirs(dst_lbl, exist_ok=True)

        for img_file in split_images:
            stem = Path(img_file).stem
            label_file = stem + ".txt"
            src_label = os.path.join(lbl_dir, label_file)
            if not os.path.exists(src_label):
                continue

            shutil.copy2(os.path.join(img_dir, img_file),
                         os.path.join(dst_img, f"local_{img_file}"))

            # Count annotations
            with open(src_label, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
            with open(os.path.join(dst_lbl, f"local_{stem}.txt"), "w") as f:
                for line in lines:
                    f.write(line + "\n")
                    cls_id = int(line.split()[0])
                    if stats is not None and cls_id in TARGET_CLASSES:
                        stats[cls_id] += 1
            total += 1

    print(f"  Local data: {total} images integrated")
    return total


# ============================================================
# Main
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Prepare Australian Speed Signs dataset for YOLOv5 training")

    parser.add_argument("--roboflow-key", type=str, default=None,
                        help="Roboflow API key (get from https://app.roboflow.com/settings)")
    parser.add_argument("--output", type=str, default="au_speed_dataset",
                        help="Output dataset directory (default: au_speed_dataset)")
    parser.add_argument("--downloads", type=str, default="downloads",
                        help="Directory for downloaded raw datasets (default: downloads)")
    parser.add_argument("--skip-gtsdb", action="store_true",
                        help="Skip GTSDB download (German signs)")
    parser.add_argument("--skip-roboflow", action="store_true",
                        help="Skip all Roboflow downloads (use local/GTSDB only)")
    parser.add_argument("--local-dir", type=str, default=None,
                        help="Path to local custom-labeled images (YOLO format)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    parser.add_argument("--clean", action="store_true",
                        help="Remove existing output directory before starting")

    return parser.parse_args()


def main():
    args = parse_args()
    random.seed(args.seed)

    output_dir = os.path.abspath(args.output)
    downloads_dir = os.path.abspath(args.downloads)

    print("=" * 55)
    print("  AU Speed Signs Dataset Preparation")
    print("=" * 55)
    print(f"  Output:    {output_dir}")
    print(f"  Downloads: {downloads_dir}")

    # Clean output if requested
    if args.clean and os.path.exists(output_dir):
        print(f"\n  Removing existing output: {output_dir}")
        shutil.rmtree(output_dir)

    # Create output structure
    for split in ["train", "valid", "test"]:
        os.makedirs(os.path.join(output_dir, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, split, "labels"), exist_ok=True)

    os.makedirs(downloads_dir, exist_ok=True)
    stats = defaultdict(int)

    # ------------------------------------------------------------------
    # Source 1: Roboflow Australia Traffic Sign
    # ------------------------------------------------------------------
    if not args.skip_roboflow:
        if not args.roboflow_key:
            print("\n  [Warning] No Roboflow API key provided.")
            print("  Get your free key at: https://app.roboflow.com/settings")
            print("  Run with: --roboflow-key YOUR_KEY")
            print("  Skipping Roboflow downloads.\n")
        else:
            # Download AU traffic signs
            au_path = download_au_traffic_signs(args.roboflow_key, downloads_dir)
            if au_path:
                yaml_data = read_yolo_dataset_yaml(au_path)
                if yaml_data:
                    print("\n  Mapping AU Traffic Sign classes:")
                    class_map = build_class_map_from_yaml(
                        yaml_data, ROBOFLOW_AU_CLASS_MAP)
                    if class_map:
                        n = merge_dataset(au_path, output_dir, class_map,
                                          prefix="au_", stats=stats)
                        print(f"  AU Traffic Sign: {n} images merged")

            # Download speed camera dataset
            cam_path = download_speed_camera_dataset(
                args.roboflow_key, downloads_dir)
            if cam_path:
                yaml_data = read_yolo_dataset_yaml(cam_path)
                if yaml_data:
                    print("\n  Mapping Speed Camera classes:")
                    class_map = build_class_map_from_yaml(
                        yaml_data, ROBOFLOW_AU_CLASS_MAP)
                    if class_map:
                        n = merge_dataset(cam_path, output_dir, class_map,
                                          prefix="cam_", stats=stats)
                        print(f"  Speed Camera: {n} images merged")

    # ------------------------------------------------------------------
    # Source 2: GTSDB (German Traffic Sign Detection Benchmark)
    # ------------------------------------------------------------------
    if not args.skip_gtsdb:
        gtsdb_path = download_gtsdb(downloads_dir)
        if gtsdb_path:
            # Check if it's in YOLO format (Roboflow) or CSV (official)
            gt_file = os.path.join(gtsdb_path, "gt.txt")
            if os.path.exists(gt_file):
                # Official format: convert CSV -> YOLO
                converted_dir = os.path.join(downloads_dir, "gtsdb_yolo")
                convert_gtsdb_csv_to_yolo(gtsdb_path, converted_dir)
                # Move converted data to train split (GTSDB has no preset splits)
                gtsdb_img = os.path.join(converted_dir, "images")
                gtsdb_lbl = os.path.join(converted_dir, "labels")
                if os.path.exists(gtsdb_img):
                    # Identity class map (already remapped during conversion)
                    identity_map = {i: i for i in range(9)}
                    n = process_yolo_split(
                        gtsdb_img, gtsdb_lbl,
                        os.path.join(output_dir, "train", "images"),
                        os.path.join(output_dir, "train", "labels"),
                        identity_map, prefix="gtsdb_", stats=stats,
                    )
                    print(f"  GTSDB (official): {n} images merged into train")
            else:
                # Roboflow YOLO format
                yaml_data = read_yolo_dataset_yaml(gtsdb_path)
                if yaml_data:
                    print("\n  Mapping GTSDB classes:")
                    class_map = build_class_map_from_yaml(
                        yaml_data, ROBOFLOW_AU_CLASS_MAP)
                    # Also try numeric GTSDB class map
                    if not class_map:
                        class_map = GTSDB_CLASS_MAP.copy()
                        print("  Using numeric GTSDB class mapping")
                    if class_map:
                        n = merge_dataset(gtsdb_path, output_dir, class_map,
                                          prefix="gtsdb_", stats=stats)
                        print(f"  GTSDB: {n} images merged")

    # ------------------------------------------------------------------
    # Source 3: Local custom data
    # ------------------------------------------------------------------
    if args.local_dir:
        integrate_local_data(args.local_dir, output_dir, stats)

    # ------------------------------------------------------------------
    # Create data.yaml and print statistics
    # ------------------------------------------------------------------
    create_output_yaml(output_dir)
    print_stats(stats, output_dir)

    # Final instructions
    total_images = sum(
        len(os.listdir(os.path.join(output_dir, s, "images")))
        for s in ["train", "valid", "test"]
        if os.path.exists(os.path.join(output_dir, s, "images"))
    )

    if total_images == 0:
        print("\n  [Warning] No images in the dataset!")
        print("  Make sure to provide a Roboflow API key or local data.")
    else:
        print(f"\n  Dataset ready: {total_images} total images")
        print(f"  Config: {os.path.join(output_dir, 'data.yaml')}")
        print("\n  Next steps:")
        print("    1. Review class distribution above")
        print("    2. Add more data for underrepresented classes if needed")
        print("    3. Start training:")
        print("       cd airockchip_yolov5")
        print(f"       python train.py --data {os.path.join(output_dir, 'data.yaml')} \\")
        print("           --cfg yolov5n.yaml --img 320 --batch 32 --epochs 100")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Crop augmentation for speed sign training data.

Solves two problems at once:
  1. 76.5% of original annotations are tiny (<6px @ 640), unlearnable
  2. Training data must match deployment scale (signs at 30-100px @ 640)

Two-tier crop strategy ensures training distribution matches inference:
  - Scene crops  (70%, pad 15-25x): signs at 25-50px, matches dashcam at 30-60m
  - Detail crops (30%, pad 4-8x):   signs at 80-160px, teaches digit classification

Only crops are included in the output. Original full images are excluded because
even after filtering (>= 20px in original), 52% of annotations are < 10px at
640x640 training resolution (due to downscale from median 3840px originals).

Usage:
  python augment_crops.py \\
      --dataset /path/to/speed_signs_dataset \\
      --output /path/to/speed_signs_augmented \\
      --min-sign-px 20 \\
      --target-per-class 800 \\
      --seed 42

Output: augmented dataset in YOLOv5 format (drop-in replacement).
"""

import argparse
import os
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("[Error] Pillow required: pip install Pillow")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("[Error] PyYAML required: pip install pyyaml")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


# ============================================================
# Crop tier configuration
# ============================================================
# Scene crops: large padding -> signs at deployment-realistic scale (25-50px @ 640)
# Detail crops: small padding -> signs large enough to learn digit features
SCENE_PAD_RANGE = (15.0, 25.0)
DETAIL_PAD_RANGE = (4.0, 8.0)
SCENE_RATIO = 0.7  # 70% scene crops, 30% detail crops


# ============================================================
# Core logic
# ============================================================

def load_annotations(label_path):
    """Load YOLO-format annotations from a label file.

    Returns list of (cls_id, cx, cy, w, h) tuples (all normalized).
    """
    annotations = []
    if not os.path.exists(label_path):
        return annotations
    with open(label_path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            cls_id = int(parts[0])
            cx, cy, w, h = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            annotations.append((cls_id, cx, cy, w, h))
    return annotations


def compute_pixel_size(ann, img_w, img_h):
    """Compute actual pixel dimensions of a normalized bbox."""
    _, _, _, nw, nh = ann
    return nw * img_w, nh * img_h


def make_crop(img, ann, img_w, img_h, pad_mult, all_anns,
              min_co_located_px=10):
    """Create a crop around a target annotation.

    Args:
        img: PIL Image
        ann: target annotation (cls_id, cx, cy, w, h) normalized
        img_w, img_h: image dimensions
        pad_mult: padding multiplier (crop = sign_size * pad_mult)
        all_anns: all annotations in the image
        min_co_located_px: minimum pixel size at 640 for co-located annotations

    Returns:
        (cropped_img, new_annotations) or None if crop is degenerate.
    """
    cls_id, cx, cy, nw, nh = ann
    sign_pw = nw * img_w
    sign_ph = nh * img_h

    # Crop size = max(sign_w, sign_h) * pad_mult, square
    crop_size = max(sign_pw, sign_ph) * pad_mult
    crop_size = max(crop_size, 64)  # minimum 64px crop

    # Center crop on the sign with slight random offset for variety
    offset_x = random.uniform(-0.15, 0.15) * crop_size
    offset_y = random.uniform(-0.15, 0.15) * crop_size
    center_x = cx * img_w + offset_x
    center_y = cy * img_h + offset_y

    half = crop_size / 2
    x1 = int(max(0, center_x - half))
    y1 = int(max(0, center_y - half))
    x2 = int(min(img_w, center_x + half))
    y2 = int(min(img_h, center_y + half))

    cw = x2 - x1
    ch = y2 - y1
    if cw < 32 or ch < 32:
        return None

    cropped = img.crop((x1, y1, x2, y2))

    # Remap annotations that fall within the crop
    new_anns = []
    for a_cls, a_cx, a_cy, a_w, a_h in all_anns:
        # Convert to pixel coords
        px = a_cx * img_w
        py = a_cy * img_h
        pw = a_w * img_w
        ph = a_h * img_h

        # Check if sign center is within crop
        if px < x1 or px > x2 or py < y1 or py > y2:
            continue

        # Recompute normalized coords relative to crop
        new_cx = (px - x1) / cw
        new_cy = (py - y1) / ch
        new_w = pw / cw
        new_h = ph / ch

        # Clamp to [0, 1]
        new_cx = max(0.001, min(0.999, new_cx))
        new_cy = max(0.001, min(0.999, new_cy))
        new_w = min(new_w, min(new_cx, 1 - new_cx) * 2)
        new_h = min(new_h, min(new_cy, 1 - new_cy) * 2)

        if new_w > 0.01 and new_h > 0.01:
            # Filter co-located annotations that are too small at 640 training resolution
            scale_to_640 = 640 / max(cw, ch)
            px_w_640 = new_w * cw * scale_to_640
            px_h_640 = new_h * ch * scale_to_640
            if min(px_w_640, px_h_640) < min_co_located_px:
                continue
            new_anns.append((a_cls, new_cx, new_cy, new_w, new_h))

    if not new_anns:
        return None

    return cropped, new_anns


def save_annotation(label_path, annotations):
    """Write YOLO-format label file."""
    with open(label_path, "w") as f:
        for cls_id, cx, cy, w, h in annotations:
            f.write(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")


# ============================================================
# Main pipeline
# ============================================================

def _scan_and_filter(src_img_dir, src_lbl_dir, min_sign_px, nc):
    """Scan images, filter annotations by minimum pixel size.

    Returns (image_data, class_counts, class_sources).
    """
    image_data = {}
    class_counts = defaultdict(int)
    class_sources = defaultdict(list)

    img_files = sorted([f for f in os.listdir(src_img_dir)
                        if f.lower().endswith((".jpg", ".jpeg", ".png"))])

    print(f"  Scanning {len(img_files)} images...")
    for img_file in tqdm(img_files, desc="  Scanning"):
        key = Path(img_file).stem
        img_path = os.path.join(src_img_dir, img_file)
        lbl_path = os.path.join(src_lbl_dir, f"{key}.txt")

        anns = load_annotations(lbl_path)
        if not anns:
            continue

        img = Image.open(img_path)
        img_w, img_h = img.size
        img.close()

        filtered = []
        for i, ann in enumerate(anns):
            pw, ph = compute_pixel_size(ann, img_w, img_h)
            min_dim = min(pw, ph)
            if min_dim >= min_sign_px:
                filtered.append((ann, pw, ph))
                cls_id = ann[0]
                class_counts[cls_id] += 1
                class_sources[cls_id].append((key, i))

        if filtered:
            image_data[key] = {
                "file": img_file,
                "anns": anns,
                "filtered": filtered,
                "img_w": img_w,
                "img_h": img_h,
            }

    print(f"  After size filter (>= {min_sign_px}px): "
          f"{sum(class_counts.values())} annotations in {len(image_data)} images")
    for cls_id in range(nc):
        print(f"    class {cls_id}: {class_counts.get(cls_id, 0)}")

    return image_data, class_counts, class_sources


def _copy_originals(image_data, src_img_dir, dst_img_dir, dst_lbl_dir):
    """Copy original images with filtered labels."""
    print(f"\n  Copying original images with filtered labels...")
    copied = 0
    for key, data in tqdm(image_data.items(), desc="  Copying"):
        src_img = os.path.join(src_img_dir, data["file"])
        dst_img = os.path.join(dst_img_dir, data["file"])
        shutil.copy2(src_img, dst_img)

        filtered_anns = [ann for ann, _, _ in data["filtered"]]
        dst_lbl = os.path.join(dst_lbl_dir, f"{key}.txt")
        save_annotation(dst_lbl, filtered_anns)
        copied += 1
    print(f"  Copied {copied} original images")


def augment_train(src_img_dir, src_lbl_dir, dst_img_dir, dst_lbl_dir,
                  min_sign_px, target_per_class, nc, seed):
    """Process train split: filter + two-tier crops with class balancing."""
    random.seed(seed)
    os.makedirs(dst_img_dir, exist_ok=True)
    os.makedirs(dst_lbl_dir, exist_ok=True)

    image_data, class_counts, class_sources = _scan_and_filter(
        src_img_dir, src_lbl_dir, min_sign_px, nc)

    # --- Two-tier crop augmentation with class balancing ---
    # No originals copied, so generate target_per_class crops per class
    crops_needed = {}
    for cls_id in range(nc):
        if cls_id in class_sources and class_sources[cls_id]:
            crops_needed[cls_id] = target_per_class
        else:
            crops_needed[cls_id] = 0
    total_crops = sum(crops_needed.values())

    print(f"\n  Two-tier crop augmentation (target {target_per_class}/class):")
    print(f"    Scene crops  ({SCENE_RATIO:.0%}, pad {SCENE_PAD_RANGE[0]:.0f}-{SCENE_PAD_RANGE[1]:.0f}x): "
          f"signs at deployment scale (~30-50px @ 640)")
    print(f"    Detail crops ({1-SCENE_RATIO:.0%}, pad {DETAIL_PAD_RANGE[0]:.0f}-{DETAIL_PAD_RANGE[1]:.0f}x): "
          f"signs large for classification (~80-160px @ 640)")
    print(f"    Total crops needed: {total_crops}")

    for cls_id in range(nc):
        print(f"    class {cls_id}: {class_counts.get(cls_id, 0)} source annotations, "
              f"generating {crops_needed[cls_id]} crops")

    crop_count = 0
    scene_count = 0
    detail_count = 0

    for cls_id in range(nc):
        n_needed = crops_needed[cls_id]
        if n_needed <= 0 or cls_id not in class_sources:
            continue

        sources = class_sources[cls_id]
        if not sources:
            print(f"    [Warning] class {cls_id}: no sources for cropping")
            continue

        n_scene = int(n_needed * SCENE_RATIO)
        n_detail = n_needed - n_scene

        for tier_name, tier_n, pad_range in [
            ("scene", n_scene, SCENE_PAD_RANGE),
            ("detail", n_detail, DETAIL_PAD_RANGE),
        ]:
            for i in range(tier_n):
                src_key, ann_idx = random.choice(sources)
                data = image_data[src_key]

                img_path = os.path.join(src_img_dir, data["file"])
                img = Image.open(img_path)
                img_w, img_h = img.size

                target_ann = data["anns"][ann_idx]
                pad_mult = random.uniform(pad_range[0], pad_range[1])

                result = make_crop(img, target_ann, img_w, img_h,
                                   pad_mult, data["anns"])
                img.close()

                if result is None:
                    continue

                cropped_img, new_anns = result

                crop_name = f"crop_{tier_name[0]}_{cls_id}_{crop_count:05d}"
                crop_img_path = os.path.join(dst_img_dir, f"{crop_name}.jpg")
                crop_lbl_path = os.path.join(dst_lbl_dir, f"{crop_name}.txt")

                cropped_img.save(crop_img_path, quality=95)
                save_annotation(crop_lbl_path, new_anns)
                crop_count += 1
                if tier_name == "scene":
                    scene_count += 1
                else:
                    detail_count += 1

    print(f"  Crops generated: {crop_count} total "
          f"({scene_count} scene + {detail_count} detail)")


def augment_val(src_img_dir, src_lbl_dir, dst_img_dir, dst_lbl_dir,
                min_sign_px, nc, seed):
    """Process val split: filter + one scene crop per annotation.

    Val strategy differs from train:
      - Scene crops only (no detail crops) -- measures deployment-scale performance
      - One crop per annotation (no class balancing) -- preserves natural distribution
      - No oversampling -- honest evaluation metric
    """
    random.seed(seed)
    os.makedirs(dst_img_dir, exist_ok=True)
    os.makedirs(dst_lbl_dir, exist_ok=True)

    image_data, class_counts, class_sources = _scan_and_filter(
        src_img_dir, src_lbl_dir, min_sign_px, nc)

    # --- One scene crop per filtered annotation ---
    print(f"\n  Val scene crops (1 per annotation, pad {SCENE_PAD_RANGE[0]:.0f}-{SCENE_PAD_RANGE[1]:.0f}x):")
    crop_count = 0

    for key, data in tqdm(image_data.items(), desc="  Cropping"):
        img_path = os.path.join(src_img_dir, data["file"])
        img = Image.open(img_path)
        img_w, img_h = img.size

        for ann, pw, ph in data["filtered"]:
            pad_mult = random.uniform(SCENE_PAD_RANGE[0], SCENE_PAD_RANGE[1])
            result = make_crop(img, ann, img_w, img_h,
                               pad_mult, data["anns"])
            if result is None:
                continue

            cropped_img, new_anns = result
            crop_name = f"crop_s_val_{crop_count:05d}"
            cropped_img.save(
                os.path.join(dst_img_dir, f"{crop_name}.jpg"), quality=95)
            save_annotation(
                os.path.join(dst_lbl_dir, f"{crop_name}.txt"), new_anns)
            crop_count += 1

        img.close()

    print(f"  Val scene crops generated: {crop_count}")


def main():
    parser = argparse.ArgumentParser(
        description="Crop augmentation for speed sign training data")
    parser.add_argument("--dataset", type=str, required=True,
                        help="Source dataset directory (YOLOv5 format)")
    parser.add_argument("--output", type=str, required=True,
                        help="Output augmented dataset directory")
    parser.add_argument("--min-sign-px", type=int, default=20,
                        help="Minimum sign pixel size in original image (default: 20)")
    parser.add_argument("--target-per-class", type=int, default=800,
                        help="Target annotations per class after augmentation (default: 800)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    args = parser.parse_args()

    src_dir = os.path.abspath(args.dataset)
    dst_dir = os.path.abspath(args.output)

    # Clean output directory to avoid stale files from previous runs
    if os.path.exists(dst_dir):
        print(f"[Cleanup] Removing previous output: {dst_dir}")
        shutil.rmtree(dst_dir)

    # Load data.yaml
    data_yaml_path = os.path.join(src_dir, "data.yaml")
    if not os.path.exists(data_yaml_path):
        print(f"[Error] data.yaml not found: {data_yaml_path}")
        sys.exit(1)

    with open(data_yaml_path) as f:
        data_cfg = yaml.safe_load(f)

    nc = data_cfg["nc"]
    class_names = data_cfg["names"]
    print("=" * 60)
    print("  Crop Augmentation for Speed Sign Dataset")
    print("=" * 60)
    print(f"  Source:           {src_dir}")
    print(f"  Output:           {dst_dir}")
    print(f"  Classes:          {nc}")
    print(f"  Min sign pixels:  {args.min_sign_px}")
    print(f"  Target/class:     {args.target_per_class}")
    print(f"  Scene crop pad:   {SCENE_PAD_RANGE[0]:.0f}-{SCENE_PAD_RANGE[1]:.0f}x "
          f"({SCENE_RATIO:.0%} of crops)")
    print(f"  Detail crop pad:  {DETAIL_PAD_RANGE[0]:.0f}-{DETAIL_PAD_RANGE[1]:.0f}x "
          f"({1-SCENE_RATIO:.0%} of crops)")
    print(f"  Seed:             {args.seed}")

    # Process train split
    print(f"\n{'='*60}")
    print(f"  Processing TRAIN split")
    print(f"{'='*60}")
    augment_train(
        src_img_dir=os.path.join(src_dir, "train", "images"),
        src_lbl_dir=os.path.join(src_dir, "train", "labels"),
        dst_img_dir=os.path.join(dst_dir, "train", "images"),
        dst_lbl_dir=os.path.join(dst_dir, "train", "labels"),
        min_sign_px=args.min_sign_px,
        target_per_class=args.target_per_class,
        nc=nc,
        seed=args.seed,
    )

    # Process val split: filter + scene crops (no balancing, no detail)
    print(f"\n{'='*60}")
    print(f"  Processing VAL split")
    print(f"{'='*60}")
    augment_val(
        src_img_dir=os.path.join(src_dir, "val", "images"),
        src_lbl_dir=os.path.join(src_dir, "val", "labels"),
        dst_img_dir=os.path.join(dst_dir, "val", "images"),
        dst_lbl_dir=os.path.join(dst_dir, "val", "labels"),
        min_sign_px=args.min_sign_px,
        nc=nc,
        seed=args.seed + 1,
    )

    # Write data.yaml for output
    out_yaml = {
        "train": os.path.join(dst_dir, "train", "images"),
        "val": os.path.join(dst_dir, "val", "images"),
        "nc": nc,
        "names": class_names,
    }
    out_yaml_path = os.path.join(dst_dir, "data.yaml")
    os.makedirs(dst_dir, exist_ok=True)
    with open(out_yaml_path, "w") as f:
        yaml.dump(out_yaml, f, default_flow_style=False, sort_keys=False)

    # Final stats
    print(f"\n{'='*60}")
    print(f"  Augmented Dataset Ready")
    print(f"{'='*60}")
    print(f"  Output: {dst_dir}")
    for split in ["train", "val"]:
        img_dir = os.path.join(dst_dir, split, "images")
        n = len([f for f in os.listdir(img_dir)
                 if f.lower().endswith((".jpg", ".jpeg", ".png"))]) if os.path.exists(img_dir) else 0
        print(f"  {split}: {n} images")

    # Count final class and size distribution
    print(f"\n  Final annotation distribution (train):")
    train_lbl_dir = os.path.join(dst_dir, "train", "labels")
    final_counts = defaultdict(int)
    area_buckets = {"tiny (<0.1%)": 0, "small (0.1-1%)": 0,
                    "med (1-5%)": 0, "large (>5%)": 0}
    for lf in os.listdir(train_lbl_dir):
        if not lf.endswith(".txt"):
            continue
        with open(os.path.join(train_lbl_dir, lf)) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 5:
                    final_counts[int(parts[0])] += 1
                    area = float(parts[3]) * float(parts[4])
                    if area < 0.001:
                        area_buckets["tiny (<0.1%)"] += 1
                    elif area < 0.01:
                        area_buckets["small (0.1-1%)"] += 1
                    elif area < 0.05:
                        area_buckets["med (1-5%)"] += 1
                    else:
                        area_buckets["large (>5%)"] += 1

    for cls_id in range(nc):
        name = class_names[cls_id] if cls_id < len(class_names) else f"cls{cls_id}"
        print(f"    {cls_id}: {name:<20s} {final_counts.get(cls_id, 0):>5d}")
    print(f"    {'TOTAL':<23s} {sum(final_counts.values()):>5d}")

    print(f"\n  Bbox size distribution (normalized area):")
    total_ann = sum(area_buckets.values())
    for bucket, count in area_buckets.items():
        pct = count / total_ann * 100 if total_ann else 0
        print(f"    {bucket:<16s} {count:>5d} ({pct:>5.1f}%)")

    print(f"\n  Next steps:")
    print(f"    1. Pack for Colab:")
    print(f"       cd {os.path.dirname(dst_dir)}")
    print(f"       tar -czf speed_signs_dataset.tar.gz {os.path.basename(dst_dir)}/")
    print(f"    2. Upload to Google Drive and retrain")


if __name__ == "__main__":
    main()

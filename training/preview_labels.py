#!/usr/bin/env python3
"""Preview YOLO-format labels on images.

Usage:
  # Random grid of 12 samples
  python preview_labels.py /path/to/dataset/train

  # Browse interactively (arrow keys / click)
  python preview_labels.py /path/to/dataset/train --browse

  # Filter by crop type or class
  python preview_labels.py /path/to/dataset/train --filter crop_s  # scene crops only
  python preview_labels.py /path/to/dataset/train --filter crop_d  # detail crops only
  python preview_labels.py /path/to/dataset/train --class-id 9     # class 9 only

  # Save grid to file instead of displaying
  python preview_labels.py /path/to/dataset/train --save grid.png
"""

import argparse
import os
import random
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("[Error] pip install Pillow")
    sys.exit(1)

# Colors per class (RGB)
COLORS = [
    (255, 80, 80), (80, 200, 80), (80, 80, 255), (255, 200, 50),
    (255, 80, 255), (80, 220, 220), (255, 140, 50), (160, 80, 255),
    (80, 160, 255), (200, 200, 80), (220, 100, 180),
]

CLASS_NAMES = [
    "20", "30", "40", "50", "60", "70", "80", "90", "100", "110", "120",
]


def load_annotations(label_path):
    """Load YOLO annotations: [(cls_id, cx, cy, w, h), ...]"""
    anns = []
    if not os.path.exists(label_path):
        return anns
    with open(label_path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 5:
                anns.append((int(parts[0]), *[float(x) for x in parts[1:5]]))
    return anns


def draw_boxes(img, anns):
    """Draw YOLO bboxes on a PIL Image. Returns annotated copy."""
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
    except (IOError, OSError):
        font = ImageFont.load_default()

    for cls_id, cx, cy, bw, bh in anns:
        color = COLORS[cls_id % len(COLORS)]
        x1 = int((cx - bw / 2) * w)
        y1 = int((cy - bh / 2) * h)
        x2 = int((cx + bw / 2) * w)
        y2 = int((cy + bh / 2) * h)

        # Box
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

        # Label background
        name = CLASS_NAMES[cls_id] if cls_id < len(CLASS_NAMES) else str(cls_id)
        label = f"{name}km/h"
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        label_y = max(0, y1 - th - 4)
        draw.rectangle([x1, label_y, x1 + tw + 4, label_y + th + 4], fill=color)
        draw.text((x1 + 2, label_y + 1), label, fill=(255, 255, 255), font=font)

    return img


def find_samples(split_dir, filter_prefix=None, class_id=None):
    """Find image/label pairs in a split directory."""
    img_dir = os.path.join(split_dir, "images")
    lbl_dir = os.path.join(split_dir, "labels")

    if not os.path.isdir(img_dir):
        # Maybe user passed the dataset root, try train/
        for sub in ["train", "val"]:
            candidate = os.path.join(split_dir, sub, "images")
            if os.path.isdir(candidate):
                img_dir = candidate
                lbl_dir = os.path.join(split_dir, sub, "labels")
                print(f"  Using: {sub}/")
                break
        else:
            print(f"[Error] No images/ directory found in {split_dir}")
            sys.exit(1)

    files = sorted([f for f in os.listdir(img_dir)
                    if f.lower().endswith((".jpg", ".jpeg", ".png"))])

    if filter_prefix:
        files = [f for f in files if f.startswith(filter_prefix)]

    if class_id is not None:
        filtered = []
        for f in files:
            stem = Path(f).stem
            lbl_path = os.path.join(lbl_dir, f"{stem}.txt")
            anns = load_annotations(lbl_path)
            if any(a[0] == class_id for a in anns):
                filtered.append(f)
        files = filtered

    return files, img_dir, lbl_dir


def make_grid(images, cols=4, cell_size=480):
    """Arrange images into a grid."""
    rows = (len(images) + cols - 1) // cols
    grid = Image.new("RGB", (cols * cell_size, rows * cell_size), (30, 30, 30))

    for i, (img, title) in enumerate(images):
        r, c = divmod(i, cols)
        # Resize maintaining aspect ratio
        img.thumbnail((cell_size, cell_size), Image.LANCZOS)
        # Center in cell
        x = c * cell_size + (cell_size - img.width) // 2
        y = r * cell_size + (cell_size - img.height) // 2
        grid.paste(img, (x, y))

        # Title
        draw = ImageDraw.Draw(grid)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
        except (IOError, OSError):
            font = ImageFont.load_default()
        text_x = c * cell_size + 4
        text_y = r * cell_size + 2
        # Truncate long filenames
        short = title[:35] + "..." if len(title) > 38 else title
        draw.text((text_x + 1, text_y + 1), short, fill=(0, 0, 0), font=font)
        draw.text((text_x, text_y), short, fill=(200, 200, 200), font=font)

    return grid


def browse_mode(files, img_dir, lbl_dir):
    """Interactive browse with matplotlib."""
    try:
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[Error] pip install matplotlib (for --browse)")
        sys.exit(1)

    idx = [0]

    def show(i):
        i = i % len(files)
        idx[0] = i
        f = files[i]
        stem = Path(f).stem
        img = Image.open(os.path.join(img_dir, f))
        anns = load_annotations(os.path.join(lbl_dir, f"{stem}.txt"))
        annotated = draw_boxes(img, anns)

        ax.clear()
        ax.imshow(annotated)
        ax.set_title(f"{f}  ({i+1}/{len(files)})  [{len(anns)} boxes]", fontsize=10)
        ax.axis("off")
        fig.canvas.draw()

    def on_key(event):
        if event.key in ("right", "d", " "):
            show(idx[0] + 1)
        elif event.key in ("left", "a"):
            show(idx[0] - 1)
        elif event.key == "r":
            show(random.randint(0, len(files) - 1))
        elif event.key in ("q", "escape"):
            plt.close()

    fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    fig.canvas.mpl_connect("key_press_event", on_key)
    show(0)
    plt.tight_layout()
    plt.show()


def main():
    parser = argparse.ArgumentParser(description="Preview YOLO labels on images")
    parser.add_argument("split_dir", help="Dataset split dir (e.g., dataset/train)")
    parser.add_argument("--browse", action="store_true",
                        help="Interactive mode (arrow keys to navigate)")
    parser.add_argument("--filter", type=str, default=None,
                        help="Filter by filename prefix (e.g., crop_s, crop_d)")
    parser.add_argument("--class-id", type=int, default=None,
                        help="Show only images containing this class")
    parser.add_argument("--n", type=int, default=12,
                        help="Number of samples in grid (default: 12)")
    parser.add_argument("--save", type=str, default=None,
                        help="Save grid to file instead of displaying")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible sampling")
    args = parser.parse_args()

    files, img_dir, lbl_dir = find_samples(
        args.split_dir, args.filter, args.class_id)

    if not files:
        print("[Error] No matching images found.")
        sys.exit(1)

    print(f"  Found {len(files)} images")

    if args.browse:
        browse_mode(files, img_dir, lbl_dir)
        return

    # Grid mode
    if args.seed is not None:
        random.seed(args.seed)
    samples = random.sample(files, min(args.n, len(files)))

    images = []
    for f in samples:
        stem = Path(f).stem
        img = Image.open(os.path.join(img_dir, f))
        anns = load_annotations(os.path.join(lbl_dir, f"{stem}.txt"))
        annotated = draw_boxes(img, anns)
        images.append((annotated, f))

    cols = 4 if len(images) >= 4 else len(images)
    grid = make_grid(images, cols=cols)

    if args.save:
        grid.save(args.save, quality=95)
        print(f"  Saved: {args.save}")
    else:
        grid.show()


if __name__ == "__main__":
    main()

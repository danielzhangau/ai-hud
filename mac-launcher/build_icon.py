#!/usr/bin/env python3
"""Generate AI-HUD.icns placeholder app icon.

Renders a dark-navy rounded square with bold white "AI-HUD" text and a
white speed-limit ring above, then writes a .iconset/ directory at the
sizes Apple requires (16, 32, 64, 128, 256, 512, 1024). Caller runs
`iconutil -c icns` on the directory to produce AI-HUD.icns.

Run from anywhere:
    python3 mac-launcher/build_icon.py
Output:
    mac-launcher/assets/AI-HUD.iconset/  (10 PNGs)
    mac-launcher/assets/AI-HUD.icns      (after iconutil)

This is a temporary placeholder until a designer ships a proper logo;
the .icns path stays the same so build.sh consumes whichever is there.
"""

import os
import sys
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "mac-launcher", "assets")
ICONSET = os.path.join(OUT_DIR, "AI-HUD.iconset")

# Color palette: dark navy bg (HUD/dashboard mood), pure white fg.
BG = (13, 27, 42, 255)       # #0d1b2a
FG = (255, 255, 255, 255)
RING = (231, 76, 60, 255)    # #e74c3c -- speed limit red ring accent


def find_font(size):
    """Best-effort: prefer SF / Helvetica Bold, fall back to PIL default."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                # Some .ttc files need an index to pick a face; 1 is usually
                # the bold variant for Helvetica.ttc.
                return ImageFont.truetype(p, size, index=1) if p.endswith(".ttc") else ImageFont.truetype(p, size)
            except OSError:
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    continue
    return ImageFont.load_default()


def render(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square background. macOS app icons have ~22.5% corner radius
    # of the icon side -- aligns with the system mask.
    radius = int(size * 0.225)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BG)

    # Speed-limit ring near the top. Skip it at the smallest sizes where
    # the stroke would smear to one pixel and look like noise.
    if size >= 64:
        cx, cy = size // 2, int(size * 0.36)
        ring_r = int(size * 0.18)
        ring_w = max(2, int(size * 0.035))
        d.ellipse(
            (cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
            outline=RING, width=ring_w,
        )
        # A short bold "60" inside the ring at large sizes only -- below
        # 256 it becomes illegible anti-aliased mud.
        if size >= 256:
            inner_font = find_font(int(ring_r * 1.1))
            txt = "60"
            l, t, r, b = d.textbbox((0, 0), txt, font=inner_font)
            tw, th = r - l, b - t
            d.text(
                (cx - tw // 2 - l, cy - th // 2 - t),
                txt, fill=FG, font=inner_font,
            )

    # Brand text "AI-HUD" centered in the lower half. Tighter tracking at
    # small sizes by dropping the hyphen below 128.
    label = "AI-HUD" if size >= 128 else "AI"
    # 28% of canvas for big label, scales down to fit at small sizes.
    font_size = max(8, int(size * 0.22))
    font = find_font(font_size)
    l, t, r, b = d.textbbox((0, 0), label, font=font)
    tw, th = r - l, b - t
    # Push the label below the ring (or center it if no ring rendered).
    cy_label = int(size * 0.72) if size >= 64 else size // 2
    d.text(
        (size // 2 - tw // 2 - l, cy_label - th // 2 - t),
        label, fill=FG, font=font,
    )
    return img


def main():
    os.makedirs(ICONSET, exist_ok=True)
    # Apple's required filename matrix. Sizes/@2x pairs map to retina.
    spec = [
        (16,   "icon_16x16.png"),
        (32,   "icon_16x16@2x.png"),
        (32,   "icon_32x32.png"),
        (64,   "icon_32x32@2x.png"),
        (128,  "icon_128x128.png"),
        (256,  "icon_128x128@2x.png"),
        (256,  "icon_256x256.png"),
        (512,  "icon_256x256@2x.png"),
        (512,  "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in spec:
        img = render(size)
        path = os.path.join(ICONSET, name)
        img.save(path, "PNG")
        print(f"  wrote {name} ({size}x{size})")
    print(f"\nNext: iconutil -c icns '{ICONSET}'")


if __name__ == "__main__":
    sys.exit(main() or 0)

#!/usr/bin/env python3
"""YOLOv5 compatibility patches for modern PyTorch and Pillow.

Idempotent -- safe to run multiple times. Must be run from the yolov5 repo root.

Patches:
  1. torch.load(): inject weights_only=False for PyTorch >= 2.6
  2. Pillow getsize(): wrap with getbbox() fallback for Pillow >= 10
"""

import subprocess
import sys


def patch_torch_load(filepath):
    """Inject weights_only=False into all torch.load() calls in a file."""
    with open(filepath, "r") as f:
        content = f.read()
    original = content
    result = []
    i = 0
    while i < len(content):
        idx = content.find("torch.load(", i)
        if idx == -1:
            result.append(content[i:])
            break
        result.append(content[i:idx])
        start = idx + len("torch.load(")
        depth = 1
        j = start
        while j < len(content) and depth > 0:
            if content[j] == "(":
                depth += 1
            elif content[j] == ")":
                depth -= 1
            j += 1
        args_str = content[start : j - 1]
        if "weights_only" not in args_str:
            result.append(f"torch.load({args_str}, weights_only=False)")
        else:
            result.append(f"torch.load({args_str})")
        i = j
    content = "".join(result)
    if content != original:
        with open(filepath, "w") as f:
            f.write(content)
        return True
    return False


def patch_pillow_getsize():
    """Wrap font.getsize() with getbbox() fallback in utils/plots.py."""
    plots_py = "utils/plots.py"
    with open(plots_py, "r") as f:
        full_text = f.read()

    if "getbbox" in full_text:
        print("[Patch] Pillow getsize: already applied")
        return
    if "getsize" not in full_text:
        print("[Patch] Pillow getsize: not needed")
        return

    lines = full_text.splitlines(keepends=True)
    new_lines = []
    for line in lines:
        if "self.font.getsize(label)" in line and "try" not in line:
            indent = line[: len(line) - len(line.lstrip())]
            inner = indent + "    "
            new_lines.append(f"{indent}try:\n")
            new_lines.append(f"{inner}w, h = self.font.getsize(label)\n")
            new_lines.append(f"{indent}except AttributeError:\n")
            new_lines.append(f"{inner}bbox = self.font.getbbox(label)\n")
            new_lines.append(
                f"{inner}w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]\n"
            )
        else:
            new_lines.append(line)
    with open(plots_py, "w") as f:
        f.writelines(new_lines)
    print("[Patch] Pillow getsize: applied")


def main():
    # 1. torch.load patch
    files_out = subprocess.run(
        ["grep", "-rl", "torch.load", "."],
        capture_output=True,
        text=True,
    ).stdout.strip().split("\n")
    patched = 0
    for f in files_out:
        if f.endswith(".py"):
            if patch_torch_load(f):
                patched += 1
                print(f"  Patched: {f}")
    print(f"[Patch] torch.load: {patched} files fixed")

    # 2. Pillow getsize patch
    patch_pillow_getsize()


if __name__ == "__main__":
    main()

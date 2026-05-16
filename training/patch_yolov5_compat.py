#!/usr/bin/env python3
"""YOLOv5 compatibility patches for modern PyTorch and Pillow.

Idempotent -- safe to run multiple times. Must be run from the yolov5 repo root.

Patches:
  1. torch.load(): inject weights_only=False for PyTorch >= 2.6
  2. Pillow getsize(): wrap with getbbox() fallback for Pillow >= 10
  3. torch.cuda.amp deprecation: autocast + GradScaler + alias patterns
"""

import re
import subprocess
import sys


# ============================================================
# Generic helpers
# ============================================================

def _make_cuda_replacer(new_call):
    """Factory for re.sub replacement functions that migrate torch.cuda.amp -> torch.amp.

    Handles three arg patterns:
      no args        -> new_call("cuda")
      keyword args   -> new_call("cuda", key=val)
      positional arg -> new_call("cuda", enabled=arg)
    """
    def replacer(match):
        args = match.group(1).strip()
        if not args:
            return f'{new_call}("cuda")'
        if "=" in args:
            return f'{new_call}("cuda", {args})'
        return f'{new_call}("cuda", enabled={args})'
    return replacer


def _grep_and_patch(grep_pattern, patch_fn, label):
    """Run grep -rl to find files, apply patch_fn to each .py file."""
    result = subprocess.run(
        ["grep", "-rl", "--include=*.py", grep_pattern, "."],
        capture_output=True,
        text=True,
    )
    files = [f for f in result.stdout.strip().split("\n") if f]
    patched = 0
    for f in files:
        if patch_fn(f):
            patched += 1
            print(f"  Patched: {f}")
    print(f"[Patch] {label}: {patched} files fixed")


# ============================================================
# Patch functions
# ============================================================

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


def patch_cuda_amp(filepath):
    """Replace deprecated torch.cuda.amp.{autocast,GradScaler} with torch.amp equivalents.

    Also handles aliased usage: `from torch.cuda import amp; amp.autocast(x)`.
    """
    with open(filepath, "r") as f:
        content = f.read()
    original = content

    # Full-path patterns: torch.cuda.amp.autocast(...) / torch.cuda.amp.GradScaler(...)
    content = re.sub(
        r"torch\.cuda\.amp\.autocast\(([^)]*)\)",
        _make_cuda_replacer("torch.amp.autocast"),
        content,
    )
    content = re.sub(
        r"torch\.cuda\.amp\.GradScaler\(([^)]*)\)",
        _make_cuda_replacer("torch.amp.GradScaler"),
        content,
    )

    # Alias pattern: `from torch.cuda import amp` + `amp.autocast(...)` / `amp.GradScaler(...)`
    if re.search(r"from\s+torch\.cuda\s+import\s+amp", content):
        content = re.sub(
            r"(?<!\w)(?<!\.)amp\.autocast\(([^)]*)\)",
            _make_cuda_replacer("torch.amp.autocast"),
            content,
        )
        content = re.sub(
            r"(?<!\w)(?<!\.)amp\.GradScaler\(([^)]*)\)",
            _make_cuda_replacer("torch.amp.GradScaler"),
            content,
        )

    if content != original:
        with open(filepath, "w") as f:
            f.write(content)
        return True
    return False


# ============================================================
# Main
# ============================================================

def main():
    # 1. torch.load patch
    _grep_and_patch("torch.load", patch_torch_load, "torch.load")

    # 2. Pillow getsize patch
    patch_pillow_getsize()

    # 3. torch.cuda.amp deprecation (autocast + GradScaler + alias, single pass)
    _grep_and_patch("torch.cuda.amp\\|amp.autocast\\|amp.GradScaler",
                    patch_cuda_amp, "cuda.amp")


if __name__ == "__main__":
    main()

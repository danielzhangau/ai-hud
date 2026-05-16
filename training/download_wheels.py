#!/usr/bin/env python3
"""
Download Python wheel files using curl with resume support.
Solves the issue of pip failing on unstable VPN connections.

Usage: python3 download_wheels.py
Then:  pip install .wheels/*.whl
"""
import subprocess, sys, re, os, html
from pathlib import Path
from urllib.request import urlopen

WHEELS_DIR = Path(__file__).parent / ".wheels"
WHEELS_DIR.mkdir(exist_ok=True)

PYPI_INDEX = "https://pypi.org/simple"

# Platform tags for macOS arm64 Python 3.12
PLATFORM_TAGS = [
    "cp312-cp312-macosx_11_0_arm64",
    "cp312-cp312-macosx_10_13_universal2",
    "cp312-cp312-macosx_11_0_universal2",
    "cp312-cp312-macosx_10_9_universal2",
    "cp312-cp312-macosx_14_0_arm64",
    "cp312-cp312-macosx_12_0_arm64",
    "cp312-cp312-macosx_13_0_arm64",
    "py3-none-any",
    "py2.py3-none-any",
]

# All packages we need (from requirements.txt + onnxscript)
PACKAGES = [
    "torch", "torchvision", "matplotlib", "numpy", "opencv-python",
    "Pillow", "scipy", "pandas", "seaborn", "tensorboard",
    "gitpython", "ipython", "psutil", "PyYAML", "requests",
    "tqdm", "thop", "onnxscript",
]


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def find_wheel_url(package_name):
    """Find the best wheel URL from PyPI simple index."""
    norm = normalize(package_name)
    url = f"{PYPI_INDEX}/{norm}/"
    try:
        resp = urlopen(url, timeout=30)
        page = resp.read().decode()
    except Exception as e:
        print(f"  [WARN] Cannot fetch index for {package_name}: {e}")
        return None

    # Parse all wheel links
    links = re.findall(r'href="([^"]+\.whl)[^"]*"', page)
    if not links:
        return None

    # Filter for compatible platform
    best = None
    for tag in PLATFORM_TAGS:
        for link in reversed(links):  # reversed = newest first
            fname = link.split("/")[-1].split("#")[0]
            if tag in fname:
                best = link
                break
        if best:
            break

    if best and best.startswith("http"):
        return best.split("#")[0]
    elif best:
        # Relative URL
        return f"https://files.pythonhosted.org/{best}".split("#")[0]
    return None


def curl_download(url, dest):
    """Download with curl, supporting resume (-C -)."""
    fname = os.path.basename(url.split("#")[0])
    out = dest / fname

    max_attempts = 30
    for attempt in range(max_attempts):
        result = subprocess.run(
            ["curl", "-fSL", "-C", "-",
             "--retry", "3", "--retry-delay", "2",
             "--connect-timeout", "30",
             "-o", str(out), url],
            capture_output=False
        )
        if result.returncode == 0:
            size = out.stat().st_size / 1024 / 1024
            print(f"  OK: {fname} ({size:.1f} MB)")
            return str(out)

        if attempt < max_attempts - 1:
            print(f"  Resume attempt {attempt + 2}/{max_attempts}...")

    print(f"  FAILED: {fname}")
    return None


def main():
    print(f"Wheels directory: {WHEELS_DIR}\n")

    downloaded = []
    failed = []

    for pkg in PACKAGES:
        print(f"\n[{pkg}]")

        # Check if already downloaded
        existing = list(WHEELS_DIR.glob(f"{normalize(pkg).replace('-','_')}*.whl")) + \
                   list(WHEELS_DIR.glob(f"{normalize(pkg)}*.whl"))
        if existing:
            print(f"  Already downloaded: {existing[0].name}")
            downloaded.append(str(existing[0]))
            continue

        url = find_wheel_url(pkg)
        if not url:
            print(f"  No compatible wheel found, will install via pip later")
            failed.append(pkg)
            continue

        print(f"  URL: ...{url[-60:]}")
        result = curl_download(url, WHEELS_DIR)
        if result:
            downloaded.append(result)
        else:
            failed.append(pkg)

    print(f"\n{'='*50}")
    print(f"Downloaded: {len(downloaded)} wheels")
    if failed:
        print(f"Failed/skipped: {', '.join(failed)}")

    print(f"\nNext steps:")
    print(f"  source .venv-yolo/bin/activate")
    print(f"  pip install .wheels/*.whl")
    if failed:
        print(f"  pip install {' '.join(failed)}  # small packages, should work")


if __name__ == "__main__":
    main()

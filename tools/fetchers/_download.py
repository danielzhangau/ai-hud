"""HTTP download + raw-snapshot archival.

We use curl (not urllib) for parity with prepare_speed_db.py and
because curl handles HTTPS, retries, and resumable downloads more
robustly than the stdlib's urllib in CI runners.

`download()` caches by destination path -- a re-run will not re-fetch
unless `force=True`. The fetcher CLIs expose `--force` to override.

`archive_raw()` compresses the downloaded blob into
    data/raw/au/<state>/<date>_<name>.gz
so a later cross-verify run can diff month-over-month or roll back
when an upstream provider ships bad data. .gz instead of .zst keeps
the toolchain to stock python + system tools.
"""
from __future__ import annotations

import gzip
import shutil
import subprocess
import time
from pathlib import Path

# Project root: tools/fetchers/_download.py -> ../.. = repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = _REPO_ROOT / "data" / "raw"


def download(url: str, dest: Path, force: bool = False,
             timeout: int = 1800) -> Path:
    """Download `url` to `dest` via curl.

    If `dest` already exists and is non-empty, return it directly --
    skipping the network round-trip. This makes local iteration cheap:
    first run pays the 60-400 MB price, subsequent runs are instant.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not force and dest.exists() and dest.stat().st_size > 0:
        print(f"[download] cached: {dest} "
              f"({dest.stat().st_size/1e6:.1f} MB)")
        return dest

    cmd = [
        "curl", "-fsSL",
        "--max-time", str(timeout),
        "--retry", "2", "--retry-delay", "5",
        "-o", str(dest),
        url,
    ]
    t0 = time.time()
    subprocess.run(cmd, check=True)
    elapsed = time.time() - t0
    size_mb = dest.stat().st_size / 1e6
    print(f"[download] fetched {size_mb:.1f} MB in {elapsed:.1f}s -> {dest}")
    return dest


def archive_raw(src: Path, state: str, basename: str) -> Path:
    """Compress `src` into data/raw/au/<state>/<YYYY-MM-DD>_<basename>.gz.

    Returns the archive path. Idempotent within a single day -- a
    second call with the same args overwrites the prior archive.
    """
    date = time.strftime("%Y-%m-%d")
    out_dir = RAW_ROOT / "au" / state.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}_{basename}.gz"
    with src.open("rb") as fin, gzip.open(out_path, "wb",
                                         compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    return out_path

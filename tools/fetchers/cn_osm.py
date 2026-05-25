"""OSM Overpass fetcher (CN).

China has no open government speed-limit dataset (see `cn_unavailable.py`),
so OSM is the de-facto official source -- not just a cross-check feed.
`OFFICIAL_BITS_CN` in `tools/cross_verify.py` reflects that policy so
single-source OSM segments resolve to OFFICIAL_ONLY (device renders a
number) instead of SINGLE_SOURCE (device renders "--").

City tile bboxes and group-code mappings come from
`tools/speed_db_config.yaml -> regions.cn`. Adding a new city is YAML-only.
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

try:
    import yaml
except ImportError as _e:
    raise SystemExit(
        "cn_osm requires PyYAML to read the city bbox config; "
        "install with: pip install pyyaml"
    ) from _e

from .base import SourceFetcher
from ._download import archive_bytes
from ._overpass import build_query, parse_ways, run_query
from .ir import SpeedSegment

LOG = "[cn_osm]"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _REPO_ROOT / "tools" / "speed_db_config.yaml"

# Sentinel returned when a tile name matches no prefix in city_codes.
# Surfaces in the per-city quarantine breakdown so an unmapped tile
# is visible rather than silently lumped with a real city.
_DEFAULT_CITY_CODE = "CN_OTHER"


def _load_cn_config(config_path: Path = _DEFAULT_CONFIG) -> tuple[dict, dict]:
    """Return (cities, city_codes) from the shared speed DB config.

    Raises SystemExit on missing file or empty `cities` -- we never
    want to silently produce an empty CN DB.
    """
    try:
        cfg = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError as e:
        raise SystemExit(f"{LOG} config not found: {config_path}") from e
    cn = (cfg.get("regions") or {}).get("cn") or {}
    cities = cn.get("cities") or {}
    if not cities:
        raise SystemExit(
            f"{LOG} no cities defined in {config_path} under "
            "regions.cn.cities -- refusing to build an empty CN DB"
        )
    city_codes = cn.get("city_codes") or {}
    return ({name: list(bbox) for name, bbox in cities.items()},
            dict(city_codes))


def _resolve_city_code(tile: str, city_codes: dict[str, str]) -> str:
    """Longest-prefix match `tile` against the YAML mapping.

    Longest first so "Xi'an_CBD" maps to XIA before any shorter prefix
    that happens to be a substring.
    """
    for prefix in sorted(city_codes.keys(), key=len, reverse=True):
        if tile.startswith(prefix):
            return city_codes[prefix]
    return _DEFAULT_CITY_CODE


class OSMCNFetcher(SourceFetcher):
    name = "OSM"
    state = "CN"

    def __init__(self, cities: list[str] | None = None,
                 sample_interval_m: float = 100.0,
                 delay_between_cities_s: float = 3.0,
                 config_path: Path = _DEFAULT_CONFIG):
        all_bbox, self.city_codes = _load_cn_config(config_path)
        if cities:
            unknown = [c for c in cities if c not in all_bbox]
            if unknown:
                raise SystemExit(
                    f"{LOG} unknown CN cities {unknown}; "
                    f"available: {sorted(all_bbox.keys())}"
                )
            selected = cities
        else:
            selected = list(all_bbox.keys())
        # Narrow to selected tiles so fetch() can iterate the dict
        # directly -- no separate self.cities list needed.
        self.bbox = {c: all_bbox[c] for c in selected}
        self.interval = sample_interval_m
        self.delay = delay_between_cities_s

    @property
    def cities(self) -> list[str]:
        return list(self.bbox.keys())

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        all_segments: list[SpeedSegment] = []
        for city, bbox in self.bbox.items():
            code = _resolve_city_code(city, self.city_codes)
            try:
                raw = run_query(build_query(bbox), city, log_prefix=LOG)
            except subprocess.CalledProcessError as exc:
                print(f"{LOG} Overpass for {city} failed: "
                      f"{exc.returncode}; continuing")
                continue
            if archive:
                # Per-city archive: keeps each query's raw JSON as its
                # own .gz instead of accumulating decoded copies in memory.
                arc = archive_bytes(raw, "cn_osm",
                                    f"{city.lower()}_overpass.json",
                                    region="cn")
                print(f"{LOG} archived: {arc} "
                      f"({arc.stat().st_size/1e6:.1f} MB)")
            all_segments.extend(parse_ways(
                raw, group=code,
                sample_interval_m=self.interval,
                log_prefix=LOG))
            if limit and len(all_segments) >= limit:
                break
            time.sleep(self.delay)

        # "generated N segments" phrase consumed by fetcher-smoke regex.
        print(f"{LOG} generated {len(all_segments):,} segments "
              f"across {len(self.bbox)} city tiles")
        return all_segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="",
                    help="comma-separated city tile names (default: all)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--interval", type=float, default=100.0)
    args = ap.parse_args()
    cities = [c.strip() for c in args.cities.split(",") if c.strip()] or None
    fetcher = OSMCNFetcher(cities=cities, sample_interval_m=args.interval)
    segs = fetcher.fetch(limit=args.limit, archive=not args.no_archive)
    print("--- summary ---")
    print(f"total: {len(segs):,}")
    if segs:
        speeds: dict[int, int] = {}
        for s in segs:
            speeds[s.speed] = speeds.get(s.speed, 0) + 1
        for sp in sorted(speeds):
            print(f"  {sp:3d} km/h: {speeds[sp]:>10,}")


if __name__ == "__main__":
    _main()

"""NSW Speed Zones fetcher (TfNSW Open Data Hub).

Dataset:       opendata.transport.nsw.gov.au/dataset/speed-zones
Resource:      Speed Zones - GeoJSON Format (~365 MB)
CKAN package:  4253a054-b377-4b5b-83d1-71385bb6ff33
GeoJSON res:   bc2da977-65d2-4caa-a73a-48d0c8bf1100
License:       Creative Commons Attribution (CC BY)
Update:        Ad-hoc; last seen 2026-05-19 per CKAN metadata_modified.

Schema per feature.properties (verified 2026-05-20):
    Type        Default | School | High Pedestrian | Variable |
                Toll Plaza | Truck & bus | Wet Weather | ...
    Status      "Existing" / ...
    Direction   "Both Directions" | "One way" | ...
    Speed       "100 km/h"  (string, always with unit)

"宁可不报" policy:
  * Type=="School"   -- 40 km/h applies only school hours (sign times
    posted on the physical sign). Treating it as a permanent 40
    under-reports for ~22 of every 24 hours. Skip until Sprint 2
    introduces time_mask.
  * Type=="Variable" -- speed changes with conditions (e.g. tunnel
    fan flow). The single Speed value in the dataset is the most
    common, not the active value. Skip for safety.
  * Type=="Wet Weather" -- only applies when wet. Skip; we don't
    have weather inputs.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from .base import SourceFetcher
from ._download import archive_raw, download
from ._geometry import bearing_of_segment, sample_linestring
from .ir import BEARING_UNKNOWN, ROAD_OTHER, SpeedSegment

GEOJSON_URL = (
    "https://opendata.transport.nsw.gov.au/data/dataset/"
    "4253a054-b377-4b5b-83d1-71385bb6ff33/resource/"
    "bc2da977-65d2-4caa-a73a-48d0c8bf1100/download/speed_zones.geojson"
)

# Strict single-value match. "100 km/h" -> 100. Reject anything else
# even if it contains digits (defense-in-depth against schema drift).
SPEED_RE = re.compile(r"^\s*(\d+)\s*km/h\s*$", re.IGNORECASE)

# Categories with time- or condition-dependent limits. See module
# docstring for rationale. Sprint 2 will add time_mask support for
# School / Wet Weather; Variable stays excluded.
SKIP_TYPES = {"School", "Variable", "Wet Weather"}


class NSWFetcher(SourceFetcher):
    name = "AU_NSW_GOV"
    state = "NSW"

    def __init__(self, sample_interval_m: float = 80.0):
        self.interval = sample_interval_m

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        tmp = Path("/tmp/_au_nsw_speed_zones.geojson")
        print(f"[au_nsw] source: {GEOJSON_URL}")
        download(GEOJSON_URL, tmp)

        if archive:
            arc = archive_raw(tmp, self.state, "speed_zones.geojson")
            print(f"[au_nsw] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        # 365 MB GeoJSON; json.load peaks at ~1.5 GB RSS. CI runners
        # (7 GB) and dev Macs handle this fine. Switching to ijson is
        # tracked in the plan but punted per YAGNI.
        with tmp.open("rb") as f:
            data = json.load(f)

        features = data.get("features", [])
        print(f"[au_nsw] {len(features):,} features in source")

        segments: list[SpeedSegment] = []
        skipped_type = 0
        skipped_speed = 0
        skipped_geom = 0
        now = int(time.time())

        for feat in features:
            props = feat.get("properties") or {}
            sz_type = (props.get("Type")
                       or props.get("sz_type")
                       or "").strip()
            if sz_type in SKIP_TYPES:
                skipped_type += 1
                continue

            speed_str = (props.get("Speed")
                         or props.get("sz_speed")
                         or "").strip()
            m = SPEED_RE.match(speed_str)
            if not m:
                skipped_speed += 1
                continue
            speed = int(m.group(1))
            if speed <= 0 or speed > 130:
                skipped_speed += 1
                continue

            geom = feat.get("geometry") or {}
            if geom.get("type") != "LineString":
                skipped_geom += 1
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                skipped_geom += 1
                continue

            direction = (props.get("Direction") or "").strip()
            if direction == "Both Directions" or not direction:
                bearing = BEARING_UNKNOWN
            else:
                bearing = bearing_of_segment(coords)

            for lat, lon in sample_linestring(coords, self.interval):
                segments.append(SpeedSegment(
                    lat=lat,
                    lon=lon,
                    speed=speed,
                    road_type=ROAD_OTHER,
                    bearing=bearing,
                    state=self.state,
                    source=self.name,
                    time_mask=0,
                    fetched_at=now,
                ))

            if limit and len(segments) >= limit:
                break

        print(f"[au_nsw] generated {len(segments):,} segments "
              f"(skipped {skipped_type} time-conditional, "
              f"{skipped_speed} bad speed, "
              f"{skipped_geom} bad geometry)")
        return segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Early-stop after roughly N segments (smoke testing)")
    ap.add_argument("--no-archive", action="store_true",
                    help="Skip the gzip snapshot under data/raw/")
    ap.add_argument("--interval", type=float, default=80.0,
                    help="LineString sample interval in metres (default 30)")
    args = ap.parse_args()

    fetcher = NSWFetcher(sample_interval_m=args.interval)
    segs = fetcher.fetch(limit=args.limit, archive=not args.no_archive)

    print("--- summary ---")
    print(f"total segments: {len(segs):,}")
    if segs:
        speeds: dict[int, int] = {}
        for s in segs:
            speeds[s.speed] = speeds.get(s.speed, 0) + 1
        for sp in sorted(speeds):
            print(f"  {sp:3d} km/h: {speeds[sp]:>10,}")
        print(f"first segment: {segs[0]}")
        print(f"last segment : {segs[-1]}")


if __name__ == "__main__":
    _main()

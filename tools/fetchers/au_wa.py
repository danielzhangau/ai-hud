"""WA Legal Speed Limits fetcher (Main Roads Western Australia).

Dataset:       portal-mainroads.opendata.arcgis.com/datasets/mainroads::legal-speed-limits
ArcGIS item:   c30239d960df4eaf85890201068ea521 (layer 8)
License:       CC BY 4.0
Update:        Weekly (IRIS-triggered refresh of the ArcGIS layer).

Schema per feature.properties (verified 2026-05-20):
    ROAD                "2110042"      -- internal road id
    ROAD_NAME           "Hacket Rd"
    COMMON_USAGE_NAME   "Hacket Rd"
    START_SLK, END_SLK  Straight Line Kilometre offsets
    CWY                 "Single" | "Dual"
    NETWORK_TYPE        "Local Road" | "State Road" | "Main Road" |
                        "Highway" | "Freeway" | "National Highway"
    SPEED_LIMIT         "60km/h" -- OR descriptive multi-value
                        ("50km/h applies in built up areas or
                         110km/h outside built up areas")
    GEOLOCSTLength      km of geometry

Source disclaimer (mandatory citation):
  Main Roads WA explicitly states this dataset is NOT for navigation
  systems and the "only enforceable source is the road sign". We
  treat WA as a cross-verification feed for OSM/NSW overlap and as a
  primary source only when the SPEED_LIMIT value is unambiguous.

"宁可不报" policy:
  * Reject any SPEED_LIMIT that doesn't match `<digits> km/h`. The
    multi-value descriptive entries dominate Local Roads (~30% of
    features per our 2026-05-20 sampling); they convey policy, not a
    posted value for the segment.
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
from .ir import (
    BEARING_UNKNOWN,
    ROAD_MOTORWAY,
    ROAD_OTHER,
    ROAD_PRIMARY,
    ROAD_TRUNK,
    SpeedSegment,
)

GEOJSON_URL = (
    "https://portal-mainroads.opendata.arcgis.com/api/download/v1/"
    "items/c30239d960df4eaf85890201068ea521/geojson?layers=8"
)

# Strict match -- a single, unambiguous "<digits>km/h". Tolerates
# optional spaces but rejects multi-value descriptions. Without
# anchors this would happily pick the first number out of
# "50km/h applies in ..." which is exactly the wrong behaviour
# under "宁可不报".
SPEED_SINGLE_RE = re.compile(r"^\s*(\d+)\s*km/h\s*$", re.IGNORECASE)

NETWORK_TO_ROADTYPE = {
    "Highway": ROAD_MOTORWAY,
    "Freeway": ROAD_MOTORWAY,
    "National Highway": ROAD_TRUNK,
    "State Road": ROAD_TRUNK,
    "Main Road": ROAD_PRIMARY,
    "Local Road": ROAD_OTHER,
}


class WAFetcher(SourceFetcher):
    name = "AU_WA_GOV"
    state = "WA"

    def __init__(self, sample_interval_m: float = 100.0):
        self.interval = sample_interval_m

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        tmp = Path("/tmp/_au_wa_speed_limits.geojson")
        print(f"[au_wa] source: {GEOJSON_URL}")
        download(GEOJSON_URL, tmp)

        if archive:
            arc = archive_raw(tmp, self.state, "legal_speed_limits.geojson")
            print(f"[au_wa] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        with tmp.open("rb") as f:
            data = json.load(f)

        features = data.get("features", [])
        print(f"[au_wa] {len(features):,} features in source")

        segments: list[SpeedSegment] = []
        skipped_descriptive = 0
        skipped_empty = 0
        skipped_geom = 0
        now = int(time.time())

        for feat in features:
            props = feat.get("properties") or {}
            speed_str = (props.get("SPEED_LIMIT") or "").strip()
            if not speed_str:
                skipped_empty += 1
                continue
            m = SPEED_SINGLE_RE.match(speed_str)
            if not m:
                # Multi-value description or some unexpected text.
                skipped_descriptive += 1
                continue
            speed = int(m.group(1))
            if speed <= 0 or speed > 130:
                skipped_descriptive += 1
                continue

            road_type = NETWORK_TO_ROADTYPE.get(
                (props.get("NETWORK_TYPE") or "").strip(),
                ROAD_OTHER,
            )

            geom = feat.get("geometry") or {}
            if geom.get("type") != "LineString":
                skipped_geom += 1
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                skipped_geom += 1
                continue

            cwy = (props.get("CWY") or "").strip()
            # "Single" carriageway shares both directions on one
            # geometry -> bearing irrelevant. "Dual" has separate
            # geometry per direction so we record bearing.
            if cwy == "Single" or not cwy:
                bearing = BEARING_UNKNOWN
            else:
                bearing = bearing_of_segment(coords)

            for lat, lon in sample_linestring(coords, self.interval):
                segments.append(SpeedSegment(
                    lat=lat,
                    lon=lon,
                    speed=speed,
                    road_type=road_type,
                    bearing=bearing,
                    state=self.state,
                    source=self.name,
                    time_mask=0,
                    fetched_at=now,
                ))

            if limit and len(segments) >= limit:
                break

        print(f"[au_wa] generated {len(segments):,} segments "
              f"(skipped {skipped_descriptive} multi-value, "
              f"{skipped_empty} empty, "
              f"{skipped_geom} bad geometry)")
        return segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Early-stop after roughly N segments (smoke testing)")
    ap.add_argument("--no-archive", action="store_true",
                    help="Skip the gzip snapshot under data/raw/")
    ap.add_argument("--interval", type=float, default=100.0,
                    help="LineString sample interval in metres (default 30)")
    args = ap.parse_args()

    fetcher = WAFetcher(sample_interval_m=args.interval)
    segs = fetcher.fetch(limit=args.limit, archive=not args.no_archive)

    print("--- summary ---")
    print(f"total segments: {len(segs):,}")
    if segs:
        speeds: dict[int, int] = {}
        rtypes: dict[int, int] = {}
        for s in segs:
            speeds[s.speed] = speeds.get(s.speed, 0) + 1
            rtypes[s.road_type] = rtypes.get(s.road_type, 0) + 1
        for sp in sorted(speeds):
            print(f"  {sp:3d} km/h: {speeds[sp]:>10,}")
        print(f"road type distribution: {rtypes}")
        print(f"first segment: {segs[0]}")


if __name__ == "__main__":
    _main()

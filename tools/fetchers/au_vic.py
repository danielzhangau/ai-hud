"""VIC Speed Zones fetcher (Department of Transport and Planning).

Dataset:       opendata.transport.vic.gov.au/dataset/speed-zones
CKAN package:  975b80b9-e530-46e2-80a5-54002765e81a
License:       CC BY 4.0
Update:        Monthly. The portal keeps every historical month as a
               separate resource (Speed Zones April 2026, March 2026,
               February 2026, ...). We resolve the latest month at
               runtime via CKAN's package_show API instead of hard-
               coding a URL that goes stale every 30 days.

Schema per feature.properties (verified 2026-05-20 on April 2026 release):
    SPEED_ZONE / SPEED / speed / speed_zone / SPEED_LIMIT / POSTED_SPEED
    -- field name has drifted across releases; the existing
       prepare_speed_db.py probes them all in order. We mirror that.
    Plus segment geometry as LineString / MultiLineString.

"宁可不报" policy:
  * Reject features without a single numeric speed value (no school
    or variable handling here -- the dataset's posted-speed value is
    the 24h minimum per the portal docs, which matches our policy of
    showing the conservative limit when in doubt).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from .base import SourceFetcher
from ._download import archive_raw, download
from ._geometry import bearing_of_segment, sample_linestring
from .ir import BEARING_UNKNOWN, ROAD_OTHER, SpeedSegment

CKAN_PACKAGE_URL = (
    "https://opendata.transport.vic.gov.au/api/3/action/package_show"
    "?id=speed-zones"
)

# Field name drift across releases -- probe in this priority order.
SPEED_FIELD_KEYS = (
    "SPEED_ZONE", "SPEED", "speed_zone", "speed",
    "SPEED_LIMIT", "speed_limit", "POSTED_SPEED",
)

# Accept "60" or "60 km/h" or "60km/h". Strict anchors -- nothing else.
SPEED_VALUE_RE = re.compile(r"^\s*(\d+)(?:\s*km/h)?\s*$", re.IGNORECASE)


def _resolve_latest_resource() -> tuple[str, str]:
    """Query CKAN for the freshest GeoJSON resource URL + filename.

    Returns (url, basename). Raises RuntimeError if no GeoJSON found.
    """
    result = subprocess.run(
        ["curl", "-fsSL", "--max-time", "60", CKAN_PACKAGE_URL],
        check=True, capture_output=True,
    )
    data = json.loads(result.stdout)
    if not data.get("success"):
        raise RuntimeError(f"VIC CKAN package_show failed: {data}")

    resources = data["result"].get("resources", [])
    # Pick newest GeoJSON by created date. The "Speed Zones <Month YYYY>"
    # naming carries the month inside the URL too, but `created` ordering
    # is more robust than parsing month names out of resource titles.
    geojson_res = [
        r for r in resources
        if (r.get("format") or "").strip().upper() == "GEOJSON"
    ]
    if not geojson_res:
        raise RuntimeError("VIC: no GeoJSON resource found in package")

    geojson_res.sort(key=lambda r: r.get("created") or "", reverse=True)
    chosen = geojson_res[0]
    url = chosen["url"]
    name = chosen.get("name", "").strip() or "speed_zones.geojson"
    print(f"[au_vic] latest resource: '{name}' "
          f"(created {chosen.get('created', '?')})")
    return url, name


class VICFetcher(SourceFetcher):
    name = "AU_VIC_GOV"
    state = "VIC"

    def __init__(self, sample_interval_m: float = 80.0):
        self.interval = sample_interval_m

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        url, _ = _resolve_latest_resource()
        print(f"[au_vic] source: {url}")

        tmp = Path("/tmp/_au_vic_speed_zones.geojson")
        download(url, tmp)

        if archive:
            arc = archive_raw(tmp, self.state, "speed_zones.geojson")
            print(f"[au_vic] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        # April 2026 release is ~670 MB. Peak RSS during json.load is
        # ~3 GB; CI ubuntu-22.04 runners (7 GB) clear that comfortably.
        # Future Sprint may switch to ijson if a smaller runner is needed.
        with tmp.open("rb") as f:
            data = json.load(f)

        features = data.get("features", [])
        print(f"[au_vic] {len(features):,} features in source")

        segments: list[SpeedSegment] = []
        skipped_speed = 0
        skipped_geom = 0
        now = int(time.time())

        for feat in features:
            props = feat.get("properties") or {}
            speed = 0
            for key in SPEED_FIELD_KEYS:
                val = props.get(key)
                if val is None:
                    continue
                m = SPEED_VALUE_RE.match(str(val))
                if m:
                    speed = int(m.group(1))
                    break
            if speed <= 0 or speed > 130:
                skipped_speed += 1
                continue

            geom = feat.get("geometry") or {}
            gtype = geom.get("type", "")
            if gtype == "LineString":
                coord_lists = [geom.get("coordinates") or []]
            elif gtype == "MultiLineString":
                coord_lists = geom.get("coordinates") or []
            else:
                skipped_geom += 1
                continue

            emitted = False
            for coords in coord_lists:
                if len(coords) < 2:
                    continue
                bearing = bearing_of_segment(coords)
                # VIC GeoJSON is single-direction per feature when
                # the same road segment carries different limits per
                # direction. When the bearing endpoints coincide
                # (closed loop), bearing_of_segment returns the
                # sentinel which downstream treats as bidirectional.
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
                    emitted = True

            if not emitted:
                skipped_geom += 1
                continue

            if limit and len(segments) >= limit:
                break

        print(f"[au_vic] generated {len(segments):,} segments "
              f"(skipped {skipped_speed} bad speed, "
              f"{skipped_geom} bad geometry)")
        return segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--interval", type=float, default=80.0)
    args = ap.parse_args()
    fetcher = VICFetcher(sample_interval_m=args.interval)
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

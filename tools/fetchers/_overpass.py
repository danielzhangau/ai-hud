"""Shared Overpass-API plumbing for OSM-based fetchers.

`au_osm.py` and `cn_osm.py` both hit OpenStreetMap's Overpass API for
maxspeed-tagged ways, then parse them into `SpeedSegment` records via
identical rules. The duplication used to live in both files; this
module is the single source.

Public surface:
  * Constants: OVERPASS_API, HIGHWAY_FILTER, HIGHWAY_TO_ROADTYPE
  * build_query(bbox, timeout=180) -> str
  * run_query(query, label, *, log_prefix) -> bytes
  * parse_ways(raw, *, group, sample_interval_m, log_prefix) -> list[SpeedSegment]
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from ._geometry import bearing_of_segment, sample_linestring
from .ir import (
    BEARING_UNKNOWN,
    ROAD_MOTORWAY,
    ROAD_OTHER,
    ROAD_PRIMARY,
    ROAD_RESIDENTIAL,
    ROAD_SECONDARY,
    ROAD_TRUNK,
    SpeedSegment,
)

OVERPASS_API = "https://overpass-api.de/api/interpreter"

HIGHWAY_FILTER = (
    "motorway|motorway_link|trunk|trunk_link|primary|primary_link|"
    "secondary|secondary_link|tertiary|tertiary_link|residential|"
    "living_street|unclassified|service"
)

HIGHWAY_TO_ROADTYPE = {
    "motorway": ROAD_MOTORWAY,    "motorway_link": ROAD_MOTORWAY,
    "trunk":    ROAD_TRUNK,       "trunk_link":    ROAD_TRUNK,
    "primary":  ROAD_PRIMARY,     "primary_link":  ROAD_PRIMARY,
    "secondary": ROAD_SECONDARY,  "secondary_link": ROAD_SECONDARY,
    "tertiary":  ROAD_SECONDARY,  "tertiary_link":  ROAD_SECONDARY,
    "residential": ROAD_RESIDENTIAL,
    "living_street": ROAD_RESIDENTIAL,
    "unclassified": ROAD_OTHER,
    "service": ROAD_OTHER,
}


def build_query(bbox: list[float], timeout: int = 180) -> str:
    s, w, n, e = bbox
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'way["maxspeed"]["highway"~"{HIGHWAY_FILTER}"]'
        f"({s},{w},{n},{e});\n"
        "out geom;\n"
    )


def run_query(query: str, label: str, *, log_prefix: str) -> bytes:
    """POST `query` to Overpass via curl and return the raw JSON bytes.

    Overpass returns HTTP 406 to UA-less requests (verified 2026-05-21);
    a self-identifying UA is also the project's good-citizen tag.
    Uses a NamedTemporaryFile so concurrent AU/CN runs cannot collide
    on a shared /tmp path.
    """
    with tempfile.NamedTemporaryFile(
            "w", prefix="_overpass_", suffix=".txt", delete=False) as tmp:
        tmp.write(query)
        tmp_path = tmp.name
    try:
        cmd = [
            "curl", "-fsSL",
            "--max-time", "600",
            "--retry", "2", "--retry-delay", "5",
            "-A", "ai-hud/1.0 (+https://github.com/danielzhangau/ai-hud)",
            "--data-urlencode", f"data@{tmp_path}",
            OVERPASS_API,
        ]
        print(f"{log_prefix}   querying Overpass for {label}...")
        t0 = time.time()
        result = subprocess.run(cmd, check=True, capture_output=True)
        elapsed = time.time() - t0
        print(f"{log_prefix}     {len(result.stdout)/1e6:.1f} MB in {elapsed:.1f}s")
        return result.stdout
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def parse_ways(raw: bytes, *, group: str, sample_interval_m: float,
               log_prefix: str) -> list[SpeedSegment]:
    """Parse an Overpass `out geom` response into SpeedSegment records.

    `group` is stamped on every emitted record's `.state` field --
    AU passes a state code (NSW/VIC/...), CN passes a city code
    (XIA/SHA/...). Same value, different meaning per caller.
    """
    data = json.loads(raw)
    elements = data.get("elements", [])
    ways = [e for e in elements if e.get("type") == "way"]
    print(f"{log_prefix}   {group}: {len(ways):,} ways")

    segments: list[SpeedSegment] = []
    skipped_speed = 0
    skipped_geom = 0
    now = int(time.time())

    for way in ways:
        tags = way.get("tags") or {}
        maxspeed_str = (tags.get("maxspeed") or "").strip()

        # Tolerate "60" and "60 km/h"; reject "AU:urban" etc.
        speed = 0
        try:
            speed = int(maxspeed_str)
        except ValueError:
            for part in maxspeed_str.split():
                try:
                    speed = int(part)
                    break
                except ValueError:
                    continue
        if speed <= 0 or speed > 130:
            skipped_speed += 1
            continue

        highway = (tags.get("highway") or "").strip()
        road_type = HIGHWAY_TO_ROADTYPE.get(highway, ROAD_OTHER)

        geom = way.get("geometry") or []
        coords = [[pt.get("lon"), pt.get("lat")] for pt in geom
                  if "lat" in pt and "lon" in pt]
        if len(coords) < 2:
            skipped_geom += 1
            continue

        if tags.get("oneway") in ("yes", "-1", "true"):
            bearing = bearing_of_segment(coords)
        else:
            bearing = BEARING_UNKNOWN

        for lat, lon in sample_linestring(coords, sample_interval_m):
            segments.append(SpeedSegment(
                lat=lat, lon=lon, speed=speed,
                road_type=road_type, bearing=bearing,
                state=group, source="OSM",
                time_mask=0, fetched_at=now,
            ))

    print(f"{log_prefix}   {group}: {len(segments):,} segments "
          f"(skipped {skipped_speed} bad speed, {skipped_geom} bad geom)")
    return segments

"""OSM Overpass fetcher.

Pulls maxspeed-tagged ways from OpenStreetMap via the Overpass API and
emits SpeedSegment records. Used as the *cross-verification* feed in
the AU pipeline -- not as a primary source, because OSM is community-
curated and prone to occasional vandalism (residential street briefly
tagged maxspeed=300, etc.).

Per the user's selected policy ("官方源优先 + OSM 仅作交叉验证"):
  * A record observed by an AU state fetcher AND by OSM at the same
    location is promoted to CONFIDENCE_OFFICIAL_VERIFIED.
  * A record observed only by OSM stays SINGLE_SOURCE, which the
    device UI renders as "--" (per "宁可不报").

Implementation note: this module deliberately reuses the same
Overpass query shape that `prepare_speed_db.py` has battle-tested
since the early builds. We only wrap the output in SpeedSegment
records (no behaviour change) so the legacy CN path keeps working
unchanged through prepare_speed_db.py while the AU path moves to
build_db.py.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path

from .base import SourceFetcher
from ._download import archive_raw
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

# Highway tag filter -- skip footways/cycleways/raceways. Same set the
# legacy builder used so cross-verify can find both halves of the join.
HIGHWAY_FILTER = (
    "motorway|motorway_link|trunk|trunk_link|primary|primary_link|"
    "secondary|secondary_link|tertiary|tertiary_link|residential|"
    "living_street|unclassified|service"
)

# AU bounding boxes per state. Tight bboxes avoid pulling the entire
# planet -- Overpass refuses queries that grow past its memory budget.
# Format: [south, west, north, east]
AU_STATE_BBOX = {
    "NSW": [-37.6, 140.9, -28.1, 153.7],
    "VIC": [-39.2, 140.9, -33.9, 150.0],
    "QLD": [-29.2, 137.9, -9.1, 153.6],
    "WA":  [-35.2, 112.9, -13.6, 129.1],
    "SA":  [-38.1, 129.0, -25.9, 141.1],
    "ACT": [-35.95, 148.75, -35.10, 149.40],
    "NT":  [-26.1, 129.0, -10.9, 138.1],
    "TAS": [-43.7, 143.8, -39.5, 148.5],
}

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


def _overpass_query(bbox: list[float], timeout: int = 180) -> str:
    """Render the Overpass-QL query for one state bbox."""
    s, w, n, e = bbox
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'way["maxspeed"]["highway"~"{HIGHWAY_FILTER}"]'
        f"({s},{w},{n},{e});\n"
        "out geom;\n"
    )


def _run_overpass(query: str, label: str) -> bytes:
    """POST the query to Overpass via curl. Returns raw JSON bytes.

    Overpass returns HTTP 406 to requests without a User-Agent
    (verified 2026-05-21); the public instance treats UA-less hits as
    abuse. Sending a self-identifying UA is also the project's good-
    citizen tag in the Overpass usage policy.
    """
    tmp_query = Path("/tmp/_overpass_query.txt")
    tmp_query.write_text(query)
    cmd = [
        "curl", "-fsSL",
        "--max-time", "600",
        "--retry", "2", "--retry-delay", "5",
        "-A", "ai-hud/1.0 (+https://github.com/danielzhangau/ai-hud)",
        "--data-urlencode", f"data@{tmp_query}",
        OVERPASS_API,
    ]
    print(f"[au_osm]   querying Overpass for {label}...")
    t0 = time.time()
    result = subprocess.run(cmd, check=True, capture_output=True)
    elapsed = time.time() - t0
    print(f"[au_osm]     {len(result.stdout)/1e6:.1f} MB in {elapsed:.1f}s")
    return result.stdout


def _parse_overpass(raw: bytes, state: str,
                    interval_m: float) -> list[SpeedSegment]:
    """Parse Overpass JSON -> SpeedSegment list. Reuses the legacy
    builder's maxspeed parsing rules; see prepare_speed_db.py."""
    data = json.loads(raw)
    elements = data.get("elements", [])
    ways = [e for e in elements if e.get("type") == "way"]
    print(f"[au_osm]   {state}: {len(ways):,} ways")

    segments: list[SpeedSegment] = []
    skipped_speed = 0
    skipped_geom = 0
    now = int(time.time())

    for way in ways:
        tags = way.get("tags") or {}
        maxspeed_str = (tags.get("maxspeed") or "").strip()

        # Tolerate "60", "60 km/h", and split on space for tagged
        # variants. Reject anything else (e.g. "AU:urban").
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

        # OSM tags `oneway=yes/-1` or default bidirectional; for the
        # cross-verify radius the bearing is mostly informational, so
        # treat plain ways as bidirectional unless tagged explicitly.
        if tags.get("oneway") in ("yes", "-1", "true"):
            bearing = bearing_of_segment(coords)
        else:
            bearing = BEARING_UNKNOWN

        for lat, lon in sample_linestring(coords, interval_m):
            segments.append(SpeedSegment(
                lat=lat, lon=lon, speed=speed,
                road_type=road_type, bearing=bearing,
                state=state, source="OSM",
                time_mask=0, fetched_at=now,
            ))

    print(f"[au_osm]   {state}: {len(segments):,} segments "
          f"(skipped {skipped_speed} bad speed, {skipped_geom} bad geom)")
    return segments


class OSMAUFetcher(SourceFetcher):
    name = "OSM"
    state = "AU"   # multi-state fetcher; per-segment .state is set per query

    def __init__(self, states: list[str] | None = None,
                 sample_interval_m: float = 100.0,
                 delay_between_states_s: float = 3.0):
        # `states=None` means every AU state; smoke tests pass a short list
        # to avoid Overpass rate-limits on iteration.
        self.states = ([s.upper() for s in states] if states
                       else list(AU_STATE_BBOX.keys()))
        self.interval = sample_interval_m
        self.delay = delay_between_states_s

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        all_segments: list[SpeedSegment] = []
        all_raw: list[dict] = []
        for state in self.states:
            bbox = AU_STATE_BBOX.get(state)
            if not bbox:
                print(f"[au_osm] unknown state '{state}', skipping")
                continue
            query = _overpass_query(bbox)
            try:
                raw = _run_overpass(query, state)
            except subprocess.CalledProcessError as exc:
                print(f"[au_osm] Overpass for {state} failed: "
                      f"{exc.returncode}; continuing")
                continue
            all_raw.append({"state": state, "raw": raw.decode("utf-8", errors="replace")})
            all_segments.extend(_parse_overpass(raw, state, self.interval))
            if limit and len(all_segments) >= limit:
                break
            time.sleep(self.delay)

        if archive and all_raw:
            tmp = Path("/tmp/_au_osm_overpass.json")
            with tmp.open("w") as f:
                json.dump(all_raw, f, separators=(",", ":"))
            arc = archive_raw(tmp, "AU_OSM", "overpass.json")
            print(f"[au_osm] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        print(f"[au_osm] total: {len(all_segments):,} segments "
              f"across {len(self.states)} states")
        return all_segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default="",
                    help="comma-separated state codes (default: all 8)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--interval", type=float, default=100.0)
    args = ap.parse_args()
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    fetcher = OSMAUFetcher(states=states,
                           sample_interval_m=args.interval)
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

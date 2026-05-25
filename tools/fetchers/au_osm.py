"""OSM Overpass fetcher (AU).

Pulls maxspeed-tagged ways from OpenStreetMap via the Overpass API for
each AU state bbox. Used by `build_db.build_au` as the cross-verify
feed -- OSM is community-curated, so a record observed by OSM AND a
state fetcher is promoted to OFFICIAL_VERIFIED, OSM-only stays at
SINGLE_SOURCE (rendered as "--" on device per "宁可不报").

The Overpass HTTP plumbing + JSON parsing lives in `_overpass.py`,
shared with `cn_osm.py`.
"""
from __future__ import annotations

import argparse
import subprocess
import time

from .base import SourceFetcher
from ._download import archive_bytes
from ._overpass import build_query, parse_ways, run_query
from .ir import SpeedSegment

LOG = "[au_osm]"

# Tight per-state bboxes -- Overpass refuses queries beyond its memory
# budget. Format: [south, west, north, east]
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


class OSMAUFetcher(SourceFetcher):
    name = "OSM"
    state = "AU"

    def __init__(self, states: list[str] | None = None,
                 sample_interval_m: float = 100.0,
                 delay_between_states_s: float = 3.0):
        self.states = ([s.upper() for s in states] if states
                       else list(AU_STATE_BBOX.keys()))
        self.interval = sample_interval_m
        self.delay = delay_between_states_s

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        all_segments: list[SpeedSegment] = []
        for state in self.states:
            bbox = AU_STATE_BBOX.get(state)
            if not bbox:
                print(f"{LOG} unknown state '{state}', skipping")
                continue
            try:
                raw = run_query(build_query(bbox), state, log_prefix=LOG)
            except subprocess.CalledProcessError as exc:
                print(f"{LOG} Overpass for {state} failed: "
                      f"{exc.returncode}; continuing")
                continue
            if archive:
                # Per-state archive avoids accumulating ~50MB of decoded
                # JSON across 8 states in memory before writing once.
                arc = archive_bytes(raw, "AU_OSM",
                                    f"{state.lower()}_overpass.json",
                                    region="au")
                print(f"{LOG} archived: {arc} "
                      f"({arc.stat().st_size/1e6:.1f} MB)")
            all_segments.extend(parse_ways(
                raw, group=state,
                sample_interval_m=self.interval,
                log_prefix=LOG))
            if limit and len(all_segments) >= limit:
                break
            time.sleep(self.delay)

        # "generated N segments" phrase consumed by fetcher-smoke regex.
        print(f"{LOG} generated {len(all_segments):,} segments "
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
    fetcher = OSMAUFetcher(states=states, sample_interval_m=args.interval)
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

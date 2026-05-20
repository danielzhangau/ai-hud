"""ACT Speed Zones fetcher (Transport Canberra and City Services).

Dataset:       data.act.gov.au/Transport/ACT-Speed-Zones/hy95-2hum
Backend:       Socrata Open Data API (SODA)
License:       CC BY-NC-SA (per ACT data portal default) -- see
               dataset page for the authoritative license string.
Update:        Ad-hoc.

Schema (verified 2026-05-20):
    the_geom     MultiLineString
    road_name    e.g. "A'BECKETT ST."
    link_betwe   route description "STREET (X -> Y)"
    speed_zone   string of integer km/h ("50", "80")
    ial_meanin   "BOTH" / "Direction <-> Direction"

"宁可不报" policy:
  * Disclaimer on the dataset says "general reference only" because of
    processing complexity. We treat ACT as a tertiary source -- it
    contributes to cross-verification but cannot be the sole source
    for displaying a posted limit.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from .base import SourceFetcher
from ._download import archive_raw
from ._geometry import bearing_of_segment, sample_linestring
from .ir import BEARING_UNKNOWN, ROAD_OTHER, SpeedSegment

# Socrata API. $limit=50000 max per page (ACT default). Pagination via
# $offset. The dataset is small (a few thousand rows) so one page is
# usually enough.
SODA_URL = "https://www.data.act.gov.au/resource/hy95-2hum.json"

SPEED_RE = re.compile(r"^\s*(\d+)\s*$")


def _fetch_all_rows(page_size: int = 50000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        url = f"{SODA_URL}?$limit={page_size}&$offset={offset}"
        result = subprocess.run(
            ["curl", "-fsSL", "--max-time", "120", url],
            check=True, capture_output=True,
        )
        page = json.loads(result.stdout)
        if not page:
            break
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


class ACTFetcher(SourceFetcher):
    name = "AU_ACT_GOV"
    state = "ACT"

    def __init__(self, sample_interval_m: float = 100.0):
        self.interval = sample_interval_m

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        rows = _fetch_all_rows()
        print(f"[au_act] {len(rows)} rows from Socrata")

        if archive and rows:
            tmp = Path("/tmp/_au_act_speed_zones.json")
            with tmp.open("w") as f:
                json.dump(rows, f, separators=(",", ":"))
            arc = archive_raw(tmp, self.state, "act_speed_zones.json")
            print(f"[au_act] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        segments: list[SpeedSegment] = []
        skipped_speed = 0
        skipped_geom = 0
        now = int(time.time())

        for rec in rows:
            speed_str = (rec.get("speed_zone") or "").strip()
            m = SPEED_RE.match(speed_str)
            if not m:
                skipped_speed += 1
                continue
            speed = int(m.group(1))
            if speed <= 0 or speed > 130:
                skipped_speed += 1
                continue

            geom = rec.get("the_geom") or {}
            if geom.get("type") != "MultiLineString":
                skipped_geom += 1
                continue
            mline = geom.get("coordinates") or []

            emitted = False
            for coords in mline:
                if len(coords) < 2:
                    continue
                # ial_meanin == "BOTH" indicates symmetric application.
                # Anything else encodes a direction restriction, which
                # we currently surface via the segment bearing.
                ial = (rec.get("ial_meanin") or "").strip().upper()
                if ial == "BOTH" or not ial:
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
                    emitted = True

            if not emitted:
                skipped_geom += 1
                continue

            if limit and len(segments) >= limit:
                break

        print(f"[au_act] generated {len(segments):,} segments "
              f"(skipped {skipped_speed} bad speed, "
              f"{skipped_geom} bad geometry)")
        return segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--interval", type=float, default=100.0)
    args = ap.parse_args()
    fetcher = ACTFetcher(sample_interval_m=args.interval)
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

"""QLD Speed Limits fetcher (Department of Transport and Main Roads).

Dataset:       data.qld.gov.au/dataset/speed-limits-for-state-and-local-roads
CKAN package:  b4a3f5d5-f3ac-4382-ae93-0c97293c8b69
License:       CC BY 4.0
Update:        Original collection 2019-2020; portal claims ongoing
               maintenance but CKAN metadata_modified still 2022.
               Treat as a slow-moving baseline, not a live source.

This dataset is unusual among AU states -- it ships as 64 separate LGA
CSV files (Brisbane North / South / Sunshine Coast / ... / Quilpie /
Winton / Mount Isa / ...). Each CSV is a *point* dataset: one row per
posted speed sign, with LATITUDE / LONGITUDE coordinates.

We pull data via CKAN datastore_search instead of the /download/ CSV
endpoint because the static CSV endpoint returns HTTP 202 with empty
body (the portal does async generation behind a CDN; the cached blob
warms up unreliably and we hit empties on the first request).

Schema (datastore_search records):
    _id          int
    NUMBER       text   internal sign id
    LATITUDE     numeric
    LONGITUDE    numeric
    SPEED        text   e.g. "60", "VARIABLE", "SCHOOL", "10", "100"
    SIGN_TXT     text
    SIGN_NOTE    text   e.g. "GANTRY"
    SIGN_NUM     text
    SIDE         text   "LANE" / road side designation
    ST_NAME      text
    ST_TYPE      text   e.g. "HIGHWAY", "ROAD", "STREET"
    LOCALITY     text
    DIRECTION    text   "NORTH" | "NORTHEAST" | ... | "BOTH"
    DATE         timestamp
    TIME         numeric

"宁可不报" policy:
  * SPEED must match `^\\d+$`. Reject "VARIABLE", "SCHOOL", blank.
  * DIRECTION translated to bearing via 8-point compass; "BOTH" or
    unknown -> 0xFFFF (bidirectional).
  * No LineString sampling -- the source is already pointwise.

The fetcher does not auto-enumerate all 64 LGA resources -- they are
listed in `QLD_LGA_RESOURCES`. Updating this list is a one-time job
when a new LGA appears (rare).
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
from .ir import BEARING_UNKNOWN, ROAD_OTHER, SpeedSegment

CKAN_PACKAGE_URL = (
    "https://www.data.qld.gov.au/api/3/action/package_show"
    "?id=speed-limits-for-state-and-local-roads"
)
DATASTORE_URL = (
    "https://www.data.qld.gov.au/api/3/action/datastore_search"
)

SPEED_RE = re.compile(r"^\s*(\d+)\s*$")

# 8-point compass -> bearing degrees (north = 0, clockwise).
DIRECTION_TO_BEARING = {
    "NORTH": 0,
    "NORTHEAST": 45,
    "EAST": 90,
    "SOUTHEAST": 135,
    "SOUTH": 180,
    "SOUTHWEST": 225,
    "WEST": 270,
    "NORTHWEST": 315,
}


def _resource_list() -> list[tuple[str, str]]:
    """Resolve LGA resources via CKAN. Returns [(resource_id, lga_name), ...]."""
    result = subprocess.run(
        ["curl", "-fsSL", "--max-time", "60", CKAN_PACKAGE_URL],
        check=True, capture_output=True,
    )
    data = json.loads(result.stdout)
    if not data.get("success"):
        raise RuntimeError(f"QLD CKAN package_show failed: {data}")
    out: list[tuple[str, str]] = []
    for res in data["result"].get("resources", []):
        if (res.get("format") or "").strip().upper() != "CSV":
            continue
        rid = res.get("id")
        name = (res.get("name") or "").strip() or "?"
        if rid:
            out.append((rid, name))
    return out


def _fetch_resource(rid: str, lga: str,
                    page_size: int = 32000) -> list[dict]:
    """Pull all records for one LGA via datastore_search pagination.

    page_size=32000 is the practical upper bound; QLD CKAN truncates
    silently above ~40k. We could pull via _id ordering for cursor-
    stability but for ~16k-row LGAs offset paging works fine.
    """
    records: list[dict] = []
    offset = 0
    while True:
        url = f"{DATASTORE_URL}?resource_id={rid}&limit={page_size}&offset={offset}"
        result = subprocess.run(
            ["curl", "-fsSL", "--max-time", "120", url],
            check=True, capture_output=True,
        )
        data = json.loads(result.stdout)
        if not data.get("success"):
            raise RuntimeError(f"QLD datastore {lga} failed at offset {offset}")
        page = data["result"].get("records", [])
        if not page:
            break
        records.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return records


class QLDFetcher(SourceFetcher):
    name = "AU_QLD_GOV"
    state = "QLD"

    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        resources = _resource_list()
        print(f"[au_qld] {len(resources)} LGA resources to fetch")

        segments: list[SpeedSegment] = []
        skipped_speed = 0
        skipped_coord = 0
        now = int(time.time())
        all_raw: list[dict] = []

        for rid, lga in resources:
            page = _fetch_resource(rid, lga)
            print(f"[au_qld]   {lga:25s}: {len(page):,} sign rows")
            all_raw.extend(page)

            for rec in page:
                # SPEED is text in the CKAN schema but the datastore
                # returns ints for rows like SPEED=60 (some LGAs encode
                # the numeric value typed, others as strings). Coerce
                # to str before the regex to handle both shapes; first
                # surfaced as an AttributeError on Winton LGA in CI on
                # 2026-05-20.
                raw_speed = rec.get("SPEED")
                speed_str = "" if raw_speed is None else str(raw_speed).strip()
                m = SPEED_RE.match(speed_str)
                if not m:
                    skipped_speed += 1
                    continue
                speed = int(m.group(1))
                if speed <= 0 or speed > 130:
                    skipped_speed += 1
                    continue

                try:
                    lat = float(rec.get("LATITUDE"))
                    lon = float(rec.get("LONGITUDE"))
                except (TypeError, ValueError):
                    skipped_coord += 1
                    continue
                if not (-44.0 <= lat <= -9.0 and 137.0 <= lon <= 154.0):
                    # Outside the QLD bbox -- almost certainly bad data.
                    skipped_coord += 1
                    continue

                direction = (rec.get("DIRECTION") or "").strip().upper()
                bearing = DIRECTION_TO_BEARING.get(direction, BEARING_UNKNOWN)

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
            if limit and len(segments) >= limit:
                break

        if archive and all_raw:
            # We don't have a single upstream blob to archive -- the
            # API merges 64 LGAs. Persist the joined JSON for diff/
            # replay purposes (gzipped).
            tmp = Path("/tmp/_au_qld_raw.json")
            with tmp.open("w") as f:
                json.dump(all_raw, f, separators=(",", ":"))
            arc = archive_raw(tmp, self.state, "speed_limits_signs.json")
            print(f"[au_qld] archived raw: {arc} "
                  f"({arc.stat().st_size/1e6:.1f} MB)")

        print(f"[au_qld] generated {len(segments):,} segments "
              f"(skipped {skipped_speed} non-numeric speed, "
              f"{skipped_coord} bad coords)")
        return segments


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()
    fetcher = QLDFetcher()
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

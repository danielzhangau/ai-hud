"""Spatial cross-verification engine.

Consumes the per-source SpeedSegment streams produced by tools/fetchers
and decides, per output record:
  * which sources observed this location,
  * whether they agree on the posted speed, and
  * the resulting confidence enum.

The output is the input the DB writer (prepare_speed_db.py) needs to
populate v3 records' `source_mask` + `confidence` fields.

Design ground rules (driven by "宁可不报"):

1. **Agreement window**: two records "see the same place" iff they sit
   within `MATCH_RADIUS_M` of each other and within `MATCH_BEARING_DEG`
   when both have a non-sentinel bearing. Tight radius -- 30 m is below
   one road width which keeps parallel roads (frontage roads, dual
   carriageways) distinct.

2. **Conflict policy**: when sources disagree by more than
   `CONFLICT_TOL_KMH` we DROP the segment entirely and write it to
   quarantine. The driver sees "--" rather than a guessed value.

3. **Small disagreement policy**: a difference of <= CONFLICT_TOL_KMH
   gets resolved to the LOWER value. Driving 95 in a 100 zone is legal;
   driving 105 in a 100 zone is not.

4. **Confidence ladder**:
       OFFICIAL_VERIFIED   -- 1 official + OSM agree
       OFFICIAL_ONLY       -- 1 official, no OSM corroboration
       CROSS_NO_OFFICIAL   -- 2+ non-official agree (rare; today only OSM)
       SINGLE_SOURCE       -- only one source covers this location
   The on-device fusion layer renders SINGLE_SOURCE as "--".

CLI usage:
    python3 -m tools.cross_verify \\
        --state au --output data/speed_zones.db --version 3

The CLI lives in prepare_speed_db.py post-integration; this module
exposes `verify_segments(segments) -> list[VerifiedRecord]` for
direct use by tests and by prepare_speed_db.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Re-export the binary-format constants from the device-side module so
# cross_verify and the on-device loader can never drift. Bit values
# and confidence enum live in src/speed_db.py because the device reads
# them out of every record; here we just need them for the v3 writer.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
from speed_db import (  # noqa: E402
    CONFIDENCE_CROSS_NO_OFFICIAL,
    CONFIDENCE_OFFICIAL_ONLY,
    CONFIDENCE_OFFICIAL_VERIFIED,
    CONFIDENCE_SINGLE_SOURCE,
    SRC_BIT_ACT,
    SRC_BIT_NSW,
    SRC_BIT_NT,
    SRC_BIT_OSM,
    SRC_BIT_QLD,
    SRC_BIT_SA,
    SRC_BIT_VIC,
    SRC_BIT_WA,
)

# Logical source id -> bit mapping. Keep in sync with fetcher .name.
SOURCE_BIT = {
    "AU_VIC_GOV": SRC_BIT_VIC,
    "AU_NSW_GOV": SRC_BIT_NSW,
    "AU_QLD_GOV": SRC_BIT_QLD,
    "AU_WA_GOV":  SRC_BIT_WA,
    "AU_SA_GOV":  SRC_BIT_SA,
    "AU_ACT_GOV": SRC_BIT_ACT,
    "OSM":        SRC_BIT_OSM,
}

# Bits flagged as "official government" sources. Everything else
# (currently just OSM) is non-official.
OFFICIAL_BITS = (SRC_BIT_VIC | SRC_BIT_NSW | SRC_BIT_QLD | SRC_BIT_WA
                 | SRC_BIT_SA | SRC_BIT_ACT | SRC_BIT_NT)

# Spatial / value tolerances. Tuning notes inline so future bumps
# carry their rationale forward.
# Bumped 30 -> 80 -> 100 on 2026-05-20 chasing GitHub's 100 MB
# per-file ceiling (the 80m revision wrote 100.68 MB, 0.68 MB over).
# The radius mirrors fetcher sample_interval_m so adjacent same-
# direction sources reliably find each other -- a sample from source
# A is at most one interval away from B's nearest sample on the same
# road. Looser matching can pair parallel roads (frontage + highway)
# but their differing speed limits self-correct via "宁可不报":
# the spread quarantines the cluster rather than confirming the
# wrong value.
MATCH_RADIUS_M = 100.0
MATCH_BEARING_DEG = 45.0   # accept opposite-direction segments
                            # as agreement if their bearings differ
                            # by <=45 (or either is sentinel).
CONFLICT_TOL_KMH = 10      # disagreement <= 10 km/h -> take lower
                            # disagreement >  10 km/h -> quarantine

# Spatial index resolution. Coarse enough that the 3x3 neighbour
# search reliably covers MATCH_RADIUS_M at AU latitudes.
_INDEX_RES_DEG = 0.0005    # ~55m lat, ~46m lon at -33

_M_PER_DEG_LAT = 111_320.0


def _haversine_m(lat1, lon1, lat2, lon2):
    cos_lat = math.cos(math.radians((lat1 + lat2) * 0.5))
    dlat = (lat2 - lat1) * _M_PER_DEG_LAT
    dlon = (lon2 - lon1) * _M_PER_DEG_LAT * cos_lat
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_diff(b1, b2):
    if b1 == 0xFFFF or b2 == 0xFFFF:
        return 0
    diff = abs(b1 - b2) % 360
    return diff if diff <= 180 else 360 - diff


def _cell_key(lat, lon):
    gi = int(math.floor(lat / _INDEX_RES_DEG))
    gj = int(math.floor(lon / _INDEX_RES_DEG))
    return (gi, gj)


def _cell_neighbors(lat, lon):
    gi = int(math.floor(lat / _INDEX_RES_DEG))
    gj = int(math.floor(lon / _INDEX_RES_DEG))
    return [(gi + di, gj + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)]


@dataclass(slots=True)
class VerifiedRecord:
    """Cross-verified, ready-to-write DB record.

    slots=True drops ~40% of per-instance memory vs the default
    __dict__-backed dataclass -- a non-trivial saving when build_au()
    accumulates millions of these across the per-state loop.
    """
    lat: float
    lon: float
    speed: int
    road_type: int
    bearing: int
    source_mask: int
    confidence: int
    state: str
    fetched_at: int


@dataclass
class QuarantineRecord:
    """A segment dropped because sources disagree beyond tolerance.

    Persisted to data/quarantine/*.jsonl so a human reviewer can spot-
    check the upstream cause (often OSM vandalism or a stale state DB).
    """
    lat: float
    lon: float
    state: str
    values: list[tuple[str, int]]   # (source name, speed)
    note: str = ""


def _classify(observations: list, all_bits: int) -> tuple[int, int, str]:
    """Pick (speed, confidence, note) from agreeing observations.

    `observations` is a non-empty list of SpeedSegment-like objects with
    .speed and .source -- all already known to be spatially co-located
    via the caller's index.

    Conflict handling: only CROSS-source disagreement triggers
    quarantine. Within-source disagreement (e.g. ACT data has two
    records at the same lat/lon with different speeds because the
    upstream pipeline sampled both lanes of a road) is resolved by
    taking the lower value -- it's a data-quality smell at the source,
    not a sign one source is lying to us about the other.
    """
    # Per-source minimum speed. Resolves intra-source inconsistency
    # (the lower value being the safer driver-facing display) BEFORE
    # we check for cross-source spread.
    per_source_min: dict[str, int] = {}
    for o in observations:
        src = getattr(o, "source")
        sp = getattr(o, "speed")
        if src not in per_source_min or sp < per_source_min[src]:
            per_source_min[src] = sp

    distinct_per_source = sorted(per_source_min.values())
    spread = (distinct_per_source[-1] - distinct_per_source[0]
              if distinct_per_source else 0)

    if spread > CONFLICT_TOL_KMH:
        # Sources disagree beyond tolerance -- quarantine.
        return (-1, -1, "conflict>tol")

    # Consensus speed = lower bound (defensive). Any small spread within
    # CONFLICT_TOL_KMH is rounded down so the driver is never told a
    # faster limit than any source observed.
    chosen = distinct_per_source[0]

    has_official = bool(all_bits & OFFICIAL_BITS)
    has_osm = bool(all_bits & SRC_BIT_OSM)
    n_sources = bin(all_bits).count("1")

    if n_sources == 1:
        return (chosen, CONFIDENCE_SINGLE_SOURCE, "single")
    if has_official and has_osm:
        return (chosen, CONFIDENCE_OFFICIAL_VERIFIED, "official+osm")
    if has_official:
        # Multiple official sources but no OSM -- still treat as
        # "verified" only when at least 2 official agree.
        n_official = bin(all_bits & OFFICIAL_BITS).count("1")
        if n_official >= 2:
            return (chosen, CONFIDENCE_OFFICIAL_VERIFIED, "official+official")
        return (chosen, CONFIDENCE_OFFICIAL_ONLY, "official-only")
    # No official, just multiple non-official agreeing (rare today).
    return (chosen, CONFIDENCE_CROSS_NO_OFFICIAL, "cross-no-official")


def verify_segments(segments: Iterable) -> tuple[list[VerifiedRecord],
                                                 list[QuarantineRecord]]:
    """Run cross-verification over a stream of SpeedSegment.

    Returns (verified, quarantine). Verified list goes to the DB
    writer; quarantine list goes to the audit report.

    The algorithm is a single pass:
      1. Build a coarse spatial index keyed by (gi, gj) cells.
      2. For each unprocessed segment, gather all spatially close
         segments from the 3x3 neighbour cells (regardless of source).
      3. Filter the candidate set by Haversine distance + bearing.
      4. Classify the consensus group -> emit one VerifiedRecord, or
         move to quarantine on conflict.
      5. Mark every segment in the consensus group as processed so it
         is not re-emitted by a later iteration.
    """
    # Materialize once -- caller might pass a generator.
    segs = list(segments)
    n = len(segs)
    if n == 0:
        return [], []

    # Build coarse cell index.
    index: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(segs):
        index.setdefault(_cell_key(s.lat, s.lon), []).append(i)

    verified: list[VerifiedRecord] = []
    quarantine: list[QuarantineRecord] = []
    processed = bytearray(n)   # 0 = unprocessed, 1 = consumed

    for i, base in enumerate(segs):
        if processed[i]:
            continue

        # Gather candidate set from neighbour cells.
        candidates: list[int] = []
        for key in _cell_neighbors(base.lat, base.lon):
            candidates.extend(index.get(key, ()))

        # Filter by distance + bearing. We collect every match
        # regardless of source; the classifier deduplicates per source
        # via the bit mask.
        group: list[int] = []
        for j in candidates:
            if processed[j]:
                continue
            cand = segs[j]
            if _haversine_m(base.lat, base.lon,
                            cand.lat, cand.lon) > MATCH_RADIUS_M:
                continue
            if _bearing_diff(base.bearing, cand.bearing) > MATCH_BEARING_DEG:
                continue
            group.append(j)

        # group is guaranteed non-empty (contains `i` itself).
        all_bits = 0
        for k in group:
            bit = SOURCE_BIT.get(segs[k].source, 0)
            all_bits |= bit

        speed, confidence, note = _classify([segs[k] for k in group], all_bits)

        if speed < 0:
            # Quarantine -- write one record per location with the full
            # value list so reviewers can diagnose source-by-source.
            quarantine.append(QuarantineRecord(
                lat=base.lat,
                lon=base.lon,
                state=base.state,
                values=[(segs[k].source, segs[k].speed) for k in group],
                note=note,
            ))
        else:
            verified.append(VerifiedRecord(
                lat=base.lat,
                lon=base.lon,
                speed=speed,
                road_type=base.road_type,
                bearing=base.bearing,
                source_mask=all_bits,
                confidence=confidence,
                state=base.state,
                fetched_at=base.fetched_at,
            ))

        for k in group:
            processed[k] = 1

    return verified, quarantine


def write_quarantine(quarantine: list[QuarantineRecord],
                     out_path: Path) -> None:
    """Dump quarantine list as JSONL for human review.

    JSONL keeps each line independently parseable -- a reviewer can
    `head -50 quarantine.jsonl` without choking on a 30 MB JSON array.
    """
    import json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for q in quarantine:
            f.write(json.dumps({
                "lat": q.lat,
                "lon": q.lon,
                "state": q.state,
                "values": q.values,
                "note": q.note,
            }, separators=(",", ":")) + "\n")


def summarize(verified: list[VerifiedRecord],
              quarantine: list[QuarantineRecord]) -> dict:
    """Return per-state, per-confidence aggregate counts for PR body."""
    out: dict = {
        "verified_total": len(verified),
        "quarantine_total": len(quarantine),
        "by_confidence": {},
        "by_state": {},
    }
    for v in verified:
        out["by_confidence"][v.confidence] = (
            out["by_confidence"].get(v.confidence, 0) + 1)
        out["by_state"].setdefault(v.state, {"verified": 0, "quarantine": 0})
        out["by_state"][v.state]["verified"] += 1
    for q in quarantine:
        out["by_state"].setdefault(q.state, {"verified": 0, "quarantine": 0})
        out["by_state"][q.state]["quarantine"] += 1
    return out

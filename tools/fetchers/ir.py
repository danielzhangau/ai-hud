"""Intermediate representation shared by every fetcher.

`SpeedSegment` is the single producer/consumer contract between
fetchers and downstream stages (cross_verify, DB writer). Keeping it
deliberately flat -- no nested objects -- so it can be serialized to
a binary DB record without extra mapping logic.

Field choices mirror src/speed_db.py's `_Record` so the on-device
reader does not need to learn a new vocabulary in Sprint 2/B2.
"""
from __future__ import annotations

from dataclasses import dataclass

# Road type enum -- must stay in sync with src/speed_db.py.
ROAD_MOTORWAY = 0
ROAD_TRUNK = 1
ROAD_PRIMARY = 2
ROAD_SECONDARY = 3
ROAD_RESIDENTIAL = 4
ROAD_OTHER = 5

# Sentinel used in the on-device binary as well -- means
# "bearing unknown / segment is bidirectional".
BEARING_UNKNOWN = 0xFFFF


@dataclass
class SpeedSegment:
    """A single sampled point on a posted-speed road segment.

    Multiple SpeedSegment records are emitted per upstream geometry --
    one for every ~30m along the LineString. This duplication is what
    makes the on-device grid index O(1) lookups possible.
    """

    lat: float
    lon: float
    speed: int                  # km/h
    road_type: int = ROAD_OTHER
    bearing: int = BEARING_UNKNOWN
    state: str = ""             # ISO-like state code: "NSW", "VIC", "WA", ...
    source: str = ""            # Logical source id, e.g. "AU_NSW_GOV", "OSM"
    time_mask: int = 0          # bit field for time-conditional limits (Sprint 2)
    fetched_at: int = 0         # epoch seconds when fetcher pulled this row

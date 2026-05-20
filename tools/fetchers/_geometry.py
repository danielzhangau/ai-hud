"""LineString sampling + bearing utilities.

Lifted verbatim from `tools/prepare_speed_db.py` so the new fetcher
layer is self-contained and doesn't depend on a script with side
effects (argparse, prints in module scope). Sprint 2 will fold
prepare_speed_db.py's copy into this module -- the duplication is a
known DRY debt acknowledged in the plan.
"""
from __future__ import annotations

import math

_M_PER_DEG = 111_320.0


def sample_linestring(coords, interval_m: float = 30.0):
    """Yield (lat, lon) points along a LineString at ~`interval_m` spacing.

    Coordinates are GeoJSON-flavored [lon, lat] -- we swap to (lat, lon)
    on output to match src/speed_db.py's lookup convention.
    """
    if len(coords) < 2:
        if coords:
            yield (coords[0][1], coords[0][0])
        return

    yield (coords[0][1], coords[0][0])

    accum = 0.0
    for i in range(1, len(coords)):
        lat1, lon1 = coords[i - 1][1], coords[i - 1][0]
        lat2, lon2 = coords[i][1], coords[i][0]

        dlat = (lat2 - lat1) * _M_PER_DEG
        cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
        dlon = (lon2 - lon1) * _M_PER_DEG * cos_lat
        seg_len = math.sqrt(dlat * dlat + dlon * dlon)

        if seg_len < 0.01:
            continue

        pos = 0.0
        while pos < seg_len:
            remaining = interval_m - accum
            if pos + remaining <= seg_len:
                pos += remaining
                frac = pos / seg_len
                yield (lat1 + (lat2 - lat1) * frac,
                       lon1 + (lon2 - lon1) * frac)
                accum = 0.0
            else:
                accum += seg_len - pos
                break

    yield (coords[-1][1], coords[-1][0])


def bearing_of_segment(coords) -> int:
    """Bearing in degrees (0..359) from first to last coordinate.

    Returns 0xFFFF when the segment is degenerate (single point, or
    start==end). Callers should pass this sentinel through unchanged
    -- the binary DB and lookup treat it as "unknown / bidirectional".
    """
    if len(coords) < 2:
        return 0xFFFF
    lat1, lon1 = coords[0][1], coords[0][0]
    lat2, lon2 = coords[-1][1], coords[-1][0]
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    if abs(dlat) < 1e-8 and abs(dlon) < 1e-8:
        return 0xFFFF
    angle = math.degrees(math.atan2(dlon, dlat)) % 360
    return int(round(angle))

"""Tests for tools/cross_verify.py.

Each test sets up a minimal cluster of fake SpeedSegment-like records
and asserts the resulting confidence + source_mask. These are the
decisions that, on the device, control whether the driver sees a
number or a "--", so coverage matters.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import cross_verify as cv  # noqa: E402


@dataclass
class FakeSeg:
    """Minimum surface area to satisfy verify_segments(). Mirrors
    tools.fetchers.ir.SpeedSegment field names."""
    lat: float
    lon: float
    speed: int
    source: str
    state: str = "AU"
    road_type: int = 5
    bearing: int = 0xFFFF
    fetched_at: int = 0


# A reference point in central Melbourne. Both colocated and
# non-colocated tests use it as the anchor.
MEL_LAT = -37.8136
MEL_LON = 144.9631


class TestCrossVerify(unittest.TestCase):

    def test_single_source_marks_single_confidence(self):
        # Just OSM -- no other source ever saw this point.
        segs = [FakeSeg(MEL_LAT, MEL_LON, 50, "OSM")]
        verified, quarantine = cv.verify_segments(segs)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0].confidence,
                         cv.CONFIDENCE_SINGLE_SOURCE)
        self.assertEqual(verified[0].source_mask, cv.SRC_BIT_OSM)
        self.assertEqual(len(quarantine), 0)

    def test_official_plus_osm_agree_yields_verified(self):
        # NSW gov + OSM, both 50 km/h, within MATCH_RADIUS_M.
        segs = [
            FakeSeg(MEL_LAT, MEL_LON, 50, "AU_NSW_GOV"),
            # 10m east -- still inside 30m radius.
            FakeSeg(MEL_LAT, MEL_LON + 0.0001, 50, "OSM"),
        ]
        verified, quarantine = cv.verify_segments(segs)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0].confidence,
                         cv.CONFIDENCE_OFFICIAL_VERIFIED)
        self.assertEqual(verified[0].source_mask,
                         cv.SRC_BIT_NSW | cv.SRC_BIT_OSM)

    def test_two_officials_agree_yields_verified(self):
        # Hypothetical overlap region: NSW + VIC both report 80.
        segs = [
            FakeSeg(MEL_LAT, MEL_LON, 80, "AU_NSW_GOV"),
            FakeSeg(MEL_LAT, MEL_LON, 80, "AU_VIC_GOV"),
        ]
        verified, _ = cv.verify_segments(segs)
        self.assertEqual(verified[0].confidence,
                         cv.CONFIDENCE_OFFICIAL_VERIFIED)

    def test_official_only_no_osm_yields_official_only(self):
        # VIC gov reports 60, no OSM record present.
        segs = [FakeSeg(MEL_LAT, MEL_LON, 60, "AU_VIC_GOV")]
        verified, _ = cv.verify_segments(segs)
        # Single VIC observation alone -- still SINGLE_SOURCE per
        # classifier (n_sources == 1).
        self.assertEqual(verified[0].confidence,
                         cv.CONFIDENCE_SINGLE_SOURCE)

    def test_minor_disagreement_picks_lower(self):
        # OSM says 60, NSW gov says 65. Spread = 5 <= CONFLICT_TOL_KMH,
        # so consensus = lower = 60.
        segs = [
            FakeSeg(MEL_LAT, MEL_LON, 60, "OSM"),
            FakeSeg(MEL_LAT, MEL_LON, 65, "AU_NSW_GOV"),
        ]
        verified, quarantine = cv.verify_segments(segs)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0].speed, 60)
        self.assertEqual(len(quarantine), 0)

    def test_hard_conflict_goes_to_quarantine(self):
        # OSM 50 vs NSW gov 100 -- spread 50 > tol. Quarantine.
        segs = [
            FakeSeg(MEL_LAT, MEL_LON, 50, "OSM"),
            FakeSeg(MEL_LAT, MEL_LON, 100, "AU_NSW_GOV"),
        ]
        verified, quarantine = cv.verify_segments(segs)
        self.assertEqual(len(verified), 0)
        self.assertEqual(len(quarantine), 1)
        # Quarantine carries both source values for the reviewer.
        sources_in_q = {s for s, _ in quarantine[0].values}
        self.assertEqual(sources_in_q, {"OSM", "AU_NSW_GOV"})

    def test_far_apart_segments_are_independent(self):
        # Two NSW gov observations 1 km apart -- not co-located, so
        # both should appear as independent SINGLE_SOURCE records.
        segs = [
            FakeSeg(MEL_LAT, MEL_LON, 50, "AU_NSW_GOV"),
            FakeSeg(MEL_LAT + 0.01, MEL_LON, 50, "AU_NSW_GOV"),
        ]
        verified, _ = cv.verify_segments(segs)
        self.assertEqual(len(verified), 2)
        for v in verified:
            self.assertEqual(v.confidence, cv.CONFIDENCE_SINGLE_SOURCE)


if __name__ == "__main__":
    unittest.main()

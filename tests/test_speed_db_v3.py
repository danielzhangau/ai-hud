"""Tests for the v3 binary DB format + reader back-compatibility.

Covers:
  * round-trip write/read for v3 records (source_mask + confidence).
  * loader accepts v1 and v2 files without break -- mandatory because
    Sprint 2 ships a new firmware version while v2 .db files remain
    in OTA bundles for at least one release cycle.
  * QueryResult exposes the new confidence + source_mask fields.

Run from repo root:
    python3 -m pytest tests/test_speed_db_v3.py -v
or stdlib unittest:
    python3 -m unittest tests.test_speed_db_v3
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

# Make src/ importable without packaging.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import speed_db as sdb  # noqa: E402

# These tests target the v3 record format and confidence wiring, not
# the HMAC signing layer (covered in test_db_signing.py). Disabling
# signature enforcement here keeps the test surface narrow and lets
# the test-side helpers write unsigned DBs without juggling keys.
sdb._ENFORCE_SIGNATURE = False


def _make_v3_db(path: Path, magic: bytes, records: list[tuple]):
    """Write a v3 binary DB to `path`. records: tuples matching v3 format."""
    with path.open("wb") as f:
        f.write(struct.pack(
            sdb.HEADER_FMT_V3, magic, 3, len(records),
            sdb.RECORD_SIZE_V3, 0, 1779000000,
        ))
        for rec in records:
            f.write(struct.pack(sdb.RECORD_FMT_V3, *rec))


def _make_v2_db(path: Path, magic: bytes, records: list[tuple]):
    """Write a v2 file (16-byte records, build_epoch in header)."""
    with path.open("wb") as f:
        f.write(struct.pack(
            sdb.HEADER_FMT_V2, magic, 2, len(records),
            sdb.RECORD_SIZE_V12, 0, 1778900000,
        ))
        for rec in records:
            f.write(struct.pack(sdb.RECORD_FMT_V12, *rec))


def _make_v1_db(path: Path, magic: bytes, records: list[tuple]):
    """Write a v1 file (no build_epoch)."""
    with path.open("wb") as f:
        f.write(struct.pack(
            sdb.HEADER_FMT_V1, magic, 1, len(records),
            sdb.RECORD_SIZE_V12, 0,
        ))
        for rec in records:
            f.write(struct.pack(sdb.RECORD_FMT_V12, *rec))


# Sample record: Sydney CBD (-33.8688, 151.2093).
SYD_LAT = -33.8688
SYD_LON = 151.2093
SYD_LAT_E6 = -33_868_800
SYD_LON_E6 = 151_209_300
# The reader buckets records by grid_key computed from lat/lon; the
# writer MUST agree on the key or query() can't find them. Use the
# module's own `_grid_key` to stay consistent across format bumps.
SYD_GK = sdb._grid_key(SYD_LAT, SYD_LON)


class TestV3Format(unittest.TestCase):

    def test_v3_record_size_is_18(self):
        self.assertEqual(sdb.RECORD_SIZE_V3, 18)

    def test_v3_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zones.db"
            _make_v3_db(p, sdb.MAGIC_ZONES, [
                # source_mask=SRC_BIT_NSW|SRC_BIT_OSM, confidence=VERIFIED
                (SYD_LAT_E6, SYD_LON_E6, 50, 5, 0xFFFF, SYD_GK,
                 sdb.SRC_BIT_NSW | sdb.SRC_BIT_OSM,
                 sdb.CONFIDENCE_OFFICIAL_VERIFIED),
            ])
            db = sdb.SpeedDB(zones_path=str(p))
            self.assertEqual(db.zone_count, 1)
            speed, conf, mask = db.query_speed_limit_full(
                -33.8688, 151.2093, heading=None)
            self.assertEqual(speed, 50)
            self.assertEqual(conf, sdb.CONFIDENCE_OFFICIAL_VERIFIED)
            self.assertEqual(mask, sdb.SRC_BIT_NSW | sdb.SRC_BIT_OSM)

    def test_v2_loads_as_confidence_unknown(self):
        # v2 records lack the confidence + source_mask fields. The reader
        # must synthesize CONFIDENCE_UNKNOWN so the fusion layer treats
        # the record as single-source.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zones.db"
            _make_v2_db(p, sdb.MAGIC_ZONES, [
                (SYD_LAT_E6, SYD_LON_E6, 80, 5, 0xFFFF, SYD_GK),
            ])
            db = sdb.SpeedDB(zones_path=str(p))
            speed, conf, mask = db.query_speed_limit_full(
                -33.8688, 151.2093, heading=None)
            self.assertEqual(speed, 80)
            self.assertEqual(conf, sdb.CONFIDENCE_UNKNOWN)
            self.assertEqual(mask, sdb.SRC_MASK_UNKNOWN)

    def test_v1_loads_with_zero_build_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zones.db"
            _make_v1_db(p, sdb.MAGIC_ZONES, [
                (SYD_LAT_E6, SYD_LON_E6, 100, 0, 0xFFFF, SYD_GK),
            ])
            db = sdb.SpeedDB(zones_path=str(p))
            self.assertEqual(db.zone_count, 1)
            self.assertEqual(db.build_epoch, 0)

    def test_v3_query_returns_confidence_in_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zones.db"
            _make_v3_db(p, sdb.MAGIC_ZONES, [
                (SYD_LAT_E6, SYD_LON_E6, 60, 5, 0xFFFF, SYD_GK,
                 sdb.SRC_BIT_OSM, sdb.CONFIDENCE_SINGLE_SOURCE),
            ])
            db = sdb.SpeedDB(zones_path=str(p))
            r = db.query(-33.8688, 151.2093)
            self.assertEqual(r.speed_limit, 60)
            self.assertEqual(r.speed_confidence, sdb.CONFIDENCE_SINGLE_SOURCE)
            self.assertEqual(r.speed_source_mask, sdb.SRC_BIT_OSM)

    def test_legacy_query_speed_limit_still_returns_int(self):
        # Existing call sites use .query_speed_limit(); assert the int
        # contract still holds (no tuple breakage).
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zones.db"
            _make_v3_db(p, sdb.MAGIC_ZONES, [
                (SYD_LAT_E6, SYD_LON_E6, 60, 5, 0xFFFF, SYD_GK,
                 sdb.SRC_BIT_VIC, sdb.CONFIDENCE_OFFICIAL_ONLY),
            ])
            db = sdb.SpeedDB(zones_path=str(p))
            speed = db.query_speed_limit(-33.8688, 151.2093)
            self.assertIsInstance(speed, int)
            self.assertEqual(speed, 60)


class TestSpeedFusionConfidence(unittest.TestCase):

    def test_low_confidence_db_routes_to_db_low_confidence_source(self):
        fusion = sdb.SpeedFusion(default_limit=100)
        # SINGLE_SOURCE limit -- per policy, source becomes
        # "DB_LOW_CONFIDENCE" so the UI can render "--" instead.
        eff = fusion.update(db_limit=80, npu_limit=0, npu_confidence=0.0,
                            db_confidence=sdb.CONFIDENCE_SINGLE_SOURCE)
        self.assertEqual(eff, 80)  # value kept internally
        self.assertEqual(fusion.source, "DB_LOW_CONFIDENCE")

    def test_official_only_confidence_is_trusted(self):
        fusion = sdb.SpeedFusion(default_limit=100)
        eff = fusion.update(db_limit=80, npu_limit=0, npu_confidence=0.0,
                            db_confidence=sdb.CONFIDENCE_OFFICIAL_ONLY)
        self.assertEqual(eff, 80)
        self.assertEqual(fusion.source, "DB")

    def test_legacy_call_without_db_confidence_still_trusts_db(self):
        # Old call sites (or v1/v2 .db) won't pass db_confidence. Verify
        # we don't accidentally regress them to DB_LOW_CONFIDENCE.
        fusion = sdb.SpeedFusion(default_limit=100)
        eff = fusion.update(db_limit=80, npu_limit=0, npu_confidence=0.0)
        self.assertEqual(eff, 80)
        self.assertEqual(fusion.source, "DB")

    def test_no_db_falls_back_to_default(self):
        fusion = sdb.SpeedFusion(default_limit=100)
        eff = fusion.update(db_limit=0, npu_limit=0, npu_confidence=0.0)
        self.assertEqual(eff, 100)
        self.assertEqual(fusion.source, "DEFAULT")


if __name__ == "__main__":
    unittest.main()

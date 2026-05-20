"""Tests for HMAC-SHA256 signing + rollback protection.

Covers the threat model in src/db_signer.py:
  * Valid signature + first-time epoch -> accepted.
  * Tampered .db -> rejected.
  * Missing .sig -> rejected.
  * Wrong key -> rejected.
  * Older build_epoch after device has accepted a newer one ->
    rejected as a rollback attempt (the forged .sig still being
    valid doesn't help an attacker if epoch is regressed).
  * /etc/ai_hud_db_allow_unsigned escape hatch -> bypasses verification.

Run from repo root:
    python3 -m unittest tests.test_db_signing
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db_signer  # noqa: E402
import speed_db as sdb  # noqa: E402


def _make_v3_db_with_epoch(path: Path, build_epoch: int):
    """Write a tiny but valid v3 zones DB pinned at build_epoch."""
    SYD_LAT_E6 = -33_868_800
    SYD_LON_E6 = 151_209_300
    gk = sdb._grid_key(-33.8688, 151.2093)
    with path.open("wb") as f:
        f.write(struct.pack(
            sdb.HEADER_FMT_V3,
            sdb.MAGIC_ZONES, 3, 1,
            sdb.RECORD_SIZE_V3, 0, build_epoch,
        ))
        f.write(struct.pack(
            sdb.RECORD_FMT_V3,
            SYD_LAT_E6, SYD_LON_E6, 50, 5, 0xFFFF, gk,
            sdb.SRC_BIT_NSW | sdb.SRC_BIT_OSM,
            sdb.CONFIDENCE_OFFICIAL_VERIFIED,
        ))


class TestSigning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # Point min_epoch + unsigned-override at temp files so tests
        # don't touch the host /etc. Saved + restored per test.
        self._orig_min = db_signer.MIN_EPOCH_PATH
        self._orig_uns = db_signer.UNSIGNED_OVERRIDE_PATH
        db_signer.MIN_EPOCH_PATH = str(self.tmp_path / "min_epoch")
        db_signer.UNSIGNED_OVERRIDE_PATH = str(self.tmp_path / "allow_unsigned")
        # Force the loader to enforce signatures for the duration of
        # the test even when running under a permissive dev env var.
        self._orig_enforce = sdb._ENFORCE_SIGNATURE
        sdb._ENFORCE_SIGNATURE = True
        # Test key -- distinct from the dev fallback so we exercise
        # the env-var path.
        os.environ["AI_HUD_DB_SECRET"] = "test-secret-please-rotate"

    def tearDown(self):
        db_signer.MIN_EPOCH_PATH = self._orig_min
        db_signer.UNSIGNED_OVERRIDE_PATH = self._orig_uns
        sdb._ENFORCE_SIGNATURE = self._orig_enforce
        os.environ.pop("AI_HUD_DB_SECRET", None)
        self.tmp.cleanup()

    def test_valid_signature_loads_records(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        key = db_signer.load_key()
        db_signer.sign_file(path, key)
        db = sdb.SpeedDB(zones_path=str(path))
        self.assertEqual(db.zone_count, 1)
        self.assertIsNone(db.last_reject_reason)

    def test_tampered_db_rejected(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        key = db_signer.load_key()
        db_signer.sign_file(path, key)
        # Flip a single byte deep in the record (not the header so
        # we don't change build_epoch and trigger the rollback path).
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))
        db = sdb.SpeedDB(zones_path=str(path))
        self.assertEqual(db.zone_count, 0)
        self.assertEqual(db.last_reject_reason, "signature")

    def test_missing_sig_rejected(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        # Do NOT write a .sig.
        db = sdb.SpeedDB(zones_path=str(path))
        self.assertEqual(db.zone_count, 0)
        self.assertEqual(db.last_reject_reason, "signature")

    def test_wrong_key_rejected(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        # Sign with one key, then verify with a different one.
        os.environ["AI_HUD_DB_SECRET"] = "attacker-key"
        attacker_key = db_signer.load_key()
        db_signer.sign_file(path, attacker_key)
        os.environ["AI_HUD_DB_SECRET"] = "test-secret-please-rotate"
        db = sdb.SpeedDB(zones_path=str(path))
        self.assertEqual(db.zone_count, 0)
        self.assertEqual(db.last_reject_reason, "signature")

    def test_rollback_attempt_rejected(self):
        # Device accepts a fresh DB first.
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        key = db_signer.load_key()
        db_signer.sign_file(path, key)
        sdb.SpeedDB(zones_path=str(path))
        # Now an attacker swaps in an OLDER, correctly-signed DB.
        # (The .sig is technically valid because the file was once
        # produced by the same key; a real attacker could have it
        # archived from a prior release.)
        old = self.tmp_path / "zones_old.db"
        _make_v3_db_with_epoch(old, build_epoch=1_500_000_000)
        db_signer.sign_file(old, key)
        db = sdb.SpeedDB(zones_path=str(old))
        self.assertEqual(db.zone_count, 0)
        self.assertEqual(db.last_reject_reason, "rollback")

    def test_unsigned_override_bypasses_signature(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1_700_000_000)
        # Touch the escape-hatch file. No .sig written.
        Path(db_signer.UNSIGNED_OVERRIDE_PATH).write_text("ok\n")
        db = sdb.SpeedDB(zones_path=str(path))
        self.assertEqual(db.zone_count, 1)
        self.assertIsNone(db.last_reject_reason)

    def test_sign_then_verify_roundtrip(self):
        path = self.tmp_path / "zones.db"
        _make_v3_db_with_epoch(path, build_epoch=1)
        key = db_signer.load_key()
        sig_path = db_signer.sign_file(path, key)
        self.assertTrue(sig_path.is_file())
        self.assertEqual(sig_path.stat().st_size, 32)
        self.assertTrue(db_signer.verify_file(path, key))


if __name__ == "__main__":
    unittest.main()

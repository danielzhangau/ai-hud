"""Tests for the auto-merge eligibility scorer.

These tests pin down the decision matrix:
  * unchanged DB -> safe
  * first-ever build (no baseline) -> needs review
  * large record-count swing -> needs review
  * quarantine spike -> needs review
  * any state silenced -> needs review

Avoid coupling to live data by hand-crafting minimal valid DB headers.
The scorer only reads the record-count uint at offset 6, so the rest
of the file can be padded zeros.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools import db_health  # noqa: E402


def _write_fake_db(path: Path, count: int):
    """Emit a minimum file the scorer can read a count out of.

    Header layout matches v1 (which is the v1/v2/v3 shared prefix):
      magic[4] + version[2] + count[4] + rec_size[2] + flags[4] = 16 bytes
    """
    blob = struct.pack("<4sHIHI", b"SZON", 3, count, 18, 0)
    path.write_bytes(blob)


def _write_summary(path: Path, *, verified_total: int = 100,
                   quarantine_total: int = 0,
                   by_state: dict | None = None):
    by_state = by_state or {"ACT": {"verified": verified_total,
                                    "quarantine": quarantine_total}}
    path.write_text(json.dumps({
        "verified_total": verified_total,
        "quarantine_total": quarantine_total,
        "by_state": by_state,
        "by_confidence": {0: verified_total},
    }))


class TestDBHealth(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpp = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_pair(self, old_count, new_count, **kw):
        """Helper: write old + new .db + summary, return scorer args."""
        old_db = self.tmpp / "old.db"
        new_db = self.tmpp / "new.db"
        old_sum = self.tmpp / "old.json"
        new_sum = self.tmpp / "new.json"
        _write_fake_db(old_db, old_count)
        _write_fake_db(new_db, new_count)
        _write_summary(old_sum, verified_total=old_count,
                       quarantine_total=kw.get("old_q", 0))
        _write_summary(new_sum, verified_total=new_count,
                       quarantine_total=kw.get("new_q", 0),
                       by_state=kw.get("by_state"))
        return old_db, new_db, old_sum, new_sum

    def test_unchanged_db_is_safe(self):
        a, b, sa, sb = self._make_pair(10_000, 10_000)
        v = db_health.score(a, b, sa, sb)
        self.assertTrue(v["safe_to_automerge"], v)

    def test_first_build_needs_review(self):
        # No baseline DB at all.
        _, new_db, _, new_sum = self._make_pair(0, 10_000)
        # Pretend old_db file simply doesn't exist.
        v = db_health.score(None, new_db, None, new_sum)
        self.assertFalse(v["safe_to_automerge"])
        self.assertIn("baseline", v["reason"])

    def test_big_delta_blocks_automerge(self):
        # 50% drop in record count
        a, b, sa, sb = self._make_pair(10_000, 5_000)
        v = db_health.score(a, b, sa, sb)
        self.assertFalse(v["safe_to_automerge"])
        self.assertIn("moved", v["reason"])

    def test_quarantine_ratio_too_high_blocks(self):
        # Verified barely moves but quarantine balloons to 20% of verified.
        a, b, sa, sb = self._make_pair(10_000, 10_050, new_q=2_010)
        v = db_health.score(a, b, sa, sb)
        self.assertFalse(v["safe_to_automerge"])
        self.assertIn("quarantine", v["reason"].lower())

    def test_quarantine_growth_too_fast_blocks(self):
        # Verified stays put, ratio stays small (1.5%), but the
        # ABSOLUTE growth is 3x the baseline.
        a, b, sa, sb = self._make_pair(10_000, 10_000,
                                       old_q=50, new_q=151)
        v = db_health.score(a, b, sa, sb)
        self.assertFalse(v["safe_to_automerge"])
        self.assertIn("quarantine", v["reason"].lower())

    def test_state_with_zero_blocks(self):
        a, b, sa, sb = self._make_pair(
            10_000, 10_050,
            by_state={"ACT": {"verified": 10_050, "quarantine": 0},
                      "VIC": {"verified": 0, "quarantine": 0}},
        )
        v = db_health.score(a, b, sa, sb)
        self.assertFalse(v["safe_to_automerge"])
        self.assertIn("zero", v["reason"].lower())


if __name__ == "__main__":
    unittest.main()

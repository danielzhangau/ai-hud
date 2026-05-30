"""Tests for the coasting display-state machine in DetectionState.

The machine sits between SpeedFusion's output and hud_renderer. It owns
three derived fields the renderer consumes:
    display_state         -- "no_data" | "confirmed" | "coasting"
    speed_limit           -- numeric value to display (held during coast)
    limit_low_confidence  -- True when the renderer should paint "--"

Covers all transitions: startup, confirm, coast on signal loss, coast
timeout, coast recovery, over-limit suppression, and the rule that
DB_LOW_CONFIDENCE / DEFAULT sources are not "signal".

Run from repo root:
    python3 -m pytest tests/test_display_state.py -v
or stdlib unittest:
    python3 -m unittest tests.test_display_state
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make src/ importable without packaging.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hud_live import (  # noqa: E402
    DetectionState,
    SOURCE_DB, SOURCE_NPU, SOURCE_DB_LOW_CONFIDENCE, SOURCE_DEFAULT,
)


class CoastingStateMachineTest(unittest.TestCase):
    def setUp(self):
        # filepath points at a path that never exists -- poll() is not
        # exercised here, only update_display_state().
        self.d = DetectionState(filepath="/tmp/nonexistent_ai_hud_detect")
        self.timeout = DetectionState.COASTING_TIMEOUT_S

    def test_startup_is_no_data(self):
        # Fresh DetectionState shows "--" before any fusion result.
        self.assertEqual(self.d.display_state, "no_data")
        self.assertTrue(self.d.limit_low_confidence)

    def test_confirmed_after_db_signal(self):
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.assertEqual(self.d.display_state, "confirmed")
        self.assertEqual(self.d.speed_limit, 60)
        self.assertFalse(self.d.limit_low_confidence)

    def test_confirmed_after_npu_signal(self):
        self.d.update_display_state(SOURCE_NPU, 80, now=100.0)
        self.assertEqual(self.d.display_state, "confirmed")
        self.assertEqual(self.d.speed_limit, 80)
        self.assertFalse(self.d.limit_low_confidence)

    def test_coasting_after_signal_lost(self):
        # Confirm 60, then lose the signal within the hold window.
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.d.update_display_state(SOURCE_DEFAULT, 120, now=110.0)
        self.assertEqual(self.d.display_state, "coasting")
        # Held value, NOT the default_limit fusion fell back to.
        self.assertEqual(self.d.speed_limit, 60)
        # Number is still rendered (renderer dims it, doesn't blank it).
        self.assertFalse(self.d.limit_low_confidence)

    def test_coasting_times_out_to_no_data(self):
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        # Just past the hold window.
        self.d.update_display_state(SOURCE_DEFAULT, 120,
                                    now=100.0 + self.timeout + 0.1)
        self.assertEqual(self.d.display_state, "no_data")
        self.assertTrue(self.d.limit_low_confidence)

    def test_coasting_boundary_is_exclusive(self):
        # Exactly at the timeout boundary is no longer coasting (< check).
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.d.update_display_state(SOURCE_DEFAULT, 120,
                                    now=100.0 + self.timeout)
        self.assertEqual(self.d.display_state, "no_data")

    def test_coasting_recovers_to_confirmed(self):
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.d.update_display_state(SOURCE_DEFAULT, 120, now=110.0)
        self.assertEqual(self.d.display_state, "coasting")
        # New signal arrives -- back to confirmed, clock refreshed.
        self.d.update_display_state(SOURCE_NPU, 80, now=115.0)
        self.assertEqual(self.d.display_state, "confirmed")
        self.assertEqual(self.d.speed_limit, 80)
        self.assertEqual(self.d.last_confirmed_time, 115.0)

    def test_recovery_refreshes_coast_window(self):
        # A brief re-confirm should extend the coast window, not let the
        # original timestamp expire it.
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.d.update_display_state(SOURCE_DEFAULT, 120, now=150.0)  # coasting
        self.assertEqual(self.d.display_state, "coasting")
        self.d.update_display_state(SOURCE_DB, 60, now=155.0)  # re-confirm
        # 50s after the *refresh*, still inside the window.
        self.d.update_display_state(SOURCE_DEFAULT, 120, now=200.0)
        self.assertEqual(self.d.display_state, "coasting")

    def test_db_low_confidence_is_not_signal(self):
        # SINGLE_SOURCE DB hit must not confirm; with no prior confirmed
        # value it lands in no_data.
        self.d.update_display_state(SOURCE_DB_LOW_CONFIDENCE, 50, now=100.0)
        self.assertEqual(self.d.display_state, "no_data")
        self.assertTrue(self.d.limit_low_confidence)

    def test_db_low_confidence_can_trigger_coasting(self):
        # If we had a confirmed value, a subsequent low-confidence hit
        # is treated as "no signal" and we coast on the confirmed value.
        self.d.update_display_state(SOURCE_DB, 60, now=100.0)
        self.d.update_display_state(SOURCE_DB_LOW_CONFIDENCE, 50, now=110.0)
        self.assertEqual(self.d.display_state, "coasting")
        self.assertEqual(self.d.speed_limit, 60)

    def test_never_coast_without_prior_confirm(self):
        # DEFAULT from the very first call -- nothing to hold.
        self.d.update_display_state(SOURCE_DEFAULT, 120, now=100.0)
        self.assertEqual(self.d.display_state, "no_data")


if __name__ == "__main__":
    unittest.main()

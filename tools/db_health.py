"""Auto-merge eligibility scorer for the monthly db-refresh PR.

The db-refresh workflow rebuilds the speed DB and opens a PR. Most
months the change is uninteresting -- VIC ships a new monthly file,
WA's IRIS sync adds a handful of records, total counts barely move.
Forcing a human to review every such PR is noise.

This tool compares the candidate .db + summary.json against the
previous-committed baseline (resolved by db-refresh.yml via `git
show HEAD:data/...`) and emits a single JSON verdict:

    {
      "safe_to_automerge": bool,
      "reason": str,
      "verified_delta_pct": float,
      "quarantine_ratio": float,
      "states_with_zero": [str, ...]
    }

Decision matrix (any failed check -> NOT safe):
  * Total verified records changed by <= 5% from the baseline. A
    bigger swing usually means either an upstream schema break or a
    fetcher producing zero rows -- both deserve human eyes.
  * Quarantine size <= 2x the baseline. A sudden jump in quarantine
    typically means one source went rogue (vandalism / WA flipping
    every road to "VARIABLE") that cross_verify caught but a human
    should still confirm.
  * No state's verified count dropped to zero. Even one state going
    silent is a regression to fix, not auto-ship.

The scorer is deliberately conservative -- false negatives (asking
for review when nothing's wrong) cost ~30 seconds; false positives
(auto-merging a broken DB into firmware) cost a recall.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Header parsing lives in src/db_signer so the build-time scorer and
# the device-time loader share one definition. Pulling that module in
# is cheap -- pure stdlib, no framebuffer / hardware deps despite the
# name suggesting otherwise.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
from db_signer import read_record_count_from_header  # noqa: E402

# Auto-merge thresholds. Tune carefully: every adjustment moves the
# false-positive vs false-negative trade-off.
MAX_VERIFIED_DELTA_PCT = 5.0     # |new - old| / old must be <= 5%
MAX_QUARANTINE_GROWTH = 2.0      # new quarantine <= 2 * old quarantine
MAX_QUARANTINE_RATIO = 0.05      # quarantine <= 5% of verified total


def _read_db_count(path: Path) -> int | None:
    """Pull the record count out of a v1/v2/v3 DB header.

    Returns None when the file is absent or unreadable -- the caller
    treats that as "no baseline" (a first-ever build), which always
    requires human review.
    """
    if not path or not path.is_file():
        return None
    return read_record_count_from_header(path)


def _load_summary(path: Path) -> dict | None:
    """Parse the JSON summary written by build_db.py. None on miss."""
    if not path or not path.is_file():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def score(old_db: Path, new_db: Path,
          old_summary: Path | None,
          new_summary: Path) -> dict:
    """Return the auto-merge verdict + supporting numbers.

    Always returns a fully-populated dict even on missing inputs so
    the CI yml can grep fields without conditional logic.
    """
    old_count = _read_db_count(old_db)
    new_count = _read_db_count(new_db)
    new_sum = _load_summary(new_summary) or {}
    old_sum = _load_summary(old_summary) if old_summary else None

    result: dict = {
        "safe_to_automerge": False,
        "reason": "",
        "old_count": old_count,
        "new_count": new_count,
        "verified_delta_pct": None,
        "quarantine_ratio": None,
        "old_quarantine": (old_sum or {}).get("quarantine_total"),
        "new_quarantine": new_sum.get("quarantine_total"),
        "states_with_zero": [],
    }

    if new_count is None or new_count == 0:
        result["reason"] = "new DB missing or empty"
        return result

    if old_count is None:
        result["reason"] = "no baseline (first build) -- needs human review"
        return result

    # ---- Record-count delta ----
    delta_pct = abs(new_count - old_count) * 100.0 / max(1, old_count)
    result["verified_delta_pct"] = round(delta_pct, 2)
    if delta_pct > MAX_VERIFIED_DELTA_PCT:
        result["reason"] = (
            f"verified count moved {delta_pct:.1f}% "
            f"(>{MAX_VERIFIED_DELTA_PCT}% threshold)"
        )
        return result

    # ---- Quarantine growth ----
    q_new = new_sum.get("quarantine_total") or 0
    v_new = new_sum.get("verified_total") or new_count
    q_ratio = q_new / max(1, v_new)
    result["quarantine_ratio"] = round(q_ratio, 4)
    if q_ratio > MAX_QUARANTINE_RATIO:
        result["reason"] = (
            f"quarantine is {q_ratio*100:.1f}% of verified "
            f"(>{MAX_QUARANTINE_RATIO*100:.1f}% threshold)"
        )
        return result

    if old_sum and (old_sum.get("quarantine_total") or 0) > 0:
        q_old = old_sum["quarantine_total"]
        if q_new > q_old * MAX_QUARANTINE_GROWTH:
            result["reason"] = (
                f"quarantine grew {q_new}/{q_old} = "
                f"{q_new/max(1,q_old):.1f}x (>{MAX_QUARANTINE_GROWTH}x)"
            )
            return result

    # ---- Per-state silent regression ----
    zero_states = []
    for state, counts in (new_sum.get("by_state") or {}).items():
        if (counts.get("verified") or 0) == 0:
            zero_states.append(state)
    result["states_with_zero"] = zero_states
    if zero_states:
        result["reason"] = (
            f"state(s) with zero verified records: {', '.join(zero_states)}"
        )
        return result

    # All checks passed.
    result["safe_to_automerge"] = True
    result["reason"] = "all delta / quarantine / state checks within bounds"
    return result


def main():
    ap = argparse.ArgumentParser(
        description="Score the monthly db-refresh PR for auto-merge eligibility.")
    ap.add_argument("--old-db", type=Path, required=False,
                    help="Previous build's .db (typically extracted via "
                         "`git show HEAD:data/speed_zones.db`)")
    ap.add_argument("--new-db", type=Path, required=True,
                    help="Freshly-built .db (data/speed_zones.db)")
    ap.add_argument("--old-summary", type=Path, required=False,
                    help="Previous build's summary JSON (optional)")
    ap.add_argument("--new-summary", type=Path, required=True,
                    help="New summary JSON written by build_db.py")
    ap.add_argument("--output", type=Path, default=None,
                    help="Optional path to write the verdict JSON")
    args = ap.parse_args()

    verdict = score(args.old_db, args.new_db,
                    args.old_summary, args.new_summary)
    serialized = json.dumps(verdict, indent=2)
    print(serialized)
    if args.output:
        args.output.write_text(serialized + "\n")
    # Always exit 0 -- the CI yml reads safe_to_automerge from the
    # JSON. A non-zero exit would short-circuit GitHub Actions before
    # the next step gets a chance to decide on PR labels.
    return 0


if __name__ == "__main__":
    sys.exit(main())

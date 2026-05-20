"""End-to-end speed DB builder: fetch -> cross-verify -> write v3 binary.

This is the new entry point that supersedes the legacy
`prepare_speed_db.py` for the AU region. It pulls from every
configured fetcher in parallel-friendly order, runs the spatial
cross-verification engine, and writes the result as a v3 binary DB.

The legacy script is kept around for the CN region (which still
relies on OSM-only Overpass) and as a fallback until the OTA
pipeline confirms v3 DBs work on shipped firmware. After one
release cycle of clean v3 builds, prepare_speed_db.py can be retired.

CLI usage:
    python3 tools/build_db.py --region au
    python3 tools/build_db.py --region au --states nsw,wa,act
    python3 tools/build_db.py --region au --quarantine-only

Outputs:
    data/speed_zones.db              -- v3 binary, sorted by grid_key
    data/quarantine/au_zones_<date>.jsonl  -- conflicting segments
    data/quarantine/au_summary_<date>.md   -- human-readable report
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

# Make src/ + repo root importable. We need both because:
#   - src/    -> for speed_db.py and db_signer.py (device-side modules
#                that we also use here to avoid wire-format drift)
#   - root/   -> for `from tools.fetchers...` and `from tools import
#                cross_verify` style imports. Adding root is the fix
#                for running via `python3 tools/build_db.py` directly
#                (the CI shape): Python prepends the SCRIPT's dir to
#                sys.path, not the repo root, so `from tools import X`
#                fails without this. `python3 -m tools.build_db` works
#                without it, but the workflow calls the script form.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import speed_db as sdb  # noqa: E402

from tools import cross_verify as cv  # noqa: E402

# db_signer lives in src/ so the device-side speed_db.py can import the
# same module without duplication. The sys.path prepend above already
# put src/ first so this import resolves cleanly on both ends.
import db_signer  # noqa: E402

# Fetcher registry. Each entry is (state code, fetcher factory). When a
# new state lands, append here -- nothing else needs to change.
from tools.fetchers import au_act, au_nsw, au_osm, au_qld, au_vic, au_wa  # noqa: E402

AU_FETCHERS = [
    ("nsw", au_nsw.NSWFetcher),
    ("vic", au_vic.VICFetcher),
    ("qld", au_qld.QLDFetcher),
    ("wa",  au_wa.WAFetcher),
    ("act", au_act.ACTFetcher),
    # OSM-only states/territories. au_unavailable.py documents these
    # have no open government speed-limit dataset; falling back to OSM
    # ensures at least *some* coverage instead of NO-DATA (which makes
    # the device fall back to default_limit, often unsafe). Cross-verify
    # will classify these as SINGLE_SOURCE -> UI shows "--" per the
    # "宁可不报" policy, but the DB hit still beats a wrong default.
    # Before this entry was added, querying Adelaide / Darwin / Hobart
    # returned NO-DATA even though OSM had street-level data.
    ("sa",  None),
    ("nt",  None),
    ("tas", None),
]

# OSM is its own (multi-state) fetcher run alongside the state ones.
# We thread it through via build_au() rather than the AU_FETCHERS
# registry because OSM is region-wide instead of per-state.


def _grid_key(lat, lon):
    """Same hash speed_db.py uses on the device."""
    gi = int(math.floor(lat / sdb.GRID_RES))
    gj = int(math.floor(lon / sdb.GRID_RES))
    gi_u = (gi + 32768) & 0xFFFF
    gj_u = (gj + 32768) & 0xFFFF
    return (gi_u << 16) | gj_u


def _write_zones_db(path: Path, verified: list) -> None:
    """Write a v3 zones DB. Records sorted by grid_key for locality."""
    keyed = []
    for v in verified:
        gk = _grid_key(v.lat, v.lon)
        lat_e6 = int(round(v.lat * 1_000_000))
        lon_e6 = int(round(v.lon * 1_000_000))
        keyed.append((gk, lat_e6, lon_e6, v.speed, v.road_type,
                      v.bearing, v.source_mask, v.confidence))
    keyed.sort(key=lambda x: x[0])

    build_epoch = int(time.time())
    with path.open("wb") as f:
        f.write(struct.pack(
            sdb.HEADER_FMT_V3,
            sdb.MAGIC_ZONES, 3, len(keyed),
            sdb.RECORD_SIZE_V3, 0, build_epoch,
        ))
        for (gk, lat_e6, lon_e6, speed, rtype, bearing,
             smask, conf) in keyed:
            f.write(struct.pack(
                sdb.RECORD_FMT_V3,
                lat_e6, lon_e6, speed, rtype, bearing, gk, smask, conf,
            ))
    print(f"[build_db] wrote {len(keyed):,} v3 records -> {path} "
          f"({path.stat().st_size/1e6:.1f} MB)")


def _write_quarantine_report(quarantine: list, out_md: Path,
                             summary: dict) -> None:
    """Write the human-readable summary that ends up in the PR body."""
    lines = []
    lines.append(f"# Speed DB build report  ({_dt.date.today()})\n")
    lines.append(f"- Verified records: **{summary['verified_total']:,}**")
    lines.append(f"- Quarantined records: **{summary['quarantine_total']:,}**\n")

    conf_names = {
        cv.CONFIDENCE_OFFICIAL_VERIFIED: "OFFICIAL_VERIFIED",
        cv.CONFIDENCE_OFFICIAL_ONLY:     "OFFICIAL_ONLY",
        cv.CONFIDENCE_CROSS_NO_OFFICIAL: "CROSS_NO_OFFICIAL",
        cv.CONFIDENCE_SINGLE_SOURCE:     "SINGLE_SOURCE",
    }
    lines.append("## Confidence distribution")
    for conf, count in sorted(summary["by_confidence"].items(),
                              key=lambda x: -x[1]):
        name = conf_names.get(conf, str(conf))
        pct = 100.0 * count / max(1, summary["verified_total"])
        lines.append(f"- {name}: {count:,} ({pct:.1f}%)")
    lines.append("")

    lines.append("## Per-state breakdown")
    lines.append("| State | Verified | Quarantine |")
    lines.append("|-------|----------|------------|")
    for state, counts in sorted(summary["by_state"].items()):
        lines.append(f"| {state} | {counts['verified']:,} | "
                     f"{counts['quarantine']:,} |")
    lines.append("")

    if quarantine:
        lines.append("## First 20 quarantined locations")
        lines.append("| Lat | Lon | State | Source values |")
        lines.append("|-----|-----|-------|---------------|")
        for q in quarantine[:20]:
            vals = ", ".join(f"{src}={sp}" for src, sp in q.values)
            lines.append(f"| {q.lat:.5f} | {q.lon:.5f} | {q.state} | {vals} |")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n")
    print(f"[build_db] wrote report -> {out_md}")


def build_au(states: list[str], output_dir: Path,
             quarantine_dir: Path,
             include_osm: bool = True) -> tuple[Path, Path]:
    """Run the full AU pipeline. Returns (zones_db_path, report_path).

    Processes one state at a time -- fetcher + matching OSM bbox +
    cross_verify -- to keep peak memory bounded by the largest state's
    working set rather than the sum of all states. The first global
    accumulator (`all_segs`) blew past the GitHub-hosted 7 GB runner
    on 2026-05-20 around 31M SpeedSegment instances (NSW + VIC + WA +
    ACT + OSM-NSW + OSM-VIC + parsing OSM-QLD) -- runner self-cancels
    once swap thrashing dominates. This per-state loop holds at most
    one state's gov+OSM segments before they collapse into VerifiedRecords.
    """
    import gc

    selected = [(code, factory) for code, factory in AU_FETCHERS
                if not states or code in states]
    if not selected:
        print("[build_db] no fetchers selected -- nothing to do")
        return Path(), Path()

    all_verified: list = []
    all_quarantine: list = []

    for code, factory in selected:
        print(f"[build_db] === processing {code} ===")
        state_segs: list = []
        if factory is None:
            # OSM-only state (sa/nt/tas) -- skip the gov-source step
            # entirely; OSM is the sole producer for this iteration.
            print(f"[build_db]   (no gov fetcher; OSM-only state)")
        else:
            try:
                state_segs = factory().fetch()
            except Exception as exc:  # pragma: no cover -- surface to op
                print(f"[build_db] ERROR: {code} fetch failed: {exc}",
                      file=sys.stderr)
                print(f"[build_db] continuing without {code}; expect "
                      f"lower confidence in that region", file=sys.stderr)
                continue
            print(f"[build_db]   -> {len(state_segs):,} gov segments "
                  f"from {code}")

        osm_segs: list = []
        if include_osm:
            try:
                osm_segs = au_osm.OSMAUFetcher(
                    states=[code.upper()]).fetch()
                print(f"[build_db]   -> {len(osm_segs):,} OSM segments "
                      f"for {code.upper()}")
            except Exception as exc:  # pragma: no cover -- surface to op
                print(f"[build_db] WARNING: OSM fetch for {code} failed: "
                      f"{exc}; verified records cap at OFFICIAL_ONLY",
                      file=sys.stderr)

        combined_count = len(state_segs) + len(osm_segs)
        print(f"[build_db] cross-verifying {combined_count:,} segments "
              f"for {code}...")
        verified, quarantine = cv.verify_segments(state_segs + osm_segs)
        print(f"[build_db]   -> verified={len(verified):,} "
              f"quarantine={len(quarantine):,}")
        all_verified.extend(verified)
        all_quarantine.extend(quarantine)

        # Drop the now-redundant per-state working set before the next
        # iteration. The verified/quarantine records we kept are MUCH
        # smaller than the raw segments (1 verified per ~30 m group),
        # so the accumulator grows slowly while peak resets each state.
        del state_segs, osm_segs, verified, quarantine
        gc.collect()

    summary = cv.summarize(all_verified, all_quarantine)
    print(f"[build_db] TOTAL verified={len(all_verified):,} "
          f"quarantine={len(all_quarantine):,}")
    verified = all_verified
    quarantine = all_quarantine

    output_dir.mkdir(parents=True, exist_ok=True)
    zones_path = output_dir / "speed_zones.db"
    _write_zones_db(zones_path, verified)

    # Sign the freshly-written DB so the on-device loader will accept
    # it. Key resolution: AI_HUD_DB_SECRET env (set by GitHub Actions)
    # -> dev fallback. The device verifies with the same algorithm.
    key = db_signer.load_key()
    sig_path = db_signer.sign_file(zones_path, key)
    print(f"[build_db] signed -> {sig_path} ({sig_path.stat().st_size} bytes)")

    date = _dt.date.today().isoformat()
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    cv.write_quarantine(quarantine, quarantine_dir / f"au_zones_{date}.jsonl")
    report = quarantine_dir / f"au_summary_{date}.md"
    _write_quarantine_report(quarantine, report, summary)

    # Machine-readable companion. Used by tools/db_health.py to decide
    # whether the auto-generated PR is safe to auto-merge. We dump the
    # raw summary dict plus a couple of derived fields the health
    # scorer needs but doesn't want to recompute (zone count, file
    # size). Keeping this separate from the .md keeps the human and
    # machine read paths from drifting.
    summary_json = quarantine_dir / f"au_summary_{date}.json"
    summary_json.write_text(json.dumps({
        **summary,
        "zones_db_path": str(zones_path),
        "zones_db_size": zones_path.stat().st_size,
        "build_date": date,
    }, separators=(",", ":")) + "\n")

    return zones_path, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="au",
                    help="Currently only 'au' is implemented in the new "
                         "pipeline; 'cn' continues to use prepare_speed_db.py")
    ap.add_argument("--states", default="",
                    help="Comma-separated state codes to include "
                         "(nsw,vic,qld,wa,act); empty = all")
    ap.add_argument("--output-dir", default=None,
                    help="Output dir for .db (default: data/)")
    ap.add_argument("--quarantine-dir", default=None,
                    help="Output dir for quarantine reports "
                         "(default: data/quarantine/)")
    args = ap.parse_args()

    if args.region != "au":
        print(f"ERROR: region '{args.region}' not supported by build_db.py; "
              f"use prepare_speed_db.py for cn", file=sys.stderr)
        sys.exit(2)

    output_dir = Path(args.output_dir or _REPO_ROOT / "data")
    quarantine_dir = Path(args.quarantine_dir
                          or _REPO_ROOT / "data" / "quarantine")
    states = [s.strip().lower() for s in args.states.split(",") if s.strip()]

    zones_path, report = build_au(states, output_dir, quarantine_dir)
    print(f"\n{'='*60}")
    print(f"Build complete.")
    print(f"  DB:     {zones_path}")
    print(f"  Report: {report}")


if __name__ == "__main__":
    main()

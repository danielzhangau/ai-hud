#!/usr/bin/env python3
"""Measure NPU false-positive rate against a static empty scene.

Runs on the device. Watches /tmp/ai_hud_detect for the configured
duration, counts how often the NPU produces a non-zero speed_limit
when there is no sign in the camera view, and reports the rate.

Use this to validate the BOX_THRESH change in postprocess.h:
  - Point the camera at a static empty scene (wall, desk, sky).
  - Disable GPS-based DB lookup contamination by running with NPU
    only (no GPS fix or DB present matters here -- we read the raw
    IPC, which is pre-fusion).
  - Run: python3 measure_false_positives.py [duration_s]
  - Default duration: 30 seconds.

Output:
  - inference rate (Hz) observed
  - false-positive event count + rate per second
  - per-class breakdown (which speed limits got mis-fired)
  - assessment vs. acceptable threshold (< 0.1 FP/s recommended)
"""

import os
import sys
import time

IPC_FILE = "/tmp/ai_hud_detect"


def parse_ipc(path):
    """Read the IPC file and return (speed_limit, confidence, camera).
    Returns (None, None, None) on any failure."""
    try:
        with open(path, "r") as f:
            kv = {}
            for line in f:
                line = line.strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k] = v
        speed = int(kv.get("speed_limit", 0))
        conf = float(kv.get("confidence", 0.0))
        cam = kv.get("camera", "0").strip() == "1"
        return speed, conf, cam
    except (OSError, ValueError):
        return None, None, None


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    if not os.path.exists(IPC_FILE):
        print(f"[FATAL] {IPC_FILE} not found -- is the ai-hud binary running?")
        return 1

    print(f"Measuring NPU false-positive rate over {duration}s.")
    print(f"  IPC file: {IPC_FILE}")
    print("  Point the camera at an empty scene now. Press Ctrl+C to abort.\n")

    inference_events = 0      # total IPC mtime updates seen
    fp_events = 0             # non-zero speed_limit events (no sign expected)
    cam_events = 0            # spurious camera detections
    conf_sum = 0.0            # for averaging FP confidence
    per_class = {}            # speed_limit -> count
    last_mtime = 0.0

    t_start = time.time()
    t_end = t_start + duration

    try:
        while time.time() < t_end:
            try:
                mtime = os.stat(IPC_FILE).st_mtime
            except OSError:
                time.sleep(0.05)
                continue

            if mtime == last_mtime:
                time.sleep(0.05)  # poll faster than NPU rate (~4 Hz)
                continue
            last_mtime = mtime

            speed, conf, cam = parse_ipc(IPC_FILE)
            if speed is None:
                continue

            inference_events += 1
            if speed > 0:
                fp_events += 1
                conf_sum += conf
                per_class[speed] = per_class.get(speed, 0) + 1
            if cam:
                cam_events += 1
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user")

    actual_duration = time.time() - t_start

    # -------------------- Report --------------------
    print(f"\n{'='*52}")
    print("False-positive measurement results")
    print(f"{'='*52}")
    print(f"Duration:          {actual_duration:.1f}s")
    print(f"Inference events:  {inference_events} "
          f"({inference_events/actual_duration:.1f} Hz)")
    print(f"FP speed_limit:    {fp_events} events "
          f"({fp_events/actual_duration:.3f}/s)")
    print(f"FP camera:         {cam_events} events "
          f"({cam_events/actual_duration:.3f}/s)")

    if fp_events > 0:
        avg_conf = conf_sum / fp_events
        print(f"Avg FP confidence: {avg_conf:.3f}")
        print(f"\nPer-class breakdown (which classes mis-fired):")
        for limit in sorted(per_class.keys()):
            n = per_class[limit]
            pct = 100.0 * n / fp_events
            print(f"  {limit:>3} km/h: {n:>4} events ({pct:5.1f}%)")

    # -------------------- Assessment --------------------
    print(f"\n{'-'*52}")
    fp_rate = fp_events / actual_duration if actual_duration > 0 else 0
    if fp_rate < 0.1:
        verdict = "EXCELLENT -- threshold is well-tuned"
    elif fp_rate < 0.5:
        verdict = "ACCEPTABLE -- a few false positives, fusion will filter"
    elif fp_rate < 2.0:
        verdict = "MARGINAL  -- consider raising BOX_THRESH another 0.05"
    else:
        verdict = "POOR      -- threshold or model needs investigation"
    print(f"Assessment: {verdict}  ({fp_rate:.2f} FP/s)")
    print(f"{'-'*52}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

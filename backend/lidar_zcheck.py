#!/usr/bin/env python3
"""Sanity-check the Go2 lidar cloud's ground-plane height (run with DIMOS_PY).

The CostMapper obstacle fix (docs/patches/) assumes the firmware's world-frame
cloud has the floor near z=0 so the lethal band 0.15–2.0m catches obstacles.
Run this once after powering the robot on (blueprint must be running) BEFORE
trusting navigation: if `ground_z` is not ~0 (e.g. -0.32 = odom origin at body
height), the band in the patch must be shifted accordingly.

Usage (on the machine running the blueprint):
    $DIMOS_PY backend/lidar_zcheck.py [--transport lcm]

Exit codes: 0 band looks sane, 1 no lidar frame, 2 band misaligned.
"""
import argparse
import json
import sys
import time

MIN_H, MAX_H = 0.15, 2.0  # keep in sync with docs/patches/apply_costmapper_patch.py


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", default="lidar", help="peek_stream name (no slash)")
    ap.add_argument("--transport", default=None)
    ap.add_argument("--wait", type=float, default=30.0)
    args = ap.parse_args()

    import numpy as np
    from dimos.core.global_config import global_config
    if args.transport:
        global_config.update(transport=args.transport)
    from dimos import Dimos

    app = Dimos.connect()
    deadline = time.time() + args.wait
    frame = None
    while time.time() < deadline:
        try:
            frame = app.peek_stream(args.stream, 2.0)
        except Exception:  # noqa: BLE001
            frame = None
        if frame is not None:
            break
        time.sleep(0.5)
    if frame is None:
        print(json.dumps({"ok": False, "error": f"no '{args.stream}' frame in {args.wait}s"}))
        return 1

    pts, _ = frame.as_numpy()
    z = pts[:, 2]
    ground_z = float(np.percentile(z, 10))
    in_band = int(((z >= MIN_H) & (z <= MAX_H)).sum())
    report = {
        "ok": True,
        "n_points": int(len(z)),
        "z_p10_ground": round(ground_z, 3),
        "z_p50": round(float(np.percentile(z, 50)), 3),
        "z_p90": round(float(np.percentile(z, 90)), 3),
        "band": [MIN_H, MAX_H],
        "n_in_band": in_band,
        "band_aligned": bool(-0.10 <= ground_z <= 0.10),
    }
    print(json.dumps(report))
    if not report["band_aligned"]:
        print(f"WARNING: ground at z={ground_z:.2f}, not ~0 — shift the patch band "
              f"to [{ground_z + MIN_H:.2f}, {ground_z + MAX_H:.2f}]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

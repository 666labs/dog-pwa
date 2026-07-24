#!/usr/bin/env python3
"""Apply the Go2 navigation fixes to an installed dimos package (idempotent).

Round 1 — obstacle visibility (see README.md "costmapper-simple"):
  CostMapper height_cost -> simple absolute z-band (0.15-2.0m lethal).
  Stock behavior made walls/people/tall boxes invisible (cost 0) and thin
  obstacles sub-lethal, so the robot plowed straight into them.

Round 2 — clearance (see README.md "clearance"):
  In the venue only a narrow scanned corridor is known-free; A* pays 80/cell
  for UNKNOWN so it hugs the inflation boundary instead of taking a berth
  through unscanned space. Stock inflation (robot_width 0.3 x 1.1 / 2 =
  0.165m) < the Go2's 0.35m body-sweep radius -> guaranteed grazes.
  Measured in a corridor repro: stock clearance 0.35m (graze), fixed 0.55m.
    - robot_width 0.3 -> 0.5 (inflation 0.165 -> 0.275m at the 1.1x floor,
      wider is_obstacle_ahead path mask)
    - restore GlobalPlanner._find_wide_path wide-first size ladder
      [1.1] -> [2.2, 1.7, 1.3, 1.1] (0.55m berth when space allows)
    - VoxelGridMapper emit_every 5 -> 2 (fresh obstacles mapped sooner)
    - nerf_speed=0.6 (0.55 -> 0.33 m/s, requested demo pacing)

Companion (not in this script): backend/vendor_config.json arrival_radius_m
0.35 -> 0.5, so safe-goal displacement near tables doesn't false-timeout the
nav leg.

Usage (on the machine whose dimos install should be patched):
    <dimos-env>/bin/python3 apply_costmapper_patch.py

Backups written next to the originals: unitree_go2.py.orig-costmap,
global_planner.py.orig-clearance — restore them to roll back.
"""
import shutil
import sys

try:
    import dimos.navigation.replanning_a_star.global_planner as gp_mod
    import dimos.robot.unitree.go2.blueprints.smart.unitree_go2 as bp_mod
except ImportError as e:
    sys.exit(f"cannot import dimos (run with the dimos env's python): {e}")

BP_PATH = bp_mod.__file__
GP_PATH = gp_mod.__file__

OLD_IMPORT = "from dimos.mapping.costmapper import CostMapper\n"
NEW_IMPORT = OLD_IMPORT + "from dimos.mapping.pointclouds.occupancy import SimpleOccupancyConfig\n"

OLD_COSTMAPPER = "    CostMapper.blueprint(),\n"
NEW_COSTMAPPER = (
    "    # PATCHED (AdventureX 2026-07-25): default height_cost algo makes obstacles\n"
    "    # taller than can_pass_under=0.6m (walls, people, tall boxes) invisible and\n"
    "    # keeps thin legs below the lethal threshold -> robot plows into them.\n"
    "    # simple = absolute z-band occupancy: everything 0.15-2.0m above ground is\n"
    "    # lethal. Backup of original: unitree_go2.py.orig-costmap\n"
    "    CostMapper.blueprint(\n"
    '        algo="simple",\n'
    "        config=SimpleOccupancyConfig(min_height=0.15, max_height=2.0),\n"
    "    ),\n"
)

OLD_VOXEL = "    VoxelGridMapper.blueprint(emit_every=5),\n"
NEW_VOXEL = "    VoxelGridMapper.blueprint(emit_every=2),\n"

OLD_GC = ').global_config(n_workers=10, robot_model="unitree_go2")'
NEW_GC = ').global_config(n_workers=10, robot_model="unitree_go2", nerf_speed=0.6, robot_width=0.5)'

OLD_SIZES = "        sizes_to_try: list[float] = [1.1]\n"
NEW_SIZES = "        sizes_to_try: list[float] = [2.2, 1.7, 1.3, 1.1]\n"


def patch_file(path, backup_suffix, replacements, marker):
    src = open(path).read()
    if marker in src:
        print(f"already patched: {path}")
        return
    for old, _ in replacements:
        if src.count(old) != 1:
            sys.exit(f"anchor not found ({old!r:.60}) — dimos version drift? inspect {path}")
    shutil.copy2(path, path + backup_suffix)
    for old, new in replacements:
        src = src.replace(old, new)
    open(path, "w").write(src)
    print(f"patched OK: {path}  (backup: {path}{backup_suffix})")


patch_file(
    BP_PATH,
    ".orig-costmap",
    [(OLD_IMPORT, NEW_IMPORT), (OLD_COSTMAPPER, NEW_COSTMAPPER),
     (OLD_VOXEL, NEW_VOXEL), (OLD_GC, NEW_GC)],
    marker="SimpleOccupancyConfig",
)
patch_file(
    GP_PATH,
    ".orig-clearance",
    [(OLD_SIZES, NEW_SIZES)],
    marker="[2.2, 1.7, 1.3, 1.1]",
)

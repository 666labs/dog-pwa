#!/usr/bin/env python3
"""Apply the CostMapper obstacle-visibility fix to an installed dimos package.

Why: the stock `unitree-go2` blueprint runs CostMapper with the default
`height_cost` (terrain-slope) algorithm. Its `can_pass_under=0.6` heuristic
treats any grid cell containing both floor points and points >0.6m up as
"robot can pass underneath" and takes the floor height, so walls, tall boxes
and standing people produce ZERO cost — the planner drives straight through
them. Its gaussian smoothing also keeps thin obstacles (chair legs: max 75)
below the lethal threshold (100) that both path inflation and the local
planner's `is_obstacle_ahead()` check require. Verified empirically with
synthetic scenes on dimos 0.0.14b1 (see docs/patches/README.md).

Fix: switch CostMapper to the `simple` absolute-height-band algorithm —
every point 0.15–2.0m above ground marks its cell lethal (100).

Usage (on the machine whose dimos install should be patched):
    <dimos-env>/bin/python3 apply_costmapper_patch.py

Idempotent: refuses to double-apply. Original backed up as
`unitree_go2.py.orig-costmap` next to the target; restore it to roll back.
"""
import shutil
import sys

try:
    import dimos.robot.unitree.go2.blueprints.smart.unitree_go2 as target_mod
except ImportError as e:
    sys.exit(f"cannot import dimos (run with the dimos env's python): {e}")

PATH = target_mod.__file__

OLD_IMPORT = "from dimos.mapping.costmapper import CostMapper\n"
NEW_IMPORT = OLD_IMPORT + "from dimos.mapping.pointclouds.occupancy import SimpleOccupancyConfig\n"

OLD_LINE = "    CostMapper.blueprint(),\n"
NEW_LINE = (
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

# Demo pacing: nerf_speed multiplies the local planner's 0.55 m/s cruise speed
# (0.6 -> ~0.33 m/s), requested for supervised venue runs.
OLD_GC = ').global_config(n_workers=10, robot_model="unitree_go2")'
NEW_GC = ').global_config(n_workers=10, robot_model="unitree_go2", nerf_speed=0.6)'

src = open(PATH).read()

if "SimpleOccupancyConfig" in src:
    sys.exit(f"already patched: {PATH}")
if src.count(OLD_IMPORT) != 1 or src.count(OLD_LINE) != 1 or src.count(OLD_GC) != 1:
    sys.exit(f"anchors not found — dimos version drift? inspect {PATH} manually")

shutil.copy2(PATH, PATH + ".orig-costmap")
patched = (
    src.replace(OLD_IMPORT, NEW_IMPORT).replace(OLD_LINE, NEW_LINE).replace(OLD_GC, NEW_GC)
)
open(PATH, "w").write(patched)
print(f"patched OK: {PATH}")
print(f"backup:     {PATH}.orig-costmap")

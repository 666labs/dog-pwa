# dimOS patches applied on the Ascent

These are minimal, documented edits to the dimos install at
`~/dimos-env/lib/python3.12/site-packages/dimos/` on the Ascent GX10
(`asus@10.76.4.120`). They are **not** applied to any repo code — this folder
exists so the patches survive a dimos reinstall and so the reasoning isn't
lost. Re-apply after any `pip install`/upgrade of dimos with:

```bash
~/dimos-env/bin/python3 docs/patches/apply_costmapper_patch.py
```

## unitree-go2-costmapper-simple (2026-07-25)

**Symptom:** during vendor-demo navigation the Go2 drove straight into
obstacles — no avoidance at all.

**Root cause** (verified empirically on dimos 0.0.14b1 with synthetic scenes,
run in the Ascent's real dimos env): the `unitree-go2` blueprint's CostMapper
defaults to the `height_cost` terrain-slope algorithm:

1. Its `can_pass_under=0.6` heuristic takes the *floor* height for any cell
   containing both floor points and points >0.6m up (intended for overhangs).
   Solid tall objects — **walls, tall boxes, standing people — produce cost 0
   and are completely invisible** to the planner.
2. Its gaussian smoothing spreads step edges over several cells, so thin
   obstacles (chair legs) top out at cost ~75 — below the lethal threshold
   (100) that obstacle inflation, `find_safe_goal`, and the local planner's
   `is_obstacle_ahead()` (which tests `== 100` exactly) all require.

Measured (obstacle between robot and a 2m goal; A* + straight-line clearance):

| scene            | height_cost (stock)             | simple (patched)   |
|------------------|---------------------------------|--------------------|
| box h=0.3/0.5m   | lethal, avoided                 | lethal, avoided    |
| tall box h=1.2m  | **cost 0 — plows through**      | lethal, avoided    |
| wall h=1.5m      | **cost 0 — plows through**      | lethal, avoided    |
| person legs h=1m | **cost 0 — plows through**      | lethal, avoided    |
| chair legs h=.45 | max 75 — safety check never fires | lethal, avoided  |

**Fix:** switch CostMapper to the `simple` absolute-height-band algorithm:
any point 0.15–2.0m above ground marks its cell lethal (100). Values are
exact 0/100/-1, so the `== 100` clearance check and `>= 100` inflation both
work. Repro script preserved at `docs/patches/costmap_repro.py`.

**Assumption to verify at power-on:** the firmware world frame has the floor
near z=0 (odom z≈0.32 is body height above ground, implying yes). Verify with
one live frame before the first nav run:

```bash
DIMOS_PY=~/dimos-env/bin/python3
$DIMOS_PY backend/lidar_zcheck.py          # blueprint must be running
```

If it warns that the ground is not near z=0, shift `min_height`/`max_height`
in the patch by the reported offset and re-apply.

**Rollback:**

```bash
F=~/dimos-env/lib/python3.12/site-packages/dimos/robot/unitree/go2/blueprints/smart/unitree_go2.py
cp "$F.orig-costmap" "$F"
```

## unitree-go2-clearance (2026-07-25, round 2)

**Symptom:** with round 1 applied, a supervised run passed nearly touching an
obstacle ("almost knocked it over").

**Root cause** (reproduced offline in a corridor scene): in the venue only the
narrow strip the robot has scanned is known-free; everything else is UNKNOWN,
which A* prices at 80/cell. The cheapest route past an obstacle is therefore
the known sliver hugging the inflation boundary. Stock inflation is
`robot_width 0.3 x 1.1 / 2 = 0.165m` — less than the Go2's ~0.35m body-sweep
radius (0.70m long body), so a planned "pass" is a physical graze. The
wide-berth ladder in `GlobalPlanner._find_wide_path` that would have preferred
more clearance is commented down to `[1.1]` in dimos 0.0.14b1.

Measured (corridor scene, min path-to-obstacle distance; body sweep = 0.35m):

| config                              | clearance | verdict     |
|-------------------------------------|-----------|-------------|
| stock (width 0.3, sizes [1.1])      | 0.35m     | graze       |
| fixed (width 0.5, sizes [2.2…1.1])  | 0.55m     | ~0.2m margin|

Open-world and 1.2m-gap scenes stay passable (the ladder falls back to
narrower inflation only when needed). Repro: `clearance_test.py`.

**Fix (all in the same two patched files):**
- `robot_width` 0.3 → 0.5 via blueprint `global_config` (harder inflation,
  wider `is_obstacle_ahead` mask)
- `_find_wide_path` sizes `[1.1]` → `[2.2, 1.7, 1.3, 1.1]`
- `VoxelGridMapper` `emit_every` 5 → 2 (costmap integrates fresh obstacles
  ~2.5x sooner while walking)
- `nerf_speed=0.45` (0.55 → ~0.25 m/s cruise; was 0.6/0.33 until 2026-07-25,
  lowered further for the slow obstacle-avoidance test run)
- companion: `backend/vendor_config.json` `arrival_radius_m` 0.35 → 0.5
  (safe-goal displacement near tables must not false-timeout the nav leg)

**Files:**
- `unitree-go2-costmapper-simple.patch` — full diff of the blueprint file (rounds 1+2)
- `replanning-astar-wide-sizes.patch` — diff of global_planner.py (round 2)
- `apply_costmapper_patch.py` — idempotent applier for both files (survives reinstall)
- `costmap_repro.py` — round-1 evidence script
- `clearance_test.py` — round-2 evidence script

**Not changed:** link-stability (WebRTC drops mid-run ~30-90s into navigation,
`accept_track` callback error, no auto-reconnect watchdog) is a separate known
issue — it killed both supervised runs short of the goal and remains the top
demo risk.

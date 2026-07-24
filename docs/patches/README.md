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

**Files:**
- `unitree-go2-costmapper-simple.patch` — the exact diff applied
- `apply_costmapper_patch.py` — idempotent applier (survives reinstall)
- `costmap_repro.py` — synthetic-scene evidence script (`~/dimos-env/bin/python3 costmap_repro.py`)

**Not changed:** link-stability (WebRTC drops mid-run, no auto-reconnect
watchdog) is a separate known issue — this patch only fixes obstacle
visibility.

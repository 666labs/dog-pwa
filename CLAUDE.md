# CLAUDE.md — dimos-pwa

This file makes this repo self-sufficient for a fresh Claude Code session
opened directly here, with no dependency on any other conversation's context.

## 1. Project context

This repo is one component of an AdventureX 2026 hackathon project: a fully
offline vending-machine / delivery-boy demo. A Galaxea A1Z robot arm picks an
ArUco-tagged drink and loads it into a Unitree Go2 quadruped's basket; the Go2
then navigates and delivers it to the requester. Everything runs local/offline
on DimOS (dimensionalOS) — the "unplug the internet on stage and it still
works" moment is the core differentiator of the demo, so nothing in this
project should introduce a hard dependency on any cloud/internet service. The
project is split into roughly 11 components, each its own git repo under
`/home/alex/dev/AdventureX/`. See section 7 for the map.

## 2. This component's role

`dimos-pwa` is the Unitree Go2's **control panel**: a FastAPI backend
(`backend/main.py`, `backend/dimos_cli.py`) + vanilla JS PWA frontend
(`frontend/index.html`, `frontend/app.js`), served together on **port 8090**.
It is a sibling of `dimos-arm-pwa` (the A1Z arm panel, port 8091) — separate
app, historically no cross-communication.

It never imports dimOS Python modules directly at request time for the main
panel — it shells out to the `dimos` CLI as subprocesses (some sidecar
daemons, e.g. `camera_daemon.py`/`lidar_daemon.py`/`yolo_watch_daemon.py`, do
`import dimos` themselves but run as separate processes under the dimOS conda
python, spawned via `DIMOS_BIN`'s sibling interpreter — see `dimos_cli.py`).

It also owns the **vending delivery orchestration**: `backend/vendor.py` is a
single-order asyncio state machine (idle → arm_picking → dog_delivering →
awaiting_pickup → dog_returning → delivered) that calls out to the arm's pick
service and drives the Go2's real navigation via a `nav_leg.py` subprocess.
This is the actual demo-night driver, not just a teleop convenience panel.

## 3. Current state

As of this session (2026-07-26, early morning, hours before a 9AM demo):

- **Pre-existing, live-tested, DO NOT TOUCH OR REGRESS:** the teleop /
  camera / lidar / telemetry dashboard (`frontend/index.html`'s `view-dashboard`
  + all of `frontend/app.js`). The user has explicitly confirmed this has
  "perfect control with no latency whatsoever." `frontend/app.js` was verified
  byte-identical to the initial commit at the end of this session (`git diff`
  against `bfd0af3` is empty) — nothing here was modified.
- **This session's changes:**
  - **Committed hours of previously-untracked, unprotected work** that was
    sitting only on disk: `vendor.py` (the vending state machine),
    `connection_watchdog.py`, `network_diagnostics.py`, `nav_leg.py`,
    `vendor_config.json`, `arm_camera_daemon.py`, `yolo_watch_daemon.py`,
    `yolo11n.pt`, `vendor_patches/`, `lidar_zcheck.py`, plus prior uncommitted
    edits to `main.py`/`dimos_cli.py`. Two commits total this session,
    `aaeda67` (checkpoint) and a second with the build below. Not pushed.
    Two `*.pre-ascent-sync.bak` files were deliberately left untracked (local
    backup copies, not real project files).
  - **Built `frontend/vendor.html` + `frontend/vendor.js`** (new files) — the
    vending-demo operator UI: drink picker (from `/api/vendor/menu`), live
    state-machine status (polls `/api/vendor/status` every ~1.2s, color-coded
    big state label), confirm-pickup button, e-stop + release, reset,
    `fake_dog`/`fake_arm` dry-run mode badge, collapsible raw nav-log viewer.
    `frontend/style.css` got ~79 additive `v-`-prefixed rules; `index.html`
    got exactly one added line (`<a href="/vendor">` link) — everything else
    in those two files is untouched.
  - **Wired `_arm_pick()` in `vendor.py` to a real HTTP call** (previously a
    stub that just slept). See §4 for the exact contract and a **live port
    discrepancy that needs reconciling before demo**.
  - **Added manually-armed process management for the two safety sidecars**
    that were built but never wired in: `connection_watchdog.py` (WebRTC
    silent-death detection + reconnect-only recovery) and
    `yolo_watch_daemon.py` (camera-based collision-corridor trigger). New
    manager classes in `dimos_cli.py` + REST endpoints in `main.py` under
    `/api/watchdog/*` and `/api/yolo-watch/*`. Deliberately **not**
    auto-started anywhere (not on boot, not on `/api/run`, not on
    `/api/run-with-retry`) — an operator must explicitly POST to arm them.
    No frontend control panel for these two yet (see §8).
  - All changes verified **statically only**: `python3 -m py_compile` /
    `ast.parse` on every touched backend file, manual diff review, route
    dedup check, HTML/JS structural checks. **Nothing was executed against a
    live server, a live robot, or a live arm connection this session.**

## 4. Interfaces

**Exposes** (all on port 8090, relative-path API, redeploy-safe):

Pre-existing dashboard/robot-lifecycle API (unchanged this session — see
`README.md` for the long-form description): `GET /api/blueprints`,
`GET /api/status`, `POST /api/run`, `POST /api/run-with-retry`,
`POST /api/stop`, `POST /api/restart`, teleop send, sport/gesture RPCs,
`GET /api/camera/{cam}/{stream.mjpg,snapshot.jpg,health}`,
`GET /api/lidar-stream`, `GET /api/log` (SSE), `GET /api/topic-rate`,
`GET /api/processes`, `POST /api/server/shutdown`, plus the network-
diagnostics endpoints (`network_diagnostics.py`'s collector, auto-started/
stopped with each run — see `dimos_cli.py`'s `diag_start`/`diag_kill`).

Vending state machine (`backend/vendor.py`, mounted via `app.include_router`,
routes verified against source — do not guess these, this list is exact):
- `GET  /api/vendor/menu` → `{"drinks": [{id, name, name_en, icon, color}]}`
- `POST /api/vendor/order` (form `drink_id`) → `{order_id}` or 409
  `{"error": "order_in_progress"|"estopped", "current": <status>}`
- `GET  /api/vendor/status` → `{state, order_id, drink, error, dist_to_goal,
  stage_started_at, fake_dog, fake_arm, pose, map, events:[...],
  estopped_at}` — states: `idle, arm_picking, dog_delivering,
  awaiting_pickup, dog_returning, delivered, failed, estopped`
- `POST /api/vendor/confirm` — only valid in `awaiting_pickup`
- `POST /api/vendor/reset` — no-ops while `estopped` (must explicitly release)
- `GET  /api/vendor/navlog?lines=40` — raw `nav_leg.py` subprocess log tail
- `POST /api/vendor/estop` / `POST /api/vendor/estop/release`
- `GET  /vendor` → serves `frontend/vendor.html`

Manually-armed safety sidecars (new this session, opt-in only):
- `POST /api/watchdog/{start,stop}`, `GET /api/watchdog/status` — arms
  `connection_watchdog.py`. `start` takes form `dry_run: bool` (default
  false); dry-run does full detection + IP re-discovery but stops short of
  actually POSTing the recovery.
- `POST /api/yolo-watch/{start,stop}`, `GET /api/yolo-watch/status` — arms
  `yolo_watch_daemon.py` (form `transport`, `topic`, `conf`, all optional).

**Calls out to:**
- `dimos` CLI binary (`DIMOS_BIN`, default
  `/home/alex/miniconda3/envs/dimos/bin/dimos`) — all robot control/telemetry.
- **`a1z-arm-control`'s `/pick_drink` HTTP endpoint**, from `vendor.py`'s
  `_arm_pick()`: `POST {ARM_CONTROL_URL}/pick_drink {"drink_id": str}` →
  `{"success": bool, "error": str|null, "residual_mm": number|null}`.
  `ARM_CONTROL_URL` env var, **defaults to `http://localhost:8100`** — this
  is what this session actually shipped and is what's live in `vendor.py`
  right now.

  > **⚠️ PORT MISMATCH — needs reconciling before demo, do not trust 8100 as
  > fact.** `a1z-arm-control`'s own README shows its example run command as
  > `--port 8420`, and the sibling `dimos-arm-pwa` repo's `CLAUDE.md`
  > independently flags this exact same 8100-vs-8420 ambiguity for its own
  > `A1Z_PICK_BASE_URL` (same default, same caveat). This is a real,
  > previously-known, still-unresolved cross-repo inconsistency, not a new
  > one introduced this session. **Before the demo: check what
  > `a1z-arm-control` actually binds to** (its own startup script/logs) and
  > either export `ARM_CONTROL_URL` correctly when launching this panel, or
  > be ready to override it. Unlike `dimos-arm-pwa`, this panel does **not**
  > currently have a UI field to override `ARM_CONTROL_URL` live — it's
  > env-var-only. Consider adding one if this isn't resolved by demo time.
  - Response contract confirmed by reading `a1z-arm-control`'s
    `arm_control/service.py` `/pick_drink` docstring directly: always HTTP
    200, body always has exactly `{"success", "error", "residual_mm"}` plus
    additive diagnostic fields (`stage`, `grasp_reach_error_mm`, etc.) safe to
    ignore. `_arm_pick()` matches this contract correctly.
- `connection_watchdog.py`'s only outbound side effect: `POST
  /api/run-with-retry` on THIS SAME panel (self-call, not another repo).

## 5. Known issues / risks

- **`ARM_CONTROL_URL` port mismatch (8100 vs 8420, see §4)** — biggest live
  cross-repo risk touching this file right now.
- **No frontend control for the watchdog/YOLO sidecars.** The backend
  endpoints exist and are safe to call, but nothing in `vendor.html` or
  `index.html` surfaces "arm watchdog" / "arm yolo-watch" buttons yet. If you
  want the connection-recovery or collision-corridor safety nets live during
  the demo, someone has to `curl -X POST` them manually or a UI needs adding.
- **`a1z-arm-control`'s `/pick_drink` may not be up at all** at demo time —
  it's being built in parallel by a different lead. `_arm_pick()` is
  defensive (connection-refused/timeout/bad-JSON all become a clean
  `VendorError` → `failed` state, never a crash), and `VENDOR_FAKE_ARM=1`
  (mirrors the pre-existing `VENDOR_FAKE_DOG=1`) gives a fully offline
  fallback demo path with zero external dependencies.
- **`connection_watchdog.py` / `yolo_watch_daemon.py` have never been run**
  this session, by design (static verification only — see §6). First arm
  should be a human watching, `dry_run=true` for the watchdog.
- **No automated tests** anywhere in this repo. Everything is verified by
  `py_compile`/`ast.parse`/manual review, never a live server or robot.
- `backend/*.pre-ascent-sync.bak` files exist on disk (local backups from a
  prior Ascent-box sync) and are deliberately left untracked — noise, not a
  gap.

## 6. Safety constraints (hard rules — read before touching anything here)

- **Never autonomously execute or run anything that would move the real
  Go2** — no launching blueprints, no sending nav goals, no `dimos run`, no
  calling `/api/vendor/order` (which drives real arm+dog motion end to end)
  against a live, non-fake backend — unless a human has explicitly granted
  live-hardware authorization *in the current session*. Default assumption
  in any fresh session on this repo: **you do not have that authorization.**
- Code and prepare changes; verify **statically only** (syntax checks,
  `ast.parse`, reading logs). No live execution against hardware.
- If live testing is genuinely needed, write out the exact steps for a human
  to run themselves — do not run them yourself.
- **`connection_watchdog.py` and `yolo_watch_daemon.py` must never be made to
  auto-arm.** Both are capable of taking action near a robot a human may be
  standing next to (the watchdog can relaunch a blueprint on its own
  initiative on trip; the corridor watch feeds a safety trigger). Keep them
  strictly opt-in via their REST endpoints — do not wire either into panel
  boot, `/api/run`, `/api/run-with-retry`, or any other lifecycle event.
  Surface state/controls in the UI instead of silently automating.
- `connection_watchdog.py`'s recovery is explicitly scoped to the
  data/telemetry link only — it must never resume, re-send, or retry a
  movement/nav goal. If you ever touch that file, preserve that boundary
  (its own docstring has a "SAFETY SCOPE" section — read it first).
- The vending state machine's e-stop (`POST /api/vendor/estop`) cancels the
  order task, kills the nav subprocess, and bursts zero-velocity Twists — it
  is designed to always succeed and be idempotent. Don't weaken that.

## 7. Sibling components map

(Brief orientation only — go to each repo's own README/CLAUDE.md for detail.)

| Name | Repo / location | Role |
| --- | --- | --- |
| **dog_pwa** (this repo) | `dimos-pwa`, port 8090 | Go2 control panel + vendor delivery state machine |
| **arm_pwa** | `dimos-arm-pwa`, port 8091 | A1Z manual-teleop control panel |
| **arm_control** | `a1z-arm-control` | Low-level A1Z HTTP service; owns `/pick_drink` (port TBD, see §4/§5) |
| **vision_pipeline** | `a1z-vision-calibration` | ArUco detection / hand-eye calibration; exposes `get_grasp_pose()`, imported in-process by `a1z-arm-control` |
| **dog_nav_obstacle_avoidance** | `dog-nav-obstacle-avoidance` | Go2 navigation/planner |
| **networking** | `/home/alex/dev/AdventureX/networking` | Venue/LAN networking setup, connectivity forensics docs (`BRIEF_connectivity_watchdog.md`, `CONNECTIVITY.md`, `TODO.md`) |
| **demo_final** | `/home/alex/dev/AdventureX/demo_final` | Integration + runbook repo. Branch convention: `main` = demo-worthy, `try/*` = experiments, `wip/*` = in-progress |
| **perception / consumer_app / ascent / dog_control** | likely live on the Ascent GX10 box | Not inventoried from this session — SSH to the GX10 was unreachable at last check per sibling repo notes |

## 8. Immediate next steps / TODO

1. **Resolve the `ARM_CONTROL_URL` port** (8100 vs 8420, §4/§5) with whoever
   owns `a1z-arm-control`, then either fix the default or export the env var
   correctly at launch. Consider adding a live-editable override field to
   `vendor.html` (mirroring what `dimos-arm-pwa` already has for the same
   problem) if this isn't nailed down before the demo.
2. Once `a1z-arm-control`'s `/pick_drink` is actually up, do a **live
   end-to-end order test** (drink pick → nav → confirm → return) with a human
   supervising and the physical e-stop reachable — this session only
   verified `_arm_pick()` statically against an unreachable port.
3. Decide whether to add a minimal UI (in `vendor.html` or `index.html`) for
   arming `connection_watchdog.py`/`yolo_watch_daemon.py` before the demo, or
   accept manual `curl` arming by whoever's running the show.
4. Confirm `vendor_config.json`'s `robot_ip` (currently `10.76.11.26`),
   `table`/`station` coordinates, and `nav_blueprint` (`unitree-go2`) are
   still correct for the actual demo venue/robot before relying on real nav.
5. If time allows: a live dry-run of the full vending flow with
   `VENDOR_FAKE_DOG=1 VENDOR_FAKE_ARM=1` (zero external dependencies) to
   shake out any frontend/state-machine bugs before touching real hardware.

## 9. Key files

- `backend/main.py` — FastAPI app, all HTTP/SSE routes, including this
  session's `/api/watchdog/*` and `/api/yolo-watch/*` endpoints and the
  `/vendor` static-file route.
- `backend/dimos_cli.py` — subprocess wrapper around the `dimos` CLI and
  manager classes for every sidecar daemon (camera, lidar, arm-camera, diag
  collector, and this session's watchdog + YOLO-watch managers).
- `backend/vendor.py` — the vending delivery state machine; `_arm_pick()` is
  this session's main logic change (see §4).
- `backend/nav_leg.py` — subprocess the state machine shells out to for one
  navigation leg (publishes `PointStamped` to `/clicked_point`, polls
  odometry for arrival).
- `backend/connection_watchdog.py` — WebRTC silent-death detector +
  reconnect-only recovery sidecar (see §5, §6 for its safety scope).
- `backend/yolo_watch_daemon.py` — camera-based collision-corridor safety
  trigger (built after a real incident where the Go2 was pushed into a table
  with zero stall detection).
- `backend/vendor_config.json` — drink list, table/station coordinates,
  robot IP, nav topic/timing config for the vending flow. Re-read on every
  order — edit and it takes effect on the next order, no restart needed.
- `frontend/index.html` / `frontend/app.js` / `frontend/style.css` — the
  main dashboard PWA. **`app.js` is untouched this session and must stay
  that way** unless explicitly asked to change dashboard behavior.
- `frontend/vendor.html` / `frontend/vendor.js` — this session's new vending
  operator UI, wired to `/api/vendor/*`.
- `README.md` — long-form documentation of the dashboard's every control and
  the real mechanism behind it (teleop daemon, spy-based liveness, etc.).
- `start.sh` / `run.sh` / `setup.sh` — host setup and launch scripts. Not
  modified this session; watchdog/YOLO-watch stay opt-in via REST, not
  auto-launched by `start.sh`.
- `.gitignore` — excludes `venv/`, `__pycache__/`, `*.pyc`, `.DS_Store`.

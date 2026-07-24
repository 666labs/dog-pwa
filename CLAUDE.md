# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A PWA control panel for a Unitree Go2 robot running **dimOS** (dimensionalOS). A thin FastAPI backend shells out to the `dimos` CLI as subprocesses and serves a vanilla-JS frontend over it. The deployment target is a **Linux** devbox on the same LAN as the robot (the code uses `/proc`, `ip`, `sysctl`, and Linux-only LCM multicast setup — it will not fully run on macOS).

## Commands

```bash
# One-time setup (plain venv — NOT the dimOS conda env)
/usr/bin/python3 -m venv venv
./venv/bin/pip install fastapi uvicorn python-multipart psutil

./start.sh    # full launch: LCM host prep (sudo as needed) + server + iPad URL banner
./run.sh      # server only, no host prep (equivalent to:
              #   ./venv/bin/python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8090)
./setup.sh    # idempotent LCM host prep only (loopback multicast, 224.0.0.0/4 route, 64MB rmem)
```

There are no tests, no linter, and no frontend build step (plain HTML/CSS/JS, no framework, no npm).

Key environment variables (all with defaults in `backend/dimos_cli.py`): `DIMOS_BIN` (path to the `dimos` binary), `DIMOS_PY` (the dimOS conda env's python, used to spawn daemons), `DIMOS_CAMERA_PORT`/`DIMOS_LIDAR_PORT` (daemon HTTP ports 8770/8771).

## Architecture

**Two strictly separated Python worlds.** The panel's venv never imports dimOS modules. Everything dimOS-related happens either by shelling out to the `dimos` binary (one-shot commands, parsed from stdout only — stderr carries a harmless warning) or by spawning the daemon scripts under the dimOS **conda** interpreter (`DIMOS_PY`). Keep this boundary: any code that needs `dimos.*` imports belongs in a `*_daemon.py` file, not in `main.py`/`dimos_cli.py`.

**Layers:**

- `backend/main.py` — FastAPI routes only; thin validation then delegates to `dimos_cli`. Also serves `frontend/` as static files from the same port (8090), mounted last so `/api/*` wins.
- `backend/dimos_cli.py` — the core. Subprocess wrapper around the `dimos` CLI, run-lifecycle tracking, and the manager classes (`_TeleopDaemon`, `_SportDaemon`, `_CameraDaemon`, `_LidarDaemon`) that own the daemon subprocesses.
- `backend/*_daemon.py` — long-lived processes under the conda python that pay dimOS's ~1.4–2s import/connect cost once:
  - `teleop_daemon.py` — warm Twist publisher for `/cmd_vel`; line-delimited JSON over stdin/stdout. Makes teleop ~0.3ms per send vs ~2s for a fresh `dimos topic send`.
  - `sport_daemon.py` — warm RPC client for GO2Connection `@rpc` methods (gestures, standup, battery). Hard allowlist of methods at BOTH the HTTP endpoint (`_SPORT_METHODS` in main.py) and inside the daemon (`_ALLOWED_METHODS`) — never expose arbitrary RPC/topic dispatch to the web.
  - `camera_daemon.py` / `lidar_daemon.py` — serve large binary payloads (MJPEG on 8770, raw point-cloud + pose on 8771) over their own tiny stdlib HTTP servers bound 0.0.0.0; FastAPI only probes their `/health` and hands the browser the port.
- `frontend/` — vanilla JS PWA. Two views in one page: the connect wizard (Wi-Fi provisioning → find IP by MAC → live-data check) and the dashboard (teleop, telemetry, launcher, log, camera, 3D lidar map).

**Daemon lifecycle rule:** all four daemons are bound to one robot connection. `run_blueprint()`, `stop()`, and `restart()` each kill all daemons first (`teleop_kill(); sport_kill(); camera_kill(); lidar_kill()`) so a daemon never outlives or publishes into a dead session. Preserve this ordering when touching lifecycle code.

**Self-tracked run state, not `dimos status/stop/restart`.** dimOS's no-arg status/stop/restart verbs target the most-recently-launched run *machine-wide*, and another panel (the A1Z arm panel, port 8091) shares this machine's dimos registry. This panel therefore tracks the PID it launched itself in `/tmp/dimos-pwa-tracked-run.json` (with PID-reuse guard via `/proc/<pid>/cmdline`) and signals that PID directly. Never route lifecycle actions through the global CLI verbs; `global_status()` exists only for debugging.

## Hard-won constraints (do not regress)

- **Redeploy-safe networking:** the frontend calls the API via *relative* paths, and builds camera/lidar/quick-link URLs from `window.location.hostname`. Never bake a hostname into frontend or API responses.
- **Proxy stripping:** `_clean_env()` removes `http_proxy`/`https_proxy` etc. from every dimos subprocess. The robot's WebRTC signaling uses bare `requests` calls that a local proxy breaks (502s against LAN IPs), and `no_proxy` is not a robust fix. All subprocess spawns must use it.
- **`dimos` global options come BEFORE the subcommand:** `dimos --robot-ip <IP> --transport zenoh run <blueprint>`. Never pass `--help` to `dimos run` (it launches for real).
- **`dimos topic echo` is broken in this build** — it never decodes messages. All liveness/rate signals go through `dimos spy` snapshots (`_spy_snapshot`, run in a real pty because spy is a Textual TUI), serialized by `_SPY_LOCK` because concurrent spy processes produce inconsistent reads. Check topics as a *set* (`any_topic_alive`): the Go2's link is genuinely intermittent per-topic, so a single quiet topic is not "disconnected".
- **The lidar topic is `'lidar'`, not `/pointcloud`** (that topic is registered but dead). Pose comes from `'odom'` via the lidar daemon (it piggybacks on the same connection rather than spawning a fifth daemon).
- **Teleop safety:** there is no obstacle avoidance — the human is the only safety. STOP (`/api/teleop/stop`) must never be silently lost: it falls back to the slow one-shot CLI send if the warm daemon fails. Blueprints with "go2" in the name get `go2connection.velocity_api=true` appended so a single Twist sustains motion (the default joystick-emulation API needs 20–50Hz refresh the CLI path can't sustain).
- **Single WebRTC client:** the robot accepts one connection; `/api/run` returns 409 when a run is active and only replaces it on an explicit `force=true` (UI confirms first, never silently).
- **Sport command results are honest:** `sport_command()` in dimOS always returns true (bug — it discards the firmware response). `/api/sport-status` exists to read the real firmware `status.code` (e.g. 3203 = firmware doesn't implement that api_id). Don't present a sport command as succeeded from the bool alone.
- Crash tracebacks from `dimos run` print outside dimOS's structured logger, so launch stdout/stderr is captured to `/tmp/dimos-pwa-last-launch.log` — that file, not `dimos log`, is where launch-failure reasons come from (`_last_crash_reason`).

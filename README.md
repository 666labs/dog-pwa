# dimOS Control

A PWA control panel for **dimOS** (dimensionalOS). It's a thin FastAPI app that
shells out to the `dimos` CLI as subprocesses and exposes a graphical UI over it.
It does **not** import dimOS Python modules or touch the dimOS conda env — it runs
in its own throwaway venv.

## Start it (one command)

```bash
cd /home/alex/dev/AdventureX/dimos-pwa
./start.sh
```

`start.sh` does three things:

1. **Prepares the host for dimOS's default LCM transport** (`setup.sh`) — enables
   loopback multicast, adds the `224.0.0.0/4` route, and raises the LCM socket
   buffers. These are root-only runtime settings that **reset on every reboot**,
   so `start.sh` re-checks them each launch and only `sudo`s for whatever is
   missing (so a second run in the same boot won't re-prompt for a password).
2. Starts the control-panel server (frontend + API on port 8090, bound
   `0.0.0.0`).
3. Prints the exact **`http://<LAN-IP>:8090`** URL to open in Safari on the iPad,
   where you can **Share → Add to Home Screen** to install the PWA.

### Why the sudo step exists

dimOS's LCM transport (what the real robot blueprints use by default) needs
loopback multicast + a multicast route + 64 MB socket buffers, all root-only and
all reset by reboot. Without them, launching an LCM blueprint crashes in dimOS's
own system configurator (it tries to `sudo` from a non-interactive subprocess and
dies). `setup.sh` applies them once, up front, in your real terminal. Values and
checks mirror dimOS's own configurator (`net.core.rmem_max` /
`net.core.rmem_default` >= 67108864). If sudo is unavailable/cancelled it prints
the exact manual commands and stops.

### Other entry points

- `./setup.sh` — just the idempotent LCM host prep, no server.
- `./run.sh` — just the server, **no** host prep (use when the host is already
  configured, or for the Zenoh-transport path which needs no root):
  ```bash
  ./venv/bin/python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8090
  ```

### If you can't / don't want to run the sudo step

Pick **Transport: zenoh** in the launcher. Zenoh doesn't use LCM multicast, so it
runs with no host setup at all — a fallback for testing without root. The real
robot blueprints assume LCM, so `start.sh` is the intended path.

## Why it's redeploy-safe

- Frontend + JSON/SSE API are served from **one** uvicorn port (8090).
- The frontend calls the API via **relative paths** (`fetch('/api/status')`) — no
  hostname is baked in. Camera links and quick-links use
  `window.location.hostname`, so they follow whatever machine serves the page.
- Move this whole folder to the ARM box, recreate the venv (`/usr/bin/python3 -m
  venv venv && ./venv/bin/pip install fastapi uvicorn python-multipart psutil`),
  run `./run.sh`, and it works from that box's own IP with **zero** code changes.
- The `dimos` binary path defaults to
  `/home/alex/miniconda3/envs/dimos/bin/dimos`; override with the `DIMOS_BIN`
  env var if it lives elsewhere on the new machine.

## Two views

**Page 1 — Connect Robot (onboarding wizard).** The landing view when nothing is
connected. Three guided steps, each with pending/active/busy/done/error state:

- **A. Wi-Fi provisioning** — SSID + password + robot BLE MAC (or serial / BLE
  name); shells out to `dimos go2tool connect-wifi`.
- **B. Find robot IP** — active LAN discovery via `dimos go2tool discover --lan`
  (parses its `SOURCE NAME IP MAC SERIAL` table), matched by MAC (full or
  suffix), with a manual-IP override. Note: the robot's Wi-Fi MAC differs from
  its BLE MAC (sequential allocation), so step B takes its own MAC field.
- **C. Live connection check** — launches `unitree-go2-basic` against the IP and
  confirms real data flow via a `dimos spy` snapshot over the Go2's telemetry
  topics (`/odom`, `/color_image`, `/camera_info`, `/lidar`) before unlocking the
  dashboard. `unitree-go2-basic` does **not** open port 5555, so there is no
  camera stream to wait on — "live" means any telemetry topic is publishing.

**Page 2 — Dashboard.** Status, teleop, telemetry grid, odometry liveness,
launcher, live log, topic-rate check, process cleanup, quick links.

### Teleop

On-screen D-pad (forward/back, strafe L/R, yaw L/R) + a large always-visible red
**STOP**. Press-and-hold to move, release to stop (buttons re-send every 300 ms
while held and send a zero Twist on release; releasing anywhere on screen also
stops). Under the hood: `dimos topic send /cmd_vel "Twist([lx,ly,lz],[ax,ay,az])"`
— `/cmd_vel` is the Go2's velocity topic and the Twist expression is dimOS's own
documented `topic send` form (message classes come from `dimos.msgs`). **There is
no obstacle avoidance — the human is the only safety.**

**Teleop latency — persistent daemon.** A fresh `dimos topic send` costs ~1.9-2.1 s
(pure Python/dimOS import boot), which meant a 1-2 s press→motion lag. The teleop
hot path instead talks to a long-lived **`backend/teleop_daemon.py`** (run under
the dimOS conda python) that pays the import cost once and keeps the transport
warm — each publish is then **~0.3 ms** (round-trip via the API ~6 ms). Message
construction is byte-identical to `dimos.robot.cli.topic.topic_send` (same
`_build_eval_context()` + `eval("Twist([...],[...])")` + `make_transport`).
The daemon is warmed (and its publisher primed with a zero Twist / STOP) eagerly
when a run is detected, so even the first press is instant; it is killed and
respawned on every `/api/run` / `/api/stop` / `/api/restart` so it never outlives
the robot connection it was bound to. STOP falls back to the one-shot CLI path if
the daemon is ever wedged.

## Features

- **Blueprint launcher** — dropdown from `dimos list` + robot-IP field + Run.
  Enforces the single-WebRTC-client rule: if a run is active it asks to
  stop-and-replace (confirmation dialog, never silent).
- **Status panel** — blueprint / PID / uptime / run ID, polled every ~2.5 s, with
  Stop and Restart.
- **Live log** — SSE tail of `dimos log -f --json`, JSON pretty-printed.
- **Telemetry grid** — one tile per Go2 topic (`/odom`, `/color_image`,
  `/camera_info`, `/lidar`, `/cmd_vel`), each alive/dead + Hz/type/bandwidth,
  from a single shared `dimos spy` snapshot polled every ~8 s. Tiles flap
  green/grey with the robot's genuinely intermittent WebRTC link; dead tiles show
  "no data Xs" staleness. This replaces the old camera panel — `unitree-go2-basic`
  never opens port 5555, and raw image/pose bytes aren't readable via any CLI
  primitive in this dimOS build.
- **Odometry liveness** — a dedicated `/odom` tile fed by the same telemetry poll;
  degrades to "no data Xs" when `/odom` goes quiet (which it does). Raw pose
  numbers are **not** shown — `dimos topic echo` can't decode in this build.
- **Topic-rate check** — free-text topic → a `dimos spy` snapshot reporting
  alive + rate + type for topics outside the core set. **Not** raw message
  content: `dimos topic echo` is broken in this install (never prints a decoded
  message even for topics visibly active in `spy`), so no panel pretends to show
  message bodies.
- **Process cleanup** — lists dimos/rerun/humancli processes; the live run PID and
  the control panel itself are protected from being killed.
- **Shutdown panel server** — self-terminates just this control-panel process
  (not the robot connection), for clearing a stuck/duplicate instance.
- **Quick links** — :5555 web interface and :8444/teleop (self-signed HTTPS —
  you'll click through a cert warning). Note :5555 only exists for blueprints
  that include `RobotWebInterface`; `unitree-go2-basic` does not.

### A note on `dimos spy` and liveness

All "is this topic live" signals go through `dimos_cli.any_topic_alive` /
`topic_alive`, which scrape `dimos spy`'s live rate table (run in a real pty —
it's a Textual TUI). `dimos topic echo` is broken in this dimOS install so it
can't be used to read the bus. Spy access is serialized by a lock so concurrent
panels never spawn competing spy processes (which produce inconsistent reads).

## Ports

- **8090** — this control panel (serves everything).
- 5555 / 8444 — dimOS's own services, only linked/probed, not owned by us.

## Setup from scratch

```bash
cd /home/alex/dev/AdventureX/dimos-pwa
/usr/bin/python3 -m venv venv
./venv/bin/pip install fastapi uvicorn python-multipart psutil
./run.sh
```

## 公网直播模式（Vercel 双模式页）

`demo-site/` 部署在 Vercel 上，默认是浏览器内模拟。要让公网页面显示真实
机器人画面/状态并可急停：

1. 在跑控制面板的机器上开隧道：
   `cloudflared tunnel --url http://localhost:8090`
2. 打开 `https://<vercel域名>/?backend=<隧道URL>` —— 参数会记住
   （localStorage），之后直接开裸域名也走真实后端；`?backend=off` 断开。

摄像头经 8090 代理（`/api/camera/{go2|arm}/stream.mjpg`），隧道只需暴露
一个端口。臂相机 USB 设备号在 `backend/vendor_config.json` 的
`arm_camera_device`（默认 0）。改完共享前端后运行 `demo-site/sync.sh` 同步。

"""dimOS Control — FastAPI backend.

Serves BOTH the static PWA frontend and the JSON/SSE API from a single uvicorn
port. The frontend calls the API via relative paths, so redeploying this exact
server on another machine "just works" from that machine's own IP — nothing here
hardcodes a hostname for the browser to use.

Run:
    ./venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8090
(from the dimos-pwa project root)
"""
import json
import os
import signal
import time
import urllib.request
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import dimos_cli

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(HERE, "..", "frontend"))

# Camera feed served by dimOS's own RobotWebInterface (only when the running
# blueprint includes one). We probe locally from the server; the browser is told
# to use its own window.location.hostname, never a hardcoded host.
CAMERA_PORT = 5555

app = FastAPI(title="dimOS Control")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/blueprints")
def api_blueprints():
    try:
        return {"blueprints": dimos_cli.list_blueprints()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"dimos list failed: {e}")


@app.get("/api/status")
def api_status():
    try:
        return dimos_cli.status()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"dimos status failed: {e}")


@app.post("/api/run")
def api_run(blueprint: str = Form(...),
            robot_ip: Optional[str] = Form(None),
            transport: Optional[str] = Form(None),
            force: bool = Form(False)):
    """Launch a blueprint. Single-WebRTC-client constraint: if something is
    already running, refuse unless `force` is set (the UI asks the user to
    confirm, then re-sends with force=true, which auto-stops the current run
    first)."""
    blueprint = (blueprint or "").strip()
    if not blueprint:
        raise HTTPException(status_code=400, detail="blueprint is required")

    st = dimos_cli.status()
    if st.get("running"):
        if not force:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "already_running",
                    "message": (
                        f"A run is already active "
                        f"({st.get('blueprint')}, PID {st.get('pid')}). "
                        "Stop it first, or confirm to auto-stop and replace it."
                    ),
                    "current": st,
                },
            )
        # Confirmed: stop the current run before launching the new one.
        dimos_cli.stop()
        # Give it a moment to release the single WebRTC client slot / ports.
        time.sleep(2)

    ip = (robot_ip or "").strip() or None
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    result = dimos_cli.run_blueprint(blueprint, ip, tr)
    return result


@app.post("/api/run-with-retry")
def api_run_with_retry(blueprint: str = Form(...),
                       robot_ip: Optional[str] = Form(None),
                       transport: Optional[str] = Form(None),
                       force: bool = Form(False),
                       max_attempts: int = Form(3)):
    """Like /api/run, but for the connect wizard's launch specifically: retries
    with a short backoff if the process dies within a few seconds of starting.
    Confirmed live tonight that the Go2's WebRTC handshake can fail under
    venue-wifi congestion with a timeout-shaped error even though the robot
    itself is fine — a brief retry often succeeds where the first attempt
    didn't. This call blocks until success or all attempts are exhausted
    (up to ~max_attempts * 9s), so the frontend should show retry progress
    rather than treat it as instant."""
    blueprint = (blueprint or "").strip()
    if not blueprint:
        raise HTTPException(status_code=400, detail="blueprint is required")

    st = dimos_cli.status()
    if st.get("running") and not force:
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_running",
                "message": (
                    f"A run is already active "
                    f"({st.get('blueprint')}, PID {st.get('pid')}). "
                    "Stop it first, or confirm to auto-stop and replace it."
                ),
                "current": st,
            },
        )

    ip = (robot_ip or "").strip() or None
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    attempts = max(1, min(int(max_attempts), 6))  # sane bounds — never unbounded
    return dimos_cli.run_blueprint_with_retry(blueprint, ip, tr, max_attempts=attempts)


@app.post("/api/stop")
def api_stop():
    return dimos_cli.stop()


@app.post("/api/restart")
def api_restart():
    return dimos_cli.restart()


def _delayed_self_terminate():
    # Give the HTTP response time to actually flush to the client before this
    # process dies, then a plain SIGTERM (uvicorn's own graceful-shutdown
    # handler) rather than SIGKILL — same as Ctrl-C in a terminal.
    time.sleep(0.4)
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/api/server/shutdown")
def api_server_shutdown(background_tasks: BackgroundTasks):
    """Tear down THIS control-panel server process itself — NOT the dimOS
    robot blueprint (use /api/stop for that; this leaves the robot connection
    alone, they're independent processes). For clearing a stuck/duplicate
    instance without needing to hunt down a PID in a terminal — re-run
    ./start.sh (or sudo ./start.sh) afterward to bring the panel back."""
    background_tasks.add_task(_delayed_self_terminate)
    return {"ok": True, "message": "Server shutting down. Re-run start.sh to bring it back."}


def _sse(gen):
    """Wrap a line generator as a text/event-stream response."""
    def event_stream():
        # Prime the connection so proxies flush.
        yield ": connected\n\n"
        try:
            for line in gen:
                # SSE: escape newlines by emitting each as its own data field.
                payload = line.replace("\r", "")
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps(str(e))}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/log")
def api_log():
    """Follow the current run's log as SSE. Each event's data is one JSONL line
    from `dimos log -f --json` (frontend JSON-parses defensively)."""
    if not dimos_cli.status().get("running"):
        raise HTTPException(status_code=409, detail="No running instance to log")
    return _sse(dimos_cli.stream_log(backfill=100))


@app.get("/api/topic")
def api_topic(name: str, transport: Optional[str] = None):
    """Stream a pub/sub topic via `dimos topic echo <name>` as SSE. Line format
    is not assumed; frontend parses defensively and falls back to raw text.

    `transport` (optional, lcm|zenoh) is a global option placed before the
    subcommand. To see messages it must match the transport the current run was
    launched with (e.g. a zenoh-launched run needs transport=zenoh here)."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="topic name is required")
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    pre = ["--transport", tr] if tr else []
    return _sse(dimos_cli.stream_command([*pre, "topic", "echo", name]))


@app.get("/api/processes")
def api_processes():
    """List processes whose command line mentions dimos/rerun/humancli, for the
    cleanup panel. Excludes the current legitimate run PID (and our own PID) from
    the `killable` flag so the UI can't shoot itself in the foot."""
    if psutil is None:
        raise HTTPException(status_code=500, detail="psutil not installed")

    current = dimos_cli.current_pid()
    our_pid = os.getpid()
    keywords = ("dimos", "rerun", "humancli")
    procs = []
    now = time.time()
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmdline = p.info.get("cmdline") or []
            joined = " ".join(cmdline)
            low = joined.lower()
            if not any(k in low for k in keywords):
                continue
            # Skip this control panel itself (uvicorn serving this file).
            if "backend.main" in low or "dimos-pwa" in low:
                continue
            pid = p.info["pid"]
            created = p.info.get("create_time") or now
            uptime_s = max(0, int(now - created))
            is_current = current is not None and pid == current
            procs.append({
                "pid": pid,
                "name": p.info.get("name") or "?",
                "cmdline": joined[:400],
                "uptime_s": uptime_s,
                "is_current_run": is_current,
                # killable = not the live run and not ourselves
                "killable": (not is_current) and pid != our_pid,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    procs.sort(key=lambda x: x["pid"])
    return {"processes": procs, "current_run_pid": current}


@app.post("/api/kill")
def api_kill(pid: int = Form(...)):
    """Kill a process by PID, but never the current legitimate dimos run."""
    if psutil is None:
        raise HTTPException(status_code=500, detail="psutil not installed")
    current = dimos_cli.current_pid()
    if current is not None and pid == current:
        raise HTTPException(
            status_code=400,
            detail="Refusing to kill the current dimos run PID. Use Stop instead.",
        )
    if pid == os.getpid():
        raise HTTPException(status_code=400, detail="Refusing to kill the control panel.")
    try:
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=3)
        except psutil.TimeoutExpired:
            p.kill()
        return {"ok": True, "pid": pid}
    except psutil.NoSuchProcess:
        return {"ok": True, "pid": pid, "note": "already gone"}
    except psutil.AccessDenied as e:
        raise HTTPException(status_code=403, detail=f"Access denied killing {pid}: {e}")


@app.get("/api/camera")
def api_camera():
    """Best-effort probe of dimOS RobotWebInterface's stream index at
    localhost:5555/streams. Returns available stream keys, or available=False if
    nothing is serving. The browser builds the actual <img> src from its OWN
    hostname + CAMERA_PORT (returned here) so it works off-box too."""
    url = f"http://localhost:{CAMERA_PORT}/streams"
    # Bypass any HTTP proxy: the dimOS camera server is strictly local, and a
    # configured proxy would otherwise turn a clean connection-refused into a
    # misleading 502.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001  (connection refused etc. -> not available)
        return {"available": False, "port": CAMERA_PORT, "reason": str(e)}

    keys = []
    parsed = None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        # Could be {"streams": [...]} or {key: ...}
        if "streams" in parsed and isinstance(parsed["streams"], list):
            keys = [str(k) for k in parsed["streams"]]
        else:
            keys = [str(k) for k in parsed.keys()]
    elif isinstance(parsed, list):
        keys = [str(k) for k in parsed]

    return {
        "available": True,
        "port": CAMERA_PORT,
        "keys": keys,
        "raw": body[:500],
    }


# dimOS's self-hosted Rerun web viewer (served when the Go2 is launched with
# --rerun-open web, which run_blueprint now does): a bundled WASM app on 9878
# that connects to the gRPC data server on 9877. This is the live camera path
# (unitree-go2-basic never opens the :5555 RobotWebInterface).
RERUN_WEB_PORT = 9878
RERUN_GRPC_PORT = 9877


@app.get("/api/rerun")
def api_rerun():
    """Probe whether dimOS's Rerun web viewer is up locally (served on 9878 when
    the Go2 was launched with --rerun-open web). Returns the ports; the browser
    builds the actual iframe URL from its OWN window.location.hostname so it works
    identically when this panel runs on another machine (redeploy-safe).

    The viewer is a self-hosted, SDK-bundled WASM app (verified: it serves the
    'Rerun SDK-bundled web viewer' HTML with no X-Frame-Options, so it embeds) —
    NOT the public app.rerun.io, so it works without internet."""
    url = f"http://localhost:{RERUN_WEB_PORT}/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as resp:
            ok = resp.status == 200
            head = resp.read(400).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001  (connection refused -> not serving)
        return {"available": False, "web_port": RERUN_WEB_PORT,
                "grpc_port": RERUN_GRPC_PORT, "reason": str(e)}
    return {
        "available": ok,
        "web_port": RERUN_WEB_PORT,
        "grpc_port": RERUN_GRPC_PORT,
        # Confirm it's really the bundled viewer, not a stray server on the port.
        "is_rerun": "Rerun" in head,
    }


@app.get("/api/camera-stream")
def api_camera_stream():
    """The dashboard Camera panel's data source: a plain, low-latency MJPEG feed
    of the robot's live /color_image, bypassing Rerun entirely.

    Lazily spawns the camera daemon (backend/camera_daemon.py, managed by
    dimos_cli._CameraDaemon) for the current run's transport — so the feed comes
    up against an already-running robot without a restart — then probes the
    daemon's own /health. The browser builds the actual <img> src from its OWN
    window.location.hostname + the returned port (redeploy/off-box safe); the
    daemon binds 0.0.0.0 so a LAN device other than this server can reach it.

    `available` = the daemon's HTTP server is up (the <img> can load it; the
    daemon serves a 'waiting for camera' placeholder until real frames arrive).
    `has_frame`/`fresh`/`frames` report whether the robot's video track is
    actually publishing /color_image right now (this connection is intermittent;
    the WebRTC video track can be silent even while the connection is otherwise
    healthy)."""
    port = dimos_cli.CAMERA_STREAM_PORT
    st = dimos_cli.status()
    if not st.get("running"):
        return {"available": False, "port": port,
                "reason": "no robot run is active"}

    ens = dimos_cli.camera_ensure(st.get("transport"))
    if not ens.get("ok"):
        return {"available": False, "port": port,
                "reason": f"camera daemon failed to start: {ens.get('error')}"}

    # Probe the daemon's own liveness endpoint (strictly local -> bypass proxy).
    url = f"http://localhost:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "port": port,
                "reason": f"camera daemon health probe failed: {e}"}

    return {
        "available": True,
        "port": port,
        "path": "/stream.mjpg",
        "snapshot_path": "/snapshot.jpg",
        "topic": health.get("topic"),
        "transport": health.get("transport"),
        "has_frame": bool(health.get("has_real_frame")),
        "fresh": bool(health.get("fresh")),
        "frames": health.get("frames"),
        "age_s": health.get("age_s"),
    }


@app.get("/api/lidar-stream")
def api_lidar_stream():
    """The dashboard 3D Map panel's data source: the robot's live lidar point
    cloud as a raw binary buffer, for a custom WebGL renderer (replacing the
    rejected Rerun web viewer).

    Lazily spawns the lidar daemon (backend/lidar_daemon.py, managed by
    dimos_cli._LidarDaemon) for the current run's transport — so it comes up
    against an already-running robot without a restart — then probes the daemon's
    own /health. The browser fetches /points.bin directly from the returned port,
    building the URL from its OWN window.location.hostname (redeploy/off-box
    safe); the daemon binds 0.0.0.0 so a LAN device other than this server can
    reach it.

    `available` = the daemon's HTTP server is up. `has_data`/`fresh`/`points`
    report whether the lidar is actually publishing right now (bbox gives the
    cloud extent so the client can auto-frame the camera)."""
    port = dimos_cli.LIDAR_STREAM_PORT
    st = dimos_cli.status()
    if not st.get("running"):
        return {"available": False, "port": port,
                "reason": "no robot run is active"}

    ens = dimos_cli.lidar_ensure(st.get("transport"))
    if not ens.get("ok"):
        return {"available": False, "port": port,
                "reason": f"lidar daemon failed to start: {ens.get('error')}"}

    # Probe the daemon's own liveness endpoint (strictly local -> bypass proxy).
    url = f"http://localhost:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "port": port,
                "reason": f"lidar daemon health probe failed: {e}"}

    return {
        "available": True,
        "port": port,
        "path": "/points.bin",
        "topic": health.get("topic"),
        "transport": health.get("transport"),
        "has_data": bool(health.get("has_data")),
        "fresh": bool(health.get("fresh")),
        "points": health.get("points"),
        "age_s": health.get("age_s"),
        "bbox": health.get("bbox"),
    }


@app.get("/api/pose")
def api_pose():
    """Real robot pose (x/y/yaw telemetry), replacing the old odom-liveness-only
    tile. Piggybacks on the lidar daemon (backend/lidar_daemon.py) — it already
    pays the one-time Dimos.connect() cost, so pose polling reuses that same
    connection/process instead of a dedicated daemon for a few floats. Small
    payload, so unlike /points.bin this proxies the daemon's /pose.json directly
    rather than handing the browser a port to fetch from itself."""
    port = dimos_cli.LIDAR_STREAM_PORT
    st = dimos_cli.status()
    if not st.get("running"):
        return {"available": False, "reason": "no robot run is active"}

    ens = dimos_cli.lidar_ensure(st.get("transport"))
    if not ens.get("ok"):
        return {"available": False,
                "reason": f"pose daemon failed to start: {ens.get('error')}"}

    url = f"http://localhost:{port}/pose.json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.5) as resp:
            pose = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"pose probe failed: {e}"}

    return {"available": True, **pose}


# --------------------------------------------------------------------------- #
# Onboarding wizard endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/provision-wifi")
def api_provision_wifi(ssid: str = Form(...),
                       password: str = Form(...),
                       mac: Optional[str] = Form(None),
                       serial: Optional[str] = Form(None),
                       name: Optional[str] = Form(None),
                       country: Optional[str] = Form(None)):
    """Step A: provision the robot's Wi-Fi over BLE."""
    ssid = (ssid or "").strip()
    if not ssid:
        raise HTTPException(status_code=400, detail="ssid is required")
    mac = (mac or "").strip() or None
    serial = (serial or "").strip() or None
    name = (name or "").strip() or None
    if not (mac or serial or name):
        raise HTTPException(
            status_code=400,
            detail="Provide a robot identifier: MAC, serial, or BLE name.")
    return dimos_cli.provision_wifi(ssid, password, mac=mac, serial=serial,
                                    name=name, country=(country or "").strip() or None)


@app.get("/api/discover")
def api_discover(timeout: float = 8.0, lan_only: bool = True):
    """List robots discovered on the LAN (and/or BLE)."""
    try:
        robots = dimos_cli.discover(timeout=timeout, lan_only=lan_only)
        return {"robots": robots}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"discover failed: {e}")


@app.get("/api/find-ip")
def api_find_ip(mac: str, timeout: float = 8.0):
    """Step B: resolve the robot's IP for a given MAC (full or suffix)."""
    mac = (mac or "").strip()
    if not mac:
        raise HTTPException(status_code=400, detail="mac is required")
    return dimos_cli.find_ip_for_mac(mac, timeout=timeout)



# Broad set of unitree-go2-basic's own telemetry topics — checked together in
# ONE spy snapshot. This robot's connection has shown genuinely intermittent
# per-topic activity live tonight (odom/color_image/lidar all active one
# moment, only the ~1Hz camera_info heartbeat the next) — checking a single
# topic in isolation produces false "not connected" reads exactly when the
# connection is fine but that particular topic happens to be quiet. "live"
# means ANY of these are producing right now, which is a fairer read of
# "is this connection actually up" for this robot.
_GO2_TELEMETRY_TOPICS = ["/odom", "/color_image", "/camera_info", "/lidar"]


@app.get("/api/connection-check")
def api_connection_check(topic: str = "/odom", seconds: float = 6.0,
                         transport: Optional[str] = None):
    """Step C helper: is data actually flowing? Checks the camera stream index
    (port 5555 — only present on blueprints that include RobotWebInterface,
    which unitree-go2-basic does NOT) and a broad set of the Go2's own
    telemetry topics via a single `dimos spy` snapshot (see
    `dimos_cli.any_topic_alive` for why one topic in isolation is unreliable
    on this connection)."""
    cam = api_camera()
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")

    req_topic = (topic or "/odom").strip()
    topics_to_check = list(dict.fromkeys([req_topic, *_GO2_TELEMETRY_TOPICS]))  # dedupe, keep order
    result = dimos_cli.any_topic_alive(topics_to_check, seconds=seconds, transport=tr)

    return {
        "camera": cam,
        "topic_check": {"topic": req_topic, **result["topics"].get(req_topic, {"alive": False, "sample": None})},
        "all_topics": result["topics"],
        "live": bool(cam.get("available")) or bool(result["alive"]),
    }


# --------------------------------------------------------------------------- #
# Teleop endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/teleop")
def api_teleop(lx: float = Form(0.0), ly: float = Form(0.0), lz: float = Form(0.0),
               ax: float = Form(0.0), ay: float = Form(0.0), az: float = Form(0.0),
               topic: str = Form("/cmd_vel"),
               transport: Optional[str] = Form(None)):
    """Send one Twist velocity command via the WARM DAEMON (sub-ms once warm,
    vs ~2.1 s for a fresh `dimos topic send`). Default /cmd_vel is the Go2's
    velocity topic. The frontend sends these on press-and-repeat and a zero
    Twist on release; there is NO obstacle avoidance — the human is the safety."""
    topic = (topic or "/cmd_vel").strip() or "/cmd_vel"
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    return dimos_cli.teleop_send(lx, ly, lz, ax, ay, az, topic=topic, transport=tr)


@app.post("/api/teleop/stop")
def api_teleop_stop(topic: str = Form("/cmd_vel"),
                    transport: Optional[str] = Form(None)):
    """Emergency stop: send a zero Twist. Uses the warm daemon; if that path is
    wedged/unavailable, falls back to the reliable one-shot CLI send so STOP is
    never silently lost."""
    topic = (topic or "/cmd_vel").strip() or "/cmd_vel"
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    res = dimos_cli.teleop_send(0, 0, 0, 0, 0, 0, topic=topic, transport=tr)
    if not res.get("ok"):
        # Safety fallback: the slow-but-reliable CLI path.
        fb = dimos_cli.send_twist(0, 0, 0, 0, 0, 0, topic=topic, transport=tr)
        fb["daemon_error"] = res.get("error")
        fb["fallback"] = "cli"
        return fb
    return res


@app.post("/api/teleop/warm")
def api_teleop_warm(topic: str = Form("/cmd_vel"),
                    transport: Optional[str] = Form(None)):
    """Eagerly spawn+warm the teleop daemon and prime the topic's publisher so
    the first real press is sub-ms. Priming sends one zero Twist (a STOP — safe,
    no motion). Idempotent — spawn is a no-op if already warm."""
    topic = (topic or "/cmd_vel").strip() or "/cmd_vel"
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    res = dimos_cli.teleop_warm(topic=topic, transport=tr)
    res["daemon"] = dimos_cli.teleop_status()
    return res


# --------------------------------------------------------------------------- #
# Sport / gesture RPC endpoints (GO2Connection @rpc methods over the bus)
# --------------------------------------------------------------------------- #
# Allowlisted @rpc methods only — never invoke arbitrary RPC names from the web.
# Value = number of numeric args the method takes (validated below).
_SPORT_METHODS = {
    "sport_command": 1,   # api_id (gesture / emote / trick — the whole table)
    "standup": 0,
    "liedown": 0,
    "balance_stand": 0,
    "stop_movement": 0,
    "battery_soc": 0,
}


@app.post("/api/sport")
def api_sport(method: str = Form(...),
              arg: Optional[int] = Form(None),
              transport: Optional[str] = Form(None)):
    """Invoke an allowlisted GO2Connection @rpc method via the warm RPC daemon.

    Returns the daemon envelope: {ok, result, ms} on success (`result` is the
    method's own return — e.g. sport_command's accepted-bool), or {ok:false,
    error} if the RPC timed out or the robot raised (notably 'Data channel is not
    open' when the WebRTC command link is flapping). The UI surfaces this
    directly — a command is NOT assumed to succeed."""
    method = (method or "").strip()
    if method not in _SPORT_METHODS:
        raise HTTPException(status_code=400,
                            detail=f"method must be one of {sorted(_SPORT_METHODS)}")
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    needs_arg = _SPORT_METHODS[method]
    args: list = []
    if needs_arg:
        if arg is None:
            raise HTTPException(status_code=400, detail=f"{method} requires 'arg'")
        args = [int(arg)]
    # sport_command animations can genuinely run longer than the daemon's
    # 12s default RPC timeout (confirmed: Dance1 specifically runs past it —
    # the robot was still mid-animation when we gave up and reported a
    # timeout, not a real failure). Give animations real room; other @rpc
    # methods here (standup/liedown/balance_stand/stop_movement/battery_soc)
    # are fast utility calls and keep the daemon's normal default.
    call_timeout = 25.0 if method == "sport_command" else None
    return dimos_cli.sport_call(method, args, transport=tr, timeout=call_timeout)


@app.post("/api/sport-status")
def api_sport_status(arg: int = Form(...), transport: Optional[str] = Form(None)):
    """Same physical action as POST /api/sport with method=sport_command —
    sends the exact same command to the robot. The only difference is this
    reads back the robot's real firmware status.code instead of the
    always-true bool sport_command() itself returns (a dimOS bug: it discards
    the real response). Use this for commands where you need to know WHY
    nothing visible happened — e.g. code 3203 means the firmware genuinely
    doesn't implement that id (not fixable client-side), vs some other code
    pointing at a real, fixable precondition."""
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    return dimos_cli.sport_command_status(int(arg), transport=tr)


@app.post("/api/sport/warm")
def api_sport_warm(transport: Optional[str] = Form(None)):
    """Eagerly spawn+warm the sport RPC client so the first gesture is instant.
    Sends nothing to the robot."""
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    res = dimos_cli.sport_warm(transport=tr)
    res["daemon"] = dimos_cli.sport_status()
    return res


@app.get("/api/battery")
def api_battery(transport: Optional[str] = None):
    """Robot battery SOC (0-100) via GO2Connection/battery_soc @rpc. Reads cached
    telemetry, so it works even while the command data channel is flapping."""
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    return dimos_cli.sport_call("battery_soc", [], transport=tr, timeout=5)


# --------------------------------------------------------------------------- #
# Live telemetry (spy-backed) — the honest replacement for the dead camera panel
# --------------------------------------------------------------------------- #
# The Go2's own telemetry topics plus the velocity command topic. `spy` reports
# rate/type/bandwidth across all transports; raw message *content* is NOT
# obtainable (this dimOS build's `topic echo` never decodes), so these panels
# show alive/rate/type rather than pretending to show frames or pose numbers.
_SENSOR_TOPICS = ["/odom", "/color_image", "/camera_info", "/lidar", "/cmd_vel"]


@app.get("/api/telemetry")
def api_telemetry(seconds: float = 5.0, transport: Optional[str] = None):
    """One shared `dimos spy` snapshot over the Go2's core telemetry + cmd_vel
    topics. Returns {topics: {t: {alive, sample}}, alive}. Each call blocks for
    `seconds`; poll it on a relaxed interval, not tightly. This connection is
    genuinely intermittent — expect per-topic alive flags to flap between polls
    with no change on our side."""
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    return dimos_cli.any_topic_alive(_SENSOR_TOPICS, seconds=seconds, transport=tr)


@app.get("/api/topic-rate")
def api_topic_rate(name: str, seconds: float = 5.0, transport: Optional[str] = None):
    """Spy-backed liveness/rate/type for a single free-text topic (for topics
    outside the core set). Returns {topic, alive, count, sample}. Raw message
    content is not available in this dimOS build — this reports rate/type only."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="topic name is required")
    tr = (transport or "").strip().lower() or None
    if tr not in (None, "lcm", "zenoh"):
        raise HTTPException(status_code=400, detail="transport must be lcm or zenoh")
    return dimos_cli.topic_alive(name, seconds=seconds, transport=tr)


@app.get("/api/health")
def api_health():
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Vendor demo (drink ordering → arm stub → Go2 nav delivery)
# --------------------------------------------------------------------------- #
import vendor  # noqa: E402  (after app setup, before the catch-all static mount)

app.include_router(vendor.router)


@app.get("/vendor")
def vendor_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "vendor.html"))


# --------------------------------------------------------------------------- #
# Static frontend (mounted last so /api/* wins). "/" serves index.html.
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

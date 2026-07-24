"""Thin subprocess wrapper around the `dimos` CLI binary.

We never import dimOS Python modules. We only shell out to the binary and parse
its text output. The binary lives in the dimOS conda env; we call it by absolute
path so this control-panel venv never needs that env activated.

Note: the dimos binary prints a harmless RequestsDependencyWarning to *stderr*.
stdout is clean, so we parse stdout only.
"""
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

# Serializes access to the `dimos spy` subprocess. Multiple concurrent spy
# instances contend for the same subscriptions and produce inconsistent per-run
# results on this robot's (already intermittent) connection — so the dashboard's
# periodic telemetry poll, a user-triggered topic-rate check, and the wizard's
# connection-check must never run spy at the same time. Each holder blocks for
# its `seconds` budget; callers should keep that short.
_SPY_LOCK = threading.Lock()

DIMOS_BIN = os.environ.get(
    "DIMOS_BIN", "/home/alex/miniconda3/envs/dimos/bin/dimos"
)

# Raw stdout+stderr of the most recent `dimos run` launch — see run_blueprint()
# for why this exists (crash tracebacks print outside dimOS's structured
# logger, so `dimos log` can't see them). Fixed path, overwritten per launch:
# only one run is ever active at a time (single-WebRTC-client constraint).
_LAST_LAUNCH_LOG = "/tmp/dimos-pwa-last-launch.log"

# --------------------------------------------------------------------------- #
# Self-tracked run state
#
# `dimos status` / `dimos stop` / `dimos restart` (no args) all resolve, inside
# dimOS's own CLI, to get_most_recent(alive_only=True) from
# dimos.core.run_registry -- i.e. THE MOST RECENTLY LAUNCHED INSTANCE ON THE
# WHOLE MACHINE, not "the one this panel launched". This machine also runs the
# A1Z arm panel (port 8091) against the same dimos binary/registry. Blindly
# trusting global status/stop/restart here means: if the arm panel launches
# AFTER this Go2 panel, then this panel's Stop/Restart button would send
# `dimos stop`/`dimos restart` and hit the ARM's coordinator instead of the
# Go2 -- an unrelated, wrong target. dimOS does NOT prevent multiple
# simultaneous `dimos run` processes; only its status/stop/restart CLI verbs
# are single-target, so we must not route our lifecycle decisions through them.
#
# Fix: track the specific PID *this panel* launched (run_blueprint() already
# has it from subprocess.Popen) plus enough to relaunch it identically
# (blueprint / robot_ip / transport), in a small local file, and use that
# directly for status/stop/restart instead of the global "most recent"
# semantics. Same fixed-path-in-/tmp convention as _LAST_LAUNCH_LOG above, and
# the same single-run assumption (one active run at a time). See
# global_status() below for the raw machine-wide answer when you want it.
# --------------------------------------------------------------------------- #
_TRACKED_RUN_FILE = "/tmp/dimos-pwa-tracked-run.json"

# Short timeout for one-shot informational commands.
_QUICK_TIMEOUT = 20


def _save_tracked_run(pid: int, blueprint: str, robot_ip: Optional[str],
                      transport: Optional[str], stdout_log: str) -> None:
    """Persist the PID (and how to relaunch it) of the run THIS panel just
    launched. Overwritten on every launch attempt, so it always names whatever
    we most recently launched ourselves."""
    try:
        with open(_TRACKED_RUN_FILE, "w") as f:
            json.dump({
                "pid": pid,
                "blueprint": blueprint,
                "robot_ip": robot_ip,
                "transport": transport,
                "stdout_log": stdout_log,
                "launched_at": time.time(),
            }, f)
    except OSError:
        pass


def _load_tracked_run() -> Optional[Dict[str, object]]:
    try:
        with open(_TRACKED_RUN_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _clear_tracked_run() -> None:
    try:
        os.unlink(_TRACKED_RUN_FILE)
    except OSError:
        pass


def _pid_matches_blueprint(pid: int, blueprint: str) -> bool:
    """Guard against PID reuse: confirm /proc/<pid>/cmdline still looks like the
    `dimos run <blueprint>` we launched, not an unrelated process that recycled
    the same PID after ours exited."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return "dimos" in cmdline and blueprint in cmdline


def _fmt_uptime(seconds: float) -> str:
    """Human uptime string for the status panel (the self-tracked path has no
    CLI-formatted uptime to borrow, so we compute our own from launched_at)."""
    s = max(0, int(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _clean_env() -> Dict[str, str]:
    """Environment for dimos subprocesses with HTTP(S) proxy vars stripped.

    ROOT CAUSE (diagnosed live 2026-07-23): ~/.bashrc exports
    http_proxy/https_proxy=http://127.0.0.1:7897 (Mihomo/Clash). The
    unitree_webrtc_connect signaling code uses bare `requests.post(...)`
    (no proxies={}, no trust_env=False), so its LAN HTTP calls to the robot
    (http://<robot-ip>:9991/con_notify) were routed through the local proxy,
    which cannot reach venue-LAN private IPs → `502 Bad Gateway` /
    "Failed to receive initial public key response". Verified A/B with curl
    against the live robot: through proxy → 502/timeout; direct → 200 in
    0.4s. NOTE: adding the robot IP to no_proxy is NOT robust — Python's
    requests/urllib does suffix string matching (no CIDR support, unlike
    curl) and the robot's DHCP IP changes — so we strip the vars entirely.
    """
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    return env


def _run(args: List[str], timeout: int = _QUICK_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a one-shot dimos command and capture output. stderr is discarded for
    parsing purposes but returned so callers can surface errors."""
    return subprocess.run(
        [DIMOS_BIN, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


_BLUEPRINT_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def list_blueprints() -> List[str]:
    """Parse `dimos list` into a flat list of blueprint names.

    Current output (verified live) is one kebab-case name per line, no
    "Built-in blueprints:" header, no indentation -- just occasional warning
    noise on stdout (e.g. RequestsDependencyWarning) mixed in. This parser
    previously assumed a header + 2-space-indented entries and only accepted
    indented lines; that format is gone, so requiring indentation silently
    matched zero lines against the current flush-left output (this shipped
    broken -- confirmed live, `/api/blueprints` was returning an empty list).
    Rather than match a specific header format that keeps changing, accept
    any line that's a plausible kebab-case identifier and reject everything
    else (warnings, blank lines, prose) by shape, not by header text.
    """
    proc = _run(["list"])
    blueprints: List[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if _BLUEPRINT_NAME_RE.match(stripped):
            blueprints.append(stripped)
    return blueprints


def global_status() -> Dict[str, object]:
    """Parse `dimos status` AS-IS: whatever dimOS itself considers the most
    recently launched instance, machine-wide (could be the A1Z arm panel's run
    on port 8091, someone else's test, or this panel's -- no way to tell from
    this alone). Kept for transparency/debugging ("what does dimos think is
    going on globally"), but NOT what this panel's own connect/stop/restart
    logic keys off of -- see status()/stop()/restart() below.

    Returns {"running": False} when idle, else
    {"running": True, "run_id":..., "pid":..., "blueprint":..., "uptime":...,
     "log":...} (keys present when the CLI reported them).
    """
    proc = _run(["status"])
    text = proc.stdout
    if "no running dimos instance" in text.lower():
        return {"running": False, "raw": text.strip()}

    result: Dict[str, object] = {"running": False, "raw": text.strip()}
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = label.strip().lower().replace(" ", "_")
        value = value.strip()
        if key and value:
            fields[key] = value

    # Map the CLI's labels to stable JSON keys.
    if fields:
        result["running"] = True
        result["run_id"] = fields.get("run_id")
        result["pid"] = fields.get("pid")
        result["blueprint"] = fields.get("blueprint")
        result["uptime"] = fields.get("uptime")
        result["log"] = fields.get("log")
        result["fields"] = fields
    return result


def status() -> Dict[str, object]:
    """Is the instance THIS panel launched still alive? Self-tracked via the PID
    captured at launch time (see _save_tracked_run), NOT the global
    most-recently-launched-machine-wide answer from `dimos status` -- see the
    _TRACKED_RUN_FILE block above for why that distinction matters now that the
    arm panel shares this machine + dimos registry.

    Returns {"running": False} when idle/untracked, else
    {"running": True, "pid":..., "blueprint":..., "robot_ip":...,
     "transport":..., "uptime_s":..., "uptime":..., "stdout_log":...}.
    (`uptime` is a human string for the status panel; `run_id`/`log` from the
    old CLI-parsed shape are intentionally absent -- self-tracking has no
    equivalent, and the frontend renders missing keys as "—".)
    """
    tracked = _load_tracked_run()
    if not tracked:
        return {"running": False}

    pid = tracked.get("pid")
    blueprint = tracked.get("blueprint")
    if (not isinstance(pid, int)
            or not os.path.exists(f"/proc/{pid}")
            or not _pid_matches_blueprint(pid, str(blueprint))):
        _clear_tracked_run()
        return {"running": False}

    uptime_s = time.time() - float(tracked.get("launched_at", time.time()))
    return {
        "running": True,
        "pid": pid,
        "blueprint": blueprint,
        "robot_ip": tracked.get("robot_ip"),
        "transport": tracked.get("transport"),
        "uptime_s": uptime_s,
        "uptime": _fmt_uptime(uptime_s),
        "stdout_log": tracked.get("stdout_log"),
    }


def current_pid() -> Optional[int]:
    """Return the PID of THIS panel's tracked run, or None."""
    st = status()
    if st.get("running") and st.get("pid"):
        try:
            return int(str(st["pid"]))
        except (ValueError, TypeError):
            return None
    return None


def _stop_tracked_pid() -> Dict[str, object]:
    """SIGTERM (then SIGKILL after a short grace period) the PID this panel
    tracked as its OWN run, and clear the tracked-run file. Does NOT touch the
    teleop/sport daemons -- stop()/restart() handle those in their own flow so
    the daemon-kill order is preserved exactly. Targeting our own PID directly
    is the only way to guarantee we never signal another panel's run."""
    st = status()
    if not st.get("running"):
        _clear_tracked_run()
        return {"ok": True, "returncode": 0,
                "stdout": "Nothing tracked as running by this panel.", "stderr": ""}

    pid = int(str(st["pid"]))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_tracked_run()
        return {"ok": True, "returncode": 0,
                "stdout": f"PID {pid} was already gone.", "stderr": ""}

    deadline = time.time() + 10
    while time.time() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            _clear_tracked_run()
            return {"ok": True, "returncode": 0,
                    "stdout": f"Stopped PID {pid} (SIGTERM).", "stderr": ""}
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_tracked_run()
    return {"ok": True, "returncode": 0,
            "stdout": f"PID {pid} did not exit on SIGTERM; sent SIGKILL.", "stderr": ""}


def stop() -> Dict[str, object]:
    """Stop THIS panel's tracked instance directly by PID (SIGTERM, then SIGKILL
    if needed) -- NOT `dimos stop`, which targets whatever is
    most-recently-launched machine-wide and could be the arm panel's run. Can
    take a couple seconds (SIGTERM->SIGKILL)."""
    # The teleop daemon holds a transport session bound to THIS connection —
    # tear it down alongside the robot connection so it can't linger and
    # silently publish into a dead session. (Kept in exactly this position.)
    teleop_kill()
    sport_kill()
    camera_kill()  # MJPEG daemon holds a subscription to this connection
    lidar_kill()   # lidar daemon holds a Dimos.connect() to this connection
    return _stop_tracked_pid()


def restart() -> Dict[str, object]:
    """Restart THIS panel's tracked instance: stop it by PID (see above), then
    relaunch the SAME blueprint/robot_ip/transport it was launched with. NOT
    `dimos restart`, which like `dimos stop` operates on whatever is
    most-recently-launched machine-wide -- same cross-panel danger stop()
    avoids."""
    # New connection/session after restart — the old daemon's transport would be
    # stale, so drop it; the next teleop call respawns a fresh one. (Kept in
    # exactly this position, before the stop/relaunch flow, as it was.)
    teleop_kill()
    sport_kill()
    camera_kill()  # run_blueprint (called below) respawns it for the new run
    lidar_kill()   # ditto — respawned by run_blueprint for the new connection

    st = status()
    if not st.get("running"):
        return {"ok": False, "returncode": 1, "stdout": "",
                "stderr": "Nothing tracked as running by this panel to restart."}

    blueprint = str(st["blueprint"])
    robot_ip = st.get("robot_ip")
    transport = st.get("transport")
    stop_result = _stop_tracked_pid()
    # Give it a moment to release the single WebRTC client slot / ports before
    # relaunching (same 2s the /api/run replace path uses).
    time.sleep(2)
    run_blueprint(blueprint,
                  str(robot_ip) if robot_ip else None,
                  str(transport) if transport else None)
    return {"ok": True, "returncode": 0,
            "stdout": f"Restarted {blueprint}.", "stderr": stop_result.get("stderr", "")}


# Blueprints that include GO2Connection default to the WIRELESS_CONTROLLER
# joystick-emulation wire API (velocity_api=False), which needs a live,
# continuously-refreshed analog-stick-rate stream (~20-50Hz) to hold a
# direction — the robot's firmware treats a gap as "stick released" almost
# immediately. Our CLI-dispatch teleop can't sustain that (confirmed live:
# ~2.1s per `dimos topic send` call, pure subprocess-boot cost), which
# produced a repeated twitch-then-settle "wobble" instead of real walking.
# Switching to velocity_api=true uses Unitree's actual SPORT_CMD "Move" API
# instead, which `move()`'s own docstring documents as continuous from a
# SINGLE call — confirmed live tonight: one send, real sustained walking,
# no refreshing needed. Applied automatically for any blueprint whose name
# contains "go2".
_GO2_VELOCITY_API_OPTION = "go2connection.velocity_api=true"


def run_blueprint(blueprint: str, robot_ip: Optional[str] = None,
                  transport: Optional[str] = None) -> Dict[str, object]:
    """Launch a blueprint as a long-running detached background process.

    IMPORTANT: global options like `--robot-ip <IP>` and `--transport <t>` MUST
    come *before* `run`:
        dimos --robot-ip 10.76.3.187 run unitree-go2-basic
        dimos --transport zenoh run coordinator-mock
    We NEVER pass `--help` to `dimos run` (it would launch for real).

    `transport` is optional. Left unset, dimOS uses its own default (LCM), which
    requires loopback multicast to be configured on the host. On a host where
    that isn't set up (no passwordless sudo), pass transport="zenoh" to skip the
    LCM system-configurator entirely.

    The process is launched in its own process group and NOT waited on, so the
    request handler returns immediately.
    """
    # Any previously-warmed teleop daemon was bound to the OLD connection's
    # transport session — a new run means a new connection, so kill it now; the
    # next teleop call (or an eager warm) will spawn a fresh one for this run.
    teleop_kill()
    sport_kill()
    camera_kill()  # old MJPEG daemon held a subscription to the old connection
    lidar_kill()   # old lidar daemon held a Dimos.connect() to the old connection

    args = [DIMOS_BIN]
    if robot_ip:
        args += ["--robot-ip", robot_ip]
    if transport:
        args += ["--transport", transport]
    if "go2" in blueprint.lower():
        # Serve dimOS's self-hosted Rerun WEB viewer (a bundled WASM app) so the
        # dashboard can iframe the live camera. Default rerun_open is "native"
        # (tries dimos-viewer/rr.spawn — fails headless on this box, no web
        # served), so we opt into "web" explicitly: gRPC on 9877, web viewer on
        # 9878. `--rerun-open` is a GLOBAL option → before `run`. open_browser is
        # a harmless no-op on this headless host.
        args += ["--rerun-open", "web"]
    args += ["run", blueprint]
    if "go2" in blueprint.lower():
        args += ["--option", _GO2_VELOCITY_API_OPTION]

    # Detach: new session so it survives independently and isn't tied to our
    # request handler. The run's own structured logs go to dimos' own log
    # store (tailed separately via `dimos log`) — but a startup CRASH (e.g.
    # the WebRTC handshake failure) prints as a raw Rich traceback panel
    # straight to stdout/stderr, NOT through dimOS's structured logger, so
    # `dimos log` never sees it. Capture raw stdio to a fixed scratch file
    # (overwritten per launch — only one run is ever active at a time, same
    # single-WebRTC-client assumption as everywhere else) so a crash reason
    # can actually be read back after the fact.
    with open(_LAST_LAUNCH_LOG, "wb") as f:
        proc = subprocess.Popen(
            args,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_clean_env(),  # proxy vars break robot LAN signaling — see _clean_env
        )
    # Record THIS launch as the panel's tracked run immediately — before we know
    # whether it will connect successfully. This keeps "whatever we most
    # recently launched, ourselves" as the target of status()/stop()/restart(),
    # so a crashed attempt's PID is what a later status() finds and self-heals
    # (its /proc entry is gone → status() clears the file), never a stale live
    # PID from an unrelated run. run_blueprint_with_retry() relies on this
    # overwrite-per-attempt behaviour (see there).
    _save_tracked_run(proc.pid, blueprint, robot_ip, transport, _LAST_LAUNCH_LOG)

    # For go2 blueprints, eagerly spawn the plain-MJPEG camera daemon so the
    # dashboard's Camera panel has a stream to point at as soon as /color_image
    # starts producing. Subscribing before the connection is fully up is
    # harmless (LCM/Zenoh pub/sub is connectionless — it simply waits for
    # frames) and non-disruptive to the publisher. Best-effort: a camera-daemon
    # spawn failure must never fail the robot launch itself.
    if "go2" in blueprint.lower():
        try:
            camera_ensure(transport)
        except Exception:  # noqa: BLE001
            pass
        # Eagerly spawn the lidar point-cloud daemon too, for the custom WebGL
        # 3D Map panel. Same best-effort discipline: never fail the robot launch.
        try:
            lidar_ensure(transport)
        except Exception:  # noqa: BLE001
            pass
    return {"launched": True, "launcher_pid": proc.pid, "cmd": " ".join(args)}


_CRASH_SIGNATURES = (
    ("Failed to receive initial public key response", "WebRTC signaling failed — check for a '502 Bad Gateway' line above it: that means an HTTP proxy (http_proxy env var) intercepted the LAN request to the robot (see _clean_env); otherwise wifi/timing"),
    ("LocalSignalingPortError", "robot not reachable on the expected signaling port — check it's powered on and on the right IP/network"),
    ("RuntimeError: Failed to deploy module", "a module failed to start (see full log for which one)"),
)


def _last_crash_reason() -> str:
    """Best-effort: scan the just-launched process's raw stdout/stderr
    (captured to _LAST_LAUNCH_LOG, NOT `dimos log`) for a known failure
    signature. Startup crashes print a Rich traceback panel directly to
    stdio, bypassing dimOS's structured logger entirely — confirmed `dimos
    log --json` never contains it — so this reads the actual captured
    process output instead."""
    try:
        with open(_LAST_LAUNCH_LOG, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return "unknown (no captured launch output)"
    for needle, human in _CRASH_SIGNATURES:
        if needle in text:
            return human
    return "unknown — process exited without a recognized error signature"


def _pid_alive(pid: Optional[int]) -> bool:
    """Lightweight liveness check that doesn't depend on `dimos status`'s own
    instance-registry (see run_blueprint_with_retry for why that matters)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def run_blueprint_with_retry(blueprint: str, robot_ip: Optional[str] = None,
                             transport: Optional[str] = None,
                             max_attempts: int = 3, stabilize_seconds: float = 18.0,
                             backoff_seconds: float = 3.0) -> Dict[str, object]:
    """Launch a blueprint, and if it dies within `stabilize_seconds` (the
    WebRTC-handshake-timeout failure mode confirmed live tonight — a
    timing-sensitive exchange failing under wifi jitter, not a real
    rejection), retry up to `max_attempts` times with a short backoff.

    HISTORY (registration-lag workaround, now folded into self-tracking):
    `dimos status` was discovered to have a real registration lag after a fresh
    launch — a genuinely healthy, successfully-connected process was still
    reported "not running" for up to ~14+ seconds after starting. The old code
    fought this by polling global status() AND cross-checking the launcher PID
    via os.kill(pid, 0), treating "process still alive at end of window" as the
    real success signal. That PID cross-check is now the PRIMARY (and only)
    signal we need: status() itself is self-tracked (a /proc liveness+identity
    check on the PID we launched — see status()/_TRACKED_RUN_FILE), so it no
    longer depends on dimOS's lagging registry at all and the lag simply cannot
    mislead us here anymore. But that also means status() goes true the INSTANT
    the process exists, which is NOT proof the WebRTC handshake survived — so we
    must NOT treat status()==running as early success. The real success
    criterion is temporal: the handshake-timeout death happens a few seconds in,
    so success = "the process we launched stayed alive through the whole
    stabilize window." We poll liveness across the window and only conclude
    success at the end; an early exit is the crash we retry on. This preserves
    the original protective intent (never declare success before the crash
    window elapses; never kill a good-but-still-starting process) with a single,
    now-reliable signal instead of the old dual-signal dance.

    Returns {"ok": bool, "attempts": int, "status": <final self-tracked status>,
    "attempt_log": [{"attempt", "ok", "reason"}]}."""
    attempt_log = []
    for attempt in range(1, max_attempts + 1):
        # Only stop an existing run if one WE launched is still up. status() is
        # self-tracked, so this reflects only this panel's run (never the arm
        # panel's), and a stale/dead PID from a prior failed attempt self-heals
        # to running=False rather than triggering a spurious stop().
        pre = status()
        if pre.get("running"):
            stop()
            time.sleep(1)

        launch = run_blueprint(blueprint, robot_ip, transport)
        launcher_pid = launch.get("launcher_pid")

        # Watch the launched process survive the stabilize window. Only ever
        # conclude failure if the process itself has actually died — a
        # still-alive process is success (killing it prematurely is what caused
        # the duplicate-launch / single-WebRTC-client conflict bug tonight).
        deadline = time.time() + stabilize_seconds
        crashed = False
        while time.time() < deadline:
            if not _pid_alive(launcher_pid):
                crashed = True
                break  # genuinely dead — no point waiting out the rest of the window
            time.sleep(1.5)

        if not crashed and _pid_alive(launcher_pid):
            # Survived the whole window without exiting — a real, healthy
            # connection, not a handshake-timeout death.
            attempt_log.append({"attempt": attempt, "ok": True,
                                "reason": f"process alive through {stabilize_seconds:g}s stabilize window"})
            return {"ok": True, "attempts": attempt, "status": status(), "attempt_log": attempt_log}

        reason = _last_crash_reason()
        attempt_log.append({"attempt": attempt, "ok": False, "reason": reason})
        if attempt < max_attempts:
            time.sleep(backoff_seconds)

    return {"ok": False, "attempts": max_attempts, "status": status(), "attempt_log": attempt_log}


def stream_log(backfill: int = 100):
    """Generator for the log viewer: first replay the last `backfill` lines
    (one-shot `dimos log -n N --json`, which prints and exits), then switch to
    following new lines (`dimos log -f --json`).

    We chain two commands because `-f` and `-n` together do NOT backfill — with
    `-f` present the CLI only tails brand-new lines, so a one-shot `-n` pass is
    needed to give the user immediate context (an idle run emits no new lines).
    """
    if backfill > 0:
        # One-shot: yields the recent lines, process then exits on its own.
        yield from stream_command(["log", "-n", str(backfill), "--json"])
    # Then follow new lines indefinitely.
    yield from stream_command(["log", "-f", "--json"])


def stream_command(args: List[str]):
    """Generator: launch `dimos <args>` and yield stdout lines as they arrive.

    Used for SSE endpoints (log follow, topic echo). The subprocess is launched
    in its own process group; when the generator is closed (client disconnect)
    we terminate the whole group.
    """
    proc = subprocess.Popen(
        [DIMOS_BIN, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,  # line-buffered
        start_new_session=True,
        env=_clean_env(),  # proxy vars break robot LAN signaling — see _clean_env
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        # Tear down the whole process group.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# --------------------------------------------------------------------------- #
# Onboarding wizard: Wi-Fi provisioning + robot discovery
# --------------------------------------------------------------------------- #
import re as _re

_IP_RE = _re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_MAC_RE = _re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")


def provision_wifi(ssid: str, password: str, mac: Optional[str] = None,
                   serial: Optional[str] = None, name: Optional[str] = None,
                   country: Optional[str] = None,
                   timeout: Optional[float] = None,
                   retries: Optional[int] = None) -> Dict[str, object]:
    """Provision a Go2 with Wi-Fi credentials over BLE via
    `dimos go2tool connect-wifi`. Non-interactive when one of mac/serial/name
    plus ssid/password are given. This can take a while (BLE scan + connect +
    retries), so we allow a generous timeout."""
    args = ["go2tool", "connect-wifi", "--ssid", ssid, "--password", password]
    if mac:
        args += ["--mac", mac]
    if serial:
        args += ["--serial", serial]
    if name:
        args += ["--name", name]
    if country:
        args += ["--country", country]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    if retries is not None:
        args += ["--retries", str(retries)]
    try:
        proc = _run(args, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout",
                "message": "connect-wifi timed out (BLE scan/connect took too long)."}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "cmd": "dimos " + " ".join(a if a != password else "***" for a in args),
    }


def _parse_discoveries(text: str) -> List[Dict[str, str]]:
    """Parse `go2tool discover` table output. Columns are
    SOURCE NAME IP MAC SERIAL, but alignment is whitespace-based and some cells
    can be blank, so we extract IP and MAC by regex per line (robust) and take
    the remaining tokens positionally for name/source/serial."""
    robots: List[Dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("SOURCE") or s.startswith("Stopped"):
            continue
        ipm = _IP_RE.search(s)
        macm = _MAC_RE.search(s)
        if not ipm and not macm:
            continue
        ip = ipm.group(1) if ipm else ""
        mac = macm.group(1) if macm else ""
        key = (ip, mac)
        if key in seen:
            continue
        seen.add(key)
        tokens = s.split()
        source = tokens[0] if tokens else ""
        # name is usually the 2nd column, before the IP
        name = ""
        if len(tokens) >= 2 and tokens[1] not in (ip, mac):
            name = tokens[1]
        serial = tokens[-1] if tokens and tokens[-1] not in (ip, mac, source, name) else ""
        robots.append({"source": source, "name": name, "ip": ip,
                       "mac": mac, "serial": serial, "raw": s})
    return robots


def discover(timeout: float = 8.0, lan_only: bool = True) -> List[Dict[str, str]]:
    """Run `dimos go2tool discover` for `timeout` seconds and return the
    discovered robots as IP/MAC/name records. LAN mode actively probes, so it's
    more reliable than the passive ARP cache right after provisioning."""
    args = ["go2tool", "discover"]
    if lan_only:
        args.append("--lan")
    args += ["-t", str(timeout)]
    try:
        proc = _run(args, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return []
    return _parse_discoveries(proc.stdout)


def ip_neigh_for_mac(mac_suffix: str) -> Optional[str]:
    """Fallback: look up an IP in the kernel neighbour (ARP) table by MAC (full
    or suffix, case-insensitive). Only finds entries already in the cache."""
    suffix = mac_suffix.strip().lower()
    if not suffix:
        return None
    try:
        proc = subprocess.run(["ip", "neigh", "show"], capture_output=True,
                              text=True, timeout=10)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in proc.stdout.splitlines():
        low = line.lower()
        if suffix in low:
            ipm = _IP_RE.search(line)
            if ipm:
                return ipm.group(1)
    return None


def find_ip_for_mac(mac: str, timeout: float = 8.0) -> Dict[str, object]:
    """Resolve a robot IP for a given MAC (full or suffix). Tries active LAN
    discovery first, then the ARP cache. Returns {found, ip, mac, source} plus
    the full discovery list for display."""
    mac_l = (mac or "").strip().lower()
    robots = discover(timeout=timeout, lan_only=True)
    # Exact match, else suffix match on the MAC.
    match = None
    for r in robots:
        rmac = r.get("mac", "").lower()
        if rmac and (rmac == mac_l or (mac_l and rmac.endswith(mac_l))):
            match = r
            break
    if match:
        return {"found": True, "ip": match["ip"], "mac": match["mac"],
                "source": "discover", "robots": robots}
    # Fallback: ARP cache (use last 2 octets as suffix if a full MAC given).
    suffix = mac_l
    if _MAC_RE.search(mac_l or ""):
        suffix = ":".join(mac_l.split(":")[-2:])
    ip = ip_neigh_for_mac(suffix) if suffix else None
    if ip:
        return {"found": True, "ip": ip, "mac": mac_l, "source": "arp",
                "robots": robots}
    return {"found": False, "ip": None, "mac": mac_l, "source": None,
            "robots": robots}


# --------------------------------------------------------------------------- #
# Teleop: send Twist velocity commands via `dimos topic send`
# --------------------------------------------------------------------------- #
def _twist_expr(lx: float, ly: float, lz: float,
                ax: float, ay: float, az: float) -> str:
    """Build the verified `topic send` Python expression for a Twist. Confirmed
    working form: Twist([lx,ly,lz],[ax,ay,az]) — positional vector-likes, with
    Twist/Vector3 available by bare name in the eval context."""
    return f"Twist([{lx},{ly},{lz}],[{ax},{ay},{az}])"


def topic_send(topic: str, expr: str,
               transport: Optional[str] = None) -> Dict[str, object]:
    """Publish one message to a topic via `dimos [--transport t] topic send`."""
    pre = ["--transport", transport] if transport else []
    args = [*pre, "topic", "send", topic, expr]
    try:
        proc = _run(args, timeout=20)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "cmd": "dimos " + " ".join(args)}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "cmd": "dimos " + " ".join(args),
    }


def send_twist(lx: float = 0.0, ly: float = 0.0, lz: float = 0.0,
               ax: float = 0.0, ay: float = 0.0, az: float = 0.0,
               topic: str = "/cmd_vel",
               transport: Optional[str] = None) -> Dict[str, object]:
    """Send a Twist velocity command via the one-shot CLI (`dimos topic send`).

    This is the ~2.1 s/call path — kept only as a reliable FALLBACK (e.g. for
    STOP if the warm daemon is wedged). The teleop hot path uses the persistent
    daemon below (`teleop_send`), which is sub-millisecond once warm."""
    return topic_send(topic, _twist_expr(lx, ly, lz, ax, ay, az), transport)


# --------------------------------------------------------------------------- #
# Persistent teleop daemon (the low-latency hot path)
# --------------------------------------------------------------------------- #
# The dimOS CLI pays ~2.1 s of Python/dimOS import overhead on every
# `dimos topic send`. For teleop that's a 1-2 s press->motion lag. We instead
# keep ONE long-lived process (running under the dimOS conda python) that pays
# that cost once and holds a warm transport, so each publish is <1 ms once warm.
# See backend/teleop_daemon.py for the protocol and fidelity notes.

# The dimOS conda interpreter (NOT the panel venv — the daemon needs dimos.msgs.*
# / dimos.core, which only exist there). Override with DIMOS_PY if it moves.
DIMOS_PY = os.environ.get(
    "DIMOS_PY", "/home/alex/miniconda3/envs/dimos/bin/python3")
_DAEMON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "teleop_daemon.py")


class _TeleopDaemon:
    """Owns the single warm daemon subprocess. Thread-safe: one send at a time
    (each is a stdin write + one ack line read — a synchronous request/response
    over the pipe). Respawns transparently if the daemon died or the requested
    transport changed; explicit kill() is called on run/stop/restart so the
    daemon is always tied to the current robot connection (a stale daemon
    holding an old transport session would silently stop working after a
    reconnect)."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.transport: Optional[str] = None
        self.ready: bool = False
        self.warmup_s: Optional[float] = None
        self.lock = threading.Lock()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> Optional[str]:
        if self.proc is None or self.proc.stdout is None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return self.proc.stdout.readline()

    def _kill_locked(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.proc = None
        self.transport = None
        self.ready = False
        self.warmup_s = None

    def _spawn_locked(self, transport: Optional[str]) -> Dict[str, object]:
        args = [DIMOS_PY, _DAEMON_PATH]
        if transport:
            args += ["--transport", transport]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            start_new_session=True)
        self.transport = transport
        # Wait for the readiness line (imports + eval-context warmup ~1.4 s).
        ready_line = self._readline(timeout=45)
        if not ready_line:
            self._kill_locked()
            return {"ok": False, "error": "daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False, "error": info.get("error", "daemon init failed")}
        self.ready = True
        self.warmup_s = info.get("warmup_s")
        return {"ok": True, "warmup_s": self.warmup_s,
                "transport": info.get("transport")}

    def _ensure_locked(self, transport: Optional[str]) -> Optional[Dict[str, object]]:
        """Spawn/respawn if needed. Returns an error dict on failure, else None."""
        if self._alive() and self.transport == transport and self.ready:
            return None
        self._kill_locked()
        res = self._spawn_locked(transport)
        return None if res.get("ok") else res

    def request(self, payload: Dict[str, object],
                transport: Optional[str]) -> Dict[str, object]:
        with self.lock:
            err = self._ensure_locked(transport)
            if err is not None:
                return err
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._kill_locked()
                return {"ok": False, "error": f"daemon pipe broken: {e}"}
            ack = self._readline(timeout=5)
            if ack is None:
                # Daemon hung or died mid-send — drop it so the next call respawns.
                self._kill_locked()
                return {"ok": False, "error": "daemon send timeout"}
            try:
                return json.loads(ack)
            except json.JSONDecodeError:
                return {"ok": True, "raw": ack.strip()}

    def kill(self) -> None:
        with self.lock:
            self._kill_locked()

    def status(self) -> Dict[str, object]:
        with self.lock:
            return {"alive": self._alive(), "ready": self.ready,
                    "transport": self.transport, "warmup_s": self.warmup_s,
                    "pid": self.proc.pid if self.proc else None}


_teleop_daemon = _TeleopDaemon()


def teleop_send(lx: float = 0.0, ly: float = 0.0, lz: float = 0.0,
                ax: float = 0.0, ay: float = 0.0, az: float = 0.0,
                topic: str = "/cmd_vel",
                transport: Optional[str] = None) -> Dict[str, object]:
    """Low-latency Twist publish via the warm daemon. First call (cold) pays the
    one-time ~1.4 s spawn+warmup; every call after is sub-millisecond."""
    payload = {"topic": topic or "/cmd_vel",
               "lx": lx, "ly": ly, "lz": lz, "ax": ax, "ay": ay, "az": az}
    return _teleop_daemon.request(payload, transport or None)


def teleop_warm(topic: str = "/cmd_vel",
                transport: Optional[str] = None) -> Dict[str, object]:
    """Eagerly spawn+warm the daemon and PRIME the topic's publisher so the FIRST
    real press is sub-ms (no spawn or first-publish setup lag). Priming sends one
    zero Twist — a STOP — which is safe (no motion) and the right post-connect
    state anyway."""
    return _teleop_daemon.request({"cmd": "ping", "topic": topic or "/cmd_vel"},
                                  transport or None)


def teleop_kill() -> None:
    """Tear down the daemon. Called on run/stop/restart so it never outlives the
    robot connection it was bound to."""
    _teleop_daemon.kill()


def teleop_status() -> Dict[str, object]:
    return _teleop_daemon.status()


# --------------------------------------------------------------------------- #
# Persistent SPORT / gesture RPC daemon (separate from teleop)
# --------------------------------------------------------------------------- #
# GO2Connection's gesture/utility methods (sport_command, standup, set_light,
# battery_soc, ...) are @rpc methods dispatched over the SAME LCM/Zenoh bus the
# robot runs on — NOT pub/sub topics, NOT MCP (this blueprint has no MCP server).
# The client is `rpc_backend()()` + `rpc.call_sync("GO2Connection/<m>", ([args],
# {}))` (verified live: GO2Connection/battery_soc -> 32). backend/sport_daemon.py
# keeps that RPC client warm. This is entirely independent of teleop — it never
# publishes Twist and never touches /cmd_vel.
_SPORT_DAEMON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "sport_daemon.py")
# The connection module's RPC instance name for unitree-go2-basic (verified the
# instance name is the class name, not lowercased).
GO2_RPC_MODULE = "GO2Connection"


class _SportDaemon:
    """Warm RPC client for gesture/utility commands. Same manager shape as the
    teleop daemon (thread-safe, one request in flight, respawn on death/transport
    change, killed on run/stop/restart) but calls @rpc methods and returns their
    values instead of publishing Twist."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.transport: Optional[str] = None
        self.module: str = GO2_RPC_MODULE
        self.ready: bool = False
        self.warmup_s: Optional[float] = None
        self.lock = threading.Lock()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> Optional[str]:
        if self.proc is None or self.proc.stdout is None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return self.proc.stdout.readline()

    def _kill_locked(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.proc = None
        self.transport = None
        self.ready = False
        self.warmup_s = None

    def _spawn_locked(self, transport: Optional[str], module: str) -> Dict[str, object]:
        args = [DIMOS_PY, _SPORT_DAEMON_PATH, "--module", module]
        if transport:
            args += ["--transport", transport]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.transport = transport
        self.module = module
        ready_line = self._readline(timeout=45)
        if not ready_line:
            self._kill_locked()
            return {"ok": False, "error": "sport daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False, "error": info.get("error", "sport daemon init failed")}
        self.ready = True
        self.warmup_s = info.get("warmup_s")
        return {"ok": True, "warmup_s": self.warmup_s, "module": info.get("module")}

    def request(self, payload: Dict[str, object], transport: Optional[str],
                module: str) -> Dict[str, object]:
        with self.lock:
            if not (self._alive() and self.transport == transport
                    and self.module == module and self.ready):
                self._kill_locked()
                err = self._spawn_locked(transport, module)
                if not err.get("ok"):
                    return err
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._kill_locked()
                return {"ok": False, "error": f"sport daemon pipe broken: {e}"}
            # RPC calls can legitimately take a few seconds; allow generous read.
            ack = self._readline(timeout=20)
            if ack is None:
                self._kill_locked()
                return {"ok": False, "error": "sport daemon send timeout"}
            try:
                return json.loads(ack)
            except json.JSONDecodeError:
                return {"ok": True, "raw": ack.strip()}

    def kill(self) -> None:
        with self.lock:
            self._kill_locked()

    def status(self) -> Dict[str, object]:
        with self.lock:
            return {"alive": self._alive(), "ready": self.ready,
                    "transport": self.transport, "module": self.module,
                    "warmup_s": self.warmup_s,
                    "pid": self.proc.pid if self.proc else None}


_sport_daemon = _SportDaemon()


def sport_call(method: str, args: Optional[list] = None,
               transport: Optional[str] = None,
               module: str = GO2_RPC_MODULE,
               timeout: Optional[float] = None) -> Dict[str, object]:
    """Invoke an @rpc method on the connection module via the warm RPC client.
    Returns {ok, result, ms} — `result` is the method's own return (e.g.
    sport_command's accepted-bool), or {ok:false, error} if the RPC failed or the
    remote raised (e.g. 'Data channel is not open' when the link is flapping)."""
    payload: Dict[str, object] = {"method": method, "args": args or []}
    if timeout is not None:
        payload["timeout"] = timeout
    return _sport_daemon.request(payload, transport or None, module)


def sport_command_status(api_id: int, transport: Optional[str] = None,
                         module: str = GO2_RPC_MODULE) -> Dict[str, object]:
    """Same physical action as sport_call('sport_command', [api_id]) — sends
    the exact same command to the robot — but reads back the real firmware
    status.code instead of the always-truthy bool sport_command() itself
    returns (dimOS bug: `return bool(publish_request(...))`, and any response
    dict is truthy, so a firmware REJECTION looks identical to acceptance at
    that layer). Returns {ok, code, accepted, raw, ms} — code 0 means the
    firmware actually accepted it; e.g. 3203 means "API not implemented",
    a real firmware-level rejection, not a bug on our end.
    Kept at 15s (under the manager's fixed 20s read window) — some
    animations run long; raise if a specific command needs more."""
    payload: Dict[str, object] = {"cmd": "sport_status", "api_id": api_id, "timeout": 15}
    return _sport_daemon.request(payload, transport or None, module)


def sport_warm(transport: Optional[str] = None,
               module: str = GO2_RPC_MODULE) -> Dict[str, object]:
    """Eagerly spawn+warm the RPC client so the first gesture is instant."""
    return _sport_daemon.request({"cmd": "ping"}, transport or None, module)


def sport_kill() -> None:
    """Tear down the sport daemon — called on run/stop/restart alongside the
    teleop daemon, so it never outlives its robot connection."""
    _sport_daemon.kill()


def sport_status() -> Dict[str, object]:
    return _sport_daemon.status()


# --------------------------------------------------------------------------- #
# Persistent camera daemon (plain low-latency MJPEG feed of /color_image)
# --------------------------------------------------------------------------- #
# Replaces the heavy Rerun web viewer as the dashboard's Camera panel: a
# lightweight <img> pointed at a direct MJPEG multipart stream. Unlike the
# teleop/sport daemons, this one does NOT use the stdin/stdout JSON request
# protocol — camera frames are large binary payloads that don't belong on a
# line-delimited-JSON pipe. Instead the daemon (backend/camera_daemon.py)
# subscribes to /color_image ONCE, JPEG-encodes each decoded frame, and serves
# it over its OWN stdlib HTTP server bound 0.0.0.0 (so a browser on another LAN
# device can reach it). This manager therefore only spawns/tracks/kills it and
# reads its single readiness line; there is no per-request round-trip. Same
# lifecycle discipline as the other daemons: respawn on transport change, and
# killed on run/stop/restart so it never outlives its robot connection.
_CAMERA_DAEMON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "camera_daemon.py")
# Fixed local port for the MJPEG stream. 8095/8096 were already taken on this
# box (a multiprocessing forkserver grabs sequential ports from 8095), so we
# use 8770 — clear of that range and of the 8090/8091 control panels. Override
# with DIMOS_CAMERA_PORT if it clashes. The browser builds the actual <img> src
# from its OWN window.location.hostname + this port (never a hardcoded host),
# same redeploy-safe pattern as the Rerun/5555 panels.
CAMERA_STREAM_PORT = int(os.environ.get("DIMOS_CAMERA_PORT", "8770"))
CAMERA_STREAM_TOPIC = os.environ.get("DIMOS_CAMERA_TOPIC", "/color_image")

# Lidar point-cloud daemon (see backend/lidar_daemon.py). Serves the raw (N,3)
# float32 cloud as a binary octet-stream for a custom WebGL renderer that
# replaces the rejected Rerun map view. Fixed port 8771 — clear of the 8770
# camera daemon and the 8090/8091 control panels. Override with DIMOS_LIDAR_PORT.
# The topic is 'lidar' (app.peek_stream('lidar')), NOT '/pointcloud' (dead here).
_LIDAR_DAEMON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "lidar_daemon.py")
LIDAR_STREAM_PORT = int(os.environ.get("DIMOS_LIDAR_PORT", "8771"))
LIDAR_STREAM_TOPIC = os.environ.get("DIMOS_LIDAR_TOPIC", "lidar")


class _CameraDaemon:
    """Owns the single camera-daemon subprocess. Thread-safe spawn/kill/ensure.
    No request pipe (frames flow over the daemon's own HTTP server); this manager
    just guarantees at most one daemon is alive, bound to the current run's
    transport, and tears it down on run/stop/restart."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.transport: Optional[str] = None
        self.port: int = CAMERA_STREAM_PORT
        self.topic: str = CAMERA_STREAM_TOPIC
        self.ready: bool = False
        self.warmup_s: Optional[float] = None
        self.lock = threading.Lock()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> Optional[str]:
        if self.proc is None or self.proc.stdout is None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return self.proc.stdout.readline()

    def _kill_locked(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.proc = None
        self.transport = None
        self.ready = False
        self.warmup_s = None

    def _spawn_locked(self, transport: Optional[str]) -> Dict[str, object]:
        args = [DIMOS_PY, _CAMERA_DAEMON_PATH,
                "--topic", self.topic, "--port", str(self.port)]
        if transport:
            args += ["--transport", transport]
        # stdout carries only the single readiness JSON line; the daemon writes
        # nothing more to it (all diagnostics -> its own stderr, DEVNULL'd here),
        # so the pipe never backs up. stdin is unused.
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.transport = transport
        # dimOS import + subscription + HTTP bind (~0.4-2 s).
        ready_line = self._readline(timeout=45)
        if not ready_line:
            self._kill_locked()
            return {"ok": False, "error": "camera daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False, "error": info.get("error", "camera daemon init failed")}
        self.ready = True
        self.warmup_s = info.get("warmup_s")
        return {"ok": True, "warmup_s": self.warmup_s, "port": self.port,
                "topic": self.topic, "transport": info.get("transport")}

    def ensure(self, transport: Optional[str]) -> Dict[str, object]:
        """Idempotent: no-op if a daemon is already alive on this transport, else
        (re)spawn one. Safe to call on every availability poll."""
        with self.lock:
            if self._alive() and self.transport == transport and self.ready:
                return {"ok": True, "port": self.port, "topic": self.topic,
                        "transport": self.transport, "already": True}
            self._kill_locked()
            return self._spawn_locked(transport)

    def kill(self) -> None:
        with self.lock:
            self._kill_locked()

    def status(self) -> Dict[str, object]:
        with self.lock:
            return {"alive": self._alive(), "ready": self.ready,
                    "transport": self.transport, "port": self.port,
                    "topic": self.topic, "warmup_s": self.warmup_s,
                    "pid": self.proc.pid if self.proc else None}


_camera_daemon = _CameraDaemon()


def camera_ensure(transport: Optional[str] = None) -> Dict[str, object]:
    """Lazily spawn (or reuse) the MJPEG camera daemon for the current run's
    transport. Called by the /api/camera-stream availability probe so the feed
    works against an already-running robot without a restart, and by
    run_blueprint on launch for go2 blueprints."""
    return _camera_daemon.ensure(transport or None)


def camera_kill() -> None:
    """Tear down the camera daemon — called on run/stop/restart alongside the
    teleop/sport daemons so it never outlives its robot connection."""
    _camera_daemon.kill()


def camera_status() -> Dict[str, object]:
    return _camera_daemon.status()


# --------------------------------------------------------------------------- #
# Persistent lidar daemon (raw point-cloud buffer for the custom WebGL 3D map)
# --------------------------------------------------------------------------- #
# Same shape as _CameraDaemon: one subprocess in the dimOS conda env, spawned/
# tracked/killed here, serving over its own HTTP server (backend/lidar_daemon.py
# connects once via Dimos.connect(), polls peek_stream('lidar'), and serves the
# latest (N,3) float32 cloud as a binary octet-stream). No per-request pipe.
# Killed on run/stop/restart so it never outlives its robot connection.
class _LidarDaemon:
    """Owns the single lidar-daemon subprocess. Thread-safe spawn/kill/ensure."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.transport: Optional[str] = None
        self.port: int = LIDAR_STREAM_PORT
        self.topic: str = LIDAR_STREAM_TOPIC
        self.ready: bool = False
        self.warmup_s: Optional[float] = None
        self.lock = threading.Lock()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> Optional[str]:
        if self.proc is None or self.proc.stdout is None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return self.proc.stdout.readline()

    def _kill_locked(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.proc = None
        self.transport = None
        self.ready = False
        self.warmup_s = None

    def _spawn_locked(self, transport: Optional[str]) -> Dict[str, object]:
        args = [DIMOS_PY, _LIDAR_DAEMON_PATH,
                "--topic", self.topic, "--port", str(self.port)]
        if transport:
            args += ["--transport", transport]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.transport = transport
        # dimOS import + connect + HTTP bind (~0.3-2 s).
        ready_line = self._readline(timeout=45)
        if not ready_line:
            self._kill_locked()
            return {"ok": False, "error": "lidar daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False, "error": info.get("error", "lidar daemon init failed")}
        self.ready = True
        self.warmup_s = info.get("warmup_s")
        return {"ok": True, "warmup_s": self.warmup_s, "port": self.port,
                "topic": self.topic, "transport": info.get("transport")}

    def ensure(self, transport: Optional[str]) -> Dict[str, object]:
        with self.lock:
            if self._alive() and self.transport == transport and self.ready:
                return {"ok": True, "port": self.port, "topic": self.topic,
                        "transport": self.transport, "already": True}
            self._kill_locked()
            return self._spawn_locked(transport)

    def kill(self) -> None:
        with self.lock:
            self._kill_locked()

    def status(self) -> Dict[str, object]:
        with self.lock:
            return {"alive": self._alive(), "ready": self.ready,
                    "transport": self.transport, "port": self.port,
                    "topic": self.topic, "warmup_s": self.warmup_s,
                    "pid": self.proc.pid if self.proc else None}


_lidar_daemon = _LidarDaemon()


def lidar_ensure(transport: Optional[str] = None) -> Dict[str, object]:
    """Lazily spawn (or reuse) the lidar point-cloud daemon for the current run's
    transport. Called by the /api/lidar-stream availability probe (so it works
    against an already-running robot without a restart) and by run_blueprint on
    launch for go2 blueprints."""
    return _lidar_daemon.ensure(transport or None)


def lidar_kill() -> None:
    """Tear down the lidar daemon — called on run/stop/restart alongside the
    camera daemon so it never outlives its robot connection."""
    _lidar_daemon.kill()


def lidar_status() -> Dict[str, object]:
    return _lidar_daemon.status()


# --------------------------------------------------------------------------- #
# Arm workcell USB camera daemon (backend/arm_camera_daemon.py)
# --------------------------------------------------------------------------- #
# Same manager shape as _CameraDaemon, but the source is a local USB camera —
# no transport, NOT tied to the robot connection. Lives for the server's
# lifetime; killed only via arm_camera_kill() on /api/server/shutdown.
# Port 8772: clear of 8770 (go2 camera) and 8771 (lidar).
_ARM_CAMERA_DAEMON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "arm_camera_daemon.py")
ARM_CAMERA_PORT = int(os.environ.get("ARM_CAMERA_PORT", "8772"))
ARM_CAM_PY = os.environ.get("ARM_CAM_PY", DIMOS_PY)


class _ArmCameraDaemon:
    """Owns the single arm-camera subprocess. Thread-safe spawn/kill/ensure."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.device: Optional[int] = None
        self.port: int = ARM_CAMERA_PORT
        self.ready: bool = False
        self.lock = threading.Lock()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _readline(self, timeout: float) -> Optional[str]:
        if self.proc is None or self.proc.stdout is None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return self.proc.stdout.readline()

    def _kill_locked(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.proc = None
        self.device = None
        self.ready = False

    def _spawn_locked(self, device: int) -> Dict[str, object]:
        args = [ARM_CAM_PY, _ARM_CAMERA_DAEMON_PATH,
                "--device", str(device), "--port", str(self.port)]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            start_new_session=True)
        self.device = device
        ready_line = self._readline(timeout=20)
        if not ready_line:
            self._kill_locked()
            return {"ok": False,
                    "error": "arm camera daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False,
                    "error": info.get("error", "arm camera daemon init failed")}
        self.ready = True
        return {"ok": True, "port": self.port, "device": device}

    def ensure(self, device: int = 0) -> Dict[str, object]:
        with self.lock:
            if self._alive() and self.device == device and self.ready:
                return {"ok": True, "port": self.port, "device": device,
                        "already": True}
            self._kill_locked()
            return self._spawn_locked(device)

    def kill(self) -> None:
        with self.lock:
            self._kill_locked()

    def status(self) -> Dict[str, object]:
        with self.lock:
            return {"alive": self._alive(), "ready": self.ready,
                    "device": self.device, "port": self.port,
                    "pid": self.proc.pid if self.proc else None}


_arm_camera_daemon = _ArmCameraDaemon()


def arm_camera_ensure(device: int = 0) -> Dict[str, object]:
    """Lazily spawn (or reuse) the arm USB camera daemon."""
    return _arm_camera_daemon.ensure(device)


def arm_camera_kill() -> None:
    _arm_camera_daemon.kill()


def arm_camera_status() -> Dict[str, object]:
    return _arm_camera_daemon.status()


_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")
_SPY_ROW_RE = _re.compile(
    r"^(lcm|zenoh)\s+(\S+)\s+(\S+)\s+([\d.]+)\s+(.+?)\s*$"
)


def _spy_snapshot(seconds: float = 5.0, transport: Optional[str] = None):
    """Run `dimos spy` for `seconds` and parse its live topic-rate table.

    `dimos spy` is a full-screen Textual TUI, so it needs a real pty (not a
    plain pipe) to render at all. This is also our liveness check's ONLY
    reliable option — `dimos topic echo` was tested extensively (bare topic,
    explicit type_name, plain pipe, unbuffered env, real pty, up to 15s
    windows) and reproducibly never prints a single decoded message even
    while the exact same topic is visibly active in `spy` at double-digit Hz.
    That's a bug in the installed dimOS CLI's `topic echo`, not a buffering
    or timing issue on our side — so liveness detection uses `spy`'s table
    instead, which does correctly reflect real bus activity.

    Returns {topic: {"type": str, "freq_hz": float, "bandwidth": str}}.
    """
    import pty
    import select

    master, slave = pty.openpty()
    pre = ["--transport", transport] if transport else []
    proc = subprocess.Popen(
        [DIMOS_BIN, *pre, "spy"],
        stdout=slave, stderr=slave, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    os.close(slave)

    buf = b""
    end = time.time() + seconds
    try:
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.5)
            if master in r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.close(master)
        except OSError:
            pass

    text = _ANSI_RE.sub("", buf.decode("utf-8", errors="replace"))
    rows: Dict[str, Dict[str, object]] = {}
    for line in text.split("\n"):
        line = line.strip().strip("│").strip()
        m = _SPY_ROW_RE.match(line)
        if m:
            _tr, topic, type_name, freq, bw = m.groups()
            rows[topic] = {"type": type_name, "freq_hz": float(freq), "bandwidth": bw.strip()}
    return rows, text


def topic_alive(topic: str, seconds: float = 5.0,
                transport: Optional[str] = None) -> Dict[str, object]:
    """Is `topic` actually producing messages right now? Backed by `dimos spy`
    (see `_spy_snapshot` for why `topic echo` can't be used for this).

    Textual (the TUI framework `spy` is built on) redraws incrementally —
    a row's topic/type text can render in full only once, with later frames
    sending just updated frequency digits via cursor-positioned partial
    updates. So a strict per-line column parse can miss a topic that's
    genuinely active but whose full row happened to render before our
    capture window's first read, or never re-render in full again. Primary
    signal is the strict row parse (gives real Hz/bandwidth); if that comes
    up empty, fall back to "does this exact topic name appear anywhere in
    the captured output at all" — looser, but avoids a false "not alive"
    from a rendering-timing miss rather than the topic genuinely being
    silent.
    """
    with _SPY_LOCK:
        rows, raw_text = _spy_snapshot(seconds=seconds, transport=transport)
    row = rows.get(topic)
    if row is not None:
        freq = float(row["freq_hz"])
        return {
            "topic": topic,
            "count": round(freq * seconds),
            "alive": freq > 0,
            "sample": f"{row['type']} @ {freq:g} Hz, {row['bandwidth']}",
        }

    # Fallback: loose substring match, word-bounded so e.g. "/odom" doesn't
    # false-positive inside a longer topic name like "/odom_raw".
    seen = bool(_re.search(re_escape_topic(topic) + r"(?!\w)", raw_text))
    return {
        "topic": topic,
        "count": 1 if seen else 0,
        "alive": seen,
        "sample": "seen in spy output (rate unparsed — rendering timing)" if seen else None,
    }


def re_escape_topic(topic: str) -> str:
    return _re.escape(topic)


def any_topic_alive(topics: List[str], seconds: float = 5.0,
                    transport: Optional[str] = None) -> Dict[str, object]:
    """Is ANY of `topics` producing messages right now? One shared `dimos spy`
    snapshot checked against every candidate, instead of one spy process per
    topic — cheaper, and avoids several `spy` instances contending for the
    same subscription at once (seen to produce inconsistent per-run results
    on this robot's connection, which is itself genuinely intermittent —
    different topics go quiet/active at different moments)."""
    with _SPY_LOCK:
        rows, raw_text = _spy_snapshot(seconds=seconds, transport=transport)
    per_topic: Dict[str, dict] = {}
    for t in topics:
        row = rows.get(t)
        if row is not None:
            freq = float(row["freq_hz"])
            per_topic[t] = {"alive": freq > 0, "sample": f"{row['type']} @ {freq:g} Hz, {row['bandwidth']}"}
        else:
            seen = bool(_re.search(re_escape_topic(t) + r"(?!\w)", raw_text))
            per_topic[t] = {"alive": seen, "sample": "seen in spy output (rate unparsed)" if seen else None}
    return {
        "topics": per_topic,
        "alive": any(v["alive"] for v in per_topic.values()),
    }

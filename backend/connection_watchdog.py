#!/usr/bin/env python3
"""External connectivity watchdog — detects a SILENT WebRTC session death and
re-establishes the robot connection with a FRESHLY RE-DISCOVERED IP.

WHY THIS EXISTS (real incident, 2026-07-24 night): during a 20-min data-collection
walk the Go2's single WebRTC session (`GO2Connection`, wrapping aiortc) died
mid-run. `camera_daemon`'s and `lidar_daemon`'s /health both went `fresh: false`
at the same instant and stayed frozen for 2+ minutes, while `/api/status` kept
cheerfully reporting `running: true` the entire time. Nothing in dimOS noticed:

  - the 2s heartbeat writes `heartbeat_response` but NOTHING EVER READS IT;
  - `reconnect()` exists in the webrtc driver and is never called from anywhere;
  - ICE/peer `failed`/`closed` handlers only `print(...)`;
  - the reactive plumbing wires only the "new data" callback, never
    on_error/on_completed — so a dead session doesn't throw, it just stops
    emitting, forever, silently;
  - `/api/status` is `os.path.exists(/proc/<pid>)` — PROCESS liveness, not DATA
    liveness. It is structurally blind to this entire failure class.

And a plain restart does NOT fix it: `dimos_cli.restart()` relaunches with the
OLD tracked `robot_ip`. The robot came back on a DIFFERENT IP (DHCP lease change
after the power-cycle), so a restart would just fail against a stale address.
Recovery requires re-discovery, which is why this calls `/api/run-with-retry`
with a fresh IP and explicitly NOT `/api/restart`.

DETECTION SIGNAL — why "both, simultaneously, sustained": every sensor topic
(color_image, lidar, odom, ...) is a subscription on ONE shared RTCPeerConnection
upstream; camera_daemon and lidar_daemon are independent downstream subscribers
with no coupling to each other. So the only thing that can freeze BOTH at once is
the shared upstream session dying. A single daemon going stale is an ordinary
per-topic hiccup and is deliberately ignored. We additionally require the dual
staleness to be SUSTAINED past --trip-after so a momentary blip can't trip it.

SECOND DETECTION PATH — sentinel tailing (added after the 2026-07-25 incidents,
six live reproductions). The health poll above is an INFERENCE: it watches two
downstream consumers go quiet and concludes the upstream died, which is why it
has to wait out --trip-after (15-20s) before believing itself. Meanwhile the
robot connection itself now says so directly: two patched files (see
backend/vendor_patches/, copied onto the Ascent's site-packages) print a
greppable sentinel the moment the link is known-dead —

  GO2_WEBRTC_TRACK_DEAD  the video track raised MediaStreamError: the track is
                         stopped forever and the shared DTLS transport under
                         lidar/odom/cmd_vel went with it. Ground truth, not
                         inference.
  GO2_HEARTBEAT_DEAD     the robot stopped answering the driver's own 2s
                         heartbeat for >6s. ~6s detection instead of waiting out
                         aiortc's ~28s ICE consent-expiry floor.

So a background thread tails the running blueprint's stdout log for those two
strings. A hit is NOT a second, parallel trip path: it is a second way to ARM
the one trip decision below — it says "consider the staleness threshold already
satisfied", and then every existing gate (dormancy, cooldown, panel reachable,
run active, startup grace, blueprint known) applies unchanged. Health polling
stays as the slower backstop that needs nothing patched on the robot side, and
still catches deaths the sentinels can't see (e.g. the blueprint process itself
wedged). Every trip logs which of the two armed it.

=============================================================================
SAFETY SCOPE — READ BEFORE EDITING
This watchdog re-establishes the DATA/TELEMETRY LINK AND NOTHING ELSE.
It must NEVER resume, re-send, or retry any movement command: no teleop, no
sport/gesture RPCs, no nav goals, no "continue the interrupted goal". If the
robot was mid-goal when the link died, reconnecting must NOT restart that goal.
The single outbound side-effecting call in this file is the POST to
/api/run-with-retry. Do not add another one.
=============================================================================

DESIGN CHOICE — standalone sidecar process, not a background task in main.py:
  1. Matches the existing sidecar pattern (camera_daemon / lidar_daemon /
     yolo_watch_daemon): standalone polling loop, own argv, stdout protocol +
     stderr diagnostics.
  2. It must be independently startable/stoppable/observable. An auto-reconnect
     is exactly the thing you want to be able to NOT have running while
     debugging or while the robot is on a bench — a background task inside
     main.py would run unconditionally whenever the panel runs.
  3. Recovery is a long BLOCKING operation: run_blueprint_with_retry watches an
     18s stabilize window per attempt (~40-60s total) and internally calls
     stop() + relaunch. Driving that from inside the same uvicorn process that
     serves the endpoint means the watchdog and its own recovery path share a
     failure domain and an event loop. As a separate process it just waits on a
     socket.
  4. Its own stderr is a clean, greppable incident log for a human during a
     live demo, not interleaved into the panel's request log.
Unlike its sibling daemons this one does NOT need `import dimos`, so it runs
under the ordinary control-panel venv python (same interpreter as main.py).

It imports `dimos_cli` only for `discover()` and the daemon port constants.
dimos_cli is stdlib-only (it shells out to the `dimos` binary) and its
module-level daemon-manager singletons are side-effect free constructors — this
process never calls their start/kill methods, so importing it here cannot
disturb the panel's own daemons.

OUTPUT PROTOCOL (mirrors the sibling daemons' stdout/stderr split):
  stderr — human-readable log lines, timestamped. This is what a person reads.
  stdout — one JSON object per line, machine-readable audit trail of significant
           events (armed / both_stale / trip / discover / recovery_result / ...).
           Also appended to --event-log if given.

TESTING WITHOUT THE ROBOT: you do NOT need to drop the robot's wifi to exercise
this. `kill` the camera_daemon and lidar_daemon processes (or just one, to
confirm it correctly does NOT trip) and watch the detection logic. The sentinel
path is even easier: `echo GO2_HEARTBEAT_DEAD >> /tmp/dimos-pwa-last-launch.log`
(or whatever --sentinel-log points at) fakes a death instantly, with no robot
and no daemons involved. Run with --dry-run to exercise everything including
re-discovery while stopping short of actually POSTing the recovery.

    ./venv/bin/python backend/connection_watchdog.py --dry-run
    ./venv/bin/python backend/connection_watchdog.py            # armed
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dimos_cli  # noqa: E402  (stdlib-only wrapper; see module docstring)

# --------------------------------------------------------------------------- #
# Defaults — every one of these is also a CLI flag.
# --------------------------------------------------------------------------- #
DEFAULT_PANEL = "http://127.0.0.1:8090"

POLL_INTERVAL_S = 6.0        # health poll cadence (brief: 5-10s)
TRIP_AFTER_S = 18.0          # both stale continuously this long => dead (brief: 15-20s)
STARTUP_GRACE_S = 45.0       # ignore a run younger than this (daemons still warming)
COOLDOWN_S = 180.0           # after firing recovery, can't trip again for this long
ABORT_COOLDOWN_S = 60.0      # after an ABORTED recovery (e.g. discovery found nothing)
MAX_COOLDOWN_S = 900.0       # ceiling for the exponential cooldown backoff
HEALTHY_RESET_S = 300.0      # continuous health this long clears the backoff escalation
MAX_RECOVERIES = 5           # then go dormant and just scream (0 = unlimited)
HEALTH_TIMEOUT_S = 3.0       # per /health request
STATUS_TIMEOUT_S = 5.0       # per /api/status request
DISCOVER_TIMEOUT_S = 8.0     # `dimos go2tool discover --lan` probe window
RECOVERY_MAX_ATTEMPTS = 2    # passed through to /api/run-with-retry
SENTINEL_POLL_S = 1.0        # how often the tailer looks for new blueprint output

# The death sentinels printed by the patched robot-side files (see
# backend/vendor_patches/ and the module docstring). Matched as plain
# substrings of a whole stdout line, deliberately NOT as a regex against the
# full line shape: dimOS's console formatter wraps every message in a
# timestamp/level/source-file prefix that we neither control nor care about.
SENTINEL_TRACK_DEAD = "GO2_WEBRTC_TRACK_DEAD"
SENTINEL_HEARTBEAT_DEAD = "GO2_HEARTBEAT_DEAD"
SENTINEL_STRINGS = (SENTINEL_TRACK_DEAD, SENTINEL_HEARTBEAT_DEAD)

# Health probe outcomes. "unreachable" (nothing listening / bad response) and
# "stale" (daemon alive, reporting fresh:false) are DELIBERATELY distinct: both
# currently count toward tripping, but a human debugging a trip needs to know
# which one happened. "daemon process died" and "daemon alive but its upstream
# stream went silent" are completely different bugs with different fixes.
S_OK = "ok"
S_STALE = "stale"
S_UNREACHABLE = "unreachable"


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(*a) -> None:
    """Human-readable diagnostics -> stderr (sibling-daemon convention).

    One write per line, deliberately: the sentinel tailer logs from its own
    thread, and print()'s separate text/newline writes can interleave.
    """
    sys.stderr.write(" ".join([f"[{_ts()}]"] + [str(x) for x in a]) + "\n")
    sys.stderr.flush()


def _banner(lines) -> None:
    """Loud, unmissable multi-line stderr block for trips and recoveries."""
    bar = "=" * 78
    print(f"\n{bar}", file=sys.stderr, flush=True)
    for ln in lines:
        print(f"[{_ts()}] {ln}", file=sys.stderr, flush=True)
    print(f"{bar}\n", file=sys.stderr, flush=True)


class EventSink:
    """Structured audit trail: one JSON object per line on stdout, optionally
    tee'd to a file so a human can reconstruct a silent recovery after the fact."""

    def __init__(self, path=None):
        self.path = path
        # The sentinel tailer emits from its own thread; one lock + one write
        # per record keeps the one-JSON-object-per-line contract intact.
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields) -> None:
        rec = {"t": time.time(), "iso": _ts(), "event": kind, **fields}
        line = json.dumps(rec, default=str)
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if self.path:
                try:
                    with open(self.path, "a") as f:
                        f.write(line + "\n")
                except OSError as e:
                    _log(f"WARN: could not append to event log {self.path}: {e!r}")


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def _get_json(url: str, timeout: float):
    """GET a JSON endpoint. Returns (payload_or_None, error_string_or_None)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return None, "timeout"
        if isinstance(reason, ConnectionRefusedError):
            return None, "connection refused (nothing listening)"
        return None, f"unreachable: {reason!r}"
    except (socket.timeout, TimeoutError):
        return None, "timeout"
    except OSError as e:
        return None, f"socket error: {e!r}"
    try:
        return json.loads(raw.decode("utf-8", "replace")), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"bad JSON response: {e!r}"


def probe_health(url: str, timeout: float, max_age=None) -> dict:
    """Probe one daemon's /health. Returns
    {state, detail, fresh, age_s, extra} where state is S_OK / S_STALE /
    S_UNREACHABLE.

    S_UNREACHABLE  = the health port itself did not answer usefully (daemon
                     process not up, wrong port, garbage response).
    S_STALE        = daemon answered, but reports fresh:false (or an age beyond
                     --max-age) — i.e. the process is fine, its upstream data
                     stream is not. THIS is the silent-WebRTC-death signature.
    """
    payload, err = _get_json(url, timeout)
    if payload is None:
        return {"state": S_UNREACHABLE, "detail": err, "fresh": None,
                "age_s": None, "extra": {}}

    fresh = bool(payload.get("fresh"))
    age = payload.get("age_s")
    # Keep the handful of fields that make a trip log actually diagnosable.
    extra = {k: payload.get(k) for k in
             ("frames", "has_real_frame", "points", "has_data", "updates", "topic")
             if k in payload}

    if fresh and max_age is not None and isinstance(age, (int, float)) and age > max_age:
        return {"state": S_STALE, "fresh": True, "age_s": age, "extra": extra,
                "detail": f"daemon says fresh but age_s={age} exceeds --max-age {max_age}"}
    if fresh:
        return {"state": S_OK, "fresh": True, "age_s": age, "extra": extra,
                "detail": f"fresh (age_s={age})"}
    if not payload.get("has_data", payload.get("has_real_frame", True)):
        return {"state": S_STALE, "fresh": False, "age_s": age, "extra": extra,
                "detail": "daemon up but has NEVER received data on this connection"}
    return {"state": S_STALE, "fresh": False, "age_s": age, "extra": extra,
            "detail": f"daemon up, stream stale (age_s={age})"}


def _describe(name: str, p: dict) -> str:
    return f"{name}={p['state']} ({p['detail']})"


# --------------------------------------------------------------------------- #
# Fast-path detection: tail the blueprint's stdout for a death sentinel
# --------------------------------------------------------------------------- #
class SentinelTailer(threading.Thread):
    """Watches the running blueprint's stdout log for GO2_WEBRTC_TRACK_DEAD /
    GO2_HEARTBEAT_DEAD and parks the newest hit for the main loop to consume.

    This thread is READ-ONLY in every sense: it opens one file, reads it, and
    holds a dict. It never probes the robot, never talks to the panel, and never
    decides anything — the main loop owns the single trip decision (see the
    module docstring). The worst a bug in here can do is arm a trip that all the
    existing gates then still get to veto.

    WHICH FILE: the fixed path dimos_cli.run_blueprint() redirects every launch's
    stdout+stderr into (dimos_cli._LAST_LAUNCH_LOG), which is also what
    dimos_cli.status() reports as "stdout_log". We take the constant rather than
    calling status() from this thread on purpose: status() SELF-HEALS the panel's
    tracked-run file (it unlinks it when the tracked PID is gone), and a
    monitoring thread must not mutate the state of the thing it is monitoring.
    --sentinel-log overrides it if the convention ever changes.

    ROTATION: that path is opened with mode "wb" on every launch, i.e. truncated
    in place (same inode, size back to 0) — and could be replaced outright by a
    future logrotate. Both are detected and handled by reopening.
    """

    def __init__(self, path: str, stop_evt: threading.Event, ev: EventSink,
                 poll_s: float = SENTINEL_POLL_S):
        super().__init__(name="sentinel-tailer", daemon=True)
        self.path = path
        self.stop_evt = stop_evt
        self.ev = ev
        self.poll_s = poll_s
        self.seen = 0                       # lifetime hit count, logging only
        self._lock = threading.Lock()
        self._pending = None                # newest unconsumed detection
        self._fh = None                     # binary handle, or None
        self._fid = None                    # (st_dev, st_ino) of self._fh

    # -- consumed by the main loop ----------------------------------------- #
    def take(self):
        """Pop the pending detection (or None). One call = at most one trip
        arming, no matter how many sentinel lines piled up behind it."""
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    # -- internals ---------------------------------------------------------- #
    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _ensure_open(self) -> bool:
        """Open (or reopen) the log. Returns False if it isn't there yet — an
        idle system that has never launched anything is normal, not an error."""
        try:
            st = os.stat(self.path)
        except OSError:
            self._close()
            return False

        fid = (st.st_dev, st.st_ino)
        if self._fh is not None and (fid != self._fid or st.st_size < self._fh.tell()):
            self._close()  # truncated by a relaunch, or replaced — reread from 0

        if self._fh is None:
            # Cold start (we have never had this file open) vs. a reopen after
            # rotation. On a COLD start seek to the end: this is a fixed path
            # carrying the previous run's output, which may well contain a
            # sentinel from an incident that was already dealt with — replaying
            # it would trip the watchdog on ancient history. After a rotation
            # every byte is new by construction, so we read from the top.
            # (The cost of seeking to the end is that if the log did not exist
            # yet when we started, we skip the sub-second of output written
            # before our first poll saw it. Nothing there can be a sentinel:
            # both require an already-established session — 6s of unanswered
            # heartbeats, or a track that was running and stopped.)
            cold_start = self._fid is None
            try:
                fh = open(self.path, "rb")
            except OSError as e:
                _log(f"WARN: sentinel tail could not open {self.path}: {e!r}")
                return False
            if cold_start:
                fh.seek(0, os.SEEK_END)
            self._fh, self._fid = fh, fid

        return True

    def _drain(self) -> None:
        """Read whatever whole lines have appeared since last time."""
        while True:
            pos = self._fh.tell()
            raw = self._fh.readline()
            if not raw:
                return
            if not raw.endswith(b"\n"):
                # The writer is mid-line. Rewind and pick it up next poll —
                # matching half a line could split a sentinel in two.
                self._fh.seek(pos)
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            for sentinel in SENTINEL_STRINGS:
                if sentinel in line:
                    self._record(sentinel, line)
                    break

    def _record(self, sentinel: str, line: str) -> None:
        text = line.strip()[:400]
        with self._lock:
            self.seen += 1
            first = self._pending is None
            if first:
                self._pending = {"sentinel": sentinel, "line": text, "count": 1,
                                 "at": time.monotonic()}
            else:
                # Coalesce. One death can print BOTH sentinels (the heartbeat
                # dies, then the track raises) and the main loop consumes at
                # most one detection per poll anyway. Keep the first line — the
                # one that actually detected it — name every distinct sentinel,
                # and just count the rest. One death is one trip.
                self._pending["count"] += 1
                if sentinel not in self._pending["sentinel"]:
                    self._pending["sentinel"] += "+" + sentinel
        if first:
            _log(f"SENTINEL {sentinel} in {self.path}: {text[:200]}")
            self.ev.emit("sentinel_seen", sentinel=sentinel, line=text,
                         log=self.path, lifetime_count=self.seen)

    def run(self) -> None:
        _log(f"sentinel tail: watching {self.path} for "
             f"{' / '.join(SENTINEL_STRINGS)} (every {self.poll_s:g}s)")
        while not self.stop_evt.is_set():
            try:
                if self._ensure_open():
                    self._drain()
            except OSError as e:
                # A transient read error must never kill the fast path, and must
                # never take the health-poll backstop down with it. Drop the
                # handle and try again next poll.
                _log(f"WARN: sentinel tail read error on {self.path}: {e!r}")
                self._close()
            self.stop_evt.wait(self.poll_s)
        self._close()


# --------------------------------------------------------------------------- #
# Robot re-discovery
# --------------------------------------------------------------------------- #
def resolve_robot_ip(known_mac, tracked_ip, timeout, ev: EventSink):
    """Actively re-discover the robot on the LAN and decide, WITHOUT GUESSING,
    which discovered record is ours.

    Returns (ip_or_None, mac_or_None, reason_string). A None ip means "abort the
    recovery and log it" — never fall back to a guess. Relaunching against the
    wrong device's IP is worse than not relaunching: it burns the retry budget,
    leaves the panel in a confused state, and hides the real failure.
    """
    try:
        robots = dimos_cli.discover(timeout=timeout, lan_only=True)
    except Exception as e:  # noqa: BLE001
        return None, None, f"discovery raised {e!r}"

    ev.emit("discover_result", count=len(robots), robots=robots)

    if not robots:
        return None, None, ("discovery found ZERO robots on the LAN — the robot is "
                            "probably powered off, still booting, or on another "
                            "network. NOT guessing an IP; aborting recovery.")

    if len(robots) == 1:
        r = robots[0]
        if not r.get("ip"):
            return None, None, (f"discovery returned one robot but with NO parseable IP "
                                f"({r!r}) — nothing to relaunch against; aborting.")
        return r.get("ip"), (r.get("mac") or None), f"single robot discovered: {r}"

    # >1 candidate: disambiguate only on hard evidence, else abort.
    listing = "; ".join(
        f"{r.get('name') or '?'} ip={r.get('ip')} mac={r.get('mac')} sn={r.get('serial')}"
        for r in robots)
    _log(f"WARN: discovery returned {len(robots)} candidates: {listing}")

    if known_mac:
        m = (known_mac or "").strip().lower()
        hits = [r for r in robots if (r.get("mac") or "").lower() == m]
        if len(hits) == 1:
            return hits[0].get("ip"), hits[0].get("mac"), \
                f"disambiguated {len(robots)} candidates by known MAC {known_mac}"

    if tracked_ip:
        hits = [r for r in robots if r.get("ip") == tracked_ip]
        if len(hits) == 1:
            return hits[0].get("ip"), hits[0].get("mac"), \
                (f"disambiguated {len(robots)} candidates by the still-valid tracked "
                 f"IP {tracked_ip} (so the IP did NOT change this time)")

    return None, None, (f"discovery returned {len(robots)} candidates and none could be "
                        f"disambiguated (no known MAC match, no tracked-IP match). "
                        f"Candidates: {listing}. NOT guessing; aborting recovery. "
                        f"Re-run with --robot-mac <mac> to make this decidable.")


# --------------------------------------------------------------------------- #
# Recovery (the ONLY side-effecting call in this file)
# --------------------------------------------------------------------------- #
def fire_recovery(panel: str, blueprint: str, robot_ip: str, transport,
                  max_attempts: int) -> dict:
    """POST /api/run-with-retry with the FRESH ip.

    Explicitly NOT /api/restart: restart() relaunches with the OLD tracked
    robot_ip, which is exactly the address that stopped working when the DHCP
    lease changed. force=true is REQUIRED here — the whole point of this
    failure mode is that /api/status still says running:true (process liveness),
    so the endpoint would 409 without it.

    This re-establishes the sensor/telemetry link ONLY. It does not, and must
    not, resume any movement or navigation goal.
    """
    form = {
        "blueprint": blueprint,
        "robot_ip": robot_ip,
        "force": "true",
        "max_attempts": str(max_attempts),
    }
    if transport:
        form["transport"] = str(transport)

    data = urllib.parse.urlencode(form).encode("utf-8")
    url = panel.rstrip("/") + "/api/run-with-retry"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    # run_blueprint_with_retry watches an 18s stabilize window per attempt plus
    # backoff, so this blocks for a long time. Budget generously — a client-side
    # timeout here does NOT cancel the server-side relaunch, it just blinds us
    # to the outcome.
    timeout = max_attempts * 30.0 + 60.0

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return {"ok": False, "http_status": e.code, "body": body,
                "elapsed_s": round(time.monotonic() - t0, 1),
                "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "http_status": None, "body": "",
                "elapsed_s": round(time.monotonic() - t0, 1),
                "error": f"{e!r} (the panel may still be relaunching server-side)"}

    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    return {
        "ok": bool(parsed.get("ok")) if isinstance(parsed, dict) else (code == 200),
        "http_status": code,
        "body": parsed if parsed is not None else body[:2000],
        "elapsed_s": round(time.monotonic() - t0, 1),
        "error": None,
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Detect a silent WebRTC session death (camera AND lidar stale "
                    "together) and recover with a freshly re-discovered robot IP.")
    ap.add_argument("--panel", default=DEFAULT_PANEL,
                    help=f"control-panel base URL (default {DEFAULT_PANEL})")
    ap.add_argument("--daemon-host", default="127.0.0.1",
                    help="host the camera/lidar daemons' /health are reachable on")
    ap.add_argument("--camera-port", type=int, default=dimos_cli.CAMERA_STREAM_PORT,
                    help="camera daemon health port (default: dimos_cli.CAMERA_STREAM_PORT)")
    ap.add_argument("--lidar-port", type=int, default=dimos_cli.LIDAR_STREAM_PORT,
                    help="lidar daemon health port (default: dimos_cli.LIDAR_STREAM_PORT)")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL_S,
                    help=f"seconds between health polls (default {POLL_INTERVAL_S:g})")
    ap.add_argument("--trip-after", type=float, default=TRIP_AFTER_S,
                    help="seconds BOTH must be stale continuously before tripping "
                         f"(default {TRIP_AFTER_S:g})")
    ap.add_argument("--startup-grace", type=float, default=STARTUP_GRACE_S,
                    help="ignore runs younger than this; daemons are still warming "
                         f"(default {STARTUP_GRACE_S:g})")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_S,
                    help=f"post-recovery quiet period (default {COOLDOWN_S:g})")
    ap.add_argument("--abort-cooldown", type=float, default=ABORT_COOLDOWN_S,
                    help="quiet period after an ABORTED recovery, e.g. discovery "
                         f"found nothing (default {ABORT_COOLDOWN_S:g})")
    ap.add_argument("--max-cooldown", type=float, default=MAX_COOLDOWN_S,
                    help="ceiling for the exponential cooldown backoff "
                         f"(default {MAX_COOLDOWN_S:g})")
    ap.add_argument("--healthy-reset", type=float, default=HEALTHY_RESET_S,
                    help="continuous health for this long clears the backoff "
                         f"escalation (default {HEALTHY_RESET_S:g})")
    ap.add_argument("--max-recoveries", type=int, default=MAX_RECOVERIES,
                    help="go dormant after this many recoveries IN A ROW that never "
                         "produced a --healthy-reset-long healthy stretch; 0 = "
                         f"unlimited (default {MAX_RECOVERIES})")
    ap.add_argument("--recovery-attempts", type=int, default=RECOVERY_MAX_ATTEMPTS,
                    help="max_attempts passed to /api/run-with-retry "
                         f"(default {RECOVERY_MAX_ATTEMPTS})")
    ap.add_argument("--discover-timeout", type=float, default=DISCOVER_TIMEOUT_S,
                    help=f"LAN discovery probe window (default {DISCOVER_TIMEOUT_S:g})")
    ap.add_argument("--robot-mac", default=None,
                    help="this robot's MAC — makes a multi-robot LAN decidable "
                         "instead of aborting")
    ap.add_argument("--sentinel-log", default=dimos_cli._LAST_LAUNCH_LOG,
                    help="blueprint stdout log to tail for the robot-side death "
                         "sentinels (GO2_WEBRTC_TRACK_DEAD / GO2_HEARTBEAT_DEAD); "
                         "a hit arms a trip immediately instead of waiting out "
                         f"--trip-after (default {dimos_cli._LAST_LAUNCH_LOG})")
    ap.add_argument("--no-sentinel-tail", action="store_true",
                    help="disable the fast sentinel path and detect only by "
                         "health polling. Use this if the robot-side logging "
                         "patches (backend/vendor_patches/) are not installed — "
                         "though leaving it on then is harmless, it just never "
                         "sees a sentinel.")
    ap.add_argument("--max-age", type=float, default=None,
                    help="optional stricter staleness bound in seconds; by default "
                         "we trust each daemon's own `fresh` flag")
    ap.add_argument("--event-log", default=None,
                    help="append the JSON event stream to this file as well as stdout")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect, re-discover and log the exact recovery call, but "
                         "do NOT POST it. Use this to validate detection by killing "
                         "the camera/lidar daemons, with no robot involved.")
    args = ap.parse_args()

    cam_url = f"http://{args.daemon_host}:{args.camera_port}/health"
    lid_url = f"http://{args.daemon_host}:{args.lidar_port}/health"
    status_url = args.panel.rstrip("/") + "/api/status"

    ev = EventSink(args.event_log)
    stop_evt = threading.Event()

    def _shutdown(_signum, _frame):
        stop_evt.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    mode = "DRY-RUN (will not POST recovery)" if args.dry_run else "ARMED"
    _banner([
        f"connection watchdog starting — {mode}",
        f"  camera health : {cam_url}",
        f"  lidar  health : {lid_url}",
        f"  panel         : {args.panel}",
        f"  trip when BOTH stale/unreachable continuously for {args.trip_after:g}s "
        f"(polling every {args.interval:g}s)",
        "  ...or IMMEDIATELY on a robot-side death sentinel: "
        + (f"tailing {args.sentinel_log} for {' / '.join(SENTINEL_STRINGS)}"
           if not args.no_sentinel_tail
           else "DISABLED (--no-sentinel-tail); health polling only"),
        f"  on trip: re-discover robot IP, then POST /api/run-with-retry "
        f"(force=true, max_attempts={args.recovery_attempts})",
        f"  cooldown {args.cooldown:g}s after recovery, {args.abort_cooldown:g}s after an abort; "
        f"max {args.max_recoveries or 'unlimited'} recoveries "
        f"(both detection paths share this one set of gates)",
        "  SCOPE: re-establishes the sensor link ONLY — never resumes a movement "
        "or nav goal.",
    ])
    ev.emit("armed", dry_run=args.dry_run, camera_url=cam_url, lidar_url=lid_url,
            panel=args.panel, interval=args.interval, trip_after=args.trip_after,
            cooldown=args.cooldown, max_recoveries=args.max_recoveries,
            sentinel_log=(None if args.no_sentinel_tail else args.sentinel_log))

    # The fast path shares the main loop's stop event, so SIGTERM/SIGINT stops
    # both. Daemon thread + a bounded join at the end: a wedged tail can never
    # hold up shutdown.
    tailer = None
    if not args.no_sentinel_tail:
        tailer = SentinelTailer(args.sentinel_log, stop_evt, ev)
        tailer.start()

    both_bad_since = None      # monotonic ts when BOTH first went bad, else None
    cooldown_until = 0.0       # monotonic ts before which we refuse to trip
    recoveries = 0             # total recoveries fired this session (logging only)
    unproven = 0               # recoveries not yet "proven good" by a healthy stretch;
                               # this (not the lifetime total) is what --max-recoveries
                               # bounds, so widely-spaced legitimate recoveries over a
                               # long session never push us dormant — only a link that
                               # keeps dying right back does.
    escalation = 0             # consecutive recoveries -> exponential cooldown
    last_recovery_ts = None    # monotonic ts of the last fired recovery
    dormant = False            # tripped --max-recoveries; log only from here on
    known_mac = args.robot_mac
    last_skip_log = 0.0        # throttle for repeated "can't trip because..." lines
    last_heartbeat = 0.0
    trigger = None             # what armed the currently-pending trip: "health"
                               # (the slow inference), or "sentinel..." (the robot
                               # said so itself). Purely so the trip log names its
                               # own cause; it does not change any decision. Reset
                               # everywhere both_bad_since is.

    def skip_log(msg: str, every: float = 60.0) -> None:
        nonlocal last_skip_log
        now = time.monotonic()
        if now - last_skip_log >= every:
            last_skip_log = now
            _log(msg)

    while not stop_evt.is_set():
        loop_start = time.monotonic()
        now = loop_start

        cam = probe_health(cam_url, HEALTH_TIMEOUT_S, args.max_age)
        lid = probe_health(lid_url, HEALTH_TIMEOUT_S, args.max_age)
        both_bad = cam["state"] != S_OK and lid["state"] != S_OK
        health_bad = both_bad  # remember what the SLOW path alone concluded

        # Low-volume heartbeat so the log proves the watchdog is alive.
        if now - last_heartbeat >= 120.0:
            last_heartbeat = now
            _log(f"alive — {_describe('camera', cam)}, {_describe('lidar', lid)}"
                 + (f", {recoveries} recover(ies) so far" if recoveries else ""))

        # ------------------------------------------------------------------ #
        # FAST PATH: the robot side told us it died. Arm the SAME trip decision
        # the health poll arms, with the sustained-staleness requirement already
        # considered satisfied — a MediaStreamError or 6s of unanswered
        # heartbeats is not a blip to wait out, it is ground truth. Everything
        # after this point (dormancy, cooldown, panel reachable, run active,
        # startup grace, blueprint known) is unchanged and still gets to veto.
        # ------------------------------------------------------------------ #
        sentinel = tailer.take() if tailer is not None else None
        if sentinel is not None:
            armed_at = now - args.trip_after   # backdate: threshold already met
            both_bad_since = (armed_at if both_bad_since is None
                              else min(both_bad_since, armed_at))
            both_bad = True
            trigger = sentinel["sentinel"] + ("+health" if health_bad else "")
            _banner([
                f"FAST-PATH DETECTION — robot-side sentinel {sentinel['sentinel']} "
                f"in {args.sentinel_log}",
                f"  {sentinel['line']}",
                (f"  ({sentinel['count']} sentinel lines coalesced into this one "
                 f"detection)" if sentinel["count"] > 1 else
                 "  (first sentinel line of this episode)"),
                f"  health poll currently says: {_describe('camera', cam)}, "
                f"{_describe('lidar', lid)}"
                + ("" if health_bad else " — i.e. the daemons have NOT gone stale "
                                         "yet; we are ahead of the slow path by "
                                         f"up to {args.trip_after:g}s"),
                "  arming the normal trip decision now (all the usual gates still "
                "apply).",
            ])
            ev.emit("sentinel_armed", sentinel=sentinel["sentinel"],
                    line=sentinel["line"], coalesced=sentinel["count"],
                    health_also_bad=health_bad, camera=cam, lidar=lid)

        if not both_bad:
            if both_bad_since is not None:
                held = now - both_bad_since
                if trigger and trigger != "health":
                    # A sentinel armed a trip that then got vetoed by a gate
                    # (cooldown/dormancy/no-run), and the health poll never
                    # agreed anything was wrong. Drop it rather than carry it:
                    # if the link really is dead the daemons go stale within
                    # --trip-after anyway and the slow path re-arms it, and if
                    # they don't, the sentinel was about something that already
                    # healed. Never trip on a stale arming.
                    _log(f"CLEARED: sentinel arming ({trigger}) dropped after "
                         f"{held:.1f}s — it never got past the trip gates and both "
                         f"daemons are healthy: {_describe('camera', cam)}, "
                         f"{_describe('lidar', lid)}")
                else:
                    _log(f"CLEARED: dual staleness recovered on its own after {held:.1f}s "
                         f"(never reached the {args.trip_after:g}s trip threshold) — "
                         f"{_describe('camera', cam)}, {_describe('lidar', lid)}")
                ev.emit("both_stale_cleared", held_s=round(held, 1),
                        trigger=trigger, camera=cam, lidar=lid)
                both_bad_since = None
                trigger = None
            # A long healthy stretch after a recovery means the link genuinely
            # came back; drop the exponential-backoff escalation.
            if ((escalation or unproven) and last_recovery_ts is not None
                    and now - last_recovery_ts >= args.healthy_reset):
                _log(f"link healthy for {args.healthy_reset:g}s since the last recovery "
                     f"— that recovery is proven good; clearing cooldown escalation "
                     f"(was x{2 ** escalation}) and the unproven-recovery count "
                     f"(was {unproven})")
                escalation = 0
                unproven = 0
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        # --- BOTH bad ---
        if both_bad_since is None:
            both_bad_since = now
            trigger = "health"
            _log(f"BOTH sensor daemons unhealthy — {_describe('camera', cam)}, "
                 f"{_describe('lidar', lid)}. Watching; trips at "
                 f"{args.trip_after:g}s if sustained.")
            ev.emit("both_stale_start", camera=cam, lidar=lid)

        stale_for = now - both_bad_since
        if stale_for < args.trip_after:
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        # --- armed (sustained past the threshold, or a sentinel): pre-trip gates ---
        if dormant:
            skip_log(f"WOULD TRIP (armed by {trigger}, {stale_for:.0f}s) but the watchdog is "
                     f"DORMANT: {unproven} recoveries in a row failed to make the link "
                     f"stick (--max-recoveries {args.max_recoveries}). Human "
                     f"intervention needed — the link is not staying up.", every=120.0)
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        if now < cooldown_until:
            skip_log(f"armed by {trigger} ({stale_for:.0f}s) but still in post-recovery cooldown "
                     f"for another {cooldown_until - now:.0f}s — the connection may "
                     f"still be warming up; not re-tripping.", every=30.0)
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        st, st_err = _get_json(status_url, STATUS_TIMEOUT_S)
        if st is None:
            skip_log(f"armed by {trigger} ({stale_for:.0f}s) but the control panel itself is not "
                     f"answering {status_url} ({st_err}) — cannot recover through it. "
                     f"Is uvicorn up? Not tripping.")
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        if not st.get("running"):
            skip_log("armed, but the panel reports NO RUN active — that's just an "
                     "idle system, not a dead link (a sentinel here is leftover "
                     "output from a run that has since stopped). Watchdog stays "
                     "dormant until something is launched.")
            both_bad_since = None  # not an incident; don't accumulate toward a trip
            trigger = None
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        uptime = st.get("uptime_s")
        if isinstance(uptime, (int, float)) and uptime < args.startup_grace:
            skip_log(f"armed by {trigger} but the run is only {uptime:.0f}s old (< "
                     f"{args.startup_grace:g}s startup grace) — daemons are still "
                     f"warming up, not tripping.", every=20.0)
            # Restart the staleness clock while in grace, so the trip threshold is
            # measured from the END of the grace window rather than from the launch
            # (otherwise a slow-but-normal startup would trip the instant grace
            # expires, having "accumulated" staleness that was never a fault).
            # A sentinel-only arming is discarded outright rather than restarted:
            # there is no clock to restart, and a sentinel from a run this young
            # is most likely the tail of the PREVIOUS run's output (the stdout log
            # is a fixed path, truncated per launch — but we may have read the old
            # bytes moments before the truncation).
            both_bad_since = now if health_bad else None
            trigger = "health" if health_bad else None
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        blueprint = st.get("blueprint")
        if not blueprint:
            _log(f"WOULD TRIP but /api/status has no blueprint to relaunch: {st!r}. "
                 f"Aborting recovery.")
            ev.emit("trip_aborted", reason="no blueprint in status", status=st,
                    trigger=trigger)
            cooldown_until = now + args.abort_cooldown
            both_bad_since = None
            trigger = None
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        # ------------------------------------------------------------------ #
        # TRIPPED
        # ------------------------------------------------------------------ #
        tracked_ip = st.get("robot_ip")
        transport = st.get("transport")
        _banner([
            "*** WATCHDOG TRIPPED — SILENT CONNECTION DEATH DETECTED ***",
            f"  armed by : {trigger}"
            + ("  (FAST path — the robot side reported its own death; the health "
               "poll is the slower backstop)" if trigger and trigger != "health"
               else "  (health polling — the original, slower backstop)"),
            f"  camera : {cam['state']} — {cam['detail']}  {cam['extra'] or ''}",
            f"  lidar  : {lid['state']} — {lid['detail']}  {lid['extra'] or ''}",
            (f"  both unhealthy continuously for {stale_for:.1f}s "
             f"(threshold {args.trip_after:g}s)" if trigger == "health" else
             "  sentinel arming counts as the staleness threshold already met — "
             "no waiting"),
            f"  meanwhile /api/status still reports running=True, pid={st.get('pid')}, "
            f"uptime={st.get('uptime')} — process liveness, NOT data liveness.",
            f"  tracked run: blueprint={blueprint} robot_ip={tracked_ip} "
            f"transport={transport}",
            "  re-discovering the robot's CURRENT IP on the LAN "
            "(the tracked IP may be stale after a DHCP lease change)...",
        ])
        ev.emit("tripped", trigger=trigger, stale_for_s=round(stale_for, 1),
                camera=cam, lidar=lid, status=st, threshold_s=args.trip_after)

        fresh_ip, found_mac, reason = resolve_robot_ip(
            known_mac, tracked_ip, args.discover_timeout, ev)

        if not fresh_ip:
            _banner([
                "RECOVERY ABORTED — could not determine the robot's IP.",
                f"  {reason}",
                f"  Nothing was relaunched. Retrying discovery after "
                f"{args.abort_cooldown:g}s.",
            ])
            ev.emit("recovery_aborted", reason=reason, trigger=trigger)
            cooldown_until = time.monotonic() + args.abort_cooldown
            both_bad_since = None
            trigger = None
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        if found_mac and not known_mac:
            known_mac = found_mac  # learn it, so a later multi-candidate scan is decidable
            _log(f"learned robot MAC {known_mac} from this discovery")

        changed = " (CHANGED — this is why a plain restart would have failed)" \
            if tracked_ip and fresh_ip != tracked_ip else " (unchanged)"
        _log(f"re-discovered robot IP: {fresh_ip}{changed}  [{reason}]")

        if args.dry_run:
            _banner([
                "DRY-RUN — not sending the recovery. Would have POSTed:",
                f"  POST {args.panel.rstrip('/')}/api/run-with-retry",
                f"    blueprint={blueprint} robot_ip={fresh_ip} "
                f"transport={transport} force=true "
                f"max_attempts={args.recovery_attempts}",
                f"  (explicitly NOT /api/restart, which would reuse the stale IP "
                f"{tracked_ip})",
                "  No movement/goal command is or would be sent.",
            ])
            ev.emit("recovery_dry_run", blueprint=blueprint, robot_ip=fresh_ip,
                    transport=transport, stale_tracked_ip=tracked_ip,
                    trigger=trigger)
            cooldown_until = time.monotonic() + args.cooldown
            both_bad_since = None
            trigger = None
            _sleep_rest(stop_evt, loop_start, args.interval)
            continue

        _banner([
            "FIRING RECOVERY — re-establishing the sensor link (no goals resumed):",
            f"  POST {args.panel.rstrip('/')}/api/run-with-retry",
            f"    blueprint={blueprint} robot_ip={fresh_ip} transport={transport} "
            f"force=true max_attempts={args.recovery_attempts}",
            f"  (NOT /api/restart — that reuses the tracked IP {tracked_ip})",
            "  this blocks while the panel stops the dead run and watches the new "
            "one survive its stabilize window...",
        ])
        ev.emit("recovery_firing", blueprint=blueprint, robot_ip=fresh_ip,
                transport=transport, stale_tracked_ip=tracked_ip,
                max_attempts=args.recovery_attempts, trigger=trigger)

        result = fire_recovery(args.panel, str(blueprint), str(fresh_ip), transport,
                               args.recovery_attempts)
        recoveries += 1
        unproven += 1
        last_recovery_ts = time.monotonic()

        cool = min(args.cooldown * (2 ** escalation), args.max_cooldown)
        escalation += 1
        cooldown_until = last_recovery_ts + cool
        both_bad_since = None
        fired_by, trigger = trigger, None
        # The relaunch truncates the stdout log, so any sentinel lines from the
        # run we just replaced are gone with it — but drain the tailer anyway so
        # a hit that landed WHILE the (long, blocking) recovery ran cannot
        # instantly re-arm on the corpse of the old session. The cooldown gate
        # would have vetoed it regardless; this just keeps the log honest.
        if tailer is not None:
            tailer.take()

        verdict = "SUCCEEDED" if result.get("ok") else "FAILED"
        _banner([
            f"RECOVERY {verdict} after {result.get('elapsed_s')}s "
            f"(recovery #{recoveries}, armed by {fired_by})",
            f"  http_status={result.get('http_status')} error={result.get('error')}",
            f"  response: {json.dumps(result.get('body'), default=str)[:900]}",
            f"  relaunched: blueprint={blueprint} on FRESH ip {fresh_ip} "
            f"(was {tracked_ip})",
            f"  cooling down for {cool:.0f}s before this can trip again.",
            "  REMINDER: only the data link was restored. Any interrupted "
            "movement/nav goal was NOT resumed — re-issue it yourself if you want it.",
        ])
        ev.emit("recovery_result", ok=bool(result.get("ok")), recovery_num=recoveries,
                trigger=fired_by,
                http_status=result.get("http_status"), error=result.get("error"),
                elapsed_s=result.get("elapsed_s"), body=result.get("body"),
                blueprint=blueprint, robot_ip=fresh_ip, stale_tracked_ip=tracked_ip,
                cooldown_s=round(cool, 1))

        if args.max_recoveries and unproven >= args.max_recoveries:
            dormant = True
            _banner([
                f"DORMANT — {unproven} recoveries in a row without the link ever "
                f"staying healthy for {args.healthy_reset:g}s "
                f"(--max-recoveries {args.max_recoveries}; {recoveries} total this session).",
                "  The link is not staying up; auto-recovery is now disabled to avoid "
                "storming it.",
                "  Detection continues and will keep logging, but nothing further "
                "will be relaunched. A human should look at this.",
            ])
            ev.emit("dormant", recoveries=recoveries)

        _sleep_rest(stop_evt, loop_start, args.interval)

    # stop_evt is already set (that is what ended the loop), so the tailer is
    # on its way out too; bound the wait so a stuck read can't hold up exit —
    # it is a daemon thread, the process may leave it behind.
    if tailer is not None:
        tailer.join(timeout=SENTINEL_POLL_S * 2)

    _log("connection watchdog stopped (signal).")
    ev.emit("stopped", recoveries=recoveries,
            sentinels_seen=(tailer.seen if tailer is not None else None))
    return 0


def _sleep_rest(stop_evt: threading.Event, loop_start: float, interval: float) -> None:
    """Pace the loop to `interval` between poll starts (same pattern the sibling
    daemons use), staying interruptible by SIGTERM/SIGINT."""
    elapsed = time.monotonic() - loop_start
    if elapsed < interval:
        stop_evt.wait(interval - elapsed)


if __name__ == "__main__":
    sys.exit(main())

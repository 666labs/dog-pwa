#!/usr/bin/env python3
"""Forensic network-evidence collector for the silent WebRTC death.
COLLECTS ONLY — never recovers, never commands the robot.

WHY THIS EXISTS, AND WHY IT IS NOT THE WATCHDOG
-----------------------------------------------
`connection_watchdog.py` (read its docstring first — it has the full root-cause
chain) DETECTS the silent death and RECOVERS from it. It is a production trip
detector: slow, conservative, deliberately hard to fire.

This file is the opposite tool for the opposite job. A teammate is about to walk
the Go2 back to the exact spot where the link died on 2026-07-24, ON PURPOSE, so
that this time there is enough network-level evidence on disk to root-cause it.
So this process is fast-polling, records EVERYTHING, decides NOTHING, and fixes
NOTHING. Its only product is one merged, time-ordered JSONL timeline, so the
post-mortem is "read one file sorted by time" instead of hand-correlating five
logs with five different clocks and formats.

TWO WAYS THIS RUNS (neither is more "real" than the other)
----------------------------------------------------------
  1. HAND-LAUNCHED in a terminal for a deliberate reproduction walk, with a
     human watching the stderr stream and typing markers into stdin.
  2. AS AN ALWAYS-ON SIDECAR, spawned headless by dimos_cli._DiagDaemon the
     moment a blueprint is launched through the control panel and killed when
     that run stops. Nobody is at a terminal, stdin is /dev/null and stderr is
     discarded — so the JSONL file is the ONLY product, and markers arrive by
     appended line via --marker-file (POST /api/diagnostics/marker) instead of
     by keystroke.
Do not reintroduce an assumption that a human is attached: mode 2 is the
default one now, and the incident we most want on disk is the one nobody was
expecting. Everything that must survive the run goes in the timeline file;
stderr is a convenience for mode 1 only.

The two are INDEPENDENT. Do not couple them, and do not assume the watchdog is
running during a collection run — for capturing the natural incident cleanly you
probably want the watchdog OFF, so the incident is allowed to just... sit there
and be observed, instead of being recovered out from under you after 18s.

WHAT IS ACTUALLY UNKNOWN (this is what the run is meant to settle)
-----------------------------------------------------------------
Known: `/api/status` is process liveness only; the heartbeat is written and never
read; ICE/peer state-change handlers only `print()`; and after a previous
power-cycle the robot came back on a DIFFERENT IP (DHCP lease change).

NOT known, and deliberately NOT assumed either way by this tool:
  Q1. Is the IP change caused by the POWER-CYCLE specifically, or would a plain
      WiFi roam / reassociation with no power-cycle also move it? The `discover`
      loop runs THE WHOLE TIME (not just after a trip) precisely to catch a live
      IP change with no power-cycle in between. A discovered IP that diverges
      from the tracked `robot_ip` WHILE `/api/status` still says running:true is
      direct evidence for the roam hypothesis.
  Q2. Does the venue's multi-AP-same-SSID roaming (already suspected for a
      teammate's iPad on this network) touch the ROBOT↔HOST leg at all? The
      robot↔host leg was never previously suspected — that is an assumption
      being tested here, not a fact. The `wifi` loop watches the HOST's own
      BSSID so a host-side roam can be told apart from a robot-side one; a
      BSSID flip on our end at the moment of the incident means we were looking
      at the wrong leg the whole time.
  Q3. What does the transport do in the seconds either side of the staleness?
      Does ICMP stop first (link-level), or does /health go stale first while
      ping is still fine (session-level, i.e. aiortc gave up while the network
      was healthy)? THIS ORDERING IS THE WHOLE BALLGAME — it decides whether
      this is a WiFi problem or a WebRTC problem. Hence ping at ~1s and health
      at ~1.5s: fine enough to resolve the ordering, which a 6s watchdog poll
      cannot.

SOURCES MERGED INTO THE ONE TIMELINE
------------------------------------
  health       camera_daemon + lidar_daemon /health (reused probe_health)
  ping         one-shot ICMP to the tracked robot IP, ~1s
  neigh        `ip neigh show <ip>` — ARP state + MAC, ~5s AND edge-triggered
  discover     `dimos go2tool discover --lan`, ~40s, running continuously
  stdout_tail  every new line of the running blueprint's own stdout log,
               VERBATIM AND UNFILTERED (this is where the otherwise-silent
               ICE/peer-state print()s land)
  wifi         host-side iwconfig BSSID/freq/quality, ~10s and on change
  status       the tracked run (robot_ip / running / pid) — refreshed every
               cycle because it does NOT self-update when the link dies without
               a relaunch, so a robot_ip that goes stale IS ITSELF the signal
  marker       a human annotation, verbatim ("at the corner now") — typed into
               stdin (attached terminal) and/or appended to --marker-file
               (headless sidecar). Both channels emit the SAME event.
  collector    this process's own lifecycle, per-probe errors, and alerts

=============================================================================
SAFETY SCOPE — READ BEFORE EDITING
This file is READ-ONLY WITH RESPECT TO THE ROBOT and must stay that way.
It may: GET localhost daemon /health, send ICMP echo, read the kernel ARP
table, run the existing read-only LAN discovery, read a log file, and read
local WiFi interface state. It may NOT: call any RPC, publish any topic,
send teleop/sport/nav/gesture commands, POST any panel endpoint, or start,
stop, restart or "wake up" anything. There is deliberately no recovery path
and no outbound side-effecting call in this file. If you are tempted to add
one "just to nudge the stream back", add it to connection_watchdog.py
instead — that is what that file is for.

Nor does it mutate PANEL state: note that it does NOT call dimos_cli.status(),
which LOOKS read-only but unlinks the tracked-run file when the PID is gone
(see _clear_tracked_run in status()). For a forensic tool that would both
mutate shared state and destroy the exact evidence — the dead run's robot_ip —
we are here to collect. We read the panel's HTTP /api/status instead, and fall
back to _load_tracked_run(), which is a pure read.
=============================================================================

HONEST CAVEATS (matter when reading the resulting timeline)
-----------------------------------------------------------
  * NOT A PASSIVE OBSERVER. This adds ~1 ICMP/s and a LAN discovery sweep every
    ~40s to the same air we are measuring. That is almost certainly negligible
    next to a video stream, but if you want to rule the collector itself out as
    a confounder, `--discover-interval 0` and `--ping-interval 0` disable those
    loops (0 disables ANY loop).
  * STDOUT-TAIL TIMESTAMPS ARE FLUSH TIMES, NOT PRINT TIMES. The blueprint's
    stdout goes to a plain file, so CPython block-buffers it (~4-8KB) unless
    that process is line-buffered or unbuffered. A burst of ICE lines can
    therefore surface in the timeline LATE and ALL AT ONCE, at the moment the
    buffer flushed, not when each was printed. Trust the ORDER of these lines
    absolutely; treat their timestamps as an upper bound. If a future run can
    be launched with PYTHONUNBUFFERED=1 in run_blueprint's env, this caveat
    goes away and the correlation gets much sharper.
  * The stdout log is a FIXED path overwritten per launch (`"wb"`, so it is
    truncated IN PLACE — same inode). A relaunch mid-collection therefore
    silently resets it; we detect the truncation and log it rather than
    skipping the new content.

OUTPUT
------
  --out FILE  the JSONL timeline: one event per line, EVERY sample kept. This
              is the artifact. Fields: t, iso, source, kind, + source-specific.
  stderr      the live human stream: changes, failures, markers, tail lines, a
              periodic rollup, and unmissable banners for the four moments you
              might need to shout "reproduced!" about. By default it does NOT
              echo every routine sample — at 1s ping + 1.5s health that is ~2
              lines/sec, which would bury the banners within seconds of them
              printing. `--stderr-echo all` gives you the full firehose;
              `--stderr-echo none` gives you banners only. The FILE always has
              everything regardless of this flag.
  stdout      ONE line: a `{"ready": true, ...}` JSON readiness handshake,
              printed the moment the timeline file is open — the same shape
              camera_daemon.py/arm_camera_daemon.py use, so a supervising
              manager can distinguish "collecting" from "died on startup".
              Otherwise quiet by default (so an attached terminal stays
              readable while you type markers into it). `--stdout-jsonl` also
              streams the timeline on stdout for piping into something live.

USAGE
-----
    ./venv/bin/python backend/network_diagnostics.py
    ./venv/bin/python backend/network_diagnostics.py --out /tmp/walk3.jsonl \
        --robot-mac 78:8c:b5:aa:bb:cc --ping-interval 0.5
    # then just type into the terminal whenever something happens:
    #   at the hallway corner now<Enter>
    #   video just froze on the tablet<Enter>

    # headless sidecar shape (what dimos_cli._DiagDaemon spawns for you):
    ./venv/bin/python backend/network_diagnostics.py \
        --out ~/dimos-network-diag/unitree-go2-basic-1753400000.jsonl \
        --marker-file ~/dimos-network-diag/unitree-go2-basic-1753400000.markers
    # markers then arrive by appending a line to that file, e.g. via
    #   POST /api/diagnostics/marker  (form field `text`)

SIGTERM or Ctrl-C to stop; it prints a summary of what it saw. Runs under the
ordinary control-panel venv python (no `import dimos` needed), same as the
watchdog — which is why the panel can spawn it with its own sys.executable.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dimos_cli  # noqa: E402  (stdlib-only wrapper; import is side-effect free)

# Reuse, don't reimplement. connection_watchdog already solved: how to probe a
# daemon's /health and classify the answer three ways, how to GET a JSON
# endpoint without letting six different socket failure modes escape as
# tracebacks, how to decide which discovered record is OUR robot without ever
# guessing, and what a loud stderr banner looks like. Importing it is
# side-effect free (everything is behind `if __name__ == "__main__"`), and in
# particular importing it does NOT arm a watchdog.
from connection_watchdog import (  # noqa: E402
    S_OK,
    S_STALE,          # noqa: F401  (re-exported for readers/analysis scripts)
    S_UNREACHABLE,    # noqa: F401
    _banner,
    _get_json,
    probe_health,
    resolve_robot_ip,
)

# --------------------------------------------------------------------------- #
# Defaults — every one is a CLI flag; 0 disables that loop entirely.
# --------------------------------------------------------------------------- #
DEFAULT_PANEL = "http://127.0.0.1:8090"

HEALTH_INTERVAL_S = 1.5      # forensics cadence, not the watchdog's 6s
PING_INTERVAL_S = 1.0        # cheapest, fastest "when did the link go quiet"
PING_TIMEOUT_S = 1.0         # per echo request
NEIGH_INTERVAL_S = 5.0       # plus edge-triggered on any state/MAC change
DISCOVER_INTERVAL_S = 40.0   # the expensive one; runs the WHOLE time (see Q1)
DISCOVER_TIMEOUT_S = 8.0     # `dimos go2tool discover --lan` probe window
WIFI_INTERVAL_S = 10.0       # host-side association snapshot
STATUS_INTERVAL_S = 3.0      # tracked-run refresh
TAIL_POLL_S = 0.5            # stdout log poll (no inotify needed)
MARKER_FILE_POLL_S = 0.5     # --marker-file poll; a marker's timestamp is only
                             # as good as this, and it costs one stat+read
ROLLUP_S = 20.0              # periodic "still alive, here's the gist" line

HEALTH_TIMEOUT_S = 3.0
STATUS_TIMEOUT_S = 5.0

# ARP/neighbour states ranked by how bad they are. Under a 1s ping the entry
# should sit at REACHABLE/DELAY forever, so anything at STALE or worse while we
# are actively pinging is real signal, not idle decay.
NEIGH_RANK = {
    "PERMANENT": 0, "NOARP": 0, "REACHABLE": 0,
    "DELAY": 1, "PROBE": 2,
    "STALE": 3, "UNKNOWN": 3,
    "NONE": 4, "INCOMPLETE": 4,
    "FAILED": 5,
    "ABSENT": 6,          # no kernel neighbour entry for this IP at all
}
NEIGH_DEGRADED_AT = 3     # rank >= this, worse than before => banner

_MAC_RE = re.compile(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
_PING_RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms")
_LINK_IFACE_RE = re.compile(r"^\d+:\s+([^:@\s]+)")

_IW_ESSID_RE = re.compile(r'ESSID:"([^"]*)"')
_IW_AP_RE = re.compile(r"Access Point:\s*([0-9A-Fa-f:]{17}|Not-Associated)")
_IW_FREQ_RE = re.compile(r"Frequency[:=]\s*([\d.]+)\s*GHz")
_IW_QUAL_RE = re.compile(r"Link Quality[=:]\s*(\d+)/(\d+)")
_IW_SIG_RE = re.compile(r"Signal level[=:]\s*(-?\d+)\s*dBm")
_IW_RATE_RE = re.compile(r"Bit Rate[=:]\s*([\d.]+)\s*(\S+)")
_IW_BEACON_RE = re.compile(r"Missed beacon:(\d+)")

# `iw` (nl80211, modern cfg80211 drivers) output — a DIFFERENT tool from the
# legacy `iwconfig` (WEXT ioctls) the regexes above parse. Confirmed live: on
# the Ascent's MediaTek Filogic (MT7925) card, `iwconfig <iface>` answers "no
# wireless extensions" — the driver never implemented the old WEXT API — so
# the WifiLoop this pattern originally supported silently found nothing to
# watch on exactly the box that needed it. `iw dev <iface> link` works fine
# there. Kept as separate regexes/parser (not merged into the ones above)
# because the two tools' output formats do not share a grammar.
_NL80211_BSSID_RE = re.compile(
    r"^Connected to ([0-9A-Fa-f:]{17})", re.MULTILINE)
_NL80211_SSID_RE = re.compile(r"^\s*SSID:\s*(.*)$", re.MULTILINE)
_NL80211_FREQ_RE = re.compile(r"^\s*freq:\s*([\d.]+)", re.MULTILINE)
_NL80211_SIGNAL_RE = re.compile(r"^\s*signal:\s*(-?\d+)\s*dBm", re.MULTILINE)
_NL80211_RXRATE_RE = re.compile(r"^\s*rx bitrate:\s*(.+)$", re.MULTILINE)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _which(name: str):
    """Resolve a binary, including the sbin dirs that are commonly off a
    non-root user's PATH (`ip` and `iwconfig` both live there on Debian)."""
    p = shutil.which(name)
    if p:
        return p
    for d in ("/sbin", "/usr/sbin", "/bin", "/usr/bin"):
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _short(s, n: int = 160) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n - 1] + "…"


# --------------------------------------------------------------------------- #
# The unified timeline: every source funnels through here, so the JSONL is
# already merged and already time-ordered and nobody has to correlate anything
# by hand afterwards.
# --------------------------------------------------------------------------- #
class Timeline:
    def __init__(self, path: str, echo: str = "interesting",
                 stdout_jsonl: bool = False):
        self.path = path
        self.echo = echo                  # all | interesting | none
        self.stdout_jsonl = stdout_jsonl
        self._lock = threading.RLock()
        self._fh = open(path, "a", buffering=1)   # line buffered + explicit flush
        self._closed = False
        self.counts = {}                  # source -> events written
        self.latest = {}                  # source -> most recent record (rollup)

    def emit(self, source: str, kind: str, notable: bool = False, **fields):
        """Write one event. `notable` only controls the LIVE stderr echo; the
        file always gets the event either way."""
        rec = {"t": time.time(), "iso": _ts(), "source": source, "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, default=str)
        with self._lock:
            # A blocking probe (discover can sit in a subprocess for ~20s) may
            # still land an event after we've closed up shop on Ctrl-C. Drop it
            # quietly rather than splattering a traceback over the summary the
            # human is trying to read.
            if self._closed:
                return rec
            self.counts[source] = self.counts.get(source, 0) + 1
            self.latest[source] = rec
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except (OSError, ValueError) as e:
                print(f"[{_ts()}] WARN: timeline write failed: {e!r}",
                      file=sys.stderr, flush=True)
            if self.stdout_jsonl:
                print(line, flush=True)
            if self.echo == "all" or (notable and self.echo != "none"):
                print(f"[{rec['iso']}] {_one_line(rec)}", file=sys.stderr, flush=True)
        return rec

    def alert(self, source: str, kind: str, lines, **fields):
        """A moment the human must not miss. Loud stderr banner (reusing the
        watchdog's exact banner shape so both tools look the same in a terminal)
        AND a first-class event in the timeline, so the alerts are greppable
        later instead of living only in scrollback."""
        with self._lock:
            _banner(lines)
        self.emit(source, kind, notable=False, alert=True, alert_lines=list(lines),
                  **fields)

    def note(self, msg: str, **fields):
        self.emit("collector", "note", notable=True, message=msg, **fields)

    def error(self, source: str, err: Exception, where: str = ""):
        self.emit(source, "error", notable=True, error=repr(err), where=where,
                  traceback=traceback.format_exc(limit=4))

    def close(self):
        with self._lock:
            self._closed = True
            try:
                self._fh.close()
            except OSError:
                pass


def _one_line(rec: dict) -> str:
    """Compact one-line rendering of any event, for the live stderr stream."""
    s, k = rec.get("source"), rec.get("kind")
    g = rec.get
    if s == "ping":
        if k == "sample":
            return (f"ping {g('ip')} ok {g('rtt_ms')}ms" if g("ok")
                    else f"ping {g('ip')} LOST ({_short(g('detail'), 60)})")
        if k == "loss_start":
            return f"ping {g('ip')} LOSS STARTED ({_short(g('detail'), 60)})"
        if k == "loss_end":
            return f"ping {g('ip')} replies resumed after {g('outage_s')}s of loss"
    elif s == "health":
        cam, lid = g("camera") or {}, g("lidar") or {}
        return (f"health camera={cam.get('state')} lidar={lid.get('state')}"
                + (f"  cam:{_short(cam.get('detail'), 50)}"
                   if cam.get("state") != S_OK else "")
                + (f"  lid:{_short(lid.get('detail'), 50)}"
                   if lid.get("state") != S_OK else ""))
    elif s == "neigh":
        return (f"neigh {g('ip')} state={g('state')} mac={g('mac')} dev={g('dev')}"
                + (" [CHANGED]" if k == "change" else ""))
    elif s == "discover":
        if k == "result":
            d = " DIVERGED" if g("diverged") else ""
            return (f"discover found={g('count')} resolved={g('resolved_ip')} "
                    f"tracked={g('tracked_ip')}{d}  [{_short(g('reason'), 90)}]")
        if k == "discover_result":
            return f"discover raw count={g('count')} {_short(g('robots'), 120)}"
    elif s == "stdout_tail":
        if k == "line":
            return f"log| {_short(g('line'), 200)}"
        return f"stdout_tail {k}: {_short(g('path'), 80)} {_short(g('detail'), 80)}"
    elif s == "wifi":
        return (f"wifi {g('iface')} bssid={g('bssid')} essid={g('essid')} "
                f"{g('freq_ghz')}GHz q={g('link_quality')} {g('signal_dbm')}dBm "
                f"missed_beacon={g('missed_beacon')}"
                + (" [CHANGED]" if k == "change" else ""))
    elif s == "status":
        return (f"status running={g('running')} robot_ip={g('robot_ip')} "
                f"pid={g('pid')} bp={g('blueprint')} via={g('via')}"
                + (f" err={_short(g('error'), 60)}" if g("error") else ""))
    elif s == "marker":
        return f">>> MARKER: {g('text')}"
    if k == "error":
        return f"{s} ERROR {_short(g('error'), 120)} @{g('where')}"
    return f"{s}/{k} " + _short({x: y for x, y in rec.items()
                                 if x not in ("t", "iso", "source", "kind")}, 160)


# --------------------------------------------------------------------------- #
# Tracked-run state, shared by every loop.
# --------------------------------------------------------------------------- #
class RobotRef:
    """The panel's view of the current run, refreshed continuously.

    Refreshed every cycle ON PURPOSE: `robot_ip` here is whatever the run was
    LAUNCHED with. It does not self-update when the link dies without a
    relaunch — so if the robot moves to a new IP mid-run, this value goes stale
    and stays stale, and THAT staleness is exactly the thing the discover loop
    is trying to catch it in the act of. Never "helpfully" repair it here."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {"running": False, "robot_ip": None, "pid": None,
                   "blueprint": None, "transport": None, "stdout_log": None,
                   "via": "none", "error": None}

    def get(self) -> dict:
        with self._lock:
            return dict(self._d)

    def set(self, d: dict) -> None:
        with self._lock:
            self._d = dict(d)


def read_status(panel: str, timeout: float) -> dict:
    """The tracked run, WITHOUT mutating anything.

    Primary source is the panel's HTTP /api/status (a thin wrapper over
    dimos_cli.status()). Fallback, when uvicorn is down, is
    dimos_cli._load_tracked_run() — a pure read of the same JSON file.

    We deliberately do NOT call dimos_cli.status() in-process: it unlinks the
    tracked-run file as a side effect once the PID is gone, which would both
    mutate the panel's state from a read-only forensic tool and delete the dead
    run's robot_ip — the single most valuable field in this whole timeline.
    """
    st, err = _get_json(panel.rstrip("/") + "/api/status", timeout)
    if isinstance(st, dict):
        return {"running": bool(st.get("running")), "robot_ip": st.get("robot_ip"),
                "pid": st.get("pid"), "blueprint": st.get("blueprint"),
                "transport": st.get("transport"), "stdout_log": st.get("stdout_log"),
                "uptime_s": st.get("uptime_s"), "via": "panel", "error": None}

    loader = getattr(dimos_cli, "_load_tracked_run", None)
    tracked = None
    if callable(loader):
        try:
            tracked = loader()
        except Exception as e:  # noqa: BLE001
            return {"running": False, "robot_ip": None, "pid": None,
                    "blueprint": None, "transport": None, "stdout_log": None,
                    "via": "none", "error": f"panel: {err}; tracked-file: {e!r}"}
    if not tracked:
        return {"running": False, "robot_ip": None, "pid": None, "blueprint": None,
                "transport": None, "stdout_log": None, "via": "none",
                "error": f"panel unreachable ({err}); no tracked run on disk"}

    pid = tracked.get("pid")
    # /proc existence only — process liveness, which is precisely the check that
    # is blind to this failure class. Recorded as such so nobody later mistakes
    # `running: true` from this path for "the data link is fine".
    alive = isinstance(pid, int) and os.path.exists(f"/proc/{pid}")
    return {"running": bool(alive), "robot_ip": tracked.get("robot_ip"), "pid": pid,
            "blueprint": tracked.get("blueprint"), "transport": tracked.get("transport"),
            "stdout_log": tracked.get("stdout_log"), "via": "tracked-file",
            "error": f"panel unreachable ({err}); read /tmp tracked-run file instead"}


# --------------------------------------------------------------------------- #
# Loop scaffolding
#
# One thread per source, each on its own cadence, because the cadences differ by
# ~80x and because discover() BLOCKS for up to timeout+15s inside a subprocess —
# a single interleaved loop would let that one probe freeze the 1s ping, which
# is the very signal the whole run is built around.
# --------------------------------------------------------------------------- #
class ProbeLoop(threading.Thread):
    source = "probe"

    def __init__(self, tl: Timeline, stop_evt: threading.Event, interval: float):
        super().__init__(name=f"{self.source}-loop", daemon=True)
        self.tl = tl
        self.stop = stop_evt
        self.interval = interval

    def step(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        # A failing probe is DATA, never a crash: the robot being off, the panel
        # being down and `ip` being missing are all things this run wants
        # recorded. So every iteration is individually contained; nothing here
        # may take the collector down with it.
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                self.tl.error(self.source, e, where=f"{self.source}.step")
                self.stop.wait(min(max(self.interval, 1.0), 5.0))  # no hot loop
            elapsed = time.monotonic() - t0
            if elapsed < self.interval:
                self.stop.wait(self.interval - elapsed)


class StatusLoop(ProbeLoop):
    source = "status"

    def __init__(self, tl, stop_evt, interval, panel, ref: RobotRef):
        super().__init__(tl, stop_evt, interval)
        self.panel = panel
        self.ref = ref
        self._prev_key = None

    def step(self):
        st = read_status(self.panel, STATUS_TIMEOUT_S)
        self.ref.set(st)
        key = (st["running"], st["robot_ip"], st["pid"], st["blueprint"], st["via"])
        changed = key != self._prev_key
        prev = self._prev_key
        self._prev_key = key
        self.tl.emit(self.source, "change" if changed else "sample",
                     notable=changed, **st)
        if changed and prev is not None:
            # A tracked robot_ip that changes without us doing anything means
            # someone relaunched; worth calling out so the timeline reader does
            # not attribute a post-relaunch IP to a live roam.
            if prev[1] != st["robot_ip"] or prev[2] != st["pid"]:
                self.tl.note(
                    f"tracked run CHANGED under us: robot_ip {prev[1]} -> "
                    f"{st['robot_ip']}, pid {prev[2]} -> {st['pid']} (something "
                    f"relaunched the blueprint; events before and after this line "
                    f"belong to different runs)")


class HealthLoop(ProbeLoop):
    source = "health"

    def __init__(self, tl, stop_evt, interval, cam_url, lid_url, max_age,
                 discover_loop=None):
        super().__init__(tl, stop_evt, interval)
        self.cam_url, self.lid_url, self.max_age = cam_url, lid_url, max_age
        # Optional DiscoverLoop to trigger an IMMEDIATE LAN scan on a fresh
        # both-stale trip (see DiscoverLoop.fire_now for why: the periodic
        # scan alone could leave "did the AP change" unanswered for up to
        # --discover-interval seconds after the exact moment that matters).
        # None when discovery is disabled (--discover-interval 0) — this loop
        # must keep working without it, just without the reactive scan.
        self.discover_loop = discover_loop
        self.prev = (None, None)
        self.flips = 0

    def step(self):
        cam = probe_health(self.cam_url, HEALTH_TIMEOUT_S, self.max_age)
        lid = probe_health(self.lid_url, HEALTH_TIMEOUT_S, self.max_age)
        cur = (cam["state"], lid["state"])
        both_stale = cam["state"] != S_OK and lid["state"] != S_OK
        changed = cur != self.prev and self.prev != (None, None)
        first = self.prev == (None, None)
        self.tl.emit(self.source, "flip" if changed else "sample",
                     notable=changed or first or both_stale,
                     camera=cam, lidar=lid, both_stale=both_stale)

        if changed:
            self.flips += 1
            prev_cam, prev_lid = self.prev
            went_bad = ((prev_cam == S_OK and cam["state"] != S_OK)
                        or (prev_lid == S_OK and lid["state"] != S_OK))
            if both_stale and (prev_cam == S_OK or prev_lid == S_OK):
                # Fire an immediate LAN scan right now, on top of (not instead
                # of) the periodic one — this is the exact moment the "did the
                # robot's AP/IP change" question needs answering, not up to
                # --discover-interval seconds later. Fire-and-forget: fire_now
                # spawns its own thread, so this never blocks the health poll.
                fired = bool(self.discover_loop and
                            self.discover_loop.fire_now("both_stale trip"))
                self.tl.alert(self.source, "alert_both_stale", [
                    "*** BOTH camera AND lidar just went non-OK — THIS IS THE "
                    "SILENT-DEATH SIGNATURE ***",
                    f"  camera: {cam['state']} — {cam['detail']}",
                    f"  lidar : {lid['state']} — {lid['detail']}",
                    "  Both daemons subscribe to the SAME upstream WebRTC session, "
                    "so only that session dying can freeze both at once.",
                    ("  Triggered an immediate LAN scan to check whether the "
                     "robot's IP/AP changed — result lands as its own "
                     "discover/result event within seconds."
                     if fired else
                     "  (no reactive LAN scan fired — discovery is disabled or "
                     "one was already in flight)"),
                    "  >>> NOTE THE TIME. Say 'reproduced' on comms. Do NOT restart "
                    "anything yet — let it sit so the timeline captures the whole "
                    "event, and type a marker describing exactly where you are.",
                ], camera=cam, lidar=lid, reactive_scan_fired=fired)
            elif went_bad:
                self.tl.alert(self.source, "alert_health_flip", [
                    f"health flipped OFF OK: camera={cam['state']} lidar={lid['state']}",
                    f"  camera: {cam['detail']}",
                    f"  lidar : {lid['detail']}",
                    "  (only ONE of them is a per-topic hiccup, not the shared "
                    "session dying — keep watching)",
                ], camera=cam, lidar=lid)
            elif cur == (S_OK, S_OK):
                self.tl.alert(self.source, "alert_health_recovered", [
                    "health RECOVERED — camera and lidar are both OK again "
                    "(nothing was restarted by this tool; it came back on its own "
                    "or someone else acted).",
                ], camera=cam, lidar=lid)
        self.prev = cur


class PingLoop(ProbeLoop):
    source = "ping"

    def __init__(self, tl, stop_evt, interval, ref: RobotRef, ping_bin, timeout):
        super().__init__(tl, stop_evt, interval)
        self.ref = ref
        self.ping_bin = ping_bin
        self.timeout = timeout
        self.prev_ok = None
        self.loss_since = None            # wall time the current outage began
        self._stats_lock = threading.Lock()
        self.stats = {"total": 0, "ok": 0, "fail": 0, "outages": 0,
                      "longest_outage_s": 0.0, "rtt_sum": 0.0, "rtt_n": 0}
        self.window = {"total": 0, "ok": 0, "rtt_sum": 0.0, "rtt_n": 0}

    def _sample(self, ip: str) -> dict:
        # One-shot per interval rather than a long-running `ping -i`: trivially
        # interruptible, no output-parsing state machine, and each sample is
        # independently timestamped by us at the moment we take it.
        cmd = [self.ping_bin, "-n", "-c", "1", "-W", f"{self.timeout:g}", ip]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout + 2.0)
        except subprocess.TimeoutExpired:
            return {"ok": False, "rtt_ms": None, "rc": None,
                    "detail": "ping binary exceeded its wall timeout"}
        except OSError as e:
            return {"ok": False, "rtt_ms": None, "rc": None,
                    "detail": f"could not run ping: {e!r}"}
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            m = _PING_RTT_RE.search(out)
            return {"ok": True, "rc": 0,
                    "rtt_ms": float(m.group(1)) if m else None,
                    "detail": None if m else "reply, but no parseable RTT"}
        # rc 1 = no reply within -W (or host-unreachable ICMP); rc 2 = usage /
        # resolution / permission error. Keep the text: "Destination Host
        # Unreachable" vs a silent timeout are different failures.
        interesting = [ln for ln in out.splitlines()
                       if ln.strip() and not ln.startswith("PING ")
                       and "packets transmitted" not in ln
                       and not ln.startswith("---")
                       and "rtt min" not in ln]
        return {"ok": False, "rtt_ms": None, "rc": p.returncode,
                "detail": _short(interesting[0] if interesting else "no reply", 200)}

    def step(self):
        ref = self.ref.get()
        ip = ref.get("robot_ip")
        if not ip:
            # No tracked IP is itself worth one line, but not 1/s forever.
            if self.prev_ok is not None or self.stats["total"] == 0:
                self.tl.emit(self.source, "no_target", notable=True,
                             detail="no tracked robot_ip to ping (nothing launched, "
                                    "or the panel and the tracked-run file are both "
                                    "unavailable)")
                self.prev_ok = None
            self.stop.wait(2.0)
            return

        r = self._sample(str(ip))
        now = time.time()
        with self._stats_lock:
            self.stats["total"] += 1
            self.window["total"] += 1
            if r["ok"]:
                self.stats["ok"] += 1
                self.window["ok"] += 1
                if r["rtt_ms"] is not None:
                    self.stats["rtt_sum"] += r["rtt_ms"]
                    self.stats["rtt_n"] += 1
                    self.window["rtt_sum"] += r["rtt_ms"]
                    self.window["rtt_n"] += 1
            else:
                self.stats["fail"] += 1

        self.tl.emit(self.source, "sample", notable=not r["ok"], ip=ip,
                     tracked_running=ref.get("running"), **r)

        if r["ok"] and self.prev_ok is False:
            outage = now - (self.loss_since or now)
            with self._stats_lock:
                self.stats["longest_outage_s"] = max(
                    self.stats["longest_outage_s"], outage)
            self.tl.emit(self.source, "loss_end", notable=True, ip=ip,
                         outage_s=round(outage, 1))
            self.tl.alert(self.source, "alert_ping_recovered", [
                f"ICMP to {ip} RESUMED after {outage:.1f}s of loss.",
                "  Compare against the health/stdout_tail lines around the same "
                "time: did the WebRTC session come back with it, or is the link "
                "back while the session stays dead? That difference is the answer.",
            ], ip=ip, outage_s=round(outage, 1))
            self.loss_since = None
        elif not r["ok"] and self.prev_ok is not False:
            self.loss_since = now
            with self._stats_lock:
                self.stats["outages"] += 1
            self.tl.emit(self.source, "loss_start", notable=True, ip=ip,
                         detail=r["detail"])
            self.tl.alert(self.source, "alert_ping_loss", [
                f"*** ICMP LOSS STARTED to the tracked robot IP {ip} ***",
                f"  {r['detail']}",
                "  The LINK just went quiet. If /health is still OK for a few more "
                "seconds, the network failed first (WiFi/roam). If /health went "
                "stale BEFORE this line, the WebRTC session died on a healthy "
                "network. Note which came first.",
                "  >>> Type a marker saying exactly where the robot is right now.",
            ], ip=ip, detail=r["detail"])
        self.prev_ok = bool(r["ok"])

    def window_and_reset(self) -> dict:
        with self._stats_lock:
            w = dict(self.window)
            self.window = {"total": 0, "ok": 0, "rtt_sum": 0.0, "rtt_n": 0}
        return w

    def snapshot(self) -> dict:
        with self._stats_lock:
            return dict(self.stats)


class NeighLoop(ProbeLoop):
    source = "neigh"

    def __init__(self, tl, stop_evt, interval, ref: RobotRef, ip_bin,
                 edge_interval: float):
        # Poll at the FAST edge cadence and only WRITE a full sample every
        # `interval`; that way a MAC flip or a drop to FAILED is caught within a
        # second instead of hiding in a 5s gap, without 5x-ing the sample count.
        super().__init__(tl, stop_evt, edge_interval)
        self.ip_bin = ip_bin
        self.ref = ref
        self.sample_interval = interval
        self.last_sample = 0.0
        self.prev = None                  # (state, mac)
        self.changes = 0
        self.mac_changes = 0

    def _query(self, ip: str) -> dict:
        p = subprocess.run([self.ip_bin, "neigh", "show", ip],
                           capture_output=True, text=True, timeout=8)
        raw = (p.stdout or "").strip()
        if not raw:
            # No entry at all: either never resolved, or the kernel garbage
            # collected it. Distinct from FAILED (tried, gave up).
            return {"ip": ip, "state": "ABSENT", "mac": None, "dev": None,
                    "raw": "", "rc": p.returncode,
                    "stderr": _short((p.stderr or "").strip(), 200) or None}
        line = raw.splitlines()[0]
        toks = line.split()
        states = [t for t in toks if t in NEIGH_RANK]
        state = max(states, key=lambda s: NEIGH_RANK[s]) if states else "UNKNOWN"
        mac_m = _MAC_RE.search(line)
        dev = None
        if "dev" in toks:
            i = toks.index("dev")
            if i + 1 < len(toks):
                dev = toks[i + 1]
        return {"ip": ip, "state": state, "mac": mac_m.group(1).lower() if mac_m else None,
                "dev": dev, "raw": raw, "rc": p.returncode, "stderr": None}

    def step(self):
        ip = self.ref.get().get("robot_ip")
        if not ip:
            self.last_sample = time.monotonic()
            self.stop.wait(2.0)
            return
        r = self._query(str(ip))
        cur = (r["state"], r["mac"])
        changed = self.prev is not None and cur != self.prev
        due = (time.monotonic() - self.last_sample) >= self.sample_interval

        if changed or due or self.prev is None:
            self.last_sample = time.monotonic()
            self.tl.emit(self.source, "change" if changed else "sample",
                         notable=changed or self.prev is None,
                         prev_state=self.prev[0] if self.prev else None,
                         prev_mac=self.prev[1] if self.prev else None, **r)

        if changed:
            self.changes += 1
            prev_state, prev_mac = self.prev
            mac_flip = bool(prev_mac and r["mac"] and prev_mac != r["mac"])
            degraded = (NEIGH_RANK.get(r["state"], 3) >= NEIGH_DEGRADED_AT
                        and NEIGH_RANK.get(r["state"], 3) > NEIGH_RANK.get(prev_state, 3))
            if mac_flip:
                self.mac_changes += 1
                self.tl.alert(self.source, "alert_mac_change", [
                    f"*** THE MAC BEHIND {ip} CHANGED: {prev_mac} -> {r['mac']} ***",
                    "  That IP is now a DIFFERENT DEVICE. Either the robot's DHCP "
                    "lease moved to something else, or we are talking to the wrong "
                    "box entirely. Everything addressed to this IP after this line "
                    "is suspect.",
                ], ip=ip, prev_mac=prev_mac, mac=r["mac"])
            elif degraded:
                raw_txt = r["raw"] or ("(no neighbour entry at all — the kernel has "
                                       "nothing cached for this IP)")
                self.tl.alert(self.source, "alert_neigh_degraded", [
                    f"ARP/neighbour state for {ip} DEGRADED: {prev_state} -> "
                    f"{r['state']}",
                    f"  {raw_txt}",
                    "  Under a 1s ping this entry should stay REACHABLE, so this is "
                    "real: the host can no longer confirm the robot at layer 2.",
                ], ip=ip, prev_state=prev_state, state=r["state"], raw=r["raw"])
        self.prev = cur


class DiscoverLoop(ProbeLoop):
    source = "discover"

    class _SinkAdapter:
        """Duck-type of connection_watchdog.EventSink so resolve_robot_ip() can
        be reused VERBATIM — its raw per-scan `discover_result` (the full record
        list) lands in our timeline instead of the watchdog's stdout."""

        def __init__(self, tl: Timeline):
            self.tl = tl

        def emit(self, kind: str, **fields):
            self.tl.emit("discover", kind, notable=False, **fields)

    def __init__(self, tl, stop_evt, interval, ref: RobotRef, timeout, known_mac):
        super().__init__(tl, stop_evt, interval)
        self.ref = ref
        self.timeout = timeout
        self.known_mac = known_mac
        self.sink = self._SinkAdapter(tl)
        self.divergences = 0
        self.scans = 0
        self.first_divergence = None
        # Guards fire_now(): a reactive scan already in flight must not be
        # piled on top of by a second one if both_stale flaps a few times in a
        # row (HealthLoop polls every ~1.5s; a scan takes up to `timeout`,
        # commonly several seconds) — one in-flight reactive scan is enough to
        # answer the question, a queue of them just hammers the LAN for no
        # extra signal.
        self._reactive_lock = threading.Lock()
        self._reactive_busy = False

    def step(self):
        self._scan(trigger="periodic")

    def fire_now(self, reason: str) -> bool:
        """Run ONE discovery scan immediately, off the periodic cadence, in a
        throwaway thread so the caller (HealthLoop, mid-poll) never blocks on
        it. Returns True if a scan was actually started, False if one was
        already in flight (see the busy-guard above) — callers can ignore the
        return value; it exists for a human reading the timeline later to
        confirm a reactive scan really launched.

        WHY THIS EXISTS: the periodic scan only runs every
        --discover-interval (default 40s), so "did the robot's IP change"
        could sit unanswered for up to 40s after a trip. The moment worth
        checking is EXACTLY the moment health goes bad, not some arbitrary
        point up to 40s later — so a trip fires an extra scan on the spot,
        on top of (not instead of) the periodic ones."""
        with self._reactive_lock:
            if self._reactive_busy:
                return False
            self._reactive_busy = True

        def _run():
            try:
                self._scan(trigger="reactive", note=reason)
            finally:
                with self._reactive_lock:
                    self._reactive_busy = False

        threading.Thread(target=_run, name="discover-reactive",
                         daemon=True).start()
        return True

    def _scan(self, trigger: str, note: str = None) -> None:
        ref = self.ref.get()
        tracked_ip = ref.get("robot_ip")
        # resolve_robot_ip() already encodes the non-guessing policy for the
        # 0-found / 1-found / >1-found cases (and returns a human-readable
        # reason instead of a guess). Reused rather than re-derived — a second
        # implementation of "which of these is our robot" is exactly how the two
        # tools would end up disagreeing at 2am.
        ip, mac, reason = resolve_robot_ip(self.known_mac, tracked_ip,
                                           self.timeout, self.sink)
        self.scans += 1
        if mac and not self.known_mac:
            self.known_mac = mac
            self.tl.note(f"learned robot MAC {mac} from discovery; later "
                         f"multi-candidate scans are now decidable")

        diverged = bool(ip and tracked_ip and ip != tracked_ip)
        self.tl.emit(self.source, "result", notable=True, resolved_ip=ip,
                     resolved_mac=mac, reason=reason, tracked_ip=tracked_ip,
                     tracked_running=ref.get("running"), diverged=diverged,
                     undecidable=ip is None, trigger=trigger,
                     trigger_note=note)

        if diverged:
            self.divergences += 1
            if self.first_divergence is None:
                self.first_divergence = (tracked_ip, ip, time.time())
            live = ref.get("running")
            self.tl.alert(self.source, "alert_ip_diverged", [
                "*** THE ROBOT IS ANSWERING ON A DIFFERENT IP THAN THE PANEL "
                "THINKS ***",
                f"  discovered now : {ip}   (mac {mac})",
                f"  panel tracks   : {tracked_ip}",
                f"  panel says running={live}",
                f"  triggered by   : {trigger}" + (f" ({note})" if note else ""),
                ("  running=TRUE means this is a LIVE IP CHANGE WITH NO RELAUNCH "
                 "AND NO POWER-CYCLE — that is the direct answer to the open "
                 "question, and it means a plain restart() would relaunch against "
                 "a dead address."
                 if live else
                 "  running=false, so this may just be a stale tracked IP from an "
                 "already-finished run; check whether a relaunch happened."),
                "  >>> Type a marker: what was happening physically right now?",
            ], resolved_ip=ip, tracked_ip=tracked_ip, mac=mac, tracked_running=live,
               trigger=trigger)
        elif trigger == "reactive":
            # A negative result is itself the answer to "did the AP change" —
            # worth one clear line even though it's not alert-worthy the way a
            # real divergence is, so it doesn't just blend into the periodic
            # "result" samples the default stderr filter mostly skips.
            self.tl.note(f"reactive scan ({note}): robot still resolves to the "
                         f"SAME ip {tracked_ip} — this incident is not an IP/AP "
                         f"change, at least not yet")


class WifiLoop(ProbeLoop):
    """Host-side association snapshot: which AP WE are on, and whether we roam.

    Purpose is elimination. If the host's BSSID flips at the moment of an
    incident, the venue's multi-AP roaming hit OUR leg and the robot may have
    been innocent. If the host's BSSID is rock stable across the incident, the
    host leg is ruled out and the problem is on the robot's leg or in the
    session itself. Either answer is progress; without this loop both stay open.
    """
    source = "wifi"

    def __init__(self, tl, stop_evt, interval, iw_bin, iwconfig_bin, iface):
        super().__init__(tl, stop_evt, interval)
        # Prefer `iw` (nl80211) when available — it's the one that actually
        # works against a modern driver (see the _NL80211_* comment above).
        # `iwconfig` is kept ONLY as a fallback for hosts where `iw` is
        # missing but the legacy WEXT ioctls still answer.
        self.iw_bin = iw_bin
        self.iwconfig_bin = iwconfig_bin
        self.iface = iface
        self.prev = None
        self.roams = 0

    def _parse_iwconfig(self, text: str) -> dict:
        def _g(rx, cast=str, grp=1):
            m = rx.search(text)
            if not m:
                return None
            try:
                return cast(m.group(grp))
            except (ValueError, TypeError):
                return None
        ap = _g(_IW_AP_RE)
        rate_m = _IW_RATE_RE.search(text)
        return {
            "iface": self.iface,
            "essid": _g(_IW_ESSID_RE),
            "bssid": (ap.lower() if ap and ap != "Not-Associated" else None),
            "associated": bool(ap and ap != "Not-Associated"),
            "freq_ghz": _g(_IW_FREQ_RE, float),
            "link_quality": _g(_IW_QUAL_RE, int),
            "link_quality_max": _g(_IW_QUAL_RE, int, 2),
            "signal_dbm": _g(_IW_SIG_RE, int),
            "bitrate": f"{rate_m.group(1)} {rate_m.group(2)}" if rate_m else None,
            "missed_beacon": _g(_IW_BEACON_RE, int),
            "via": "iwconfig",
        }

    def _parse_nl80211(self, text: str) -> dict:
        """Parse `iw dev <iface> link`. "Not connected." (no "Connected to"
        line) is the not-associated case — same meaning as iwconfig's
        Access Point: Not-Associated, just a different sentence for it.
        `iw` has no Link-Quality-style percentage (that's a WEXT concept);
        `signal_dbm` is the metric to trend instead. `missed_beacon` isn't in
        `iw ... link` output (would need a second call, `iw ... station
        dump`) — left None here rather than paying for a call we don't need
        to answer the question this loop exists for (did we roam / drop)."""
        def _g(rx, cast=str, grp=1):
            m = rx.search(text)
            if not m:
                return None
            try:
                return cast(m.group(grp))
            except (ValueError, TypeError):
                return None
        bssid = _g(_NL80211_BSSID_RE)
        freq_mhz = _g(_NL80211_FREQ_RE, float)
        return {
            "iface": self.iface,
            "essid": _g(_NL80211_SSID_RE),
            "bssid": bssid.lower() if bssid else None,
            "associated": bool(bssid),
            "freq_ghz": round(freq_mhz / 1000.0, 3) if freq_mhz else None,
            "link_quality": None,
            "link_quality_max": None,
            "signal_dbm": _g(_NL80211_SIGNAL_RE, int),
            "bitrate": _g(_NL80211_RXRATE_RE),
            "missed_beacon": None,
            "via": "iw",
        }

    def step(self):
        if self.iw_bin:
            p = subprocess.run([self.iw_bin, "dev", self.iface, "link"],
                               capture_output=True, text=True, timeout=8)
            r = self._parse_nl80211((p.stdout or "") + (p.stderr or ""))
        else:
            p = subprocess.run([self.iwconfig_bin, self.iface],
                               capture_output=True, text=True, timeout=8)
            r = self._parse_iwconfig((p.stdout or "") + (p.stderr or ""))
        cur = (r["bssid"], r["essid"], r["freq_ghz"], r["associated"])
        sig = r["signal_dbm"]
        prev_sig = self.prev[1] if self.prev else None
        big_sig_move = (isinstance(sig, int) and isinstance(prev_sig, int)
                        and abs(sig - prev_sig) >= 10)
        prev_key = self.prev[0] if self.prev else None
        changed = prev_key is not None and cur != prev_key
        self.tl.emit(self.source, "change" if changed else "sample",
                     notable=changed or big_sig_move or self.prev is None, **r)

        if changed:
            pb, pe, pf, pa = prev_key
            if r["bssid"] != pb:
                self.roams += 1
                self.tl.alert(self.source, "alert_host_roam", [
                    "*** THIS HOST JUST ROAMED TO A DIFFERENT ACCESS POINT ***",
                    f"  bssid {pb} -> {r['bssid']}   essid {pe} -> {r['essid']}   "
                    f"freq {pf} -> {r['freq_ghz']} GHz",
                    "  This is the HOST's own leg, not the robot's. If the "
                    "disconnect lines up with this, the venue's multi-AP roaming "
                    "hit US and the robot leg may be innocent.",
                ], **r)
            elif pa and not r["associated"]:
                self.tl.alert(self.source, "alert_host_deassociated", [
                    f"*** THIS HOST IS NO LONGER ASSOCIATED to any AP on "
                    f"{self.iface} ***",
                    "  Everything else in the timeline during this window is "
                    "measuring our own broken WiFi, not the robot's.",
                ], **r)
        self.prev = (cur, sig)


class TailLoop(ProbeLoop):
    """Forward every new line of the running blueprint's own stdout, VERBATIM.

    NO KEYWORD FILTERING, on purpose. The ICE/peer connection-state handlers are
    bare print()s and nobody knows the exact strings; filtering at collection
    time is how you discover afterwards that the one line that mattered was the
    one you dropped. Capture everything for the collection window; grep later.

    Truncation handling matters here: run_blueprint opens this path with "wb",
    so a relaunch truncates the SAME inode. Detect size < offset (truncated) and
    inode change (replaced) and keep reading rather than sitting at a stale
    offset forever.
    """
    source = "stdout_tail"

    def __init__(self, tl, stop_evt, interval, ref: RobotRef, override_path,
                 from_start: bool):
        super().__init__(tl, stop_evt, interval)
        self.ref = ref
        self.override = override_path
        self.from_start = from_start
        self.path = None
        self.fh = None
        self.ino = None
        self.offset = 0
        self.remainder = ""
        self.lines = 0
        self.missing_logged = False

    def _log_path(self):
        # NOT named _target(): ProbeLoop subclasses threading.Thread, which
        # already owns an instance attribute called `_target` (the callable it
        # invokes from run() when constructed with target=...). Even though we
        # never pass target=, Thread.__init__ still sets self._target = None,
        # which silently shadows a same-named method defined here — self._target
        # would resolve to that None, not this function, and calling it raises
        # "'NoneType' object is not callable". Confirmed live: this crashed the
        # tail loop every single poll for an entire capture run before being
        # caught, because static checks (ast.parse/py_compile) can't see a
        # runtime attribute-shadowing bug like this.
        if self.override:
            return self.override
        st = self.ref.get()
        return st.get("stdout_log") or getattr(
            dimos_cli, "_LAST_LAUNCH_LOG", "/tmp/dimos-pwa-last-launch.log")

    def _close(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
        self.fh, self.ino, self.offset, self.remainder = None, None, 0, ""

    def _open(self, path, why):
        self.fh = open(path, "r", errors="replace")
        stat = os.fstat(self.fh.fileno())
        self.ino = stat.st_ino
        if self.from_start:
            self.offset = 0
        else:
            self.fh.seek(0, os.SEEK_END)
            self.offset = self.fh.tell()
        self.path = path
        self.missing_logged = False
        self.tl.emit(self.source, "open", notable=True, path=path,
                     detail=f"{why}; starting at offset {self.offset} "
                            f"({'whole file' if self.from_start else 'end of file'})",
                     size=stat.st_size)

    def step(self):
        path = self._log_path()
        if path != self.path and self.fh is not None:
            self._close()
        if self.fh is None:
            if not os.path.exists(path):
                if not self.missing_logged:
                    self.missing_logged = True
                    self.tl.emit(self.source, "missing", notable=True, path=path,
                                 detail="stdout log does not exist yet (nothing "
                                        "launched through the panel?); will keep "
                                        "watching for it")
                return
            self._open(path, "opened stdout log")

        try:
            stat = os.stat(path)
        except OSError as e:
            self.tl.emit(self.source, "vanished", notable=True, path=path,
                         detail=f"stat failed: {e!r}")
            self._close()
            return

        if stat.st_ino != self.ino:
            self.tl.emit(self.source, "rotated", notable=True, path=path,
                         detail="inode changed — the log was replaced; reopening "
                                "from the start of the new file")
            self._close()
            self._open(path, "reopened after rotation")
            self.offset = 0
            self.fh.seek(0)
        elif stat.st_size < self.offset:
            self.tl.emit(self.source, "truncated", notable=True, path=path,
                         detail=f"size {stat.st_size} < our offset {self.offset} — "
                                f"run_blueprint reopened this path 'wb', i.e. "
                                f"SOMETHING RELAUNCHED THE BLUEPRINT. Rewinding to "
                                f"0; lines after this belong to the new run.")
            self.offset = 0
            self.remainder = ""
            self.fh.seek(0)

        self.fh.seek(self.offset)
        chunk = self.fh.read()
        self.offset = self.fh.tell()
        if not chunk:
            return
        buf = self.remainder + chunk
        parts = buf.split("\n")
        self.remainder = parts.pop()          # trailing partial line, if any
        for ln in parts:
            self.lines += 1
            self.tl.emit(self.source, "line", notable=True,
                         line=ln.rstrip("\r"), path=path, offset=self.offset)

    def flush_remainder(self):
        if self.remainder:
            self.tl.emit(self.source, "partial_line", notable=True,
                         line=self.remainder, path=self.path,
                         detail="unterminated final line at shutdown")
            self.remainder = ""


class MarkerSink:
    """THE definition of what a marker event is — one place, several inputs.

    There are now two ways a human annotation reaches the timeline (typed into
    an attached terminal, or appended to --marker-file by the panel while this
    runs headless). They are the same human act and must produce byte-identical
    events, so both go through here rather than each building its own
    `tl.alert(...)` call that drifts the moment one of them is edited. The
    counter is shared and locked for the same reason: markers are numbered by
    the ORDER THEY HAPPENED, not by which channel carried them."""

    def __init__(self, tl: Timeline):
        self.tl = tl
        self._lock = threading.Lock()
        self.count = 0

    def add(self, text: str, via: str) -> int:
        with self._lock:
            self.count += 1
            n = self.count
        # `via` is additive metadata for the post-mortem ("was this typed at the
        # robot or POSTed from the panel?"); every other field, the source, the
        # kind and the banner are identical across channels on purpose.
        self.tl.alert("marker", "marker", [f">>> MARKER #{n}: {text}"],
                      text=text, n=n, via=via)
        return n


class MarkerThread(threading.Thread):
    """Whatever the human types + Enter goes straight into the timeline.

    No keyword required: "at the hallway corner", "video froze", "AAAA" are all
    useful, and demanding a syntax from someone who is walking a robot and
    talking on comms means you get no markers at all.

    Degrades silently and harmlessly when there is no terminal (the sidecar case
    spawns us with stdin=DEVNULL): it logs one line and returns, leaving the
    file channel — MarkerFileLoop — as the only marker input. Both may run at
    once; they share a MarkerSink so that is a no-op either way."""

    def __init__(self, tl: Timeline, stop_evt: threading.Event, sink: MarkerSink):
        super().__init__(name="marker", daemon=True)
        self.tl = tl
        self.stop = stop_evt
        self.sink = sink

    def run(self):
        if not sys.stdin or sys.stdin.closed:
            self.tl.note("stdin unavailable — typed markers disabled (this is "
                         "normal for the headless sidecar; use --marker-file)")
            return
        try:
            for raw in sys.stdin:
                if self.stop.is_set():
                    return
                text = raw.rstrip("\n").rstrip("\r")
                if not text.strip():
                    continue
                self.sink.add(text, via="stdin")
        except Exception as e:  # noqa: BLE001
            self.tl.error("marker", e, where="marker.run")
        else:
            self.tl.note("stdin closed (EOF) — typed markers are off for the rest "
                         "of this run; everything else keeps collecting")


class MarkerFileLoop(ProbeLoop):
    """Second marker channel: every new line appended to --marker-file.

    WHY: once this tool is spawned as a sidecar by the panel there is no
    terminal to type into, and a marker is worth more than almost any automatic
    sample — it is the only source that says WHERE THE ROBOT PHYSICALLY WAS.
    Losing that channel in headless mode would gut the timeline. A plain
    append-only text file is the cheapest possible IPC that works across a
    process boundary with no socket, no port and no permissions to arrange: the
    panel's POST /api/diagnostics/marker appends one line, we pick it up within
    --marker-file-interval and emit the identical event MarkerThread would have.

    Tails the same way TailLoop tails the blueprint's stdout log (poll, read
    from a saved offset, keep a trailing partial line for next time), minus the
    log-specific parts. Differences worth knowing:
      * The file is read FROM THE BEGINNING on first open, not from the end.
        The sidecar gets a fresh per-run path, so there is nothing stale to skip
        and this closes the race where a marker is POSTed in the moment between
        the panel creating the file and this loop first opening it. Point
        --marker-file at a fresh path per run, or old lines replay as markers.
      * A missing file is normal, not an error: it may simply not have been
        created yet, and it is logged once and then waited for.
      * Truncation/replacement is handled (someone may `> file` it) so a
        truncated file does not park us at a stale offset forever.
    """
    # Deliberately NOT "marker": these are this loop's own lifecycle/error
    # events, and tagging them "marker" would make them indistinguishable from
    # real human markers when grepping the timeline (and would render them
    # wrong in _one_line). The markers themselves go through MarkerSink and ARE
    # tagged "marker".
    source = "marker_file"

    def __init__(self, tl, stop_evt, interval, path: str, sink: MarkerSink):
        super().__init__(tl, stop_evt, interval)
        self.path = path
        self.sink = sink
        self.fh = None
        self.ino = None
        self.offset = 0
        self.remainder = ""
        self.missing_logged = False

    def _close(self):
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
        self.fh, self.ino, self.offset, self.remainder = None, None, 0, ""

    def step(self):
        if self.fh is None:
            if not os.path.exists(self.path):
                if not self.missing_logged:
                    self.missing_logged = True
                    self.tl.emit(self.source, "missing", notable=True,
                                 path=self.path,
                                 detail="marker file does not exist yet; will keep "
                                        "watching for it (appending a line to this "
                                        "path drops a marker into the timeline)")
                return
            self.fh = open(self.path, "r", errors="replace")
            self.ino = os.fstat(self.fh.fileno()).st_ino
            self.offset = 0
            self.missing_logged = False
            self.tl.emit(self.source, "open", notable=True, path=self.path,
                         detail="watching for appended marker lines (reading from "
                                "the start so nothing appended before we opened it "
                                "is lost)")

        try:
            stat = os.stat(self.path)
        except OSError as e:
            self.tl.emit(self.source, "vanished", notable=True, path=self.path,
                         detail=f"stat failed: {e!r}")
            self._close()
            return

        if stat.st_ino != self.ino or stat.st_size < self.offset:
            # Replaced or truncated out from under us. Reopen from scratch
            # rather than sit at an offset past the end reading nothing forever.
            self.tl.emit(self.source, "reset", notable=True, path=self.path,
                         detail=f"marker file was replaced or truncated "
                                f"(size {stat.st_size} vs offset {self.offset}); "
                                f"rereading from the start")
            self._close()
            return

        self.fh.seek(self.offset)
        chunk = self.fh.read()
        self.offset = self.fh.tell()
        if not chunk:
            return
        buf = self.remainder + chunk
        parts = buf.split("\n")
        # A line with no trailing newline yet is a HALF-WRITTEN marker; hold it
        # until the writer finishes the line instead of emitting a truncated one.
        self.remainder = parts.pop()
        for ln in parts:
            text = ln.rstrip("\r")
            if not text.strip():
                continue
            self.sink.add(text, via="file")


# --------------------------------------------------------------------------- #
def _pick_wifi_iface(tl: Timeline, ip_bin, iw_bin, iwconfig_bin, forced):
    """Find a WiFi interface on THIS host, best-effort.

    Prefer `iw dev` to enumerate: it lists ONLY wireless interfaces directly
    (no probing every link), and it is what actually works on a modern
    cfg80211 driver — confirmed live on the Ascent's MediaTek Filogic
    (MT7925): `iwconfig <iface>` answers "no wireless extensions" for that
    card (the driver never implemented the legacy WEXT ioctls at all), which
    silently produced zero WiFi data on exactly the box this loop most needed
    to run on. `iwconfig` is kept as a fallback for a host where `iw` isn't
    installed but the interface still answers WEXT (true of the dev laptop
    tonight). A host with neither, or genuinely on ethernet, is a perfectly
    valid setup — this loop just skips, once, with a clear reason.
    """
    if forced:
        return forced

    if iw_bin:
        try:
            p = subprocess.run([iw_bin, "dev"], capture_output=True, text=True,
                               timeout=8)
            names = re.findall(r"Interface\s+(\S+)", p.stdout or "")
        except (subprocess.SubprocessError, OSError) as e:
            tl.note(f"`iw dev` failed ({e!r}); falling back to iwconfig probing")
            names = []
        if names:
            associated = []
            for n in names:
                try:
                    p = subprocess.run([iw_bin, "dev", n, "link"],
                                       capture_output=True, text=True, timeout=8)
                except (subprocess.SubprocessError, OSError):
                    continue
                if (p.stdout or "").startswith("Connected to"):
                    associated.append(n)
            if associated:
                return associated[0]
            tl.note(f"WiFi interface(s) {names} exist (via `iw dev`) but none are "
                    f"associated right now; watching {names[0]} anyway (it may "
                    f"associate later)")
            return names[0]
        # `iw` is present but found no wireless interfaces at all — a host
        # genuinely on ethernet. No point falling through to iwconfig too.
        if not iwconfig_bin:
            tl.note("`iw dev` found no wireless interfaces — host is probably on "
                    "ethernet; skipping the host WiFi snapshot loop")
            return None

    if not iwconfig_bin:
        tl.note("neither `iw` nor `iwconfig` found on this host — skipping the "
                "host WiFi snapshot loop entirely. Everything else still "
                "collects; you just won't be able to rule the host's own "
                "roaming in or out.")
        return None
    names = []
    if ip_bin:
        try:
            p = subprocess.run([ip_bin, "-o", "link", "show"], capture_output=True,
                               text=True, timeout=8)
            for line in (p.stdout or "").splitlines():
                m = _LINK_IFACE_RE.match(line.strip())
                if m and m.group(1) != "lo":
                    names.append(m.group(1))
        except (subprocess.SubprocessError, OSError) as e:
            tl.note(f"could not enumerate interfaces ({e!r}); WiFi snapshot skipped")
            return None
    if not names:
        tl.note("no non-loopback interfaces found; WiFi snapshot skipped")
        return None

    wireless, associated = [], []
    for n in names:
        try:
            p = subprocess.run([iwconfig_bin, n], capture_output=True, text=True,
                               timeout=8)
        except (subprocess.SubprocessError, OSError):
            continue
        text = (p.stdout or "") + (p.stderr or "")
        if "no wireless extensions" in text.lower():
            continue
        if "IEEE 802.11" in text or "ESSID" in text:
            wireless.append(n)
            m = _IW_AP_RE.search(text)
            if m and m.group(1) != "Not-Associated":
                associated.append(n)
    if associated:
        return associated[0]
    if wireless:
        tl.note(f"WiFi interface(s) {wireless} exist but none are associated right "
                f"now; watching {wireless[0]} anyway (it may associate later)")
        return wireless[0]
    tl.note(f"no interface among {names} has wireless extensions (host is probably "
            f"on ethernet) — skipping the host WiFi snapshot loop")
    return None


def _rollup(tl: Timeline, ping: PingLoop) -> str:
    """One periodic line proving we're alive and summarising the quiet samples
    that the default stderr filter deliberately does not print."""
    bits = []
    if ping is not None:
        w = ping.window_and_reset()
        if w["total"]:
            avg = (w["rtt_sum"] / w["rtt_n"]) if w["rtt_n"] else None
            bits.append(f"ping {w['ok']}/{w['total']} ok"
                        + (f" avg {avg:.1f}ms" if avg is not None else ""))
    h = tl.latest.get("health")
    if h:
        bits.append(f"cam={(h.get('camera') or {}).get('state')} "
                    f"lid={(h.get('lidar') or {}).get('state')}")
    n = tl.latest.get("neigh")
    if n:
        bits.append(f"neigh={n.get('state')}")
    w_ = tl.latest.get("wifi")
    if w_ and w_.get("bssid"):
        bits.append(f"ap={w_.get('bssid')}@{w_.get('freq_ghz')}GHz "
                    f"{w_.get('signal_dbm')}dBm")
    s = tl.latest.get("status")
    if s:
        bits.append(f"ip={s.get('robot_ip')} running={s.get('running')}")
    return " | ".join(bits) if bits else "no samples yet"


def _ready_line(ready: bool, **fields) -> None:
    """The single JSON line on stdout that a supervising process reads.

    Same shape and same contract as camera_daemon.py / arm_camera_daemon.py:
    ONE line, on stdout, then stdout goes quiet — so the manager's blocking
    readline returns immediately and the pipe can never back up and wedge us
    (every other byte we produce goes to stderr or the timeline file). The
    ready=False variant exists so a supervisor learns WHY we failed instead of
    waiting out its whole readiness timeout on a process that already exited.

    Harmless when nobody is listening: a hand-launched run just prints one
    extra line before the banner.
    """
    print(json.dumps({"ready": bool(ready), "pid": os.getpid(), **fields}),
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Forensic network-evidence collector for a deliberate "
                    "reproduction of the Go2's silent WebRTC disconnect. Collects "
                    "only — never sends the robot anything, never recovers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Any interval flag set to 0 disables that loop. Type anything + "
               "Enter while it runs to drop a timestamped marker into the "
               "timeline — or, when running headless, append a line to "
               "--marker-file.")
    ap.add_argument("--out", default=None,
                    help="JSONL timeline path (default "
                         "/tmp/dimos-network-diag-<start>.jsonl)")
    ap.add_argument("--panel", default=DEFAULT_PANEL,
                    help=f"control-panel base URL for /api/status (default {DEFAULT_PANEL})")
    ap.add_argument("--daemon-host", default="127.0.0.1",
                    help="host the camera/lidar daemons' /health are reachable on")
    ap.add_argument("--camera-port", type=int, default=dimos_cli.CAMERA_STREAM_PORT,
                    help="camera daemon health port (default: dimos_cli.CAMERA_STREAM_PORT)")
    ap.add_argument("--lidar-port", type=int, default=dimos_cli.LIDAR_STREAM_PORT,
                    help="lidar daemon health port (default: dimos_cli.LIDAR_STREAM_PORT)")
    ap.add_argument("--health-interval", type=float, default=HEALTH_INTERVAL_S,
                    help=f"seconds between /health polls (default {HEALTH_INTERVAL_S:g}; "
                         f"deliberately much faster than the watchdog's 6s)")
    ap.add_argument("--max-age", type=float, default=None,
                    help="optional stricter staleness bound in seconds; by default "
                         "we trust each daemon's own `fresh` flag")
    ap.add_argument("--ping-interval", type=float, default=PING_INTERVAL_S,
                    help=f"seconds between one-shot pings (default {PING_INTERVAL_S:g})")
    ap.add_argument("--ping-timeout", type=float, default=PING_TIMEOUT_S,
                    help=f"per-ping -W wait in seconds (default {PING_TIMEOUT_S:g}; "
                         f"fractional values need a reasonably modern iputils-ping)")
    ap.add_argument("--neigh-interval", type=float, default=NEIGH_INTERVAL_S,
                    help=f"seconds between recorded `ip neigh` samples (default "
                         f"{NEIGH_INTERVAL_S:g}); changes are ALSO caught "
                         f"edge-triggered at --neigh-edge-interval")
    ap.add_argument("--neigh-edge-interval", type=float, default=1.0,
                    help="how often to CHECK for a neigh state/MAC change between "
                         "recorded samples (default 1.0)")
    ap.add_argument("--discover-interval", type=float, default=DISCOVER_INTERVAL_S,
                    help=f"seconds between LAN re-discoveries (default "
                         f"{DISCOVER_INTERVAL_S:g}); 0 disables, which also removes "
                         f"this tool's only non-trivial network chatter")
    ap.add_argument("--discover-timeout", type=float, default=DISCOVER_TIMEOUT_S,
                    help=f"LAN discovery probe window (default {DISCOVER_TIMEOUT_S:g})")
    ap.add_argument("--robot-mac", default=None,
                    help="this robot's MAC — makes a multi-robot LAN decidable "
                         "instead of undecidable; learned automatically on the first "
                         "single-robot scan if omitted")
    ap.add_argument("--wifi-interval", type=float, default=WIFI_INTERVAL_S,
                    help=f"seconds between host WiFi snapshots (default {WIFI_INTERVAL_S:g})")
    ap.add_argument("--wifi-iface", default=None,
                    help="force a specific host WiFi interface instead of autodetecting")
    ap.add_argument("--status-interval", type=float, default=STATUS_INTERVAL_S,
                    help=f"seconds between tracked-run refreshes (default {STATUS_INTERVAL_S:g})")
    ap.add_argument("--tail-interval", type=float, default=TAIL_POLL_S,
                    help=f"seconds between stdout-log polls (default {TAIL_POLL_S:g})")
    ap.add_argument("--stdout-log", default=None,
                    help="override the blueprint stdout log path (default: whatever "
                         "/api/status reports, else dimos_cli._LAST_LAUNCH_LOG)")
    ap.add_argument("--tail-from-start", action="store_true",
                    help="ingest the WHOLE existing stdout log first instead of only "
                         "new lines (useful if the run started before you did)")
    ap.add_argument("--marker-file", default=None,
                    help="path to an append-only text file whose new lines each "
                         "become a marker event — the marker channel that works "
                         "with no terminal attached (the panel's POST "
                         "/api/diagnostics/marker writes here). Read from the "
                         "START on first open, so use a FRESH path per run. Works "
                         "alongside typed stdin markers, not instead of them.")
    ap.add_argument("--marker-file-interval", type=float, default=MARKER_FILE_POLL_S,
                    help=f"seconds between --marker-file polls (default "
                         f"{MARKER_FILE_POLL_S:g}); 0 disables the file channel")
    ap.add_argument("--rollup-interval", type=float, default=ROLLUP_S,
                    help=f"seconds between periodic stderr rollup lines (default {ROLLUP_S:g})")
    ap.add_argument("--stderr-echo", choices=("all", "interesting", "none"),
                    default="interesting",
                    help="how much of the timeline to mirror to stderr live. "
                         "'interesting' (default) = changes, failures, tail lines, "
                         "markers, discoveries + a periodic rollup, so the banners "
                         "stay visible. 'all' = every single sample (~2 lines/sec — "
                         "banners WILL scroll away). 'none' = banners only. The "
                         "--out file always contains everything.")
    ap.add_argument("--stdout-jsonl", action="store_true",
                    help="also stream the JSONL timeline on stdout for piping")
    args = ap.parse_args()

    started = time.time()
    out = args.out or ("/tmp/dimos-network-diag-"
                       + time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
                       + ".jsonl")
    out_dir = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(out_dir):
        _ready_line(ready=False, out=out,
                    error=f"output directory does not exist: {out_dir}")
        print(f"[{_ts()}] FATAL: output directory does not exist: {out_dir}",
              file=sys.stderr, flush=True)
        return 2
    try:
        tl = Timeline(out, echo=args.stderr_echo, stdout_jsonl=args.stdout_jsonl)
    except OSError as e:
        _ready_line(ready=False, out=out,
                    error=f"cannot open timeline file {out}: {e!r}")
        print(f"[{_ts()}] FATAL: cannot open timeline file {out}: {e!r}",
              file=sys.stderr, flush=True)
        return 2

    # Readiness handshake — printed HERE, and nowhere later, for two reasons.
    # (1) The only startup failure a supervisor can do anything about is "I
    # could not open the evidence file"; everything after this point (no ping
    # binary, panel down, no robot yet) is DATA this tool exists to record, not
    # a reason to call the spawn failed. (2) The seed read_status() below makes
    # an HTTP call to the very panel process that may be blocking on this line
    # inside run_blueprint — answering before that call keeps the handshake off
    # any network path entirely.
    _ready_line(ready=True, out=out, marker_file=args.marker_file)

    stop_evt = threading.Event()

    def _shutdown(_signum, _frame):
        stop_evt.set()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    ping_bin = _which("ping")
    ip_bin = _which("ip")
    iw_bin = _which("iw")
    iwconfig_bin = _which("iwconfig")

    cam_url = f"http://{args.daemon_host}:{args.camera_port}/health"
    lid_url = f"http://{args.daemon_host}:{args.lidar_port}/health"

    ref = RobotRef()
    ref.set(read_status(args.panel, STATUS_TIMEOUT_S))   # seed before loops start
    seed = ref.get()

    _banner([
        "NETWORK DIAGNOSTICS COLLECTOR — forensic capture only, NO recovery",
        f"  timeline  : {out}",
        f"  panel     : {args.panel}   (read-only /api/status)",
        f"  health    : {cam_url} + {lid_url}  every {args.health_interval:g}s",
        f"  ping      : every {args.ping_interval:g}s (-W {args.ping_timeout:g})  "
        f"binary={ping_bin or 'MISSING'}",
        f"  ip neigh  : sample {args.neigh_interval:g}s, change-check "
        f"{args.neigh_edge_interval:g}s  binary={ip_bin or 'MISSING'}",
        f"  discover  : every {args.discover_interval:g}s "
        f"(-t {args.discover_timeout:g}) — runs the WHOLE time, not just after a trip",
        f"  wifi      : every {args.wifi_interval:g}s  "
        f"binary={iw_bin or iwconfig_bin or 'MISSING'} "
        f"({'iw' if iw_bin else 'iwconfig' if iwconfig_bin else 'none'})",
        f"  stdout log: {args.stdout_log or seed.get('stdout_log') or 'auto'}  "
        f"(verbatim, UNFILTERED)",
        f"  tracked   : running={seed.get('running')} robot_ip={seed.get('robot_ip')} "
        f"blueprint={seed.get('blueprint')} via={seed.get('via')}",
        "",
        "  THIS TOOL NEVER SENDS THE ROBOT ANYTHING. It will not recover, restart,",
        "  reconnect or nudge. If the link dies, LET IT SIT — that is the data.",
        "",
        f"  stderr echo: {args.stderr_echo} (the file always has every sample)",
        "  TYPE ANYTHING + ENTER at any time to drop a marker, e.g.",
        "     at the hallway corner now",
        (f"  marker file: {args.marker_file} (append a line = drop a marker, "
         f"no terminal needed)" if args.marker_file
         else "  marker file: none (--marker-file) — typed markers only"),
        "  Ctrl-C (or SIGTERM) to stop and print a summary.",
    ])
    tl.emit("collector", "start", started=started, out=out, panel=args.panel,
            marker_file=args.marker_file, argv=sys.argv[1:], seed_status=seed,
            binaries={"ping": ping_bin, "ip": ip_bin, "iw": iw_bin,
                     "iwconfig": iwconfig_bin},
            intervals={"health": args.health_interval, "ping": args.ping_interval,
                       "neigh": args.neigh_interval, "discover": args.discover_interval,
                       "wifi": args.wifi_interval, "status": args.status_interval,
                       "tail": args.tail_interval})

    loops = []
    if args.status_interval > 0:
        loops.append(StatusLoop(tl, stop_evt, args.status_interval, args.panel, ref))

    # Built BEFORE HealthLoop (even though it's appended after PingLoop/NeighLoop
    # below, for readability) specifically so HealthLoop can hold a reference and
    # fire an immediate out-of-band scan the instant a both-stale trip happens,
    # on top of this loop's own periodic cadence. None when discovery is
    # disabled — HealthLoop degrades to "no reactive scan" cleanly in that case.
    discover_loop = None
    if args.discover_interval > 0:
        discover_loop = DiscoverLoop(tl, stop_evt, args.discover_interval, ref,
                                     args.discover_timeout, args.robot_mac)
    else:
        tl.note("LAN re-discovery DISABLED (--discover-interval 0) — the live-IP-change "
                "question cannot be answered from this run, including reactively "
                "on a trip")

    if args.health_interval > 0:
        loops.append(HealthLoop(tl, stop_evt, args.health_interval, cam_url, lid_url,
                                args.max_age, discover_loop=discover_loop))

    ping = None
    if args.ping_interval > 0:
        if ping_bin:
            ping = PingLoop(tl, stop_evt, args.ping_interval, ref, ping_bin,
                            args.ping_timeout)
            loops.append(ping)
        else:
            tl.note("no `ping` binary found — the ICMP loop is disabled. This is the "
                    "cheapest 'when exactly did the link go quiet' signal, so install "
                    "iputils-ping before the real run if you can.")

    if args.neigh_interval > 0:
        if ip_bin:
            loops.append(NeighLoop(tl, stop_evt, args.neigh_interval, ref, ip_bin,
                                   max(args.neigh_edge_interval, 0.2)))
        else:
            tl.note("no `ip` binary found — ARP/neighbour tracking disabled")

    if discover_loop is not None:
        loops.append(discover_loop)

    tail = None
    if args.tail_interval > 0:
        tail = TailLoop(tl, stop_evt, args.tail_interval, ref, args.stdout_log,
                        args.tail_from_start)
        loops.append(tail)

    wifi_iface = None
    if args.wifi_interval > 0:
        wifi_iface = _pick_wifi_iface(tl, ip_bin, iw_bin, iwconfig_bin, args.wifi_iface)
        if wifi_iface:
            loops.append(WifiLoop(tl, stop_evt, args.wifi_interval, iw_bin,
                                  iwconfig_bin, wifi_iface))

    # Both marker channels share one sink so markers are numbered in the order
    # they actually happened, whichever way they arrived. Either channel may be
    # a no-op (no terminal / no --marker-file) without affecting the other.
    marker_sink = MarkerSink(tl)
    marker = MarkerThread(tl, stop_evt, marker_sink)
    marker.start()
    if args.marker_file and args.marker_file_interval > 0:
        loops.append(MarkerFileLoop(tl, stop_evt, args.marker_file_interval,
                                    args.marker_file, marker_sink))
    elif not args.marker_file:
        tl.note("no --marker-file given — markers can only come from a typed "
                "stdin, so a headless run of this collector would have no way "
                "to record where the robot physically was")

    for lp in loops:
        lp.start()

    # Main thread does nothing but the periodic rollup, so Ctrl-C is always
    # responsive no matter what a probe thread is blocked on.
    last_rollup = time.monotonic()
    while not stop_evt.is_set():
        stop_evt.wait(0.5)
        if args.rollup_interval > 0 and args.stderr_echo != "none":
            now = time.monotonic()
            if now - last_rollup >= args.rollup_interval:
                last_rollup = now
                print(f"[{_ts()}] .. {_rollup(tl, ping)}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------------ #
    # Shutdown + summary
    # ------------------------------------------------------------------ #
    for lp in loops:
        lp.join(timeout=2.0)
    if tail is not None:
        try:
            tail.flush_remainder()
        except Exception as e:  # noqa: BLE001
            tl.error("stdout_tail", e, where="flush_remainder")

    dur = time.time() - started
    pstats = ping.snapshot() if ping else {}
    disc = next((lp for lp in loops if isinstance(lp, DiscoverLoop)), None)
    neigh = next((lp for lp in loops if isinstance(lp, NeighLoop)), None)
    health = next((lp for lp in loops if isinstance(lp, HealthLoop)), None)
    wifi = next((lp for lp in loops if isinstance(lp, WifiLoop)), None)

    loss_pct = (100.0 * pstats["fail"] / pstats["total"]) if pstats.get("total") else None
    summary = {
        "duration_s": round(dur, 1),
        "events_by_source": dict(tl.counts),
        "ping": {**pstats,
                 "loss_pct": round(loss_pct, 2) if loss_pct is not None else None},
        "neigh_changes": getattr(neigh, "changes", None),
        "neigh_mac_changes": getattr(neigh, "mac_changes", None),
        "health_flips": getattr(health, "flips", None),
        "discover_scans": getattr(disc, "scans", None),
        "discover_divergences": getattr(disc, "divergences", None),
        "first_divergence": getattr(disc, "first_divergence", None),
        "host_roams": getattr(wifi, "roams", None),
        "stdout_lines": getattr(tail, "lines", None),
        "markers": marker_sink.count,   # both channels, one number
    }
    tl.emit("collector", "summary", **summary)
    _banner([
        f"COLLECTION FINISHED after {dur / 60:.1f} min — timeline: {out}",
        f"  events: {json.dumps(dict(tl.counts))}",
        (f"  ping: {pstats.get('ok')}/{pstats.get('total')} ok "
         f"({loss_pct:.1f}% loss), {pstats.get('outages')} outage(s), longest "
         f"{pstats.get('longest_outage_s', 0):.1f}s"
         if pstats.get("total") else "  ping: no samples"),
        f"  health flips: {summary['health_flips']}   neigh changes: "
        f"{summary['neigh_changes']} (MAC changes: {summary['neigh_mac_changes']})",
        f"  discovery: {summary['discover_scans']} scans, "
        f"{summary['discover_divergences']} IP divergence(s)"
        + (f" — FIRST: tracked {summary['first_divergence'][0]} vs discovered "
           f"{summary['first_divergence'][1]}"
           if summary["first_divergence"] else ""),
        f"  host AP roams: {summary['host_roams']}   stdout lines captured: "
        f"{summary['stdout_lines']}   markers: {summary['markers']}",
        "",
        "  Next: sort the JSONL by `t` and read the window around the first "
        "health flip. The question to answer FIRST is which came first —",
        "  ping loss (network failed) or health staleness (session died on a "
        "healthy network).",
        "  Nothing was sent to the robot and nothing was restarted by this tool.",
    ])
    tl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

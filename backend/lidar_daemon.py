#!/usr/bin/env python3
"""Persistent lidar daemon — serves the robot's live lidar point cloud as a
compact raw binary buffer for a custom WebGL renderer in the dashboard.

Runs under the dimOS conda env's OWN python (it needs `dimos` — the control-panel
venv can never import it). Spawned as a subprocess by dimos_cli.py's `_LidarDaemon`
manager using the conda interpreter, mirroring camera_daemon.py in lifecycle
discipline (spawned lazily / on launch, tracked by PID, killed on
run/stop/restart so it never outlives its robot connection).

WHY THIS EXISTS (not Rerun): dimOS's bundled Rerun web viewer renders the point
cloud only inside a generic multi-panel debugger UI with a fixed camera framing
that can't be URL-driven — it reads as "a stupid sim", not a demo. This daemon
instead extracts the raw (N,3) float32 point array and serves it directly, so the
browser can render JUST the point cloud with height-colored points and orbit
controls in a hand-rolled WebGL canvas.

WHY A SEPARATE HTTP SERVER (like camera_daemon, unlike teleop/sport's stdin/stdout
JSON): a 20K+ point cloud is a large binary payload (~250 KB as float32 xyz) that
doesn't belong on a line-delimited-JSON pipe. This daemon connects to the running
dimOS app ONCE (paying the ~1.4-2s import + connect cost once), polls the 'lidar'
stream in a background thread, and serves the latest snapshot over its OWN tiny
stdlib HTTP server as a raw octet-stream. The browser fetches it directly; the
FastAPI panel only probes /health and hands the browser the port.

TOPIC NOTE: the live point-cloud topic is 'lidar' (via app.peek_stream('lidar')),
NOT '/pointcloud' — a separate '/pointcloud' topic is registered on the blueprint
but is DEAD (peek_stream times out, 0 data). Always use 'lidar'. The message is a
dimos.msgs.sensor_msgs.PointCloud2.PointCloud2; `.points_f32()` returns an (N,3)
float32 array in meters, robot frame. `.intensities_f32()` exists but returns
None-filled on this build, so we color by height (z) client-side instead.

ALSO SERVES POSE (x/y/yaw telemetry): the 'odom' topic (app.peek_stream('odom'))
returns a dimos.msgs.geometry_msgs.PoseStamped.PoseStamped with direct .x/.y/.z/
.yaw/.pitch/.roll properties (no quaternion math needed) — confirmed live. This
daemon already pays the one-time Dimos.connect() cost for the point cloud, so pose
polling piggybacks on the same connection/process instead of spawning a dedicated
5th daemon for a handful of floats.

ENDPOINTS (bind 0.0.0.0 so a browser on another LAN device can reach it):
  GET /points.bin -> application/octet-stream: raw little-endian float32, xyz
                     interleaved (3 floats/point). N = Content-Length / 12.
                     A stale/empty snapshot returns an empty body (0 points).
  GET /pose.json  -> JSON {ok, has_data, fresh, age_s, x, y, z, yaw, pitch, roll}
  GET /health     -> JSON liveness {ok, points, has_data, fresh, age_s, bbox, ...}

READINESS: one JSON line on stdout once the server is bound and the app is
connected: {"ready": true, "port": 8771, "topic": "lidar", "transport": ...}.
All diagnostics go to stderr; stdout carries only that single readiness line.
"""
import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class CloudHolder:
    """Newest point-cloud snapshot, shared between the poll thread (writer) and
    HTTP threads (readers). Stores the raw interleaved-xyz float32 bytes plus a
    little metadata so /health can report extent without re-parsing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = b""            # raw float32 xyz interleaved
        self._count = 0            # number of points in _buf
        self._updates = 0          # count of real snapshots stored
        self._last_ts = 0.0        # monotonic time of the last real snapshot
        self._bbox = None          # [xmin,ymin,zmin,xmax,ymax,zmax] or None

    def set_cloud(self, buf: bytes, count: int, bbox) -> None:
        with self._lock:
            self._buf = buf
            self._count = count
            self._updates += 1
            self._last_ts = time.monotonic()
            self._bbox = bbox

    def raw(self):
        with self._lock:
            return self._buf, self._count

    def stats(self):
        with self._lock:
            age = (time.monotonic() - self._last_ts) if self._updates else None
            return {
                "points": self._count,
                "has_data": self._updates > 0 and self._count > 0,
                "fresh": bool(self._updates and age is not None and age <= 3.0),
                "age_s": round(age, 3) if age is not None else None,
                "updates": self._updates,
                "bbox": self._bbox,
            }


class PoseHolder:
    """Newest x/y/z/yaw/pitch/roll snapshot — tiny, so just store the values
    directly under a lock rather than bytes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vals = None          # dict of x/y/z/yaw/pitch/roll or None
        self._updates = 0
        self._last_ts = 0.0

    def set_pose(self, vals: dict) -> None:
        with self._lock:
            self._vals = vals
            self._updates += 1
            self._last_ts = time.monotonic()

    def stats(self):
        with self._lock:
            age = (time.monotonic() - self._last_ts) if self._updates else None
            out = {
                "has_data": self._updates > 0 and self._vals is not None,
                "fresh": bool(self._updates and age is not None and age <= 3.0),
                "age_s": round(age, 3) if age is not None else None,
                "updates": self._updates,
            }
            if self._vals:
                out.update(self._vals)
            return out


def _build_handler(holder: CloudHolder, pose_holder: PoseHolder, topic: str, transport_name: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence stdlib access logging (keep stdout clean)
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/points.bin", "/points", "/"):
                return self._points()
            if path == "/health":
                return self._health()
            if path == "/pose.json":
                return self._pose()
            self.send_error(404, "not found")

        def _pose(self):
            body = json.dumps({"ok": True, **pose_holder.stats()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _points(self):
            buf, _count = holder.raw()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(buf)))
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if buf:
                self.wfile.write(buf)

        def _health(self):
            body = json.dumps({
                "ok": True, "topic": topic, "transport": transport_name,
                **holder.stats(),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="lidar")
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--transport", default=None,
                    help="lcm|zenoh — must match the running blueprint's transport")
    ap.add_argument("--interval", type=float, default=0.3,
                    help="min seconds between lidar polls")
    ap.add_argument("--peek-timeout", type=float, default=1.5,
                    help="peek_stream timeout per poll (seconds)")
    args = ap.parse_args()

    t0 = time.time()
    # --- warm dimOS + connect to the running app (the ~1.4-2s cost, paid once) ---
    try:
        from dimos.core.global_config import global_config
        # Mirror the CLI's `--transport` global override (same as the other
        # daemons): update global_config BEFORE connecting.
        if args.transport:
            global_config.update(transport=args.transport)
        from dimos import Dimos
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"import/init failed: {e!r}"}),
              flush=True)
        return 1

    try:
        app = Dimos.connect()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"connect failed: {e!r}"}),
              flush=True)
        return 1

    transport_name = str(getattr(global_config, "transport", "lcm"))
    holder = CloudHolder()
    pose_holder = PoseHolder()
    stop_evt = threading.Event()

    def poll_loop():
        """Background poll: peek the lidar stream, extract (N,3) float32, store
        interleaved-xyz bytes. One bad/empty poll never kills the loop."""
        errors = 0
        while not stop_evt.is_set():
            loop_start = time.monotonic()
            try:
                v = app.peek_stream(args.topic, args.peek_timeout)
                pts = v.points_f32() if v is not None else None
                if pts is not None:
                    # points_f32() is an (N,3) float32 array. Serialize contiguously.
                    # Avoid a hard numpy dependency assumption: use the buffer
                    # protocol via memoryview when possible, else fall back.
                    try:
                        import numpy as np
                        a = np.ascontiguousarray(np.asarray(pts, dtype=np.float32))
                        if a.ndim == 2 and a.shape[1] == 3 and a.shape[0] > 0:
                            n = int(a.shape[0])
                            buf = a.tobytes()
                            xmin = float(a[:, 0].min()); xmax = float(a[:, 0].max())
                            ymin = float(a[:, 1].min()); ymax = float(a[:, 1].max())
                            zmin = float(a[:, 2].min()); zmax = float(a[:, 2].max())
                            holder.set_cloud(
                                buf, n, [xmin, ymin, zmin, xmax, ymax, zmax])
                    except Exception as inner:  # noqa: BLE001
                        errors += 1
                        if errors <= 3 or errors % 200 == 0:
                            _log(f"parse error #{errors}: {inner!r}")
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 3 or errors % 200 == 0:
                    _log(f"peek error #{errors}: {e!r}")
                # peek_stream already blocked for its timeout on failure; small
                # backoff so a hard-down stream doesn't spin.
                stop_evt.wait(0.2)
            # Pace to at least --interval between poll starts.
            elapsed = time.monotonic() - loop_start
            if elapsed < args.interval:
                stop_evt.wait(args.interval - elapsed)

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    def pose_poll_loop():
        """Background poll: peek 'odom', store x/y/z/yaw/pitch/roll. Independent
        cadence from the point-cloud poll — pose is cheap, poll it faster."""
        errors = 0
        while not stop_evt.is_set():
            loop_start = time.monotonic()
            try:
                v = app.peek_stream("odom", args.peek_timeout)
                if v is not None:
                    pose_holder.set_pose({
                        "x": float(v.x), "y": float(v.y), "z": float(v.z),
                        "yaw": float(v.yaw), "pitch": float(v.pitch), "roll": float(v.roll),
                    })
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 3 or errors % 200 == 0:
                    _log(f"pose peek error #{errors}: {e!r}")
                stop_evt.wait(0.2)
            elapsed = time.monotonic() - loop_start
            pose_interval = 0.5  # faster than the point-cloud poll; pose is cheap
            if elapsed < pose_interval:
                stop_evt.wait(pose_interval - elapsed)

    pose_poll_thread = threading.Thread(target=pose_poll_loop, daemon=True)
    pose_poll_thread.start()

    # --- HTTP server ---
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer(
            (args.host, args.port),
            _build_handler(holder, pose_holder, args.topic, transport_name))
        httpd.daemon_threads = True
    except OSError as e:
        stop_evt.set()
        print(json.dumps({"ready": False,
                          "error": f"bind {args.host}:{args.port} failed: {e!r}"}),
              flush=True)
        return 1

    def _shutdown(_signum, _frame):
        stop_evt.set()
        try:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Single readiness line (stdout) — the manager blocks on this, same protocol
    # shape as the camera daemon.
    print(json.dumps({
        "ready": True,
        "warmup_s": round(time.time() - t0, 3),
        "port": args.port,
        "host": args.host,
        "topic": args.topic,
        "transport": transport_name,
    }), flush=True)

    _log(f"lidar daemon serving point clouds on {args.host}:{args.port} "
         f"for '{args.topic}' ({transport_name})")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_evt.set()
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Persistent YOLO collision-corridor watch daemon — an INDEPENDENT, camera-based
obstacle trigger complementing the lidar-based nav stack.

WHY THIS EXISTS: dimOS's autonomous nav drove the Go2 into a table with zero
response and had to be physically powered off — the lidar obstacle map never
registered the table (thin legs / wrong height band), and dimOS's execution layer
has no stall/contact detection at all. The external odometry watchdog in
test_mls_nav_supervised.py bounds how long the robot can push against something,
but it can't prevent initial contact. A camera looking at the SAME space ahead of
the robot is a genuinely different sensor with different blind spots: where lidar
missed the thin table legs, an RGB detector sees the table as an object. This
daemon is that second, independent check.

DELIBERATELY THE FAST/SIMPLE VERSION: no 3D projection, no floor-plane math, no
fusion with the lidar map (a separate parallel effort owns the heavy "real fusion"
version). This is a coarse safety trigger, not a precise localizer. It runs
YOLO11n on the live color feed and asks one question: does any detected object's
bounding box overlap a fixed "collision corridor" region (the lower-center of the
frame — the patch of floor/space directly ahead of the robot)? If so, it reports
`obstacle_in_corridor: true`. The consumer (the nav watchdog) decides what to do.

SAFETY POSTURE: false positives here just cause an unnecessary stop (safe); false
negatives mean a missed catch (the exact failure we're preventing). So defaults
lean toward firing: a low confidence threshold (0.25) and "any bbox intersection
with the corridor counts". Tune via CLI if it's too twitchy.

ARCHITECTURE (mirrors lidar_daemon.py / camera_daemon.py exactly): runs under the
dimOS conda env's OWN python (needs `import dimos`). Connects to the running app
ONCE via `Dimos.connect()` (paying the ~1.4-2s import+connect cost once), then a
background thread repeatedly `peek_stream('color_image', ...)`s the live feed,
converts each frame to a BGR ndarray via the Image object's `.to_opencv()` (the
same conversion the dimOS YOLO detector itself uses — see
dimos/perception/detection/detectors/yolo.py), runs YOLO, computes corridor
overlap, and stores the result. HTTP threads serve that latest result over a tiny
stdlib server. One bad/empty poll never kills the loop.

NOTE ON REUSING dimOS's Detection2DModule: its detector (Yolo2DDetector) is NOT
cleanly reusable standalone — it resolves its weights through `get_data("models_yolo")`,
which is a git-LFS test-data fetch, and Detection2DModule itself is a full dimOS
Module that must be wired into a coordinator with a CameraInfo config. For this
standalone fast-path daemon we call `ultralytics.YOLO(...)` directly on the frames
we already have, which is what Yolo2DDetector does internally anyway
(`self.model.track(source=image.to_opencv(), ...)`). Weights: a yolo11n.pt is
staged next to this file (--model overrides); if absent, ultralytics auto-downloads
"yolo11n.pt" on load.

ENDPOINTS (bind 0.0.0.0 so a browser/consumer on another LAN device can reach it):
  GET /health -> JSON {ok, obstacle_in_corridor, detections:[...], age_s,
                       frames_processed, corridor, fresh, ...}

READINESS: one JSON line on stdout once the model is loaded, the app is connected,
and the server is bound: {"ready": true, "port": 8097, "topic": "/color_image", ...}.
All diagnostics go to stderr; stdout carries only that single readiness line.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolo11n.pt")


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class DetectionHolder:
    """Newest detection result, shared between the poll thread (writer) and HTTP
    threads (readers). Stores the corridor verdict, the detection list, and a bit
    of metadata so /health can report without re-running anything."""

    def __init__(self, corridor):
        self._lock = threading.Lock()
        self._obstacle = False          # obstacle_in_corridor for the last frame
        self._detections = []           # list of detection dicts
        self._frames = 0                # frames actually run through YOLO
        self._last_ts = 0.0             # monotonic time of the last processed frame
        self._corridor = corridor       # [x0,y0,x1,y1] in pixels, or None until known
        self._frame_wh = None           # (w,h) of the last processed frame

    def set_result(self, obstacle, detections, corridor, frame_wh):
        with self._lock:
            self._obstacle = bool(obstacle)
            self._detections = detections
            self._corridor = corridor
            self._frame_wh = frame_wh
            self._frames += 1
            self._last_ts = time.monotonic()

    def stats(self):
        with self._lock:
            age = (time.monotonic() - self._last_ts) if self._frames else None
            return {
                # NOTE: obstacle_in_corridor is reported as-is (the last processed
                # frame's verdict). A stale feed is surfaced via `fresh`/`age_s`;
                # the consumer must treat a non-fresh reading as "unknown", not
                # "clear" — see the watchdog integration.
                "obstacle_in_corridor": self._obstacle,
                "detections": self._detections,
                "frames_processed": self._frames,
                "has_data": self._frames > 0,
                "fresh": bool(self._frames and age is not None and age <= 2.0),
                "age_s": round(age, 3) if age is not None else None,
                "corridor": self._corridor,
                "frame_wh": self._frame_wh,
            }


def _corridor_for(w, h, center_frac, bottom_frac):
    """Pixel rectangle [x0,y0,x1,y1] for the collision corridor: the lower
    `bottom_frac` of the frame, centered horizontally spanning `center_frac` of
    the width. Coarse by design."""
    half = center_frac / 2.0
    x0 = int((0.5 - half) * w)
    x1 = int((0.5 + half) * w)
    y0 = int((1.0 - bottom_frac) * h)
    y1 = int(h)
    return [x0, y0, x1, y1]


def _overlaps(box, corridor):
    """Axis-aligned rectangle intersection test. box/corridor = [x0,y0,x1,y1].
    Any positive-area overlap counts (conservative: touching edges do too via >=)."""
    bx0, by0, bx1, by1 = box
    cx0, cy0, cx1, cy1 = corridor
    return not (bx1 < cx0 or bx0 > cx1 or by1 < cy0 or by0 > cy1)


def _build_handler(holder, topic, transport_name, model_name):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence stdlib access logging (keep stdout clean)
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/"):
                return self._health()
            self.send_error(404, "not found")

        def _health(self):
            body = json.dumps({
                "ok": True, "topic": topic, "transport": transport_name,
                "model": model_name, **holder.stats(),
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
    ap.add_argument("--topic", default="/color_image")
    ap.add_argument("--port", type=int, default=8097)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--transport", default=None,
                    help="lcm|zenoh — must match the running blueprint's transport")
    ap.add_argument("--model", default=_DEFAULT_MODEL,
                    help="path to a YOLO .pt (falls back to auto-download 'yolo11n.pt' if missing)")
    ap.add_argument("--conf", type=float, default=0.25,
                    help="YOLO confidence threshold — LOW on purpose (err toward firing)")
    ap.add_argument("--iou", type=float, default=0.6)
    ap.add_argument("--interval", type=float, default=0.25,
                    help="min seconds between detection passes (~4Hz default)")
    ap.add_argument("--peek-timeout", type=float, default=1.5,
                    help="peek_stream timeout per poll (seconds)")
    ap.add_argument("--corridor-center-frac", type=float, default=0.5,
                    help="fraction of frame WIDTH the corridor spans, centered")
    ap.add_argument("--corridor-bottom-frac", type=float, default=0.34,
                    help="fraction of frame HEIGHT (from bottom) the corridor spans")
    args = ap.parse_args()

    t0 = time.time()
    # --- warm dimOS + connect (the ~1.4-2s cost, paid once) ---
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

    # --- load YOLO (independent of the robot connection) ---
    try:
        from ultralytics import YOLO
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
        model_path = args.model if os.path.exists(args.model) else "yolo11n.pt"
        model = YOLO(model_path, task="detect")
        # Warm the model once on a dummy frame so the first REAL frame isn't slow
        # (first CUDA inference is ~2.7s; warm is ~5ms).
        try:
            import numpy as np
            model.predict(source=np.zeros((480, 640, 3), dtype=np.uint8),
                          device=device, conf=args.conf, iou=args.iou, verbose=False)
        except Exception as e:  # noqa: BLE001
            _log(f"model warmup pass failed (non-fatal): {e!r}")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"YOLO load failed: {e!r}"}),
              flush=True)
        return 1

    try:
        app = Dimos.connect()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"connect failed: {e!r}"}),
              flush=True)
        return 1

    transport_name = str(getattr(global_config, "transport", "lcm"))
    holder = DetectionHolder(corridor=None)
    stop_evt = threading.Event()

    def process_frame(bgr):
        """Run YOLO on a BGR ndarray, return (obstacle, detections, corridor, wh)."""
        h, w = bgr.shape[:2]
        corridor = _corridor_for(w, h, args.corridor_center_frac, args.corridor_bottom_frac)
        r = model.predict(source=bgr, device=device, conf=args.conf,
                          iou=args.iou, verbose=False)[0]
        detections = []
        obstacle = False
        boxes = getattr(r, "boxes", None)
        names = getattr(r, "names", {}) or {}
        if boxes is not None and len(boxes) > 0:
            xyxy = boxes.xyxy.tolist()
            confs = boxes.conf.tolist()
            clss = boxes.cls.tolist()
            for box, cf, cl in zip(xyxy, confs, clss):
                box = [float(v) for v in box]
                in_corr = _overlaps(box, corridor)
                if in_corr:
                    obstacle = True
                detections.append({
                    "cls": int(cl),
                    "label": names.get(int(cl), str(int(cl))),
                    "conf": round(float(cf), 3),
                    "bbox": [round(v, 1) for v in box],
                    "in_corridor": in_corr,
                })
        return obstacle, detections, corridor, [w, h]

    def poll_loop():
        errors = 0
        while not stop_evt.is_set():
            loop_start = time.monotonic()
            try:
                # peek_stream() matches a module's stream ATTRIBUTE name (e.g.
                # "color_image"), not the wire topic string (e.g. "/color_image")
                # -- unlike make_transport(), which camera_daemon.py uses and does
                # want the full topic. --topic keeps its display/CLI form; strip
                # the leading slash only for this call.
                v = app.peek_stream(args.topic.lstrip("/"), args.peek_timeout)
                if v is not None:
                    bgr = v.to_opencv()  # BGR ndarray, same path dimOS's YOLO uses
                    obstacle, detections, corridor, wh = process_frame(bgr)
                    holder.set_result(obstacle, detections, corridor, wh)
            except Exception as e:  # noqa: BLE001
                errors += 1
                if errors <= 3 or errors % 200 == 0:
                    _log(f"detect poll error #{errors}: {e!r}")
                stop_evt.wait(0.2)
            elapsed = time.monotonic() - loop_start
            if elapsed < args.interval:
                stop_evt.wait(args.interval - elapsed)

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # --- HTTP server ---
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer(
            (args.host, args.port),
            _build_handler(holder, args.topic, transport_name, os.path.basename(model_path)))
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

    # Single readiness line (stdout) — a supervising launcher blocks on this, same
    # protocol shape as the camera / lidar daemons.
    print(json.dumps({
        "ready": True,
        "warmup_s": round(time.time() - t0, 3),
        "port": args.port,
        "host": args.host,
        "topic": args.topic,
        "transport": transport_name,
        "model": os.path.basename(model_path),
        "device": device,
    }), flush=True)

    _log(f"yolo watch daemon serving /health on {args.host}:{args.port} "
         f"for {args.topic} ({transport_name}) model={os.path.basename(model_path)} device={device}")
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

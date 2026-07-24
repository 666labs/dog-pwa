#!/usr/bin/env python3
"""Persistent camera daemon — a plain, low-latency MJPEG feed of /color_image.

Runs under the dimOS conda env's OWN python (it needs dimos.msgs.* and
dimos.core.transport_factory — the control-panel venv never imports dimOS). It
is spawned as a subprocess by dimos_cli.py's `_CameraDaemon` manager using the
conda interpreter, mirroring teleop_daemon.py / sport_daemon.py in lifecycle
discipline (spawned lazily / on launch, tracked by PID, killed on
run/stop/restart so it never outlives its robot connection).

WHY A SEPARATE HTTP SERVER (not the stdin/stdout JSON protocol the teleop/sport
daemons use): camera frames are large binary payloads. Piping JPEG frames
through a line-delimited-JSON stdout channel to FastAPI, then re-streaming them,
would be slow and awkward. Instead this daemon subscribes to /color_image ONCE
(paying dimOS import cost once, then holding a warm LCM/Zenoh subscription with
its own background handler thread), JPEG-encodes each decoded frame, and serves
it directly over its OWN tiny stdlib HTTP server as an
`multipart/x-mixed-replace` MJPEG stream. The browser's <img> points straight at
it; the FastAPI panel only probes /health and hands the browser the port.

CHANNEL: /color_image carries dimos.msgs.sensor_msgs.Image.Image over a typed
LCMTransport — verified live from the running blueprint's own transport log:
  topic=/color_image#sensor_msgs.Image transport=LCMTransport type=...Image.Image
`make_transport("/color_image", Image)` reconstructs exactly that channel, so a
read-only subscriber here decodes the same frames the vis module renders. A
subscription is inherently non-disruptive — it never affects the publisher.

ENDPOINTS (bind 0.0.0.0 so a browser on another LAN device can reach it, same as
the panel's own server):
  GET /stream.mjpg  -> multipart/x-mixed-replace MJPEG stream (the <img> src)
  GET /snapshot.jpg -> the single latest frame as image/jpeg
  GET /health       -> JSON liveness {ok, frames, has_real_frame, age_s, ...}

READINESS: one JSON line on stdout once the server is bound and the subscription
is live: {"ready": true, "port": 8096, "topic": "/color_image", "transport": ...}.
All diagnostics go to stderr; stdout carries only that single readiness line.
"""
import argparse
import importlib
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _encode_jpeg(img, quality: int) -> bytes:
    """Encode a dimOS Image to JPEG bytes via OpenCV.

    NOT Image.to_jpeg_bytes(): that path uses TurboJPEG, whose native
    libturbojpeg shared library is NOT locatable in this conda env (the python
    `turbojpeg` wrapper imports, but `TurboJPEG()` raises "Unable to locate
    turbojpeg library automatically") — confirmed live, it produced zero
    encoded frames. The Image class's own `to_base64` uses cv2.imencode on the
    BGR array, and cv2 (4.13) works here — so we mirror that path exactly and
    return raw bytes instead of base64."""
    bgr = img.to_bgr().to_opencv()
    ok, buf = cv2.imencode(".jpg", bgr,
                           [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ValueError("cv2.imencode failed")
    return buf.tobytes()


class FrameHolder:
    """Newest-JPEG-frame slot, shared between the LCM handler thread (writer) and
    any number of HTTP streaming threads (readers). A single Condition both
    guards the slot and wakes readers the instant a new frame lands, so the
    stream pushes at the source frame rate with no polling latency."""

    def __init__(self, placeholder: bytes):
        self._cond = threading.Condition()
        self._jpeg = placeholder      # bytes currently served
        self._seq = 0                 # bumped on every real frame
        self._real_frames = 0         # count of genuine camera frames seen
        self._last_real_ts = 0.0      # monotonic time of the last real frame
        self._placeholder = placeholder

    def set_frame(self, jpeg: bytes):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._real_frames += 1
            self._last_real_ts = time.monotonic()
            self._cond.notify_all()

    def wait_for(self, last_seq: int, timeout: float):
        """Block until a newer frame than last_seq, or timeout. Returns
        (jpeg_bytes, seq). On timeout returns the current slot unchanged so the
        stream can keep the connection alive (and swap to the placeholder if the
        real feed has gone stale)."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout)
            # Serve placeholder if the real feed has gone stale (>2s silent).
            stale = (self._real_frames == 0
                     or (time.monotonic() - self._last_real_ts) > 2.0)
            jpeg = self._placeholder if stale else self._jpeg
            return jpeg, self._seq

    def snapshot(self):
        with self._cond:
            stale = (self._real_frames == 0
                     or (time.monotonic() - self._last_real_ts) > 2.0)
            return self._placeholder if stale else self._jpeg

    def stats(self):
        with self._cond:
            age = (time.monotonic() - self._last_real_ts) if self._real_frames else None
            return {
                "frames": self._real_frames,
                "has_real_frame": self._real_frames > 0,
                "fresh": bool(self._real_frames and age is not None and age <= 2.0),
                "age_s": round(age, 3) if age is not None else None,
            }


def _make_placeholder(text: str, w: int = 640, h: int = 480) -> bytes:
    """A dark 'waiting for camera' JPEG so the <img> always has something to
    show before (or between) real frames — no broken-image icon."""
    img = np.full((h, w, 3), 24, dtype=np.uint8)  # near-black
    cv2.putText(img, text, (28, h // 2), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (170, 170, 170), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return buf.tobytes() if ok else b""


def _build_handler(holder: FrameHolder, topic: str, transport_name: str):
    BOUNDARY = "frame"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence stdlib access logging (keep stdout clean)
            pass

        def _cors(self):
            # The browser loads this cross-origin (panel on :8090, stream on
            # :8096), but a plain <img> is not gated by CORS. Headers are added
            # for any fetch()-based probe convenience; harmless for <img>.
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/stream.mjpg", "/stream"):
                return self._stream()
            if path in ("/snapshot.jpg", "/snapshot"):
                return self._snapshot()
            if path == "/health":
                return self._health()
            self.send_error(404, "not found")

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

        def _snapshot(self):
            jpeg = holder.snapshot()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpeg)

        def _stream(self):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self._cors()
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            last_seq = -1
            try:
                while True:
                    jpeg, last_seq = holder.wait_for(last_seq, timeout=1.0)
                    # One MJPEG part. Written as a single buffer to avoid partial
                    # writes interleaving across the (threaded) connections.
                    part = (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii") + jpeg + b"\r\n"
                    self.wfile.write(part)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # client closed the <img> / navigated away — normal

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="/color_image")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--transport", default=None,
                    help="lcm|zenoh — must match the running blueprint's transport")
    ap.add_argument("--quality", type=int, default=70,
                    help="JPEG quality 0-100")
    ap.add_argument("--max-width", type=int, default=0,
                    help="downscale frames wider than this (0 = no downscale)")
    args = ap.parse_args()

    t0 = time.time()
    # --- warm dimOS + build the subscription (the ~1.4-2s import cost, paid once) ---
    try:
        from dimos.core.global_config import global_config
        # Mirror the CLI's `--transport` global override exactly (same as the
        # teleop/sport daemons): update global_config BEFORE building transport.
        if args.transport:
            global_config.update(transport=args.transport)
        Image = getattr(
            importlib.import_module("dimos.msgs.sensor_msgs.Image"), "Image")
        from dimos.core.transport_factory import make_transport
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"import/init failed: {e!r}"}),
              flush=True)
        return 1

    quality = int(max(0, min(100, args.quality)))
    max_width = args.max_width if args.max_width and args.max_width > 0 else None

    try:
        placeholder = _make_placeholder("waiting for camera…")
    except Exception as e:  # noqa: BLE001
        _log(f"placeholder generation failed (non-fatal): {e!r}")
        placeholder = b""
    holder = FrameHolder(placeholder)

    encode_errors = {"n": 0}

    def on_image(msg):
        """LCM handler-thread callback: decode -> JPEG -> publish to readers.
        Any per-frame failure is swallowed (logged sparsely) so one bad frame
        never kills the warm subscription."""
        try:
            img = msg
            if max_width is not None and getattr(img, "width", 0) > max_width:
                # resize_to_fit preserves aspect ratio, returns (image, scale).
                try:
                    img, _scale = img.resize_to_fit(max_width, 100000)
                except Exception:  # noqa: BLE001
                    img = msg
            jpeg = _encode_jpeg(img, quality)
            holder.set_frame(jpeg)
        except Exception as e:  # noqa: BLE001
            encode_errors["n"] += 1
            if encode_errors["n"] <= 3 or encode_errors["n"] % 200 == 0:
                _log(f"frame decode/encode error #{encode_errors['n']}: {e!r}")

    try:
        transport = make_transport(args.topic, Image)
        transport.subscribe(on_image)  # starts the LCM background handler thread
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"subscribe failed: {e!r}"}),
              flush=True)
        return 1

    transport_name = str(getattr(global_config, "transport", "lcm"))

    # --- HTTP server ---
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer(
            (args.host, args.port),
            _build_handler(holder, args.topic, transport_name))
        httpd.daemon_threads = True
    except OSError as e:
        print(json.dumps({"ready": False,
                          "error": f"bind {args.host}:{args.port} failed: {e!r}"}),
              flush=True)
        return 1

    # Clean shutdown on SIGTERM/SIGINT (the manager sends SIGTERM to the group).
    def _shutdown(_signum, _frame):
        try:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Single readiness line (stdout) — the manager blocks on this, same protocol
    # shape as the teleop/sport daemons.
    print(json.dumps({
        "ready": True,
        "warmup_s": round(time.time() - t0, 3),
        "port": args.port,
        "host": args.host,
        "topic": args.topic,
        "transport": transport_name,
    }), flush=True)

    _log(f"camera daemon serving MJPEG on {args.host}:{args.port} "
         f"for {args.topic} ({transport_name})")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

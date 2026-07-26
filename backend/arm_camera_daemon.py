#!/usr/bin/env python3
"""Arm workcell USB camera daemon — MJPEG server over a local OpenCV capture.

No dimOS dependency: reuses FrameHolder/_build_handler/_make_placeholder from
camera_daemon.py (pure cv2/numpy/stdlib — the dimOS imports there live inside
its main()). Spawned by dimos_cli._ArmCameraDaemon under ARM_CAM_PY (defaults
to the dimOS conda python, which has cv2). Unlike the go2 camera daemon it is
NOT tied to a robot connection — the source is a local USB device, so it lives
for the server's lifetime and is only killed on /api/server/shutdown.

ENDPOINTS: same as camera_daemon (/stream.mjpg /snapshot.jpg /health).
READINESS: single stdout JSON line {"ready": true, "port": ..., "device": ...}
printed as soon as the HTTP server is bound — the camera itself may attach
later; the placeholder frame is served until it does, and the capture loop
retries the device every second (USB unplug/replug self-heals).
"""
import argparse
import json
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import cv2

from camera_daemon import FrameHolder, _build_handler, _log, _make_placeholder


def _capture_loop(holder: FrameHolder, args, stop: threading.Event) -> None:
    cap = None
    failures = 0
    while not stop.is_set():
        if cap is None:
            cap = cv2.VideoCapture(args.device)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
                _log(f"arm camera device {args.device} opened")
            else:
                cap.release()
                cap = None
                time.sleep(1.0)
                continue
        ok, frame = cap.read()
        if not ok:
            failures += 1
            if failures <= 3 or failures % 100 == 0:
                _log(f"arm camera read failed #{failures}; reopening")
            cap.release()
            cap = None
            time.sleep(0.5)
            continue
        failures = 0
        ok2, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(args.quality)])
        if ok2:
            holder.set_frame(buf.tobytes())
        time.sleep(1.0 / args.fps)
    if cap is not None:
        cap.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--port", type=int, default=8772,
                    help="0 = OS-assigned (tests)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=float, default=15.0)
    args = ap.parse_args()

    holder = FrameHolder(_make_placeholder("waiting for arm camera..."))
    stop = threading.Event()
    threading.Thread(target=_capture_loop, args=(holder, args, stop),
                     daemon=True).start()

    try:
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer(
            (args.host, args.port),
            _build_handler(holder, f"usb:{args.device}", "usb"))
        httpd.daemon_threads = True
    except OSError as e:
        print(json.dumps({"ready": False, "error": f"bind failed: {e!r}"}),
              flush=True)
        return 1

    def _shutdown(_signum, _frame):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(json.dumps({"ready": True, "port": httpd.server_address[1],
                      "device": args.device}), flush=True)
    _log(f"arm camera daemon on {args.host}:{httpd.server_address[1]} "
         f"device={args.device}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

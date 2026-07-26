"""Arm camera daemon 冒烟测试 — 无摄像头设备也必须服务占位帧。"""
import json
import os
import signal
import subprocess
import sys
import urllib.request

import pytest

pytest.importorskip("cv2")

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")


def test_placeholder_without_device():
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND, "arm_camera_daemon.py"),
         "--device", "99", "--port", "0", "--host", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=BACKEND)
    try:
        info = json.loads(proc.stdout.readline())
        assert info.get("ready") is True
        port = info["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            h = json.loads(r.read())
        assert h["ok"] is True and h["has_real_frame"] is False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot.jpg", timeout=3) as r:
            jpg = r.read()
        assert jpg.startswith(b"\xff\xd8")  # JPEG SOI — 占位帧
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

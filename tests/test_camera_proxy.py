"""摄像头代理路由测试 — 本地 stub daemon 替真身，不碰机器人/摄像头。"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

FAKE_JPEG = b"\xff\xd8\xffFAKEJPEG\xff\xd9"


class _StubCam(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True, "fresh": True, "frames": 7}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/snapshot.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FAKE_JPEG)))
            self.end_headers()
            self.wfile.write(FAKE_JPEG)
        else:
            self.send_error(404)


@pytest.fixture()
def stub_port():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubCam)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture()
def client(stub_port, monkeypatch):
    import dimos_cli
    import main
    monkeypatch.setattr(dimos_cli, "ARM_CAMERA_PORT", stub_port)
    monkeypatch.setattr(dimos_cli, "arm_camera_ensure",
                        lambda device=0: {"ok": True, "port": stub_port})
    return TestClient(main.app)


def test_arm_snapshot_proxy(client):
    r = client.get("/api/camera/arm/snapshot.jpg")
    assert r.status_code == 200
    assert r.content == FAKE_JPEG
    assert r.headers["content-type"].startswith("image/jpeg")


def test_arm_health_proxy(client):
    h = client.get("/api/camera/arm/health").json()
    assert h["ok"] is True and h["fresh"] is True


def test_unknown_cam(client):
    assert client.get("/api/camera/nope/health").json()["ok"] is False


def test_go2_health_no_robot(client, monkeypatch):
    import dimos_cli
    monkeypatch.setattr(dimos_cli, "status", lambda: {"running": False})
    h = client.get("/api/camera/go2/health").json()
    assert h["ok"] is False

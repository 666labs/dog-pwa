"""Vendor 状态机测试 — 全部走 VENDOR_FAKE_DOG=1 模拟路径，无机器人。"""
import importlib
import json
import os
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)


def make_app(tmp_path, monkeypatch):
    cfg = {
        "drinks": [{"id": "cola", "name": "可乐", "color": "#e0312e", "arm_action": "pick_slot_1"}],
        "table": {"x": 1.0, "y": 0.0},
        "station": {"x": 0.0, "y": 0.0},
        "arm_stub_delay_s": 0.05,
        "arrival_radius_m": 0.35,
        "nav_timeout_s": 5,
        "result_display_s": 0.2,
    }
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("VENDOR_CONFIG_PATH", str(p))
    monkeypatch.setenv("VENDOR_FAKE_DOG", "1")
    monkeypatch.setenv("VENDOR_FAKE_DOG_DIST", "1.0")
    monkeypatch.setenv("VENDOR_FAKE_DOG_SPEED", "8.0")
    import vendor
    importlib.reload(vendor)  # 每个测试拿到全新的 OrderManager
    app = FastAPI()
    app.include_router(vendor.router)
    return app


def wait_state(client, target, timeout=5.0):
    s = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/vendor/status").json()
        if s["state"] == target:
            return s
        time.sleep(0.05)
    raise AssertionError(f"state never reached {target!r}, last={s}")


def test_menu(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        drinks = client.get("/api/vendor/menu").json()["drinks"]
        assert drinks[0]["id"] == "cola"
        assert "arm_action" not in drinks[0]  # 内部字段不外泄


def test_unknown_drink_404(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/vendor/order", data={"drink_id": "nope"}).status_code == 404


def test_full_flow(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/vendor/order", data={"drink_id": "cola"})
        assert r.status_code == 200 and r.json()["order_id"] == 1
        wait_state(client, "awaiting_pickup")
        # 送达途中/等待确认时再点单 → 409
        assert client.post("/api/vendor/order", data={"drink_id": "cola"}).status_code == 409
        assert client.post("/api/vendor/confirm").status_code == 200
        wait_state(client, "delivered")
        wait_state(client, "idle")


def test_confirm_wrong_state_409(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/vendor/confirm").status_code == 409


def test_reset_from_awaiting(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/vendor/order", data={"drink_id": "cola"})
        wait_state(client, "awaiting_pickup")
        assert client.post("/api/vendor/reset").json()["state"] == "idle"
        # 复位后能再点
        assert client.post("/api/vendor/order", data={"drink_id": "cola"}).status_code == 200

# Vendor 任务控制台（双模式）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 vendor 顾客页升级为任务控制台：双路摄像头（Go2 + 臂 USB 相机）实时画面、E-stop 急停、事件时间线/遥测/小地图/导航日志，且 Vercel 页支持 `?backend=` 隧道双模式。

**Architecture:** 后端在 `vendor.py` 加 `estopped` 状态 + 事件缓冲 + pose/map，`main.py` 加 CORS 与摄像头代理路由（隧道只暴露 8090），新 `arm_camera_daemon.py`（USB→MJPEG :8772）由 `dimos_cli._ArmCameraDaemon` 管理。前端合并两份 vendor.js 为共享核心 `vendor-app.js`（RealBackend/MockBackend 适配层），`demo-site/` 持拷贝。

**Tech Stack:** FastAPI、stdlib urllib 代理、OpenCV（仅 daemon）、原生 JS + canvas（无构建工具）。

**验证约束:** 机器人无电——真机项（Go2 相机出画面、真导航急停、Ascent USB 设备号）延后；其余全部本地验证。

**设计文档:** `docs/superpowers/specs/2026-07-25-vendor-console-design.md`

---

### Task 1: vendor.py — estopped 状态 + 事件缓冲 + pose/map

**Files:**
- Modify: `backend/vendor.py`
- Test: `tests/test_vendor.py`

- [ ] **Step 1.1: 写失败测试**（追加到 `tests/test_vendor.py`）

```python
def test_estop_from_active(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/vendor/order", data={"drink_id": "cola"})
        wait_state(client, "dog_delivering")
        r = client.post("/api/vendor/estop")
        assert r.status_code == 200 and r.json()["state"] == "estopped"
        # 急停期间不可下单
        assert client.post("/api/vendor/order", data={"drink_id": "cola"}).status_code == 409
        # reset 不能解除急停
        assert client.post("/api/vendor/reset").json()["state"] == "estopped"
        # 显式解除后恢复
        assert client.post("/api/vendor/estop/release").status_code == 200
        assert client.get("/api/vendor/status").json()["state"] == "idle"
        assert client.post("/api/vendor/order", data={"drink_id": "cola"}).status_code == 200


def test_estop_idempotent_from_idle(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/vendor/estop").json()["state"] == "estopped"
        assert client.post("/api/vendor/estop").json()["state"] == "estopped"  # 再按仍 200
        assert client.post("/api/vendor/estop/release").status_code == 200


def test_estop_release_wrong_state_409(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/vendor/estop/release").status_code == 409


def test_events_pose_map_in_status(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post("/api/vendor/order", data={"drink_id": "cola"})
        st = wait_state(client, "awaiting_pickup")
        keys = [e["key"] for e in st["events"]]
        for want in ("order_placed", "arm_pick_start", "arm_pick_done",
                     "nav_start", "nav_arrived", "awaiting_pickup"):
            assert want in keys, f"missing event {want}: {keys}"
        assert st["pose"] is not None and "x" in st["pose"]  # fake 腿合成位姿
        assert st["map"]["table"] == {"x": 1.0, "y": 0.0}
        assert all("ts" in e and "zh" in e and "en" in e for e in st["events"])
```

- [ ] **Step 1.2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_vendor.py -x -q`
Expected: 新增 4 个测试 FAIL（404 /api/vendor/estop 或 KeyError 'events'）

- [ ] **Step 1.3: 实现**（`backend/vendor.py`）

顶部：`import collections`；常量区加：

```python
ESTOPPED = "estopped"
```

`_NAV_LEG_LOG` 改为环境可覆盖（Task 2 测试需要）：

```python
_NAV_LEG_LOG = os.environ.get("VENDOR_NAV_LOG", "/tmp/vendor-nav-leg.log")
```

`OrderManager.__init__` 追加：

```python
        self.pose: Optional[dict] = None
        self.events: collections.deque = collections.deque(maxlen=60)
        self._event_seq = 0
        self.estopped_at: Optional[float] = None
```

新增 `_event`，`_set` 的清理元组加 `ESTOPPED`：

```python
    def _event(self, key: str, zh: str, en: str, data: Optional[dict] = None) -> None:
        self._event_seq += 1
        self.events.append({"seq": self._event_seq, "ts": time.time(),
                            "key": key, "zh": zh, "en": en, "data": data or {}})
```

```python
        if state in (IDLE, DELIVERED, FAILED, ESTOPPED):
            self.dist_to_goal = None
```

`status()` 返回值追加（map 读 config，失败置 None——status 永不抛错）：

```python
        try:
            cfg = load_config()
            map_info = {"station": cfg.get("station"), "table": cfg.get("table"),
                        "arrival_radius_m": cfg.get("arrival_radius_m", 0.35)}
        except Exception:  # noqa: BLE001
            map_info = None
        return {
            ...原有字段...,
            "pose": self.pose,
            "map": map_info,
            "events": list(self.events),
            "estopped_at": self.estopped_at,
        }
```

`place_order` 在 `self._set(ARM_PICKING)` 前加：

```python
        self._event("order_placed", f"已接单：{self.drink['name']}",
                    f"Order received: {self.drink.get('name_en') or self.drink['id']}",
                    {"order_id": self.order_seq})
```

`_run_order` 在各阶段落点插入事件（完整新体）：

```python
    async def _run_order(self, drink: dict, cfg: dict) -> None:
        display = float(cfg.get("result_display_s", 6))
        try:
            self._set(ARM_PICKING)
            self._event("arm_pick_start", "机械臂开始取货", "Arm pick started",
                        {"arm_action": drink.get("arm_action")})
            await self._arm_pick(drink, cfg)
            self._event("arm_pick_done", "机械臂取货完成，已装载", "Arm pick done — loaded")
            self._set(DOG_DELIVERING)
            t = cfg["table"]
            self._event("nav_start", f"导航启动 → 桌位 ({t['x']}, {t['y']})",
                        f"Nav started → table ({t['x']}, {t['y']})", {"goal": t})
            await self._dog_go_to(t, cfg)
            self._event("nav_arrived", "已到达桌位", "Arrived at table")
            self._set(AWAITING_PICKUP)
            self._event("awaiting_pickup", "等待顾客取货", "Awaiting pickup")
            self._confirm = asyncio.Event()
            await self._confirm.wait()
            self._event("confirmed", "顾客已确认取货", "Pickup confirmed")
            self._set(DOG_RETURNING)
            s = cfg["station"]
            self._event("nav_start", f"返程 → 取餐站 ({s['x']}, {s['y']})",
                        f"Returning → station ({s['x']}, {s['y']})", {"goal": s})
            await self._dog_go_to(s, cfg)
            self._event("nav_arrived", "已回到取餐站", "Back at station")
            self._set(DELIVERED)
            self._event("delivered", "订单完成", "Delivered")
            await asyncio.sleep(display)
            if self.state == DELIVERED:
                self._set(IDLE)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  — 任何失败都进 failed 并展示原因
            self._set(FAILED, error=str(e))
            self._event("failed", f"失败：{e}", f"Failed: {e}")
            await asyncio.sleep(display)
            if self.state == FAILED:
                self._set(IDLE)
```

`_dog_go_to` 的 `_read()` 里 pose 事件同时回填位姿：

```python
                    if ev.get("event") == "pose":
                        self.dist_to_goal = ev.get("dist")
                        if "x" in ev and "y" in ev:
                            self.pose = {"x": ev["x"], "y": ev["y"]}
```

`_fake_go_to` 换为合成位姿版（起点=当前位姿或站点，向目标直线插值）：

```python
    async def _fake_go_to(self, goal: dict, cfg: dict) -> None:
        """Dry-run：以恒定速度逼近目标并合成直线位姿，不碰 dimos。"""
        dist0 = float(os.environ.get("VENDOR_FAKE_DOG_DIST", "3.0"))
        speed = float(os.environ.get("VENDOR_FAKE_DOG_SPEED", "0.6"))
        radius = float(cfg.get("arrival_radius_m", 0.35))
        step = 0.25
        start = self.pose or dict(cfg.get("station", {"x": 0.0, "y": 0.0}))
        sx, sy = float(start.get("x", 0.0)), float(start.get("y", 0.0))
        gx, gy = float(goal["x"]), float(goal["y"])
        dist = dist0
        while dist > radius:
            await asyncio.sleep(step)
            dist = max(0.0, dist - speed * step)
            self.dist_to_goal = round(dist, 3)
            f = 1.0 - min(1.0, dist / dist0) if dist0 > 0 else 1.0
            self.pose = {"x": round(sx + (gx - sx) * f, 3),
                         "y": round(sy + (gy - sy) * f, 3)}
```

新增 estop 方法（放在 `reset` 前）：

```python
    async def estop(self) -> dict:
        """急停：终止订单流程、杀导航子进程、连发零速。必须永远成功、幂等。"""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        proc = self._nav_proc
        if proc is not None and proc.returncode is None:
            proc.kill()
        already = self.state == ESTOPPED
        self._set(ESTOPPED)
        if not already:
            self.estopped_at = time.time()
            self._event("estop", "🛑 紧急停止已触发", "EMERGENCY STOP triggered")
        if not _env_flag("VENDOR_FAKE_DOG"):
            asyncio.create_task(self._estop_burst())
        return self.status()

    async def _estop_burst(self) -> None:
        """1 秒内连发 5 帧零速 Twist——触发 dimOS MovementManager._cancel_goal
        并压住任何残余速度指令。任何异常吞掉：急停不能因通信问题半途而废。"""
        try:
            cfg = load_config()
            st = await asyncio.to_thread(dimos_cli.status)
            if not st.get("running"):
                return
            topic = cfg.get("cancel_topic", "/tele_cmd_vel")
            transport = cfg.get("transport") or st.get("transport")
            for _ in range(5):
                await asyncio.to_thread(dimos_cli.teleop_send, 0, 0, 0, 0, 0, 0,
                                        topic=topic, transport=transport)
                await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass

    async def estop_release(self) -> bool:
        if self.state != ESTOPPED:
            return False
        self.estopped_at = None
        self._set(IDLE)
        self.drink = None
        self._event("estop_release", "急停已解除", "E-stop released")
        return True
```

`reset()` 开头加急停守卫 + 末尾记事件：

```python
    async def reset(self) -> dict:
        if self.state == ESTOPPED:
            return self.status()  # 急停语义强于复位——必须显式 release
        ...原有内容不变...
        self._set(IDLE)
        self.drink = None
        self._event("reset", "已复位", "Reset")
        return self.status()
```

`api_vendor_order` 在 `_ACTIVE` 检查前加：

```python
    if manager.state == ESTOPPED:
        return JSONResponse(status_code=409, content={
            "error": "estopped", "current": manager.status()})
```

路由区追加：

```python
@router.post("/api/vendor/estop")
async def api_vendor_estop():
    return await manager.estop()


@router.post("/api/vendor/estop/release")
async def api_vendor_estop_release():
    if not await manager.estop_release():
        return JSONResponse(status_code=409, content={
            "error": "not_estopped", "state": manager.state})
    return {"ok": True}
```

- [ ] **Step 1.4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_vendor.py -x -q`
Expected: 全部 PASS（原有 5 + 新增 4）

- [ ] **Step 1.5: Commit**

```bash
git add backend/vendor.py tests/test_vendor.py
git commit -m "feat(vendor): estop state machine + event timeline + pose/map in status"
```

---

### Task 2: navlog 端点

**Files:**
- Modify: `backend/vendor.py`
- Test: `tests/test_vendor.py`

- [ ] **Step 2.1: 写失败测试**（追加）

```python
def test_navlog_tail(tmp_path, monkeypatch):
    log = tmp_path / "nav.log"
    log.write_text("line1\nline2\nline3\n")
    monkeypatch.setenv("VENDOR_NAV_LOG", str(log))
    app = make_app(tmp_path, monkeypatch)  # make_app 里 reload(vendor) 会重读环境
    with TestClient(app) as client:
        assert client.get("/api/vendor/navlog?lines=2").json()["lines"] == ["line2", "line3"]


def test_navlog_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDOR_NAV_LOG", str(tmp_path / "absent.log"))
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/vendor/navlog").json()["lines"] == []
```

- [ ] **Step 2.2: 确认失败** — Run: `./venv/bin/python -m pytest tests/test_vendor.py -x -q` → 404 FAIL

- [ ] **Step 2.3: 实现**（`backend/vendor.py` 路由区追加）

```python
@router.get("/api/vendor/navlog")
def api_vendor_navlog(lines: int = 40):
    """nav_leg 日志尾巴 — 前端折叠面板的原始技术输出。"""
    n = max(1, min(int(lines), 200))
    try:
        with open(_NAV_LEG_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        tail = []
    return {"lines": tail[-n:]}
```

- [ ] **Step 2.4: 确认通过** — 同命令全 PASS
- [ ] **Step 2.5: Commit** — `git add backend/vendor.py tests/test_vendor.py && git commit -m "feat(vendor): navlog tail endpoint"`

---

### Task 3: arm_camera_daemon.py + dimos_cli 管理器

**Files:**
- Create: `backend/arm_camera_daemon.py`
- Modify: `backend/dimos_cli.py`（`_LidarDaemon` 定义之后、文件尾部工具函数区）
- Modify: `backend/main.py`（`/api/server/shutdown` 处加 `arm_camera_kill()`）
- Test: `tests/test_arm_camera_daemon.py`

- [ ] **Step 3.1: 写测试**（新文件 `tests/test_arm_camera_daemon.py`；无 cv2 自动 skip）

```python
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
```

- [ ] **Step 3.2: 实现 daemon**（`backend/arm_camera_daemon.py` 完整新文件）

```python
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
```

- [ ] **Step 3.3: 跑 daemon 测试**

Run: `./venv/bin/python -m pytest tests/test_arm_camera_daemon.py -q`
Expected: venv 无 cv2 → SKIP；随后 `./venv/bin/pip install opencv-python-headless` 后重跑 → PASS（Task 8 也会复跑）

- [ ] **Step 3.4: dimos_cli 管理器**（加在 lidar daemon 段之后，镜像 `_CameraDaemon`）

```python
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
            return {"ok": False, "error": "arm camera daemon did not signal readiness"}
        try:
            info = json.loads(ready_line)
        except json.JSONDecodeError:
            self._kill_locked()
            return {"ok": False, "error": f"bad readiness line: {ready_line!r}"}
        if not info.get("ready"):
            self._kill_locked()
            return {"ok": False, "error": info.get("error", "arm camera daemon init failed")}
        self.ready = True
        return {"ok": True, "port": self.port, "device": device}

    def ensure(self, device: int = 0) -> Dict[str, object]:
        with self.lock:
            if self._alive() and self.device == device and self.ready:
                return {"ok": True, "port": self.port, "device": device, "already": True}
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
```

- [ ] **Step 3.5: server shutdown 时终止臂相机**（`backend/main.py` `/api/server/shutdown` handler 内、现有 daemon 清理处并列加一行）

```python
    dimos_cli.arm_camera_kill()
```

（执行时先读该 handler 现状，与既有清理调用并列摆放；若无类似调用则放在 shutdown 动作前。）

- [ ] **Step 3.6: Commit**

```bash
git add backend/arm_camera_daemon.py backend/dimos_cli.py backend/main.py tests/test_arm_camera_daemon.py
git commit -m "feat(camera): arm workcell USB camera daemon + manager (port 8772)"
```

---

### Task 4: main.py — CORS + 摄像头代理路由

**Files:**
- Modify: `backend/main.py`
- Test: `tests/test_camera_proxy.py`

- [ ] **Step 4.1: 写失败测试**（新文件 `tests/test_camera_proxy.py`）

```python
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
```

- [ ] **Step 4.2: 确认失败** — Run: `./venv/bin/python -m pytest tests/test_camera_proxy.py -q` → 404 FAIL

- [ ] **Step 4.3: 实现**（`backend/main.py`）

imports 补 `Response` 与 CORS，`app = FastAPI(...)` 之后立刻：

```python
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
```

```python
# Vercel 双模式页跨域打隧道调本 API——黑客松场景直接全放开。
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
```

代理路由（放在 `/api/camera-stream` 段之后）：

```python
# --------------------------------------------------------------------------- #
# Camera proxy — 隧道只暴露 8090，浏览器到不了 daemon 端口，由这里转发。
# LAN 页同样走代理（省一套分支逻辑）。cam ∈ go2|arm。
# --------------------------------------------------------------------------- #
def _cam_port(cam: str):
    """Resolve+ensure the daemon behind a camera name → (port, err|None)."""
    if cam == "go2":
        st = dimos_cli.status()
        if not st.get("running"):
            return None, {"ok": False, "reason": "no robot run is active"}
        ens = dimos_cli.camera_ensure(st.get("transport"))
        if not ens.get("ok"):
            return None, {"ok": False,
                          "reason": f"camera daemon failed: {ens.get('error')}"}
        return dimos_cli.CAMERA_STREAM_PORT, None
    if cam == "arm":
        import vendor
        try:
            device = int(vendor.load_config().get("arm_camera_device", 0))
        except Exception:  # noqa: BLE001
            device = 0
        ens = dimos_cli.arm_camera_ensure(device)
        if not ens.get("ok"):
            return None, {"ok": False,
                          "reason": f"arm camera daemon failed: {ens.get('error')}"}
        return dimos_cli.ARM_CAMERA_PORT, None
    return None, {"ok": False, "reason": f"unknown camera '{cam}'"}


def _local_open(port: int, path: str, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(f"http://localhost:{port}{path}", timeout=timeout)


@app.get("/api/camera/{cam}/stream.mjpg")
def api_camera_proxy_stream(cam: str):
    """MJPEG pass-through。同步生成器 → FastAPI 线程池，每个观看者占一个
    线程（现场 1-3 个客户端，够用）。上游断开即结束响应。"""
    port, err = _cam_port(cam)
    if err:
        return JSONResponse(status_code=503, content=err)
    try:
        upstream = _local_open(port, "/stream.mjpg", timeout=5)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"ok": False, "reason": str(e)})
    ctype = upstream.headers.get("Content-Type") \
        or "multipart/x-mixed-replace; boundary=frame"

    def gen():
        try:
            while True:
                chunk = upstream.read(16384)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(gen(), media_type=ctype,
                             headers={"Cache-Control": "no-store"})


@app.get("/api/camera/{cam}/snapshot.jpg")
def api_camera_proxy_snapshot(cam: str):
    port, err = _cam_port(cam)
    if err:
        return JSONResponse(status_code=503, content=err)
    try:
        with _local_open(port, "/snapshot.jpg", timeout=2.5) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"ok": False, "reason": str(e)})
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/camera/{cam}/health")
def api_camera_proxy_health(cam: str):
    """永远 200 JSON——前端相机徽章直接消费 ok/fresh 字段。"""
    port, err = _cam_port(cam)
    if err:
        return err
    try:
        with _local_open(port, "/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)}
```

- [ ] **Step 4.4: 确认通过** — `./venv/bin/python -m pytest tests/test_camera_proxy.py tests/test_vendor.py -q` 全 PASS
- [ ] **Step 4.5: Commit** — `git add backend/main.py tests/test_camera_proxy.py && git commit -m "feat(api): CORS + go2/arm camera proxy routes through :8090"`

---

### Task 5: frontend/vendor.html — 控制台布局壳

**Files:**
- Rewrite: `frontend/vendor.html`

- [ ] **Step 5.1: 完整替换文件内容**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>dimOS 饮料速递 · 任务控制台</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, "PingFang SC", "Noto Sans SC", sans-serif;
    background: #0e1116; color: #e8eaed;
    display: flex; flex-direction: column;
    user-select: none; -webkit-user-select: none;
  }
  .en { color: #7d879c; font-weight: 400; }

  header {
    padding: 14px 22px; display: flex; align-items: center; gap: 14px;
    border-bottom: 1px solid #1d2330; flex-wrap: wrap;
  }
  header h1 { font-size: 20px; font-weight: 700; letter-spacing: .5px; }
  header h1 .en { font-size: 14px; margin-left: 6px; }
  .badge {
    font-size: 12px; font-weight: 700; border-radius: 6px; padding: 3px 10px;
    border: 1px solid; letter-spacing: .5px;
  }
  .badge.live { color: #34c65a; border-color: #34c65a; }
  .badge.sim  { color: #d9a441; border-color: #d9a441; }
  .badge.off  { color: #e0655e; border-color: #e0655e; }
  #estopBtn {
    margin-left: auto; font-size: 17px; font-weight: 800; color: #fff;
    background: #c62828; border: 2px solid #ff5f52; border-radius: 12px;
    padding: 10px 20px; cursor: pointer; line-height: 1.2;
  }
  #estopBtn .en { color: #ffd7d4; font-size: 11px; display: block; }
  #estopBtn:active { filter: brightness(.8); }

  #simNote {
    padding: 8px 22px; font-size: 12px; color: #7d879c;
    background: #12161f; border-bottom: 1px solid #1d2330; display: none;
  }
  #simNote b { color: #9aa3b5; }
  #simNote a { color: #3d6ff2; }

  #console {
    flex: 1; display: grid; grid-template-columns: 400px 1fr;
    gap: 16px; padding: 16px 22px; min-height: 0; align-items: start;
    overflow: auto;
  }
  @media (max-width: 900px) { #console { grid-template-columns: 1fr; } }

  #sysline { font-size: 12px; color: #7d879c; padding: 4px 2px 10px; }

  /* ---- 菜单 ---- */
  #menu {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  }
  .card {
    border: 1px solid #232a3a; border-radius: 16px; background: #141926;
    padding: 20px 12px; text-align: center; cursor: pointer;
    transition: transform .12s ease;
    min-height: 170px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px;
  }
  .card:active { transform: scale(.96); }
  .card .dot {
    width: 62px; height: 62px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 32px; line-height: 1;
  }
  .card .name { font-size: 20px; font-weight: 700; }
  .card .name-en { font-size: 13px; color: #7d879c; margin-top: -4px; }
  .card .hint { font-size: 11px; color: #7d879c; }

  /* ---- 进度 ---- */
  #progress { display: none; flex-direction: column; gap: 16px; }
  #orderTitle { font-size: 20px; font-weight: 700; }
  #orderTitle .en { font-size: 14px; display: block; margin-top: 4px; }
  #stages { display: flex; flex-direction: column; gap: 8px; }
  .stage {
    display: flex; align-items: center; gap: 12px; padding: 8px 14px;
    border-radius: 10px; border: 1px solid #232a3a; background: #141926;
    font-size: 15px; color: #7d879c;
  }
  .stage .idx {
    width: 24px; height: 24px; border-radius: 50%; border: 2px solid #2b3040;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; flex-shrink: 0;
  }
  .stage .lbl { display: flex; flex-direction: column; }
  .stage .lbl .en { font-size: 11px; }
  .stage.done { color: #9aa3b5; }
  .stage.done .idx { border-color: #2ea84f; color: #2ea84f; }
  .stage.active { color: #fff; border-color: #3d6ff2; background: #17203a; }
  .stage.active .idx { border-color: #3d6ff2; color: #3d6ff2; }

  .bigBtn {
    font-size: 20px; font-weight: 700; color: #fff; border: none; cursor: pointer;
    border-radius: 14px; padding: 16px 30px; background: #2ea84f;
    display: none; line-height: 1.4;
  }
  .bigBtn .en { color: #d9f2e0; font-size: 13px; display: block; }
  .bigBtn:active { filter: brightness(.85); }
  #doneMark { display: none; font-size: 48px; text-align: center; }
  #failBox { display: none; flex-direction: column; gap: 12px; }
  #failMsg { color: #e0655e; font-size: 15px; }
  #resetBtn {
    font-size: 16px; color: #fff; background: #b0413b; border: none;
    border-radius: 10px; padding: 12px 26px; cursor: pointer; align-self: flex-start;
  }

  /* ---- 时间线 ---- */
  #timelineWrap h3, #liveCol h3 {
    font-size: 13px; color: #9aa3b5; font-weight: 700; margin: 14px 0 8px;
    letter-spacing: .5px;
  }
  #timeline {
    display: flex; flex-direction: column; gap: 4px;
    max-height: 300px; overflow-y: auto;
  }
  .ev {
    display: flex; align-items: baseline; gap: 10px; padding: 5px 10px;
    border-radius: 8px; font-size: 13px; color: #9aa3b5; background: #12161f;
  }
  .ev .t { font-family: ui-monospace, monospace; font-size: 11px; color: #4a5468; flex-shrink: 0; }
  .ev .en { font-size: 11px; }
  .ev.latest { background: #17203a; color: #fff; border: 1px solid #26314d; }
  .ev.bad { color: #ff8a80; }
  .tlEmpty { font-size: 12px; color: #4a5468; padding: 6px 10px; }

  /* ---- 实况列 ---- */
  .cams { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 560px) { .cams { grid-template-columns: 1fr; } }
  .cam {
    border: 1px solid #232a3a; border-radius: 12px; background: #0a0d12;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .camBody { aspect-ratio: 16 / 9; position: relative; background: #0a0d12; }
  .camBody img, .camBody canvas {
    position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
  }
  .cam figcaption {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: #9aa3b5; padding: 7px 10px;
    border-top: 1px solid #1d2330;
  }
  .chip {
    margin-left: auto; font-size: 10px; font-weight: 700; border-radius: 5px;
    padding: 1px 7px; border: 1px solid #2b3040; color: #7d879c;
  }
  .chip.live { color: #34c65a; border-color: #34c65a; }
  .chip.wait { color: #d9a441; border-color: #d9a441; }
  .chip.sim  { color: #d9a441; border-color: #d9a441; }
  .chip.off  { color: #e0655e; border-color: #e0655e; }

  #telemetry {
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-top: 12px;
  }
  @media (max-width: 560px) { #telemetry { grid-template-columns: repeat(3, 1fr); } }
  .tl {
    border: 1px solid #232a3a; border-radius: 10px; background: #141926;
    padding: 8px 10px; display: flex; flex-direction: column; gap: 2px;
  }
  .tl .k { font-size: 10px; color: #7d879c; letter-spacing: .5px; }
  .tl .v { font-size: 16px; font-weight: 700; font-family: ui-monospace, monospace; }

  #mapCanvas {
    width: 100%; height: 220px; margin-top: 12px;
    border: 1px solid #232a3a; border-radius: 12px; background: #0a0d12; display: block;
  }

  #navlogWrap { margin-top: 12px; }
  #navlogWrap summary { font-size: 13px; color: #9aa3b5; cursor: pointer; padding: 4px 0; }
  #navlogBox {
    font-family: ui-monospace, monospace; font-size: 11px; color: #7d879c;
    background: #0a0d12; border: 1px solid #232a3a; border-radius: 10px;
    padding: 10px; max-height: 180px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-all; margin-top: 6px;
  }

  /* ---- 急停遮罩 ---- */
  #estopOverlay {
    position: fixed; inset: 0; z-index: 50; display: none;
    background: rgba(112, 12, 12, .95);
    align-items: center; justify-content: center; text-align: center;
  }
  #estopOverlay .inner { display: flex; flex-direction: column; gap: 18px; align-items: center; }
  #estopOverlay .sign { font-size: 72px; }
  #estopOverlay h2 { font-size: 34px; letter-spacing: 2px; }
  #estopOverlay h2 .en { color: #ffb4ae; font-size: 18px; display: block; margin-top: 6px; }
  #estopTime { font-size: 13px; color: #ffb4ae; }
  #releaseBtn {
    font-size: 18px; font-weight: 700; color: #7a1210; background: #fff;
    border: none; border-radius: 12px; padding: 16px 34px; cursor: pointer;
  }
  #releaseBtn .en { color: #b0413b; font-size: 12px; display: block; }

  #miniReset {
    position: fixed; right: 16px; bottom: 14px; font-size: 12px; color: #4a5468;
    background: none; border: none; cursor: pointer; padding: 8px;
  }
  #toast {
    position: fixed; top: 18px; left: 50%; transform: translateX(-50%); z-index: 60;
    background: #17203a; border: 1px solid #3d6ff2; color: #fff;
    padding: 10px 22px; border-radius: 10px; font-size: 15px;
    opacity: 0; pointer-events: none; transition: opacity .25s; text-align: center;
  }
</style>
</head>
<body>
  <header>
    <h1>🤖 dimOS 饮料速递<span class="en">Drink Delivery</span></h1>
    <span id="badge" class="badge off">连接中…</span>
    <button id="estopBtn">🛑 急停<span class="en">EMERGENCY STOP</span></button>
  </header>
  <div id="simNote"></div>

  <main id="console">
    <section id="flowCol">
      <div id="sysline"></div>
      <div id="menu"></div>
      <div id="progress">
        <div id="orderTitle"></div>
        <div id="stages"></div>
        <div id="doneMark">🎉</div>
        <button id="confirmBtn" class="bigBtn">我已取到饮料 ✓<span class="en">I've got my drink</span></button>
        <div id="failBox">
          <div id="failMsg"></div>
          <button id="resetBtn">重置 Reset</button>
        </div>
      </div>
      <div id="timelineWrap">
        <h3>事件时间线 <span class="en">TIMELINE</span></h3>
        <div id="timeline"></div>
      </div>
    </section>

    <section id="liveCol">
      <div class="cams">
        <figure class="cam">
          <div class="camBody" id="camGo2Body"></div>
          <figcaption>🐕 Go2 视角 <span class="en">Go2 POV</span><span class="chip" id="camGo2Chip">—</span></figcaption>
        </figure>
        <figure class="cam">
          <div class="camBody" id="camArmBody"></div>
          <figcaption>🦾 机械臂工位 <span class="en">Arm workcell</span><span class="chip" id="camArmChip">—</span></figcaption>
        </figure>
      </div>
      <div id="telemetry">
        <div class="tl"><span class="k">位姿 X (m)</span><span class="v" id="tX">—</span></div>
        <div class="tl"><span class="k">位姿 Y (m)</span><span class="v" id="tY">—</span></div>
        <div class="tl"><span class="k">速度 SPEED</span><span class="v" id="tSpd">—</span></div>
        <div class="tl"><span class="k">距目标 DIST</span><span class="v" id="tDist">—</span></div>
        <div class="tl"><span class="k">电量 BATT</span><span class="v" id="tBatt">—</span></div>
      </div>
      <canvas id="mapCanvas"></canvas>
      <details id="navlogWrap">
        <summary>导航日志 <span class="en">NAV LOG</span></summary>
        <pre id="navlogBox"></pre>
      </details>
    </section>
  </main>

  <div id="estopOverlay">
    <div class="inner">
      <div class="sign">🛑</div>
      <h2>已紧急停止<span class="en">E-STOPPED</span></h2>
      <div id="estopTime"></div>
      <button id="releaseBtn">解除急停<span class="en">Release E-stop</span></button>
    </div>
  </div>

  <button id="miniReset" title="复位 Reset">复位 Reset</button>
  <div id="toast"></div>

<script>window.VENDOR_MODE = "real";</script>
<script src="vendor-app.js"></script>
</body>
</html>
```

- [ ] **Step 5.2: Commit** — `git add frontend/vendor.html && git commit -m "feat(ui): mission-console layout shell for vendor page"`

---

### Task 6: frontend/vendor-app.js — 共享核心（并删 frontend/vendor.js）

**Files:**
- Create: `frontend/vendor-app.js`
- Delete: `frontend/vendor.js`

- [ ] **Step 6.1: 写完整文件**（内容如下——RealBackend / MockBackend / 渲染 / 小地图 / 相机管理 / 急停 UI）

```js
"use strict";
/* Vendor 任务控制台共享核心 — LAN 真实页(frontend/) 与 Vercel 双模式页(demo-site/) 共用。
   模式解析:
     window.VENDOR_MODE === "real" → 同源 RealBackend("")
     window.VENDOR_MODE === "auto" → ?backend=<url>(记入 localStorage) → RealBackend(url)
                                     否则 MockBackend（浏览器内模拟）
   demo-site/vendor-app.js 是本文件的拷贝（demo-site/sync.sh 同步，勿单独改）。 */

const $ = (id) => document.getElementById(id);

const STAGES = [
  { key: "received",        zh: "已接单",       en: "Order received" },
  { key: "arm_picking",     zh: "机械臂取货中", en: "Arm picking up" },
  { key: "dog_delivering",  zh: "机器狗配送中", en: "Dog delivering" },
  { key: "awaiting_pickup", zh: "请取走饮料",   en: "Please take your drink" },
  { key: "dog_returning",   zh: "机器狗返程中", en: "Dog returning" },
  { key: "delivered",       zh: "已完成",       en: "Completed" },
];
const STAGE_INDEX = {
  arm_picking: 1, dog_delivering: 2, awaiting_pickup: 3,
  dog_returning: 4, delivered: 5,
};
const MOVING_STATES = ["dog_delivering", "dog_returning"];

/* ---------------- 模式解析 ---------------- */
function resolveMode() {
  if (window.VENDOR_MODE === "real") return { kind: "real", base: "" };
  const qp = new URLSearchParams(location.search);
  let base = qp.get("backend");
  if (base === "off" || base === "clear") {
    localStorage.removeItem("vendorBackend");
    base = null;
  } else if (base) {
    localStorage.setItem("vendorBackend", base);
  } else {
    base = localStorage.getItem("vendorBackend");
  }
  if (base) {
    if (!/^https?:\/\//i.test(base)) base = "https://" + base;
    return { kind: "real", base: base.replace(/\/+$/, "") };
  }
  return { kind: "mock", base: null };
}

/* ---------------- RealBackend ---------------- */
class RealBackend {
  constructor(base) {
    this.base = base;
    this.kind = "real";
    this.cameraKind = "url";
    this.live = false;
    this.battery = null;
    this._pollBattery();
    setInterval(() => this._pollBattery(), 10000);
  }
  async _pollBattery() {
    try {
      const j = await (await fetch(this.base + "/api/battery", { cache: "no-store" })).json();
      const v = (typeof j === "number") ? j : (j.value ?? j.soc ?? j.result ?? null);
      if (typeof v === "number" && v >= 0 && v <= 100) this.battery = v;
    } catch (e) { /* 电量非关键，静默 */ }
  }
  _post(path, data) {
    const opts = { method: "POST" };
    if (data) {
      const fd = new FormData();
      for (const [k, v] of Object.entries(data)) fd.append(k, v);
      opts.body = fd;
    }
    return fetch(this.base + path, opts);
  }
  async status() {
    try {
      const st = await (await fetch(this.base + "/api/vendor/status", { cache: "no-store" })).json();
      this.live = true;
      st.battery = this.battery;
      return st;
    } catch (e) {
      this.live = false;
      return null;
    }
  }
  async menu() { return (await (await fetch(this.base + "/api/vendor/menu")).json()).drinks; }
  order(id)      { return this._post("/api/vendor/order", { drink_id: id }); }
  confirm()      { return this._post("/api/vendor/confirm"); }
  reset()        { return this._post("/api/vendor/reset"); }
  estop()        { return this._post("/api/vendor/estop"); }
  estopRelease() { return this._post("/api/vendor/estop/release"); }
  async navlog() {
    try {
      const j = await (await fetch(this.base + "/api/vendor/navlog?lines=40", { cache: "no-store" })).json();
      return j.lines || [];
    } catch (e) { return []; }
  }
  camStreamUrl(cam)   { return `${this.base}/api/camera/${cam}/stream.mjpg`; }
  camSnapshotUrl(cam) { return `${this.base}/api/camera/${cam}/snapshot.jpg?t=${Date.now()}`; }
  async camHealth(cam) {
    try {
      return await (await fetch(`${this.base}/api/camera/${cam}/health`, { cache: "no-store" })).json();
    } catch (e) { return null; }
  }
}

/* ---------------- MockBackend（浏览器内模拟；与后端 status schema 一致） ---------------- */
class MockBackend {
  constructor() {
    this.kind = "mock";
    this.cameraKind = "canvas";
    this.live = true;
    this.drinks = [
      { id: "cola",   name: "可乐",   name_en: "Cola",   icon: "🥤", color: "#e0312e" },
      { id: "sprite", name: "雪碧",   name_en: "Sprite", icon: "🍋", color: "#2ea84f" },
      { id: "water",  name: "矿泉水", name_en: "Water",  icon: "💧", color: "#2e7de0" },
    ];
    this.map = { station: { x: 0, y: 0 }, table: { x: 2.6, y: 1.2 }, arrival_radius_m: 0.4 };
    this.speed = 0.6; this.armDelay = 3.0; this.displayS = 5.0;
    this.state = "idle"; this.orderSeq = 0; this.drink = null; this.error = null;
    this.distToGoal = null; this.pose = { ...this.map.station }; this.goal = null;
    this.battery = 87; this.events = []; this._eventSeq = 0;
    this.estoppedAt = null; this._afterArrive = null; this._armTimer = null;
    this._navlog = ["[sim] mock backend ready — 模拟后端就绪"];
    setInterval(() => this._tick(0.25), 250);
    setInterval(() => { this.battery = Math.max(5, this.battery - 0.05); }, 10000);
  }
  _event(key, zh, en) {
    this.events.push({ seq: ++this._eventSeq, ts: Date.now() / 1000, key, zh, en, data: {} });
    if (this.events.length > 60) this.events.shift();
  }
  _log(line) { this._navlog.push(line); if (this._navlog.length > 60) this._navlog.shift(); }
  _active() { return ["arm_picking", "dog_delivering", "awaiting_pickup", "dog_returning"].includes(this.state); }
  status() {
    return {
      state: this.state, order_id: this.orderSeq || null, drink: this.drink,
      error: this.error, dist_to_goal: this.distToGoal, fake_dog: true, sim: true,
      pose: { x: +this.pose.x.toFixed(3), y: +this.pose.y.toFixed(3) },
      map: this.map, events: this.events.slice(),
      battery: Math.round(this.battery), estopped_at: this.estoppedAt,
    };
  }
  menu() { return this.drinks; }
  order(id) {
    const d = this.drinks.find((x) => x.id === id);
    if (!d) return { status: 404, ok: false };
    if (this.state === "estopped" || this._active()) return { status: 409, ok: false };
    this.orderSeq += 1;
    this.drink = { id: d.id, name: d.name, name_en: d.name_en };
    this.error = null;
    this._event("order_placed", `已接单：${d.name}`, `Order received: ${d.name_en}`);
    this.state = "arm_picking";
    this._event("arm_pick_start", "机械臂开始取货", "Arm pick started");
    this._log(`[sim] arm pick ${d.id}`);
    this._armTimer = setTimeout(() => {
      this._armTimer = null;
      this._event("arm_pick_done", "机械臂取货完成，已装载", "Arm pick done — loaded");
      this._startLeg("dog_delivering", this.map.table, () => {
        this.state = "awaiting_pickup";
        this._event("awaiting_pickup", "等待顾客取货", "Awaiting pickup");
      });
    }, this.armDelay * 1000);
    return { status: 200, ok: true };
  }
  _startLeg(stateName, goal, onArrive) {
    this.state = stateName;
    this.goal = { ...goal };
    this._afterArrive = onArrive;
    this._event("nav_start", `导航启动 → (${goal.x}, ${goal.y})`, `Nav started → (${goal.x}, ${goal.y})`);
    this._log(`[sim] goal_sent x=${goal.x} y=${goal.y}`);
  }
  _tick(dt) {
    if (!this.goal || !MOVING_STATES.includes(this.state)) return;
    const dx = this.goal.x - this.pose.x, dy = this.goal.y - this.pose.y;
    const dist = Math.hypot(dx, dy);
    this.distToGoal = +dist.toFixed(3);
    this._log(`[sim] pose x=${this.pose.x.toFixed(2)} y=${this.pose.y.toFixed(2)} dist=${dist.toFixed(2)}`);
    if (dist <= this.map.arrival_radius_m) {
      this.goal = null;
      this.distToGoal = null;
      this._event("nav_arrived", "已到达目标", "Arrived");
      const cb = this._afterArrive;
      this._afterArrive = null;
      if (cb) cb();
      return;
    }
    const step = Math.min(dist, this.speed * dt);
    this.pose.x += (dx / dist) * step;
    this.pose.y += (dy / dist) * step;
  }
  confirm() {
    if (this.state !== "awaiting_pickup") return { status: 409, ok: false };
    this._event("confirmed", "顾客已确认取货", "Pickup confirmed");
    this._startLeg("dog_returning", this.map.station, () => {
      this.state = "delivered";
      this._event("delivered", "订单完成", "Delivered");
      setTimeout(() => { if (this.state === "delivered") this._toIdle(false); }, this.displayS * 1000);
    });
    return { status: 200, ok: true };
  }
  reset() {
    if (this.state === "estopped") return { status: 200, ok: true }; // 急停必须显式解除
    if (this._armTimer) { clearTimeout(this._armTimer); this._armTimer = null; }
    this._toIdle(true);
    return { status: 200, ok: true };
  }
  estop() {
    if (this._armTimer) { clearTimeout(this._armTimer); this._armTimer = null; }
    this.goal = null;
    this._afterArrive = null;
    if (this.state !== "estopped") {
      this.estoppedAt = Date.now() / 1000;
      this._event("estop", "🛑 紧急停止已触发", "EMERGENCY STOP triggered");
      this._log("[sim] ESTOP — zero-velocity burst sent");
    }
    this.state = "estopped";
    return { status: 200, ok: true };
  }
  estopRelease() {
    if (this.state !== "estopped") return { status: 409, ok: false };
    this.estoppedAt = null;
    this._event("estop_release", "急停已解除", "E-stop released");
    this._toIdle(false);
    return { status: 200, ok: true };
  }
  _toIdle(logReset) {
    this.state = "idle"; this.drink = null; this.error = null;
    this.distToGoal = null; this.goal = null;
    if (logReset) this._event("reset", "已复位", "Reset");
  }
  navlog() { return this._navlog.slice(-40); }
  camHealth() { return { ok: true, fresh: true, sim: true }; }

  /* ---- 模拟摄像头（canvas 绘制，10fps 由 App 驱动） ---- */
  drawCamera(cam, canvas) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height, t = Date.now() / 1000;
    ctx.fillStyle = "#0a0d12";
    ctx.fillRect(0, 0, w, h);
    if (cam === "go2") this._drawGo2(ctx, w, h, t);
    else this._drawArm(ctx, w, h, t);
    ctx.fillStyle = "rgba(217,164,65,.95)";
    ctx.font = "bold 12px monospace";
    ctx.fillText("SIM", w - 36, 18);
    ctx.fillStyle = "rgba(180,190,205,.8)";
    ctx.font = "11px monospace";
    ctx.fillText(new Date().toLocaleTimeString("zh-CN", { hour12: false }), 8, 16);
  }
  _drawGo2(ctx, w, h, t) {
    const moving = MOVING_STATES.includes(this.state);
    const bob = moving ? Math.sin(t * 6) * 4 : 0;
    const cx = w / 2 + (moving ? Math.sin(t * 2.1) * 10 : 0);
    const cy = h * 0.45 + bob;
    ctx.strokeStyle = "#1d2531";
    ctx.lineWidth = 1;
    for (let i = -6; i <= 6; i++) {                       // 放射地线
      ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.lineTo(w / 2 + i * (w / 7), h + 10); ctx.stroke();
    }
    const phase = moving ? (t * 1.5) % 1 : 0;
    for (let r = 0; r < 6; r++) {                          // 前进横线
      const f = (r + phase) / 6, y = cy + (h - cy) * f * f;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.strokeStyle = "#2a3547";                           // 走廊墙线
    ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(cx, cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(w, 0); ctx.lineTo(cx, cy); ctx.stroke();
    if (this.distToGoal != null) {                         // 目标标记，越近越大
      const near = 1 - Math.min(1, this.distToGoal / 4);
      const s = 8 + near * 60;
      ctx.strokeStyle = "#3d6ff2"; ctx.lineWidth = 2;
      ctx.strokeRect(cx - s / 2, cy - s, s, s);
      ctx.fillStyle = "#9db4f5"; ctx.font = "11px monospace";
      ctx.fillText(`goal ${this.distToGoal.toFixed(2)}m`, cx - s / 2, cy - s - 6);
    }
    if (this.state === "estopped") {
      ctx.fillStyle = "rgba(198,40,40,.28)"; ctx.fillRect(0, 0, w, h);
    }
    ctx.fillStyle = "#7d879c"; ctx.font = "11px monospace";
    ctx.fillText(`x=${this.pose.x.toFixed(2)} y=${this.pose.y.toFixed(2)} ${this.state}`, 8, h - 10);
  }
  _drawArm(ctx, w, h, t) {
    const picking = this.state === "arm_picking";
    const p = picking ? (Math.sin(t * 2.5 - Math.PI / 2) + 1) / 2   // 0→1 抓取摆动
                      : 0.08 + Math.sin(t * 0.8) * 0.03;            // 待机微晃
    const bx = w * 0.3, by = h * 0.78;
    ctx.strokeStyle = "#242e3e";                                    // 台面
    ctx.beginPath(); ctx.moveTo(0, by + 14); ctx.lineTo(w, by + 14); ctx.stroke();
    const tx = w * 0.72, ty = by - 26;                              // 目标饮料
    ctx.fillStyle = "#16324a"; ctx.fillRect(tx - 10, ty, 20, 40);
    ctx.strokeStyle = "#2e7de0"; ctx.strokeRect(tx - 10, ty, 20, 40);
    const a1 = -1.5 + p * 0.95, a2 = 0.9 - p * 1.15;                // 两段臂角
    const ex = bx + Math.cos(a1) * h * 0.34, ey = by + Math.sin(a1) * h * 0.34;
    const wx = ex + Math.cos(a1 + a2) * h * 0.30, wy = ey + Math.sin(a1 + a2) * h * 0.30;
    ctx.lineWidth = 7; ctx.lineCap = "round";
    ctx.strokeStyle = "#3a4burnish" === "never" ? "#0" : "#3a4a63";
    ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.strokeStyle = "#4d6285";
    ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(wx, wy); ctx.stroke();
    ctx.fillStyle = "#5a729c";                                      // 关节
    for (const [jx, jy] of [[bx, by], [ex, ey]]) {
      ctx.beginPath(); ctx.arc(jx, jy, 6, 0, Math.PI * 2); ctx.fill();
    }
    const g = picking ? 4 + (1 - p) * 8 : 10;                       // 夹爪开合
    ctx.lineWidth = 3; ctx.strokeStyle = "#9db4f5";
    ctx.beginPath(); ctx.moveTo(wx, wy); ctx.lineTo(wx + 12, wy - g); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(wx, wy); ctx.lineTo(wx + 12, wy + g); ctx.stroke();
    if (this.state === "estopped") {
      ctx.fillStyle = "rgba(198,40,40,.28)"; ctx.fillRect(0, 0, w, h);
    }
    ctx.fillStyle = "#7d879c"; ctx.font = "11px monospace";
    ctx.fillText(picking ? "picking..." : "standby", 8, h - 10);
  }
}

/* ---------------- App ---------------- */
const MODE = resolveMode();
const B = MODE.kind === "real" ? new RealBackend(MODE.base) : new MockBackend();

let menuLoaded = false;
let lastOrderId = null;
const trail = [];
let lastPose = null, lastPoseTs = 0, speedMps = null;
const camState = {
  go2: { mode: "mjpeg", timer: null, img: null, downTicks: 0 },
  arm: { mode: "mjpeg", timer: null, img: null, downTicks: 0 },
};
const camHealthCache = { go2: null, arm: null };
const CAM_DEFS = [
  { key: "go2", body: "camGo2Body", chip: "camGo2Chip" },
  { key: "arm", body: "camArmBody", chip: "camArmChip" },
];
let releaseArmed = false, releaseTimer = null;

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.style.opacity = "1";
  setTimeout(() => { el.style.opacity = "0"; }, 2600);
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function drinkLabel(drink) {
  if (!drink) return "";
  return drink.name_en ? `${drink.name} ${drink.name_en}` : drink.name;
}

async function loadMenu() {
  const drinks = await B.menu();
  const menu = $("menu");
  menu.innerHTML = "";
  for (const d of drinks) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      `<div class="dot" style="background:${d.color}33; border: 2px solid ${d.color}">${d.icon || "🥤"}</div>` +
      `<div class="name">${d.name}</div>` +
      `<div class="name-en">${d.name_en || ""}</div>` +
      `<div class="hint">点击下单 · Tap to order</div>`;
    card.addEventListener("click", () => order(d.id));
    menu.appendChild(card);
  }
  menuLoaded = true;
}

async function order(id) {
  const r = await B.order(id);
  if (r.status === 409) toast("有订单进行中或已急停 · Busy or e-stopped");
  else if (r.status !== 200) toast(`下单失败 Order failed (${r.status})`);
}

function renderStages(state) {
  const activeIdx = STAGE_INDEX[state] ?? 0;
  const box = $("stages");
  box.innerHTML = "";
  STAGES.forEach((s, i) => {
    const div = document.createElement("div");
    const cls = state === "delivered" || i < activeIdx ? "done"
              : i === activeIdx ? "active" : "";
    div.className = `stage ${cls}`;
    const mark = (cls === "done") ? "✓" : String(i + 1);
    div.innerHTML =
      `<span class="idx">${mark}</span>` +
      `<span class="lbl"><span>${s.zh}</span><span class="en">${s.en}</span></span>`;
    box.appendChild(div);
  });
}

function renderTimeline(events) {
  const box = $("timeline");
  if (!events || !events.length) {
    box.innerHTML = `<div class="tlEmpty">暂无事件 · No events yet</div>`;
    return;
  }
  box.innerHTML = events.slice().reverse().map((e, i) =>
    `<div class="ev${i === 0 ? " latest" : ""}${e.key === "estop" || e.key === "failed" ? " bad" : ""}">` +
    `<span class="t">${fmtTime(e.ts)}</span><span>${e.zh}</span><span class="en">${e.en}</span></div>`
  ).join("");
}

function renderTelemetry(st) {
  const pose = st.pose;
  $("tX").textContent = pose ? pose.x.toFixed(2) : "—";
  $("tY").textContent = pose ? pose.y.toFixed(2) : "—";
  $("tDist").textContent = st.dist_to_goal != null ? st.dist_to_goal.toFixed(2) + "m" : "—";
  const now = Date.now() / 1000;
  if (pose) {
    if (lastPose && now - lastPoseTs > 0.2) {
      speedMps = Math.hypot(pose.x - lastPose.x, pose.y - lastPose.y) / (now - lastPoseTs);
      lastPose = pose; lastPoseTs = now;
    } else if (!lastPose) { lastPose = pose; lastPoseTs = now; }
  }
  const moving = MOVING_STATES.includes(st.state);
  $("tSpd").textContent = (moving && speedMps != null) ? speedMps.toFixed(2) + "m/s" : "—";
  $("tBatt").textContent = st.battery != null ? Math.round(st.battery) + "%" : "—";
}

function drawMap(st) {
  const canvas = $("mapCanvas");
  const dpr = window.devicePixelRatio || 1;
  const cw = canvas.clientWidth || 300, ch = canvas.clientHeight || 220;
  if (canvas.width !== Math.round(cw * dpr)) {
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a0d12";
  ctx.fillRect(0, 0, cw, ch);
  const map = st && st.map;
  if (!map || !map.station || !map.table) {
    ctx.fillStyle = "#4a5468"; ctx.font = "12px sans-serif";
    ctx.fillText("无地图配置 · no map config", 12, 22);
    return;
  }
  const pts = [map.station, map.table, ...(st.pose ? [st.pose] : []), ...trail];
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const pad = 0.8;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const scale = Math.min(cw / (maxX - minX), ch / (maxY - minY));
  const ox = (cw - (maxX - minX) * scale) / 2, oy = (ch - (maxY - minY) * scale) / 2;
  const X = (x) => ox + (x - minX) * scale;
  const Y = (y) => ch - (oy + (y - minY) * scale);        // 世界 y 朝上 → canvas 翻转
  ctx.strokeStyle = "#161c26"; ctx.lineWidth = 1;         // 1m 网格
  for (let gx = Math.ceil(minX); gx <= maxX; gx++) {
    ctx.beginPath(); ctx.moveTo(X(gx), 0); ctx.lineTo(X(gx), ch); ctx.stroke();
  }
  for (let gy = Math.ceil(minY); gy <= maxY; gy++) {
    ctx.beginPath(); ctx.moveTo(0, Y(gy)); ctx.lineTo(cw, Y(gy)); ctx.stroke();
  }
  const sx = X(map.station.x), sy = Y(map.station.y);     // 站点 ▲
  ctx.fillStyle = "#2ea84f";
  ctx.beginPath(); ctx.moveTo(sx, sy - 8); ctx.lineTo(sx - 7, sy + 6);
  ctx.lineTo(sx + 7, sy + 6); ctx.closePath(); ctx.fill();
  ctx.fillStyle = "#7d879c"; ctx.font = "11px sans-serif";
  ctx.fillText("站 station", sx + 10, sy + 4);
  const tx = X(map.table.x), ty = Y(map.table.y);         // 桌 ■ + 到达圈
  ctx.fillStyle = "#3d6ff2"; ctx.fillRect(tx - 6, ty - 6, 12, 12);
  ctx.strokeStyle = "rgba(61,111,242,.4)";
  ctx.beginPath(); ctx.arc(tx, ty, (map.arrival_radius_m || 0.35) * scale, 0, Math.PI * 2); ctx.stroke();
  ctx.fillStyle = "#7d879c";
  ctx.fillText("桌 table", tx + 10, ty + 4);
  if (trail.length > 1) {                                 // 轨迹
    ctx.strokeStyle = "rgba(61,111,242,.7)"; ctx.lineWidth = 2;
    ctx.beginPath();
    trail.forEach((p, i) => {
      if (i) ctx.lineTo(X(p.x), Y(p.y)); else ctx.moveTo(X(p.x), Y(p.y));
    });
    ctx.stroke();
  }
  if (st.pose) {                                          // 狗 ● + 朝向 + 移动脉冲
    const px = X(st.pose.x), py = Y(st.pose.y);
    if (trail.length > 1) {
      const a = trail[trail.length - 2], b = trail[trail.length - 1];
      const ang = Math.atan2(Y(b.y) - Y(a.y), X(b.x) - X(a.x));
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(ang) * 12, py + Math.sin(ang) * 12); ctx.stroke();
    }
    ctx.fillStyle = st.state === "estopped" ? "#ff5f52" : "#fff";
    ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2); ctx.fill();
    if (MOVING_STATES.includes(st.state)) {
      const r = 8 + ((Date.now() / 100) % 10);
      ctx.strokeStyle = `rgba(255,255,255,${Math.max(0, 1 - r / 18)})`;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.stroke();
    }
  }
}

/* ---------------- 摄像头 ---------------- */
function setupCameras() {
  for (const def of CAM_DEFS) {
    const body = $(def.body);
    if (B.cameraKind === "canvas") {
      const cv = document.createElement("canvas");
      cv.width = 480; cv.height = 270;
      body.appendChild(cv);
      setInterval(() => B.drawCamera(def.key, cv), 100);
      const chip = $(def.chip);
      chip.textContent = "模拟 SIM";
      chip.className = "chip sim";
      continue;
    }
    const img = document.createElement("img");
    img.alt = def.key;
    body.appendChild(img);
    const cs = camState[def.key];
    cs.img = img;
    img.addEventListener("error", () => { if (cs.mode === "mjpeg") switchSnap(def); });
    startMjpeg(def);
    pollCamHealth(def);
    setInterval(() => pollCamHealth(def), 3000);
  }
}
function startMjpeg(def) {
  const cs = camState[def.key];
  cs.mode = "mjpeg";
  clearInterval(cs.timer);
  cs.timer = null;
  cs.img.src = B.camStreamUrl(def.key);
}
function switchSnap(def) {
  const cs = camState[def.key];
  if (cs.mode === "snap") return;
  cs.mode = "snap";
  clearInterval(cs.timer);
  cs.timer = setInterval(() => { cs.img.src = B.camSnapshotUrl(def.key); }, 500);
}
async function pollCamHealth(def) {
  const cs = camState[def.key];
  const h = await B.camHealth(def.key);
  camHealthCache[def.key] = h;
  const chip = $(def.chip);
  if (!h || h.ok === false) {
    chip.textContent = "离线 OFF";
    chip.className = "chip off";
    cs.downTicks += 1;
    if (cs.downTicks % 3 === 0) startMjpeg(def);  // 每 ~9s 试着重连
    return;
  }
  cs.downTicks = 0;
  chip.textContent = cs.mode === "snap" ? "快照 SNAP" : (h.fresh ? "实时 LIVE" : "等待帧 WAIT");
  chip.className = "chip " + (cs.mode === "snap" ? "wait" : (h.fresh ? "live" : "wait"));
}

/* ---------------- 渲染 ---------------- */
function renderSysline(st) {
  const el = $("sysline");
  if (!st) { el.textContent = "后端离线 · Backend offline"; return; }
  if (st.sim) { el.textContent = "模拟运行中 · Simulation — 点一杯试试 Tap a drink"; return; }
  const c = (h) => (h && h.ok !== false) ? (h.fresh ? "✓" : "…") : "×";
  el.textContent = `后端 ✓ · Go2相机 ${c(camHealthCache.go2)} · 臂相机 ${c(camHealthCache.arm)}`;
}

function renderSimNote(st) {
  const note = $("simNote");
  if (!note || window.VENDOR_MODE !== "auto") { if (note) note.style.display = "none"; return; }
  note.style.display = "block";
  if (MODE.kind === "mock") {
    note.innerHTML = "这是交互<b>模拟版</b>——真实系统运行在机器人现场局域网。" +
      "在 URL 后加 <b>?backend=&lt;隧道地址&gt;</b> 即可切换为实况直连。" +
      ' <span class="en">Interactive <b>simulation</b> — append <b>?backend=&lt;tunnel URL&gt;</b> to go live.</span>';
  } else {
    note.innerHTML = `已连接真实后端 <b>${MODE.base}</b>` +
      ` · <a href="?backend=off">断开 disconnect</a>`;
  }
}

function render(st) {
  const badge = $("badge");
  if (!st) {
    badge.textContent = "后端离线 OFFLINE";
    badge.className = "badge off";
    renderSysline(null);
    return;
  }
  if (st.sim) { badge.textContent = "在线模拟 SIM"; badge.className = "badge sim"; }
  else if (st.fake_dog) { badge.textContent = "实况·模拟狗 LIVE/FAKE-DOG"; badge.className = "badge sim"; }
  else { badge.textContent = "实况 LIVE"; badge.className = "badge live"; }
  renderSimNote(st);

  const es = st.state === "estopped";
  $("estopOverlay").style.display = es ? "flex" : "none";
  if (es) {
    $("estopTime").textContent = st.estopped_at
      ? `触发于 triggered at ${fmtTime(st.estopped_at)}` : "";
  } else if (releaseArmed) {
    releaseArmed = false;
    clearTimeout(releaseTimer);
    resetReleaseBtn();
  }

  if (st.order_id !== lastOrderId) {           // 新订单：清轨迹/速度
    trail.length = 0;
    lastOrderId = st.order_id;
    speedMps = null; lastPose = null;
  }
  if (st.pose && MOVING_STATES.includes(st.state)) {
    const lp = trail[trail.length - 1];
    if (!lp || Math.hypot(st.pose.x - lp.x, st.pose.y - lp.y) > 0.03) {
      trail.push({ x: st.pose.x, y: st.pose.y });
      if (trail.length > 600) trail.shift();
    }
  }

  renderTimeline(st.events);
  renderTelemetry(st);
  drawMap(st);
  renderSysline(st);

  const inProgress = st.state !== "idle" && !es;
  $("menu").style.display = inProgress ? "none" : "grid";
  $("progress").style.display = inProgress ? "flex" : "none";
  if (!inProgress) return;

  const name = drinkLabel(st.drink);
  $("orderTitle").innerHTML =
    st.state === "failed" ? `订单失败<span class="en">Order failed</span>` :
    st.state === "delivered" ? `${name} 已送达！<span class="en">Delivered — enjoy!</span>` :
    `${name} · 订单 Order #${st.order_id ?? ""}`;

  const failed = st.state === "failed";
  $("stages").style.display = failed ? "none" : "flex";
  $("failBox").style.display = failed ? "flex" : "none";
  if (failed) $("failMsg").textContent = st.error || "未知原因 Unknown error";
  else renderStages(st.state);

  $("confirmBtn").style.display = st.state === "awaiting_pickup" ? "block" : "none";
  $("doneMark").style.display = st.state === "delivered" ? "block" : "none";
}

/* ---------------- 事件绑定 ---------------- */
$("estopBtn").addEventListener("click", async () => {
  await B.estop();                 // 急停不设确认——立即执行
  toast("🛑 已发送急停 · E-STOP sent");
});
function resetReleaseBtn() {
  $("releaseBtn").innerHTML = `解除急停<span class="en">Release E-stop</span>`;
}
$("releaseBtn").addEventListener("click", async () => {
  if (!releaseArmed) {             // 解除需要 3 秒内二次确认
    releaseArmed = true;
    $("releaseBtn").innerHTML = `再点一次确认解除<span class="en">Tap again to confirm</span>`;
    releaseTimer = setTimeout(() => { releaseArmed = false; resetReleaseBtn(); }, 3000);
    return;
  }
  clearTimeout(releaseTimer);
  releaseArmed = false;
  resetReleaseBtn();
  const r = await B.estopRelease();
  if (r.status !== 200) toast("解除失败 · Release failed");
});
$("confirmBtn").addEventListener("click", async () => {
  const r = await B.confirm();
  if (r.status !== 200) toast("现在不在等待取货状态 · Not awaiting pickup right now");
});
$("resetBtn").addEventListener("click", () => B.reset());
$("miniReset").addEventListener("click", async () => {
  await B.reset();
  toast("已复位 · Reset done");
});

/* ---------------- 导航日志（折叠打开时才拉取） ---------------- */
setInterval(async () => {
  if (!$("navlogWrap").open) return;
  const lines = await B.navlog();
  $("navlogBox").textContent = lines.length ? lines.join("\n") : "（暂无日志 · no log yet）";
}, 2000);

/* ---------------- 轮询主循环 ---------------- */
async function poll() {
  const st = await B.status();
  if (st && !menuLoaded) { try { await loadMenu(); } catch (e) { /* 下轮重试 */ } }
  render(st);
}
loadMenu().catch(() => {});
setupCameras();
poll();
setInterval(poll, MODE.kind === "mock" ? 250 : 1000);
```

**注意**：上面 `_drawArm` 中有一行故意写坏的占位 `ctx.strokeStyle = "#3a4burnish" === "never" ? "#0" : "#3a4a63";` —— 执行时直接写成 `ctx.strokeStyle = "#3a4a63";`（计划文档笔误防呆标记，勿照抄）。

- [ ] **Step 6.2: 删除旧文件 + 语法检查**

```bash
rm frontend/vendor.js
node --check frontend/vendor-app.js
```

Expected: node 无输出（语法 OK）。若本机无 node：`python3 -c "print(open('frontend/vendor-app.js').read().count('{')==open('frontend/vendor-app.js').read().count('}'))"` 粗检并靠 Task 8 浏览器验证。

- [ ] **Step 6.3: Commit** — `git add -A frontend/ && git commit -m "feat(ui): shared vendor-app.js core (real/mock adapters, timeline, telemetry, minimap, cameras, estop)"`

---

### Task 7: demo-site 双模式页 + sync.sh + README

**Files:**
- Rewrite: `demo-site/index.html`
- Create: `demo-site/sync.sh`
- Create: `demo-site/vendor-app.js`（拷贝）
- Delete: `demo-site/vendor.js`
- Modify: `README.md`（追加「公网直播模式」一节）

- [ ] **Step 7.1: demo-site/index.html** — 与 `frontend/vendor.html` 完全同构，仅 3 处差异：
  1. `<title>dimOS 饮料速递 · Drink Delivery (Online)</title>`
  2. `<script>window.VENDOR_MODE = "auto";</script>`
  3. header 徽章初始文案 `模拟 SIM`（JS 首轮 render 会覆盖）

  制作方式（执行时）：复制 frontend/vendor.html 内容，按上述 3 点修改后写入。

- [ ] **Step 7.2: demo-site/sync.sh**

```bash
#!/bin/sh
# 同步共享前端核心到 demo-site（Vercel 部署目录）。
# frontend/vendor-app.js 是唯一事实源——勿直接改 demo-site/vendor-app.js。
set -e
cd "$(dirname "$0")"
cp ../frontend/vendor-app.js vendor-app.js
echo "synced: frontend/vendor-app.js -> demo-site/vendor-app.js"
```

```bash
chmod +x demo-site/sync.sh && demo-site/sync.sh && rm demo-site/vendor.js
```

- [ ] **Step 7.3: README 追加**（`README.md` 末尾）

```markdown
## 公网直播模式（Vercel 双模式页）

`demo-site/` 部署在 Vercel 上，默认是浏览器内模拟。要让公网页面显示真实
机器人画面/状态并可急停：

1. 在跑控制面板的机器上开隧道：
   `cloudflared tunnel --url http://localhost:8090`
2. 打开 `https://<vercel域名>/?backend=<隧道URL>` —— 参数会记住
   （localStorage），之后直接开裸域名也走真实后端；`?backend=off` 断开。

摄像头经 8090 代理（`/api/camera/{go2|arm}/stream.mjpg`），隧道只需暴露
一个端口。臂相机 USB 设备号在 `backend/vendor_config.json` 的
`arm_camera_device`（默认 0）。改完共享前端后运行 `demo-site/sync.sh` 同步。
```

- [ ] **Step 7.4: Commit** — `git add -A demo-site/ README.md && git commit -m "feat(demo-site): dual-mode Vercel page (?backend= tunnel) + sync script"`

---

### Task 8: 全量本地验证（机器人无电版）

- [ ] **Step 8.1: 测试套件**

```bash
./venv/bin/pip install opencv-python-headless  # 若未装（臂 daemon 测试用）
./venv/bin/python -m pytest tests/ -q
```
Expected: 全 PASS（vendor 11 + camera_proxy 4 + arm_daemon 1）

- [ ] **Step 8.2: FAKE_DOG 后端全流程**

```bash
VENDOR_FAKE_DOG=1 ./venv/bin/python -m uvicorn main:app --app-dir backend --port 8090 &
sleep 2
curl -s localhost:8090/api/vendor/status | python3 -m json.tool   # idle + map + events
curl -s -X POST -F drink_id=cola localhost:8090/api/vendor/order
sleep 2; curl -s localhost:8090/api/vendor/status | python3 -m json.tool  # pose/dist 在动
curl -s -X POST localhost:8090/api/vendor/estop | python3 -m json.tool    # estopped
curl -s -X POST localhost:8090/api/vendor/estop/release
curl -s localhost:8090/api/camera/arm/health | python3 -m json.tool       # ARM_CAM_PY 未设时预期 daemon 启动失败→ok:false（Mac 上设 ARM_CAM_PY=$PWD/venv/bin/python 后应 ok:true 占位帧）
```

- [ ] **Step 8.3: 浏览器过一遍** — 打开 `http://localhost:8090/vendor`：下单→时间线滚动→小地图轨迹→急停红屏→二次确认解除→再下单。
- [ ] **Step 8.4: 模拟模式** — `python3 -m http.server 8099 -d demo-site` 开 `http://localhost:8099/`：无参数纯模拟全流程 + 假摄像头动画；`?backend=http://localhost:8090` 双模式连通（CORS 生效）。
- [ ] **Step 8.5: 收尾** — 杀后台 server；如有 UI 小修，改 `frontend/vendor-app.js` 后跑 `demo-site/sync.sh` 再 commit。

---

### Task 9: Vercel 部署 + 真机待办清单

- [ ] **Step 9.1: 部署 demo-site 到 Vercel**（沿用既有项目；用 vercel 插件/CLI，preview 先看一眼再 prod）
- [ ] **Step 9.2: 在 HACKATHON_STATE.md §3 的 Go2 小节补一行真机待办**：

```markdown
- Vendor 控制台真机待验证项（2026-07-25 机器人没电延后）：Go2 相机代理出画面、
  真导航中 /api/vendor/estop 实停（零速连发路径）、Ascent 上臂 USB 相机设备号
  （vendor_config.json: arm_camera_device）+ ARM_CAM_PY 解释器确认、隧道端到端。
```

- [ ] **Step 9.3: 最终 commit + push**（分支 `feat/vendor-app-server`）

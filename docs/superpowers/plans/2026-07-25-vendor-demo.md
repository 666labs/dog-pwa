# Vendor 点单 Demo 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 顾客页点击饮料 → Ascent 上的编排器驱动「臂 stub 取货 → Go2 真导航送桌 → 确认取货 → 自动返程」，全链路可先在无机器人 dry-run 模式跑通，然后部署到 10.76.4.120。

**Architecture:** 扩展现有 FastAPI 后端（8090 单进程）：新增 `vendor.py`（asyncio 单订单状态机 + APIRouter）与 `nav_leg.py`（dimos-env 子进程：发布 `/clicked_point` 目标点 + peek `odometry` 判到达，JSONL 汇报）。前端新增自包含顾客页 `/vendor`。

**Tech Stack:** FastAPI/uvicorn（已有）、dimOS CLI/Python（仅 Ascent 上有，本地用 `VENDOR_FAKE_DOG=1` 模拟）、原生 JS 前端、pytest + httpx 测试。

**已验证的集成事实**（2026-07-25 在 Ascent 的 dimos 0.0.14b1 源码确认）：
- nav 目标入口：`MovementManager.clicked_point: In[PointStamped]`，rerun viewer 的点击即发此话题 → 话题名 `/clicked_point`，消息 `PointStamped(x, y, z, ts, frame_id)`（构造签名已确认）。
- 到达判定位姿源：nav-3d 中 GO2 自带 odom 被重命名 `/odom_go2`；规划坐标系 `world_frame="odom"` 是 PointLio 的输出 → **peek `odometry`（无斜杠流名），不是 `odom`**。
- 取消导航：向 `/tele_cmd_vel` 发零速 `Twist` 触发 `MovementManager._cancel_goal()`（内置）。直接发 NaN 到 `/clicked_point` 无效（`_on_click` 过滤非有限值）。
- 风险：`unitree-go2-nav-3d` 依赖外置 Mid-360 雷达 + PointLio。蓝图名/话题名全部做成配置项，现场可换。
- `dimos_cli.DIMOS_PY` 已存在（daemon 就用它），`_clean_env()` 剥代理变量必须沿用。
- `main.py` 的 `app.mount("/", StaticFiles...)` 在文件末尾，**新路由必须注册在 mount 之前**。

---

### Task 0: 本地测试环境（不提交）

**Files:** 无（`venv/` 已在仓库外/被忽略）

- [ ] **Step 0.1:** `cd ~/projects/dog-pwa && python3 -m venv venv && ./venv/bin/pip install fastapi 'uvicorn[standard]' python-multipart pytest httpx psutil`
- [ ] **Step 0.2:** 验证：`./venv/bin/python -c "import fastapi, multipart; print('ok')"` → 输出 `ok`

### Task 1: 用已验证事实更新 spec（提交）

**Files:** Modify: `docs/superpowers/specs/2026-07-25-vending-demo-design.md`

- [ ] **Step 1.1:** 在 spec 中修订：①目标是 `PointStamped`（无朝向）→ `table`/`station` 去掉 `yaw`；②`nav_daemon.py` 方案改为 `nav_leg.py`（每条腿一个短生命周期子进程：connect → 发目标 → 轮询位姿 → 退出；比 warm-daemon 简单，每单只发 2 次目标，2s 启动成本可忽略）；③补充已验证话题（`/clicked_point`、`odometry`、`/tele_cmd_vel` 取消）；④`reset` 不再 `dimos_cli.stop()`——取消目标+零速即可，蓝图保留（恢复更快）；⑤记录 Mid-360 依赖风险与配置化对策。
- [ ] **Step 1.2:** Commit: `git add docs/ && git commit -m "Spec: fold in verified nav integration facts (clicked_point/odometry/tele_cmd_vel)"`

### Task 2: `vendor_config.json`

**Files:** Create: `backend/vendor_config.json`

- [ ] **Step 2.1:** 写入：

```json
{
  "drinks": [
    {"id": "cola",   "name": "可乐",   "color": "#e0312e", "arm_action": "pick_slot_1"},
    {"id": "sprite", "name": "雪碧",   "color": "#2ea84f", "arm_action": "pick_slot_2"},
    {"id": "water",  "name": "矿泉水", "color": "#2e7de0", "arm_action": "pick_slot_3"}
  ],
  "table":   {"x": 2.0, "y": 0.0},
  "station": {"x": 0.0, "y": 0.0},
  "robot_ip": null,
  "transport": null,
  "nav_blueprint": "unitree-go2-nav-3d",
  "goal_topic": "/clicked_point",
  "goal_frame_id": "map",
  "pose_topic": "odometry",
  "cancel_topic": "/tele_cmd_vel",
  "arm_stub_delay_s": 5.0,
  "arrival_radius_m": 0.35,
  "nav_timeout_s": 90,
  "result_display_s": 6
}
```

（`table`/`station` 是占位值，现场标定后改。）

### Task 3: `backend/vendor.py` 状态机 + stub + fake-dog（TDD）

**Files:** Create: `backend/vendor.py`, `tests/test_vendor.py`

- [ ] **Step 3.1: 先写失败测试** `tests/test_vendor.py`：

```python
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
        # 非等待状态 confirm 已在下面单独测；这里正常确认
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
```

- [ ] **Step 3.2:** 运行确认失败：`./venv/bin/pytest tests/test_vendor.py -x -q` → `ModuleNotFoundError: No module named 'vendor'`
- [ ] **Step 3.3: 实现** `backend/vendor.py`（完整）：

```python
"""Vendor 点单编排器 — 单订单 asyncio 状态机 + /api/vendor/* 路由。

状态机：idle → arm_picking → dog_delivering → awaiting_pickup →(confirm)→
dog_returning → delivered → idle；失败进 failed，展示后自动回 idle。
臂侧为 stub（接口即契约，队友换实现）；狗侧真导航走 nav_leg.py 子进程
（VENDOR_FAKE_DOG=1 时全程模拟，无机器人联调用）。
设计依据：docs/superpowers/specs/2026-07-25-vending-demo-design.md
"""
import asyncio
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

import dimos_cli

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(HERE, "vendor_config.json")
NAV_LEG = os.path.join(HERE, "nav_leg.py")
_NAV_LEG_LOG = "/tmp/vendor-nav-leg.log"

IDLE = "idle"
ARM_PICKING = "arm_picking"
DOG_DELIVERING = "dog_delivering"
AWAITING_PICKUP = "awaiting_pickup"
DOG_RETURNING = "dog_returning"
DELIVERED = "delivered"
FAILED = "failed"
_ACTIVE = {ARM_PICKING, DOG_DELIVERING, AWAITING_PICKUP, DOG_RETURNING}


class VendorError(Exception):
    """订单流程失败（导航超时、蓝图启动失败等）— 消息直接展示给前端。"""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    """每次调用重读配置 — 现场改 JSON 后下一单即生效，无需重启。"""
    path = os.environ.get("VENDOR_CONFIG_PATH", _DEFAULT_CONFIG)
    with open(path) as f:
        return json.load(f)


class OrderManager:
    def __init__(self) -> None:
        self.state = IDLE
        self.drink: Optional[dict] = None
        self.order_seq = 0
        self.error: Optional[str] = None
        self.dist_to_goal: Optional[float] = None
        self.stage_started_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._confirm: Optional[asyncio.Event] = None
        self._nav_proc = None

    def _set(self, state: str, error: Optional[str] = None) -> None:
        self.state = state
        self.error = error
        self.stage_started_at = time.time()
        if state in (IDLE, DELIVERED, FAILED):
            self.dist_to_goal = None

    def status(self) -> dict:
        return {
            "state": self.state,
            "order_id": self.order_seq or None,
            "drink": self.drink,
            "error": self.error,
            "dist_to_goal": self.dist_to_goal,
            "stage_started_at": self.stage_started_at,
            "fake_dog": _env_flag("VENDOR_FAKE_DOG"),
        }

    async def place_order(self, drink: dict, cfg: dict) -> dict:
        self.order_seq += 1
        self.drink = {"id": drink["id"], "name": drink["name"]}
        self._set(ARM_PICKING)
        self._task = asyncio.create_task(self._run_order(drink, cfg))
        return {"order_id": self.order_seq}

    async def _run_order(self, drink: dict, cfg: dict) -> None:
        display = float(cfg.get("result_display_s", 6))
        try:
            self._set(ARM_PICKING)
            await self._arm_pick(drink, cfg)
            self._set(DOG_DELIVERING)
            await self._dog_go_to(cfg["table"], cfg)
            self._set(AWAITING_PICKUP)
            self._confirm = asyncio.Event()
            await self._confirm.wait()
            self._set(DOG_RETURNING)
            await self._dog_go_to(cfg["station"], cfg)
            self._set(DELIVERED)
            await asyncio.sleep(display)
            if self.state == DELIVERED:
                self._set(IDLE)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  — 任何失败都进 failed 并展示原因
            self._set(FAILED, error=str(e))
            await asyncio.sleep(display)
            if self.state == FAILED:
                self._set(IDLE)

    async def _arm_pick(self, drink: dict, cfg: dict) -> None:
        """机械臂 stub：结构化日志 + 可配置延时。真实现替换本方法体即可，
        入参契约（drink 含 arm_action，cfg 全量配置）保持不变。"""
        print(json.dumps({"vendor_arm_stub": {
            "drink": drink["id"], "arm_action": drink.get("arm_action"),
        }}), flush=True)
        await asyncio.sleep(float(cfg.get("arm_stub_delay_s", 5.0)))

    async def _dog_go_to(self, goal: dict, cfg: dict) -> None:
        if _env_flag("VENDOR_FAKE_DOG"):
            await self._fake_go_to(goal, cfg)
            return

        bp = cfg.get("nav_blueprint", "unitree-go2-nav-3d")
        st = await asyncio.to_thread(dimos_cli.status)
        transport = cfg.get("transport") or st.get("transport")
        if not st.get("running") or st.get("blueprint") != bp:
            if st.get("running"):
                await asyncio.to_thread(dimos_cli.stop)
                await asyncio.sleep(2)  # 释放唯一 WebRTC 连接槽
            launch = await asyncio.to_thread(
                dimos_cli.run_blueprint, bp, cfg.get("robot_ip"), cfg.get("transport"))
            if not launch.get("launched"):
                raise VendorError(f"nav blueprint 启动失败: {launch}")
            transport = cfg.get("transport")

        args = [dimos_cli.DIMOS_PY, NAV_LEG,
                "--x", str(goal["x"]), "--y", str(goal["y"]),
                "--frame-id", str(cfg.get("goal_frame_id", "map")),
                "--goal-topic", str(cfg.get("goal_topic", "/clicked_point")),
                "--pose-topic", str(cfg.get("pose_topic", "odometry")),
                "--radius", str(cfg.get("arrival_radius_m", 0.35)),
                "--timeout", str(cfg.get("nav_timeout_s", 90))]
        if transport:
            args += ["--transport", str(transport)]

        with open(_NAV_LEG_LOG, "ab") as errlog:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=errlog,
                env=dimos_cli._clean_env())
        self._nav_proc = proc
        last: dict = {}
        try:
            async def _read() -> None:
                nonlocal last
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    try:
                        ev = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    last = ev
                    if ev.get("event") == "pose":
                        self.dist_to_goal = ev.get("dist")

            # nav_leg 自身有 --timeout；这里只留连接/就绪余量的墙钟兜底
            wall = float(cfg.get("nav_timeout_s", 90)) + 90
            try:
                await asyncio.wait_for(_read(), timeout=wall)
            except asyncio.TimeoutError:
                raise VendorError("导航子进程超时无响应")
            rc = await proc.wait()
            if rc == 0:
                return
            ev, err = last.get("event"), last.get("error")
            if ev == "timeout":
                raise VendorError("导航超时——未在时限内到达目标")
            if ev == "pose_lost":
                raise VendorError("里程计中断——连续读不到位姿")
            raise VendorError(err or f"导航子进程异常退出（code {rc}）")
        finally:
            self._nav_proc = None
            if proc.returncode is None:
                proc.kill()

    async def _fake_go_to(self, goal: dict, cfg: dict) -> None:
        """Dry-run：以恒定速度逼近目标，不碰 dimos。"""
        dist = float(os.environ.get("VENDOR_FAKE_DOG_DIST", "3.0"))
        speed = float(os.environ.get("VENDOR_FAKE_DOG_SPEED", "0.6"))
        radius = float(cfg.get("arrival_radius_m", 0.35))
        step = 0.25
        while dist > radius:
            await asyncio.sleep(step)
            dist = max(0.0, dist - speed * step)
            self.dist_to_goal = round(dist, 3)

    async def confirm(self) -> bool:
        if self.state != AWAITING_PICKUP or self._confirm is None:
            return False
        self._confirm.set()
        return True

    async def reset(self) -> dict:
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
        if not _env_flag("VENDOR_FAKE_DOG"):
            # 取消 nav 目标：/tele_cmd_vel 上一条零速 Twist 触发
            # MovementManager._cancel_goal()（蓝图保留，不 stop——恢复快）。
            try:
                cfg = load_config()
                st = await asyncio.to_thread(dimos_cli.status)
                if st.get("running"):
                    await asyncio.to_thread(
                        dimos_cli.teleop_send, 0, 0, 0, 0, 0, 0,
                        topic=cfg.get("cancel_topic", "/tele_cmd_vel"),
                        transport=cfg.get("transport") or st.get("transport"))
            except Exception:  # noqa: BLE001  — reset 必须永远成功
                pass
        self._set(IDLE)
        self.drink = None
        return self.status()


router = APIRouter()
manager = OrderManager()


@router.get("/api/vendor/menu")
def api_vendor_menu():
    cfg = load_config()
    return {"drinks": [
        {"id": d["id"], "name": d["name"], "color": d.get("color", "#888888")}
        for d in cfg.get("drinks", [])
    ]}


@router.post("/api/vendor/order")
async def api_vendor_order(drink_id: str = Form(...)):
    cfg = load_config()
    drink = next((d for d in cfg.get("drinks", []) if d["id"] == drink_id), None)
    if drink is None:
        return JSONResponse(status_code=404, content={"error": "unknown_drink"})
    if manager.state in _ACTIVE:
        return JSONResponse(status_code=409, content={
            "error": "order_in_progress", "current": manager.status()})
    return await manager.place_order(drink, cfg)


@router.get("/api/vendor/status")
def api_vendor_status():
    return manager.status()


@router.post("/api/vendor/confirm")
async def api_vendor_confirm():
    if not await manager.confirm():
        return JSONResponse(status_code=409, content={
            "error": "not_awaiting_pickup", "state": manager.state})
    return {"ok": True}


@router.post("/api/vendor/reset")
async def api_vendor_reset():
    return await manager.reset()
```

- [ ] **Step 3.4:** `./venv/bin/pytest tests/test_vendor.py -q` → 5 passed
- [ ] **Step 3.5:** Commit: `git add backend/vendor.py backend/vendor_config.json tests/ && git commit -m "feat: vendor order state machine + API (arm stub, fake-dog dry-run)"`

### Task 4: `backend/nav_leg.py`（dimos-env 导航腿子进程）

**Files:** Create: `backend/nav_leg.py`

- [ ] **Step 4.1:** 写入（完整）：

```python
#!/usr/bin/env python3
"""One navigation leg — 在 dimOS 环境里跑的短生命周期子进程。

由 vendor.py 每条送货/返程腿 spawn 一次（用 dimos_cli.DIMOS_PY 解释器）：
connect → 等首个位姿（就绪判据）→ 发布 PointStamped 目标到 /clicked_point
（nav-3d 的 MovementManager 入口，与 rerun viewer 点击同一条路）→ 轮询
peek_stream 位姿直到进入到达半径或超时。

坐标系注意（实测于 dimos 0.0.14b1 源码）：nav-3d 中 GO2 自带 odom 被重命名
/odom_go2；规划器 world_frame="odom" 用的是 PointLio 的输出 → 位姿流名默认
"odometry"（peek_stream 流名无斜杠），目标坐标必须与它同系。

PROTOCOL: stdout 每行一个 JSON 事件（connected/ready/goal_sent/pose/arrived/
timeout/pose_lost/error），诊断走 stderr。退出码：0 到达，1 初始化失败，
2 导航超时，3 位姿丢失/就绪超时。
"""
import argparse
import json
import math
import sys
import time


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x", type=float, required=True)
    ap.add_argument("--y", type=float, required=True)
    ap.add_argument("--z", type=float, default=0.0)
    ap.add_argument("--frame-id", default="map")
    ap.add_argument("--goal-topic", default="/clicked_point")
    ap.add_argument("--pose-topic", default="odometry",
                    help="peek_stream 流名（无斜杠）——必须与目标同坐标系")
    ap.add_argument("--radius", type=float, default=0.35)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--ready-timeout", type=float, default=45.0,
                    help="等首个位姿出现（蓝图可能刚启动）")
    ap.add_argument("--transport", default=None)
    ap.add_argument("--resend", type=int, default=3,
                    help="目标重发次数（LCM 无连接，首发前后订阅时序保险）")
    ap.add_argument("--poll", type=float, default=0.5)
    args = ap.parse_args()

    t0 = time.time()
    try:
        from dimos.core.global_config import global_config
        if args.transport:
            global_config.update(transport=args.transport)
        from dimos import Dimos
        from dimos.core.transport_factory import make_transport
        from dimos.msgs.geometry_msgs.PointStamped import PointStamped
    except Exception as e:  # noqa: BLE001
        _emit({"event": "error", "error": f"import failed: {e!r}"})
        return 1
    try:
        app = Dimos.connect()
    except Exception as e:  # noqa: BLE001
        _emit({"event": "error", "error": f"connect failed: {e!r}"})
        return 1
    _emit({"event": "connected", "warmup_s": round(time.time() - t0, 3)})

    def peek_pose():
        try:
            return app.peek_stream(args.pose_topic, 1.5)
        except Exception:  # noqa: BLE001
            return None

    ready_deadline = time.time() + args.ready_timeout
    pose = None
    while time.time() < ready_deadline:
        pose = peek_pose()
        if pose is not None:
            break
        time.sleep(0.5)
    if pose is None:
        _emit({"event": "pose_lost",
               "error": f"no pose on '{args.pose_topic}' within {args.ready_timeout}s"})
        return 3
    _emit({"event": "ready", "x": round(float(pose.x), 3), "y": round(float(pose.y), 3)})

    goal = PointStamped(x=args.x, y=args.y, z=args.z, frame_id=args.frame_id)
    transport = make_transport(args.goal_topic, PointStamped)
    for _ in range(max(1, args.resend)):
        transport.broadcast(None, goal)
        time.sleep(0.5)
    _emit({"event": "goal_sent", "x": args.x, "y": args.y})

    deadline = time.time() + args.timeout
    misses = 0
    while time.time() < deadline:
        v = peek_pose()
        if v is None:
            misses += 1
            if misses >= 5:  # peek 超时 1.5s + poll 0.5s → 约 10s 无位姿
                _emit({"event": "pose_lost"})
                return 3
        else:
            misses = 0
            dist = math.hypot(float(v.x) - args.x, float(v.y) - args.y)
            _emit({"event": "pose", "x": round(float(v.x), 3),
                   "y": round(float(v.y), 3), "dist": round(dist, 3)})
            if dist <= args.radius:
                _emit({"event": "arrived", "dist": round(dist, 3)})
                return 0
        time.sleep(args.poll)
    _emit({"event": "timeout"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4.2:** 语法检查（本地无 dimos，只能编译级验证）：`./venv/bin/python -m py_compile backend/nav_leg.py && echo OK` → OK
- [ ] **Step 4.3:** Commit: `git add backend/nav_leg.py && git commit -m "feat: nav_leg subprocess (goal publish + arrival watch, dimos-env)"`

### Task 5: 前端顾客页

**Files:** Create: `frontend/vendor.html`, `frontend/vendor.js`

- [ ] **Step 5.1:** `vendor.html`：自包含（内联 CSS）、暗色、iPad 大触控目标。结构：`<div id="menu">`（饮料卡片网格）+ `<div id="progress" hidden>`（六段时间线 `已接单/机械臂取货中/机器狗配送中/请取走饮料/机器狗返程中/已完成` + `dist` 读数 + `awaiting_pickup` 时全屏「我已取到饮料 ✓」按钮 + `failed` 时错误文案与「重置」按钮）。完整代码见执行时按本描述实现，交互契约：
  - 点卡片 → `POST /api/vendor/order`（form-data `drink_id`）；409 时弹提示。
  - 1 Hz `GET /api/vendor/status` 轮询驱动一切渲染（无本地状态机）。
  - `state=idle` 显示菜单；活动状态显示时间线；`delivered/failed` 由后端自动回 `idle`，前端跟随。
  - 确认按钮 → `POST /api/vendor/confirm`；重置按钮 → `POST /api/vendor/reset`。
- [ ] **Step 5.2:** `vendor.js`：`loadMenu()`（渲染卡片）、`poll()`（setInterval 1000ms）、`render(status)`（stage 高亮映射表 `{arm_picking:1, dog_delivering:2, awaiting_pickup:3, dog_returning:4, delivered:5}`）。
- [ ] **Step 5.3:** Commit: `git add frontend/vendor.html frontend/vendor.js && git commit -m "feat: vendor customer page (drink cards + delivery timeline)"`

### Task 6: `main.py` 挂载

**Files:** Modify: `backend/main.py`（`/api/health` 之后、`# Static frontend` mount 段之前插入——mount("/") 在文件末尾，注册顺序决定路由优先级）

- [ ] **Step 6.1:** 插入：

```python
# --------------------------------------------------------------------------- #
# Vendor demo (drink ordering → arm stub → Go2 nav delivery)
# --------------------------------------------------------------------------- #
import vendor  # noqa: E402  (import placed here to keep the router group visible)

app.include_router(vendor.router)


@app.get("/vendor")
def vendor_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "vendor.html"))
```

- [ ] **Step 6.2:** 全链路 dry-run：`VENDOR_FAKE_DOG=1 ./venv/bin/python -m uvicorn main:app --app-dir backend --port 8093` 起本地服务，curl 走完 order→awaiting→confirm→delivered→idle，`GET /vendor` 返回 200。
- [ ] **Step 6.3:** `./venv/bin/pytest tests/ -q` 仍全绿。
- [ ] **Step 6.4:** Commit: `git add backend/main.py && git commit -m "feat: mount vendor router and /vendor page"`

### Task 7: 部署到 Ascent（10.76.4.120）

**Files:** 无新文件（远端操作）

- [ ] **Step 7.1:** rsync 代码：`rsync -av --exclude venv --exclude .git --exclude __pycache__ --exclude tests backend frontend asus@10.76.4.120:~/dimos-pwa/`
- [ ] **Step 7.2:** 重启面板（`pkill -f 'uvicorn main:app'`（该机只有这一个 uvicorn）→ handover 里的 nohup 启动命令）。
- [ ] **Step 7.3:** 远端冒烟：`curl http://10.76.4.120:8090/api/health`、`/api/vendor/menu`、`/api/vendor/status`、`GET /vendor` 均 200。**不触发真实点单**（机器人未连接；真机联调是现场任务）。

### Task 8: 桌面部署文件

**Files:** Create: `~/Desktop/vendor-demo-deploy/deploy.sh`、`~/Desktop/vendor-demo-deploy/README.md`（仓库外交付物）

- [ ] **Step 8.1:** `deploy.sh`：rsync + 远程重启 + 冒烟 curl，一键重部署。
- [ ] **Step 8.2:** `README.md`：顾客页/面板 URL、现场标定步骤（sudo ./setup.sh → 建图 → 改 `vendor_config.json` 的 table/station/robot_ip → 真机首单）、故障处置（reset 接口、nav_leg 日志位置 `/tmp/vendor-nav-leg.log`、Mid-360 依赖提醒、蓝图可换配置）。

---

## Self-Review

- **Spec 覆盖**：状态机 6 态+failed（Task 3）✓；confirm/返程（Task 3/5）✓；4+1 API（Task 3）✓；配置文件含 station（Task 2）✓；dry-run（Task 3/6）✓；部署（Task 7/8）✓；spec 修订项本身（Task 1）✓。
- **占位符**：Task 5 前端给的是交互契约而非逐行 HTML——执行者即本会话（上下文完整），按契约实现；其余任务全部含完整代码。
- **类型一致性**：`load_config()/manager/_ACTIVE` 命名在 Task 3/6 一致；`nav_leg` CLI 参数与 `_dog_go_to` 传参一一对应（--x/--y/--frame-id/--goal-topic/--pose-topic/--radius/--timeout/--transport）✓。

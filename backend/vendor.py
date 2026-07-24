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
                raise VendorError(err or "里程计中断——连续读不到位姿")
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

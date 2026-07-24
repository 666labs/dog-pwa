# 贩卖点单 Demo（Vendor App）设计文档

**日期**: 2026-07-25　**分支**: `feat/vendor-app-server`　**作者**: Helios + Claude
**背景**: AdventureX 2026，Demo 1（贩卖机送饮料）。提交截止 2026-07-26 01:00。
**上下文文档**: `docs/HACKATHON_STATE.md`、`docs/ASCENT_HANDOVER.md`

## 1. 目标

一个顾客可用的点单页面：点击一种饮料 → 部署在 Ascent GX10（`10.76.4.120`，端口 8090）上的服务器编排「机械臂取货 → 机器狗送货」全自动流水线，并向前端实时反馈订单进度。

点击取代语音作为触发入口（语音层 0% 已建，不在本次范围）。

## 2. 已对齐的范围决定

| 决定点 | 结论 |
|---|---|
| 命令通道 | **Ascent 直接控制**：vendor server 跑在 Ascent 上，通过本机 dimos 驱动两台机器人（非消息中转） |
| 机械臂（Galaxea A1Z） | **只做接口 + stub**：定义固定的 ArmExecutor 接口，本次实现为可配置延时的模拟执行器；真实拓取动作由队友后续接入 |
| 机器狗（Go2） | **真导航**：启动 dimOS nav blueprint（`unitree-go2-nav-3d`），下发硬编码桌位坐标，A* 规划 + 避障；**不做**脚本化速度序列兜底 |
| Server 形态 | **扩展现有后端**：`backend/main.py`（8090 单进程）新增 vendor 模块，与现有 blueprint 进程管理共享状态，避免两个进程抢 Go2 唯一的 WebRTC 连接 |
| 编排时序 | **全自动流水线**：点击后无人工干预推进到完成；仅保留一个全局 `reset` 紧急复位接口作为唯一人工出口 |

## 3. 架构

```
iPad 顾客页 /vendor (vendor.html + vendor.js)
   │  点击饮料卡片 → POST /api/vendor/order {drink_id}
   ▼
vendor 编排器（backend/vendor.py，挂载进 main.py 的 FastAPI app）
   │  asyncio 状态机，同一时刻仅允许一个活动订单
   │
   ├─ 阶段1: ArmExecutor.pick(drink)      ← 本次为 stub（configurable delay + 日志）
   ├─ 阶段2: DogExecutor.deliver(table)   ← 确保 nav blueprint 运行
   │           → nav_daemon 发布目标位姿 (x, y, yaw)
   │           → 轮询里程计，距目标 < arrival_radius 判定到达
   │           → 超时（nav_timeout_s）判定失败
   ▼
GET /api/vendor/status ← 前端 1 Hz 轮询（与现有面板轮询风格一致）
```

不新增进程、不新增端口。`nav_daemon.py` 仿照现有 `teleop_daemon.py` / `sport_daemon.py` 的 warm-daemon 模式：由 `dimos_cli.py` 按需拉起、保持热连接，每次目标下发 < 1 ms。

## 4. 组件

### 4.1 `backend/vendor.py`（新）
- **APIRouter**：4 个路由（见 §6），在 `main.py` 中 `include_router`，一行接入。
- **OrderManager**：内存态单订单状态机（见 §5），持有当前订单的 asyncio task。
- **ArmExecutor（stub）**：`async def pick(drink: Drink) -> None`。实现为 `await asyncio.sleep(config.arm_stub_delay_s)` + 结构化日志（打出 drink 的 `arm_action` 标识）。接口即契约：队友的真实现替换这个类即可，编排器不改。
- **DogExecutor**：`async def deliver(table: Table) -> None`。步骤：
  1. 查 `dimos_cli.status()`——nav blueprint 未运行则 `run_blueprint()` 启动并等待就绪（就绪判据：里程计话题开始有数据，沿用现有 `/api/pose` 的读取路径；启动后 30 s 内无数据视为启动失败）；已有**其他** blueprint 在跑则先 `stop()` 再启动（尊重单 WebRTC 连接约束）。
  2. 通过 `nav_daemon` 发布目标位姿。
  3. 以 ~2 Hz 轮询里程计（复用现有 `/api/pose` 的底层读取），`dist(pose, goal) < arrival_radius` → 到达；超过 `nav_timeout_s` → 抛 `DeliveryTimeout`。

### 4.2 `backend/nav_daemon.py`（新）
仿 `teleop_daemon.py`：长驻小进程，用 dimos Python 环境向 nav blueprint 订阅的目标话题发布位姿。由 `dimos_cli.py` 新增的 `nav_goal(x, y, yaw)` / `nav_warm()` 函数管理（spawn、prime、复用）。具体话题名/消息类型在实现时从 `unitree-go2-nav-3d` blueprint 源码确认，同时需确认目标位姿所在坐标系与里程计位姿坐标系一致（否则到达判定失效；注意 `HACKATHON_STATE.md` §3 记录的 Unitree 固件 world-frame 累积地图怪癖）——这是**本设计中唯一未现场验证过的链路**，实施计划中排最早联调。

### 4.3 `backend/vendor_config.json`（新）
现场标定只改此文件，不改代码：

```json
{
  "drinks": [
    {"id": "cola",   "name": "可乐",  "color": "#e0312e", "arm_action": "pick_slot_1"},
    {"id": "sprite", "name": "雪碧",  "color": "#2ea84f", "arm_action": "pick_slot_2"},
    {"id": "water",  "name": "矿泉水", "color": "#2e7de0", "arm_action": "pick_slot_3"}
  ],
  "table": {"x": 0.0, "y": 0.0, "yaw": 0.0},
  "arm_stub_delay_s": 5.0,
  "arrival_radius_m": 0.35,
  "nav_timeout_s": 90,
  "result_display_s": 6
}
```

单桌位（demo 只有一张桌子）；`arm_action` 现在只进日志，将来原样传给真 ArmExecutor。

### 4.4 `frontend/vendor.html` + `frontend/vendor.js`（新）
- 顾客页，独立于现有仪表盘 `index.html`（顾客不应看到工程面板）；`main.py` 静态路由加 `/vendor`。
- **点单态**：全屏大饮料卡片（从 `/api/vendor/menu` 渲染），适配 iPad 触摸。
- **进度态**：点击后转全屏时间线：`已接单 → 机械臂取货中 → 机器狗配送中 → 已送达`，随 `/api/vendor/status` 推进；`failed` 显示失败原因和「重置」按钮（调 `/api/vendor/reset`）。
- 活动订单期间卡片置灰（后端同时以 409 保护）。
- `delivered` / `failed` 展示 `result_display_s` 秒后自动回点单态。

### 4.5 现有文件改动（最小化）
- `main.py`：`include_router(vendor_router)` + `/vendor` 静态页路由。
- `dimos_cli.py`：新增 `nav_goal()` / `nav_warm()` / `nav_kill()`（照抄 teleop 三件套的模式）。
- **不碰** frozen 的 teleop 路径；不改现有任何端点行为。

## 5. 订单状态机

```
idle ──POST /order──▶ arm_picking ──stub完成──▶ dog_delivering ──到达──▶ delivered ──result_display_s──▶ idle
                          │                        │                                   
                          │ reset                  │ reset / DeliveryTimeout / nav启动失败
                          ▼                        ▼
                        idle                     failed ──result_display_s 或 reset──▶ idle
```

- 全内存态，不落盘；进程重启 = 回 `idle`（demo 可接受）。
- 活动订单（`arm_picking` / `dog_delivering`）期间新点单返回 `409 {"error": "order_in_progress"}`。
- `reset`：取消编排 task → 若 nav 在跑则发一次零速度并 `dimos_cli.stop()` → 状态回 `idle`。是唯一人工出口（用户明确选择全自动流水线，不设逐段确认）。

## 6. API

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/vendor/menu` | — | `{drinks: [{id, name, color}]}` | 前端渲染卡片 |
| POST | `/api/vendor/order` | `{drink_id}` | `200 {order_id}` / `409` / `404`（未知 drink_id） | 触发流水线 |
| GET | `/api/vendor/status` | — | `{state, drink_id, stage_started_at, error, progress: {dist_to_goal}}` | 1 Hz 轮询 |
| POST | `/api/vendor/reset` | — | `200 {state: "idle"}` | 紧急复位，任何状态可调 |

## 7. 错误处理

| 故障 | 行为 |
|---|---|
| nav blueprint 启动失败 | 订单 → `failed`，`error` 带 dimos stderr 摘要 |
| 导航超时（`nav_timeout_s`） | 订单 → `failed`，狗停在原地（nav blueprint 继续跑，不自动 stop，便于人工接管） |
| 里程计读取中断 | 连续 10 s 读不到 → 视同超时 → `failed` |
| 未知 drink_id / 并发点单 | 404 / 409，不影响当前订单 |
| 任何卡死 | `POST /api/vendor/reset` 全局兜底 |

已知的「Go2 连接死掉无自动重连」风险**不在本次范围**（`HACKATHON_STATE.md` §3 已记录），沿用现有面板人工重连流程。

## 8. 测试策略

- **Dry-run 模式**：环境变量 `VENDOR_FAKE_DOG=1` 时 DogExecutor 不碰 dimos，改为模拟位姿以恒定速度逼近目标。臂本来就是 stub。→ 整条「点击 → 状态机 → 时间线 UI」链路可在开发笔记本上无机器人联调。
- **真机联调顺序**（在 Ascent 上）：① `sudo ./setup.sh`（LCM 前置，从未跑过）→ ② 手动验证 nav blueprint 可从 Ascent 启动并连狗（**Ascent 首次连接任何机器人**）→ ③ 验证 `nav_daemon` 目标下发 → ④ 全链路点单。
- 无自动化测试要求（hackathon）；`vendor.py` 状态机保持纯逻辑、与 dimos 调用隔离，便于将来补测。

## 9. 明确不做（本次范围外）

- 机械臂真实动作（ArUco 视觉、路点录制）——队友接 ArmExecutor 接口。
- 语音入口、多桌位选择、多订单队列、订单持久化。
- Go2 自动重连 watchdog。
- 脚本化速度序列兜底（用户明确决定只做 nav 真导航）。

## 10. 遗留的现场任务（非代码）

1. Ascent 上跑 `sudo ./setup.sh`（见 `ASCENT_HANDOVER.md` §4）。
2. 现场建图 + 标定桌位坐标，写入 `vendor_config.json` 的 `table`。
3. 确认演示时只有 vendor 流程占用 Go2 连接（`dimos status` / 清理孤儿进程）。

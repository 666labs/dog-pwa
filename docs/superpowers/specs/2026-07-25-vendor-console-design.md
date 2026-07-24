# Vendor 任务控制台（双模式）设计文档

**日期**: 2026-07-25　**分支**: `feat/vendor-app-server`　**作者**: Helios + Claude
**背景**: AdventureX 2026，Demo 1（贩卖机送饮料）。提交截止 2026-07-26 01:00。
**前置文档**: `2026-07-25-vending-demo-design.md`（vendor 状态机 + 点单流程）、`docs/HACKATHON_STATE.md`
**验证约束**: 设计确认时机器人无电——真机联调（相机流、真导航 E-stop、隧道端到端）延后；
本地可验证项（状态机测试、FAKE_DOG 全流程、Mac 摄像头、模拟模式 UI）照常。

## 1. 目标

把现在"只有 6 格进度条"的 vendor 顾客页升级为**任务控制台**：

1. **实时画面**：机器狗（Go2 `/color_image`，已有 MJPEG daemon）+ 机械臂工位（新接 USB 摄像头）两路视频。
2. **E-stop**：常驻红色急停按钮——单击即停狗 + 终止订单，进入 `estopped` 状态，显式解除才能恢复。
3. **执行细节**：事件时间线、实时遥测（位姿/距目标/电量）、导航日志尾巴、小地图轨迹。
4. **Vercel 页双模式**：带 `?backend=<隧道URL>` 时连真实后端（真画面+真 E-stop）；无参数时回退增强版浏览器内模拟。

## 2. 已对齐的范围决定

| 决定点 | 结论 |
|---|---|
| Vercel 页与真实后端 | **双模式**：`?backend=` 查询参数（存 localStorage）指向 cloudflared 隧道；无参数回退 MockBackend 模拟。LAN 真实页同款 UI（同源，base 为空） |
| 机械臂画面来源 | **USB 摄像头拍臂**：新 `arm_camera_daemon.py`，OpenCV `VideoCapture`，复用现有 FrameHolder/MJPEG server 模式 |
| E-stop 语义 | **软停**：零速 Twist 连发（1 秒 5 帧，触发 dimOS `_cancel_goal`）+ 杀 nav_leg + 取消订单任务；**blueprint 保持运行**（恢复快）。臂是 stub，E-stop 对臂 = 终止流程。触发单击无确认；解除需二次确认 |
| 执行细节 | 全部四项：事件时间线、遥测面板、导航日志尾巴、小地图轨迹 |
| 鉴权 | **不做**。隧道随机 URL 即口令（黑客松场景，用户知情） |
| 代码共享 | 共享核心 `vendor-app.js` 放 `frontend/`，`demo-site/` 持有拷贝，`demo-site/sync.sh`（cp 若干文件）负责同步；不引入构建工具 |

## 3. 架构

```
┌─ Vercel 静态页 (demo-site/) ────────────────┐
│ vendor-app.js（共享核心，frontend/ 的拷贝）    │
│   ├─ ?backend=https://xxx.trycloudflare.com │
│   │    → RealBackend(base=隧道URL)          │
│   └─ 无参数 → MockBackend（浏览器内模拟）      │
└─────────────────────────────────────────────┘
┌─ LAN 真实页 :8090/vendor (frontend/) ───────┐
│ vendor-app.js → RealBackend(base="")  同源   │
└─────────────────────────────────────────────┘
            │ HTTPS（隧道）或 LAN HTTP
            ▼
FastAPI :8090（main.py + vendor.py，新增 CORS allow-all）
  ├─ /api/vendor/status     ← 扩展：events[]、pose、map、estopped
  ├─ /api/vendor/estop      ← 新：软停 + estopped 状态
  ├─ /api/vendor/estop/release
  ├─ /api/vendor/navlog     ← 新：tail /tmp/vendor-nav-leg.log
  ├─ /api/camera/go2.mjpg|go2.jpg  ← 新：代理 localhost:8770（Go2 daemon）
  └─ /api/camera/arm.mjpg|arm.jpg  ← 新：代理 localhost:8771（臂 daemon）
        ├─ camera_daemon.py（已有，dimOS /color_image → MJPEG :8770）
        └─ arm_camera_daemon.py（新，USB cam → MJPEG :8771）
```

**为什么走代理**：隧道只暴露 8090 一个端口，浏览器无法直连 daemon 端口；LAN 页走代理同样工作（省一套分支逻辑）。前端自带"MJPEG 停帧 → 自动降级 2fps 快照轮询"兜底（隧道下长连接不稳时仍有画面）。

## 4. 后端设计（backend/）

### 4.1 vendor.py — estopped 状态 + 事件缓冲

- 新终态 `ESTOPPED = "estopped"`：
  - `estop()`：取消 `_task`、`kill` nav_leg 子进程、后台连发 5 帧零速 Twist（复用 `dimos_cli.teleop_send`，间隔 0.2s，任何异常吞掉——急停必须永远成功）、`_set(ESTOPPED)`。幂等：estopped 中再按仍返回 200。
  - `estop_release()`：仅当 state == ESTOPPED 时回 `idle`，否则 409。
  - `place_order` 在 `estopped` 时 409（错误码 `estopped`）。
  - `reset()` 不清除 estopped（急停语义强于复位；只有 release 能解除）。
- 事件缓冲：`self.events: deque(maxlen=60)`，元素 `{seq, ts, key, zh, en, data}`。记录点：下单、臂取货开始/完成、导航启动（含目标坐标）、到达、等待取货、确认、返程、完成、失败（含原因）、急停、解除、复位。`_set()` 内嵌事件记录，减少散落调用。
- status 响应新增：`events`（全量缓冲，1Hz 轮询体量可忽略）、`pose {x,y,yaw}`（nav_leg pose 事件回填；FAKE_DOG 模式合成直线插值）、`map {station, table, arrival_radius_m}`（来自 config，喂小地图）、`estopped` 布尔。
- nav_leg stdout 读取处：`pose` 事件除 `dist` 外把 `x/y/yaw` 也写进 manager。

### 4.2 arm_camera_daemon.py（新）

- 参数：`--device`（默认 0）、`--port`（默认 8771）、`--width/--height/--fps/--quality`。
- 从 `camera_daemon.py` 导入 `FrameHolder`、`_build_handler`、`_make_placeholder`（这三者不依赖 dimOS——仅 cv2/numpy/stdlib）。采集线程 `VideoCapture.read()` 循环 → JPEG → `holder.set_frame()`；相机打不开时持续供占位帧并按秒重试（拔插 USB 可自愈）。
- stdout 单行 readiness JSON（沿用既有 daemon 协议）；SIGTERM 干净退出。
- 解释器：优先 `DIMOS_PY`（conda，有 cv2），Mac 本地测试可用任何有 cv2 的解释器覆盖（`ARM_CAM_PY` 环境变量）。

### 4.3 dimos_cli.py — 臂相机进程管理

- 仿 `_CameraDaemon` 增加 `_ArmCameraDaemon`（懒启动、PID 跟踪、`arm_camera_ensure()`、stop 时不杀——臂相机与机器人连接无关，跟服务器生命周期走即可；`server shutdown` 时终止）。
- `ARM_CAMERA_PORT = int(os.environ.get("ARM_CAMERA_PORT", "8771"))`。

### 4.4 main.py — 代理路由 + CORS + navlog

- `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`。
- `GET /api/camera/{cam}.mjpg`（cam ∈ go2|arm）：`StreamingResponse` 逐块转发 `localhost:<port>/stream.mjpg`（urllib，无新依赖；上游断开即结束响应）。go2 路径先走既有 `camera_ensure`，arm 路径走 `arm_camera_ensure`。
- `GET /api/camera/{cam}.jpg`：单帧转发 `/snapshot.jpg`，`Cache-Control: no-store`。
- `GET /api/camera/{cam}/health`：转发 daemon `/health`（前端画"画面活着"徽章用）。
- `GET /api/vendor/navlog?lines=40`：tail 日志文件，文件不存在返回空数组。

## 5. 前端设计（frontend/ → 拷贝到 demo-site/）

### 5.1 文件结构

| 文件 | 角色 |
|---|---|
| `frontend/vendor-app.js` | 共享核心：渲染 + 轮询 + RealBackend/MockBackend 适配层 + canvas（小地图、模拟摄像头） |
| `frontend/vendor.html` | LAN 真实页壳：新布局 DOM + `window.VENDOR_MODE = "real"` |
| `frontend/vendor.js`、`demo-site/vendor.js` | 删除（被 vendor-app.js 取代） |
| `demo-site/index.html` | Vercel 页壳：同款 DOM + `window.VENDOR_MODE = "auto"`（有 ?backend= → real，否则 mock）+ 模拟声明横幅 |
| `demo-site/vendor-app.js` | `frontend/` 拷贝 |
| `demo-site/sync.sh` | `cp frontend/vendor-app.js demo-site/` 等数行 |

后端适配器接口（两实现共同契约）：
`status()`、`menu()`、`order(id)`、`confirm()`、`reset()`、`estop()`、`estopRelease()`、`navlog()`、`cameraUrl(cam)`（real → `{base}/api/camera/...`；mock → 内部 canvas 流标记）、`live`（连通性布尔，驱动 LIVE/SIM/离线徽章）。

### 5.2 布局（桌面双栏，≤900px 单栏）

```
┌────────────────────────────────────────────────┐
│ 🤖 dimOS 饮料速递  [● LIVE/SIM/离线]   [🛑 急停] │
├────────────────┬───────────────────────────────┤
│ 订单标题+阶段6格 │ [Go2 视角]  [机械臂工位]（16:9）  │
│ 事件时间线       │ 遥测条: x·y·yaw·距目标·电量       │
│ (滚动,新在上)    │ [小地图 canvas：站/桌/狗+轨迹]    │
│                │ ▸ 导航日志（<details> 折叠）      │
└────────────────┴───────────────────────────────┘
```

- **空闲态**：菜单卡片照旧居中；顶部窄条系统状态（后端连通、两路相机 health）。摄像头/地图列在空闲态也显示（点单前就能看到画面——展示价值最高的部分）。
- **E-stop 按钮**：header 常驻，红底白字，单击立即 `estop()`（无确认）。
- **急停态**：全屏红遮罩「🛑 已紧急停止 E-STOPPED」+ 触发时间 + 「解除急停」按钮（点击后变为「再点一次确认解除」，3 秒内二击生效）。
- **事件时间线**：`HH:MM:SS + 中文 + 英文小字`，新事件在顶部，高亮最新一条。
- **遥测**：1Hz 随 status 更新；电量走既有 `/api/battery`（real 模式 10s 一次，低频不给隧道添堵）。
- **小地图**：canvas 2D，自动取 station/table 外接框 + 边距缩放；画站点（▲）、桌子（■）、到达半径圈、狗（●+朝向短线）、本单轨迹 polyline（前端累积，新订单清空）。
- **摄像头 tile**：`<img src=.../go2.mjpg>`；`onerror` 或 3s 无进帧（用 health 轮询判断）→ 降级为 `setInterval` 换 `.jpg?t=` 快照轮询，tile 角标显示「快照模式」。

### 5.3 MockBackend 增强（Vercel 无隧道）

- 与真实 status 相同 schema：假事件流（同 key/文案）、假位姿（station↔table 线性插值 + 小噪声）、假电量（缓慢下降）、`estopped` 状态机同语义。
- 两路假摄像头：canvas 定时绘制 → `canvas.captureStream` 不用（复杂），直接把 canvas 元素放在 tile 里替代 `<img>`。Go2 视角 = 深色走廊单点透视线框 + 行走晃动 + 叠加时间戳/坐标 HUD；臂工位 = 俯视线框六轴臂 + 取货阶段播放抓取动画。角标「模拟画面 SIM」。
- E-stop 模拟：立即冻结假狗位置 + 事件时间线记录 + 全屏红遮罩，与真实模式体验一致。

## 6. 部署与使用

- 现场：Ascent 上 `cloudflared tunnel --url http://localhost:8090` → 打开 `https://<vercel域名>/?backend=<隧道URL>`。README 补一节「公网直播模式」。
- Vercel：`demo-site/` 静态部署照旧（`vercel.json` 不变），改完由 Claude 直接重新部署。
- LAN：`http://<Ascent>:8090/vendor` 即新 UI，零配置。

## 7. 验证计划（机器人无电版）

| 项 | 方式 |
|---|---|
| estop 状态机、事件缓冲、409 语义 | `tests/test_vendor.py` 扩展（FAKE_DOG，无 dimos 依赖） |
| 全流程 UI（下单→送达→急停→解除） | 本机 `VENDOR_FAKE_DOG=1` 起后端，浏览器过一遍 |
| 臂相机 daemon | Mac 内置摄像头实测（`--device 0`），代理路由连通 |
| 模拟模式 | 直接开 `demo-site/index.html` 无参数过全流程 |
| 隧道 + Vercel 双模式 | 本机 cloudflared 快速隧道指向本机 FAKE_DOG 后端，Vercel 预览页带 `?backend=` 实测 |
| **真机项（延后）** | Go2 相机代理出画面、真导航中 E-stop 实停、Ascent 上 USB 相机设备号确认 |

## 8. 不做（YAGNI）

多订单队列；鉴权/口令；WebRTC/WebSocket 推流；触碰冻结的 teleop 路径；臂真实运动控制；持久化事件历史；i18n 框架（继续中英硬编码双语）。

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

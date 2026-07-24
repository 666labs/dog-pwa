#!/usr/bin/env python3
"""Persistent teleop daemon — the low-latency teleop hot path.

Runs under the dimOS conda env's OWN python (it needs dimos.msgs.* and
dimos.core.transport_factory). The control-panel venv never imports dimOS; this
script is spawned as a subprocess by dimos_cli.py using the conda interpreter.

WHY THIS EXISTS: `dimos topic send /cmd_vel "Twist(...)"` costs ~2.1 s per call
— almost entirely Python + dimOS import overhead paid fresh every invocation.
For teleop (many sends while a button is held) that's a 1-2 s lag between press
and motion. This daemon pays that import cost ONCE at startup and keeps the
transport warm, so each subsequent publish is <1 ms after the first.

FIDELITY: message construction is byte-identical to `dimos.robot.cli.topic`'s
proven `topic_send` — same `_build_eval_context()`, same
`eval("Twist([lx,ly,lz],[ax,ay,az])", ctx)`, same `make_transport(topic, type)`
+ `transport.broadcast(None, msg)`. The only difference is that the eval context
and the per-topic transport are built once and cached, instead of rebuilt per
call.

PROTOCOL: line-delimited JSON on stdin, exactly one JSON ack line on stdout per
request.
  ready (once, at startup):  {"ready": true, "warmup_s": 1.38, "transport": "lcm"}
  request  (per send):       {"topic": "/cmd_vel", "lx": 0.3, "ly": 0, "lz": 0,
                              "ax": 0, "ay": 0, "az": 0}
  request  (warm/no-op):     {"cmd": "ping"}
  ack:                       {"ok": true, "ms": 0.42}
                             {"ok": true, "pong": true}
                             {"ok": false, "error": "..."}
Diagnostics go to stderr, never stdout (stdout is the ack channel).
"""
import argparse
import json
import sys
import time


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default=None,
                    help="lcm|zenoh — must match the running blueprint's transport")
    args = ap.parse_args()

    t0 = time.time()
    try:
        from dimos.core.global_config import global_config
        # Replicate the CLI's `--transport` override exactly: the global
        # option does `global_config.update(transport=...)` before anything
        # builds a transport (see dimos/robot/cli/dimos.py).
        if args.transport:
            global_config.update(transport=args.transport)
        from dimos.robot.cli.topic import _build_eval_context
        from dimos.core.transport_factory import make_transport

        eval_ctx = _build_eval_context()   # warm the message-class namespace once
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"import/init failed: {e!r}"})
        return 1

    # (topic, msg_type) -> transport, built once and reused (also avoids
    # re-paying per-topic transport/session setup, not just import cost).
    transports = {}

    def get_transport(topic, msg_type):
        key = (topic, msg_type)
        tr = transports.get(key)
        if tr is None:
            tr = make_transport(topic, msg_type)
            transports[key] = tr
        return tr

    _emit({"ready": True,
           "warmup_s": round(time.time() - t0, 3),
           "transport": str(global_config.transport)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        started = time.time()
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _emit({"ok": False, "error": f"bad json: {e}"})
            continue

        cmd = req.get("cmd")
        if cmd == "ping":
            # Warm-up / liveness probe. When a topic is given it PRIMES that
            # topic's publisher by broadcasting a single zero Twist — i.e. a
            # STOP, which is safe (no motion) and is exactly the state a robot
            # should be in right after connecting. Priming pays the ~50 ms LCM
            # first-publish setup here, at warm time, so the user's very first
            # real press is sub-millisecond instead.
            try:
                topic = req.get("topic")
                if topic:
                    twist = eval("Twist([0,0,0],[0,0,0])", eval_ctx)  # noqa: S307
                    get_transport(topic, type(twist)).broadcast(None, twist)
                _emit({"ok": True, "pong": True, "primed": bool(topic)})
            except Exception as e:  # noqa: BLE001
                _emit({"ok": False, "error": repr(e)})
            continue
        if cmd == "shutdown":
            _emit({"ok": True, "bye": True})
            break

        try:
            topic = req.get("topic", "/cmd_vel")
            lx = float(req.get("lx", 0)); ly = float(req.get("ly", 0)); lz = float(req.get("lz", 0))
            ax = float(req.get("ax", 0)); ay = float(req.get("ay", 0)); az = float(req.get("az", 0))
            # Identical expression form to dimos_cli._twist_expr / the CLI path.
            expr = f"Twist([{lx},{ly},{lz}],[{ax},{ay},{az}])"
            message = eval(expr, eval_ctx)  # noqa: S307  (same as topic_send)
            get_transport(topic, type(message)).broadcast(None, message)
            _emit({"ok": True, "ms": round((time.time() - started) * 1000, 2)})
        except Exception as e:  # noqa: BLE001
            _emit({"ok": False, "error": repr(e)})

    return 0


if __name__ == "__main__":
    sys.exit(main())

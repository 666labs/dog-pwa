#!/usr/bin/env python3
"""Persistent sport/gesture RPC daemon — separate from the teleop daemon.

Runs under the dimOS conda env's OWN python. Keeps a warm RPC client connected
to the running blueprint's bus so gesture/emote/utility commands are dispatched
without re-paying the ~1.4 s dimOS import cost per press.

THE MECHANISM (discovered by investigation, not a documented path):
GO2Connection's `sport_command`/`standup`/`set_light`/`battery_soc`/... are
`@rpc` methods, NOT pub/sub topics and NOT MCP skills — and this blueprint runs
no MCP server. `@rpc` methods are served over the SAME LCM/Zenoh bus the robot
already uses, under channels `/rpc/<instance>/<method>/{req,res}`. The canonical
client (see dimos.core.coordination.coordinator_rpc) is:

    rpc = rpc_backend()()            # LCMRPC or ZenohRPC per global_config
    rpc.start()
    result, _ = rpc.call_sync(f"{module}/{method}", ([*args], {}), rpc_timeout=T)

The module instance name for unitree-go2-basic is the class name "GO2Connection"
(verified live: GO2Connection/battery_soc returned 32). sport_command(api_id)
returns a bool = whether the robot ACCEPTED the command; we surface that.

This process does NOT publish Twist and never touches /cmd_vel — teleop is a
completely separate daemon.

PROTOCOL (line-delimited JSON, one ack line per request):
  ready:    {"ready": true, "warmup_s": 1.4, "transport": "lcm", "module": "GO2Connection"}
  request:  {"method": "sport_command", "args": [1016]}   # or {"cmd":"ping"}
  ack:      {"ok": true, "result": true, "ms": 12.3}       # result = the @rpc return
            {"ok": false, "error": "...timed out..."}
"""
import argparse
import json
import sys
import time


# HARD allowlist — the ONLY @rpc methods this daemon will ever invoke, so there
# is no arbitrary-method-invocation surface even at the daemon level (defense in
# depth beyond the HTTP endpoint's own allowlist). Narrow by design: the whole
# SPORT_CMD table is reachable through sport_command(api_id)'s single int arg, so
# these few methods cover the entire feature.
#
# NOTE deliberately does NOT include "publish_request" — that method takes an
# arbitrary (topic, data) pair, which would be real generic RPC/topic dispatch
# if exposed directly (exactly what we've avoided all along). Instead, the
# one legitimate use we have for it — reading the robot's REAL status.code for
# a sport command, since sport_command() itself discards it (dimOS bug: it does
# `return bool(publish_request(...))`, and a response dict is always truthy, so
# a firmware REJECTION looks identical to a success at that layer) — is wired
# as its own narrow special-cased command below (`cmd: "sport_status"`), with
# the topic hardcoded to the sport-command channel and the payload shape fixed
# to {"api_id": int}. Still just "controlled sport command dispatch", not a
# generic passthrough.
_ALLOWED_METHODS = frozenset({
    "sport_command", "standup", "liedown", "balance_stand",
    "stop_movement", "battery_soc",
})

_SPORT_TOPIC = "rt/api/sport/request"


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default=None, help="lcm|zenoh (match the run)")
    ap.add_argument("--module", default="GO2Connection",
                    help="RPC instance name of the connection module")
    ap.add_argument("--timeout", type=float, default=12.0,
                    help="default per-call RPC timeout (s)")
    args = ap.parse_args()

    t0 = time.time()
    try:
        from dimos.core.global_config import global_config
        if args.transport:
            global_config.update(transport=args.transport)
        from dimos.core.transport_factory import rpc_backend

        rpc = rpc_backend()()
        rpc.start()   # open the bus (Zenoh needs this before any call)
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"rpc init failed: {e!r}"})
        return 1

    module = args.module
    default_timeout = args.timeout

    _emit({"ready": True,
           "warmup_s": round(time.time() - t0, 3),
           "transport": str(global_config.transport),
           "module": module})

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
            _emit({"ok": True, "pong": True})
            continue
        if cmd == "shutdown":
            _emit({"ok": True, "bye": True})
            break
        if cmd == "sport_status":
            # Same physical action as sport_command(api_id) — this DOES send
            # the command to the robot, it just also reads back the real
            # status.code instead of discarding it. Topic and payload shape
            # are fixed here, not caller-controlled.
            api_id = req.get("api_id")
            if not isinstance(api_id, int):
                _emit({"ok": False, "error": "sport_status requires integer 'api_id'"})
                continue
            mod = req.get("module", module)
            timeout = float(req.get("timeout", default_timeout))
            try:
                result, _unsub = rpc.call_sync(
                    f"{mod}/publish_request",
                    ([_SPORT_TOPIC, {"api_id": api_id}], {}),
                    rpc_timeout=timeout,
                )
                data = result.get("data", {}) if isinstance(result, dict) else {}
                header = data.get("header", {}) if isinstance(data, dict) else {}
                status = header.get("status", {}) if isinstance(header, dict) else {}
                code = status.get("code") if isinstance(status, dict) else None
                _emit({"ok": True, "code": code, "accepted": code == 0,
                       "raw": result, "ms": round((time.time() - started) * 1000, 2)})
            except Exception as e:  # noqa: BLE001
                _emit({"ok": False, "error": f"{type(e).__name__}: {e}",
                       "ms": round((time.time() - started) * 1000, 2)})
            continue

        method = req.get("method")
        if not method:
            _emit({"ok": False, "error": "missing 'method'"})
            continue
        if method not in _ALLOWED_METHODS:
            _emit({"ok": False, "error": f"method '{method}' not allowed"})
            continue
        call_args = req.get("args", [])
        if not isinstance(call_args, list):
            call_args = [call_args]
        mod = req.get("module", module)
        timeout = float(req.get("timeout", default_timeout))
        try:
            result, _unsub = rpc.call_sync(
                f"{mod}/{method}", ([*call_args], {}), rpc_timeout=timeout)
            _emit({"ok": True, "result": result,
                   "ms": round((time.time() - started) * 1000, 2)})
        except Exception as e:  # noqa: BLE001
            # TimeoutError, or an exception propagated from the remote handler.
            _emit({"ok": False, "error": f"{type(e).__name__}: {e}",
                   "ms": round((time.time() - started) * 1000, 2)})

    try:
        rpc.stop()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

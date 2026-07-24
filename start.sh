#!/usr/bin/env bash
#
# dimOS Control — one-shot setup + launch.
#
# Run this on the server/devbox:   ./start.sh
#   1. prepares the host for dimOS's default (LCM) transport (idempotent; sudo
#      prompts inline only for whatever is missing after a reboot),
#   2. starts the control-panel server (frontend + API on one port),
#   3. prints the exact URL to open on the iPad.
#
set -uo pipefail
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

PORT=8090

# --server-only: internal re-entry flag used by the privilege-drop below.
# Never pass this yourself — just run `./start.sh` or `sudo ./start.sh`.
SERVER_ONLY=0
if [ "${1:-}" = "--server-only" ]; then
  SERVER_ONLY=1
fi

if [ "$SERVER_ONLY" -eq 0 ]; then
  # 1. Host prerequisites for LCM (the default transport the real robot
  #    blueprints use). Aborts with clear manual instructions if it can't
  #    apply them. Running as `sudo ./start.sh` covers all three prerequisite
  #    commands with a single upfront password prompt instead of one prompt
  #    per command.
  ./setup.sh || exit 1

  # If we got here as root, setup.sh just ran with root already — no more
  # sudo prompts needed. But the server itself has no business running as
  # root: it's a long-lived process bound to 0.0.0.0 (reachable by anyone on
  # the venue Wi-Fi) that takes robot-IP/blueprint input from the network and
  # shells out to the dimos CLI. Keeping it unprivileged limits the blast
  # radius if that input handling ever has a bug. So: re-launch ourselves as
  # the invoking non-root user for the actual server, skipping setup.sh the
  # second time around (--server-only).
  #
  # $SUDO_USER is only set when invoked as exactly `sudo ./start.sh` from a
  # normal login shell — it's unset if you're already in a root shell (`su`,
  # `sudo -i`, etc.), which silently left the server running as root before.
  # Fall back to `logname` (the original login user, survives su/sudo -i),
  # then to whoever owns this directory (reliable in practice: this project
  # belongs to the person who should run it) — in that priority order.
  if [ "$(id -u)" -eq 0 ]; then
    TARGET_USER="${SUDO_USER:-}"
    [ -z "$TARGET_USER" ] && TARGET_USER="$(logname 2>/dev/null || true)"
    [ -z "$TARGET_USER" ] && TARGET_USER="$(stat -c '%U' "$SCRIPT_DIR" 2>/dev/null || true)"

    if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
      echo "  [warn] running as root and couldn't determine a non-root user to drop to" >&2
      echo "  [warn] (checked \$SUDO_USER, logname, and directory ownership — all empty or root)" >&2
      echo "  [warn] continuing as root rather than guessing — fix by running as the intended user" >&2
      echo "  [warn] via 'sudo ./start.sh' from their login shell, not from an already-root shell" >&2
    else
      echo "  [ok]   host prerequisites applied as root — dropping to user '$TARGET_USER' for the server itself"
      exec sudo -u "$TARGET_USER" -H bash -c 'cd "$0" && exec "$1" --server-only' \
        "$SCRIPT_DIR" "$SCRIPT_DIR/start.sh"
    fi
  fi
fi

# 2. Detect this machine's LAN IP (not localhost/0.0.0.0 — the iPad needs a real
#    reachable address). The `src` of the default route is the interface that
#    actually reaches the LAN; this avoids docker bridges and VPN/proxy tunnels
#    (e.g. a `ip route get 1.1.1.1` probe can get hijacked by a local proxy).
LAN_IP=$(ip -4 route show default 2>/dev/null \
  | awk '{ for (i=1;i<=NF;i++) if ($i=="src") { print $(i+1); exit } }')
# Fallback: first non-loopback, non-docker address from hostname -I.
if [ -z "${LAN_IP:-}" ]; then
  LAN_IP=$(hostname -I 2>/dev/null \
    | tr ' ' '\n' | grep -vE '^(127\.|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[0-1]\.|198\.18\.)' | head -1)
fi
[ -z "${LAN_IP:-}" ] && LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "${LAN_IP:-}" ] && LAN_IP="<this-machine-LAN-IP>"

# 3. Friendly launch banner.
cat <<EOF

  ============================================================
    dimOS Control  ->  http://$LAN_IP:$PORT
  ------------------------------------------------------------
    On the iPad (same Wi-Fi network), open Safari to:

        http://$LAN_IP:$PORT

    Then tap  Share  ->  "Add to Home Screen"  to install it
    as the dimOS Control app.

    Keep this terminal open. Press Ctrl-C to stop the server.
  ============================================================

EOF

# 4. Launch the server in the foreground (binds 0.0.0.0 so the iPad can reach it;
#    the frontend calls the API via relative paths, so this is redeploy-safe).
exec ./venv/bin/python -m uvicorn main:app \
  --app-dir backend \
  --host 0.0.0.0 --port "$PORT"

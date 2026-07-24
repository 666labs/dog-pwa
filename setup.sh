#!/usr/bin/env bash
#
# Idempotent host setup for dimOS's DEFAULT (LCM) transport.
#
# dimOS's LCM stack requires three things that reset on every reboot and each
# need root. Values/checks here mirror dimOS's own configurator exactly
# (site-packages/dimos/protocol/service/system_configurator/lcm.py):
#
#   1. loopback multicast : MULTICAST flag on `lo`   (critical)
#   2. multicast route    : 224.0.0.0/4 dev lo       (critical)
#   3. socket buffers      : net.core.rmem_max and net.core.rmem_default
#                            each >= 67108864 (64 MB)  (recommended)
#
# Each step is checked first and only applied via `sudo` if actually missing,
# so re-running after the fix is in place does NOT prompt for a password again.
# Run this directly in your own terminal (sudo will prompt inline for your
# password on the first change of a fresh boot).
#
set -uo pipefail

RMEM_TARGET=67108864   # 64 MB — dimOS IDEAL_RMEM_SIZE

# No sudo needed if we're already root (e.g. `sudo ./start.sh`).
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

fail() {
  echo                                                                    >&2
  echo "  ERROR: $1"                                                      >&2
  echo                                                                    >&2
  echo "  Could not apply the LCM prerequisites automatically."           >&2
  echo "  Run these yourself in a terminal, then re-run this script:"     >&2
  echo "      sudo ip link set lo multicast on"                           >&2
  echo "      sudo ip route add 224.0.0.0/4 dev lo"                       >&2
  echo "      sudo sysctl -w net.core.rmem_max=$RMEM_TARGET"              >&2
  echo "      sudo sysctl -w net.core.rmem_default=$RMEM_TARGET"          >&2
  echo                                                                    >&2
  exit 1
}

echo "Checking dimOS LCM prerequisites..."

# 1. loopback multicast ------------------------------------------------------
if ip -o link show lo 2>/dev/null | grep -qw MULTICAST; then
  echo "  [ok]   loopback multicast already enabled"
else
  echo "  [fix]  enabling loopback multicast (sudo)"
  $SUDO ip link set lo multicast on || fail "failed to enable loopback multicast"
  echo "  [done] loopback multicast enabled"
fi

# 2. multicast route ---------------------------------------------------------
if ip -o route show 224.0.0.0/4 2>/dev/null | grep -q .; then
  echo "  [ok]   multicast route 224.0.0.0/4 already present"
else
  echo "  [fix]  adding multicast route 224.0.0.0/4 dev lo (sudo)"
  $SUDO ip route add 224.0.0.0/4 dev lo || fail "failed to add multicast route"
  echo "  [done] multicast route added"
fi

# 3+4. socket buffers --------------------------------------------------------
for key in net.core.rmem_max net.core.rmem_default; do
  cur=$(sysctl -n "$key" 2>/dev/null || echo 0)
  case "$cur" in ''|*[!0-9]*) cur=0 ;; esac
  if [ "$cur" -ge "$RMEM_TARGET" ]; then
    echo "  [ok]   $key already >= $RMEM_TARGET (current $cur)"
  else
    echo "  [fix]  setting $key=$RMEM_TARGET (sudo)"
    $SUDO sysctl -w "$key=$RMEM_TARGET" >/dev/null || fail "failed to set $key"
    echo "  [done] $key set to $RMEM_TARGET"
  fi
done


# Final hard verification — don't just trust that the commands above returned
# 0. Re-read live kernel state and fail loudly if it didn't actually stick
# (this is what should have caught it if it silently doesn't take effect).
problems=""
ip -o link show lo 2>/dev/null | grep -qw MULTICAST || problems="${problems}  - loopback multicast is still off\n"
ip -o route show 224.0.0.0/4 2>/dev/null | grep -q . || problems="${problems}  - multicast route 224.0.0.0/4 is still missing\n"
for key in net.core.rmem_max net.core.rmem_default; do
  cur=$(sysctl -n "$key" 2>/dev/null || echo 0)
  case "$cur" in ''|*[!0-9]*) cur=0 ;; esac
  [ "$cur" -ge "$RMEM_TARGET" ] || problems="${problems}  - $key is still below $RMEM_TARGET (current $cur)\n"
done

if [ -n "$problems" ]; then
  echo                                                                        >&2
  echo "  ERROR: prerequisites were applied but did not stick on re-check:"   >&2
  printf "%b" "$problems"                                                     >&2
  echo                                                                        >&2
  echo "  Something on this machine is resetting them right after they're set"  >&2
  echo "  (e.g. a network manager reconnect/route-flush) — this is not a"     >&2
  echo "  simple reboot-reset case. Re-run this script; if it fails the same" >&2
  echo "  way again, apply the four commands manually immediately before"    >&2
  echo "  launching dimOS, right before you need it:"                        >&2
  echo "      sudo ip link set lo multicast on"                              >&2
  echo "      sudo ip route add 224.0.0.0/4 dev lo"                          >&2
  echo "      sudo sysctl -w net.core.rmem_max=$RMEM_TARGET"                 >&2
  echo "      sudo sysctl -w net.core.rmem_default=$RMEM_TARGET"             >&2
  echo                                                                        >&2
  exit 1
fi

echo "LCM prerequisites satisfied and verified. dimOS default (LCM) transport is ready."

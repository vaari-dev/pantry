#!/usr/bin/env bash
# Tailscale (userspace) with a SOCKS5 proxy on :1055 exiting at home, for Chrome only.
# State is on the volume, so TS_AUTHKEY is needed once. Then runs the command with a display.
set -euo pipefail

if [[ -n "${TS_EXIT_NODE:-}" ]]; then
  state=/data/tailscale
  mkdir -p "$state"
  tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 \
             --statedir="$state" --socket="$state/tailscaled.sock" >"$state/tailscaled.log" 2>&1 &
  ts() { tailscale --socket="$state/tailscaled.sock" "$@"; }
  for _ in $(seq 30); do
    ts up ${TS_AUTHKEY:+--authkey="$TS_AUTHKEY"} --hostname="${TS_HOSTNAME:-grocery-bot}" \
       --exit-node="$TS_EXIT_NODE" >/dev/null 2>&1 && break
    sleep 2
  done
  ts status --peers=false >/dev/null || { echo "tailscale did not come up" >&2; tail -20 "$state/tailscaled.log" >&2; exit 1; }
  # Up is not routed: orders sent before the exit node carries traffic fail.
  reach() { curl -s -o /dev/null --max-time 10 -x socks5h://127.0.0.1:1055 "${SHOP_URL:?}"; }
  for _ in $(seq 20); do reach && break; sleep 2; done
  reach || { echo "the shop is not reachable through the exit node" >&2; exit 1; }
fi

# Not xvfb-run: it would take the stop signal the bot needs to finish its jobs.
rm -f /tmp/.X99-lock  # left by the last run: restarts keep /tmp
Xvfb :99 -screen 0 1024x700x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &
for _ in $(seq 50); do [[ -S /tmp/.X11-unix/X99 ]] && break; sleep 0.1; done
[[ -S /tmp/.X11-unix/X99 ]] || { echo "Xvfb did not start" >&2; tail -20 /tmp/xvfb.log >&2; exit 1; }
export DISPLAY=:99
exec "$@"

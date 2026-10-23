#!/usr/bin/env bash
# Tailscale (userspace) with a SOCKS5 proxy on :1055 exiting at home, for Chrome only.
# State is on the volume, so TS_AUTHKEY is needed once. Then runs the command under Xvfb.
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
fi

exec xvfb-run -a -s "-screen 0 1024x700x16" "$@"

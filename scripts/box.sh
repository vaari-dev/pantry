#!/usr/bin/env bash
# Runs on the box, copied next to compose.yaml by the deploy workflow and called over `ssh box`.
# Only ever touches its own directory, the grocery-bot compose project and its volume.
#
#   deploy IMAGE  pull IMAGE and (re)start
#   set-login [if-missing]  install the shop session from stdin (if-missing: only when there is none)
#   check         self-check inside the running bot (login, address, home exit)
#   remove        stop and delete everything, volume included
set -euo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=grocery-bot
compose() { docker compose --env-file .image "$@"; }
in_bot() { compose exec -T bot "$@"; }

wait_healthy() {
  for _ in $(seq 36); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q bot)" 2>/dev/null)" = healthy ] && return
    sleep 5
  done
  compose logs --tail 50 bot >&2
  echo "the bot did not become healthy" >&2
  return 1
}


case "${1:?command}" in
  deploy)
    export APP_IMAGE="${2:?image}"
    echo "APP_IMAGE=$APP_IMAGE" > .image  # so later commands (and reboots) use the same image
    compose pull -q
    compose up -d --remove-orphans
    wait_healthy
    docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/vaari-dev/grocery-bot" >/dev/null
    ;;
  set-login)
    if [ "${2:-}" = if-missing ] && in_bot test -s /data/session.json; then
      echo "keeping the bot's shop session"
      exit 0
    fi
    in_bot sh -c 'umask 077; cat > /data/session.json'
    ;;
  check)
    in_bot xvfb-run -a python -m grocerybot check
    ;;
  remove)
    compose down --volumes --rmi all --remove-orphans
    rm -rf "$PWD"
    ;;
  *)
    echo "unknown command: $1" >&2
    exit 2
    ;;
esac

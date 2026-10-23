#!/usr/bin/env bash
# On the box, via `ssh box`. Touches only this directory, its compose project and volume.
#   deploy IMAGE | set-login [if-missing] (session on stdin) | check | logs | remove
set -euo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=grocery-bot
compose() { docker compose --env-file .image "$@"; }
in_bot() { compose exec -T grocery-bot "$@"; }

wait_healthy() {
  for _ in $(seq 36); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q grocery-bot)" 2>/dev/null)" = healthy ] && return
    sleep 5
  done
  compose logs --tail 50 grocery-bot >&2
  echo "the bot did not become healthy" >&2
  return 1
}


case "${1:?command}" in
  deploy)
    export APP_IMAGE="${2:?image}"
    echo "APP_IMAGE=$APP_IMAGE" > .image  # for later commands
    compose pull -q
    for _ in $(seq 60); do  # let a running order finish (up to 10 min)
      in_bot test -e /tmp/busy 2>/dev/null || break
      sleep 10
    done
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
  logs)
    docker inspect -f 'health: {{.State.Health.Status}}, started {{.State.StartedAt}}' "$(compose ps -q grocery-bot)"
    compose logs --no-log-prefix --tail 200 grocery-bot
    ;;
  remove)
    compose down --volumes --rmi all --remove-orphans
    sudo rm -rf -- "$PWD"
    ;;
  *)
    echo "unknown command: $1" >&2
    exit 2
    ;;
esac

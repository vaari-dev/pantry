#!/usr/bin/env bash
# Runs on the box, copied next to compose.yaml by the deploy workflow and called over `ssh box`.
# Only ever touches its own directory, the grocery-bot compose project and its volume.
#
#   deploy IMAGE  pull IMAGE and (re)start
#   check         self-check inside the running bot
#   logs          the bot's recent log lines and health
#   remove        stop and delete everything, volume included
set -euo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=grocery-bot
compose() { docker compose --env-file .image "$@"; }
in_bot() { compose exec -T grocery-bot "$@"; }

wait_healthy() {
  for _ in $(seq 72); do
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
    echo "APP_IMAGE=$APP_IMAGE" > .image  # so later commands (and reboots) use the same image
    compose pull -q
    compose up -d --remove-orphans  # the old bot finishes its jobs first: stop_grace_period
    wait_healthy
    docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/vaari-dev/grocery-bot" >/dev/null
    ;;
  check)
    in_bot python -m grocerybot check
    ;;
  logs)
    docker inspect -f 'health: {{.State.Health.Status}}, started {{.State.StartedAt}}' "$(compose ps -q grocery-bot)"
    compose logs --no-log-prefix --tail 200 grocery-bot
    ;;
  remove)
    compose down --volumes --rmi all --remove-orphans
    sudo rm -rf -- "$PWD"  # the directory sits in /opt
    ;;
  *)
    echo "unknown command: $1" >&2
    exit 2
    ;;
esac

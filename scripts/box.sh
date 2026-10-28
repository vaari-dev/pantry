#!/usr/bin/env bash
# Runs on the box, copied next to compose.yaml by the deploy workflow and called over `ssh box`.
# Only ever touches its own directory, the pantry compose project and its volume (and, once, the old grocery-bot ones).
#
#   deploy IMAGE  pull IMAGE and (re)start
#   check         self-check inside the running bot
#   logs          the bot's recent log lines and health
#   remove        stop and delete everything, volume included
set -euo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=pantry
OLD=grocery-bot  # the project's name before the rename, in /opt/grocery-bot
compose() { docker compose --env-file .image "$@"; }
in_bot() { compose exec -T pantry "$@"; }

wait_healthy() {
  for _ in $(seq 72); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q pantry)" 2>/dev/null)" = healthy ] && return
    sleep 5
  done
  compose logs --tail 50 pantry >&2
  echo "the bot did not become healthy" >&2
  return 1
}

# Once, while the bot still runs as the old project: stop it (it finishes its jobs first) and copy its data
# into this project's volume. The old bot is deleted only once the new one is healthy; until then any
# failure stops the new bot and starts the old one again, and the next deploy starts over.
old() { docker ps -aq --filter "label=com.docker.compose.project=$OLD"; }

from_old() {
  [ -n "$(old)" ] || return 0
  docker volume inspect "${OLD}_data" >/dev/null  # never copy a missing volume as an empty one
  # shellcheck disable=SC2046  # one id per word
  docker stop --time 600 $(old) >/dev/null
  if ! compose create ||
     ! docker run --rm --user 0 --entrypoint sh -v "${OLD}_data:/from:ro" -v "${COMPOSE_PROJECT_NAME}_data:/data" \
         "$APP_IMAGE" -c 'find /data -mindepth 1 -delete && cp -a /from/. /data/'; then
    back_to_old
  fi
}

back_to_old() {
  compose stop
  # shellcheck disable=SC2046
  docker start $(old) >/dev/null
  echo "the move from $OLD failed; the old bot is running again" >&2
  return 1
}

old_gone() {
  [ -n "$(old)" ] || return 0
  # shellcheck disable=SC2046
  docker rm $(old) >/dev/null
  docker volume rm "${OLD}_data" >/dev/null
  docker image prune -af --filter "label=org.opencontainers.image.source=https://github.com/vaari-dev/$OLD" >/dev/null
  sudo rm -rf -- "/opt/$OLD"
}

case "${1:?command}" in
  deploy)
    export APP_IMAGE="${2:?image}"
    echo "APP_IMAGE=$APP_IMAGE" > .image  # so later commands (and reboots) use the same image
    compose pull -q
    from_old
    compose up -d --remove-orphans  # the old bot finishes its jobs first: stop_grace_period
    wait_healthy || { [ -z "$(old)" ] || back_to_old; exit 1; }
    old_gone
    docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/vaari-dev/pantry" >/dev/null
    ;;
  check)
    in_bot python -m pantry check
    ;;
  logs)
    docker inspect -f 'health: {{.State.Health.Status}}, started {{.State.StartedAt}}' "$(compose ps -q pantry)"
    compose logs --no-log-prefix --tail 200 pantry
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

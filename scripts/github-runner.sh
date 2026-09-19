#!/usr/bin/env bash
# Manage repository-scoped fallback runners without persisting registration tokens.

set -euo pipefail

COMMAND="${1:-}"
ROLE="${2:-all}"
IMAGE_NAME="${RUNNER_IMAGE:-esp32cam-actions-runner:2.336.0}"
CONTAINER_PREFIX="${RUNNER_CONTAINER_PREFIX:-esp32cam-actions-runner}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

usage() {
  echo "Usage: scripts/github-runner.sh {build|install|status|logs|start|stop|unregister} [gate|fallback|all]" >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "github-runner: required command not found: $1" >&2
    exit 2
  }
}

repository_name() {
  if [[ -n "${GH_REPO:-}" ]]; then
    printf '%s\n' "$GH_REPO"
  else
    gh repo view --json nameWithOwner --jq .nameWithOwner
  fi
}

validate_role() {
  case "$1" in
    gate|fallback|all) ;;
    *)
      echo "github-runner: unsupported role: $1" >&2
      exit 2
      ;;
  esac
}

roles() {
  if [[ "$ROLE" == "all" ]]; then
    printf '%s\n' gate fallback
  else
    printf '%s\n' "$ROLE"
  fi
}

container_name() {
  printf '%s-%s\n' "$CONTAINER_PREFIX" "$1"
}

runner_name() {
  printf '%s-%s\n' "${RUNNER_NAME_PREFIX:-esp32cam-$(hostname)}" "$1"
}

runner_label() {
  printf 'esp32cam-ci-%s\n' "$1"
}

memory_limit() {
  if [[ "$1" == "gate" ]]; then
    printf '%s\n' 2g
  else
    printf '%s\n' 8g
  fi
}

runner_id() {
  local repository="$1"
  local name="$2"
  gh api "repos/${repository}/actions/runners" \
    --jq ".runners[] | select(.name == \"${name}\") | .id"
}

install_runner() {
  local repository="$1"
  local role="$2"
  local container
  local name
  local label
  local registration_token
  container="$(container_name "$role")"
  name="$(runner_name "$role")"
  label="$(runner_label "$role")"

  if docker container inspect "$container" >/dev/null 2>&1; then
    echo "github-runner: container already exists: $container" >&2
    return 2
  fi
  registration_token="$(
    gh api --method POST \
      "repos/${repository}/actions/runners/registration-token" \
      --jq .token
  )"
  docker run \
    --detach \
    --name "$container" \
    --hostname "$name" \
    --restart=unless-stopped \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --pids-limit=1024 \
    --memory="$(memory_limit "$role")" \
    --tmpfs /run/runner-secrets:rw,noexec,nosuid,nodev,size=64k \
    --env "RUNNER_REPOSITORY=${repository}" \
    --env "RUNNER_NAME=${name}" \
    --env "RUNNER_LABELS=${label}" \
    "$IMAGE_NAME"
  for _attempt in $(seq 1 50); do
    if docker exec "$container" test -p /run/runner-secrets/registration-token; then
      break
    fi
    sleep 0.1
  done
  if ! docker exec "$container" test -p /run/runner-secrets/registration-token; then
    echo "github-runner: registration token pipe was not created: $container" >&2
    return 2
  fi
  printf '%s\n' "$registration_token" | docker exec --interactive "$container" \
    sh -c 'cat > /run/runner-secrets/registration-token'
  unset registration_token
}

show_status() {
  local repository="$1"
  local role="$2"
  local container
  local name
  container="$(container_name "$role")"
  name="$(runner_name "$role")"
  docker container inspect "$container" \
    --format 'container={{.Name}} state={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}'
  gh api "repos/${repository}/actions/runners" \
    --jq ".runners[] | select(.name == \"${name}\") | {id, name, status, busy, labels: [.labels[].name]}"
}

unregister_runner() {
  local repository="$1"
  local role="$2"
  local container
  local name
  local id
  container="$(container_name "$role")"
  name="$(runner_name "$role")"
  id="$(runner_id "$repository" "$name")"
  if [[ -z "$id" ]]; then
    echo "github-runner: GitHub runner was not found: $name" >&2
    return 2
  fi
  gh api --method DELETE "repos/${repository}/actions/runners/${id}"
  docker stop --timeout 30 "$container"
  docker rm "$container"
}

validate_role "$ROLE"

case "$COMMAND" in
  build)
    require_command docker
    docker build --tag "$IMAGE_NAME" "$REPO_ROOT/infra/github-actions-runner"
    ;;
  install)
    require_command docker
    require_command gh
    repository="$(repository_name)"
    while IFS= read -r role; do
      install_runner "$repository" "$role"
    done < <(roles)
    ;;
  status)
    require_command docker
    require_command gh
    repository="$(repository_name)"
    while IFS= read -r role; do
      show_status "$repository" "$role"
    done < <(roles)
    ;;
  logs)
    require_command docker
    while IFS= read -r role; do
      docker logs --tail 200 "$(container_name "$role")"
    done < <(roles)
    ;;
  start)
    require_command docker
    while IFS= read -r role; do
      docker start "$(container_name "$role")"
    done < <(roles)
    ;;
  stop)
    require_command docker
    while IFS= read -r role; do
      docker stop --timeout 30 "$(container_name "$role")"
    done < <(roles)
    ;;
  unregister)
    require_command docker
    require_command gh
    repository="$(repository_name)"
    while IFS= read -r role; do
      unregister_runner "$repository" "$role"
    done < <(roles)
    ;;
  *)
    usage
    exit 2
    ;;
esac

#!/usr/bin/env bash

set -euo pipefail

cd /opt/actions-runner

if [[ ! -f .runner ]]; then
  : "${RUNNER_REPOSITORY:?RUNNER_REPOSITORY is required}"
  : "${RUNNER_NAME:?RUNNER_NAME is required}"
  : "${RUNNER_LABELS:?RUNNER_LABELS is required}"
  case "$RUNNER_LABELS" in
    esp32cam-ci-gate|esp32cam-ci-fallback) ;;
    *)
      echo "runner-entrypoint: unsupported runner label: $RUNNER_LABELS" >&2
      exit 2
      ;;
  esac

  token_pipe=/run/runner-secrets/registration-token
  umask 077
  mkfifo "$token_pipe"
  IFS= read -r -s registration_token < "$token_pipe"
  rm -f "$token_pipe"
  if [[ -z "$registration_token" ]]; then
    echo "runner-entrypoint: registration token was not provided on stdin" >&2
    exit 2
  fi

  ./config.sh \
    --url "https://github.com/${RUNNER_REPOSITORY}" \
    --token "$registration_token" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work _work \
    --unattended \
    --replace \
    --disableupdate
  unset registration_token
fi

exec ./run.sh

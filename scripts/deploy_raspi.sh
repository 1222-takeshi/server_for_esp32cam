#!/usr/bin/env bash
# Deploy the server to the Raspberry Pi and restart it.
#
# The repository is private and the Raspi deployment is a plain copy (not a
# git clone), so this script pushes the tracked files from the local checkout
# over rsync — no GitHub credentials ever live on the Pi. Untracked runtime
# state on the Pi (.env, data/, uploads/, models/, .venv/) is never touched.
#
# Usage:
#   ./scripts/deploy_raspi.sh              # deploy HEAD of the current checkout
#   DEPLOY_HOST=ubuntu@other.local ./scripts/deploy_raspi.sh
#
# What it does:
#   1. rsync all git-tracked files to the Pi
#   2. pip install -e . into the Pi's venv (editable, so future syncs apply
#      without reinstall unless dependencies change)
#   3. restart the systemd user service and verify /healthz/ready

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-ubuntu@raspi-server.local}"
DEPLOY_DIR="${DEPLOY_DIR:-server_for_esp32cam}"
SERVICE="${DEPLOY_SERVICE:-esp32cam-server}"

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

echo "==> Deploying $(git rev-parse --short HEAD) ($(git branch --show-current)) to ${DEPLOY_HOST}:${DEPLOY_DIR}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "!! Working tree has uncommitted changes — they WILL be deployed." >&2
fi

git ls-files -z | rsync -az --files-from=- --from0 . "${DEPLOY_HOST}:${DEPLOY_DIR}/"

echo "==> Installing package (editable) and restarting ${SERVICE}"
ssh "$DEPLOY_HOST" "cd ${DEPLOY_DIR} \
  && .venv/bin/pip install -q -e . \
  && systemctl --user restart ${SERVICE}"

echo "==> Waiting for /healthz/ready"
for i in $(seq 1 15); do
  if ssh "$DEPLOY_HOST" "curl -sf http://127.0.0.1:8080/healthz/ready" >/dev/null 2>&1; then
    ssh "$DEPLOY_HOST" "curl -s http://127.0.0.1:8080/healthz/ready"; echo
    echo "==> Deploy OK"
    exit 0
  fi
  sleep 2
done

echo "!! Server did not become ready within 30s. Check:" >&2
echo "   ssh ${DEPLOY_HOST} journalctl --user -u ${SERVICE} -n 50" >&2
exit 1

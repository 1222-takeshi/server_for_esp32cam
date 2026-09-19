#!/usr/bin/env bash

set -euo pipefail

MODE="${1:-}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$REPO_ROOT"

export PIP_DISABLE_PIP_VERSION_CHECK=1

run_test() {
  "$PYTHON_BIN" -m pip install --upgrade pip setuptools
  "$PYTHON_BIN" -m pip install -e ".[dev]"
  "$PYTHON_BIN" -m ruff check .
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON_BIN" -m pytest \
    -p pytest_cov \
    --cov=script \
    --cov-report=xml \
    --cov-report=term-missing \
    --cov-fail-under=80 \
    --durations=10
}

run_audit() {
  "$PYTHON_BIN" -m pip install --upgrade pip setuptools
  "$PYTHON_BIN" -m pip install --require-hashes -r requirements.lock.txt
  "$PYTHON_BIN" -m pip install --no-deps -e .
  "$PYTHON_BIN" -m pip_audit --desc on
}

case "$MODE" in
  test)
    run_test
    ;;
  audit)
    run_audit
    ;;
  *)
    echo "Usage: scripts/ci/run-check.sh {test|audit}" >&2
    exit 2
    ;;
esac

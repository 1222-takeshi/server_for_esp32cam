#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"

run_test() {
  python -m pip install --upgrade pip setuptools
  python -m pip install -e ".[dev]"
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
    -p pytest_cov \
    --cov=script \
    --cov-report=term-missing \
    --cov-fail-under=80 \
    --durations=10
}

run_dependency_audit() {
  python -m pip install --upgrade pip setuptools
  python -m pip install --require-hashes -r requirements.lock.txt
  python -m pip install --no-deps -e .
  python -m pip_audit --desc on
}

case "$mode" in
  test)
    run_test
    ;;
  dependency-audit)
    run_dependency_audit
    ;;
  all)
    run_test
    run_dependency_audit
    ;;
  *)
    echo "Usage: scripts/ci/run-check.sh {test|dependency-audit|all}" >&2
    exit 2
    ;;
esac

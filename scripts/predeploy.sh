#!/usr/bin/env bash
#
# Pre-deploy gate: format check, lint, and test the project locally.
# Run this before any scripts/deploy-*.sh to catch regressions early.
#
# Usage:
#   scripts/predeploy.sh                # check-only, fails if formatting/lint/tests fail
#   scripts/predeploy.sh --fix          # auto-apply ruff format + ruff --fix
#   scripts/predeploy.sh --skip-tests   # skip pytest (fast iterative loop)
#   scripts/predeploy.sh --tests-only   # skip format + lint, run pytest only
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

FIX_MODE=false
SKIP_TESTS=false
TESTS_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix)        FIX_MODE=true;   shift ;;
    --skip-tests) SKIP_TESTS=true; shift ;;
    --tests-only) TESTS_ONLY=true; shift ;;
    -h|--help)
      awk '/^#!/{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Tool preflight
need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: '$1' is not on PATH." >&2
    echo "       Install dev dependencies: pip install -r requirements-dev.txt" >&2
    exit 1
  }
}

if ! $TESTS_ONLY; then
  need ruff
fi
if ! $SKIP_TESTS; then
  need pytest
fi

PY_TARGETS=(config.py logging_setup.py server.py providers tests)

run_format() {
  if $FIX_MODE; then
    echo "==> ruff format (apply)"
    ruff format "${PY_TARGETS[@]}"
  else
    echo "==> ruff format --check"
    if ! ruff format --check "${PY_TARGETS[@]}"; then
      echo "" >&2
      echo "Formatting drift detected. Run: scripts/predeploy.sh --fix" >&2
      exit 1
    fi
  fi
}

run_lint() {
  if $FIX_MODE; then
    echo "==> ruff check --fix"
    ruff check --fix "${PY_TARGETS[@]}"
  else
    echo "==> ruff check"
    ruff check "${PY_TARGETS[@]}"
  fi
}

run_tests() {
  echo "==> pytest"
  pytest -v
}

if ! $TESTS_ONLY; then
  run_format
  run_lint
fi

if ! $SKIP_TESTS; then
  run_tests
fi

echo ""
echo "================================================================================"
echo "All pre-deploy checks passed."
echo "Next: scripts/deploy-{miro,figma,lucid}.sh"
echo "================================================================================"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CI_MIN_FREE_DISK_GB=2 \
  CI_AVAILABLE_DISK_KB_OVERRIDE=$((3 * 1024 * 1024)) \
  "$ROOT/scripts/check_ci_capacity.sh" >/dev/null

if CI_MIN_FREE_DISK_GB=4 \
    CI_AVAILABLE_DISK_KB_OVERRIDE=$((3 * 1024 * 1024)) \
    "$ROOT/scripts/check_ci_capacity.sh" >/dev/null 2>&1; then
  echo "check_ci_capacity.sh unexpectedly accepted insufficient disk" >&2
  exit 70
fi

if CI_MIN_FREE_DISK_GB=not-a-number \
    CI_AVAILABLE_DISK_KB_OVERRIDE=$((3 * 1024 * 1024)) \
    "$ROOT/scripts/check_ci_capacity.sh" >/dev/null 2>&1; then
  echo "check_ci_capacity.sh unexpectedly accepted an invalid minimum" >&2
  exit 70
fi

CI_MIN_FREE_DISK_GB=0 "$ROOT/scripts/check_ci_capacity.sh" >/dev/null

echo "check_ci_capacity.sh smoke test passed"

#!/usr/bin/env bash
set -euo pipefail

MIN_FREE_DISK_GB="${CI_MIN_FREE_DISK_GB:-0}"
CHECK_PATH="${CI_CAPACITY_PATH:-$PWD}"
AVAILABLE_DISK_KB_OVERRIDE="${CI_AVAILABLE_DISK_KB_OVERRIDE:-}"

fail() {
  echo "error: $*" >&2
  exit 70
}

case "$MIN_FREE_DISK_GB" in
  ''|*[!0-9]*)
    fail "CI_MIN_FREE_DISK_GB must be a non-negative integer, got '$MIN_FREE_DISK_GB'"
    ;;
esac

if [[ "$MIN_FREE_DISK_GB" == "0" ]]; then
  echo "CI capacity check skipped because CI_MIN_FREE_DISK_GB=0"
  exit 0
fi

if [[ ! -e "$CHECK_PATH" ]]; then
  fail "capacity check path does not exist: $CHECK_PATH"
fi

available_disk_kb() {
  if [[ -n "$AVAILABLE_DISK_KB_OVERRIDE" ]]; then
    case "$AVAILABLE_DISK_KB_OVERRIDE" in
      *[!0-9]*)
        fail "CI_AVAILABLE_DISK_KB_OVERRIDE must be an integer, got '$AVAILABLE_DISK_KB_OVERRIDE'"
        ;;
    esac
    printf '%s\n' "$AVAILABLE_DISK_KB_OVERRIDE"
    return
  fi
  df -Pk "$CHECK_PATH" | awk 'NR == 2 { print $4 }'
}

available_kb="$(available_disk_kb)"
case "$available_kb" in
  ''|*[!0-9]*)
    fail "could not determine available disk space for $CHECK_PATH"
    ;;
esac

required_kb=$((MIN_FREE_DISK_GB * 1024 * 1024))
available_gb=$((available_kb / 1024 / 1024))

echo "CI capacity: ${available_gb} GiB free at $CHECK_PATH; required: ${MIN_FREE_DISK_GB} GiB"

if (( available_kb < required_kb )); then
  fail "runner has insufficient free disk for the heavy SDK/engine build. Use a larger or self-hosted runner label, or free disk before this step."
fi

#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  echo "usage: $0 <args.gn>... | <args.gn> key=value ..." >&2
  exit 64
fi

verify_no_ddm() {
  local args_file="$1"

  if [[ ! -f "$args_file" ]]; then
    echo "missing args.gn: $args_file" >&2
    exit 66
  fi

  local actual
  if ! actual="$(read_gn_value "$args_file" dart_dynamic_modules)"; then
    return 0
  fi
  if [[ "$actual" == "true" ]]; then
    echo "DART_DYNAMIC_MODULES must not be enabled: $args_file" >&2
    exit 70
  fi
}

read_gn_value() {
  local file="$1"
  local key="$2"
  awk -v key="$key" '
    $1 == key && $2 == "=" {
      value = $0
      sub("^[[:space:]]*" key "[[:space:]]*=[[:space:]]*", "", value)
      sub("[[:space:]]*$", "", value)
      found = 1
    }
    END {
      if (!found) {
        exit 1
      }
      print value
    }
  ' "$file"
}

has_expectations=0
for arg in "$@"; do
  if [[ "$arg" == *=* ]]; then
    has_expectations=1
    break
  fi
done

if [[ "$has_expectations" == "0" ]]; then
  for args_file in "$@"; do
    verify_no_ddm "$args_file"
    echo "Verified $args_file: dart_dynamic_modules is not true"
  done
  exit 0
fi

args_file="$1"
shift
verify_no_ddm "$args_file"

for expectation in "$@"; do
  if [[ "$expectation" != *=* ]]; then
    echo "invalid expectation: $expectation; expected key=value" >&2
    exit 64
  fi

  key="${expectation%%=*}"
  value="${expectation#*=}"
  if ! actual="$(read_gn_value "$args_file" "$key")"; then
    echo "expected $key = $value in $args_file, but $key is missing" >&2
    exit 70
  fi
  if [[ "$actual" != "$value" ]]; then
    echo "expected $key = $value in $args_file, found $actual" >&2
    exit 70
  fi
done

echo "Verified $args_file: dart_dynamic_modules is not true and expected flags are present"

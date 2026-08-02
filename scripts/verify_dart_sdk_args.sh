#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  echo "usage: $0 <args.gn>..." >&2
  exit 64
fi

require_gn_value() {
  local args_file="$1"
  local key="$2"
  local value="$3"
  local actual

  if ! actual="$(read_gn_value "$args_file" "$key")"; then
    echo "expected $key = $value in $args_file, but $key is missing" >&2
    exit 70
  fi
  if [[ "$actual" != "$value" ]]; then
    echo "expected $key = $value in $args_file, found $actual" >&2
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

for args_file in "$@"; do
  if [[ ! -f "$args_file" ]]; then
    echo "missing args.gn: $args_file" >&2
    exit 66
  fi

  if [[ "$(read_gn_value "$args_file" dart_dynamic_modules)" == "true" ]]; then
    echo "DART_DYNAMIC_MODULES must not be enabled: $args_file" >&2
    exit 70
  fi

  require_gn_value "$args_file" dart_dynamic_modules false
  require_gn_value "$args_file" dart_enable_aot_patching true
  require_gn_value "$args_file" dart_enable_shorebird_interpreter true

  echo "Verified $args_file: patched Dart SDK flags are enabled without DDM"
done

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_SRC="${DART_SRC:-$ROOT/dart-sdk}"
TOOL_SDK="$DART_SRC/tools/sdks/dart-sdk"
ENGINE_DART="$ROOT/flutter/engine/src/flutter/third_party/dart"

required_constraint="$(
  ruby -ryaml -e 'puts YAML.load_file(ARGV[0]).fetch("environment").fetch("sdk")' \
    "$DART_SRC/pkg/front_end/pubspec.yaml"
)"
required_major_minor="$(
  ruby -e 'ARGV[0] =~ /([0-9]+)\.([0-9]+)/ or abort "unable to parse SDK constraint"; puts "#{$1}.#{$2}"' \
    "$required_constraint"
)"

if [[ ! -x "$TOOL_SDK/bin/dart" ]]; then
  echo "missing executable Dart tool SDK: $TOOL_SDK/bin/dart" >&2
  exit 66
fi

version_output="$("$TOOL_SDK/bin/dart" --version 2>&1)"
actual_major_minor="$(
  ruby -e 'ARGV[0] =~ /Dart SDK version: ([0-9]+)\.([0-9]+)/ or abort "unable to parse Dart version"; puts "#{$1}.#{$2}"' \
    "$version_output"
)"

if [[ "$actual_major_minor" != "$required_major_minor" ]]; then
  echo "Dart tool SDK version does not satisfy front_end SDK constraint." >&2
  echo "  required major.minor: $required_major_minor from $required_constraint" >&2
  echo "  actual: $version_output" >&2
  exit 70
fi

if [[ -e "$ENGINE_DART" ]]; then
  engine_real="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$ENGINE_DART")"
  dart_real="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DART_SRC")"
  if [[ "$engine_real" != "$dart_real" ]]; then
    echo "Flutter engine Dart checkout points at $engine_real, expected $dart_real" >&2
    exit 70
  fi
fi

echo "[open-source-sync] Dart tool SDK is compatible: $version_output"

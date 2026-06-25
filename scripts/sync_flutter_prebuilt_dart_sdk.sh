#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_CONFIG="${1:-linux-x64}"
DART_SDK_SOURCE="${DART_SDK_SOURCE:-$ROOT/dart-sdk/tools/sdks/dart-sdk}"
TARGET="$ROOT/flutter/engine/src/flutter/prebuilts/$HOST_CONFIG/dart-sdk"

relative_path() {
  python3 - "$1" "$2" <<'PY'
import os
import sys
print(os.path.relpath(sys.argv[2], os.path.dirname(sys.argv[1])))
PY
}

real_path() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
}

case "$HOST_CONFIG" in
  linux-x64|macos-x64|macos-arm64)
    ;;
  *)
    echo "unsupported Flutter prebuilt Dart SDK host config: $HOST_CONFIG" >&2
    exit 64
    ;;
esac

if [[ ! -d "$DART_SDK_SOURCE" ]]; then
  echo "missing Dart tool SDK source: $DART_SDK_SOURCE" >&2
  exit 66
fi

for required in \
  bin/dart \
  bin/dartaotruntime \
  bin/snapshots/dartdevc_aot.dart.snapshot \
  bin/snapshots/kernel_worker_aot.dart.snapshot
do
  if [[ ! -e "$DART_SDK_SOURCE/$required" ]]; then
    echo "Dart tool SDK is missing $required: $DART_SDK_SOURCE" >&2
    exit 66
  fi
done

mkdir -p "$(dirname "$TARGET")"

if [[ -L "$TARGET" ]]; then
  source_real="$(real_path "$DART_SDK_SOURCE")"
  target_real="$(real_path "$TARGET")"
  if [[ "$source_real" != "$target_real" ]]; then
    echo "Flutter prebuilt Dart SDK link points at $target_real, expected $source_real" >&2
    exit 70
  fi
elif [[ -e "$TARGET" ]]; then
  echo "Flutter prebuilt Dart SDK target exists but is not the workspace Dart tool SDK symlink: $TARGET" >&2
  exit 70
else
  ln -s "$(relative_path "$TARGET" "$DART_SDK_SOURCE")" "$TARGET"
fi

echo "[open-source-sync] Flutter $HOST_CONFIG prebuilt Dart SDK uses $DART_SDK_SOURCE"

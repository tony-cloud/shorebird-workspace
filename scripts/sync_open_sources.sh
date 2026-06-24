#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_SRC="${DART_SRC:-$ROOT/dart-sdk-new}"
DART_TARGET="$ROOT/flutter/engine/src/flutter/third_party/dart"
UPDATER_SRC="${UPDATER_SRC:-$ROOT/updater}"
UPDATER_URL="${UPDATER_URL:-https://github.com/shorebirdtech/updater.git}"
TARGET="$ROOT/flutter/engine/src/flutter/third_party/updater"

is_git_checkout() {
  git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

echo "[open-source-sync] dart source: $DART_SRC"
echo "[open-source-sync] dart target: $DART_TARGET"
echo "[open-source-sync] updater source: $UPDATER_SRC"
echo "[open-source-sync] target: $TARGET"

mkdir -p "$(dirname "$DART_TARGET")"
mkdir -p "$(dirname "$TARGET")"

if [[ -L "$DART_TARGET" ]]; then
  echo "[open-source-sync] Dart target is already a symlink."
elif is_git_checkout "$DART_TARGET"; then
  echo "[open-source-sync] Dart target is already a git checkout."
elif [[ -e "$DART_TARGET" ]]; then
  echo "Dart target exists but is not a symlink or git checkout: $DART_TARGET" >&2
  exit 1
elif is_git_checkout "$DART_SRC"; then
  rel_target="$(python3 - "$DART_TARGET" "$DART_SRC" <<'PY'
import os
import sys
print(os.path.relpath(sys.argv[2], os.path.dirname(sys.argv[1])))
PY
)"
  ln -s "$rel_target" "$DART_TARGET"
  echo "[open-source-sync] linked Dart SDK checkout into Flutter engine."
else
  echo "Dart source checkout is missing: $DART_SRC" >&2
  exit 1
fi

if [[ ! -f "$DART_TARGET/runtime/vm/dart_api_impl.h" ]]; then
  echo "Dart checkout is missing runtime/vm/dart_api_impl.h" >&2
  exit 1
fi

if [[ -L "$TARGET" ]]; then
  echo "[open-source-sync] updater target is already a symlink."
  exit 0
fi

if is_git_checkout "$TARGET"; then
  echo "[open-source-sync] updating existing updater checkout."
  git -C "$TARGET" fetch --tags origin
  git -C "$TARGET" checkout "${UPDATER_REVISION:-main}"
  if [[ "${UPDATER_REVISION:-main}" == "main" ]]; then
    git -C "$TARGET" pull --ff-only
  fi
elif [[ -e "$TARGET" ]]; then
  echo "target exists but is not a symlink or git checkout: $TARGET" >&2
  exit 1
elif is_git_checkout "$UPDATER_SRC"; then
  rel_target="$(python3 - "$TARGET" "$UPDATER_SRC" <<'PY'
import os
import sys
print(os.path.relpath(sys.argv[2], os.path.dirname(sys.argv[1])))
PY
)"
  ln -s "$rel_target" "$TARGET"
  echo "[open-source-sync] linked updater submodule into Flutter engine."
else
  echo "[open-source-sync] cloning public updater checkout."
  git clone "$UPDATER_URL" "$TARGET"
  git -C "$TARGET" checkout "${UPDATER_REVISION:-main}"
fi

if [[ ! -f "$TARGET/library/include/updater_engine.h" ]]; then
  echo "updater checkout is missing library/include/updater_engine.h" >&2
  exit 1
fi

echo "[open-source-sync] public Shorebird updater is available."

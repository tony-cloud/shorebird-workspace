#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATER_SRC="${UPDATER_SRC:-$ROOT/updater}"
UPDATER_URL="${UPDATER_URL:-https://github.com/shorebirdtech/updater.git}"
TARGET="$ROOT/flutter/engine/src/flutter/third_party/updater"

echo "[open-source-sync] updater source: $UPDATER_SRC"
echo "[open-source-sync] target: $TARGET"

mkdir -p "$(dirname "$TARGET")"

if [[ -L "$TARGET" ]]; then
  echo "[open-source-sync] updater target is already a symlink."
  exit 0
fi

if [[ -d "$TARGET/.git" ]]; then
  echo "[open-source-sync] updating existing updater checkout."
  git -C "$TARGET" fetch --tags origin
  git -C "$TARGET" checkout "${UPDATER_REVISION:-main}"
  if [[ "${UPDATER_REVISION:-main}" == "main" ]]; then
    git -C "$TARGET" pull --ff-only
  fi
elif [[ -e "$TARGET" ]]; then
  echo "target exists but is not a symlink or git checkout: $TARGET" >&2
  exit 1
elif [[ -d "$UPDATER_SRC/.git" ]]; then
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

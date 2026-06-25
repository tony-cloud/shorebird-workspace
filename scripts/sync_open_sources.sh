#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_SRC="${DART_SRC:-$ROOT/dart-sdk-new}"
DART_TARGET="$ROOT/flutter/engine/src/flutter/third_party/dart"
UPDATER_SRC="${UPDATER_SRC:-$ROOT/updater}"
UPDATER_URL="${UPDATER_URL:-}"
TARGET="$ROOT/flutter/engine/src/flutter/third_party/updater"

is_git_checkout() {
  git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

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

link_checkout() {
  local target="$1"
  local source="$2"
  local label="$3"
  local rel_target
  rel_target="$(relative_path "$target" "$source")"
  ln -s "$rel_target" "$target"
  echo "[open-source-sync] linked $label checkout into Flutter engine."
}

is_clean_git_checkout() {
  [[ -z "$(git -C "$1" status --porcelain)" ]]
}

reject_forbidden_remotes() {
  local source="$1"
  local label="$2"
  shift 2

  local remotes
  remotes="$(git -C "$source" remote -v 2>/dev/null || true)"
  if [[ -z "$remotes" ]]; then
    return
  fi

  local forbidden
  for forbidden in "$@"; do
    if grep -Fq "$forbidden" <<<"$remotes"; then
      echo "$label source checkout uses forbidden remote fragment '$forbidden': $source" >&2
      echo "$remotes" >&2
      exit 1
    fi
  done
}

ensure_source_link() {
  local target="$1"
  local source="$2"
  local label="$3"

  if [[ ! -d "$source" ]] || ! is_git_checkout "$source"; then
    echo "$label source checkout is missing: $source" >&2
    exit 1
  fi

  if [[ -L "$target" ]]; then
    local target_real
    local source_real
    target_real="$(real_path "$target")"
    source_real="$(real_path "$source")"
    if [[ "$target_real" != "$source_real" ]]; then
      echo "$label target symlink points at $target_real, expected $source_real" >&2
      exit 1
    fi
    echo "[open-source-sync] $label target already links to the workspace checkout."
  elif is_git_checkout "$target"; then
    if ! is_clean_git_checkout "$target"; then
      echo "$label target is a dirty git checkout and cannot be replaced: $target" >&2
      exit 1
    fi
    rm -rf "$target"
    link_checkout "$target" "$source" "$label"
  elif [[ -e "$target" ]]; then
    echo "$label target exists but is not a symlink or git checkout: $target" >&2
    exit 1
  else
    link_checkout "$target" "$source" "$label"
  fi
}

echo "[open-source-sync] dart source: $DART_SRC"
echo "[open-source-sync] dart target: $DART_TARGET"
echo "[open-source-sync] updater source: $UPDATER_SRC"
echo "[open-source-sync] target: $TARGET"

mkdir -p "$(dirname "$DART_TARGET")"
mkdir -p "$(dirname "$TARGET")"

ensure_source_link "$DART_TARGET" "$DART_SRC" "Dart SDK"
reject_forbidden_remotes \
  "$DART_SRC" \
  "Dart SDK" \
  "github.com/dart-lang/sdk" \
  "dart.googlesource.com/sdk"

if [[ ! -f "$DART_TARGET/runtime/vm/dart_api_impl.h" ]]; then
  echo "Dart checkout is missing runtime/vm/dart_api_impl.h" >&2
  exit 1
fi

if is_git_checkout "$UPDATER_SRC"; then
  ensure_source_link "$TARGET" "$UPDATER_SRC" "updater submodule"
  reject_forbidden_remotes \
    "$UPDATER_SRC" \
    "updater submodule" \
    "github.com/shorebirdtech/updater" \
    "github.com/shorebirdtech/shorebird-updater"
elif [[ -n "$UPDATER_URL" ]]; then
  if [[ "$UPDATER_URL" == *github.com/shorebirdtech/updater* ||
        "$UPDATER_URL" == *github.com/shorebirdtech/shorebird-updater* ]]; then
    echo "UPDATER_URL points at a forbidden official Shorebird updater remote: $UPDATER_URL" >&2
    exit 1
  fi
  if [[ -L "$TARGET" ]]; then
    rm "$TARGET"
  fi
  if is_git_checkout "$TARGET"; then
    echo "[open-source-sync] updating existing updater checkout."
    git -C "$TARGET" remote set-url origin "$UPDATER_URL"
    git -C "$TARGET" fetch --tags origin
    git -C "$TARGET" checkout "${UPDATER_REVISION:-main}"
    if [[ "${UPDATER_REVISION:-main}" == "main" ]]; then
      git -C "$TARGET" pull --ff-only
    fi
  elif [[ -e "$TARGET" ]]; then
    echo "target exists but is not a symlink or git checkout: $TARGET" >&2
    exit 1
  else
    echo "[open-source-sync] cloning updater checkout from explicit UPDATER_URL."
    git clone "$UPDATER_URL" "$TARGET"
    git -C "$TARGET" checkout "${UPDATER_REVISION:-main}"
  fi
else
  echo "updater source checkout is missing: $UPDATER_SRC" >&2
  echo "Set UPDATER_SRC to a local fork or set UPDATER_URL explicitly." >&2
  exit 1
fi

if [[ ! -f "$TARGET/library/include/updater_engine.h" ]]; then
  echo "updater checkout is missing library/include/updater_engine.h" >&2
  exit 1
fi

echo "[open-source-sync] public Shorebird updater is available."

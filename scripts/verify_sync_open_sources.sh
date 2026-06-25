#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-source-sync.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

init_git_checkout() {
  local path="$1"
  mkdir -p "$path"
  git -C "$path" init -q
}

real_path() {
  python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

make_workspace() {
  local workspace="$1"

  mkdir -p "$workspace/scripts"
  cp "$ROOT/scripts/sync_open_sources.sh" "$workspace/scripts/sync_open_sources.sh"
  chmod +x "$workspace/scripts/sync_open_sources.sh"

  init_git_checkout "$workspace/dart-sdk-new"
  mkdir -p "$workspace/dart-sdk-new/runtime/vm"
  : > "$workspace/dart-sdk-new/runtime/vm/dart_api_impl.h"

  init_git_checkout "$workspace/updater"
  mkdir -p "$workspace/updater/library/include"
  : > "$workspace/updater/library/include/updater_engine.h"

  mkdir -p "$workspace/flutter/engine/src/flutter/third_party"
}

run_sync() {
  local workspace="$1"
  DART_SRC="$workspace/dart-sdk-new" \
    UPDATER_SRC="$workspace/updater" \
    "$workspace/scripts/sync_open_sources.sh"
}

assert_links_to_workspace_sources() {
  local workspace="$1"
  local dart_target="$workspace/flutter/engine/src/flutter/third_party/dart"
  local updater_target="$workspace/flutter/engine/src/flutter/third_party/updater"

  test -L "$dart_target"
  test -L "$updater_target"
  [[ "$(real_path "$dart_target")" == "$(real_path "$workspace/dart-sdk-new")" ]]
  [[ "$(real_path "$updater_target")" == "$(real_path "$workspace/updater")" ]]
  test -f "$dart_target/runtime/vm/dart_api_impl.h"
  test -f "$updater_target/library/include/updater_engine.h"
}

clean_checkout_workspace="$TMP_DIR/clean-checkouts"
make_workspace "$clean_checkout_workspace"
init_git_checkout "$clean_checkout_workspace/flutter/engine/src/flutter/third_party/dart"
init_git_checkout "$clean_checkout_workspace/flutter/engine/src/flutter/third_party/updater"
run_sync "$clean_checkout_workspace"
assert_links_to_workspace_sources "$clean_checkout_workspace"
run_sync "$clean_checkout_workspace"
assert_links_to_workspace_sources "$clean_checkout_workspace"

stale_link_workspace="$TMP_DIR/stale-link"
make_workspace "$stale_link_workspace"
mkdir -p "$stale_link_workspace/other-dart"
ln -s "../../../../../other-dart" \
  "$stale_link_workspace/flutter/engine/src/flutter/third_party/dart"
if run_sync "$stale_link_workspace" >"$TMP_DIR/stale-link.log" 2>&1; then
  echo "expected stale Dart symlink to fail" >&2
  exit 1
fi
grep -q "Dart SDK target symlink points at" "$TMP_DIR/stale-link.log"

dirty_checkout_workspace="$TMP_DIR/dirty-checkout"
make_workspace "$dirty_checkout_workspace"
dirty_dart="$dirty_checkout_workspace/flutter/engine/src/flutter/third_party/dart"
init_git_checkout "$dirty_dart"
: > "$dirty_dart/untracked.txt"
if run_sync "$dirty_checkout_workspace" >"$TMP_DIR/dirty-checkout.log" 2>&1; then
  echo "expected dirty Dart checkout to fail" >&2
  exit 1
fi
grep -q "Dart SDK target is a dirty git checkout" "$TMP_DIR/dirty-checkout.log"
test -d "$dirty_dart/.git"
test -f "$dirty_dart/untracked.txt"

forbidden_dart_remote_workspace="$TMP_DIR/forbidden-dart-remote"
make_workspace "$forbidden_dart_remote_workspace"
git -C "$forbidden_dart_remote_workspace/dart-sdk-new" remote add origin \
  https://github.com/dart-lang/sdk.git
if run_sync "$forbidden_dart_remote_workspace" >"$TMP_DIR/forbidden-dart-remote.log" 2>&1; then
  echo "expected forbidden Dart SDK remote to fail" >&2
  exit 1
fi
grep -q "Dart SDK source checkout uses forbidden remote fragment" \
  "$TMP_DIR/forbidden-dart-remote.log"

forbidden_updater_remote_workspace="$TMP_DIR/forbidden-updater-remote"
make_workspace "$forbidden_updater_remote_workspace"
git -C "$forbidden_updater_remote_workspace/updater" remote add origin \
  https://github.com/shorebirdtech/updater.git
if run_sync "$forbidden_updater_remote_workspace" >"$TMP_DIR/forbidden-updater-remote.log" 2>&1; then
  echo "expected forbidden updater source remote to fail" >&2
  exit 1
fi
grep -q "updater submodule source checkout uses forbidden remote fragment" \
  "$TMP_DIR/forbidden-updater-remote.log"

forbidden_updater_url_workspace="$TMP_DIR/forbidden-updater-url"
make_workspace "$forbidden_updater_url_workspace"
rm -rf "$forbidden_updater_url_workspace/updater"
if DART_SRC="$forbidden_updater_url_workspace/dart-sdk-new" \
  UPDATER_SRC="$forbidden_updater_url_workspace/missing-updater" \
  UPDATER_URL=https://github.com/shorebirdtech/updater.git \
  "$forbidden_updater_url_workspace/scripts/sync_open_sources.sh" \
    >"$TMP_DIR/forbidden-updater-url.log" 2>&1; then
  echo "expected forbidden explicit UPDATER_URL to fail" >&2
  exit 1
fi
grep -q "UPDATER_URL points at a forbidden official Shorebird updater remote" \
  "$TMP_DIR/forbidden-updater-url.log"

echo "sync_open_sources.sh smoke test passed"

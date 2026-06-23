#!/usr/bin/env bash
set -euo pipefail

PLATFORM="${1:?platform is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_BIN="${DART_BIN:-dart}"
FLUTTER_BIN="${FLUTTER_BIN:-flutter}"
GO_BIN="${GO_BIN:-go}"

run() {
  echo
  echo "==> $*"
  "$@"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 127
  fi
}

require_command git
require_command "$DART_BIN"
require_command "$GO_BIN"

if [[ "$PLATFORM" == "macos" ]]; then
  if command -v xcodebuild >/dev/null 2>&1; then
    xcodebuild -version
  else
    echo "warning: xcodebuild not found; iOS follow-up checks will be unavailable." >&2
  fi
fi

run git -C "$ROOT" submodule update --init --recursive
run "$ROOT/scripts/write_gclient.sh" "$PLATFORM"
run "$ROOT/scripts/sync_open_sources.sh"

export PATH="$ROOT/depot_tools:$PATH"
if [[ "${SKIP_GCLIENT_SYNC:-0}" != "1" ]]; then
  require_command gclient
  run gclient sync --no-history
else
  echo
  echo "==> skipping gclient sync because SKIP_GCLIENT_SYNC=1"
fi

if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
  echo
  echo "==> skipping tests because SKIP_TESTS=1"
  exit 0
fi

run bash -lc "cd '$ROOT/shorebird/packages/shorebird_cli' && '$DART_BIN' pub get && '$DART_BIN' test test/src/user_config_test.dart test/src/shorebird_env_test.dart test/src/shorebird_cli_command_runner_test.dart test/src/commands/init_command_test.dart"
run bash -lc "cd '$ROOT/shorebird/packages/open_aot_patch_tools' && '$DART_BIN' pub get && '$DART_BIN' test"
run bash -lc "cd '$ROOT/shorebird-server' && '$GO_BIN' test ./..."

AOT_ARGS="$ROOT/dart-sdk-new/out/ReleaseX64AotPatch/args.gn"
if [[ -f "$AOT_ARGS" ]]; then
  run bash -lc "cd '$ROOT/testapps/license_flavor_patch_test' && '$FLUTTER_BIN' pub get && '$DART_BIN' run tool/verify_aot_patch.dart"
else
  echo
  echo "==> skipping license/flavor AOT verification; missing $AOT_ARGS"
fi

if [[ "$PLATFORM" == "macos" && "${RUN_IOS_SMOKE:-0}" == "1" ]]; then
  require_command "$FLUTTER_BIN"
  run bash -lc "cd '$ROOT/testapps/license_flavor_patch_test' && '$FLUTTER_BIN' build ios --debug --no-codesign"
fi

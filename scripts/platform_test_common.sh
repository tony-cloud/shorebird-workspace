#!/usr/bin/env bash
set -euo pipefail

PLATFORM="${1:?platform is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_BIN="${DART_BIN:-dart}"
FLUTTER_BIN="${FLUTTER_BIN:-flutter}"
GO_BIN="${GO_BIN:-go}"
CARGO_BIN="${CARGO_BIN:-cargo}"

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
require_command "$CARGO_BIN"

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
export DEPOT_TOOLS_UPDATE="${DEPOT_TOOLS_UPDATE:-0}"
if [[ "${SKIP_GCLIENT_SYNC:-0}" != "1" ]]; then
  require_command gclient
  run gclient sync --no-history
  if [[ "${INCLUDE_ENGINE_DEPS:-0}" == "1" && -f "$ROOT/flutter/.gclient" ]]; then
    run bash -lc "cd '$ROOT/flutter' && gclient sync --no-history"
  fi
else
  echo
  echo "==> skipping gclient sync because SKIP_GCLIENT_SYNC=1"
fi

if [[ "${SKIP_TESTS:-0}" == "1" ]]; then
  echo
  echo "==> skipping tests because SKIP_TESTS=1"
  exit 0
fi

run bash -lc "cd '$ROOT/shorebird' && '$DART_BIN' pub get"
run bash -lc "cd '$ROOT/shorebird/packages/shorebird_cli' && '$DART_BIN' pub get && '$DART_BIN' test test/src/user_config_test.dart test/src/shorebird_env_test.dart test/src/shorebird_cli_command_runner_test.dart test/src/commands/doctor_command_test.dart test/src/commands/init_command_test.dart test/src/cache_test.dart test/src/shorebird_process_test.dart test/src/network_checker_test.dart test/src/shorebird_web_console_test.dart test/src/auth/auth_test.dart test/src/commands/login_command_test.dart test/src/commands/login_ci_command_test.dart test/src/commands/release/aar_releaser_test.dart test/src/shorebird_validator_test.dart test/src/shorebird_flutter_test.dart test/src/shorebird_artifacts_test.dart test/src/artifact_builder/artifact_builder_test.dart test/src/config/shorebird_yaml_test.dart test/src/commands/patch/ios_patcher_test.dart"
run bash -lc "cd '$ROOT/shorebird' && '$DART_BIN' test packages/shorebird_code_push_client/test/src/code_push_client_test.dart"
run bash -lc "cd '$ROOT/shorebird' && '$DART_BIN' test packages/artifact_proxy/test/artifact_proxy_test.dart packages/artifact_proxy/test/server_bin_test.dart packages/artifact_proxy/test/src/artifact_manifest_client_test.dart"
run bash -lc "cd '$ROOT/shorebird/packages/open_aot_patch_tools' && '$DART_BIN' pub get && '$DART_BIN' test"
run "$CARGO_BIN" test --manifest-path "$ROOT/updater/library/Cargo.toml"
run bash -lc "cd '$ROOT/shorebird-server' && '$GO_BIN' test ./..."

AOT_PATCH_BUILD_DIR="${AOT_PATCH_BUILD_DIR:-}"
if [[ -z "$AOT_PATCH_BUILD_DIR" ]]; then
  for candidate in \
    "$ROOT/dart-sdk/xcodebuild/ReleaseARM64" \
    "$ROOT/dart-sdk/out/ReleaseARM64AotPatch" \
    "$ROOT/dart-sdk/out/ReleaseX64AotPatch"; do
    if [[ -f "$candidate/args.gn" ]]; then
      AOT_PATCH_BUILD_DIR="$candidate"
      break
    fi
  done
fi

if [[ -n "$AOT_PATCH_BUILD_DIR" && -f "$AOT_PATCH_BUILD_DIR/args.gn" ]]; then
  run bash -lc "cd '$ROOT/testapps/license_flavor_patch_test' && '$FLUTTER_BIN' pub get && AOT_PATCH_BUILD_DIR='$AOT_PATCH_BUILD_DIR' '$DART_BIN' run tool/verify_aot_patch.dart"
else
  echo
  echo "==> skipping license/flavor AOT verification; missing AOT patch args.gn"
fi

if [[ "$PLATFORM" == "macos" ]]; then
  if [[ -f "$ROOT/flutter/engine/src/out/ios_release/args.gn" &&
        -f "$ROOT/flutter/engine/src/out/host_release_arm64/args.gn" ]]; then
    run "$ROOT/scripts/verify_ios_interpreter_route.sh"
  else
    echo
    echo "==> skipping iOS interpreter route verification; missing ios_release or host_release_arm64 args.gn"
  fi
fi

if [[ "$PLATFORM" == "macos" && "${RUN_IOS_SMOKE:-0}" == "1" ]]; then
  require_command "$FLUTTER_BIN"
  run bash -lc "cd '$ROOT/testapps/license_flavor_patch_test' && '$FLUTTER_BIN' build ios --debug --no-codesign"
fi

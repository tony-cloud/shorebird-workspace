#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_BIN="${DART_BIN:-dart}"
RESULTS="$ROOT/artifacts/cli-native-archive-tests"
mkdir -p "$RESULTS"

tests=(
  test/src/artifact_manager_test.dart
  test/src/archive_analysis/portable_zip_namespace_validator_test.dart
  test/src/archive_analysis/release_supplement_validator_test.dart
  test/src/archive_analysis/android_archive_differ_test.dart
  test/src/archive_analysis/linux_bundle_differ_test.dart
  test/src/archive_analysis/windows_archive_differ_test.dart
  test/src/archive_analysis/apple_archive_security_test.dart
  test/src/archive_analysis/full_replacement_archive_differ_test.dart
  test/src/platform/apple/macho_test.dart
  test/src/patch_diff_checker_test.dart
  test/src/android_native_patch_diff_test.dart
  test/src/commands/patch/patch_command_test.dart
  test/src/commands/patch/windows_patcher_test.dart
  test/src/commands/patch/apple_patcher_mixin_test.dart
)
if [[ "$(uname -s)" == Darwin ]]; then
  tests+=(
    test/src/archive_analysis/apple_archive_differ_test.dart
    test/src/commands/patch/ios_patcher_test.dart
    test/src/commands/patch/macos_patcher_test.dart
  )
fi

# The shared security suite selects real codesign/ditto fixtures on macOS.
# Windows skips only the Linux writer's Unix permission/symlink fixture.
# pipefail keeps a failing test or pub resolution from being hidden by tee.
cd "$ROOT/shorebird/packages/shorebird_cli"
{
  uname -s
  "$DART_BIN" --version
  printf 'Selected suite: %s\n' "${tests[@]}"
  "$DART_BIN" pub get
  "$DART_BIN" test --reporter expanded "${tests[@]}"
} 2>&1 | tee "$RESULTS/tests.log"

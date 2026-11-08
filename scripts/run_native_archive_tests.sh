#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DART_BIN="${DART_BIN:-dart}"
RESULTS="$ROOT/artifacts/cli-native-archive-tests"
mkdir -p "$RESULTS"

tests=(
  test/src/artifact_builder/ios_replacement_host_test.dart
  test/src/shorebird_artifacts_test.dart
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
  test/src/commands/full_replacement_pipeline_test.dart
  test/src/commands/patch/patch_command_test.dart
  test/src/commands/patch/windows_patcher_test.dart
  test/src/commands/patch/apple_patcher_mixin_test.dart
  test/src/commands/release/macos_releaser_test.dart
  test/src/validators/macos_network_entitlement_validator_test.dart
  # P2 origin policy and toolchain supply (HARDENING_CONTRACTS.md section 8):
  # scoped origins and conflict diagnosis, no implicit --dev Shorebird host,
  # on-demand bundletool and the bounded retry policy, on every CLI host.
  test/src/shorebird_env_test.dart
  test/src/shorebird_process_test.dart
  test/src/shorebird_cli_command_runner_test.dart
  test/src/cache_test.dart
  test/src/executables/bundletool_test.dart
)

# These shared suites mock Apple builds, signing and ditto, so their full-profile
# and entitlement policy checks run on Linux/Windows as well as macOS.
# Keep the larger builder/release-command suites targeted. Each expected case
# runs separately: removing or renaming it must fail with no matching tests.
macos_interpreter_cases=(
  'test/src/commands/release/release_command_test.dart|macOS requires full profile, explicit ARM64 and managed trust'
  'test/src/commands/release/release_command_test.dart|macOS encrypted release rejects weakened signing installOnly=false'
  'test/src/commands/release/release_command_test.dart|macOS encrypted release rejects weakened signing installOnly=true'
  'test/src/artifact_builder/artifact_builder_test.dart|macos/arm64 full profile generates portable registrant inputs: false'
  'test/src/artifact_builder/artifact_builder_test.dart|macos/arm64 full profile generates portable registrant inputs: true'
  'test/src/artifact_builder/artifact_builder_test.dart|macos/x64 full profile generates portable registrant inputs: false'
  'test/src/artifact_builder/artifact_builder_test.dart|macos/x64 full profile generates portable registrant inputs: true'
  # P2: a hardened release refuses the legacy all-in-one artifact mirror.
  'test/src/commands/release/release_command_test.dart|refuses the legacy all-in-one artifact mirror'
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
  printf 'Selected macOS interpreter case: %s\n' "${macos_interpreter_cases[@]}"
  "$DART_BIN" pub get
  "$DART_BIN" test --reporter expanded "${tests[@]}"
  for test_case in "${macos_interpreter_cases[@]}"; do
    test_file="${test_case%%|*}"
    test_name="${test_case#*|}"
    "$DART_BIN" test --reporter expanded --plain-name "$test_name" "$test_file"
  done
} 2>&1 | tee "$RESULTS/tests.log"

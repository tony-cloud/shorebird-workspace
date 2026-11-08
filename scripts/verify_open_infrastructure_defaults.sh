#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  echo "error: $*" >&2
  exit 70
}

require_contains() {
  local path="$1"
  local needle="$2"
  grep -Fq "$needle" "$path" || fail "$path is missing required text: $needle"
}

reject_contains() {
  local path="$1"
  local needle="$2"
  if grep -Fq "$needle" "$path"; then
    fail "$path contains forbidden text: $needle"
  fi
}

check_forbidden_in_file() {
  local path="$1"
  local pattern

  [[ -f "$path" ]] || fail "missing open-infrastructure check input: $path"
  for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    reject_contains "$path" "$pattern"
  done
}

check_forbidden_in_tree() {
  local tree="$1"
  local extension="$2"
  local path

  [[ -d "$tree" ]] || fail "missing open-infrastructure check tree: $tree"
  while IFS= read -r -d '' path; do
    check_forbidden_in_file "$path"
  done < <(find "$tree" -type f -name "*.$extension" -print0)
}

FORBIDDEN_PATTERNS=(
  "https://download.shorebird.dev"
  "download.shorebird.dev"
  "api.shorebird.dev"
  "auth.shorebird.dev"
  "console.shorebird.dev"
  "api-dev.shorebird.dev"
  "auth-dev.shorebird.dev"
  "console-dev.shorebird.dev"
  "cdn.shorebird.cloud"
  "git@github.com:shorebirdtech/dart-sdk.git"
  "github.com/shorebirdtech/updater.git"
  "github.com/shorebirdtech/flutter.git"
  "shorebird-dart-sdk-prebuilt"
  "shorebirdtech/_build_engine"
)

BUILD_SENSITIVE_FILES=(
  "$ROOT/.gitmodules"
  "$ROOT/flutter/DEPS"
  "$ROOT/flutter/bin/internal/update_dart_sdk.ps1"
  "$ROOT/flutter/bin/internal/update_dart_sdk.sh"
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.ps1"
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh"
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.pins"
  "$ROOT/flutter/bin/internal/shorebird_artifact_trust.json"
  "$ROOT/flutter/dev/bots/post_process_docs.dart"
  "$ROOT/flutter/dev/bots/unpublish_package.dart"
  "$ROOT/flutter/dev/integration_tests/pure_android_host_apps/android_host_app_v2_embedding/settings.gradle"
  "$ROOT/flutter/dev/integration_tests/pure_android_host_apps/host_app_kotlin_gradle_dsl/settings.gradle.kts"
  "$ROOT/flutter/dev/tools/create_api_docs.dart"
  "$ROOT/flutter/engine/src/flutter/build/zip_bundle.gni"
  "$ROOT/flutter/engine/src/flutter/lib/web_ui/dev/steps/copy_artifacts_step.dart"
  "$ROOT/flutter/packages/flutter_tools/gradle/aar_init_script.gradle"
  "$ROOT/flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt"
  "$ROOT/flutter/packages/flutter_tools/lib/src/cache.dart"
  "$ROOT/flutter/packages/flutter_tools/lib/src/http_host_validator.dart"
  "$ROOT/flutter/packages/flutter_tools/lib/src/shorebird/shorebird_artifact_catalog.dart"
  "$ROOT/flutter/packages/flutter_tools/lib/src/shorebird/shorebird_artifact_origins.dart"
  "$ROOT/flutter/packages/flutter_tools/pubspec.yaml"
  "$ROOT/flutter/packages/shorebird_tests/test/shorebird_tests.dart"
  "$ROOT/scripts/write_gclient.sh"
  "$ROOT/shorebird/bin/shorebird.ps1"
  "$ROOT/shorebird/third_party/flutter/bin/internal/shared.sh"
  "$ROOT/updater/library/src/config.rs"
)

for path in "${BUILD_SENSITIVE_FILES[@]}"; do
  check_forbidden_in_file "$path"
done

check_forbidden_in_tree "$ROOT/shorebird/packages/artifact_proxy/lib" dart
check_forbidden_in_tree "$ROOT/shorebird/packages/shorebird_cli/lib" dart
check_forbidden_in_tree "$ROOT/shorebird/packages/shorebird_code_push_client/lib" dart

require_contains "$ROOT/.gitmodules" "https://github.com/tony-cloud/dart-sdk.git"
require_contains "$ROOT/.gitmodules" "https://github.com/tony-cloud/flutter.git"
require_contains "$ROOT/.gitmodules" "https://github.com/tony-cloud/shorebird.git"
require_contains "$ROOT/.gitmodules" "https://github.com/tony-cloud/shorebird-server.git"
require_contains "$ROOT/.gitmodules" "https://github.com/tony-cloud/shorebird-updater.git"

require_contains "$ROOT/flutter/DEPS" '"dart_sdk_git": "https://github.com/tony-cloud/dart-sdk.git"'
require_contains "$ROOT/flutter/DEPS" '"updater_git": "https://github.com/tony-cloud/shorebird-updater.git"'

require_contains "$ROOT/flutter/packages/flutter_tools/lib/src/cache.dart" \
  "kOpenFlutterStorageUrl = 'http://localhost:8080/download.flutter.io'"
# P2: the custom Dart SDK comes from an immutable, digest-pinned address
# (bin/internal/shorebird_dart_sdk.pins) and is verified before it is unpacked.
# A mutable "releases/latest" URL is never a default and is refused as an
# override.
for path in \
  "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  "$ROOT/flutter/bin/internal/update_dart_sdk.ps1" \
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh" \
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.ps1" \
  "$ROOT/flutter/bin/internal/shorebird_dart_sdk.pins"; do
  reject_contains "$path" "releases/latest/download"
done
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  'source "$FLUTTER_ROOT/bin/internal/shorebird_dart_sdk.sh"'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  'shorebird::resolve_dart_sdk "$DART_ZIP_NAME"'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  'shorebird::fetch_dart_sdk "$DART_SDK_ZIP"'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  'shorebird::record_dart_sdk_identity'
require_contains "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh" \
  'SHOREBIRD_DART_SDK_LOCK="$FLUTTER_ROOT/bin/internal/shorebird_dart_sdk.pins"'
require_contains "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh" \
  'shorebird::reject_mutable SHOREBIRD_DART_SDK_RELEASE_BASE_URL'
require_contains "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh" \
  'shorebird::verify_archive'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.ps1" \
  '. "$flutterRoot\bin\internal\shorebird_dart_sdk.ps1"'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.ps1" \
  'Resolve-ShorebirdDartSdk $dartZipNames'
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.ps1" \
  'Get-ShorebirdDartSdkArchive $dartSdk $dartSdkZip'
require_contains "$ROOT/flutter/bin/internal/shorebird_dart_sdk.ps1" \
  'Assert-ShorebirdImmutable '"'"'SHOREBIRD_DART_SDK_RELEASE_BASE_URL'"'"
require_contains "$ROOT/flutter/bin/internal/shorebird_dart_sdk.ps1" \
  'bin\internal\shorebird_dart_sdk.pins'
python3 "$ROOT/scripts/dart_sdk_lock.py" check "$ROOT/flutter" >/dev/null ||
  fail "flutter/bin/internal/shorebird_dart_sdk.pins does not match the checkout's engine and Dart SDK"
# The shipped catalog trust store must not grant a development key production
# trust (artifact_catalog.py validates the full trust contract).
python3 "$ROOT/scripts/artifact_catalog.py" check-trust "$ROOT/flutter/bin/internal/shorebird_artifact_trust.json" >/dev/null ||
  fail "flutter/bin/internal/shorebird_artifact_trust.json is not a valid catalog trust store"
require_contains "$ROOT/flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt" \
  'DEFAULT_MAVEN_HOST = "http://localhost:8080/download.flutter.io"'
require_contains "$ROOT/flutter/dev/tools/create_api_docs.dart" \
  "LocalPlatform().environment['FLUTTER_STORAGE_BASE_URL']"
require_contains "$ROOT/flutter/packages/shorebird_tests/test/shorebird_tests.dart" \
  "'FLUTTER_STORAGE_BASE_URL': 'http://localhost:8080/download.flutter.io'"

require_contains "$ROOT/shorebird/packages/shorebird_cli/lib/src/shorebird_env.dart" \
  "defaultHostedUrl = 'http://localhost:8080'"
require_contains "$ROOT/shorebird/packages/shorebird_cli/lib/src/cache.dart" \
  "defaultArtifactBaseUrl = 'http://localhost:8080/artifacts'"
require_contains "$ROOT/updater/library/src/config.rs" \
  'const DEFAULT_BASE_URL: &str = "http://localhost:8080";'

echo "open infrastructure defaults check passed"

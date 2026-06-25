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

require_contains "$ROOT/.gitmodules" "https://git.tonycloud.org/dart-lang/sdk.git"
require_contains "$ROOT/.gitmodules" "https://git.tonycloud.org/flutter/flutter.git"
require_contains "$ROOT/.gitmodules" "https://git.tonycloud.org/flutter/shorebird.git"
require_contains "$ROOT/.gitmodules" "https://git.tonycloud.org/flutter/shorebird-server.git"
require_contains "$ROOT/.gitmodules" "https://git.tonycloud.org/flutter/shorebird-updater.git"

require_contains "$ROOT/flutter/DEPS" '"dart_sdk_git": "https://git.tonycloud.org/dart-lang/sdk.git"'
require_contains "$ROOT/flutter/DEPS" '"updater_git": "https://git.tonycloud.org/flutter/shorebird-updater.git"'

require_contains "$ROOT/flutter/packages/flutter_tools/lib/src/cache.dart" \
  "kOpenFlutterStorageUrl = 'http://localhost:8080/download.flutter.io'"
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.sh" \
  "http://localhost:8080/download.flutter.io"
require_contains "$ROOT/flutter/bin/internal/update_dart_sdk.ps1" \
  "http://localhost:8080/download.flutter.io"
require_contains "$ROOT/flutter/packages/flutter_tools/gradle/src/main/kotlin/FlutterPluginConstants.kt" \
  'DEFAULT_MAVEN_HOST = "http://localhost:8080/download.flutter.io"'
require_contains "$ROOT/flutter/dev/tools/create_api_docs.dart" \
  "Platform.environment['FLUTTER_STORAGE_BASE_URL']"
require_contains "$ROOT/flutter/packages/shorebird_tests/test/shorebird_tests.dart" \
  "'FLUTTER_STORAGE_BASE_URL': 'http://localhost:8080/download.flutter.io'"

require_contains "$ROOT/shorebird/packages/shorebird_cli/lib/src/shorebird_env.dart" \
  "defaultHostedUrl = 'http://localhost:8080'"
require_contains "$ROOT/shorebird/packages/shorebird_cli/lib/src/cache.dart" \
  "defaultArtifactBaseUrl = 'http://localhost:8080/artifacts'"
require_contains "$ROOT/updater/library/src/config.rs" \
  'const DEFAULT_BASE_URL: &str = "http://localhost:8080";'

echo "open infrastructure defaults check passed"

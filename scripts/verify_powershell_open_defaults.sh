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

shorebird_launcher="$ROOT/shorebird/bin/shorebird.ps1"
flutter_dart_updater="$ROOT/flutter/bin/internal/update_dart_sdk.ps1"

[[ -f "$shorebird_launcher" ]] || fail "missing PowerShell launcher: $shorebird_launcher"
[[ -f "$flutter_dart_updater" ]] || fail "missing Flutter Dart SDK updater: $flutter_dart_updater"

require_contains "$shorebird_launcher" \
  '$defaultFlutterGitUrl = "https://github.com/tony-cloud/flutter.git"'
require_contains "$shorebird_launcher" \
  '$defaultFlutterStorageBaseUrl = "http://localhost:8080/download.flutter.io"'
require_contains "$shorebird_launcher" 'SHOREBIRD_FLUTTER_GIT_URL'
require_contains "$shorebird_launcher" 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL'
require_contains "$shorebird_launcher" 'FLUTTER_STORAGE_BASE_URL'

require_contains "$flutter_dart_updater" '$Env:FLUTTER_STORAGE_BASE_URL'
require_contains "$flutter_dart_updater" \
  '$dartSdkBaseUrl = "http://localhost:8080/download.flutter.io"'

for path in "$shorebird_launcher" "$flutter_dart_updater"; do
  reject_contains "$path" 'download.shorebird.dev'
  reject_contains "$path" 'api.shorebird.dev'
  reject_contains "$path" 'auth.shorebird.dev'
  reject_contains "$path" 'console.shorebird.dev'
  reject_contains "$path" 'docs.shorebird.dev'
  reject_contains "$path" 'github.com/shorebirdtech/flutter.git'
  reject_contains "$path" 'github.com/shorebirdtech/shorebird'
  reject_contains "$path" 'git@github.com:shorebirdtech'
done

if command -v pwsh >/dev/null 2>&1; then
  SHOREBIRD_POWERSHELL_LAUNCHER="$shorebird_launcher" \
  FLUTTER_DART_SDK_POWERSHELL_UPDATER="$flutter_dart_updater" \
  pwsh -NoProfile -NonInteractive -Command '
    $paths = @(
      $env:SHOREBIRD_POWERSHELL_LAUNCHER,
      $env:FLUTTER_DART_SDK_POWERSHELL_UPDATER
    )
    $failed = $false
    foreach ($path in $paths) {
      $tokens = $null
      $errors = $null
      [System.Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref]$tokens,
        [ref]$errors
      ) | Out-Null
      if ($errors.Count -gt 0) {
        Write-Error "$path has PowerShell parse errors:"
        foreach ($errorRecord in $errors) {
          Write-Error "  $($errorRecord.Message)"
        }
        $failed = $true
      }
    }
    if ($failed) {
      exit 70
    }
  '
else
  echo "warning: pwsh not found; skipped PowerShell parse check" >&2
fi

echo "PowerShell open-default checks passed"

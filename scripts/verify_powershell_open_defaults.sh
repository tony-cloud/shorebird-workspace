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
flutter_dart_resolver="$ROOT/flutter/bin/internal/shorebird_dart_sdk.ps1"

[[ -f "$shorebird_launcher" ]] || fail "missing PowerShell launcher: $shorebird_launcher"
[[ -f "$flutter_dart_updater" ]] || fail "missing Flutter Dart SDK updater: $flutter_dart_updater"
[[ -f "$flutter_dart_resolver" ]] || fail "missing Flutter Dart SDK resolver: $flutter_dart_resolver"

require_contains "$shorebird_launcher" \
  '$defaultFlutterGitUrl = "https://github.com/tony-cloud/flutter.git"'
require_contains "$shorebird_launcher" 'SHOREBIRD_FLUTTER_GIT_URL'
# P2: the launcher no longer rewrites artifact origins; the vended Flutter's
# own bootstrap applies the shared precedence model (split by default, legacy
# all-in-one mirror only through its aliases), so the launcher must not set
# FLUTTER_STORAGE_BASE_URL for it.
require_contains "$shorebird_launcher" '& $flutter --version'
reject_contains "$shorebird_launcher" '$env:FLUTTER_STORAGE_BASE_URL ='
reject_contains "$shorebird_launcher" '$defaultFlutterStorageBaseUrl'

require_contains "$flutter_dart_updater" \
  '. "$flutterRoot\bin\internal\shorebird_dart_sdk.ps1"'
require_contains "$flutter_dart_updater" 'Test-ShorebirdDartSdkNeedsUpdate'
require_contains "$flutter_dart_updater" 'Resolve-ShorebirdDartSdk $dartZipNames'
require_contains "$flutter_dart_updater" 'Get-ShorebirdDartSdkArchive $dartSdk $dartSdkZip'
require_contains "$flutter_dart_resolver" '$Env:FLUTTER_STORAGE_BASE_URL'
require_contains "$flutter_dart_resolver" '$Env:SHOREBIRD_DART_SDK_RELEASE_BASE_URL'
require_contains "$flutter_dart_resolver" '$Env:SHOREBIRD_ARTIFACT_BASE_URL'
require_contains "$flutter_dart_resolver" 'bin\internal\shorebird_dart_sdk.pins'
require_contains "$flutter_dart_resolver" 'Get-FileHash -Algorithm SHA256'

for path in "$shorebird_launcher" "$flutter_dart_updater" "$flutter_dart_resolver"; do
  reject_contains "$path" 'releases/latest/download'

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
  FLUTTER_DART_SDK_POWERSHELL_RESOLVER="$flutter_dart_resolver" \
  pwsh -NoProfile -NonInteractive -Command '
    $paths = @(
      $env:SHOREBIRD_POWERSHELL_LAUNCHER,
      $env:FLUTTER_DART_SDK_POWERSHELL_UPDATER,
      $env:FLUTTER_DART_SDK_POWERSHELL_RESOLVER
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

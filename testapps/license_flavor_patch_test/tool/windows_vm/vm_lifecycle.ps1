# Run the native ARM64 job's lifecycle steps against the built engine:
# "Verify native ARM64 engine and SDK machine types", "Restore verified Flutter
# version tag for lifecycle", "Prepare Windows encrypted lifecycle tools",
# "Run Windows DPAPI provider contract", "Run Windows encrypted native lifecycle"
# (-Mode encrypted, default) or "Run Windows native V2 variant lifecycle" (-Mode variant).
# Commands and fixture arguments are copied from .github/workflows/open-shorebird-ci.yml;
# the V2 receipt validator is read from that workflow step itself so it cannot drift.
param([string]$Root = 'C:\sb', [string]$LockToken = '', [Parameter(Mandatory = $true)][string]$ResultsDir,
      [ValidateSet('encrypted', 'variant')][string]$Mode = 'encrypted',
      [switch]$FreshPubCache, [switch]$SkipProvider, [int]$BuildTimeout = 0)
. (Join-Path $PSScriptRoot 'vm_env.ps1') -Root $Root -LockToken $LockToken
Set-Location $Workspace
New-Item -ItemType Directory -Force $ResultsDir | Out-Null
$engine = Join-Path $Workspace 'flutter/engine/src'
$out = Join-Path $engine 'out/windows_release_arm64'

Invoke-Checked 'verify native ARM64 engine and SDK machine types' {
  & $NativePython scripts/verify_native_desktop_arch.py --platform windows --arch arm64 --require-native-host `
    --binary flutter/engine/src/out/windows_release_arm64/dart-sdk/bin/dart.exe `
    --binary flutter/engine/src/out/windows_release_arm64/flutter_windows.dll `
    --build-tool $env:NATIVE_BUILD_LINKER --build-tool $env:NATIVE_PYTHON `
    --build-tool flutter/engine/src/flutter/third_party/gn/gn.exe `
    --build-tool flutter/engine/src/flutter/buildtools/windows-x64/clang/bin/clang-cl.exe
}
Invoke-Checked 'restore verified Flutter version tag' { & $NativePython scripts/prepare_flutter_version_tag.py flutter }

Write-Host '[vm] prepare Windows encrypted lifecycle tools'
Expand-Archive -LiteralPath "$out/zip_archives/flutter_patched_sdk_product.zip" -DestinationPath $out -Force
Copy-Item "$out/gen_snapshot/gen_snapshot.exe" "$out/gen_snapshot.exe" -Force
$cache = Join-Path $Workspace 'flutter/bin/cache'
New-Item -ItemType Directory -Force $cache | Out-Null
if (Test-Path "$cache/dart-sdk") { Remove-Item -Recurse -Force "$cache/dart-sdk" }
Copy-Item "$out/dart-sdk" "$cache/dart-sdk" -Recurse
# CI uses a fresh $RUNNER_TEMP pub cache; keep one between local runs unless asked.
$env:PUB_CACHE = Join-Path $RunnerTemp 'shorebird-lifecycle-pub'
if ($FreshPubCache -and (Test-Path $env:PUB_CACHE)) { Remove-Item -Recurse -Force $env:PUB_CACHE }
foreach ($project in @('shorebird', 'flutter/packages/flutter_tools')) {
  Push-Location (Join-Path $Workspace $project)
  try { Invoke-Checked "pub get $project" { & "$out/dart-sdk/bin/dart.exe" pub get } } finally { Pop-Location }
}
Push-Location updater
try { Invoke-Checked 'build public patch tool' { cargo build --release --locked -p patch --bin patch } } finally { Pop-Location }

if ($Mode -eq 'encrypted' -and !$SkipProvider) {
  $fixture = (Resolve-Path testapps/license_flavor_patch_test/tool/windows_native).Path
  $public = (Resolve-Path $out).Path
  $build = Join-Path $RunnerTemp ('shorebird-native-probe-' + [guid]::NewGuid())
  $providerResults = Join-Path $ResultsDir 'windows-native-provider-tests'
  New-Item -ItemType Directory $build, $providerResults -Force | Out-Null
  Push-Location $build
  try {
    Invoke-Checked 'compile DPAPI provider contract' {
      cl /nologo /std:c++17 /EHsc /W4 /WX /utf-8 /I"$public" "$fixture/native_cache_contract_test.cpp" /Fe:native_cache_contract_test.exe /link winhttp.lib crypt32.lib bcrypt.lib 2>&1 | Tee-Object "$providerResults/build.log"
    }
    Invoke-Checked 'provider probe machine type' {
      & $NativePython "$Workspace/scripts/verify_native_desktop_arch.py" --platform windows --arch arm64 --require-native-host --binary "$build/native_cache_contract_test.exe"
    }
    Invoke-Checked 'run DPAPI provider contract' { & ./native_cache_contract_test.exe 2>&1 | Tee-Object "$providerResults/test.log" }
  } finally {
    Pop-Location
    Remove-Item -Recurse -Force $build
  }
}

$extra = @()
if ($BuildTimeout) { $extra += @('--build-timeout', $BuildTimeout) }

if ($Mode -eq 'variant') {
  Write-Host '[vm] run Windows native V2 variant lifecycle'
  $work = Join-Path $RunnerTemp ('shorebird-variant-e2e-' + [guid]::NewGuid())
  $results = Join-Path $ResultsDir 'windows-native-variant-e2e'
  & $NativePython testapps/license_flavor_patch_test/tool/windows_variant_e2e.py run --execute --arch arm64 `
    --engine-src $engine --host-engine windows_release_arm64 --target-engine windows_release_arm64 `
    --tooling-dart "$out/dart-sdk/bin/dart.exe" --patch-tool "$Workspace/updater/target/release/patch.exe" `
    --pub-cache $env:PUB_CACHE --work $work --results $results @extra 2>&1 | Tee-Object "$results.log"
  $variantExit = $LASTEXITCODE
  if (Test-Path $work) { Write-Host "[vm] WARNING: variant work directory was not removed: $work" }
  if ($variantExit) { throw "Windows native V2 variant lifecycle failed (exit $variantExit)" }
  # Receipt validator: the exact `$validate = @' ... '@` block of the arm64 workflow step.
  $workflow = Get-Content (Join-Path $Workspace '.github/workflows/open-shorebird-ci.yml')
  $step = [Array]::FindIndex($workflow, [Predicate[string]]{ param($l) $l -match 'id: windows_native_v2_variant_arm64\s*$' })
  if ($step -lt 0) { throw 'Workflow step windows_native_v2_variant_arm64 not found' }
  $begin = -1; $end = -1
  for ($i = $step; $i -lt $workflow.Count; $i++) {
    if ($begin -lt 0 -and $workflow[$i] -match "^(\s*)\`$validate = @'\s*$") { $begin = $i; $indent = $matches[1].Length }
    elseif ($begin -ge 0 -and $workflow[$i] -match "^\s*'@\s*$") { $end = $i; break }
  }
  if ($begin -lt 0 -or $end -lt 0) { throw 'V2 receipt validator block not found in the workflow step' }
  $validate = ($workflow[($begin + 1)..($end - 1)] | ForEach-Object { if ($_.Length -ge $indent) { $_.Substring($indent) } else { $_.TrimStart() } }) -join "`n"
  Invoke-Checked 'V2 variant execution receipt' { & $NativePython -c $validate "$results/results.json" windows arm64 }
  Write-Host '[vm] variant lifecycle passed'
  return
}

Write-Host '[vm] run Windows encrypted native lifecycle'
$work = Join-Path $RunnerTemp ('shorebird-native-e2e-' + [guid]::NewGuid())
$results = Join-Path $ResultsDir 'windows-native-cache-e2e'
& $NativePython testapps/license_flavor_patch_test/tool/windows_native_cache_e2e.py run --execute --arch arm64 `
  --engine-src $engine --host-engine windows_release_arm64 --target-engine windows_release_arm64 `
  --tooling-dart "$out/dart-sdk/bin/dart.exe" --patch-tool "$Workspace/updater/target/release/patch.exe" `
  --pub-cache $env:PUB_CACHE --work $work --results $results @extra
$lifecycleExit = $LASTEXITCODE
if (Test-Path $work) { Write-Host "[vm] WARNING: lifecycle work directory was not removed: $work" }
if ($lifecycleExit) { throw "Windows encrypted native lifecycle failed (exit $lifecycleExit)" }
Invoke-Checked 'lifecycle receipt inventory' {
  & $NativePython scripts/verify_native_desktop_arch.py --platform windows --arch arm64 --require-native-host --lifecycle-report "$results/results.json"
}
$report = Get-Content "$results/results.json" -Raw | ConvertFrom-Json
if ($report.target_arch -ne 'arm64' -or $report.runtime_arch -ne 'aarch64') { throw 'ARM64 native lifecycle architecture evidence is missing' }
if ($report.schema -ne 'windows-native-cache-e2e-v1' -or $report.result -ne 'passed' -or $report.cleanup -ne 'owned processes, ports, disposable app cache, private session removed') {
  throw 'Windows lifecycle did not produce a passing cleaned execution report'
}
Write-Host '[vm] lifecycle passed'

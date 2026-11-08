# Build the windows_release_arm64 engine exactly as the CI job steps
# "Write gclient config" .. "Verify Windows engine args" do (native ARM64 job).
# gclient sync runs only when .gclient or the DEPS files changed since the last
# successful sync (or with -Sync always); ninja is incremental.
param([string]$Root = 'C:\sb', [string]$LockToken = '', [ValidateSet('auto', 'always', 'never')][string]$Sync = 'auto',
      [int]$Jobs = 0)
. (Join-Path $PSScriptRoot 'vm_env.ps1') -Root $Root -LockToken $LockToken
Set-Location $Workspace

$depot = Join-Path $Workspace 'depot_tools'
if (!(Test-Path (Join-Path $depot 'python3_bin_reldir.txt')) -or !(Test-Path (Join-Path $depot 'git.bat'))) {
  Invoke-Checked 'bootstrap depot_tools' { & (Join-Path $depot 'bootstrap\win_tools.bat') }
  . (Join-Path $PSScriptRoot 'vm_env.ps1') -Root $Root -LockToken $LockToken
  Set-Location $Workspace
}

$env:INCLUDE_ENGINE_DEPS = '1'
Invoke-Bash 'write gclient config' './scripts/write_gclient.sh windows'
Remove-Item env:INCLUDE_ENGINE_DEPS
Invoke-Bash 'sync open-source links' './scripts/sync_open_sources.sh'

$stampFile = Join-Path $Root 'logs\gclient-sync.stamp'
$inputs = @('.gclient', 'flutter/.gclient', 'dart-sdk/DEPS', 'flutter/DEPS') | ForEach-Object {
  (Get-FileHash (Join-Path $Workspace $_) -Algorithm SHA256).Hash
}
$stamp = $inputs -join "`n"
$needSync = $Sync -eq 'always' -or ($Sync -eq 'auto' -and (!(Test-Path $stampFile) -or (Get-Content $stampFile -Raw).Trim() -ne $stamp))
if ($needSync) {
  if (Test-Path $stampFile) { Remove-Item $stampFile }
  Invoke-Checked 'gclient sync' { gclient sync --no-history }
  Push-Location flutter
  try { Invoke-Checked 'gclient sync Flutter engine deps' { gclient sync --no-history } } finally { Pop-Location }
  Set-Content $stampFile $stamp
} else {
  Write-Host "[vm] gclient sync skipped (Sync=$Sync, DEPS unchanged)"
}

Push-Location (Join-Path $Workspace 'flutter/engine/src')
try {
  Invoke-Checked 'configure Windows engine' {
    & $NativePython flutter/tools/gn --windows-cpu=arm64 --runtime-mode=release --target-dir=windows_release_arm64 `
      --enable-unittests --no-prebuilt-dart-sdk `
      "--gn-args=dart_dynamic_modules=false dart_enable_aot_patching=true dart_enable_shorebird_interpreter=false shorebird_enable_aot_patching=true shorebird_use_interpreter=false flutter_prebuilt_dart_sdk=false"
  }
  # Same single-linker rule as the CI step: Git's GNU link.exe must never win.
  $expectedLinkerDir = [IO.Path]::GetFullPath((Join-Path $env:VCToolsInstallDir 'bin\HostARM64\ARM64'))
  $found = @(Get-Command link.exe -All -CommandType Application -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
  $candidates = @($found | Where-Object { [string]::Equals([IO.Path]::GetDirectoryName($_), $expectedLinkerDir, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -Unique)
  if ($candidates.Count -ne 1) { throw "Expected exactly one MSVC HostARM64/ARM64 link.exe, found: $($found -join '; ')" }
  Write-Host "[vm] Using Microsoft linker: $($candidates[0])"
  $results = Join-Path $Workspace 'artifacts/windows-native-cache-tests'
  New-Item -ItemType Directory -Force $results | Out-Null
  # frontend_server/gen_kernel steps take 2-3 GB each; without a pagefile ninja's
  # default (cores+2) exhausted the VM commit limit. Default: one job per 3 GB of
  # commit limit (RAM + pagefile, see setup_toolchain.ps1), at most cores+2.
  if (!$Jobs) {
    $commitGb = [math]::Floor((Get-CimInstance Win32_OperatingSystem).TotalVirtualMemorySize / 1MB)
    $Jobs = [math]::Max(2, [math]::Min([Environment]::ProcessorCount + 2, [math]::Floor($commitGb / 3)))
  }
  Write-Host "[vm] ninja -j $Jobs"
  Invoke-Checked 'build Windows engine' {
    ninja -j $Jobs -C out/windows_release_arm64 flutter/build/archives:windows_flutter flutter/tools/font_subset:font_subset `
      flutter/shell/common/shorebird:shorebird_unittests flutter/shell/platform/embedder:embedder_unittests `
      flutter/shell/platform/windows:flutter_windows_unittests flutter/build/dart:dart_sdk `
      flutter/flutter_frontend_server:frontend_server flutter/tools/const_finder:const_finder `
      flutter/sky/packages/sky_engine:sky_engine flutter/shell/platform/windows:windows `
      flutter/build/archives:flutter_patched_sdk flutter/impeller/compiler:impellerc 2>&1 | Tee-Object -FilePath "$results/build.log"
  }
} finally { Pop-Location }

Invoke-Bash 'verify Windows engine args' ("./scripts/verify_engine_args.sh flutter/engine/src/out/windows_release_arm64/args.gn " +
  "'target_cpu=`"arm64`"' 'host_cpu=`"arm64`"' enable_unittests=true dart_dynamic_modules=false dart_enable_aot_patching=true " +
  "dart_enable_shorebird_interpreter=false shorebird_enable_aot_patching=true shorebird_use_interpreter=false flutter_prebuilt_dart_sdk=false")
Write-Host '[vm] engine build done'

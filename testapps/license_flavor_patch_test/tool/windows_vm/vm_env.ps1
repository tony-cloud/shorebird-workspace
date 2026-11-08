# Dot-sourced by the VM phase scripts. Recreates the job environment of
# "Build Windows engine artifacts (native ARM64)" (.github/workflows/open-shorebird-ci.yml):
# workflow-level env, VsDevCmd -arch=arm64 -host_arch=arm64, depot_tools + its python3,
# native ARM64 Python (NATIVE_PYTHON), Go, Rust with the VS ARM64 clang for ring.
param([string]$Root = 'C:\sb', [string]$LockToken = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'vm_lock.ps1')
Assert-RunbookLock -Root $Root -Token $LockToken
$ProgressPreference = 'SilentlyContinue'
$script:Root = $Root
$script:Workspace = Join-Path $Root 'ws'
$script:RunnerTemp = Join-Path $Root 'tmp'
New-Item -ItemType Directory -Force $RunnerTemp, (Join-Path $Root 'logs') | Out-Null

# Workflow-level env (open-shorebird-ci.yml `env:`), English tool output (VSLANG) so
# localized MSBuild/link banners never differ from the hosted runners. No PYTHONUTF8:
# build scripts must decode child output in the console code page, as on the runners.
$workflowEnv = @{
  COPYFILE_DISABLE = '1'; SHOREBIRD_UPSTREAM_REVISION = '5f04c3ace43b02d0fec7c19b99c88a85989c704f'
  DART_SDK_CHANNEL = 'stable'; GO_VERSION = '1.26.10'; JAVA_VERSION = '17'; DEPOT_TOOLS_UPDATE = '0'
  BASE_FLUTTER_ENGINE_REVISION = 'cf636b1dfb818dd6a3ff16e390e93b1a036969c8'
  SDK_MIN_FREE_DISK_GB = '8'; ENGINE_MIN_FREE_DISK_GB = '8'
  DEPOT_TOOLS_WIN_TOOLCHAIN = '0'; GYP_MSVS_VERSION = '2026'; MSYS = 'winsymlinks:nativestrict'
  PUB_CACHE = (Join-Path $RunnerTemp 'pub-cache'); RUNNER_TEMP = $RunnerTemp; GITHUB_WORKSPACE = $Workspace
  VSLANG = '1033'
  RUSTUP_HOME = (Join-Path $Root 'tools\rustup'); CARGO_HOME = (Join-Path $Root 'tools\cargo')
}
foreach ($name in $workflowEnv.Keys) { Set-Item "env:$name" $workflowEnv[$name] }

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$script:VsInstall = & $vswhere -latest -version '[18.0,19.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.ARM64 -property installationPath
if (!$VsInstall) { throw 'Visual Studio 2026 ARM64 C++ toolchain is required (run setup_toolchain.ps1)' }
$env:GYP_MSVS_OVERRIDE_PATH = $VsInstall
cmd /c "`"$VsInstall\Common7\Tools\VsDevCmd.bat`" -arch=arm64 -host_arch=arm64 >nul && set" | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { Set-Item -LiteralPath "env:$($matches[1])" $matches[2] }
}
if ($LASTEXITCODE) { throw 'VsDevCmd failed' }
$env:CC_aarch64_pc_windows_msvc = Join-Path $VsInstall 'VC\Tools\Llvm\ARM64\bin\clang.exe'
$env:CXX_aarch64_pc_windows_msvc = Join-Path $VsInstall 'VC\Tools\Llvm\ARM64\bin\clang++.exe'
$script:Linker = Join-Path $env:VCToolsInstallDir 'bin\HostARM64\ARM64\link.exe'
if (!(Test-Path $Linker)) { throw "MSVC HostARM64/ARM64 link.exe missing: $Linker" }
$env:NATIVE_BUILD_LINKER = $Linker

$script:NativePython = 'C:\Program Files\Python313-arm64\python.exe'
if (!(Test-Path $NativePython)) { throw 'Native ARM64 Python 3.13 missing (run setup_toolchain.ps1)' }
$env:NATIVE_PYTHON = $NativePython -replace '\\', '/'

$depot = Join-Path $Workspace 'depot_tools'
$prefix = @((Join-Path $env:CARGO_HOME 'bin'), (Join-Path $Root 'tools\go\bin'), (Split-Path $NativePython),
            (Join-Path (Split-Path $NativePython) 'Scripts'), 'C:\Program Files\PowerShell\7')
if (Test-Path (Join-Path $depot 'python3_bin_reldir.txt')) {
  $prefix = @($depot, (Join-Path $depot (Get-Content (Join-Path $depot 'python3_bin_reldir.txt') -Raw).Trim())) + $prefix
} elseif (Test-Path $depot) {
  $prefix = @($depot) + $prefix
}
$env:PATH = (($prefix | Where-Object { Test-Path $_ }) + @($env:PATH)) -join ';'
$script:GitBash = 'C:\Program Files\Git\bin\bash.exe'

function Invoke-Checked([string]$label, [scriptblock]$block) {
  Write-Host "[vm] $label"
  & $block
  if ($LASTEXITCODE) { throw "$label failed (exit $LASTEXITCODE)" }
}
function Invoke-Bash([string]$label, [string]$command) {
  Invoke-Checked $label { & $GitBash --noprofile --norc -eo pipefail -c "cd '$($Workspace -replace '\\', '/')' && $command" }
}

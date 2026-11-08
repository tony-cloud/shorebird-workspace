# PowerShell half of scripts/test_dart_sdk_bootstrap.sh: runs
# flutter/bin/internal/update_dart_sdk.ps1 (and shorebird_dart_sdk.ps1) against
# the loopback server that script starts. Exercises the pinned install, offline
# reuse, digest mismatch, the unpublished lock, conflicting overrides, mutable
# URLs, legacy-mirror compatibility and the Windows ARM64 archive preference.
param(
    [Parameter(Mandatory)] [string]$FlutterSource,
    [Parameter(Mandatory)] [string]$Work,
    [Parameter(Mandatory)] [string]$Www,
    [Parameter(Mandatory)] [string]$BaseUrl,
    [Parameter(Mandatory)] [string]$Engine,
    [Parameter(Mandatory)] [string]$Dart,
    [Parameter(Mandatory)] [string]$GoodZip,
    [Parameter(Mandatory)] [string]$EvilZip,
    [Parameter(Mandatory)] [string]$RequestLog
)
$ErrorActionPreference = 'Stop'
$x64 = 'dart-sdk-windows-x64.zip'
$arm64 = 'dart-sdk-windows-arm64.zip'
$good = (Get-FileHash -Algorithm SHA256 $GoodZip).Hash.ToLowerInvariant()
$goodSize = (Get-Item $GoodZip).Length
$evil = (Get-FileHash -Algorithm SHA256 $EvilZip).Hash.ToLowerInvariant()
$release = Join-Path $Www 'ps-release'
$mirror = Join-Path $Www "ps-mirror/flutter_infra_release/flutter/$Engine"
New-Item -ItemType Directory -Force $release, $mirror | Out-Null
Copy-Item $GoodZip (Join-Path $release $x64)
Copy-Item $EvilZip (Join-Path $release "evil-$x64")
Copy-Item $GoodZip (Join-Path $mirror $x64)

function Fail([string]$message) { Write-Error "FAIL (PowerShell): $message"; exit 1 }

function New-Root([string]$name) {
    $root = Join-Path $Work $name
    if (Test-Path $root) { Remove-Item -Recurse -Force $root }
    New-Item -ItemType Directory -Force (Join-Path $root 'bin/internal'), (Join-Path $root 'bin/cache') | Out-Null
    Copy-Item (Join-Path $FlutterSource 'bin/internal/update_dart_sdk.ps1') (Join-Path $root 'bin/internal/')
    Copy-Item (Join-Path $FlutterSource 'bin/internal/shorebird_dart_sdk.ps1') (Join-Path $root 'bin/internal/')
    Set-Content (Join-Path $root 'DEPS') "vars = {`n  `"dart_sdk_revision`": `"$Dart`",`n}"
    Set-Content (Join-Path $root 'bin/cache/engine.stamp') $Engine
    Set-Content (Join-Path $root 'bin/cache/engine.realm') ''
    return $root
}

function Write-Lock([string]$root, [string]$baseUrl, [string]$assets) {
    $lines = @('schema open-shorebird.dart-sdk-lock', 'contract_version 1', "engine_revision $Engine", "dart_sdk_revision $Dart")
    if ($baseUrl) { $lines += "base_url $baseUrl"; $lines += $assets }
    Set-Content (Join-Path $root 'bin/internal/shorebird_dart_sdk.pins') $lines
}

$script:shimDir = $null
function Get-SevenZipShimDir {
    if ($script:shimDir) { return $script:shimDir }
    $script:shimDir = Join-Path $Work '7z-shim'
    New-Item -ItemType Directory -Force $script:shimDir | Out-Null
    foreach ($tool in '7z', '7za') {
        $real = Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $real) { continue }
        $shim = Join-Path $script:shimDir $tool
        Set-Content $shim "#!/usr/bin/env bash`nargs=()`nfor a in `"`$@`"; do args+=(`"`${a//\\//}`"); done`nexec '$($real.Source)' `"`${args[@]}`"`n"
        & chmod +x $shim
    }
    return $script:shimDir
}

$script:out = ''
function Invoke-Bootstrap([string]$root, [int]$expected, [hashtable]$environment = @{}) {
    $names = @('SHOREBIRD_ORIGIN_MODE', 'SHOREBIRD_DOWNLOAD_URL', 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL', 'FLUTTER_STORAGE_BASE_URL',
        'SHOREBIRD_ARTIFACT_BASE_URL', 'SHOREBIRD_DART_SDK_RELEASE_BASE_URL', 'SHOREBIRD_DART_SDK_SHA256', 'PROCESSOR_ARCHITECTURE')
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $environment[$name]) }
    # update_dart_sdk.ps1 is Windows-only and hands native 7z/7za Windows-style
    # '\' paths, which name the wrong directory on a non-Windows host (e.g. the
    # Linux CI runner, which ships 7z). There, put a shim first on PATH that
    # translates '\' to '/' and runs the real tool, so the 7z branch is still
    # exercised; Windows hosts use their 7z unchanged.
    $savedPath = $env:PATH
    if (-not $IsWindows) { $env:PATH = (Get-SevenZipShimDir) + [IO.Path]::PathSeparator + $env:PATH }
    try {
        $script:out = (& pwsh -NoProfile -NonInteractive -File (Join-Path $root 'bin/internal/update_dart_sdk.ps1') *>&1 | Out-String)
        $status = $LASTEXITCODE
    } finally {
        $env:PATH = $savedPath
    }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $null) }
    if ($status -ne $expected) { Write-Host $script:out; Fail "expected exit $expected, got $status" }
}
function Assert-Output([string]$text) { if (-not $script:out.Contains($text)) { Write-Host $script:out; Fail "output lacks: $text" } }
function Get-Mark { return @(Get-Content $RequestLog).Count }
function Get-RequestsSince([int]$mark) { return @(Get-Content $RequestLog | Select-Object -Skip $mark) }
function Get-Identity([string]$root) { return (Get-Content (Join-Path $root 'bin/cache/engine-dart-sdk.identity') -TotalCount 1).Trim() }

# Pinned clean install, then offline reuse.
$root = New-Root 'published'
Write-Lock $root "$BaseUrl/ps-release" "asset $x64 $good $goodSize"
$mark = Get-Mark
Invoke-Bootstrap $root 0
if ((Get-Identity $root) -ne "$Engine $Dart $x64 $good") { Fail 'identity not recorded' }
if (-not ((Get-RequestsSince $mark) -contains "/ps-release/$x64")) { Fail 'lock URL not fetched' }
if (-not (Test-Path (Join-Path $root 'bin/cache/dart-sdk/bin/dart'))) { Fail 'SDK not expanded' }
$mark = Get-Mark
Invoke-Bootstrap $root 0 @{ SHOREBIRD_DART_SDK_RELEASE_BASE_URL = 'http://127.0.0.1:9/unreachable' }
if ((Get-Mark) -ne $mark) { Fail 'network used for a current verified install' }
Write-Host 'ok - PowerShell: pinned clean install and offline reuse'

# Digest mismatch: refused before expansion.
$root = New-Root 'tampered'
Write-Lock $root "$BaseUrl/ps-release" "asset $x64 $evil $goodSize"
Invoke-Bootstrap $root 2
Assert-Output 'does not match its pinned digest'
if (Test-Path (Join-Path $root 'bin/cache/dart-sdk/bin/dart')) { Fail 'tampered SDK expanded' }
Write-Host 'ok - PowerShell: digest mismatch refused'

# Unpublished lock: precise error and no request, even with an official origin.
$root = New-Root 'unpublished'
Write-Lock $root '' ''
$mark = Get-Mark
Invoke-Bootstrap $root 2 @{ FLUTTER_STORAGE_BASE_URL = "$BaseUrl/official" }
Assert-Output 'shorebird_dart_sdk.pins is unpublished'
if ((Get-Mark) -ne $mark) { Fail 'unpublished lock fetched something' }
Write-Host 'ok - PowerShell: unpublished lock, no official substitution'

# Conflicts and mutable URLs.
$root = New-Root 'conflict'
Write-Lock $root "$BaseUrl/ps-release" "asset $x64 $good $goodSize"
Invoke-Bootstrap $root 2 @{ SHOREBIRD_DOWNLOAD_URL = "$BaseUrl/a"; SHOREBIRD_FLUTTER_STORAGE_BASE_URL = "$BaseUrl/b" }
Assert-Output 'Conflicting artifact mirror overrides'
Invoke-Bootstrap $root 2 @{ SHOREBIRD_ORIGIN_MODE = 'split'; SHOREBIRD_DOWNLOAD_URL = "$BaseUrl/ps-mirror" }
Assert-Output 'SHOREBIRD_ORIGIN_MODE=split conflicts'
Invoke-Bootstrap $root 2 @{ SHOREBIRD_DOWNLOAD_URL = "$BaseUrl/ps-mirror"; FLUTTER_STORAGE_BASE_URL = "$BaseUrl/official" }
Assert-Output 'Conflicting artifact origins'
Invoke-Bootstrap $root 2 @{ SHOREBIRD_DART_SDK_RELEASE_BASE_URL = 'https://github.com/o/r/releases/latest/download' }
Assert-Output 'mutable "latest" release'
Write-Host 'ok - PowerShell: conflicting overrides and mutable URLs refused'

# Legacy all-in-one mirror without a pin: compatible, labelled unverified.
$root = New-Root 'legacy'
Write-Lock $root '' ''
Invoke-Bootstrap $root 0 @{ SHOREBIRD_DOWNLOAD_URL = "$BaseUrl/ps-mirror" }
Assert-Output 'UNVERIFIED Dart SDK from the legacy all-in-one mirror'
if (-not (Get-Identity $root).EndsWith(' unverified')) { Fail 'unverified identity' }
Write-Host 'ok - PowerShell: legacy-mirror compatibility'

# Windows ARM64 prefers the ARM64 archive but falls back to a pinned x64 one.
$root = New-Root 'arm64'
Write-Lock $root "$BaseUrl/ps-release" "asset $x64 $good $goodSize"
Invoke-Bootstrap $root 0 @{ PROCESSOR_ARCHITECTURE = 'ARM64' }
Assert-Output "has no $arm64, falling back to $x64"
if ((Get-Identity $root) -ne "$Engine $Dart $x64 $good") { Fail 'ARM64 fallback identity' }
Write-Host 'ok - PowerShell: ARM64 host falls back to the pinned x64 archive'
exit 0

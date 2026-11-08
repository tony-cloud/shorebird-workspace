# One-time (idempotent) toolchain setup for the local Windows ARM64 lifecycle VM.
# Run elevated (prlctl exec as SYSTEM). Mirrors the windows-11-vs2026-arm runner
# tools the "Build Windows engine artifacts (native ARM64)" job relies on.
# Everything large lives on the VM disk under $Root (default C:\sb).
param(
  [string]$Root = 'C:\sb',
  [string]$PythonVersion = '3.13.9',
  [string]$GoVersion = '1.26.10',
  [string]$PwshVersion = '7.4.6'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tools = Join-Path $Root 'tools'
$downloads = Join-Path $Root 'downloads'
New-Item -ItemType Directory -Force $Root, $tools, $downloads, (Join-Path $Root 'tmp'), (Join-Path $Root 'logs') | Out-Null

function Step($name) { Write-Host "[setup] $name" }
function Fetch($url, $name) {
  $path = Join-Path $downloads $name
  if (!(Test-Path $path)) {
    Step "download $name"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile "$path.part"
    Move-Item "$path.part" $path
  }
  return $path
}
function Run($file, [string[]]$arguments, [int[]]$ok = @(0)) {
  $process = Start-Process -FilePath $file -ArgumentList $arguments -Wait -PassThru -NoNewWindow
  if ($ok -notcontains $process.ExitCode) { throw "$file exited $($process.ExitCode)" }
  return $process.ExitCode
}

Step 'machine policy: long paths (as on the hosted runners)'
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null

# The VM shipped without a pagefile (commit limit = 12 GB RAM); the engine build's
# 2-3 GB frontend_server/gen_kernel steps then abort with "zone.cc: 95: Out of memory".
# A fixed 16-32 GB pagefile raises the commit limit; it takes effect after a reboot.
Step 'pagefile C:\pagefile.sys 16384-32768 MB'
$memory = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
$pagefile = 'C:\pagefile.sys 16384 32768'
$current = @((Get-ItemProperty $memory).PagingFiles)
if (($current -join '|') -ne $pagefile) {
  $system = Get-CimInstance Win32_ComputerSystem
  if ($system.AutomaticManagedPagefile) { Set-CimInstance -InputObject $system -Property @{ AutomaticManagedPagefile = $false } }
  Set-ItemProperty $memory -Name PagingFiles -Value ([string[]]@($pagefile)) -Type MultiString
  Write-Host '[setup] REBOOT REQUIRED for the pagefile (prlctl restart "CWindows 11")'
}
$commitGb = [math]::Round((Get-CimInstance Win32_OperatingSystem).TotalVirtualMemorySize / 1MB, 1)
Write-Host "[setup] current commit limit: $commitGb GB"

Step 'Visual Studio 2026 C++ ARM64/x64 tools, LLVM clang, CMake, ATL'
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products '*' -version '[18.0,19.0)' -property installationPath
if (!$vs) { throw 'Visual Studio 2026 (18.x) is not installed' }
$components = @(
  'Microsoft.VisualStudio.Workload.NativeDesktop',
  'Microsoft.VisualStudio.Component.VC.Tools.ARM64',
  'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
  'Microsoft.VisualStudio.Component.VC.Llvm.Clang',
  'Microsoft.VisualStudio.Component.VC.CMake.Project',
  'Microsoft.VisualStudio.Component.VC.ATL',
  'Microsoft.VisualStudio.Component.VC.ATL.ARM64',
  'Microsoft.VisualStudio.Component.Windows11SDK.26100')
$missing = @($components | Where-Object { !(& $vswhere -latest -products '*' -version '[18.0,19.0)' -requires $_ -property installationPath) })
if ($missing.Count) {
  $installerArgs = @('modify', '--installPath', "`"$vs`"", '--quiet', '--norestart', '--nocache', '--includeRecommended')
  foreach ($component in $missing) { $installerArgs += @('--add', $component) }
  # 3010 = success, reboot required.
  Run "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe" $installerArgs @(0, 3010) | Out-Null
}
$linkers = @(Get-ChildItem "$vs\VC\Tools\MSVC\*\bin\HostARM64\ARM64\link.exe" -ErrorAction SilentlyContinue)
if (!$linkers.Count) { throw 'MSVC HostARM64/ARM64 link.exe still missing after Visual Studio modify' }
if (!(Test-Path "$vs\VC\Tools\Llvm\ARM64\bin\clang.exe")) { throw 'Visual Studio native ARM64 clang missing' }

Step 'Windows SDK debugging tools (dbghelp.dll for the engine build)'
$sdk = "${env:ProgramFiles(x86)}\Windows Kits\10"
if (!(Test-Path "$sdk\Debuggers\arm64\dbghelp.dll")) {
  $winsdk = Fetch 'https://go.microsoft.com/fwlink/?linkid=2286561' 'winsdksetup-26100.exe'
  Run $winsdk @('/features', 'OptionId.WindowsDesktopDebuggers', '/quiet', '/norestart', '/ceip', 'off') @(0, 3010) | Out-Null
  if (!(Test-Path "$sdk\Debuggers\arm64\dbghelp.dll")) { throw 'Debugging Tools for Windows did not install dbghelp.dll' }
}

Step "Python $PythonVersion ARM64"
$python = "C:\Program Files\Python313-arm64\python.exe"
if (!(Test-Path $python)) {
  $installer = Fetch "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-arm64.exe" "python-$PythonVersion-arm64.exe"
  Run $installer @('/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0', 'Include_launcher=1') | Out-Null
}

Step "Go $GoVersion ARM64"
$go = Join-Path $tools 'go\bin\go.exe'
if (!(Test-Path $go) -or ((& $go version) -notmatch "go$GoVersion ")) {
  $zip = Fetch "https://go.dev/dl/go$GoVersion.windows-arm64.zip" "go$GoVersion.windows-arm64.zip"
  if (Test-Path (Join-Path $tools 'go')) { Remove-Item -Recurse -Force (Join-Path $tools 'go') }
  Expand-Archive -LiteralPath $zip -DestinationPath $tools -Force
}

Step "PowerShell $PwshVersion ARM64"
if (!(Test-Path 'C:\Program Files\PowerShell\7\pwsh.exe')) {
  $msi = Fetch "https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion/PowerShell-$PwshVersion-win-arm64.msi" "PowerShell-$PwshVersion-win-arm64.msi"
  Run 'msiexec.exe' @('/i', "`"$msi`"", '/quiet', '/norestart', 'ADD_PATH=1') | Out-Null
}

Step 'Rust (shared RUSTUP_HOME/CARGO_HOME under the VM root)'
$env:RUSTUP_HOME = Join-Path $tools 'rustup'
$env:CARGO_HOME = Join-Path $tools 'cargo'
$cargo = Join-Path $env:CARGO_HOME 'bin\cargo.exe'
if (!(Test-Path $cargo)) {
  $rustup = Fetch 'https://static.rust-lang.org/rustup/dist/aarch64-pc-windows-msvc/rustup-init.exe' 'rustup-init-aarch64.exe'
  Run $rustup @('-y', '--no-modify-path', '--profile', 'minimal', '--default-toolchain', 'stable', '--default-host', 'aarch64-pc-windows-msvc') | Out-Null
}
& (Join-Path $env:CARGO_HOME 'bin\rustup.exe') target add aarch64-pc-windows-msvc | Out-Null

# The interactive user runs builds; give it full control of the VM root.
$user = (Get-CimInstance Win32_ComputerSystem).UserName
# Root-only grant: inheritance covers new files; /T over a synced engine checkout takes hours.
if ($user) { & icacls $Root /grant "${user}:(OI)(CI)F" /Q | Out-Null }

Step 'summary'
& $vswhere -latest -products '*' -version '[18.0,19.0)' -property catalog_productDisplayVersion
$linkers | ForEach-Object { $_.FullName }
& $python --version
& $go version
& (Join-Path $env:CARGO_HOME 'bin\cargo.exe') --version
Write-Host '[setup] done'

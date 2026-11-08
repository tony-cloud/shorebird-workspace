# Make C:\sb\ws match the Mac workspace described by <SyncDir>\manifest.json:
# per repo (root + submodules) the commit to check out and an optional git bundle.
# Uncommitted Mac changes arrive as a synthetic commit (HEAD + working tree, built
# with a temporary index on the Mac) inside the bundle, so checkout applies this
# VM's normal line-ending conversion exactly as for CI. Clones come from the public
# GitHub remotes (full history, as actions/checkout fetch-depth 0); gclient-synced
# and ignored build output (engine deps, out/) survives between syncs.
param([string]$Root = 'C:\sb', [Parameter(Mandatory = $true)][string]$SyncDir, [string]$LockToken = '')
. (Join-Path $PSScriptRoot 'vm_lock.ps1')
Assert-RunbookLock -Root $Root -Token $LockToken
# Native git writes progress to stderr; failures are detected by exit code (works in PowerShell 5.1 and 7).
$ErrorActionPreference = 'Continue'
$workspace = Join-Path $Root 'ws'
$manifest = Get-Content (Join-Path $SyncDir 'manifest.json') -Raw | ConvertFrom-Json
# Not named "Git": PowerShell names are case-insensitive, so it would call itself.
function Invoke-Git { & git.exe @args; if ($LASTEXITCODE) { throw "git $($args -join ' ') failed ($LASTEXITCODE)" } }
Invoke-Git config --global core.longpaths true
Invoke-Git config --global core.symlinks true

foreach ($repo in $manifest.repos) {
  $path = if ($repo.path -eq '.') { $workspace } else { Join-Path $workspace $repo.path }
  Write-Host "[sync] $($repo.path) -> $($repo.head)"
  if (!(Test-Path (Join-Path $path '.git'))) {
    if (Test-Path $path) {
      if (@(Get-ChildItem -Force $path).Count) { throw "$path exists but is not a git checkout" }
      Remove-Item $path
    }
    Invoke-Git clone --no-checkout $repo.url $path
  }
  $present = $false
  & git.exe -C $path cat-file -e "$($repo.head)^{commit}" 2>$null
  if (!$LASTEXITCODE) { $present = $true }
  if (!$present) { Invoke-Git -C $path fetch --tags origin }
  if ($repo.bundle) { Invoke-Git -C $path fetch --no-tags (Join-Path $SyncDir $repo.bundle) 'refs/vm-sync/head' }
  Invoke-Git -C $path checkout --force --detach $repo.head
  Invoke-Git -C $path reset --hard --quiet
  # -fd without -x: keeps ignored state (gclient deps, out/, .dart_tool) for incremental builds.
  $exclude = @()
  if ($repo.path -eq '.') { foreach ($sub in $manifest.repos) { if ($sub.path -ne '.') { $exclude += @('-e', $sub.path) } } }
  Invoke-Git -C $path clean -fdq @exclude
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Root 'logs\last-sync.json')
Write-Host '[sync] done'

# Single-owner lock for the VM runbook (functions only; dot-source it).
# run_windows_lifecycle_vm.sh takes <Root>\runbook.lock before sync/build/lifecycle/
# variant and passes its token to every VM phase script, which refuses to run
# without the matching token, so two sessions can never overlap on one checkout.
function Get-RunbookLockPath([string]$Root) { Join-Path $Root 'runbook.lock' }

function Enter-RunbookLock([string]$Root, [string]$Token, [string]$Owner) {
  New-Item -ItemType Directory -Force $Root | Out-Null
  $path = Get-RunbookLockPath $Root
  try {
    # CreateNew is atomic: exactly one caller can create the file.
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
  } catch [IO.IOException] {
    $held = if (Test-Path $path) { (Get-Content $path -Raw).Trim() } else { 'unknown' }
    # A retried acquire (prlctl can lose a return code) finds its own token.
    if ((Test-Path $path) -and (Get-Content $path -TotalCount 1).Trim() -eq $Token) { Write-Host "[lock] already held $path"; return }
    Write-Host "[lock] busy: $held"
    exit 75
  }
  $writer = New-Object IO.StreamWriter($stream)
  $writer.WriteLine($Token)
  $writer.WriteLine("owner=$Owner acquired=$((Get-Date).ToUniversalTime().ToString('o'))")
  $writer.Close()
  Write-Host "[lock] acquired $path"
}

function Exit-RunbookLock([string]$Root, [string]$Token) {
  $path = Get-RunbookLockPath $Root
  if ((Test-Path $path) -and ((Get-Content $path -TotalCount 1).Trim() -eq $Token)) {
    Remove-Item $path
    Write-Host "[lock] released $path"
  }
}

function Assert-RunbookLock([string]$Root, [string]$Token) {
  $path = Get-RunbookLockPath $Root
  if (!(Test-Path $path)) { throw "VM runbook lock $path is not held; run through run_windows_lifecycle_vm.sh" }
  $holder = (Get-Content $path -TotalCount 1).Trim()
  if (!$Token -or $holder -ne $Token) {
    throw "VM runbook lock $path is held by another session: $((Get-Content $path -Raw).Trim())"
  }
}

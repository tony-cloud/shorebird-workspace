#!/usr/bin/env bash
# Run the Windows native ARM64 encrypted lifecycle on the local Parallels VM,
# the way CI job "Build Windows engine artifacts (native ARM64)" does.
#
#   tool/run_windows_lifecycle_vm.sh setup       # once: VS C++/clang, SDK debuggers, Python, Go, pwsh, Rust (SYSTEM)
#   tool/run_windows_lifecycle_vm.sh all         # sync + build + lifecycle (the usual loop)
#   tool/run_windows_lifecycle_vm.sh variant     # V2 variant lifecycle (after build; add "sync build" first as needed)
#   tool/run_windows_lifecycle_vm.sh sync|build|lifecycle|cleanup|status|unlock
#   Several modes may be chained: `... sync build lifecycle variant`.
#
# Locking: modes that touch the VM checkout take <VM_ROOT>\runbook.lock (atomic
# create) and pass its token to every VM phase script, which refuses to run
# without it. A busy lock exits 75 and prints the holder; `unlock` removes a
# stale lock left by a killed session (check `status` first).
#
# Inputs reach the VM this way: the Mac workspace HEAD of every repo (root and
# submodules) plus any uncommitted/untracked (non-ignored) changes are turned into
# commits with a temporary index (the Mac index, refs and files are not modified)
# and shipped as git bundles under .agents/windows-vm/sync/ on this volume, read
# by the VM through the Parallels share \\Mac\<volume>. Pushed commits are fetched
# from GitHub. The VM checkout, gclient deps, engine out dir, pub/cargo/go caches
# and lifecycle work dirs live on the VM disk under $VM_ROOT (default C:\sb);
# logs and sanitized results come back to .agents/windows-vm/ on this volume.
# Nothing large is written to the Mac system disk.
#
# Env: VM_NAME ("CWindows 11"), VM_ROOT (C:\sb), GCLIENT_SYNC (auto|always|never),
#      LIFECYCLE_ARGS (extra vm_lifecycle.ps1 args, e.g. "-FreshPubCache -SkipProvider"),
#      BUILD_JOBS (ninja -j; default one job per 3 GB of VM commit limit, at most cores+2).
set -euo pipefail

VM_NAME="${VM_NAME:-CWindows 11}"
VM_ROOT="${VM_ROOT:-C:\\sb}"
GCLIENT_SYNC="${GCLIENT_SYNC:-auto}"
LIFECYCLE_ARGS="${LIFECYCLE_ARGS:-}"
BUILD_JOBS="${BUILD_JOBS:-0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
STATE="$ROOT/.agents/windows-vm"
REPOS=(. dart-sdk flutter shorebird shorebird-server updater depot_tools)
PWSH='C:\Program Files\PowerShell\7\pwsh.exe'
VM_SCRIPTS="$VM_ROOT\\ws\\testapps\\license_flavor_patch_test\\tool\\windows_vm"

case "$ROOT" in
  /Volumes/*/*) ;;
  *) echo "workspace must live on an external volume shared with the VM (/Volumes/<name>/...): $ROOT" >&2; exit 64 ;;
esac
# /Volumes/<name>/rest -> \\Mac\<name>\rest
unc() { local rel="${1#/Volumes/}"; printf '\\\\Mac\\%s' "${rel//\//\\}"; }

mkdir -p "$STATE/logs" "$STATE/results" "$STATE/sync"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
LOCK_TOKEN="$(hostname -s)-$$-$stamp"
LOCK_HELD=0

vm_exec() {  # vm_exec <log-name> <prlctl exec args...>
  local log="$STATE/logs/$stamp-$1.log" code attempt; shift
  echo "[vm-runbook] $* (log: $log)"
  for attempt in 1 2 3; do
    set +e
    prlctl exec "$VM_NAME" "$@" < /dev/null 2>&1 | tee "$log"
    code=${PIPESTATUS[0]}
    set -e
    # prlctl intermittently fails to open the guest session ("PrlJob_GetResult:
    # Invalid argument", exit 255) before anything runs; only then is a retry safe.
    if [[ $code == 255 ]] && ! LC_ALL=C grep -avq '^PrlJob_' "$log"; then
      echo "[vm-runbook] guest session did not start (attempt $attempt); retrying" | tee -a "$log"
      sleep 10
      continue
    fi
    break
  done
  echo "[vm-runbook] exit=$code" | tee -a "$log"
  return "$code"
}
vm_pwsh_user() { local log="$1"; shift; vm_exec "$log" --current-user "$PWSH" -NoProfile -ExecutionPolicy Bypass -File "$@" -LockToken "$LOCK_TOKEN"; }
lock_script() { printf '%s\\testapps\\license_flavor_patch_test\\tool\\windows_vm\\vm_lock.ps1' "$(unc "$ROOT")"; }
acquire_lock() {
  local code attempt
  for attempt in 1 2 3; do
    code=0
    prlctl exec "$VM_NAME" --current-user "$PWSH" -NoProfile -ExecutionPolicy Bypass -Command < /dev/null \
      ". '$(lock_script)'; Enter-RunbookLock -Root '$VM_ROOT' -Token '$LOCK_TOKEN' -Owner 'mac:$(hostname -s) pid:$$ cmd:${*// /,}'" || code=$?
    # 75 = held by someone else; other non-zero codes are prlctl/session failures (retry).
    [[ $code == 0 || $code == 75 ]] && break
    echo "[vm-runbook] lock attempt $attempt: prlctl exit $code, retrying" >&2
    sleep 5
  done
  if [[ $code != 0 ]]; then echo "[vm-runbook] VM runbook lock unavailable (exit $code); another session is using $VM_ROOT" >&2; exit 75; fi
  LOCK_HELD=1
}
release_lock() {
  [[ $LOCK_HELD == 1 ]] || return 0
  prlctl exec "$VM_NAME" --current-user "$PWSH" -NoProfile -ExecutionPolicy Bypass -Command \
    ". '$(lock_script)'; Exit-RunbookLock -Root '$VM_ROOT' -Token '$LOCK_TOKEN'" || true
  LOCK_HELD=0
}

preflight() {
  prlctl list -a | grep -F "$VM_NAME" | grep -q running || { echo "VM '$VM_NAME' is not running (prlctl start \"$VM_NAME\")" >&2; exit 69; }
  prlctl exec "$VM_NAME" cmd /c "dir $(unc "$ROOT")\\scripts >nul" || { echo "VM cannot read the workspace share $(unc "$ROOT")" >&2; exit 69; }
}

repo_url() {
  if [[ "$1" == . ]]; then git -C "$ROOT" remote get-url origin | sed 's#^git@github.com:#https://github.com/#'
  else git -C "$ROOT" config -f .gitmodules "submodule.$1.url"; fi
}

make_sync() {
  local dir="$STATE/sync/$stamp" entries=() repo
  mkdir -p "$dir"
  for repo in "${REPOS[@]}"; do
    local git_dir="$ROOT/$repo" head tree base_tree commit bundle="" index
    base_tree="$(git -C "$git_dir" rev-parse 'HEAD^{tree}')"
    index="$(mktemp "$dir/index.XXXXXX")"
    local excludes=()
    if [[ "$repo" == . ]]; then for sub in "${REPOS[@]:1}"; do excludes+=(":(exclude)$sub"); done; fi
    GIT_INDEX_FILE="$index" git -C "$git_dir" read-tree HEAD
    GIT_INDEX_FILE="$index" git -C "$git_dir" add -A -- . ${excludes[@]+"${excludes[@]}"}
    tree="$(GIT_INDEX_FILE="$index" git -C "$git_dir" write-tree)"
    rm -f "$index"
    head="$(git -C "$git_dir" rev-parse HEAD)"
    commit="$head"
    if [[ "$tree" != "$base_tree" ]]; then
      commit="$(GIT_AUTHOR_NAME=vm-sync GIT_AUTHOR_EMAIL=vm-sync@localhost GIT_COMMITTER_NAME=vm-sync \
        GIT_COMMITTER_EMAIL=vm-sync@localhost git -C "$git_dir" commit-tree "$tree" -p "$head" -m "vm-sync working tree $stamp")"
    fi
    if [[ "$commit" != "$head" || -z "$(git -C "$git_dir" branch -r --contains "$head" 2>/dev/null | head -1)" ]]; then
      bundle="$(echo "$repo" | tr './' '__').bundle"
      git -C "$git_dir" update-ref refs/vm-sync/head "$commit"
      git -C "$git_dir" bundle create -q "$dir/$bundle" refs/vm-sync/head --not --remotes
      git -C "$git_dir" update-ref -d refs/vm-sync/head
      echo "[vm-runbook] $repo: $commit (local changes, bundle $(du -h "$dir/$bundle" | cut -f1))" >&2
    else
      echo "[vm-runbook] $repo: $commit (pushed)" >&2
    fi
    entries+=("$(python3 -I -c 'import json,sys; print(json.dumps({"path":sys.argv[1],"url":sys.argv[2],"head":sys.argv[3],"bundle":sys.argv[4] or None}))' \
      "$repo" "$(repo_url "$repo")" "$commit" "$bundle")")
  done
  (IFS=,; echo "{\"repos\":[${entries[*]}]}") > "$dir/manifest.json"
  echo "$dir"
}

do_setup() {
  vm_exec setup powershell -NoProfile -ExecutionPolicy Bypass -File \
    "$(unc "$ROOT")\\testapps\\license_flavor_patch_test\\tool\\windows_vm\\setup_toolchain.ps1" -Root "$VM_ROOT"
}
do_sync() {
  local dir; dir="$(make_sync | tail -1)"
  vm_pwsh_user sync "$(unc "$ROOT")\\testapps\\license_flavor_patch_test\\tool\\windows_vm\\vm_sync.ps1" -Root "$VM_ROOT" -SyncDir "$(unc "$dir")"
}
do_build() { vm_pwsh_user build "$VM_SCRIPTS\\vm_build_engine.ps1" -Root "$VM_ROOT" -Sync "$GCLIENT_SYNC" -Jobs "$BUILD_JOBS"; }
do_lifecycle() {
  local results="$STATE/results/$stamp-$1"
  mkdir -p "$results"
  local code=0
  # shellcheck disable=SC2086
  vm_pwsh_user "lifecycle-$1" "$VM_SCRIPTS\\vm_lifecycle.ps1" -Root "$VM_ROOT" -Mode "$1" -ResultsDir "$(unc "$results")" $LIFECYCLE_ARGS || code=$?
  echo "[vm-runbook] sanitized results: $results"
  return "$code"
}
do_cleanup() {
  # Owned leftovers only: anything started from the VM root, plus MSBuild/compiler servers.
  prlctl exec "$VM_NAME" powershell -NoProfile -Command "Get-Process | Where-Object { (\$_.Path -and \$_.Path.StartsWith('$VM_ROOT\\', 'OrdinalIgnoreCase')) -or \$_.Name -in @('MSBuild','mspdbsrv','VBCSCompiler','vctip') } | ForEach-Object { Write-Host ('stopping ' + \$_.Name + ' ' + \$_.Id); Stop-Process -Id \$_.Id -Force -ErrorAction SilentlyContinue }"
  # Private session dirs a failed fixture cleanup left behind (they can hold tokens and DPAPI blobs).
  prlctl exec "$VM_NAME" powershell -NoProfile -Command "Get-ChildItem '$VM_ROOT\\tmp' -Directory -ErrorAction SilentlyContinue | Where-Object { \$_.Name -match '^shorebird-(native|variant)-(e2e|probe)-' } | ForEach-Object { Write-Host ('removing leftover ' + \$_.Name); Remove-Item -Recurse -Force \$_.FullName -ErrorAction Continue }"
}
do_status() {
  prlctl exec "$VM_NAME" powershell -NoProfile -Command "Get-Process | Where-Object { (\$_.Path -and \$_.Path.StartsWith('$VM_ROOT\\', 'OrdinalIgnoreCase')) -or \$_.Name -in @('MSBuild','ninja','cl','link','clang-cl','dart','dartvm','cargo','go') } | Select-Object Name,Id,StartTime | Format-Table -AutoSize | Out-String -Width 200; Get-PSDrive C | Select-Object Used,Free | Format-List | Out-String"
}

on_exit() {
  local code=$?
  if [[ $LOCK_HELD == 1 ]]; then do_cleanup || true; fi
  release_lock
  exit "$code"
}

[[ $# -gt 0 ]] || set -- all
for command in "$@"; do
  case "$command" in setup|sync|build|lifecycle|variant|all|cleanup|status|unlock) ;;
    *) echo "usage: $0 [setup|sync|build|lifecycle|variant|all|cleanup|status|unlock]..." >&2; exit 64 ;;
  esac
done
preflight
trap on_exit EXIT
for command in "$@"; do
  case "$command" in
    sync|build|lifecycle|variant|all) [[ $LOCK_HELD == 1 ]] || acquire_lock "$@" ;;
  esac
  case "$command" in
    setup) do_setup ;;
    sync) do_sync ;;
    build) do_build ;;
    lifecycle) do_lifecycle encrypted ;;
    variant) do_lifecycle variant ;;
    all) do_sync; do_build; do_lifecycle encrypted ;;
    cleanup) do_cleanup ;;
    status) do_status; prlctl exec "$VM_NAME" cmd /c "type $VM_ROOT\\runbook.lock 2>nul || echo no runbook lock" ;;
    unlock) prlctl exec "$VM_NAME" cmd /c "del $VM_ROOT\\runbook.lock" ;;
  esac
done

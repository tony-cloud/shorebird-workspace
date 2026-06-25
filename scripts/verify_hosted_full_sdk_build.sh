#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: verify_hosted_full_sdk_build.sh --repo owner/name [options]

Dispatches the Open Shorebird full SDK build on GitHub Actions, waits for the
workflow run to finish, downloads all artifacts, and verifies the release
manifest plus assembled artifact mirror.

Options:
  --repo owner/name                 GitHub repository to run against.
  --ref branch-or-sha               Ref to dispatch. Defaults to current branch.
  --workflow file                   Workflow file. Defaults to open-shorebird-ci.yml.
  --download-dir path               Artifact download directory. Defaults to hosted-full-sdk-artifacts.
  --timeout-minutes minutes         Maximum wait time. Defaults to 720.
  --poll-seconds seconds            Poll interval. Defaults to 30.
  --linux-heavy-runner label        Override linux_heavy_runner.
  --macos-heavy-runner label        Override macos_heavy_runner.
  --sdk-min-free-disk-gb value      Override sdk_min_free_disk_gb.
  --engine-min-free-disk-gb value   Override engine_min_free_disk_gb.
  --base-flutter-engine-revision v  Override base_flutter_engine_revision.
  --skip-gclient-sync               Dispatch with run_gclient_sync=false.
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=""
REF=""
WORKFLOW="open-shorebird-ci.yml"
DOWNLOAD_DIR="hosted-full-sdk-artifacts"
TIMEOUT_MINUTES=720
POLL_SECONDS=30
LINUX_HEAVY_RUNNER=""
MACOS_HEAVY_RUNNER=""
SDK_MIN_FREE_DISK_GB=""
ENGINE_MIN_FREE_DISK_GB=""
BASE_FLUTTER_ENGINE_REVISION=""
RUN_GCLIENT_SYNC=true

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --ref)
      REF="${2:-}"
      shift 2
      ;;
    --workflow)
      WORKFLOW="${2:-}"
      shift 2
      ;;
    --download-dir)
      DOWNLOAD_DIR="${2:-}"
      shift 2
      ;;
    --timeout-minutes)
      TIMEOUT_MINUTES="${2:-}"
      shift 2
      ;;
    --poll-seconds)
      POLL_SECONDS="${2:-}"
      shift 2
      ;;
    --linux-heavy-runner)
      LINUX_HEAVY_RUNNER="${2:-}"
      shift 2
      ;;
    --macos-heavy-runner)
      MACOS_HEAVY_RUNNER="${2:-}"
      shift 2
      ;;
    --sdk-min-free-disk-gb)
      SDK_MIN_FREE_DISK_GB="${2:-}"
      shift 2
      ;;
    --engine-min-free-disk-gb)
      ENGINE_MIN_FREE_DISK_GB="${2:-}"
      shift 2
      ;;
    --base-flutter-engine-revision)
      BASE_FLUTTER_ENGINE_REVISION="${2:-}"
      shift 2
      ;;
    --skip-gclient-sync)
      RUN_GCLIENT_SYNC=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 64
      ;;
  esac
done

if [[ -z "$REPO" ]]; then
  echo "--repo owner/name is required" >&2
  usage
  exit 64
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI 'gh' is required" >&2
  exit 69
fi

if [[ -z "$REF" ]]; then
  REF="$(git -C "$ROOT" branch --show-current 2>/dev/null || true)"
fi
if [[ -z "$REF" ]]; then
  echo "--ref is required when the current checkout is detached" >&2
  exit 64
fi

run_fields=(
  -f full_sdk_build=true
  -f run_gclient_sync="$RUN_GCLIENT_SYNC"
  -f run_runtime_smokes=false
)
[[ -z "$LINUX_HEAVY_RUNNER" ]] || run_fields+=(-f linux_heavy_runner="$LINUX_HEAVY_RUNNER")
[[ -z "$MACOS_HEAVY_RUNNER" ]] || run_fields+=(-f macos_heavy_runner="$MACOS_HEAVY_RUNNER")
[[ -z "$SDK_MIN_FREE_DISK_GB" ]] || run_fields+=(-f sdk_min_free_disk_gb="$SDK_MIN_FREE_DISK_GB")
[[ -z "$ENGINE_MIN_FREE_DISK_GB" ]] || run_fields+=(-f engine_min_free_disk_gb="$ENGINE_MIN_FREE_DISK_GB")
[[ -z "$BASE_FLUTTER_ENGINE_REVISION" ]] || run_fields+=(-f base_flutter_engine_revision="$BASE_FLUTTER_ENGINE_REVISION")

echo "Dispatching $WORKFLOW on $REPO@$REF with full_sdk_build=true"
start_epoch="$(date +%s)"
start_iso="$(date -u -r "$start_epoch" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d "@$start_epoch" +"%Y-%m-%dT%H:%M:%SZ")"
gh workflow run "$WORKFLOW" \
  --repo "$REPO" \
  --ref "$REF" \
  "${run_fields[@]}"

run_id=""
for _ in {1..40}; do
  run_list_args=(
    --repo "$REPO"
    --workflow "$WORKFLOW"
    --event workflow_dispatch
    --json databaseId,createdAt
    --limit 20
  )
  if [[ "$REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
    run_list_args+=(--commit "$REF")
  else
    run_list_args+=(--branch "$REF")
  fi
  run_id="$(
    gh run list \
      "${run_list_args[@]}" \
      --jq ".[] | select(.createdAt >= \"$start_iso\") | .databaseId" \
      |
      head -n 1
  )"
  [[ -z "$run_id" ]] || break
  sleep 3
done

if [[ -z "$run_id" ]]; then
  echo "unable to find dispatched workflow run for $WORKFLOW on $REF" >&2
  exit 70
fi

echo "Waiting for hosted full SDK run: $run_id"
deadline=$((start_epoch + TIMEOUT_MINUTES * 60))
run_head_sha=""
while true; do
  IFS=$'\t' read -r status conclusion url run_head_sha < <(
    gh run view "$run_id" \
      --repo "$REPO" \
      --json status,conclusion,url,headSha \
      --jq '[.status, (.conclusion // ""), .url, (.headSha // "")] | @tsv'
  )
  echo "run $run_id status=$status conclusion=${conclusion:-null} url=$url"

  if [[ "$status" == "completed" ]]; then
    if [[ "$conclusion" != "success" ]]; then
      echo "hosted full SDK build failed: conclusion=$conclusion" >&2
      exit 70
    fi
    break
  fi
  if [[ "$(date +%s)" -ge "$deadline" ]]; then
    echo "timed out waiting for hosted full SDK build after $TIMEOUT_MINUTES minutes" >&2
    exit 70
  fi
  sleep "$POLL_SECONDS"
done

rm -rf "$DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"
gh run download "$run_id" --repo "$REPO" --dir "$DOWNLOAD_DIR"
if [[ -z "$run_head_sha" ]]; then
  echo "unable to read headSha for workflow run $run_id" >&2
  exit 70
fi
"$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
  --github-sha "$run_head_sha" \
  "$DOWNLOAD_DIR"

echo "hosted full SDK build verified: $run_id"

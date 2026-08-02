#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: verify_hosted_full_sdk_build.sh --repo owner/name [options]

Dispatches the Open Shorebird full SDK build on GitHub Actions, waits for the
workflow run to finish, downloads all artifacts, and verifies the release
manifest plus assembled artifact mirror.

Uses GitHub CLI when `gh` is available. Otherwise uses the GitHub REST API with
`GITHUB_TOKEN` or `GH_TOKEN`; that fallback also requires `curl`, `jq`, and
`unzip`.

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
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

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

api_token() {
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    printf '%s' "$GITHUB_TOKEN"
  elif [[ -n "${GH_TOKEN:-}" ]]; then
    printf '%s' "$GH_TOKEN"
  else
    echo "GITHUB_TOKEN or GH_TOKEN is required when gh is not installed" >&2
    exit 69
  fi
}

api_require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required when gh is not installed" >&2
    exit 69
  fi
}

api_request() {
  local method="$1"
  local path="$2"
  local data="${3:-}"
  local token
  token="$(api_token)"

  local curl_args=(
    -fsSL
    -X "$method"
    -H "Accept: application/vnd.github+json"
    -H "Authorization: Bearer $token"
    -H "X-GitHub-Api-Version: 2022-11-28"
  )
  if [[ -n "$data" ]]; then
    curl_args+=(-H "Content-Type: application/json" -d "$data")
  fi

  curl "${curl_args[@]}" "$GITHUB_API_URL/repos/$REPO$path"
}

api_dispatch_payload() {
  local inputs_filter
  inputs_filter='{full_sdk_build: "true", run_gclient_sync: $run_gclient_sync, run_runtime_smokes: "false"}'
  local jq_args=(
    --arg ref "$REF"
    --arg run_gclient_sync "$RUN_GCLIENT_SYNC"
  )
  if [[ -n "$LINUX_HEAVY_RUNNER" ]]; then
    inputs_filter="$inputs_filter + {linux_heavy_runner: \$linux_heavy_runner}"
    jq_args+=(--arg linux_heavy_runner "$LINUX_HEAVY_RUNNER")
  fi
  if [[ -n "$MACOS_HEAVY_RUNNER" ]]; then
    inputs_filter="$inputs_filter + {macos_heavy_runner: \$macos_heavy_runner}"
    jq_args+=(--arg macos_heavy_runner "$MACOS_HEAVY_RUNNER")
  fi
  if [[ -n "$SDK_MIN_FREE_DISK_GB" ]]; then
    inputs_filter="$inputs_filter + {sdk_min_free_disk_gb: \$sdk_min_free_disk_gb}"
    jq_args+=(--arg sdk_min_free_disk_gb "$SDK_MIN_FREE_DISK_GB")
  fi
  if [[ -n "$ENGINE_MIN_FREE_DISK_GB" ]]; then
    inputs_filter="$inputs_filter + {engine_min_free_disk_gb: \$engine_min_free_disk_gb}"
    jq_args+=(--arg engine_min_free_disk_gb "$ENGINE_MIN_FREE_DISK_GB")
  fi
  if [[ -n "$BASE_FLUTTER_ENGINE_REVISION" ]]; then
    inputs_filter="$inputs_filter + {base_flutter_engine_revision: \$base_flutter_engine_revision}"
    jq_args+=(--arg base_flutter_engine_revision "$BASE_FLUTTER_ENGINE_REVISION")
  fi

  jq -n "${jq_args[@]}" "{ref: \$ref, inputs: ($inputs_filter)}"
}

api_download_artifacts() {
  rm -rf "$DOWNLOAD_DIR"
  mkdir -p "$DOWNLOAD_DIR"

  local page=1
  local downloaded_count=0
  while true; do
    local response
    response="$(api_request GET "/actions/runs/$run_id/artifacts?per_page=100&page=$page")"
    local artifact_count
    artifact_count="$(jq '.artifacts | length' <<<"$response")"
    [[ "$artifact_count" == "0" ]] && break

    while IFS= read -r artifact; do
      local name
      local url
      name="$(jq -r '.name' <<<"$artifact")"
      url="$(jq -r '.archive_download_url' <<<"$artifact")"
      case "$name" in
        ""|*/*|*..*)
          echo "unsafe GitHub artifact name: $name" >&2
          exit 70
          ;;
      esac
      if [[ -z "$url" || "$url" == "null" ]]; then
        echo "missing archive_download_url for artifact: $name" >&2
        exit 70
      fi

      local artifact_dir
      local zip_path
      artifact_dir="$DOWNLOAD_DIR/$name"
      zip_path="$(mktemp "${TMPDIR:-/tmp}/github-artifact.XXXXXX")"
      curl \
        -fsSL \
        -L \
        -H "Accept: application/vnd.github+json" \
        -H "Authorization: Bearer $(api_token)" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        -o "$zip_path" \
        "$url"
      rm -rf "$artifact_dir"
      mkdir -p "$artifact_dir"
      unzip -q "$zip_path" -d "$artifact_dir"
      rm -f "$zip_path"
      downloaded_count=$((downloaded_count + 1))
    done < <(jq -c '.artifacts[] | select(.expired | not)' <<<"$response")

    page=$((page + 1))
  done

  if [[ "$downloaded_count" -eq 0 ]]; then
    echo "no non-expired artifacts were available for workflow run $run_id" >&2
    exit 70
  fi
}

use_gh=0
if command -v gh >/dev/null 2>&1; then
  use_gh=1
else
  api_require_tool curl
  api_require_tool jq
  api_require_tool unzip
  api_token >/dev/null
fi

echo "Dispatching $WORKFLOW on $REPO@$REF with full_sdk_build=true"
start_epoch="$(date +%s)"
start_iso="$(date -u -r "$start_epoch" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d "@$start_epoch" +"%Y-%m-%dT%H:%M:%SZ")"
if [[ "$use_gh" == "1" ]]; then
  gh workflow run "$WORKFLOW" \
    --repo "$REPO" \
    --ref "$REF" \
    "${run_fields[@]}"
else
  api_request POST "/actions/workflows/$WORKFLOW/dispatches" "$(api_dispatch_payload)" >/dev/null
fi

run_id=""
for _ in {1..40}; do
  if [[ "$use_gh" == "1" ]]; then
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
  else
    runs_response="$(api_request GET "/actions/workflows/$WORKFLOW/runs?event=workflow_dispatch&per_page=20")"
    run_id="$(
      jq -r \
        --arg ref "$REF" \
        --arg start_iso "$start_iso" \
        '
          .workflow_runs
          | map(select(.created_at >= $start_iso))
          | map(select(.head_branch == $ref or .head_sha == $ref))
          | sort_by(.created_at)
          | reverse
          | .[0].id // ""
        ' \
        <<<"$runs_response"
    )"
  fi
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
  if [[ "$use_gh" == "1" ]]; then
    IFS=$'\t' read -r status conclusion url run_head_sha < <(
      gh run view "$run_id" \
        --repo "$REPO" \
        --json status,conclusion,url,headSha \
        --jq '[.status, (.conclusion // ""), .url, (.headSha // "")] | @tsv'
    )
  else
    run_response="$(api_request GET "/actions/runs/$run_id")"
    IFS=$'\t' read -r status conclusion url run_head_sha < <(
      jq -r '[.status, (.conclusion // ""), .html_url, (.head_sha // "")] | @tsv' \
        <<<"$run_response"
    )
  fi
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

if [[ "$use_gh" == "1" ]]; then
  rm -rf "$DOWNLOAD_DIR"
  mkdir -p "$DOWNLOAD_DIR"
  gh run download "$run_id" --repo "$REPO" --dir "$DOWNLOAD_DIR"
else
  api_download_artifacts
fi
if [[ -z "$run_head_sha" ]]; then
  echo "unable to read headSha for workflow run $run_id" >&2
  exit 70
fi
"$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
  --github-sha "$run_head_sha" \
  "$DOWNLOAD_DIR"

echo "hosted full SDK build verified: $run_id"

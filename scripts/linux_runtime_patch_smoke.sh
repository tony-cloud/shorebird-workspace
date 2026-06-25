#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_APP_DIR="${SOURCE_APP_DIR:-$ROOT/testapps/license_flavor_patch_test}"
FLUTTER_BIN="${FLUTTER_BIN:-$ROOT/flutter/bin/flutter}"
APP_ID="${SHOREBIRD_APP_ID:-license-flavor-patch-test}"
LOCAL_ENGINE_SRC_PATH="${LOCAL_ENGINE_SRC_PATH:-$ROOT/flutter/engine/src}"
DEFAULT_LOCAL_ENGINE="linux_release_x64"
if [[ ! -d "$LOCAL_ENGINE_SRC_PATH/out/$DEFAULT_LOCAL_ENGINE" &&
      -d "$LOCAL_ENGINE_SRC_PATH/out/host_release" ]]; then
  DEFAULT_LOCAL_ENGINE="host_release"
fi
LOCAL_ENGINE="${LOCAL_ENGINE:-$DEFAULT_LOCAL_ENGINE}"
LOCAL_ENGINE_HOST="${LOCAL_ENGINE_HOST:-$LOCAL_ENGINE}"
WORK_DIR="${LINUX_RUNTIME_SMOKE_WORK_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-linux-runtime.XXXXXX")}"
APP_COPY="$WORK_DIR/app"
HOME_DIR="$WORK_DIR/home"

if [[ "${KEEP_LINUX_RUNTIME_SMOKE_ARTIFACTS:-0}" != "1" ]]; then
  trap 'rm -rf "$WORK_DIR"' EXIT
fi

require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 127
  fi
}

python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
  else
    printf '%s\n' python
  fi
}

copy_app_fixture() {
  mkdir -p "$APP_COPY"
  (
    cd "$SOURCE_APP_DIR"
    tar \
      --exclude='./build' \
      --exclude='./.dart_tool' \
      --exclude='./android/.gradle' \
      --exclude='./ios/Pods' \
      --exclude='./macos/Flutter/ephemeral' \
      -cf - .
  ) | tar -C "$APP_COPY" -xf -
}

ensure_linux_platform() {
  if [[ -d "$APP_COPY/linux" ]]; then
    return
  fi
  (
    cd "$APP_COPY"
    "$FLUTTER_BIN" create --platforms=linux --project-name=license_flavor_patch_test .
  )
}

build_linux_bundle() {
  local license="$1"
  local output="$2"
  (
    cd "$APP_COPY"
    "$FLUTTER_BIN" build linux --release \
      --local-engine-src-path="$LOCAL_ENGINE_SRC_PATH" \
      --local-engine="$LOCAL_ENGINE" \
      --local-engine-host="$LOCAL_ENGINE_HOST" \
      --dart-define="LICENSE_TYPE=$license"
  )
  local bundle
  bundle="$(find "$APP_COPY/build/linux" -path '*/release/bundle' -type d -print -quit)"
  if [[ -z "$bundle" || ! -x "$bundle/license_flavor_patch_test" ]]; then
    echo "Failed to find Linux release bundle under $APP_COPY/build/linux" >&2
    exit 66
  fi
  rm -rf "$output"
  cp -a "$bundle" "$output"
}

release_version_for_bundle() {
  local bundle="$1"
  local version_json="$bundle/data/flutter_assets/version.json"
  local py
  py="$(python_bin)"
  "$py" - "$version_json" <<'PY'
import json
import pathlib
import sys

version = json.loads(pathlib.Path(sys.argv[1]).read_text())
build_name = str(version.get("version", ""))
build_number = str(version.get("build_number", ""))
if build_number:
    print(f"{build_name}+{build_number}")
else:
    print(build_name)
PY
}

run_saved_app() {
  local label="$1"
  local bundle="$2"
  local stdout="$WORK_DIR/$label.stdout"
  local stderr="$WORK_DIR/$label.stderr"
  local tmp_status="$WORK_DIR/tmp/license_flavor_patch_status.txt"
  local home_status="$HOME_DIR/Library/Application Support/license_flavor_patch_status.txt"

  rm -f "$tmp_status" "$home_status"
  mkdir -p "$WORK_DIR/tmp" "$HOME_DIR"

  local -a app_command=(env HOME="$HOME_DIR" TMPDIR="$WORK_DIR/tmp" "$bundle/license_flavor_patch_test")
  if [[ "${LINUX_RUNTIME_SMOKE_XVFB:-auto}" != "0" && -z "${DISPLAY:-}" && "$(command -v xvfb-run || true)" != "" ]]; then
    app_command=(xvfb-run -a "${app_command[@]}")
  fi

  "${app_command[@]}" >"$stdout" 2>"$stderr" &
  local pid=$!
  for _ in $(seq 1 "${LINUX_RUNTIME_WAIT_ATTEMPTS:-120}"); do
    if [[ -f "$tmp_status" || -f "$home_status" ]]; then
      break
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  else
    wait "$pid" 2>/dev/null || true
  fi

  echo "${label}_stdout=$stdout"
  echo "${label}_stderr=$stderr"
  if [[ -f "$tmp_status" ]]; then
    echo "${label}_status_file=$tmp_status"
    cat "$tmp_status"
  elif [[ -f "$home_status" ]]; then
    echo "${label}_status_file=$home_status"
    cat "$home_status"
  else
    echo "${label}_status_file_missing" >&2
    echo "--- $label stderr ---" >&2
    sed -n '1,180p' "$stderr" >&2 || true
    return 70
  fi
}

read_launch_status() {
  local tmp_status="$WORK_DIR/tmp/license_flavor_patch_status.txt"
  local home_status="$HOME_DIR/Library/Application Support/license_flavor_patch_status.txt"
  if [[ -f "$tmp_status" ]]; then
    cat "$tmp_status"
  elif [[ -f "$home_status" ]]; then
    cat "$home_status"
  fi
}

require_status() {
  local label="$1"
  local expected_license="$2"
  local expected_feature="$3"
  local status
  status="$(read_launch_status)"
  if ! grep -q "license:$expected_license" <<<"$status" ||
     ! grep -q "pro-feature:$expected_feature" <<<"$status"; then
    echo "$label Linux app did not report expected status." >&2
    echo "Expected: license:$expected_license / pro-feature:$expected_feature" >&2
    echo "Actual:" >&2
    printf '%s\n' "$status" >&2
    exit 70
  fi
}

seed_patch() {
  local pro_bundle="$1"
  local release_version="$2"
  local patch_file="$pro_bundle/lib/libapp.so"
  local state_root="$HOME_DIR/.shorebird_cache/shorebird_updater/$APP_ID"
  local size
  size="$(wc -c <"$patch_file" | tr -d ' ')"

  rm -rf "$state_root"
  mkdir -p "$state_root/patches/1"
  cp "$patch_file" "$state_root/patches/1/dlc.vmcode"
  cat >"$state_root/state.json" <<EOF
{
  "client_id": "linux-runtime-smoke",
  "release_version": "$release_version",
  "queued_events": []
}
EOF
  cat >"$state_root/pointers.json" <<'EOF'
{
  "next_boot_patch": 1,
  "last_booted_patch": null,
  "currently_booting_patch": null,
  "boot_started_at": null
}
EOF
  cat >"$state_root/patches/1/state.json" <<EOF
{
  "kind": "Installed",
  "signature": null,
  "size": $size
}
EOF
  echo "linux_seeded_patch=$state_root/patches/1/dlc.vmcode"
  echo "linux_seeded_patch_size=$size"
}

require_tool "$FLUTTER_BIN"
require_tool tar

copy_app_fixture
ensure_linux_platform

free_bundle="$WORK_DIR/free-bundle"
pro_bundle="$WORK_DIR/pro-bundle"

build_linux_bundle free "$free_bundle"
build_linux_bundle pro "$pro_bundle"

release_version="$(release_version_for_bundle "$free_bundle")"

run_saved_app base "$free_bundle"
require_status base free off
seed_patch "$pro_bundle" "$release_version"
run_saved_app patch "$free_bundle"
require_status patch pro enabled

echo "linux_runtime_patch_smoke=passed"

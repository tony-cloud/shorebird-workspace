#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$ROOT/testapps/license_flavor_patch_test}"
FLUTTER_BIN="${FLUTTER_BIN:-$ROOT/flutter/bin/flutter}"
ADB_BIN="${ADB_BIN:-adb}"
PACKAGE="${ANDROID_PACKAGE:-com.example.licenseflavorpatchtest.license_flavor_patch_test}"
ACTIVITY="${ANDROID_ACTIVITY:-com.example.licenseflavorpatchtest.license_flavor_patch_test.MainActivity}"
RELEASE_VERSION="${ANDROID_RELEASE_VERSION:-1.0+1}"
LOCAL_ENGINE_SRC_PATH="${LOCAL_ENGINE_SRC_PATH:-$ROOT/flutter/engine/src}"
LOCAL_ENGINE="${LOCAL_ENGINE:-android_release_arm64}"
LOCAL_ENGINE_HOST="${LOCAL_ENGINE_HOST:-host_release_arm64}"
WORK_DIR="${ANDROID_RUNTIME_SMOKE_WORK_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-android-runtime.XXXXXX")}"
SEED_REMOTE_DIR="${ANDROID_SEED_REMOTE_DIR:-/data/local/tmp/open-shorebird-android-runtime-seed}"
TARGET_PLATFORM="${ANDROID_TARGET_PLATFORM:-android-arm64}"

if [[ "${KEEP_ANDROID_RUNTIME_SMOKE_ARTIFACTS:-0}" != "1" ]]; then
  trap 'rm -rf "$WORK_DIR"' EXIT
fi

ADB=("$ADB_BIN")
if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  ADB+=("-s" "$ANDROID_SERIAL")
fi

adb_cmd() {
  "${ADB[@]}" "$@"
}

adb_shell() {
  adb_cmd shell "$@"
}

python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
  else
    printf '%s\n' python
  fi
}

require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 127
  fi
}

build_apk() {
  local license="$1"
  local output="$2"
  if [[ "${SKIP_ANDROID_BUILDS:-0}" == "1" ]]; then
    local env_name
    case "$license" in
      free) env_name=ANDROID_FREE_APK ;;
      pro) env_name=ANDROID_PRO_APK ;;
      *)
        echo "Unsupported license variant: $license" >&2
        exit 64
        ;;
    esac
    local existing="${!env_name:-}"
    if [[ -z "$existing" || ! -f "$existing" ]]; then
      echo "SKIP_ANDROID_BUILDS=1 requires $env_name to point at an APK" >&2
      exit 66
    fi
    cp "$existing" "$output"
    return
  fi

  (
    cd "$APP_DIR"
    "$FLUTTER_BIN" build apk --release \
      --target-platform "$TARGET_PLATFORM" \
      --local-engine-src-path="$LOCAL_ENGINE_SRC_PATH" \
      --local-engine="$LOCAL_ENGINE" \
      --local-engine-host="$LOCAL_ENGINE_HOST" \
      --dart-define="LICENSE_TYPE=$license"
  )
  cp "$APP_DIR/build/app/outputs/flutter-apk/app-release.apk" "$output"
}

extract_libapp() {
  local apk="$1"
  local output="$2"
  local py
  py="$(python_bin)"
  "$py" - "$apk" "$output" <<'PY'
import pathlib
import sys
import zipfile

apk_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(apk_path) as archive:
    candidates = [
        name for name in archive.namelist()
        if name.endswith("/libapp.so") and "arm64-v8a/" in name
    ]
    if not candidates:
        raise SystemExit(f"missing arm64 libapp.so in {apk_path}")
    output_path.write_bytes(archive.read(candidates[0]))
PY
}

wait_for_text() {
  local expected="$1"
  local dump="$WORK_DIR/window.xml"
  for _ in $(seq 1 "${ANDROID_UI_WAIT_ATTEMPTS:-80}"); do
    adb_shell uiautomator dump /sdcard/open_shorebird_window.xml >/dev/null 2>&1 || true
    adb_cmd exec-out cat /sdcard/open_shorebird_window.xml >"$dump" 2>/dev/null || true
    if grep -q "$expected" "$dump"; then
      return 0
    fi
    sleep 0.25
  done
  echo "Timed out waiting for Android UI text: $expected" >&2
  echo "Last uiautomator dump:" >&2
  sed -n '1,120p' "$dump" >&2 || true
  return 70
}

start_app() {
  adb_shell am force-stop "$PACKAGE" >/dev/null 2>&1 || true
  adb_shell am start -W -n "$PACKAGE/$ACTIVITY" >/dev/null
}

can_seed_with_run_as() {
  adb_shell run-as "$PACKAGE" sh -c 'test -d files' >/dev/null 2>&1
}

can_seed_with_root() {
  if adb_shell sh -c 'test "$(id -u)" = "0"' >/dev/null 2>&1; then
    return 0
  fi
  adb_cmd root >/dev/null 2>&1 || true
  adb_cmd wait-for-device >/dev/null 2>&1 || true
  adb_shell sh -c 'test "$(id -u)" = "0"' >/dev/null 2>&1
}

seed_with_run_as() {
  adb_shell run-as "$PACKAGE" sh -c \
    "mkdir -p files && rm -rf files/shorebird_updater && cp -R '$SEED_REMOTE_DIR/shorebird_updater' files/"
}

seed_with_root() {
  local target="/data/data/$PACKAGE/files"
  local owner
  owner="$(adb_shell sh -c "stat -c '%u:%g' '$target'" | tr -d '\r')"
  adb_shell sh -c \
    "rm -rf '$target/shorebird_updater' && cp -R '$SEED_REMOTE_DIR/shorebird_updater' '$target/' && chown -R '$owner' '$target/shorebird_updater'"
}

prepare_seed() {
  local libapp="$1"
  local seed_root="$WORK_DIR/seed/shorebird_updater"
  local size
  size="$(wc -c <"$libapp" | tr -d ' ')"
  rm -rf "$WORK_DIR/seed"
  mkdir -p "$seed_root/patches/1"
  cp "$libapp" "$seed_root/patches/1/dlc.vmcode"
  cat >"$seed_root/state.json" <<EOF
{
  "client_id": "android-runtime-smoke",
  "release_version": "$RELEASE_VERSION",
  "queued_events": []
}
EOF
  cat >"$seed_root/pointers.json" <<'EOF'
{
  "next_boot_patch": 1,
  "last_booted_patch": null,
  "currently_booting_patch": null,
  "boot_started_at": null
}
EOF
  cat >"$seed_root/patches/1/state.json" <<EOF
{
  "kind": "Installed",
  "signature": null,
  "size": $size
}
EOF
  adb_shell rm -rf "$SEED_REMOTE_DIR" >/dev/null 2>&1 || true
  adb_shell mkdir -p "$SEED_REMOTE_DIR" >/dev/null
  adb_cmd push "$seed_root" "$SEED_REMOTE_DIR/" >/dev/null
  echo "seeded_patch_size=$size"
}

require_tool "$ADB_BIN"
require_tool "$FLUTTER_BIN"
require_tool java

adb_cmd start-server >/dev/null
device_count="$(adb_cmd devices | awk 'NR > 1 && $2 == "device" { count++ } END { print count + 0 }')"
if [[ "$device_count" -lt 1 ]]; then
  echo "No Android device or emulator is connected." >&2
  echo "Start an emulator/device, then rerun this script. Use ANDROID_SERIAL to pick a device." >&2
  exit 69
fi
if [[ "$device_count" -gt 1 && -z "${ANDROID_SERIAL:-}" ]]; then
  echo "Multiple Android devices are connected; set ANDROID_SERIAL." >&2
  adb_cmd devices -l >&2
  exit 64
fi

free_apk="$WORK_DIR/free.apk"
pro_apk="$WORK_DIR/pro.apk"
pro_libapp="$WORK_DIR/pro-libapp.so"

build_apk free "$free_apk"
build_apk pro "$pro_apk"
extract_libapp "$pro_apk" "$pro_libapp"

adb_cmd uninstall "$PACKAGE" >/dev/null 2>&1 || true
adb_cmd install -r "$free_apk" >/dev/null

start_app
wait_for_text 'license:free'
wait_for_text 'pro-feature:off'
echo "android_base_status=license:free/pro-feature:off"

prepare_seed "$pro_libapp"
if can_seed_with_run_as; then
  seed_with_run_as
  echo "android_seed_mode=run-as"
elif can_seed_with_root; then
  seed_with_root
  echo "android_seed_mode=root"
else
  cat >&2 <<EOF
Unable to seed the Android app-private updater directory.

Use a debuggable build/device where 'adb shell run-as $PACKAGE' works, or use
a rooted emulator/device where 'adb root' works. The smoke seed target is:
  /data/data/$PACKAGE/files/shorebird_updater
EOF
  exit 77
fi

start_app
wait_for_text 'license:pro'
wait_for_text 'pro-feature:enabled'
echo "android_patch_status=license:pro/pro-feature:enabled"

adb_shell am force-stop "$PACKAGE" >/dev/null 2>&1 || true
echo "android_runtime_patch_smoke=passed"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ios-route-validator.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

IOS_ENGINE_DIR="$TMP_DIR/ios_release"
HOST_ENGINE_DIR="$TMP_DIR/host_release_arm64"
mkdir -p "$IOS_ENGINE_DIR" "$HOST_ENGINE_DIR"

cat > "$IOS_ENGINE_DIR/args.gn" <<'EOF'
target_os = "android"
target_os = "ios"
dart_dynamic_modules = false
dart_enable_aot_patching = false
dart_enable_aot_patching = true
dart_enable_shorebird_interpreter = false
dart_enable_shorebird_interpreter = true
shorebird_use_interpreter = false
shorebird_use_interpreter = true
shorebird_enable_aot_patching = true
shorebird_enable_aot_patching = false
EOF

cat > "$HOST_ENGINE_DIR/args.gn" <<'EOF'
target_os = "linux"
target_os = "mac"
dart_dynamic_modules = false
dart_enable_aot_patching = false
dart_enable_aot_patching = true
dart_enable_shorebird_interpreter = false
dart_enable_shorebird_interpreter = true
shorebird_use_interpreter = false
shorebird_use_interpreter = true
EOF

write_artifact() {
  local path="$1"
  local runtime_mode="$2"
  local target_os="${3:-ios}"
  cat > "$path" <<EOF
{
  "format": "open-aot-vmcode-encrypted-v1",
  "metadata": {
    "app_id": "app.test",
    "app_build_id": "1",
    "flavor_id": "pro",
    "license_type": "pro",
    "sdk_hash": "sdk",
    "base_snapshot_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "patch_snapshot_hash": "1111111111111111111111111111111111111111111111111111111111111111",
    "target_os": "$target_os",
    "target_arch": "arm64",
    "runtime_mode": "$runtime_mode"
  },
  "payload_kind": "full-snapshot",
  "reconstructed_size": 4,
  "payload_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "encrypted_payload_base64": "AQIDBA==",
  "encryption": {
    "algorithm": "AES-256-GCM",
    "key_id": "test-key",
    "nonce_base64": "AQIDBAUGBwgJCgsM",
    "tag_base64": "AQIDBAUGBwgJCgsMDQ4PEA==",
    "aad_sha256": "3333333333333333333333333333333333333333333333333333333333333333"
  }
}
EOF
}

valid_artifact="$TMP_DIR/valid.vmcode"
write_artifact "$valid_artifact" "dart-bytecode-interpreter"
IOS_ENGINE_DIR="$IOS_ENGINE_DIR" \
  HOST_ENGINE_DIR="$HOST_ENGINE_DIR" \
  IOS_PATCH_ARTIFACT="$valid_artifact" \
  "$ROOT/scripts/verify_ios_interpreter_route.sh" >/dev/null

bad_runtime="$TMP_DIR/bad-runtime.vmcode"
write_artifact "$bad_runtime" "dart-dynamic-modules"
if IOS_ENGINE_DIR="$IOS_ENGINE_DIR" \
    HOST_ENGINE_DIR="$HOST_ENGINE_DIR" \
    IOS_PATCH_ARTIFACT="$bad_runtime" \
    "$ROOT/scripts/verify_ios_interpreter_route.sh" >/dev/null 2>&1; then
  echo "iOS route validator unexpectedly accepted DART_DYNAMIC_MODULES metadata" >&2
  exit 70
fi

bad_target="$TMP_DIR/bad-target.vmcode"
write_artifact "$bad_target" "dart-bytecode-interpreter" "android"
if IOS_ENGINE_DIR="$IOS_ENGINE_DIR" \
    HOST_ENGINE_DIR="$HOST_ENGINE_DIR" \
    IOS_PATCH_ARTIFACT="$bad_target" \
    "$ROOT/scripts/verify_ios_interpreter_route.sh" >/dev/null 2>&1; then
  echo "iOS route validator unexpectedly accepted a non-iOS patch artifact" >&2
  exit 70
fi

bad_json="$TMP_DIR/bad-json.vmcode"
printf '{"format":"open-aot-vmcode-encrypted-v1"\n' > "$bad_json"
if IOS_ENGINE_DIR="$IOS_ENGINE_DIR" \
    HOST_ENGINE_DIR="$HOST_ENGINE_DIR" \
    IOS_PATCH_ARTIFACT="$bad_json" \
    "$ROOT/scripts/verify_ios_interpreter_route.sh" >/dev/null 2>&1; then
  echo "iOS route validator unexpectedly accepted malformed JSON" >&2
  exit 70
fi

bad_native="$TMP_DIR/bad-native.vmcode"
printf '\xcf\xfa\xed\xfe' > "$bad_native"
if IOS_ENGINE_DIR="$IOS_ENGINE_DIR" \
    HOST_ENGINE_DIR="$HOST_ENGINE_DIR" \
    IOS_PATCH_ARTIFACT="$bad_native" \
    "$ROOT/scripts/verify_ios_interpreter_route.sh" >/dev/null 2>&1; then
  echo "iOS route validator unexpectedly accepted a Mach-O patch artifact" >&2
  exit 70
fi

echo "iOS interpreter route validator smoke test passed"

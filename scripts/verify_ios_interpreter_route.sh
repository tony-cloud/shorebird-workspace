#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IOS_ENGINE_DIR="${IOS_ENGINE_DIR:-$ROOT/flutter/engine/src/out/ios_release}"
HOST_ENGINE_DIR="${HOST_ENGINE_DIR:-$ROOT/flutter/engine/src/out/host_release_arm64}"
IOS_APP_BUNDLE="${IOS_APP_BUNDLE:-}"
IOS_IPA="${IOS_IPA:-}"
IOS_PATCH_ARTIFACT="${IOS_PATCH_ARTIFACT:-}"
APP_STORE_STRICT="${APP_STORE_STRICT:-0}"

CHECKED_APP_BUNDLE=""
CHECKED_IPA=""
CHECKED_PATCH_ARTIFACT=""
ENTITLEMENTS_CHECKED=0
APP_STORE_STRICT_CHECKED=0
NO_BUNDLED_KEY_CHECKED=0
CLEANUP_DIR=""

cleanup() {
  if [[ -n "$CLEANUP_DIR" && -d "$CLEANUP_DIR" ]]; then
    rm -rf "$CLEANUP_DIR"
  fi
}
trap cleanup EXIT

fail() {
  echo "error: $*" >&2
  exit 1
}

read_gn_value() {
  local file="$1"
  local key="$2"
  awk -v key="$key" '
    $1 == key && $2 == "=" {
      value = $0
      sub("^[[:space:]]*" key "[[:space:]]*=[[:space:]]*", "", value)
      sub("[[:space:]]*$", "", value)
      found = 1
    }
    END {
      if (!found) {
        exit 1
      }
      print value
    }
  ' "$file"
}

require_gn_value() {
  local file="$1"
  local key="$2"
  local expected="$3"
  local actual
  if ! actual="$(read_gn_value "$file" "$key")"; then
    fail "$file is missing required GN arg $key"
  fi
  if [[ "$actual" != "$expected" ]]; then
    fail "$file has $key = $actual; expected $expected"
  fi
}

verify_ios_engine_args() {
  local args_file="$IOS_ENGINE_DIR/args.gn"
  [[ -f "$args_file" ]] || fail "missing iOS engine args: $args_file"

  require_gn_value "$args_file" target_os '"ios"'
  require_gn_value "$args_file" dart_dynamic_modules false
  require_gn_value "$args_file" dart_enable_aot_patching true
  require_gn_value "$args_file" dart_enable_shorebird_interpreter true
  require_gn_value "$args_file" shorebird_use_interpreter true
  require_gn_value "$args_file" shorebird_enable_aot_patching false
  require_gn_value "$args_file" flutter_prebuilt_dart_sdk false
}

verify_host_engine_args() {
  local args_file="$HOST_ENGINE_DIR/args.gn"
  [[ -f "$args_file" ]] || fail "missing host engine args: $args_file"

  require_gn_value "$args_file" target_os '"mac"'
  require_gn_value "$args_file" dart_dynamic_modules false
  require_gn_value "$args_file" dart_enable_aot_patching true
  require_gn_value "$args_file" dart_enable_shorebird_interpreter true
  require_gn_value "$args_file" shorebird_use_interpreter true
  require_gn_value "$args_file" flutter_prebuilt_dart_sdk false
}

app_bundle_from_ipa() {
  [[ -f "$IOS_IPA" ]] || fail "missing iOS IPA: $IOS_IPA"
  command -v unzip >/dev/null 2>&1 || fail "unzip is required to inspect IOS_IPA"

  CLEANUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ios-route.XXXXXX")"
  unzip -qq "$IOS_IPA" -d "$CLEANUP_DIR"

  local payload_dir="$CLEANUP_DIR/Payload"
  [[ -d "$payload_dir" ]] || fail "IPA does not contain a Payload directory"

  local app_bundle
  app_bundle="$(find "$payload_dir" -maxdepth 1 -type d -name "*.app" -print -quit)"
  [[ -n "$app_bundle" ]] || fail "IPA does not contain a Payload/*.app bundle"
  IOS_APP_BUNDLE="$app_bundle"
  CHECKED_IPA="$IOS_IPA"
}

verify_entitlements() {
  local app_bundle="$1"
  local entitlements

  if ! command -v codesign >/dev/null 2>&1; then
    if [[ "$APP_STORE_STRICT" == "1" ]]; then
      fail "codesign is required for APP_STORE_STRICT=1 entitlement checks"
    fi
    echo "warning: codesign not found; skipping entitlement checks" >&2
    return 0
  fi

  if ! entitlements="$(codesign -d --entitlements :- "$app_bundle" 2>/dev/null)"; then
    if [[ "$APP_STORE_STRICT" == "1" ]]; then
      fail "failed to read app entitlements with codesign: $app_bundle"
    fi
    echo "warning: failed to read app entitlements; skipping entitlement checks" >&2
    return 0
  fi

  local forbidden_entitlements=(
    "com.apple.security.cs.allow-jit"
    "com.apple.security.cs.allow-unsigned-executable-memory"
    "com.apple.security.cs.disable-executable-page-protection"
    "com.apple.security.cs.dynamic-codesigning"
  )
  local entitlement
  for entitlement in "${forbidden_entitlements[@]}"; do
    if grep -Fq "<key>$entitlement</key>" <<<"$entitlements"; then
      fail "app entitlement enables executable-memory behavior: $entitlement"
    fi
  done

  ENTITLEMENTS_CHECKED=1

  if [[ "$APP_STORE_STRICT" == "1" ]]; then
    local compact_entitlements
    compact_entitlements="$(printf '%s' "$entitlements" | tr -d '\n\r\t ')"
    if grep -Fq "<key>get-task-allow</key><true/>" <<<"$compact_entitlements"; then
      fail "APP_STORE_STRICT=1 rejects development entitlement get-task-allow=true"
    fi
    APP_STORE_STRICT_CHECKED=1
  fi
}

verify_no_bundled_patch_key() {
  local app_bundle="$1"
  [[ "$APP_STORE_STRICT" == "1" ]] || return 0

  local shorebird_yaml
  shorebird_yaml="$(
    find "$app_bundle" -name "shorebird.yaml" -type f -print -quit
  )"
  if [[ -z "$shorebird_yaml" ]]; then
    return 0
  fi

  if grep -Eq '^[[:space:]]*aot_patch_key_hex[[:space:]]*:' "$shorebird_yaml"; then
    fail "APP_STORE_STRICT=1 rejects bundled aot_patch_key_hex in $shorebird_yaml"
  fi
  NO_BUNDLED_KEY_CHECKED=1
}

verify_app_bundle() {
  if [[ -n "$IOS_APP_BUNDLE" && -n "$IOS_IPA" ]]; then
    fail "set only one of IOS_APP_BUNDLE or IOS_IPA"
  fi
  if [[ -n "$IOS_IPA" ]]; then
    app_bundle_from_ipa
  fi
  [[ -n "$IOS_APP_BUNDLE" ]] || return 0
  [[ -d "$IOS_APP_BUNDLE" ]] || fail "missing iOS app bundle: $IOS_APP_BUNDLE"
  CHECKED_APP_BUNDLE="$IOS_APP_BUNDLE"

  local bundled_patch
  bundled_patch="$(
    find "$IOS_APP_BUNDLE" \
      \( -path "*/shorebird_updater/*" -o -name "dlc.vmcode" \) \
      -print -quit
  )"
  if [[ -n "$bundled_patch" ]]; then
    fail "reviewed app bundle contains a seeded patch payload: $bundled_patch"
  fi

  verify_entitlements "$IOS_APP_BUNDLE"
  verify_no_bundled_patch_key "$IOS_APP_BUNDLE"
}

file_magic_hex() {
  od -An -tx1 -N4 "$1" | tr -d ' \n'
}

verify_patch_artifact() {
  [[ -n "$IOS_PATCH_ARTIFACT" ]] || return 0
  [[ -f "$IOS_PATCH_ARTIFACT" ]] || fail "missing iOS patch artifact: $IOS_PATCH_ARTIFACT"

  local magic
  magic="$(file_magic_hex "$IOS_PATCH_ARTIFACT")"
  case "$magic" in
    feedface|cefaedfe|feedfacf|cffaedfe)
      fail "iOS interpreter route must not use a Mach-O native patch artifact: $IOS_PATCH_ARTIFACT"
      ;;
    7f454c46)
      fail "iOS interpreter route must not use an ELF native patch artifact: $IOS_PATCH_ARTIFACT"
      ;;
  esac

  local python_bin
  python_bin=python3
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    python_bin=python
  fi
  command -v "$python_bin" >/dev/null 2>&1 ||
    fail "python3 or python is required to inspect IOS_PATCH_ARTIFACT"

  "$python_bin" - "$IOS_PATCH_ARTIFACT" <<'PY'
import base64
import binascii
import json
import re
import sys

path = sys.argv[1]

try:
    with open(path, encoding="utf-8") as file:
        artifact = json.load(file)
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit(
        "iOS patch artifact must be the encrypted open JSON wrapper, "
        f"not a raw native/code payload: {error}"
    )

if not isinstance(artifact, dict):
    raise SystemExit("iOS patch artifact JSON must be an object")

metadata = artifact.get("metadata")
if not isinstance(metadata, dict):
    raise SystemExit("iOS patch artifact must contain metadata object")


def require(mapping, key, expected, scope):
    actual = mapping.get(key)
    if actual != expected:
        raise SystemExit(
            f"iOS patch artifact {scope}.{key} is {actual!r}; "
            f"expected {expected!r}"
        )


require(artifact, "format", "open-aot-vmcode-encrypted-v1", "artifact")
runtime_mode = metadata.get("runtime_mode")
if runtime_mode in {"dart-dynamic-modules", "dynamic-modules"}:
    raise SystemExit("iOS patch artifact uses DART_DYNAMIC_MODULES runtime mode")
require(metadata, "runtime_mode", "dart-bytecode-interpreter", "metadata")
require(metadata, "target_os", "ios", "metadata")
require(metadata, "target_arch", "arm64", "metadata")
require(artifact, "payload_kind", "full-snapshot", "artifact")

encryption = artifact.get("encryption")
if not isinstance(encryption, dict):
    raise SystemExit("iOS patch artifact must contain encryption object")
require(encryption, "algorithm", "AES-256-GCM", "encryption")


def require_base64(mapping, key, scope):
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"iOS patch artifact {scope}.{key} must be non-empty")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SystemExit(
            f"iOS patch artifact {scope}.{key} is not valid base64: {error}"
        )
    if not decoded:
        raise SystemExit(f"iOS patch artifact {scope}.{key} decodes to empty bytes")
    return decoded


require_base64(artifact, "encrypted_payload_base64", "artifact")
require_base64(encryption, "nonce_base64", "encryption")
require_base64(encryption, "tag_base64", "encryption")

key_id = encryption.get("key_id")
if not isinstance(key_id, str) or not key_id:
    raise SystemExit("iOS patch artifact encryption.key_id must be non-empty")

hex_pattern = re.compile(r"^[0-9a-f]{64}$")
for scope, mapping, key in (
    ("artifact", artifact, "payload_sha256"),
    ("encryption", encryption, "aad_sha256"),
):
    value = mapping.get(key)
    if not isinstance(value, str) or not hex_pattern.fullmatch(value):
        raise SystemExit(
            f"iOS patch artifact {scope}.{key} must be a lowercase SHA-256 hex digest"
        )

reconstructed_size = artifact.get("reconstructed_size")
if reconstructed_size is not None:
    if not isinstance(reconstructed_size, int) or reconstructed_size <= 0:
        raise SystemExit(
            "iOS patch artifact reconstructed_size must be a positive integer"
        )
PY

  CHECKED_PATCH_ARTIFACT="$IOS_PATCH_ARTIFACT"
}

verify_ios_engine_args
verify_host_engine_args
verify_app_bundle
verify_patch_artifact

cat <<EOF
iOS interpreter route verified:
  iOS engine args:  $IOS_ENGINE_DIR/args.gn
  host engine args: $HOST_ENGINE_DIR/args.gn
  dynamic modules:  disabled
  iOS patch mode:   Dart bytecode interpreter
EOF

if [[ -n "$CHECKED_IPA" ]]; then
  echo "  IPA:              inspected Payload/*.app"
fi
if [[ -n "$CHECKED_APP_BUNDLE" ]]; then
  echo "  app bundle:       no bundled shorebird patch payload"
fi
if [[ "$ENTITLEMENTS_CHECKED" == "1" ]]; then
  echo "  entitlements:     no JIT/unsigned-executable-memory entitlement"
fi
if [[ "$APP_STORE_STRICT_CHECKED" == "1" ]]; then
  echo "  App Store strict: get-task-allow is not true"
fi
if [[ "$NO_BUNDLED_KEY_CHECKED" == "1" ]]; then
  echo "  key material:     no bundled aot_patch_key_hex"
fi
if [[ -n "$CHECKED_PATCH_ARTIFACT" ]]; then
  echo "  patch artifact:   encrypted interpreter full-snapshot for ios/arm64"
fi

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
      print value
      found = 1
      exit
    }
    END {
      if (!found) {
        exit 1
      }
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
  require_gn_value "$args_file" dart_enable_shorebird_interpreter true
  require_gn_value "$args_file" shorebird_use_interpreter true
  require_gn_value "$args_file" shorebird_enable_aot_patching false
}

verify_host_engine_args() {
  local args_file="$HOST_ENGINE_DIR/args.gn"
  [[ -f "$args_file" ]] || fail "missing host engine args: $args_file"

  require_gn_value "$args_file" target_os '"mac"'
  require_gn_value "$args_file" dart_dynamic_modules false
  require_gn_value "$args_file" dart_enable_shorebird_interpreter true
  require_gn_value "$args_file" shorebird_use_interpreter true
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

  local compact_json
  compact_json="$(LC_ALL=C tr -d '[:space:]' < "$IOS_PATCH_ARTIFACT")"

  if [[ "$compact_json" != \{* ]]; then
    fail "iOS patch artifact must be the encrypted open JSON wrapper, not a raw native/code payload"
  fi
  if ! grep -Fq '"format":"open-aot-vmcode-encrypted-v1"' <<<"$compact_json"; then
    fail "iOS patch artifact is not an open encrypted VM code artifact"
  fi
  if grep -Fq '"runtime_mode":"dart-dynamic-modules"' <<<"$compact_json" ||
     grep -Fq '"runtime_mode":"dynamic-modules"' <<<"$compact_json"; then
    fail "iOS patch artifact uses DART_DYNAMIC_MODULES runtime mode"
  fi
  if ! grep -Fq '"runtime_mode":"dart-bytecode-interpreter"' <<<"$compact_json"; then
    fail "iOS patch artifact must declare runtime_mode dart-bytecode-interpreter"
  fi
  if ! grep -Fq '"target_os":"ios"' <<<"$compact_json"; then
    fail "iOS patch artifact must target iOS"
  fi
  if ! grep -Fq '"target_arch":"arm64"' <<<"$compact_json"; then
    fail "iOS patch artifact must target arm64"
  fi
  if ! grep -Fq '"payload_kind":"full-snapshot"' <<<"$compact_json"; then
    fail "current iOS interpreter mapper requires payload_kind full-snapshot"
  fi

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
if [[ -n "$CHECKED_PATCH_ARTIFACT" ]]; then
  echo "  patch artifact:   encrypted interpreter full-snapshot for ios/arm64"
fi

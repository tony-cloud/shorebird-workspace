#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-}"
INCLUDE_ENGINE_DEPS="${INCLUDE_ENGINE_DEPS:-0}"

case "$PLATFORM" in
  linux)
    if [[ "$INCLUDE_ENGINE_DEPS" == "1" ]]; then
      TARGET_OS='["linux", "android"]'
      DART_DOWNLOAD_ANDROID_DEPS="True"
    else
      TARGET_OS='["linux"]'
      DART_DOWNLOAD_ANDROID_DEPS="False"
    fi
    ;;
  macos)
    if [[ "$INCLUDE_ENGINE_DEPS" == "1" ]]; then
      TARGET_OS='["mac", "ios", "android"]'
      DART_DOWNLOAD_ANDROID_DEPS="True"
    else
      TARGET_OS='["mac", "ios"]'
      DART_DOWNLOAD_ANDROID_DEPS="False"
    fi
    ;;
  *)
    echo "usage: $0 <linux|macos>" >&2
    exit 64
    ;;
esac

cat > "$ROOT/.gclient" <<EOF
solutions = [
  {
    "name": "dart-sdk",
    "url": "https://github.com/tony-cloud/dart-sdk.git",
    "deps_file": "DEPS",
    "managed": False,
    "custom_deps": {},
    "custom_vars": {
      "dart_root": "dart-sdk",
      "download_android_deps": $DART_DOWNLOAD_ANDROID_DEPS,
      "checkout_javascript_engines": False,
      "checkout_benchmarks_internal": False,
      "checkout_flute": False,
    },
  },
]
target_os = $TARGET_OS
EOF

echo "Wrote $ROOT/.gclient for $PLATFORM with target_os = $TARGET_OS"

if [[ "$INCLUDE_ENGINE_DEPS" == "1" ]]; then
  cat > "$ROOT/flutter/.gclient" <<EOF
solutions = [
  {
    "name": ".",
    "url": "https://github.com/tony-cloud/flutter.git",
    "deps_file": "DEPS",
    "managed": False,
    "custom_deps": {
      "engine/src/flutter/third_party/dart": None,
      "engine/src/flutter/third_party/dart/third_party/binaryen/src": None,
      "engine/src/flutter/third_party/dart/third_party/devtools": None,
      "engine/src/flutter/third_party/dart/third_party/perfetto/src": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/core": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/dart_style": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/dartdoc": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/ecosystem": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/http": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/i18n": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/leak_tracker": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/native": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/protobuf": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/pub": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/shelf": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/sync_http": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/tar": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/test": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/tools": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/vector_math": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/web": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/webdev": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/webdriver": None,
      "engine/src/flutter/third_party/dart/third_party/pkg/webkit_inspection_protocol": None,
      "engine/src/flutter/third_party/dart/tools/sdks/dart-sdk": None,
      "engine/src/flutter/third_party/updater": None,
    },
    "custom_vars": {
      "download_dart_sdk": False,
      "download_android_deps": True,
      "download_emsdk": True,
      "setup_githooks": False,
      "use_rbe": False,
    },
  },
]
target_os = $TARGET_OS
EOF
  echo "Wrote $ROOT/flutter/.gclient for Flutter engine dependencies"
else
  rm -f "$ROOT/flutter/.gclient"
fi

echo "Flutter engine dependency sync: $INCLUDE_ENGINE_DEPS"

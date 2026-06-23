#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-}"

case "$PLATFORM" in
  linux)
    TARGET_OS='["linux"]'
    ;;
  macos)
    TARGET_OS='["mac", "ios"]'
    ;;
  *)
    echo "usage: $0 <linux|macos>" >&2
    exit 64
    ;;
esac

cat > "$ROOT/.gclient" <<EOF
solutions = [
  {
    "name": "dart-sdk-new",
    "url": "https://git.tonycloud.org/dart-lang/sdk.git",
    "deps_file": "DEPS",
    "managed": False,
    "custom_deps": {},
    "custom_vars": {
      "dart_root": "dart-sdk-new",
      "download_android_deps": False,
      "checkout_javascript_engines": False,
      "checkout_benchmarks_internal": False,
      "checkout_flute": False,
    },
  },
]
target_os = $TARGET_OS
EOF

echo "Wrote $ROOT/.gclient for $PLATFORM with target_os = $TARGET_OS"

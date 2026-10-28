#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${DART_BIN:-}" ]]; then
  for candidate in \
    "$ROOT/dart-sdk/tools/sdks/dart-sdk/bin/dart" \
    "$ROOT/dart-sdk/xcodebuild/ReleaseARM64/dart-sdk/bin/dart" \
    "$ROOT/flutter/bin/cache/dart-sdk/bin/dart"; do
    if [[ -x "$candidate" ]]; then
      DART_BIN="$candidate"
      break
    fi
  done
fi
DART_BIN="${DART_BIN:-dart}"
GO_BIN="${GO_BIN:-go}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/shorebird-server-client-smoke.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

if [[ ! -f "$ROOT/shorebird/.dart_tool/package_config.json" ]]; then
  (cd "$ROOT/shorebird" && "$DART_BIN" pub get)
fi
(cd "$ROOT/shorebird-server" && "$GO_BIN" build -o "$WORK_DIR/server" ./cmd/server)
"$DART_BIN" --packages="$ROOT/shorebird/.dart_tool/package_config.json" \
  "$ROOT/scripts/smoke_server_client.dart" "$WORK_DIR/server"

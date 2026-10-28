#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_root="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-tool-sdk.XXXXXX")"
trap 'rm -rf "$fixture_root"' EXIT
mkdir -p "$fixture_root/scripts" "$fixture_root/dart-sdk/pkg/front_end" \
  "$fixture_root/dart-sdk/tools/sdks/dart-sdk/bin"
cp "$ROOT/scripts/verify_dart_tool_sdk.sh" "$fixture_root/scripts/"
printf 'environment:\n  sdk: ^3.14.0\n' > "$fixture_root/dart-sdk/pkg/front_end/pubspec.yaml"

write_dart() {
  local name="$1"
  local version="$2"
  printf '#!/usr/bin/env bash\nprintf "Dart SDK version: %s\\n"\n' "$version" \
    > "$fixture_root/dart-sdk/tools/sdks/dart-sdk/bin/$name"
  chmod +x "$fixture_root/dart-sdk/tools/sdks/dart-sdk/bin/$name"
}
write_dart dart 3.14.0
"$fixture_root/scripts/verify_dart_tool_sdk.sh" >/dev/null
rm "$fixture_root/dart-sdk/tools/sdks/dart-sdk/bin/dart"
write_dart dart.exe 3.14.0
"$fixture_root/scripts/verify_dart_tool_sdk.sh" >/dev/null
write_dart dart.exe 3.13.0
if "$fixture_root/scripts/verify_dart_tool_sdk.sh" >"$fixture_root/version.log" 2>&1; then
  echo 'unexpectedly accepted a mismatched Windows Dart tool SDK' >&2
  exit 1
fi
grep -q 'does not satisfy front_end SDK constraint' "$fixture_root/version.log"
rm "$fixture_root/dart-sdk/tools/sdks/dart-sdk/bin/dart.exe"
if "$fixture_root/scripts/verify_dart_tool_sdk.sh" >"$fixture_root/missing.log" 2>&1; then
  echo 'unexpectedly accepted a missing Dart tool SDK' >&2
  exit 1
fi
grep -q 'missing executable Dart tool SDK' "$fixture_root/missing.log"
echo 'Dart tool SDK validator smoke test passed'

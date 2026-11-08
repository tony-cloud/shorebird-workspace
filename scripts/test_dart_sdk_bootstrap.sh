#!/usr/bin/env bash
# Clean-cache tests of the verified custom Dart SDK bootstrap
# (flutter/bin/internal/update_dart_sdk.sh + shorebird_dart_sdk.sh), against a
# loopback HTTP server. Covers: immutable pinned install, offline reuse,
# pre-seeded archive reuse, digest/size mismatch, unpublished and mismatched
# locks, conflicting overrides, FLUTTER_STORAGE_BASE_URL never serving the
# custom SDK, mutable "latest" URLs, legacy-mirror compatibility, and local
# provisioning. When pwsh is available the same matrix runs against
# update_dart_sdk.ps1 (see test_dart_sdk_bootstrap.ps1).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dart-sdk-bootstrap.XXXXXX")"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

ENGINE=0123456789abcdef0123456789abcdef01234567
DART=89abcdef0123456789abcdef0123456789abcdef
PASSED=0

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { PASSED=$((PASSED + 1)); echo "ok - $*"; }

case "$(uname -s)" in
  Darwin)
    if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == 1 ]]; then ZIP=dart-sdk-darwin-arm64.zip; else ZIP=dart-sdk-darwin-x64.zip; fi ;;
  Linux)
    case "$(uname -m)" in x86_64) ZIP=dart-sdk-linux-x64.zip ;; *) ZIP=dart-sdk-linux-arm64.zip ;; esac ;;
  *) echo "skipping: unsupported host $(uname -s)"; exit 0 ;;
esac

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d ' ' -f 1; else shasum -a 256 "$1" | cut -d ' ' -f 1; fi; }

# Archives: a good SDK, and a tampered one of the same size.
mkdir -p "$WORK/src/dart-sdk/bin" "$WORK/www/release" "$WORK/www/mirror/flutter_infra_release/flutter/$ENGINE" \
  "$WORK/www/store/shorebird/flutter_infra_release/flutter/$ENGINE" "$WORK/www/official/flutter_infra_release/flutter/$ENGINE"
printf '#!/bin/sh\necho custom-dart %s\n' "$DART" > "$WORK/src/dart-sdk/bin/dart"
chmod +x "$WORK/src/dart-sdk/bin/dart"
(cd "$WORK/src" && zip -q -r "$WORK/good.zip" dart-sdk)
sed "s/custom-dart/stock-dart!/" "$WORK/src/dart-sdk/bin/dart" > "$WORK/src/dart-sdk/bin/dart.t" && mv "$WORK/src/dart-sdk/bin/dart.t" "$WORK/src/dart-sdk/bin/dart"
chmod +x "$WORK/src/dart-sdk/bin/dart"
(cd "$WORK/src" && zip -q -r "$WORK/evil.zip" dart-sdk)
GOOD_SHA="$(sha256 "$WORK/good.zip")"
GOOD_SIZE="$(wc -c < "$WORK/good.zip" | tr -d '[:space:]')"
cp "$WORK/good.zip" "$WORK/www/release/$ZIP"
cp "$WORK/good.zip" "$WORK/www/store/shorebird/flutter_infra_release/flutter/$ENGINE/$ZIP"
cp "$WORK/good.zip" "$WORK/www/mirror/flutter_infra_release/flutter/$ENGINE/$ZIP"
# An "official" origin serving a stock SDK at the custom route must never be used.
cp "$WORK/evil.zip" "$WORK/www/official/flutter_infra_release/flutter/$ENGINE/$ZIP"
cp "$WORK/evil.zip" "$WORK/www/release/evil-$ZIP"

cat > "$WORK/server.py" <<'PY'
import http.server, sys, os
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        with open(os.environ['REQUEST_LOG'], 'a') as log:
            log.write(self.path + '\n')
os.chdir(sys.argv[1])
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
PY
export REQUEST_LOG="$WORK/requests.log"
: > "$REQUEST_LOG"
python3 -u "$WORK/server.py" "$WORK/www" > "$WORK/port" 2>/dev/null &
SERVER_PID=$!
for _ in $(seq 1 50); do [[ -s "$WORK/port" ]] && break; sleep 0.1; done
PORT="$(cat "$WORK/port")"
[[ -n "$PORT" ]] || fail "loopback server did not start"
BASE="http://127.0.0.1:$PORT"

# A minimal FLUTTER_ROOT: the two bootstrap scripts, DEPS, engine stamps, lock.
new_root() {
  local root="$WORK/flutter-$1"
  rm -rf "$root"
  mkdir -p "$root/bin/internal" "$root/bin/cache"
  cp "$ROOT/flutter/bin/internal/update_dart_sdk.sh" "$ROOT/flutter/bin/internal/shorebird_dart_sdk.sh" "$root/bin/internal/"
  printf 'vars = {\n  "dart_sdk_revision": "%s",\n}\n' "$DART" > "$root/DEPS"
  echo "$ENGINE" > "$root/bin/cache/engine.stamp"
  echo "" > "$root/bin/cache/engine.realm"
  echo "$root"
}

write_lock() { # root base_url sha size [engine] [dart]
  {
    echo '# test lock'
    echo 'schema open-shorebird.dart-sdk-lock'
    echo 'contract_version 1'
    echo "engine_revision ${5:-$ENGINE}"
    echo "dart_sdk_revision ${6:-$DART}"
    if [[ -n "$2" ]]; then
      echo "base_url $2"
      echo "asset $ZIP $3 $4"
    fi
  } > "$1/bin/internal/shorebird_dart_sdk.pins"
}

# Runs the bootstrap with a sanitized environment plus the given assignments.
run() { # root expected_status [VAR=value ...]
  local root="$1" expected="$2"
  shift 2
  set +e
  env -i PATH="$PATH" HOME="$WORK" "$@" bash "$root/bin/internal/update_dart_sdk.sh" > "$WORK/out" 2>&1
  local status=$?
  set -e
  if [[ "$status" != "$expected" ]]; then
    cat "$WORK/out" >&2
    fail "expected exit $expected, got $status"
  fi
}
expect_output() { grep -Fq -- "$1" "$WORK/out" || { cat "$WORK/out" >&2; fail "output lacks: $1"; }; }
installed() { "$1/bin/cache/dart-sdk/bin/dart"; }
requests_since() { tail -n "+$(( $1 + 1 ))" "$REQUEST_LOG"; }
mark() { wc -l < "$REQUEST_LOG" | tr -d '[:space:]'; }

# 1. Clean cache, published lock: immutable URL, verified, identity recorded.
R="$(new_root published)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE"
m="$(mark)"
run "$R" 0
[[ "$(installed "$R")" == "custom-dart $DART" ]] || fail "wrong SDK installed"
[[ "$(cat "$R/bin/cache/engine-dart-sdk.identity")" == "$ENGINE $DART $ZIP $GOOD_SHA" ]] || fail "identity not recorded"
[[ "$(cat "$R/bin/cache/engine-dart-sdk.stamp")" == "$ENGINE" ]] || fail "stamp not written"
requests_since "$m" | grep -qx "/release/$ZIP" || fail "did not fetch the lock's immutable URL"
[[ ! -e "$R/bin/cache/$GOOD_SHA-$ZIP" ]] || fail "archive left behind"
pass "clean bootstrap installs the lock-pinned archive and records its identity"

# 2. Offline reuse: identity matches, no network at all.
m="$(mark)"
run "$R" 0 SHOREBIRD_DART_SDK_RELEASE_BASE_URL=http://127.0.0.1:9/unreachable
[[ "$(mark)" == "$m" ]] || fail "network used for a verified, current install"
pass "a current verified install is reused offline"

# 3. A new pin for the same engine re-installs (cache keyed by digest).
write_lock "$R" "$BASE/release" "$(sha256 "$WORK/evil.zip")" "$GOOD_SIZE"
run "$R" 2
expect_output "does not match its pinned digest"
pass "a changed pin re-verifies; a release serving other bytes is refused"
[[ ! -e "$R/bin/cache/dart-sdk.partial" && ! -e "$R/bin/cache/$(sha256 "$WORK/evil.zip")-$ZIP.partial" ]] || fail "partial left"

# 4. Digest mismatch on a clean cache: nothing unpacked or run.
R="$(new_root tampered)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE"
cp "$WORK/evil.zip" "$WORK/www/release/$ZIP.tmp" && mv "$WORK/www/release/$ZIP" "$WORK/good.keep" && mv "$WORK/www/release/$ZIP.tmp" "$WORK/www/release/$ZIP"
run "$R" 2
expect_output "does not match its pinned digest"
[[ ! -e "$R/bin/cache/dart-sdk/bin/dart" ]] || fail "tampered SDK was unpacked"
[[ ! -e "$R/bin/cache/engine-dart-sdk.stamp" ]] || fail "stamp written for a refused SDK"
mv "$WORK/good.keep" "$WORK/www/release/$ZIP"
pass "digest mismatch is refused before unpacking"

# 5. Size mismatch.
R="$(new_root size)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$((GOOD_SIZE + 1))"
run "$R" 2
expect_output "does not match the pinned $((GOOD_SIZE + 1))"
pass "size mismatch is refused"

# 6. Pre-seeded verified archive, no server reachable: offline install.
R="$(new_root seeded)"
write_lock "$R" "http://127.0.0.1:9/unreachable" "$GOOD_SHA" "$GOOD_SIZE"
cp "$WORK/good.zip" "$R/bin/cache/$GOOD_SHA-$ZIP"
run "$R" 0
expect_output "Reusing the verified Dart SDK archive"
[[ "$(installed "$R")" == "custom-dart $DART" ]] || fail "seeded SDK not installed"
pass "a pre-seeded verified archive installs offline"

# 7. A corrupt pre-seeded archive is discarded, then the (offline) fetch fails.
R="$(new_root seeded-bad)"
write_lock "$R" "http://127.0.0.1:9/unreachable" "$GOOD_SHA" "$GOOD_SIZE"
cp "$WORK/evil.zip" "$R/bin/cache/$GOOD_SHA-$ZIP"
run "$R" 1
[[ ! -e "$R/bin/cache/$GOOD_SHA-$ZIP" ]] || fail "corrupt cached archive kept"
[[ ! -e "$R/bin/cache/dart-sdk/bin/dart" ]] || fail "corrupt cached archive unpacked"
pass "a corrupt cached archive is never used"

# 8. Unpublished lock: precise error, no request anywhere, even with an
#    official origin configured that serves a stock SDK at the custom route.
R="$(new_root unpublished)"
write_lock "$R" "" "" ""
m="$(mark)"
run "$R" 2 FLUTTER_STORAGE_BASE_URL="$BASE/official"
expect_output "shorebird_dart_sdk.pins is unpublished"
expect_output "a stock Dart SDK is never substituted"
[[ "$(mark)" == "$m" ]] || fail "unpublished lock still fetched something"
pass "unpublished lock fails precisely; FLUTTER_STORAGE_BASE_URL never serves the custom SDK"

# 9. Lock for another identity (Dart SDK revision drift).
R="$(new_root drift)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE" "$ENGINE" "fedcba9876543210fedcba9876543210fedcba98"
run "$R" 2
expect_output "but this checkout pins engine $ENGINE and Dart SDK $DART"
pass "a lock for another Dart SDK revision is refused"

# 10. Custom store (SHOREBIRD_ARTIFACT_BASE_URL) with the lock's digest.
R="$(new_root store)"
write_lock "$R" "https://release.invalid/never-used" "$GOOD_SHA" "$GOOD_SIZE"
m="$(mark)"
run "$R" 0 SHOREBIRD_ARTIFACT_BASE_URL="$BASE/store/"
requests_since "$m" | grep -qx "/store/shorebird/flutter_infra_release/flutter/$ENGINE/$ZIP" || fail "custom store not used"
pass "SHOREBIRD_ARTIFACT_BASE_URL serves the custom SDK, verified by the lock"

# 11. Explicit digest pin for an unpublished lock.
R="$(new_root explicit)"
write_lock "$R" "" "" ""
run "$R" 0 SHOREBIRD_ARTIFACT_BASE_URL="$BASE/store" SHOREBIRD_DART_SDK_SHA256="$GOOD_SHA"
[[ "$(cut -d ' ' -f 4 "$R/bin/cache/engine-dart-sdk.identity")" == "$GOOD_SHA" ]] || fail "explicit pin identity"
run "$R" 2 SHOREBIRD_DART_SDK_SHA256=NOTHEX
expect_output "64 lowercase hex"
pass "an explicit SHOREBIRD_DART_SDK_SHA256 pin verifies an operator store"

# 12. Mutable URLs are refused.
R="$(new_root mutable)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE"
run "$R" 2 SHOREBIRD_DART_SDK_RELEASE_BASE_URL="https://github.com/o/r/releases/latest/download"
expect_output 'mutable "latest" release'
write_lock "$R" "https://github.com/o/r/releases/latest/download" "$GOOD_SHA" "$GOOD_SIZE"
run "$R" 2
expect_output 'mutable "latest" release'
pass "mutable latest-release URLs are refused"

# 13. Conflicting overrides.
R="$(new_root conflict)"
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE"
run "$R" 2 SHOREBIRD_DOWNLOAD_URL="$BASE/a" SHOREBIRD_FLUTTER_STORAGE_BASE_URL="$BASE/b"
expect_output "Conflicting artifact mirror overrides"
run "$R" 2 SHOREBIRD_ORIGIN_MODE=split SHOREBIRD_DOWNLOAD_URL="$BASE/mirror"
expect_output "SHOREBIRD_ORIGIN_MODE=split conflicts"
run "$R" 2 SHOREBIRD_DOWNLOAD_URL="$BASE/mirror" FLUTTER_STORAGE_BASE_URL="$BASE/official"
expect_output "Conflicting artifact origins"
run "$R" 2 SHOREBIRD_ORIGIN_MODE=everything
expect_output "Unknown SHOREBIRD_ORIGIN_MODE"
[[ ! -e "$R/bin/cache/dart-sdk" ]] || fail "conflict still installed"
pass "conflicting overrides are diagnosed before any download"

# 14. Legacy all-in-one mirror: pinned when possible, loud when not.
R="$(new_root legacy)"
write_lock "$R" "" "" ""
m="$(mark)"
run "$R" 0 SHOREBIRD_DOWNLOAD_URL="$BASE/mirror"
expect_output "UNVERIFIED Dart SDK from the legacy all-in-one mirror"
requests_since "$m" | grep -qx "/mirror/flutter_infra_release/flutter/$ENGINE/$ZIP" || fail "legacy mirror layout"
[[ "$(cut -d ' ' -f 4 "$R/bin/cache/engine-dart-sdk.identity")" == unverified ]] || fail "unverified identity"
run "$R" 0 SHOREBIRD_DOWNLOAD_URL="$BASE/mirror"
pass "legacy-mirror mode stays compatible and labels an unpinned SDK unverified"
# Publishing a pin replaces the unverified install on the next run.
write_lock "$R" "$BASE/release" "$GOOD_SHA" "$GOOD_SIZE"
m="$(mark)"
run "$R" 0
[[ "$(cut -d ' ' -f 4 "$R/bin/cache/engine-dart-sdk.identity")" == "$GOOD_SHA" ]] || fail "not re-verified"
requests_since "$m" | grep -qx "/release/$ZIP" || fail "pinned SDK not fetched"
pass "a published pin replaces an unverified install"
# The open proxy URL in FLUTTER_STORAGE_BASE_URL is the legacy alias.
R="$(new_root legacy-alias)"
write_lock "$R" "" "" ""
run "$R" 1 FLUTTER_STORAGE_BASE_URL=http://localhost:8080/download.flutter.io/ SHOREBIRD_DART_SDK_SHA256="$GOOD_SHA"
expect_output "localhost:8080/download.flutter.io/flutter_infra_release/flutter/$ENGINE/$ZIP"
pass "FLUTTER_STORAGE_BASE_URL naming the open proxy selects legacy-mirror mode"

# 15. A locally provisioned SDK (stamp without identity) is kept as before.
R="$(new_root local)"
write_lock "$R" "" "" ""
mkdir -p "$R/bin/cache/dart-sdk/bin"
printf '#!/bin/sh\necho local\n' > "$R/bin/cache/dart-sdk/bin/dart"; chmod +x "$R/bin/cache/dart-sdk/bin/dart"
echo "$ENGINE" > "$R/bin/cache/engine-dart-sdk.stamp"
run "$R" 0
[[ "$(installed "$R")" == local ]] || fail "local SDK replaced"
pass "a locally provisioned SDK is kept"

if command -v pwsh >/dev/null 2>&1; then
  pwsh -NoProfile -NonInteractive -File "$ROOT/scripts/test_dart_sdk_bootstrap.ps1" \
    -FlutterSource "$ROOT/flutter" -Work "$WORK/ps" -Www "$WORK/www" -BaseUrl "$BASE" -Engine "$ENGINE" -Dart "$DART" \
    -GoodZip "$WORK/good.zip" -EvilZip "$WORK/evil.zip" -RequestLog "$REQUEST_LOG"
  pass "PowerShell bootstrap matrix (test_dart_sdk_bootstrap.ps1)"
else
  echo "warning: pwsh not found; skipped the PowerShell bootstrap matrix" >&2
fi

echo "dart SDK bootstrap tests passed: $PASSED"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-sha256.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

ARTIFACT="$TMP_DIR/artifact.txt"
SIDECAR="$TMP_DIR/artifact.txt.sha256"
CUSTOM_SIDECAR="$TMP_DIR/custom.sha256"

printf 'open-shorebird\n' > "$ARTIFACT"

"$ROOT/scripts/write_sha256.sh" "$ARTIFACT"

EXPECTED_HASH="$("$PYTHON_BIN" - "$ARTIFACT" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
EXPECTED_LINE="$EXPECTED_HASH  artifact.txt"
ACTUAL_LINE="$(cat "$SIDECAR")"

if [[ "$ACTUAL_LINE" != "$EXPECTED_LINE" ]]; then
  echo "unexpected sha256 sidecar: $ACTUAL_LINE" >&2
  echo "expected: $EXPECTED_LINE" >&2
  exit 70
fi

"$ROOT/scripts/write_sha256.sh" "$ARTIFACT" "$CUSTOM_SIDECAR"
CUSTOM_LINE="$(cat "$CUSTOM_SIDECAR")"

if [[ "$CUSTOM_LINE" != "$EXPECTED_LINE" ]]; then
  echo "unexpected custom sha256 sidecar: $CUSTOM_LINE" >&2
  echo "expected: $EXPECTED_LINE" >&2
  exit 70
fi

if "$ROOT/scripts/write_sha256.sh" "$TMP_DIR/missing.txt" >/dev/null 2>&1; then
  echo "write_sha256.sh unexpectedly succeeded for a missing artifact" >&2
  exit 70
fi

echo "write_sha256.sh smoke test passed"

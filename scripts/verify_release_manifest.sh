#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-release-manifest.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

INPUT_DIR="$TMP_DIR/artifacts"
mkdir -p "$INPUT_DIR/cli-linux" "$INPUT_DIR/server-linux" "$INPUT_DIR/mirror-patch/artifacts/mirror"
printf 'cli archive\n' > "$INPUT_DIR/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
printf 'server archive\n' > "$INPUT_DIR/server-linux/shorebird-server-linux-amd64.tar.gz"
printf 'patch archive\n' > "$INPUT_DIR/mirror-patch/artifacts/mirror/patch-linux-x64.zip"

"$ROOT/scripts/write_sha256.sh" "$INPUT_DIR/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
"$ROOT/scripts/write_sha256.sh" "$INPUT_DIR/server-linux/shorebird-server-linux-amd64.tar.gz"
"$ROOT/scripts/write_sha256.sh" "$INPUT_DIR/mirror-patch/artifacts/mirror/patch-linux-x64.zip"

"$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
  "$INPUT_DIR" \
  --github-sha test-sha \
  --require 'cli-linux/*open-shorebird-cli-linux-x64.tar.gz' \
  --require 'server-linux/*shorebird-server-linux-amd64.tar.gz' \
  --require 'mirror-patch/*patch-linux-x64.zip' \
  --output "$TMP_DIR/release-manifest.json"

"$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
  --github-sha test-sha \
  "$INPUT_DIR" \
  "$TMP_DIR/release-manifest.json"

"$PYTHON_BIN" - "$TMP_DIR/release-manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
assert manifest["format_version"] == 1
assert manifest["github_sha"] == "test-sha"
assert manifest["artifact_count"] == 3
paths = {artifact["path"] for artifact in manifest["artifacts"]}
assert "cli-linux/open-shorebird-cli-linux-x64.tar.gz" in paths
assert "server-linux/shorebird-server-linux-amd64.tar.gz" in paths
assert "mirror-patch/artifacts/mirror/patch-linux-x64.zip" in paths
for artifact in manifest["artifacts"]:
    assert artifact["artifact_group"] in {
        "cli-linux",
        "server-linux",
        "mirror-patch",
    }
    assert artifact["filename"] == artifact["path"].split("/")[-1]
    assert len(artifact["sha256"]) == 64
    assert artifact["size"] > 0
    assert artifact["sidecar"].endswith(".sha256")
PY

TAMPERED_MANIFEST="$TMP_DIR/tampered-release-manifest.json"
"$PYTHON_BIN" - "$TMP_DIR/release-manifest.json" "$TAMPERED_MANIFEST" <<'PY'
import json
import sys

source, output = sys.argv[1:]
manifest = json.load(open(source, encoding="utf-8"))
manifest["artifacts"][0]["sha256"] = "0" * 64
json.dump(manifest, open(output, "w", encoding="utf-8"))
PY
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$INPUT_DIR" \
    "$TAMPERED_MANIFEST" >/dev/null 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted a tampered digest" >&2
  exit 70
fi

if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    --github-sha wrong-sha \
    "$INPUT_DIR" \
    "$TMP_DIR/release-manifest.json" >"$TMP_DIR/wrong-sha.log" 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted the wrong github_sha" >&2
  exit 70
fi
grep -q "github_sha is" "$TMP_DIR/wrong-sha.log"

UNLISTED_ARTIFACTS="$TMP_DIR/unlisted-artifacts"
cp -R "$INPUT_DIR" "$UNLISTED_ARTIFACTS"
printf 'extra artifact\n' > "$UNLISTED_ARTIFACTS/extra.tar.gz"
"$ROOT/scripts/write_sha256.sh" "$UNLISTED_ARTIFACTS/extra.tar.gz"
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$UNLISTED_ARTIFACTS" \
    "$TMP_DIR/release-manifest.json" >/dev/null 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted an unlisted artifact" >&2
  exit 70
fi

UNSAFE_MANIFEST="$TMP_DIR/unsafe-release-manifest.json"
"$PYTHON_BIN" - "$TMP_DIR/release-manifest.json" "$UNSAFE_MANIFEST" <<'PY'
import json
import sys

source, output = sys.argv[1:]
manifest = json.load(open(source, encoding="utf-8"))
manifest["artifacts"][0]["path"] = r"cli-linux\open-shorebird-cli-linux-x64.tar.gz"
manifest["artifacts"][0]["sidecar"] = (
    r"cli-linux\open-shorebird-cli-linux-x64.tar.gz.sha256"
)
json.dump(manifest, open(output, "w", encoding="utf-8"))
PY
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$INPUT_DIR" \
    "$UNSAFE_MANIFEST" >/dev/null 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted an unsafe artifact path" >&2
  exit 70
fi

BAD_PROVENANCE_MANIFEST="$TMP_DIR/bad-provenance-release-manifest.json"
"$PYTHON_BIN" - "$TMP_DIR/release-manifest.json" "$BAD_PROVENANCE_MANIFEST" <<'PY'
import json
import sys

source, output = sys.argv[1:]
manifest = json.load(open(source, encoding="utf-8"))
manifest["artifacts"][0]["artifact_group"] = "wrong-group"
manifest["artifacts"][1]["filename"] = "wrong-name.tar.gz"
json.dump(manifest, open(output, "w", encoding="utf-8"))
PY
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$INPUT_DIR" \
    "$BAD_PROVENANCE_MANIFEST" >/dev/null 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted bad provenance fields" >&2
  exit 70
fi

SYMLINK_LISTED_ARTIFACT="$TMP_DIR/symlink-listed-artifact"
cp -R "$INPUT_DIR" "$SYMLINK_LISTED_ARTIFACT"
rm "$SYMLINK_LISTED_ARTIFACT/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
ln -s "$INPUT_DIR/cli-linux/open-shorebird-cli-linux-x64.tar.gz" \
  "$SYMLINK_LISTED_ARTIFACT/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$SYMLINK_LISTED_ARTIFACT" \
    "$TMP_DIR/release-manifest.json" >/dev/null 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted a symlink artifact" >&2
  exit 70
fi

EMPTY_LISTED_ARTIFACT="$TMP_DIR/empty-listed-artifact"
cp -R "$INPUT_DIR" "$EMPTY_LISTED_ARTIFACT"
printf '' > "$EMPTY_LISTED_ARTIFACT/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
"$ROOT/scripts/write_sha256.sh" \
  "$EMPTY_LISTED_ARTIFACT/cli-linux/open-shorebird-cli-linux-x64.tar.gz"
EMPTY_LISTED_MANIFEST="$TMP_DIR/empty-listed-release-manifest.json"
"$PYTHON_BIN" - "$TMP_DIR/release-manifest.json" "$EMPTY_LISTED_MANIFEST" <<'PY'
import hashlib
import json
import sys

source, output = sys.argv[1:]
manifest = json.load(open(source, encoding="utf-8"))
empty_digest = hashlib.sha256(b"").hexdigest()
for artifact in manifest["artifacts"]:
    if artifact["path"] == "cli-linux/open-shorebird-cli-linux-x64.tar.gz":
        artifact["sha256"] = empty_digest
        artifact["size"] = 0
json.dump(manifest, open(output, "w", encoding="utf-8"))
PY
if "$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
    "$EMPTY_LISTED_ARTIFACT" \
    "$EMPTY_LISTED_MANIFEST" >"$TMP_DIR/empty-listed.log" 2>&1; then
  echo "validate_release_manifest.py unexpectedly accepted an empty artifact" >&2
  exit 70
fi
grep -q "empty artifacts are not allowed" "$TMP_DIR/empty-listed.log"

SYMLINK_INPUT="$TMP_DIR/symlink-input"
mkdir -p "$SYMLINK_INPUT"
printf 'symlink target\n' > "$SYMLINK_INPUT/target.tar.gz"
"$ROOT/scripts/write_sha256.sh" "$SYMLINK_INPUT/target.tar.gz"
ln -s target.tar.gz "$SYMLINK_INPUT/link.tar.gz"
if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$SYMLINK_INPUT" \
    --output "$TMP_DIR/symlink-input.json" >/dev/null 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted a symlink artifact" >&2
  exit 70
fi

MISSING_SIDECAR="$TMP_DIR/missing-sidecar"
mkdir -p "$MISSING_SIDECAR"
printf 'missing sidecar\n' > "$MISSING_SIDECAR/artifact.tar.gz"
if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$MISSING_SIDECAR" \
    --output "$TMP_DIR/missing.json" >/dev/null 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted a missing sidecar" >&2
  exit 70
fi

BAD_SIDECAR="$TMP_DIR/bad-sidecar"
mkdir -p "$BAD_SIDECAR"
printf 'bad sidecar\n' > "$BAD_SIDECAR/artifact.tar.gz"
printf '%064d  artifact.tar.gz\n' 0 > "$BAD_SIDECAR/artifact.tar.gz.sha256"
if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$BAD_SIDECAR" \
    --output "$TMP_DIR/bad.json" >/dev/null 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted a bad sidecar" >&2
  exit 70
fi

EMPTY_ARTIFACT="$TMP_DIR/empty-artifact"
mkdir -p "$EMPTY_ARTIFACT"
printf '' > "$EMPTY_ARTIFACT/artifact.tar.gz"
"$ROOT/scripts/write_sha256.sh" "$EMPTY_ARTIFACT/artifact.tar.gz"
if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$EMPTY_ARTIFACT" \
    --output "$TMP_DIR/empty.json" >"$TMP_DIR/empty-artifact.log" 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted an empty artifact" >&2
  exit 70
fi
grep -q "empty artifacts are not allowed" "$TMP_DIR/empty-artifact.log"

ORPHAN_SIDECAR="$TMP_DIR/orphan-sidecar"
mkdir -p "$ORPHAN_SIDECAR"
printf '%064d  missing.tar.gz\n' 0 > "$ORPHAN_SIDECAR/missing.tar.gz.sha256"
if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$ORPHAN_SIDECAR" \
    --output "$TMP_DIR/orphan.json" >/dev/null 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted an orphan sidecar" >&2
  exit 70
fi

if "$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
    "$INPUT_DIR" \
    --require 'missing-artifact/*.tar.gz' \
    --output "$TMP_DIR/missing-required.json" >/dev/null 2>&1; then
  echo "write_release_manifest.py unexpectedly accepted a missing required artifact" >&2
  exit 70
fi

echo "write_release_manifest.py smoke test passed"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-assemble.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

ENGINE_REVISION=engine123
INPUT_DIR="$TMP_DIR/downloaded-artifacts"
OUTPUT_DIR="$TMP_DIR/mirror"
mkdir -p "$INPUT_DIR"

mkdir -p "$INPUT_DIR/mirror-metadata/artifacts/mirror/shorebird/$ENGINE_REVISION"
cat > "$INPUT_DIR/mirror-metadata/artifacts/mirror/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" <<EOF
flutter_engine_revision: 'base-engine'
storage_bucket: 'shorebird'
artifact_overrides:
  - 'flutter_infra_release/flutter/\$engine/linux-x64-release/artifacts.zip'
EOF

mkdir -p "$INPUT_DIR/mirror-patch/artifacts/mirror/shorebird/$ENGINE_REVISION"
"$PYTHON_BIN" - "$INPUT_DIR/mirror-patch/artifacts/mirror/shorebird/$ENGINE_REVISION" <<'PY'
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1])
patch_zips = {
    "patch-linux-x64.zip": "patch",
    "patch-darwin-x64.zip": "patch",
    "patch-darwin-arm64.zip": "patch",
    "patch-windows-x64.zip": "patch.exe",
}
for zip_name, entry_name in patch_zips.items():
    with zipfile.ZipFile(root / zip_name, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, f"{zip_name}:{entry_name}\n")
PY

engine_staging="$TMP_DIR/linux-engine"
mkdir -p "$engine_staging/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release"
printf 'linux-engine-artifacts\n' \
  > "$engine_staging/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip"
tar -C "$engine_staging" -czf "$INPUT_DIR/linux-engine-x64.tar.gz" linux-engine

"$ROOT/scripts/assemble_artifact_mirror.sh" "$INPUT_DIR" "$OUTPUT_DIR"

test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml.sha256"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-linux-x64.zip"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-linux-x64.zip.sha256"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-darwin-x64.zip"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-darwin-x64.zip.sha256"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-darwin-arm64.zip"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-darwin-arm64.zip.sha256"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-windows-x64.zip"
test -f "$OUTPUT_DIR/shorebird/$ENGINE_REVISION/patch-windows-x64.zip.sha256"
test -f "$OUTPUT_DIR/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip"
test -f "$OUTPUT_DIR/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip.sha256"

CONFLICT_INPUT="$TMP_DIR/conflicting-artifacts"
mkdir -p "$CONFLICT_INPUT/conflict/artifacts/mirror/shorebird/$ENGINE_REVISION"
"$PYTHON_BIN" - "$CONFLICT_INPUT/conflict/artifacts/mirror/shorebird/$ENGINE_REVISION/patch-linux-x64.zip" <<'PY'
from pathlib import Path
import sys
import zipfile

with zipfile.ZipFile(Path(sys.argv[1]), "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("patch", "different-patch\n")
PY

if "$ROOT/scripts/assemble_artifact_mirror.sh" "$CONFLICT_INPUT" "$OUTPUT_DIR" >/dev/null 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed a conflicting mirror file" >&2
  exit 70
fi

UNSAFE_TAR_INPUT="$TMP_DIR/unsafe-tar"
UNSAFE_TAR_OUTPUT="$TMP_DIR/unsafe-tar-output"
mkdir -p "$UNSAFE_TAR_INPUT"
"$PYTHON_BIN" - "$UNSAFE_TAR_INPUT/unsafe-engine.tar.gz" <<'PY'
import io
import sys
import tarfile

archive_path = sys.argv[1]
with tarfile.open(archive_path, "w:gz") as archive:
    content = b"unsafe\n"
    member = tarfile.TarInfo("../outside.txt")
    member.size = len(content)
    archive.addfile(member, io.BytesIO(content))
PY
if "$ROOT/scripts/assemble_artifact_mirror.sh" \
    "$UNSAFE_TAR_INPUT" \
    "$UNSAFE_TAR_OUTPUT" >"$TMP_DIR/unsafe-tar.log" 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed an unsafe tar member" >&2
  exit 70
fi
grep -q "unsafe archive member path" "$TMP_DIR/unsafe-tar.log"

SYMLINK_TAR_INPUT="$TMP_DIR/symlink-tar"
SYMLINK_TAR_OUTPUT="$TMP_DIR/symlink-tar-output"
mkdir -p "$SYMLINK_TAR_INPUT"
"$PYTHON_BIN" - "$SYMLINK_TAR_INPUT/symlink-engine.tar.gz" <<'PY'
import sys
import tarfile

archive_path = sys.argv[1]
with tarfile.open(archive_path, "w:gz") as archive:
    member = tarfile.TarInfo("engine/mirror/shorebird/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/tmp/outside"
    archive.addfile(member)
PY
if "$ROOT/scripts/assemble_artifact_mirror.sh" \
    "$SYMLINK_TAR_INPUT" \
    "$SYMLINK_TAR_OUTPUT" >"$TMP_DIR/symlink-tar.log" 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed a tar symlink member" >&2
  exit 70
fi
grep -q "unsupported archive member type" "$TMP_DIR/symlink-tar.log"

DUPLICATE_TAR_INPUT="$TMP_DIR/duplicate-tar"
DUPLICATE_TAR_OUTPUT="$TMP_DIR/duplicate-tar-output"
mkdir -p "$DUPLICATE_TAR_INPUT"
"$PYTHON_BIN" - "$DUPLICATE_TAR_INPUT/duplicate-engine.tar.gz" <<'PY'
import io
import sys
import tarfile

archive_path = sys.argv[1]
with tarfile.open(archive_path, "w:gz") as archive:
    for content in (b"first\n", b"second\n"):
        member = tarfile.TarInfo("engine/mirror/shorebird/duplicate.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
PY
if "$ROOT/scripts/assemble_artifact_mirror.sh" \
    "$DUPLICATE_TAR_INPUT" \
    "$DUPLICATE_TAR_OUTPUT" >"$TMP_DIR/duplicate-tar.log" 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed a duplicate tar member" >&2
  exit 70
fi
grep -q "duplicate archive member path" "$TMP_DIR/duplicate-tar.log"

BAD_ZIP_INPUT="$TMP_DIR/bad-zip"
BAD_ZIP_OUTPUT="$TMP_DIR/bad-zip-output"
mkdir -p "$BAD_ZIP_INPUT/metadata/artifacts/mirror/shorebird/$ENGINE_REVISION"
cat > "$BAD_ZIP_INPUT/metadata/artifacts/mirror/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" <<EOF
flutter_engine_revision: 'base-engine'
storage_bucket: 'shorebird'
artifact_overrides: []
EOF
printf 'not a zip\n' > "$BAD_ZIP_INPUT/metadata/artifacts/mirror/shorebird/$ENGINE_REVISION/patch-linux-x64.zip"

if "$ROOT/scripts/assemble_artifact_mirror.sh" "$BAD_ZIP_INPUT" "$BAD_ZIP_OUTPUT" >/dev/null 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed an invalid patch zip" >&2
  exit 70
fi

MISSING_INPUT="$TMP_DIR/missing-override"
MISSING_OUTPUT="$TMP_DIR/missing-output"
mkdir -p "$MISSING_INPUT/metadata/artifacts/mirror/shorebird/$ENGINE_REVISION"
cat > "$MISSING_INPUT/metadata/artifacts/mirror/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" <<EOF
flutter_engine_revision: 'base-engine'
storage_bucket: 'shorebird'
artifact_overrides:
  - 'flutter_infra_release/flutter/\$engine/ios-release/artifacts.zip'
EOF

if "$ROOT/scripts/assemble_artifact_mirror.sh" "$MISSING_INPUT" "$MISSING_OUTPUT" >/dev/null 2>&1; then
  echo "assemble_artifact_mirror.sh unexpectedly allowed a missing manifest override" >&2
  exit 70
fi

echo "assemble_artifact_mirror.sh smoke test passed"

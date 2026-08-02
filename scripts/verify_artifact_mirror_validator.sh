#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-mirror-validator.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

"$ROOT/scripts/verify_assemble_artifact_mirror.sh" >/dev/null

PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

ENGINE_REVISION=engine123
MIRROR_ROOT="$TMP_DIR/mirror"
mkdir -p "$MIRROR_ROOT/shorebird/$ENGINE_REVISION"
cat > "$MIRROR_ROOT/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" <<EOF
flutter_engine_revision: 'base-engine'
storage_bucket: 'shorebird'
artifact_overrides:
  - 'flutter_infra_release/flutter/\$engine/linux-x64-release/artifacts.zip'
EOF

"$PYTHON_BIN" - "$MIRROR_ROOT/shorebird/$ENGINE_REVISION" <<'PY'
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

mkdir -p "$MIRROR_ROOT/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release"
printf 'linux-engine-artifacts\n' \
  > "$MIRROR_ROOT/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip"

while IFS= read -r -d '' mirror_file; do
  if [[ "$mirror_file" == *.sha256 ]]; then
    continue
  fi
  "$ROOT/scripts/write_sha256.sh" "$mirror_file"
done < <(find "$MIRROR_ROOT/shorebird" -type f -print0)

"$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" "$MIRROR_ROOT" >/dev/null

UNSAFE_MIRROR_ROOT="$TMP_DIR/unsafe-mirror"
cp -R "$MIRROR_ROOT" "$UNSAFE_MIRROR_ROOT"
"$PYTHON_BIN" - "$UNSAFE_MIRROR_ROOT/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" <<'PY'
from pathlib import Path
import sys

manifest_path = Path(sys.argv[1])
manifest_path.write_text(
    "\n".join(
        [
            "flutter_engine_revision: 'base-engine'",
            "storage_bucket: 'shorebird'",
            "artifact_overrides:",
            "  - '../outside/artifacts.zip'",
        ]
    )
    + "\n",
    encoding="utf-8",
)
PY
"$ROOT/scripts/write_sha256.sh" \
  "$UNSAFE_MIRROR_ROOT/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml" \
  "$UNSAFE_MIRROR_ROOT/shorebird/$ENGINE_REVISION/artifacts_manifest.yaml.sha256"
if "$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" \
    "$UNSAFE_MIRROR_ROOT" >"$TMP_DIR/unsafe-mirror.log" 2>&1; then
  echo "validate_artifact_mirror.py unexpectedly accepted an unsafe manifest override" >&2
  exit 70
fi
grep -q "unsafe artifact override path" "$TMP_DIR/unsafe-mirror.log"

SYMLINK_MIRROR_ROOT="$TMP_DIR/symlink-mirror"
cp -R "$MIRROR_ROOT" "$SYMLINK_MIRROR_ROOT"
rm "$SYMLINK_MIRROR_ROOT/shorebird/$ENGINE_REVISION/patch-linux-x64.zip"
ln -s "$MIRROR_ROOT/shorebird/$ENGINE_REVISION/patch-linux-x64.zip" \
  "$SYMLINK_MIRROR_ROOT/shorebird/$ENGINE_REVISION/patch-linux-x64.zip"
if "$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" \
    "$SYMLINK_MIRROR_ROOT" >"$TMP_DIR/symlink-mirror.log" 2>&1; then
  echo "validate_artifact_mirror.py unexpectedly accepted a symlink artifact" >&2
  exit 70
fi
grep -q "symlink entries are not allowed" "$TMP_DIR/symlink-mirror.log"

EMPTY_OVERRIDE_ROOT="$TMP_DIR/empty-override-mirror"
cp -R "$MIRROR_ROOT" "$EMPTY_OVERRIDE_ROOT"
"$PYTHON_BIN" - "$EMPTY_OVERRIDE_ROOT/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_bytes(b"")
PY
"$ROOT/scripts/write_sha256.sh" \
  "$EMPTY_OVERRIDE_ROOT/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip" \
  "$EMPTY_OVERRIDE_ROOT/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/linux-x64-release/artifacts.zip.sha256"
if "$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" \
    "$EMPTY_OVERRIDE_ROOT" >"$TMP_DIR/empty-override-mirror.log" 2>&1; then
  echo "validate_artifact_mirror.py unexpectedly accepted an empty manifest override artifact" >&2
  exit 70
fi
grep -q "artifact override is empty" "$TMP_DIR/empty-override-mirror.log"

printf 'tampered\n' >> "$MIRROR_ROOT/shorebird/$ENGINE_REVISION/patch-linux-x64.zip"
if "$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" "$MIRROR_ROOT" >/dev/null 2>&1; then
  echo "validate_artifact_mirror.py unexpectedly accepted a stale sidecar" >&2
  exit 70
fi

echo "validate_artifact_mirror.py smoke test passed"

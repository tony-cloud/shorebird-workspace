#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat >&2 <<'EOF'
usage: verify_downloaded_release_artifacts.sh [--github-sha sha] downloaded-artifacts

Verifies a downloaded full SDK build artifact set. The release manifest and
artifact mirror archive must have valid checksum sidecars, the manifest must
cover every downloaded artifact, and the mirror archive must safely extract to a
valid open Shorebird artifact mirror.
EOF
}

DOWNLOAD_DIR=""
EXPECTED_GITHUB_SHA=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --github-sha)
      if [[ "$#" -lt 2 || -z "${2:-}" ]]; then
        echo "--github-sha value is required" >&2
        usage
        exit 64
      fi
      EXPECTED_GITHUB_SHA="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "unknown argument: $1" >&2
      usage
      exit 64
      ;;
    *)
      if [[ -n "$DOWNLOAD_DIR" ]]; then
        echo "unexpected extra argument: $1" >&2
        usage
        exit 64
      fi
      DOWNLOAD_DIR="$1"
      shift
      ;;
  esac
done

if [[ -z "$DOWNLOAD_DIR" ]]; then
  usage
  exit 64
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-downloaded-release.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

fail() {
  echo "error: $*" >&2
  exit 70
}

verify_sha256_sidecar() {
  local artifact_path="$1"
  local sidecar_path="$2"

  [[ -f "$sidecar_path" ]] || fail "missing checksum sidecar: $sidecar_path"

  "$PYTHON_BIN" - "$artifact_path" "$sidecar_path" <<'PY'
import hashlib
from pathlib import Path
import sys

artifact_path = Path(sys.argv[1])
sidecar_path = Path(sys.argv[2])

try:
    text = sidecar_path.read_text(encoding="utf-8").strip()
except UnicodeDecodeError as error:
    print(f"error: {sidecar_path}: invalid UTF-8: {error}", file=sys.stderr)
    sys.exit(70)

parts = text.split()
if len(parts) != 2:
    print(
        f"error: {sidecar_path}: expected '<sha256>  <filename>', got {text!r}",
        file=sys.stderr,
    )
    sys.exit(70)

expected_digest, expected_filename = parts
if len(expected_digest) != 64 or any(
    char not in "0123456789abcdef" for char in expected_digest
):
    print(f"error: {sidecar_path}: invalid sha256 digest {expected_digest!r}", file=sys.stderr)
    sys.exit(70)

if expected_filename != artifact_path.name:
    print(
        f"error: {sidecar_path}: filename mismatch "
        f"{expected_filename!r} != {artifact_path.name!r}",
        file=sys.stderr,
    )
    sys.exit(70)

digest = hashlib.sha256()
with artifact_path.open("rb") as artifact:
    for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
        digest.update(chunk)

actual_digest = digest.hexdigest()
if expected_digest != actual_digest:
    print(
        f"error: {sidecar_path}: digest mismatch "
        f"{expected_digest} != {actual_digest}",
        file=sys.stderr,
    )
    sys.exit(70)
PY
}

[[ -d "$DOWNLOAD_DIR" ]] || fail "missing downloaded artifacts directory: $DOWNLOAD_DIR"

manifest_paths=()
while IFS= read -r path; do
  manifest_paths+=("$path")
done < <(find "$DOWNLOAD_DIR" -type f -name open-shorebird-release-manifest.json | sort)
if [[ "${#manifest_paths[@]}" -ne 1 ]]; then
  fail "expected exactly one open-shorebird-release-manifest.json, found ${#manifest_paths[@]}"
fi
manifest_path="${manifest_paths[0]}"
manifest_sidecar="$manifest_path.sha256"

verify_sha256_sidecar "$manifest_path" "$manifest_sidecar"

validate_manifest_args=()
if [[ -n "$EXPECTED_GITHUB_SHA" ]]; then
  validate_manifest_args+=(--github-sha "$EXPECTED_GITHUB_SHA")
fi
"$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
  "${validate_manifest_args[@]}" \
  "$DOWNLOAD_DIR" \
  "$manifest_path"

mirror_archives=()
while IFS= read -r path; do
  mirror_archives+=("$path")
done < <(find "$DOWNLOAD_DIR" -type f -name open-shorebird-artifact-mirror.tar.gz | sort)
if [[ "${#mirror_archives[@]}" -ne 1 ]]; then
  fail "expected exactly one open-shorebird-artifact-mirror.tar.gz, found ${#mirror_archives[@]}"
fi
mirror_archive="${mirror_archives[0]}"

mirror_sidecar="$mirror_archive.sha256"
verify_sha256_sidecar "$mirror_archive" "$mirror_sidecar"

"$PYTHON_BIN" "$ROOT/scripts/safe_extract_tar.py" "$mirror_archive" "$TMP_DIR"
mirror_root="$TMP_DIR/open-shorebird-artifact-mirror"
[[ -d "$mirror_root" ]] || fail "mirror archive did not contain open-shorebird-artifact-mirror/"

"$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" "$mirror_root"

echo "downloaded release artifacts verified: $DOWNLOAD_DIR"

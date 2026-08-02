#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: assemble_artifact_mirror.sh <downloaded-workflow-artifacts-dir> <output-mirror-root>

Copies every publish-ready shorebird/ mirror subtree from downloaded GitHub
Actions artifacts into <output-mirror-root>. Engine archives are extracted and
scanned for nested mirror/shorebird trees. Existing files may be reused only
when their bytes match.
EOF
}

if [[ "$#" -ne 2 ]]; then
  usage
  exit 64
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_DIR="$1"
OUTPUT_DIR="$2"

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "input artifact directory does not exist: $INPUT_DIR" >&2
  exit 66
fi

PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-mirror.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$OUTPUT_DIR"

FOUND_TREES=0

copy_tree_to_shorebird_prefix() {
  local tree="$1"
  local prefix="$2"
  local source_file rel_file target_file

  FOUND_TREES=$((FOUND_TREES + 1))
  while IFS= read -r -d '' source_file; do
    rel_file="${source_file#"$tree"/}"
    target_file="$OUTPUT_DIR/shorebird/$prefix$rel_file"
    mkdir -p "$(dirname "$target_file")"
    if [[ -e "$target_file" ]]; then
      if ! cmp -s "$source_file" "$target_file"; then
        echo "conflicting mirror file: shorebird/$prefix$rel_file" >&2
        echo "  existing: $target_file" >&2
        echo "  incoming: $source_file" >&2
        exit 70
      fi
      continue
    fi
    cp -p "$source_file" "$target_file"
  done < <(find "$tree" -type f -print0)
}

copy_shorebird_tree() {
  copy_tree_to_shorebird_prefix "$1" ""
}

scan_for_shorebird_trees() {
  local search_root="$1"
  local tree

  while IFS= read -r -d '' tree; do
    copy_shorebird_tree "$tree"
  done < <(find "$search_root" -type d -name shorebird -print0)
}

scan_for_downloaded_metadata_trees() {
  local search_root="$1"
  local manifest_path metadata_dir engine_revision

  # actions/upload-artifact strips the non-wildcard prefix from
  # artifacts/mirror/shorebird/**/artifacts_manifest.yaml, so the downloaded
  # mirror-metadata artifact is shaped as <engine>/artifacts_manifest.yaml.
  while IFS= read -r -d '' manifest_path; do
    if [[ "$manifest_path" == */shorebird/* ]]; then
      continue
    fi

    metadata_dir="$(dirname "$manifest_path")"
    engine_revision="$(basename "$metadata_dir")"
    copy_tree_to_shorebird_prefix "$metadata_dir" "$engine_revision/"
  done < <(find "$search_root" -type f -name artifacts_manifest.yaml -print0)
}

scan_for_shorebird_trees "$INPUT_DIR"
scan_for_downloaded_metadata_trees "$INPUT_DIR"

archive_index=0
while IFS= read -r -d '' archive_path; do
  archive_index=$((archive_index + 1))
  extract_dir="$TMP_DIR/archive-$archive_index"
  mkdir -p "$extract_dir"
  "$PYTHON_BIN" "$ROOT/scripts/safe_extract_tar.py" "$archive_path" "$extract_dir"
  scan_for_shorebird_trees "$extract_dir"
done < <(find "$INPUT_DIR" -type f \( -name '*.tar.gz' -o -name '*.tgz' \) -print0)

if [[ "$FOUND_TREES" -eq 0 ]]; then
  echo "no shorebird/ mirror subtrees found under $INPUT_DIR" >&2
  exit 65
fi

while IFS= read -r -d '' mirror_file; do
  if [[ "$mirror_file" == *.sha256 ]]; then
    continue
  fi
  sidecar="$mirror_file.sha256"
  if [[ ! -f "$sidecar" ]]; then
    "$ROOT/scripts/write_sha256.sh" "$mirror_file" "$sidecar"
  fi
done < <(find "$OUTPUT_DIR/shorebird" -type f -print0)

"$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" "$OUTPUT_DIR"

echo "assembled artifact mirror at $OUTPUT_DIR"

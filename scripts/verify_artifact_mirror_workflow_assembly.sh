#!/usr/bin/env bash
set -euo pipefail

# macOS tar otherwise emits AppleDouble files alongside checksum sidecars.
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open-shorebird-artifact-job.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

ENGINE_REVISION=engine123
DOWNLOADED="$TMP_DIR/downloaded-artifacts"
mkdir -p "$DOWNLOADED"

write_artifact() {
  local path="$1"
  local content="$2"
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" > "$path"
  "$ROOT/scripts/write_sha256.sh" "$path"
}

write_zip() {
  local zip_path="$1"
  local entry_name="$2"
  local content="$3"
  mkdir -p "$(dirname "$zip_path")"
  "$PYTHON_BIN" - "$zip_path" "$entry_name" "$content" <<'PY'
from pathlib import Path
import sys
import zipfile

zip_path, entry_name, content = sys.argv[1:]
with zipfile.ZipFile(Path(zip_path), "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr(entry_name, content + "\n")
PY
  "$ROOT/scripts/write_sha256.sh" "$zip_path"
}

write_tgz() {
  local archive_path="$1"
  local staging_dir="$2"
  local root_entry="$3"
  mkdir -p "$(dirname "$archive_path")"
  tar -C "$staging_dir" -czf "$archive_path" "$root_entry"
  "$ROOT/scripts/write_sha256.sh" "$archive_path"
}

for target in \
  cli-linux-x64/open-shorebird-cli-linux-x64.tar.gz \
  cli-macos-x64/open-shorebird-cli-macos-x64.tar.gz \
  cli-macos-arm64/open-shorebird-cli-macos-arm64.tar.gz \
  cli-windows-x64/open-shorebird-cli-windows-x64.tar.gz \
  shorebird-server-linux-amd64/shorebird-server-linux-amd64.tar.gz \
  shorebird-server-linux-arm64/shorebird-server-linux-arm64.tar.gz \
  shorebird-server-darwin-amd64/shorebird-server-darwin-amd64.tar.gz \
  shorebird-server-darwin-arm64/shorebird-server-darwin-arm64.tar.gz \
  shorebird-server-windows-amd64/shorebird-server-windows-amd64.tar.gz \
  custom-dart-sdk-linux-x64/custom-dart-sdk-linux-x64.tar.gz \
  custom-dart-sdk-macos-arm64/custom-dart-sdk-macos-arm64.tar.gz \
  custom-dart-sdk-macos-x64/custom-dart-sdk-macos-x64.tar.gz \
  custom-dart-sdk-windows-x64/custom-dart-sdk-windows-x64.tar.gz; do
  write_artifact "$DOWNLOADED/$target" "$target"
done

mkdir -p "$DOWNLOADED/mirror-metadata/$ENGINE_REVISION"
"$PYTHON_BIN" "$ROOT/scripts/write_artifact_manifest.py" \
  --flutter-engine-revision base-engine \
  --output "$DOWNLOADED/mirror-metadata/$ENGINE_REVISION/artifacts_manifest.yaml"

"$ROOT/scripts/write_sha256.sh" \
  "$DOWNLOADED/mirror-metadata/$ENGINE_REVISION/artifacts_manifest.yaml"

write_patch_artifact() {
  local artifact_name="$1"
  local zip_name="$2"
  local entry_name="$3"
  write_zip "$DOWNLOADED/$artifact_name/artifacts/mirror/$zip_name" "$entry_name" "$zip_name"
  write_zip "$DOWNLOADED/$artifact_name/artifacts/mirror/shorebird/$ENGINE_REVISION/$zip_name" "$entry_name" "$zip_name"
}
write_patch_artifact mirror-patch-linux-x64.zip patch-linux-x64.zip patch
write_patch_artifact mirror-patch-darwin-x64.zip patch-darwin-x64.zip patch
write_patch_artifact mirror-patch-darwin-arm64.zip patch-darwin-arm64.zip patch
write_patch_artifact mirror-patch-windows-x64.zip patch-windows-x64.zip patch.exe

engine_stage="$TMP_DIR/engine-stage"
mkdir -p "$engine_stage"

make_engine_archive() {
  local artifact_dir="$1"
  local archive_name="$2"
  local root_name="$3"
  local mirror_subdir="$4"
  shift 4

  local stage="$engine_stage/$root_name"
  rm -rf "$stage"
  mkdir -p "$stage/$root_name/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/$mirror_subdir"
  while [[ "$#" -gt 0 ]]; do
    local file_name="$1"
    local content="$2"
    shift 2
    write_artifact \
      "$stage/$root_name/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/$mirror_subdir/$file_name" \
      "$content"
  done
  write_tgz "$DOWNLOADED/$artifact_dir/$archive_name" "$stage" "$root_name"
}

make_engine_archive \
  linux-engine-x64 linux-engine-x64.tar.gz linux-engine linux-x64-release \
  artifacts.zip linux-artifacts \
  linux-x64-flutter-gtk.zip linux-gtk
mkdir -p "$engine_stage/linux-engine/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION"
write_artifact \
  "$engine_stage/linux-engine/linux-engine/mirror/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION/flutter_patched_sdk_product.zip" \
  linux-patched-sdk
write_tgz "$DOWNLOADED/linux-engine-x64/linux-engine-x64.tar.gz" \
  "$engine_stage/linux-engine" \
  linux-engine

make_engine_archive \
  android-engine-arm64 android-engine-arm64.tar.gz android-engine android-arm64-release \
  artifacts.zip android-artifacts \
  symbols.zip android-symbols \
  linux-x64.zip android-linux-snapshotter
make_engine_archive \
  ios-interpreter-engine ios-interpreter-engine.tar.gz ios-engine ios-release \
  artifacts.zip ios-artifacts
make_engine_archive \
  macos-engine-arm64 macos-engine-arm64.tar.gz macos-engine darwin-arm64-release \
  FlutterMacOS.framework.zip macos-framework

for arch in arm x64; do
  make_engine_archive \
    "android-engine-$arch" "android-engine-$arch.tar.gz" android-engine "android-$arch-release" \
    artifacts.zip "android-$arch-artifacts" symbols.zip "android-$arch-symbols" \
    linux-x64.zip "android-$arch-linux-snapshotter"
done
for host in darwin-x64 windows-x64; do
  for arch in arm arm64 x64; do
    make_engine_archive \
      "android-host-tools-$host-$arch" "android-host-tools-$host-$arch.tar.gz" android-host-tools "android-$arch-release" \
      "$host.zip" "android-$arch-$host-snapshotter"
  done
done
make_engine_archive \
  macos-engine-x64 macos-engine-x64.tar.gz macos-engine darwin-x64-release \
  FlutterMacOS.framework.zip macos-x64-framework
make_engine_archive \
  macos-engine-universal macos-engine-universal.tar.gz macos-engine-universal darwin-x64-release \
  framework.zip macos-universal-framework gen_snapshot.zip macos-snapshotters artifacts.zip macos-host-artifacts
make_engine_archive \
  windows-engine-x64 windows-engine-x64.tar.gz windows-engine windows-x64-release \
  windows-x64-flutter.zip windows-engine

mirror_input="$TMP_DIR/mirror-input"
mkdir -p "$mirror_input"
cp -R "$DOWNLOADED"/mirror-* "$mirror_input/"
cp -R "$DOWNLOADED/linux-engine-x64" "$mirror_input/"
cp -R "$DOWNLOADED/android-engine-arm64" "$mirror_input/"
cp -R "$DOWNLOADED/ios-interpreter-engine" "$mirror_input/"
cp -R "$DOWNLOADED/macos-engine-arm64" "$mirror_input/"
cp -R "$DOWNLOADED/macos-engine-x64" "$mirror_input/"
cp -R "$DOWNLOADED/macos-engine-universal" "$mirror_input/"
cp -R "$DOWNLOADED/windows-engine-x64" "$mirror_input/"
cp -R "$DOWNLOADED/android-engine-arm" "$mirror_input/"
cp -R "$DOWNLOADED/android-engine-x64" "$mirror_input/"
cp -R "$DOWNLOADED"/android-host-tools-* "$mirror_input/"

sdk_mirror="$mirror_input/shorebird/flutter_infra_release/flutter/$ENGINE_REVISION"
write_zip "$sdk_mirror/dart-sdk-linux-x64.zip" dart-sdk/bin/dart linux-sdk
write_zip "$sdk_mirror/dart-sdk-darwin-arm64.zip" dart-sdk/bin/dart macos-sdk
write_zip "$sdk_mirror/dart-sdk-darwin-x64.zip" dart-sdk/bin/dart macos-x64-sdk
write_zip "$sdk_mirror/dart-sdk-windows-x64.zip" dart-sdk/bin/dart.exe windows-sdk

# The three ABI producers publish Maven paths alongside their engine trees;
# arm64 is the sole producer of the shared embedding artifact.
for artifact in flutter_embedding_release arm64_v8a_release armeabi_v7a_release x86_64_release; do
  maven_dir="$mirror_input/shorebird/download.flutter.io/io/flutter/$artifact/1.0.0-$ENGINE_REVISION"
  for extension in jar pom maven-metadata.xml; do
    write_artifact "$maven_dir/$artifact-1.0.0-$ENGINE_REVISION.$extension" "$artifact-$extension"
    write_artifact "$maven_dir/$artifact-1.0.0-$ENGINE_REVISION.$extension.sha1" "sha1-$artifact-$extension"
  done
done

assembled="$TMP_DIR/artifacts/open-shorebird-artifact-mirror"
mkdir -p "$TMP_DIR/artifacts"
"$ROOT/scripts/assemble_artifact_mirror.sh" "$mirror_input" "$assembled" >/dev/null
tar -C "$TMP_DIR/artifacts" -czf "$TMP_DIR/open-shorebird-artifact-mirror.tar.gz" open-shorebird-artifact-mirror
mirror_extract_dir="$TMP_DIR/mirror-extract"
mkdir -p "$mirror_extract_dir"
"$PYTHON_BIN" "$ROOT/scripts/safe_extract_tar.py" \
  "$TMP_DIR/open-shorebird-artifact-mirror.tar.gz" \
  "$mirror_extract_dir"
"$PYTHON_BIN" "$ROOT/scripts/validate_artifact_mirror.py" \
  "$mirror_extract_dir/open-shorebird-artifact-mirror" >/dev/null
"$ROOT/scripts/write_sha256.sh" "$TMP_DIR/open-shorebird-artifact-mirror.tar.gz"

manifest_input="$TMP_DIR/manifest-input"
mkdir -p "$manifest_input"
cp -R "$DOWNLOADED"/. "$manifest_input/"
mkdir -p "$manifest_input/open-shorebird-artifact-mirror"
cp "$TMP_DIR/open-shorebird-artifact-mirror.tar.gz" "$manifest_input/open-shorebird-artifact-mirror/"
cp "$TMP_DIR/open-shorebird-artifact-mirror.tar.gz.sha256" "$manifest_input/open-shorebird-artifact-mirror/"

"$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
  "$manifest_input" \
  --github-sha test-sha \
  --require 'cli-linux-x64/*open-shorebird-cli-linux-x64.tar.gz' \
  --require 'cli-macos-x64/*open-shorebird-cli-macos-x64.tar.gz' \
  --require 'cli-macos-arm64/*open-shorebird-cli-macos-arm64.tar.gz' \
  --require 'cli-windows-x64/*open-shorebird-cli-windows-x64.tar.gz' \
  --require 'shorebird-server-linux-amd64/*shorebird-server-linux-amd64.tar.gz' \
  --require 'shorebird-server-linux-arm64/*shorebird-server-linux-arm64.tar.gz' \
  --require 'shorebird-server-darwin-amd64/*shorebird-server-darwin-amd64.tar.gz' \
  --require 'shorebird-server-darwin-arm64/*shorebird-server-darwin-arm64.tar.gz' \
  --require 'shorebird-server-windows-amd64/*shorebird-server-windows-amd64.tar.gz' \
  --require 'custom-dart-sdk-linux-x64/*custom-dart-sdk-linux-x64.tar.gz' \
  --require 'custom-dart-sdk-macos-arm64/*custom-dart-sdk-macos-arm64.tar.gz' \
  --require 'linux-engine-x64/*linux-engine-x64.tar.gz' \
  --require 'android-engine-arm64/*android-engine-arm64.tar.gz' \
  --require 'custom-dart-sdk-macos-x64/*custom-dart-sdk-macos-x64.tar.gz' \
  --require 'custom-dart-sdk-windows-x64/*custom-dart-sdk-windows-x64.tar.gz' \
  --require 'android-engine-arm/*android-engine-arm.tar.gz' \
  --require 'android-engine-x64/*android-engine-x64.tar.gz' \
  --require 'macos-engine-x64/*macos-engine-x64.tar.gz' \
  --require 'macos-engine-universal/*macos-engine-universal.tar.gz' \
  --require 'windows-engine-x64/*windows-engine-x64.tar.gz' \
  --require 'android-host-tools-darwin-x64-arm/*android-host-tools-darwin-x64-arm.tar.gz' \
  --require 'android-host-tools-darwin-x64-arm64/*android-host-tools-darwin-x64-arm64.tar.gz' \
  --require 'android-host-tools-darwin-x64-x64/*android-host-tools-darwin-x64-x64.tar.gz' \
  --require 'android-host-tools-windows-x64-arm/*android-host-tools-windows-x64-arm.tar.gz' \
  --require 'android-host-tools-windows-x64-arm64/*android-host-tools-windows-x64-arm64.tar.gz' \
  --require 'android-host-tools-windows-x64-x64/*android-host-tools-windows-x64-x64.tar.gz' \
  --require 'ios-interpreter-engine/*ios-interpreter-engine.tar.gz' \
  --require 'macos-engine-arm64/*macos-engine-arm64.tar.gz' \
  --require 'mirror-patch-linux-x64.zip/*patch-linux-x64.zip' \
  --require 'mirror-patch-darwin-x64.zip/*patch-darwin-x64.zip' \
  --require 'mirror-patch-darwin-arm64.zip/*patch-darwin-arm64.zip' \
  --require 'mirror-patch-windows-x64.zip/*patch-windows-x64.zip' \
  --require 'mirror-metadata/*artifacts_manifest.yaml' \
  --require 'open-shorebird-artifact-mirror/*open-shorebird-artifact-mirror.tar.gz' \
  --output "$TMP_DIR/open-shorebird-release-manifest.json"

"$PYTHON_BIN" "$ROOT/scripts/validate_release_manifest.py" \
  "$manifest_input" \
  "$TMP_DIR/open-shorebird-release-manifest.json" >/dev/null

mkdir -p "$manifest_input/open-shorebird-release-manifest"
cp "$TMP_DIR/open-shorebird-release-manifest.json" \
  "$manifest_input/open-shorebird-release-manifest/"
"$ROOT/scripts/write_sha256.sh" \
  "$manifest_input/open-shorebird-release-manifest/open-shorebird-release-manifest.json"
"$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
  --github-sha test-sha \
  "$manifest_input" >/dev/null
if "$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
    --github-sha wrong-sha \
    "$manifest_input" >"$TMP_DIR/wrong-download-sha.log" 2>&1; then
  echo "unexpectedly accepted downloaded artifacts for the wrong github_sha" >&2
  exit 1
fi
grep -q "github_sha is" "$TMP_DIR/wrong-download-sha.log"

release_manifest_path="$manifest_input/open-shorebird-release-manifest/open-shorebird-release-manifest.json"
mirror_archive_path="$manifest_input/open-shorebird-artifact-mirror/open-shorebird-artifact-mirror.tar.gz"
printf '%064d  open-shorebird-release-manifest.json\n' 0 > "$release_manifest_path.sha256"
if "$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
    --github-sha test-sha \
    "$manifest_input" >/dev/null 2>&1; then
  echo "unexpectedly accepted a stale downloaded release manifest sidecar" >&2
  exit 1
fi
"$ROOT/scripts/write_sha256.sh" "$release_manifest_path"

printf '%064d  open-shorebird-artifact-mirror.tar.gz\n' 0 > "$mirror_archive_path.sha256"
if "$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
    --github-sha test-sha \
    "$manifest_input" >/dev/null 2>&1; then
  echo "unexpectedly accepted a stale downloaded mirror archive sidecar" >&2
  exit 1
fi
"$ROOT/scripts/write_sha256.sh" "$mirror_archive_path"
"$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
  --github-sha test-sha \
  "$manifest_input" >/dev/null

unsafe_download="$TMP_DIR/unsafe-download"
cp -R "$manifest_input" "$unsafe_download"
unsafe_mirror_archive="$unsafe_download/open-shorebird-artifact-mirror/open-shorebird-artifact-mirror.tar.gz"
"$PYTHON_BIN" - "$unsafe_mirror_archive" <<'PY'
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
"$ROOT/scripts/write_sha256.sh" "$unsafe_mirror_archive"
"$PYTHON_BIN" "$ROOT/scripts/write_release_manifest.py" \
  "$unsafe_download" \
  --github-sha test-sha \
  --require 'open-shorebird-artifact-mirror/*open-shorebird-artifact-mirror.tar.gz' \
  --output "$unsafe_download/open-shorebird-release-manifest/open-shorebird-release-manifest.json"
"$ROOT/scripts/write_sha256.sh" \
  "$unsafe_download/open-shorebird-release-manifest/open-shorebird-release-manifest.json"
if "$ROOT/scripts/verify_downloaded_release_artifacts.sh" \
    --github-sha test-sha \
    "$unsafe_download" >"$TMP_DIR/unsafe-download.log" 2>&1; then
  echo "unexpectedly accepted an unsafe downloaded mirror archive" >&2
  exit 1
fi
grep -q "unsafe archive member path" "$TMP_DIR/unsafe-download.log"

"$PYTHON_BIN" - "$TMP_DIR/open-shorebird-release-manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
paths = {artifact["path"] for artifact in manifest["artifacts"]}
assert any(path.endswith("open-shorebird-artifact-mirror.tar.gz") for path in paths)
assert any(path.endswith("linux-engine-x64.tar.gz") for path in paths)
assert any(path.endswith("patch-windows-x64.zip") for path in paths)
assert any(path.endswith("artifacts_manifest.yaml") for path in paths)
PY

echo "artifact-mirror workflow assembly smoke test passed"

#!/usr/bin/env python3
"""Validate an assembled open Shorebird artifact mirror."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


REQUIRED_PATCH_ZIPS = {
    "patch-linux-x64.zip": "patch",
    "patch-darwin-x64.zip": "patch",
    "patch-darwin-arm64.zip": "patch",
    "patch-windows-x64.zip": "patch.exe",
}


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def parse_sidecar(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    parts = text.split()
    if len(parts) != 2:
        raise ValueError(f"expected '<sha256>  <filename>', got {text!r}")
    digest, filename = parts
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"invalid sha256 digest {digest!r}")
    return digest, filename


def is_safe_relative_path(path: str) -> bool:
    if not path or path == ".":
        return False
    if "\\" in path or "\x00" in path or path.endswith("/"):
        return False
    if any(ord(character) < 32 for character in path):
        return False

    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        return False
    if any(part in ("", ".", "..") for part in candidate.parts):
        return False
    if candidate.parts and ":" in candidate.parts[0]:
        return False
    return True


def validate_sidecars(shorebird_root: Path) -> list[str]:
    errors: list[str] = []
    for artifact_path in sorted(shorebird_root.rglob("*")):
        if artifact_path.is_symlink():
            errors.append(f"{artifact_path.relative_to(shorebird_root.parent)}: symlink entries are not allowed")
            continue
        if not artifact_path.is_file():
            continue
        if artifact_path.suffix == ".sha256":
            artifact_path_without_suffix = Path(str(artifact_path)[: -len(".sha256")])
            if not is_plain_file(artifact_path_without_suffix):
                errors.append(f"{artifact_path.relative_to(shorebird_root.parent)}: orphan sidecar")
            continue

        sidecar_path = Path(f"{artifact_path}.sha256")
        if not is_plain_file(sidecar_path):
            errors.append(f"{artifact_path.relative_to(shorebird_root.parent)}: missing sidecar")
            continue
        try:
            sidecar_digest, sidecar_filename = parse_sidecar(sidecar_path)
        except ValueError as error:
            errors.append(f"{sidecar_path.relative_to(shorebird_root.parent)}: {error}")
            continue
        actual_digest = digest_file(artifact_path)
        if sidecar_digest != actual_digest:
            errors.append(
                f"{sidecar_path.relative_to(shorebird_root.parent)}: digest mismatch "
                f"{sidecar_digest} != {actual_digest}"
            )
        if sidecar_filename != artifact_path.name:
            errors.append(
                f"{sidecar_path.relative_to(shorebird_root.parent)}: filename mismatch "
                f"{sidecar_filename!r} != {artifact_path.name!r}"
            )
    return errors


def validate_manifest_overrides(mirror_root: Path, manifest_paths: list[Path]) -> list[str]:
    override_pattern = re.compile(r"^\s*-\s*'?(?P<path>[^'#\n]+?)'?\s*(?:#.*)?$")
    shorebird_root = mirror_root / "shorebird"
    errors: list[str] = []
    for manifest_path in manifest_paths:
        engine_revision = manifest_path.parent.name
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            match = override_pattern.match(line)
            if not match:
                continue
            artifact_path = match.group("path").replace("$engine", engine_revision)
            if not is_safe_relative_path(artifact_path):
                errors.append(
                    f"{manifest_path.relative_to(mirror_root)} -> "
                    f"unsafe artifact override path: {artifact_path}"
                )
                continue
            resolved_artifact_path = shorebird_root / artifact_path
            if not is_plain_file(resolved_artifact_path):
                errors.append(
                    f"{manifest_path.relative_to(mirror_root)} -> shorebird/{artifact_path}"
                )
                continue
            if resolved_artifact_path.stat().st_size <= 0:
                errors.append(
                    f"{manifest_path.relative_to(mirror_root)} -> "
                    f"shorebird/{artifact_path}: artifact override is empty"
                )
    return errors


def validate_patch_zips(mirror_root: Path, manifest_paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for manifest_path in manifest_paths:
        engine_dir = manifest_path.parent
        for zip_name, expected_entry in REQUIRED_PATCH_ZIPS.items():
            zip_path = engine_dir / zip_name
            display_path = zip_path.relative_to(mirror_root)
            if not is_plain_file(zip_path):
                errors.append(f"{display_path}: missing")
                continue
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    names = archive.namelist()
                    if names != [expected_entry]:
                        errors.append(
                            f"{display_path}: expected only {expected_entry!r}, got {names!r}"
                        )
                        continue
                    if archive.getinfo(expected_entry).file_size <= 0:
                        errors.append(f"{display_path}: {expected_entry} is empty")
            except zipfile.BadZipFile:
                errors.append(f"{display_path}: invalid zip")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mirror_root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mirror_root = args.mirror_root
    shorebird_root = mirror_root / "shorebird"

    if not mirror_root.is_dir():
        print(f"missing mirror root: {mirror_root}", file=sys.stderr)
        return 66
    if not shorebird_root.is_dir():
        print(f"mirror root is missing shorebird/: {mirror_root}", file=sys.stderr)
        return 70

    manifest_paths = sorted(shorebird_root.glob("*/artifacts_manifest.yaml"))
    if not manifest_paths:
        print(
            "artifact mirror is missing shorebird/<engine>/artifacts_manifest.yaml",
            file=sys.stderr,
        )
        return 70

    errors: list[str] = []
    sidecar_errors = validate_sidecars(shorebird_root)
    if sidecar_errors:
        errors.append("invalid checksum sidecars:\n" + "\n".join(f"  {e}" for e in sidecar_errors))

    override_errors = validate_manifest_overrides(mirror_root, manifest_paths)
    if override_errors:
        errors.append(
            "invalid files referenced by artifacts_manifest.yaml:\n"
            + "\n".join(f"  {path}" for path in override_errors)
        )

    patch_errors = validate_patch_zips(mirror_root, manifest_paths)
    if patch_errors:
        errors.append(
            "invalid CLI patch-tool artifacts:\n"
            + "\n".join(f"  {error}" for error in patch_errors)
        )

    if errors:
        print("artifact mirror validation failed:\n" + "\n".join(errors), file=sys.stderr)
        return 70

    print(f"artifact mirror validated: {mirror_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

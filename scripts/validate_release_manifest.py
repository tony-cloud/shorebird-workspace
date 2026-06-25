#!/usr/bin/env python3
"""Validate a JSON release manifest against downloaded CI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--github-sha",
        default="",
        help="Require the manifest github_sha field to match this commit SHA.",
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("manifest", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir
    manifest_path = args.manifest

    if not input_dir.is_dir():
        print(f"missing input directory: {input_dir}", file=sys.stderr)
        return 66
    if not manifest_path.is_file():
        print(f"missing release manifest: {manifest_path}", file=sys.stderr)
        return 66

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"invalid release manifest JSON: {error}", file=sys.stderr)
        return 70

    errors: list[str] = []
    if manifest.get("format_version") != 1:
        errors.append(f"format_version is {manifest.get('format_version')!r}; expected 1")
    if not isinstance(manifest.get("github_sha", ""), str):
        errors.append("github_sha must be a string")
    elif args.github_sha and manifest.get("github_sha") != args.github_sha:
        errors.append(
            f"github_sha is {manifest.get('github_sha')!r}; expected {args.github_sha!r}"
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
        artifacts = []

    expected_count = manifest.get("artifact_count")
    if expected_count != len(artifacts):
        errors.append(
            f"artifact_count is {expected_count!r}; expected {len(artifacts)}"
        )

    seen_paths: set[str] = set()
    seen_sidecars: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifacts[{index}] must be an object")
            continue

        artifact_path_text = artifact.get("path")
        artifact_group = artifact.get("artifact_group")
        filename = artifact.get("filename")
        sidecar_path_text = artifact.get("sidecar")
        expected_digest = artifact.get("sha256")
        expected_size = artifact.get("size")
        if not isinstance(artifact_path_text, str):
            errors.append(f"artifacts[{index}].path must be a string")
            continue
        if artifact_path_text in seen_paths:
            errors.append(f"{artifact_path_text}: duplicate artifact path")
        seen_paths.add(artifact_path_text)
        if not is_safe_relative_path(artifact_path_text):
            errors.append(f"{artifact_path_text}: unsafe artifact path")
            continue
        artifact_relative = PurePosixPath(artifact_path_text)

        if not isinstance(artifact_group, str):
            errors.append(f"{artifact_path_text}: artifact_group must be a string")
        elif artifact_group != artifact_relative.parts[0]:
            errors.append(
                f"{artifact_path_text}: artifact_group {artifact_group!r} "
                f"does not match path group {artifact_relative.parts[0]!r}"
            )

        if not isinstance(filename, str):
            errors.append(f"{artifact_path_text}: filename must be a string")
        elif filename != artifact_relative.name:
            errors.append(
                f"{artifact_path_text}: filename {filename!r} "
                f"does not match path filename {artifact_relative.name!r}"
            )

        if not isinstance(sidecar_path_text, str):
            errors.append(f"{artifact_path_text}: sidecar must be a string")
            continue
        if sidecar_path_text in seen_sidecars:
            errors.append(f"{sidecar_path_text}: duplicate sidecar path")
        seen_sidecars.add(sidecar_path_text)
        if not is_safe_relative_path(sidecar_path_text):
            errors.append(f"{sidecar_path_text}: unsafe sidecar path")
            continue

        artifact_path = input_dir / artifact_path_text
        sidecar_path = input_dir / sidecar_path_text
        if not is_plain_file(artifact_path):
            errors.append(f"{artifact_path_text}: missing artifact file")
            continue
        if not is_plain_file(sidecar_path):
            errors.append(f"{sidecar_path_text}: missing sidecar file")
            continue
        actual_size = artifact_path.stat().st_size
        if actual_size <= 0:
            errors.append(f"{artifact_path_text}: empty artifacts are not allowed")
        if sidecar_path != Path(f"{artifact_path}.sha256"):
            errors.append(
                f"{artifact_path_text}: sidecar path {sidecar_path_text!r} "
                f"does not match sibling {artifact_path.name}.sha256"
            )

        actual_digest = digest_file(artifact_path)
        if expected_digest != actual_digest:
            errors.append(
                f"{artifact_path_text}: digest mismatch "
                f"{expected_digest!r} != {actual_digest}"
            )
        if expected_size != actual_size:
            errors.append(
                f"{artifact_path_text}: size mismatch "
                f"{expected_size!r} != {actual_size}"
            )

        try:
            sidecar_digest, sidecar_filename = parse_sidecar(sidecar_path)
        except ValueError as error:
            errors.append(f"{sidecar_path_text}: {error}")
            continue
        if sidecar_digest != actual_digest:
            errors.append(
                f"{sidecar_path_text}: sidecar digest mismatch "
                f"{sidecar_digest} != {actual_digest}"
            )
        if sidecar_filename != artifact_path.name:
            errors.append(
                f"{sidecar_path_text}: sidecar filename mismatch "
                f"{sidecar_filename!r} != {artifact_path.name!r}"
            )

    manifest_resolved = manifest_path.resolve()
    manifest_sidecar_resolved = Path(f"{manifest_path}.sha256").resolve()
    actual_artifacts: set[str] = set()
    actual_sidecars: set[str] = set()
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink():
            relative = path.relative_to(input_dir).as_posix()
            if path.suffix == ".sha256":
                actual_sidecars.add(relative)
            else:
                actual_artifacts.add(relative)
            continue
        resolved = path.resolve()
        if resolved == manifest_resolved or resolved == manifest_sidecar_resolved:
            continue
        relative = path.relative_to(input_dir).as_posix()
        if path.suffix == ".sha256":
            actual_sidecars.add(relative)
        else:
            actual_artifacts.add(relative)

    missing_from_manifest = sorted(actual_artifacts - seen_paths)
    if missing_from_manifest:
        errors.append(
            "artifacts missing from release manifest: "
            + ", ".join(missing_from_manifest)
        )

    orphan_sidecars = sorted(actual_sidecars - seen_sidecars)
    if orphan_sidecars:
        errors.append(
            "sidecars missing from release manifest: " + ", ".join(orphan_sidecars)
        )

    if errors:
        print(
            "release manifest validation failed:\n"
            + "\n".join(f"  {error}" for error in errors),
            file=sys.stderr,
        )
        return 70

    print(f"release manifest validated: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

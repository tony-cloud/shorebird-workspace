#!/usr/bin/env python3
"""Write a JSON manifest for CI release artifacts.

Every non-sidecar file under the input directory must have a sibling
`<file>.sha256` sidecar in the format written by scripts/write_sha256.py:

  <hex sha256>  <basename>
"""

from __future__ import annotations

import argparse
import fnmatch
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
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-sha", default="")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="GLOB",
        help="Require at least one artifact path matching this glob. May be repeated.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir
    output_path = args.output

    if not input_dir.is_dir():
        print(f"missing input directory: {input_dir}", file=sys.stderr)
        return 66

    artifacts = []
    errors = []
    for artifact_path in sorted(path for path in input_dir.rglob("*") if path.is_file()):
        if artifact_path.is_symlink():
            errors.append(f"{artifact_path.relative_to(input_dir).as_posix()}: symlink artifacts are not allowed")
            continue
        if artifact_path.suffix == ".sha256":
            continue
        if artifact_path.resolve() == output_path.resolve():
            continue

        artifact_relative_path = artifact_path.relative_to(input_dir).as_posix()
        if not is_safe_relative_path(artifact_relative_path):
            errors.append(f"{artifact_relative_path}: unsafe artifact path")
            continue
        if artifact_path.stat().st_size <= 0:
            errors.append(f"{artifact_relative_path}: empty artifacts are not allowed")
            continue
        artifact_relative = PurePosixPath(artifact_relative_path)

        sidecar_path = Path(f"{artifact_path}.sha256")
        if not is_plain_file(sidecar_path):
            errors.append(f"{artifact_relative_path}: missing .sha256 sidecar")
            continue

        actual_digest = digest_file(artifact_path)
        try:
            sidecar_digest, sidecar_filename = parse_sidecar(sidecar_path)
        except ValueError as error:
            errors.append(f"{sidecar_path.relative_to(input_dir).as_posix()}: {error}")
            continue

        if sidecar_digest != actual_digest:
            errors.append(
                f"{sidecar_path.relative_to(input_dir).as_posix()}: digest mismatch "
                f"{sidecar_digest} != {actual_digest}"
            )
        if sidecar_filename != artifact_path.name:
            errors.append(
                f"{sidecar_path.relative_to(input_dir).as_posix()}: filename mismatch "
                f"{sidecar_filename!r} != {artifact_path.name!r}"
            )

        artifacts.append(
            {
                "path": artifact_relative_path,
                "artifact_group": artifact_relative.parts[0],
                "filename": artifact_path.name,
                "sha256": actual_digest,
                "size": artifact_path.stat().st_size,
                "sidecar": sidecar_path.relative_to(input_dir).as_posix(),
            }
        )

    orphan_sidecars = []
    for sidecar_path in sorted(input_dir.rglob("*.sha256")):
        artifact_path = Path(str(sidecar_path)[: -len(".sha256")])
        if sidecar_path.is_symlink():
            orphan_sidecars.append(sidecar_path.relative_to(input_dir).as_posix())
        elif not is_plain_file(artifact_path):
            orphan_sidecars.append(sidecar_path.relative_to(input_dir).as_posix())
    if orphan_sidecars:
        errors.append(f"orphan .sha256 sidecars: {', '.join(orphan_sidecars)}")

    artifact_paths = [artifact["path"] for artifact in artifacts]
    for required_glob in args.require:
        if not any(fnmatch.fnmatchcase(path, required_glob) for path in artifact_paths):
            errors.append(f"missing required artifact matching {required_glob!r}")

    if errors:
        print(
            "release manifest validation failed:\n"
            + "\n".join(f"  {error}" for error in errors),
            file=sys.stderr,
        )
        return 70

    manifest = {
        "format_version": 1,
        "github_sha": args.github_sha,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

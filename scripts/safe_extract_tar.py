#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(70)


def safe_member_name(member: tarfile.TarInfo) -> str | None:
    name = member.name
    if member.isdir():
        name = name.rstrip("/")
    if not name or name == ".":
        return None
    if "\\" in name or "\x00" in name:
        return None
    if any(ord(character) < 32 for character in name):
        return None

    candidate = PurePosixPath(name)
    if candidate.is_absolute():
        return None
    if any(part in ("", ".", "..") for part in candidate.parts):
        return None
    if candidate.parts and ":" in candidate.parts[0]:
        return None
    return candidate.as_posix()


def validate_members(archive_path: Path, members: list[tarfile.TarInfo]) -> None:
    member_types: dict[str, str] = {}
    for member in members:
        safe_name = safe_member_name(member)
        if safe_name is None:
            fail(f"{archive_path}: unsafe archive member path {member.name!r}")
        if not (member.isdir() or member.isfile()):
            fail(f"{archive_path}: unsupported archive member type {member.name!r}")

        member_type = "dir" if member.isdir() else "file"
        previous_type = member_types.get(safe_name)
        if previous_type is None:
            member_types[safe_name] = member_type
            continue
        if previous_type != "dir" or member_type != "dir":
            fail(f"{archive_path}: duplicate archive member path {member.name!r}")


def ensure_within_root(archive_path: Path, extract_root: Path, target: Path, name: str) -> None:
    try:
        target.relative_to(extract_root)
    except ValueError:
        fail(f"{archive_path}: archive member escapes extraction root {name!r}")


def extract_safe_tar_archive(archive_path: Path, extract_dir: Path) -> None:
    extract_root = extract_dir.resolve()
    extracted_files: set[Path] = set()

    try:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            validate_members(archive_path, members)

            for member in members:
                safe_name = safe_member_name(member)
                assert safe_name is not None
                target = (extract_root / safe_name).resolve()
                ensure_within_root(archive_path, extract_root, target, member.name)

                if member.isdir():
                    if target in extracted_files:
                        fail(f"{archive_path}: directory collides with file {member.name!r}")
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                for parent in target.parents:
                    if parent == extract_root:
                        break
                    if parent in extracted_files:
                        fail(f"{archive_path}: file parent collides with file {member.name!r}")

                if target.exists() and not target.is_file():
                    fail(f"{archive_path}: file collides with directory {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    fail(f"{archive_path}: unable to read archive member {member.name!r}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
                extracted_files.add(target)
    except tarfile.TarError as error:
        fail(f"{archive_path}: invalid tar archive: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely extract a tar archive containing only files and directories.",
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("extract_dir", type=Path)
    args = parser.parse_args()

    args.extract_dir.mkdir(parents=True, exist_ok=True)
    extract_safe_tar_archive(args.archive, args.extract_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

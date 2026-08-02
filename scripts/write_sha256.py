#!/usr/bin/env python3
"""Write a sha256 sidecar in the common `digest  filename` format."""

from __future__ import annotations

import hashlib
import pathlib
import sys


def digest_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: write_sha256.py <artifact> [output]", file=sys.stderr)
        return 64

    artifact_path = pathlib.Path(sys.argv[1])
    output_path = (
        pathlib.Path(sys.argv[2])
        if len(sys.argv) == 3
        else pathlib.Path(f"{artifact_path}.sha256")
    )

    if not artifact_path.is_file():
        print(f"missing artifact: {artifact_path}", file=sys.stderr)
        return 66

    output_path.write_text(
        f"{digest_file(artifact_path)}  {artifact_path.name}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

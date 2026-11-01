#!/usr/bin/env python3
"""Write/verify the CLI's coherent toolchain pair from the root Flutter gitlink."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


REVISION = re.compile(r'[0-9a-f]{40}')


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(['git', '-C', str(directory), *arguments], check=True,
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def source_pins(workspace: Path) -> dict[str, str]:
    entry = git(workspace, 'ls-tree', 'HEAD', '--', 'flutter').split()
    if (len(entry) != 4 or entry[:2] != ['160000', 'commit'] or
            entry[3] != 'flutter' or not REVISION.fullmatch(entry[2])):
        raise ValueError('Root HEAD must pin Flutter as a gitlink')
    flutter = workspace / 'flutter'
    revision = git(flutter, 'rev-parse', 'HEAD')
    if revision != entry[2]:
        raise ValueError('Checked-out Flutter HEAD differs from the root Flutter gitlink')
    engine = git(flutter, 'show', f'{revision}:bin/internal/engine.version')
    if not REVISION.fullmatch(engine):
        raise ValueError('Pinned Flutter engine.version must contain one full revision')
    if (flutter / 'bin/internal/engine.version').read_text().strip() != engine:
        raise ValueError('Flutter engine.version differs from its checked-out revision')
    return {'flutter_revision': revision, 'engine_revision': engine}


def write_pins(workspace: Path, package: Path) -> dict[str, str]:
    pins = source_pins(workspace)
    internal = package / 'bin/internal'
    internal.mkdir(parents=True, exist_ok=True)
    for name, revision in pins.items():
        (internal / name.replace('_revision', '.version')).write_text(revision + '\n')
    return pins


def verify_pins(workspace: Path, package: Path) -> dict[str, str]:
    pins = source_pins(workspace)
    for name, revision in pins.items():
        path = package / 'bin/internal' / name.replace('_revision', '.version')
        if path.read_text() != revision + '\n':
            raise ValueError(f'Packaged {path.name} differs from the root Flutter toolchain')
    manifest = json.loads((package / 'manifest.json').read_text())
    if not isinstance(manifest, dict) or any(manifest.get(name) != revision
                                             for name, revision in pins.items()):
        raise ValueError('CLI manifest differs from the root Flutter toolchain')
    return pins


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('write', 'verify'))
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    try:
        operation = write_pins if args.mode == 'write' else verify_pins
        pins = operation(args.workspace.resolve(), args.package.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'CLI toolchain pins failed: {error}\n')
    print(f'CLI toolchain pins {args.mode} ok: '
          f'Flutter {pins["flutter_revision"]}, engine {pins["engine_revision"]}')


if __name__ == '__main__':
    main()

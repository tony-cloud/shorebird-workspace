#!/usr/bin/env python3
"""Read, write and check the custom Dart SDK bootstrap lock (contract v1).

`flutter/bin/internal/shorebird_dart_sdk.pins` is the bootstrap trust path of
docs/HARDENING_CONTRACTS.md section 8. `update_dart_sdk.sh` and
`update_dart_sdk.ps1` read it before any SDK runs, so it is a line format that
both shells parse without a JSON tool:

    schema open-shorebird.dart-sdk-lock
    contract_version 1
    engine_revision <40 hex>        # must equal bin/internal/engine.version
    dart_sdk_revision <40 hex>      # must equal DEPS dart_sdk_revision
    base_url <https URL>            # immutable release; only with assets
    asset <zip name> <sha256> <size>

A lock with no `base_url` and no `asset` lines is *unpublished*: the identity is
pinned but no verified archive exists yet, so the default bootstrap stops with
a precise error instead of fetching anything.

Usage:
  dart_sdk_lock.py check FLUTTER_ROOT
  dart_sdk_lock.py write-unpublished FLUTTER_ROOT
  dart_sdk_lock.py write-published FLUTTER_ROOT --base-url URL --assets-dir DIR [--output FILE]
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys

SCHEMA = 'open-shorebird.dart-sdk-lock'
LOCK_PATH = Path('bin') / 'internal' / 'shorebird_dart_sdk.pins'
ASSETS = tuple(f'dart-sdk-{host}.zip' for host in (
    'darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'windows-arm64', 'windows-x64'))
_REVISION = re.compile(r'^[0-9a-f]{40}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_BASE_URL = re.compile(r'^https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~%+-]+)+$')
HEADER = (
    '# Open Shorebird custom Dart SDK bootstrap lock, contract v1.\n'
    '# Read by update_dart_sdk.{sh,ps1} before the SDK runs. Written by\n'
    '# shorebird-workspace scripts/dart_sdk_lock.py; do not edit by hand.\n'
)


class LockError(ValueError):
    pass


def parse(text: str) -> dict:
    lock: dict = {'assets': {}}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        key = fields[0]
        if key == 'asset':
            if len(fields) != 4:
                raise LockError(f'line {number}: asset takes <name> <sha256> <size>')
            name, digest, size = fields[1:]
            if name not in ASSETS:
                raise LockError(f'line {number}: unknown asset {name}')
            if name in lock['assets']:
                raise LockError(f'line {number}: duplicate asset {name}')
            if not _SHA256.match(digest) or not size.isdigit() or int(size) <= 0:
                raise LockError(f'line {number}: asset needs a lowercase sha256 and a positive size')
            lock['assets'][name] = {'sha256': digest, 'size': int(size)}
            continue
        if len(fields) != 2:
            raise LockError(f'line {number}: {key} takes exactly one value')
        if key in lock:
            raise LockError(f'line {number}: duplicate {key}')
        if key not in ('schema', 'contract_version', 'engine_revision', 'dart_sdk_revision', 'base_url'):
            raise LockError(f'line {number}: unknown field {key}')
        lock[key] = fields[1]
    if lock.get('schema') != SCHEMA or lock.get('contract_version') != '1':
        raise LockError(f'lock must declare schema {SCHEMA} and contract_version 1')
    for key in ('engine_revision', 'dart_sdk_revision'):
        if not _REVISION.match(lock.get(key, '')):
            raise LockError(f'{key} must be a full 40-hex revision')
    base_url = lock.get('base_url')
    if (base_url is None) != (not lock['assets']):
        raise LockError('base_url and asset lines appear together (published) or not at all (unpublished)')
    if base_url is not None:
        if not _BASE_URL.match(base_url):
            raise LockError('base_url must be an https URL without query, fragment or trailing slash')
        if '/latest' in base_url:
            raise LockError('base_url must name an immutable release, never a mutable "latest" alias')
    return lock


def render(lock: dict) -> str:
    lines = [HEADER.rstrip('\n'),
             f'schema {SCHEMA}',
             'contract_version 1',
             f'engine_revision {lock["engine_revision"]}',
             f'dart_sdk_revision {lock["dart_sdk_revision"]}']
    if lock.get('base_url'):
        lines.append(f'base_url {lock["base_url"]}')
        for name in sorted(lock['assets']):
            asset = lock['assets'][name]
            lines.append(f'asset {name} {asset["sha256"]} {asset["size"]}')
    text = '\n'.join(lines) + '\n'
    parse(text)
    return text


def checkout_identity(flutter_root: Path) -> tuple[str, str]:
    engine = (flutter_root / 'bin' / 'internal' / 'engine.version').read_text(encoding='utf-8').strip()
    deps = (flutter_root / 'DEPS').read_text(encoding='utf-8')
    match = re.search(r'"dart_sdk_revision":\s*"([0-9a-f]{40})"', deps)
    if match is None:
        raise LockError(f'{flutter_root}/DEPS has no dart_sdk_revision pin')
    return engine, match.group(1)


def check(flutter_root: Path) -> dict:
    lock = parse((flutter_root / LOCK_PATH).read_text(encoding='utf-8'))
    engine, dart = checkout_identity(flutter_root)
    if lock['engine_revision'] != engine:
        raise LockError(f'lock engine_revision {lock["engine_revision"]} != engine.version {engine}')
    if lock['dart_sdk_revision'] != dart:
        raise LockError(f'lock dart_sdk_revision {lock["dart_sdk_revision"]} != DEPS dart_sdk_revision {dart}')
    return lock


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    check_parser = sub.add_parser('check')
    check_parser.add_argument('flutter_root', type=Path)
    unpublished = sub.add_parser('write-unpublished')
    unpublished.add_argument('flutter_root', type=Path)
    published = sub.add_parser('write-published')
    published.add_argument('flutter_root', type=Path)
    published.add_argument('--base-url', required=True)
    published.add_argument('--assets-dir', type=Path, required=True)
    published.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            lock = check(args.flutter_root)
            state = f'published ({len(lock["assets"])} assets)' if lock['assets'] else 'unpublished'
            print(f'{args.flutter_root / LOCK_PATH}: {state}, matches engine.version and DEPS')
            return 0
        engine, dart = checkout_identity(args.flutter_root)
        lock = {'engine_revision': engine, 'dart_sdk_revision': dart, 'assets': {}}
        if args.command == 'write-published':
            lock['base_url'] = args.base_url.rstrip('/')
            for name in ASSETS:
                path = args.assets_dir / name
                if path.is_file():
                    digest, size = _digest(path)
                    lock['assets'][name] = {'sha256': digest, 'size': size}
            if not lock['assets']:
                raise LockError(f'no dart-sdk-*.zip assets in {args.assets_dir}')
        output = getattr(args, "output", None) or args.flutter_root / LOCK_PATH
        output.write_text(render(lock), encoding='utf-8')
        print(f'{output}: wrote {"published" if lock["assets"] else "unpublished"} lock')
    except (LockError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

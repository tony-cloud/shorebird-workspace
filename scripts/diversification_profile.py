#!/usr/bin/env python3
"""Generate and check `hardened-diversified` profiles (contract v3, P5).

See docs/HARDENING_CONTRACTS.md section 11. A diversification profile is a
build input of one native-release lineage. Contract v3 defines one
subprofile, the Dart bytecode opcode permutation:

* a random 32-byte seed is generated once per lineage and kept in a private
  seed record (never committed, never shipped);
* the profile is derived from the seed deterministically
  (`open-shorebird.diversification.v1`): HMAC-SHA256 counter-mode bytes drive
  an unbiased Fisher-Yates shuffle of the wire bytes of the public opcodes.
  `Trap` keeps wire byte 0, so zero-filled memory still traps; VM-internal
  opcodes keep their canonical values and are rejected in files anyway;
* `profile_id` is `sha256:` over the canonical JSON of the profile. It is
  not secret and is what releases, manifests, envelopes and bytecode headers
  record. The profile itself (the map) is a build input of the engine and of
  dart2bytecode; it is kept with the release records, not in the app.

Subcommands:
  new       create a seed record and its profile in a private directory
  derive    re-derive the profile from a seed record (reproduction, recovery)
  id        print a profile's id
  verify    check a profile (and optionally its id, the opcode table, --production)
  gn-args   print the engine GN args and toolchain_security.json fields
  encode    print the encoded profile (the dart_bytecode_profile GN value) only

The opcode table is the PUBLIC_KERNEL_BYTECODES_LIST of
dart-sdk/runtime/vm/constants_kbc.h; dart2bytecode's `Opcode` enum and the
VM's GN generator (dart-sdk/runtime/tools/bytecode_profile.py) use the same
names, and tests pin all three together.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_hardening_contracts import ContractError, canonical_json, schema_errors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / 'docs' / 'schemas'
TESTDATA = ROOT / 'scripts' / 'testdata' / 'diversification'
CONSTANTS_KBC = ROOT / 'dart-sdk' / 'runtime' / 'vm' / 'constants_kbc.h'

CONTRACT_VERSION = 3
PROFILE_SCHEMA = 'open-shorebird.diversification-profile'
SEED_SCHEMA = 'open-shorebird.diversification-seed'
DERIVATION = 'open-shorebird.diversification.v1'
BYTECODE_SCHEME = 'opcode-permutation-v1'
BYTECODE_FORMAT_VERSION = 3
OPCODE_SUBKEY_LABEL = b'open-shorebird.diversification.v1/bytecode-opcode-permutation'
SEED_BYTES = 32
# A derived permutation has about one fixed point besides Trap. A profile
# with this many is hand-made or degenerate, and refused.
MAX_FIXED_POINTS = 16
# Encoded (base64) profile size limit shared by GN, build.rs and tooling.
MAX_ENCODED_PROFILE = 8192
PROFILE_ID_RE = re.compile(r'^sha256:[0-9a-f]{64}$')

DEVELOPMENT_SEED_RECORD = TESTDATA / 'seed.DEVELOPMENT-ONLY.json'
DEVELOPMENT_PROFILE = TESTDATA / 'profile.DEVELOPMENT-ONLY.json'


class ProfileError(ContractError):
    pass


# ---------------------------------------------------------------------------
# Opcode table


def opcode_names(constants_h: Path = CONSTANTS_KBC) -> list[str]:
    """The public opcode names, in semantic (enum) order."""
    text = constants_h.read_text(encoding='utf-8')
    match = re.search(r'#define PUBLIC_KERNEL_BYTECODES_LIST\(V\)(.*?)\n\n', text, re.S)
    if match is None:
        raise ProfileError(f'{constants_h}: PUBLIC_KERNEL_BYTECODES_LIST not found')
    names = re.findall(r'V\((\w+),', match.group(1))
    if not names or names[0] != 'Trap' or len(names) > 256 or len(set(names)) != len(names):
        raise ProfileError(f'{constants_h}: unexpected public opcode list')
    return names


def opcode_table_sha256(names: list[str]) -> str:
    return hashlib.sha256('\n'.join(names).encode('ascii')).hexdigest()


# ---------------------------------------------------------------------------
# Derivation


class _Stream:
    """HMAC-SHA256(subkey, uint32_be(counter)) blocks, consumed 4 bytes at a time."""

    def __init__(self, subkey: bytes) -> None:
        self._subkey = subkey
        self._counter = 0
        self._buffer = b''

    def uint32(self) -> int:
        if len(self._buffer) < 4:
            self._buffer += hmac.new(self._subkey, self._counter.to_bytes(4, 'big'), hashlib.sha256).digest()
            self._counter += 1
        value, self._buffer = int.from_bytes(self._buffer[:4], 'big'), self._buffer[4:]
        return value

    def below(self, bound: int) -> int:
        """Unbiased integer in [0, bound) by rejection sampling."""
        limit = (1 << 32) - ((1 << 32) % bound)
        while True:
            value = self.uint32()
            if value < limit:
                return value % bound


def derive_wire_opcodes(seed: bytes, count: int) -> list[int]:
    if len(seed) != SEED_BYTES:
        raise ProfileError(f'seed must be {SEED_BYTES} bytes')
    if not 2 <= count <= 256:
        raise ProfileError('opcode count must be in [2, 256]')
    stream = _Stream(hmac.new(seed, OPCODE_SUBKEY_LABEL, hashlib.sha256).digest())
    wires = list(range(1, count))
    for i in range(len(wires) - 1, 0, -1):
        j = stream.below(i + 1)
        wires[i], wires[j] = wires[j], wires[i]
    return [0] + wires


def derive_profile(seed: bytes, names: list[str]) -> dict:
    return {
        'schema': PROFILE_SCHEMA,
        'contract_version': CONTRACT_VERSION,
        'bytecode': {
            'scheme': BYTECODE_SCHEME,
            'format_version': BYTECODE_FORMAT_VERSION,
            'opcode_table_sha256': opcode_table_sha256(names),
            'wire_opcodes': derive_wire_opcodes(seed, len(names)),
        },
        'native_layout': None,
    }


def profile_id(profile: dict) -> str:
    return 'sha256:' + hashlib.sha256(canonical_json(profile)).hexdigest()


def encode_profile(profile: dict) -> str:
    """The GN/build.rs form: base64 of the canonical profile bytes."""
    return base64.b64encode(canonical_json(profile)).decode('ascii')


# ---------------------------------------------------------------------------
# Validation


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding='utf-8'))


def validate_profile(profile: Any, names: list[str] | None = None) -> list[str]:
    schema = _schema('diversification-profile.v3.schema.json')
    errors = schema_errors(profile, schema, schema)
    if errors:
        return errors
    wires = profile['bytecode']['wire_opcodes']
    count = len(wires)
    if count > 256:
        return ['$.bytecode.wire_opcodes: at most 256 opcodes fit in one byte']
    if sorted(wires) != list(range(count)):
        errors.append('$.bytecode.wire_opcodes: must be a permutation of 0..N-1')
        return errors
    if wires[0] != 0:
        errors.append('$.bytecode.wire_opcodes[0]: Trap must keep wire byte 0')
    fixed = sum(1 for semantic, wire in enumerate(wires) if semantic and semantic == wire)
    if fixed >= MAX_FIXED_POINTS:
        errors.append(f'$.bytecode.wire_opcodes: {fixed} fixed points; a derived profile has about one '
                      f'(at most {MAX_FIXED_POINTS - 1} allowed)')
    if names is not None:
        if count != len(names):
            errors.append(f'$.bytecode.wire_opcodes: {count} entries, the opcode table has {len(names)}')
        if profile['bytecode']['opcode_table_sha256'] != opcode_table_sha256(names):
            errors.append('$.bytecode.opcode_table_sha256: does not match this toolchain\'s opcode table')
    return errors


def validate_seed_record(record: Any) -> list[str]:
    schema = _schema('diversification-seed.v3.schema.json')
    errors = schema_errors(record, schema, schema)
    if errors:
        return errors
    try:
        seed = base64.b64decode(record['seed'], validate=True)
    except ValueError:
        return ['$.seed: not base64']
    if len(seed) != SEED_BYTES or base64.b64encode(seed).decode('ascii') != record['seed']:
        errors.append(f'$.seed: must be canonical base64 of {SEED_BYTES} bytes')
    return errors


def decode_profile(encoded: str) -> dict:
    """Parse the GN/build.rs form; the bytes must be canonical JSON."""
    if len(encoded) > MAX_ENCODED_PROFILE:
        raise ProfileError('encoded profile is too large')
    try:
        raw = base64.b64decode(encoded, validate=True)
        profile = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeDecodeError) as error:
        raise ProfileError(f'encoded profile is not base64 canonical JSON: {error}') from error
    if base64.b64encode(raw).decode('ascii') != encoded or canonical_json(profile) != raw:
        raise ProfileError('encoded profile is not base64 of canonical JSON')
    return profile


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate key {key}')
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_no_duplicates)
    except (OSError, ValueError) as error:
        raise ProfileError(f'{path}: {error}') from error


def development_profile_ids() -> set[str]:
    """IDs that production tooling refuses (the published test profile)."""
    return {profile_id(load_json(DEVELOPMENT_PROFILE))}


def profile_from_seed_record(record: dict, names: list[str]) -> dict:
    errors = validate_seed_record(record)
    if errors:
        raise ProfileError('; '.join(errors))
    profile = derive_profile(base64.b64decode(record['seed']), names)
    if profile_id(profile) != record['profile_id']:
        raise ProfileError('seed record profile_id does not match the derivation; '
                           'the opcode table or derivation changed (a new native release needs a new profile)')
    return profile


def check(profile: Any, *, names: list[str] | None, expect_id: str | None = None,
          production: bool = False) -> str:
    errors = validate_profile(profile, names)
    if errors:
        raise ProfileError('; '.join(errors))
    identifier = profile_id(profile)
    if expect_id is not None and identifier != expect_id:
        raise ProfileError(f'profile id {identifier} does not match the expected {expect_id}')
    if production and identifier in development_profile_ids():
        raise ProfileError('production refuses the DEVELOPMENT-ONLY diversification profile')
    return identifier


# ---------------------------------------------------------------------------
# Private directory handling


def _inside_git_work_tree(path: Path) -> bool:
    probe = path if path.is_dir() else path.parent
    while not probe.exists():
        probe = probe.parent
    result = subprocess.run(['git', '-C', str(probe), 'rev-parse', '--is-inside-work-tree'],
                            capture_output=True, text=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == 'true'


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def _pretty(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode('utf-8')


# ---------------------------------------------------------------------------
# CLI


def _names(args: argparse.Namespace) -> list[str]:
    return opcode_names(Path(args.constants))


def cmd_new(args: argparse.Namespace) -> int:
    out = Path(args.output_dir).resolve()
    if not args.development and _inside_git_work_tree(out):
        raise ProfileError(f'{out} is inside a git work tree; keep the seed in a private directory')
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed_path, profile_path = out / 'diversification-seed.json', out / 'diversification-profile.json'
    if seed_path.exists() or profile_path.exists():
        raise ProfileError(f'{out} already holds a profile; a lineage keeps its first seed')
    names = _names(args)
    seed = os.urandom(SEED_BYTES)
    profile = derive_profile(seed, names)
    identifier = check(profile, names=names)
    record = {
        'schema': SEED_SCHEMA,
        'contract_version': CONTRACT_VERSION,
        'derivation': DERIVATION,
        'seed': base64.b64encode(seed).decode('ascii'),
        'profile_id': identifier,
    }
    _write_private(seed_path, _pretty(record))
    _write_private(profile_path, _pretty(profile))
    print(identifier)
    return 0


def cmd_derive(args: argparse.Namespace) -> int:
    names = _names(args)
    profile = profile_from_seed_record(load_json(Path(args.seed_record)), names)
    data = _pretty(profile)
    if args.output:
        _write_private(Path(args.output), data)
    else:
        sys.stdout.write(data.decode('utf-8'))
    return 0


def cmd_id(args: argparse.Namespace) -> int:
    print(profile_id(load_json(Path(args.profile))))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    names = None if args.no_opcode_table else _names(args)
    identifier = check(load_json(Path(args.profile)), names=names, expect_id=args.expect_id,
                       production=args.production)
    print(f'{args.profile}: valid diversification profile {identifier}')
    return 0


def cmd_gn_args(args: argparse.Namespace) -> int:
    names = _names(args)
    profile = load_json(Path(args.profile))
    identifier = check(profile, names=names, production=args.production)
    print('# engine GN args (with the hardened args of section 10.4)')
    print('shorebird_security_profile = "hardened-diversified"')
    print(f'dart_bytecode_profile = "{encode_profile(profile)}"')
    print('# toolchain_security.json (contract v3)')
    print(json.dumps({'security_profile': 'hardened-diversified', 'diversification_profile_id': identifier},
                     sort_keys=True))
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    profile = load_json(Path(args.profile))
    check(profile, names=_names(args), production=args.production)
    print(encode_profile(profile))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--constants', default=str(CONSTANTS_KBC),
                        help='constants_kbc.h that defines the opcode table')
    sub = parser.add_subparsers(dest='command', required=True)
    new = sub.add_parser('new', help='create a seed record and profile')
    new.add_argument('--output-dir', required=True)
    new.add_argument('--development', action='store_true',
                     help='allow a directory inside a git work tree (tests only)')
    new.set_defaults(func=cmd_new)
    derive = sub.add_parser('derive', help='re-derive a profile from its seed record')
    derive.add_argument('seed_record')
    derive.add_argument('--output')
    derive.set_defaults(func=cmd_derive)
    ident = sub.add_parser('id', help='print a profile id')
    ident.add_argument('profile')
    ident.set_defaults(func=cmd_id)
    verify = sub.add_parser('verify', help='check a profile')
    verify.add_argument('profile')
    verify.add_argument('--expect-id')
    verify.add_argument('--production', action='store_true')
    verify.add_argument('--no-opcode-table', action='store_true',
                        help='skip the check against constants_kbc.h')
    verify.set_defaults(func=cmd_verify)
    gn = sub.add_parser('gn-args', help='print engine GN args and toolchain fields')
    gn.add_argument('profile')
    gn.add_argument('--production', action='store_true')
    gn.set_defaults(func=cmd_gn_args)
    encode = sub.add_parser('encode', help='print the dart_bytecode_profile GN value')
    encode.add_argument('profile')
    encode.add_argument('--production', action='store_true')
    encode.set_defaults(func=cmd_encode)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProfileError, ContractError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

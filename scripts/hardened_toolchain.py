#!/usr/bin/env python3
"""Inputs and outputs of a hardened or hardened-diversified toolchain build (P6).

docs/HARDENING_CONTRACTS.md section 12. One description of a toolchain build
drives every consumer, so the engine GN args, the `toolchain_security.json`
the CLI reads and the artifact catalog can never disagree:

* `toolchain_id` (section 3): the checkout's fork revisions, the engine
  revision and the profile (contract v1 for `hardened`, v3 with the profile
  ID for `hardened-diversified`).
* The release-root pin and delegations floor (section 10.4), from, in order:
  `--release-root` / `OPEN_SHOREBIRD_RELEASE_ROOT` (the production pin, a CI
  variable), `--release-pins` (`release-pins/release-pins.json` written by
  `provision_release_keys.sh sync --write-repo-pins`), or the committed
  DEVELOPMENT-ONLY root with `--development`.
* For `hardened-diversified`, the lineage's diversification profile
  (section 11.2): `--diversification-profile` /
  `OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE` (a path or the profile JSON), or
  the committed DEVELOPMENT-ONLY profile with `--development`.

A toolchain that uses any DEVELOPMENT-ONLY input is a development toolchain:
its engine is built with `shorebird_security_development_exceptions = true`,
its `toolchain_security.json` says so, and `shorebird release` refuses it.
`--production` refuses every DEVELOPMENT-ONLY input instead.

Subcommands:
  describe            print the description (JSON) of the toolchain
  gn-args             print the engine `--gn-args` fragment (empty for legacy;
                      empty for a non-interpreter engine of a diversified
                      toolchain, whose platforms the CLI refuses)
  toolchain-security  write `toolchain_security.json` (v2 hardened, v3
                      diversified); legacy writes nothing
  verify-args         check a built engine's args.gn against the description
  github-env          print KEY=VALUE lines for $GITHUB_ENV
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diversification_profile as dp  # noqa: E402
import release_trust as rt  # noqa: E402
from artifact_catalog import CatalogError, toolchain_id_from_checkout  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('legacy', 'hardened', 'hardened-diversified')
DEVELOPMENT_ROOT = rt.TESTDATA / 'root.v1.DEVELOPMENT-ONLY.json'
TOOLCHAIN_SCHEMA = 'open-shorebird.toolchain-security'
MAX_FLOOR = 1 << 31


class ToolchainError(Exception):
    pass


def _read_text_or_path(value: str) -> str:
    """A JSON document given inline or as a path."""
    stripped = value.strip()
    if stripped.startswith('{'):
        return stripped
    path = Path(stripped)
    if not path.is_file():
        raise ToolchainError(f'{value}: not a file and not inline JSON')
    return path.read_text(encoding='utf-8')


def _root_pin(args: argparse.Namespace) -> tuple[str, int, str]:
    """(base64 pin, min delegations version, source)."""
    pin = args.release_root or os.environ.get('OPEN_SHOREBIRD_RELEASE_ROOT', '').strip()
    floor = args.min_delegations_version
    if pin:
        source = 'release-root'
    elif args.release_pins:
        pins = json.loads(Path(args.release_pins).read_text(encoding='utf-8'))
        if pins.get('schema') != 'open-shorebird.release-pins':
            raise ToolchainError(f'{args.release_pins}: not a release-pins document')
        pin = pins['gn_args']['shorebird_release_root']
        floor = floor or pins['gn_args']['shorebird_release_min_delegations_version']
        source = 'release-pins'
    elif args.development:
        pin = rt.pin_fields(json.loads(DEVELOPMENT_ROOT.read_text(encoding='utf-8')))['release_root']
        source = 'development'
    else:
        raise ToolchainError('a hardened toolchain needs a release root: --release-root '
                             '(or OPEN_SHOREBIRD_RELEASE_ROOT), --release-pins, or --development')
    floor = floor or 1
    if not 1 <= floor < MAX_FLOOR:
        raise ToolchainError('--min-delegations-version must be a positive integer')
    try:
        data = base64.b64decode(pin, validate=True)
    except ValueError:
        raise ToolchainError('the release root pin is not base64') from None
    if base64.b64encode(data).decode('ascii') != pin:
        raise ToolchainError('the release root pin is not canonical base64')
    try:
        rt.parse_payload(data, 'release-root')
    except rt.TrustError as error:
        raise ToolchainError(f'the release root pin is invalid: {error}') from None
    return pin, floor, source


def _is_development_root(pin: str) -> bool:
    root = json.loads(base64.b64decode(pin))
    return bool(rt.development_root_key_ids() & {key['key_id'] for key in root['keys']})


def _diversification(args: argparse.Namespace) -> tuple[dict, str, str]:
    """(profile, profile ID, source)."""
    value = args.diversification_profile or os.environ.get('OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE', '')
    if value.strip():
        try:
            profile = json.loads(_read_text_or_path(value), object_pairs_hook=dp._no_duplicates)
        except (ValueError, dp.ProfileError) as error:
            raise ToolchainError(f'the diversification profile is not valid JSON: {error}') from None
        source = 'diversification-profile'
    elif args.development:
        profile = dp.load_json(dp.DEVELOPMENT_PROFILE)
        source = 'development'
    else:
        raise ToolchainError('a hardened-diversified toolchain needs --diversification-profile '
                             '(or OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE), or --development')
    names = None if args.no_opcode_table else dp.opcode_names(Path(args.constants))
    try:
        identifier = dp.check(profile, names=names)
    except dp.ProfileError as error:
        raise ToolchainError(f'the diversification profile is invalid: {error}') from None
    return profile, identifier, source


def describe(args: argparse.Namespace) -> dict:
    profile = args.profile
    if profile not in PROFILES:
        raise ToolchainError(f'unknown security profile {profile}')
    if profile == 'legacy':
        return {'security_profile': 'legacy', 'gn_args': '', 'toolchain_security': None,
                'development': False}
    pin, floor, root_source = _root_pin(args)
    development = _is_development_root(pin)
    diversification = None
    profile_id = None
    if profile == 'hardened-diversified':
        diversification, profile_id, profile_source = _diversification(args)
        development = development or profile_id in dp.development_profile_ids()
    elif args.diversification_profile:
        raise ToolchainError('--diversification-profile is only for hardened-diversified')
    if development and args.production:
        raise ToolchainError('--production refuses the DEVELOPMENT-ONLY release root and '
                             'diversification profile')
    if development and not args.development:
        raise ToolchainError('a DEVELOPMENT-ONLY root or profile builds a development toolchain; '
                             'pass --development to accept that')
    try:
        identity = args.toolchain_id or toolchain_id_from_checkout(Path(args.workspace_root), profile, profile_id)
    except CatalogError as error:
        raise ToolchainError(str(error)) from None
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
        raise ToolchainError('--toolchain-id must be sha256:<64 lowercase hex>')
    gn = {
        'shorebird_security_profile': profile,
        'shorebird_toolchain_id': identity,
        'shorebird_release_root': pin,
        'shorebird_release_min_delegations_version': floor,
    }
    if development:
        gn['shorebird_security_development_exceptions'] = True
    if diversification is not None:
        gn['dart_bytecode_profile'] = dp.encode_profile(diversification)
    security = {
        'schema': TOOLCHAIN_SCHEMA,
        'contract_version': 3 if profile_id else 2,
        'security_profile': profile,
        'toolchain_id': identity,
        'development_exceptions': development,
        'release_root': pin,
        'min_delegations_version': floor,
    }
    if profile_id:
        security['diversification_profile_id'] = profile_id
    description = {
        'security_profile': profile,
        'toolchain_id': identity,
        'development': development,
        'release_root_source': root_source,
        'release_root_sha256': 'sha256:' + hashlib.sha256(base64.b64decode(pin)).hexdigest(),
        'min_delegations_version': floor,
        'diversification_profile_id': profile_id,
        'gn': gn,
        'gn_args': gn_fragment(gn),
        'toolchain_security': security,
    }
    if profile_id:
        description['diversification_profile_source'] = profile_source
    return description


def _gn_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if not re.fullmatch(r'[A-Za-z0-9+/=:._-]*', value):
        raise ToolchainError(f'refusing to quote an unexpected GN value {value!r}')
    return f'"{value}"'


def gn_fragment(gn: dict) -> str:
    return ' '.join(f'{key}={_gn_value(value)}' for key, value in gn.items())


def engine_gn_args(description: dict, interpreter: bool) -> str:
    """The fragment for one engine build. A diversified toolchain diversifies
    only interpreter engines; its other engines stay legacy, and the CLI
    refuses every non-interpreter release on it (section 11.7)."""
    if description['security_profile'] == 'hardened-diversified' and not interpreter:
        return ''
    return description['gn_args']


def parse_args_gn(text: str) -> dict:
    """`key = value` assignments of an args.gn; `gn gen` wraps long values
    onto the next line, so a value runs until the next assignment."""
    lines = [line.split('#', 1)[0].rstrip() for line in text.splitlines()]
    values: dict[str, str] = {}
    key = None
    for line in lines:
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if match:
            key = match.group(1)
            values[key] = match.group(2).strip()
        elif key is not None and line.strip():
            values[key] = (values[key] + ' ' + line.strip()).strip()
    return values


def verify_args(description: dict, args_gn: str, interpreter: bool) -> list[str]:
    actual = parse_args_gn(args_gn)
    problems = []
    expected = description.get('gn') or {}
    if description['security_profile'] == 'hardened-diversified' and not interpreter:
        expected = {}
    for key, value in expected.items():
        if actual.get(key) != _gn_value(value):
            problems.append(f'{key} is {actual.get(key)!r}, expected {_gn_value(value)!r}')
    if not expected:
        for key in ('shorebird_security_profile', 'dart_bytecode_profile', 'shorebird_release_root'):
            if key in actual and actual[key] not in ('"legacy"', '""'):
                problems.append(f'a legacy engine must not set {key}')
    return problems


def toolchain_security_text(description: dict) -> str:
    return json.dumps(description['toolchain_security'], indent=2, sort_keys=True) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--profile', required=True, choices=PROFILES)
    parser.add_argument('--release-root', help='base64 release-root pin (default $OPEN_SHOREBIRD_RELEASE_ROOT)')
    parser.add_argument('--release-pins', help='release-pins/release-pins.json')
    parser.add_argument('--min-delegations-version', type=int, default=0)
    parser.add_argument('--diversification-profile',
                        help='diversification-profile.json, path or inline (default '
                             '$OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE)')
    parser.add_argument('--development', action='store_true',
                        help='allow (or default to) the DEVELOPMENT-ONLY root and profile')
    parser.add_argument('--production', action='store_true', help='refuse every DEVELOPMENT-ONLY input')
    parser.add_argument('--toolchain-id', help='override the checkout-derived toolchain ID (tests)')
    parser.add_argument('--workspace-root', default=str(ROOT))
    parser.add_argument('--constants', default=str(dp.CONSTANTS_KBC))
    parser.add_argument('--no-opcode-table', action='store_true', help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('describe')
    gn = sub.add_parser('gn-args')
    gn.add_argument('--engine', choices=('interpreter', 'native'), default='interpreter')
    sec = sub.add_parser('toolchain-security')
    sec.add_argument('--output', required=True)
    check = sub.add_parser('verify-args')
    check.add_argument('args_gn')
    check.add_argument('--engine', choices=('interpreter', 'native'), default='interpreter')
    sub.add_parser('github-env')
    args = parser.parse_args(argv)
    if args.development and args.production:
        parser.error('--development and --production are exclusive')
    try:
        description = describe(args)
        if args.command == 'describe':
            print(json.dumps(description, indent=2, sort_keys=True))
        elif args.command == 'gn-args':
            print(engine_gn_args(description, args.engine == 'interpreter'))
        elif args.command == 'toolchain-security':
            if description['toolchain_security'] is None:
                print('legacy toolchain: no toolchain_security.json is written')
                return 0
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(toolchain_security_text(description), encoding='utf-8')
            print(f'{args.output}: {description["security_profile"]} {description["toolchain_id"]}'
                  f'{" (DEVELOPMENT toolchain)" if description["development"] else ""}')
        elif args.command == 'verify-args':
            problems = verify_args(description, Path(args.args_gn).read_text(encoding='utf-8'),
                                   args.engine == 'interpreter')
            if problems:
                raise ToolchainError(f'{args.args_gn}: ' + '; '.join(problems))
            print(f'{args.args_gn}: matches the {description["security_profile"]} toolchain')
        else:
            print(f'SHOREBIRD_SECURITY_PROFILE_BUILD={description["security_profile"]}')
            print(f'SHOREBIRD_SECURITY_TOOLCHAIN_ID={description.get("toolchain_id") or ""}')
            print(f'SHOREBIRD_SECURITY_DEVELOPMENT={"true" if description["development"] else "false"}')
            print(f'SHOREBIRD_SECURITY_GN_ARGS={engine_gn_args(description, True)}')
            print(f'SHOREBIRD_SECURITY_NATIVE_GN_ARGS={engine_gn_args(description, False)}')
            print(f'SHOREBIRD_DIVERSIFICATION_PROFILE_ID={description.get("diversification_profile_id") or ""}')
    except (ToolchainError, OSError, ValueError, KeyError, rt.TrustError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

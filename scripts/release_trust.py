#!/usr/bin/env python3
"""Create, rotate and verify the Open Shorebird release root of trust (contract v2).

See docs/HARDENING_CONTRACTS.md section 10. The release root is an offline
Ed25519 key set (k-of-n). It signs two kinds of metadata:

* `release-root` documents. Version 1 is signed by its own keys. Version N+1
  is signed by a threshold of version N's keys and a threshold of its own, so
  a holder of the pinned root can follow a planned rotation.
* `release-delegations` documents, which name the keys of each signing role
  (`build-manifest`, `artifact-catalog`), their validity windows, and every
  revoked key. A key belongs to exactly one role and is never a root key.

Each document travels in an envelope:

    {"payload": base64(payload bytes), "signatures": [{"key_id", "signature"}]}

A signature covers `<domain> + <lowercase hex SHA-256 of the payload bytes>`,
with one domain per document type, so a root signature is never a
delegations signature. Payloads are canonical JSON (contract section 1).

A `release-trust` bundle carries the root chain and the current delegations.
Hardened builds embed it in the build manifest line of shorebird.yaml; the
engine compiles the root pin (`shorebird_release_root`, the base64 payload of
one root version) and a delegations version floor.

Subcommands:
  root        sign a root version (self-signed, or cross-signed with --previous)
  delegate    sign a delegations version under a root
  bundle      assemble a release-trust bundle
  pin         print the compiled pin (GN args, toolchain_security.json fields)
  verify      verify a bundle against a pin (optionally a role key at a time)
  sign        reference external signer (the KMS/HSM interface) backed by a PEM:
              `sign --key-id ID --message TEXT` prints the base64 signature
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from artifact_catalog import (  # noqa: E402
    SIGNATURE_DOMAIN as CATALOG_SIGNATURE_DOMAIN,
    CatalogError,
    _decompress,
    ed25519_public_key,
    ed25519_sign,
    ed25519_verify,
    key_id,
    load_private_seed,
    load_public_key,
    strict_loads,
)
from validate_hardening_contracts import ContractError, canonical_json, schema_errors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / 'docs' / 'schemas'
TESTDATA = ROOT / 'scripts' / 'testdata' / 'release-trust'
CONTRACT_VERSION = 2

ROOT_SCHEMA = 'open-shorebird.release-root'
DELEGATIONS_SCHEMA = 'open-shorebird.release-delegations'
TRUST_SCHEMA = 'open-shorebird.release-trust'
ROOT_DOMAIN = b'open-shorebird.release-root-signature.v2:'
DELEGATIONS_DOMAIN = b'open-shorebird.release-delegations-signature.v2:'
MANIFEST_DOMAIN = b'open-shorebird.build-manifest-signature.v2:'
ROLES = ('build-manifest', 'artifact-catalog')
# What each role's key may sign. The reference signer refuses anything else,
# and a KMS/HSM adapter should apply the same rule in its key policy.
ROLE_DOMAINS = {
    'build-manifest': MANIFEST_DOMAIN,
    'artifact-catalog': CATALOG_SIGNATURE_DOMAIN,
}
MAX_ROOT_CHAIN = 16
MAX_KEYS = 32
MAX_BUNDLE_BYTES = 32 * 1024
TIME_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')

# The committed DEVELOPMENT-ONLY root key. Its private half is public, so
# production verification and production engine builds refuse any root that
# contains it.
DEVELOPMENT_ROOT_KEYS = (TESTDATA / 'root-1.DEVELOPMENT-ONLY.pem',)


class TrustError(ValueError):
    """A release-trust document is not admissible. `code` matches the updater."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code


def development_root_key_ids() -> set[str]:
    return {key_id(load_public_key(path)) for path in DEVELOPMENT_ROOT_KEYS}


def development_root_public_keys() -> set[str]:
    return {base64.b64encode(load_public_key(path)).decode('ascii') for path in DEVELOPMENT_ROOT_KEYS}


# ---------------------------------------------------------------------------
# Time


def parse_time(value: str) -> dt.datetime:
    if not TIME_PATTERN.match(value):
        raise TrustError('trust_malformed', f'{value!r} is not YYYY-MM-DDTHH:MM:SSZ')
    try:
        return dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise TrustError('trust_malformed', f'{value!r} is not a valid time') from None


def now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Envelopes


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding='utf-8'))


def _b64decode(value: Any, what: str) -> bytes:
    if not isinstance(value, str):
        raise TrustError('trust_malformed', f'{what} is not a string')
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise TrustError('trust_malformed', f'{what} is not base64') from None
    if base64.b64encode(raw).decode('ascii') != value:
        raise TrustError('trust_malformed', f'{what} is not canonical base64')
    return raw


def signed_message(domain: bytes, payload: bytes) -> bytes:
    return domain + hashlib.sha256(payload).hexdigest().encode('ascii')


def envelope(payload: dict, domain: bytes, seeds: list[bytes]) -> dict:
    data = canonical_json(payload)
    message = signed_message(domain, data)
    return {
        'payload': base64.b64encode(data).decode('ascii'),
        'signatures': [
            {'key_id': key_id(ed25519_public_key(seed)),
             'signature': base64.b64encode(ed25519_sign(seed, message)).decode('ascii')}
            for seed in seeds
        ],
    }


def open_envelope(document: Any, what: str) -> tuple[bytes, list[tuple[str, bytes]]]:
    """(payload bytes, [(key_id, signature)]) after shape checks only."""
    errors = schema_errors(document, _schema('release-trust.v2.schema.json')['$defs']['envelope'],
                           _schema('release-trust.v2.schema.json'))
    if errors:
        raise TrustError('trust_malformed', f'{what}: ' + '; '.join(errors))
    payload = _b64decode(document['payload'], f'{what}.payload')
    signatures = []
    for index, entry in enumerate(document['signatures']):
        signature = _b64decode(entry['signature'], f'{what}.signatures[{index}].signature')
        if len(signature) != 64:
            raise TrustError('trust_malformed', f'{what}.signatures[{index}]: an Ed25519 signature is 64 bytes')
        if any(entry['key_id'] == seen for seen, _ in signatures):
            raise TrustError('trust_malformed', f'{what}: key {entry["key_id"]} signs twice')
        signatures.append((entry['key_id'], signature))
    return payload, signatures


def count_valid(domain: bytes, payload: bytes, signatures: list[tuple[str, bytes]], keys: dict[str, bytes]) -> int:
    """Distinct authorized keys whose signature verifies. Others are ignored (TUF)."""
    message = signed_message(domain, payload)
    return sum(1 for kid, signature in signatures
               if kid in keys and ed25519_verify(keys[kid], message, signature))


# ---------------------------------------------------------------------------
# Payloads


def _keys(entries: list[dict], where: str) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for index, entry in enumerate(entries):
        raw = _b64decode(entry['public_key'], f'{where}[{index}].public_key')
        if len(raw) != 32 or _decompress(raw) is None:
            raise TrustError('trust_malformed', f'{where}[{index}]: not an Ed25519 public key')
        if entry['key_id'] != key_id(raw):
            raise TrustError('trust_malformed', f'{where}[{index}].key_id: must be {key_id(raw)}')
        if entry['key_id'] in keys:
            raise TrustError('trust_malformed', f'{where}: duplicate key {entry["key_id"]}')
        keys[entry['key_id']] = raw
    return keys


def parse_payload(data: bytes, kind: str) -> dict:
    try:
        document = strict_loads(data)
    except CatalogError as error:
        raise TrustError('trust_malformed', f'{kind}: {error}') from None
    if canonical_json(document) != data:
        raise TrustError('trust_malformed', f'{kind} payload is not canonical JSON')
    errors = validate_payload(document, kind)
    if errors:
        raise TrustError('trust_malformed', f'{kind}: ' + '; '.join(errors))
    return document


def validate_payload(document: Any, kind: str) -> list[str]:
    schema = _schema(f'{kind}.v2.schema.json')
    try:
        errors = schema_errors(document, schema, schema)
    except ContractError as error:
        return [str(error)]
    if errors:
        return errors
    try:
        if kind == 'release-root':
            keys = _keys(document['keys'], '$.keys')
            if len(keys) > MAX_KEYS:
                errors.append(f'$.keys: at most {MAX_KEYS} keys')
            if document['threshold'] > len(keys):
                errors.append('$.threshold: cannot exceed the number of keys')
            parse_time(document['expires_at'])
        elif kind == 'release-delegations':
            if parse_time(document['issued_at']) >= parse_time(document['expires_at']):
                errors.append('$.expires_at: must be after issued_at')
            seen: dict[str, str] = {}
            for role in ROLES:
                entries = document['roles'][role]
                keys = _keys(entries, f'$.roles.{role}')
                for index, entry in enumerate(entries):
                    if parse_time(entry['not_before']) >= parse_time(entry['not_after']):
                        errors.append(f'$.roles.{role}[{index}]: not_after must be after not_before')
                for kid in keys:
                    if kid in seen:
                        errors.append(f'$.roles.{role}: {kid} is also a {seen[kid]} key; '
                                      'a key belongs to one role')
                    seen[kid] = role
            if not document['roles']['build-manifest']:
                errors.append('$.roles.build-manifest: at least one key')
            if len(seen) > MAX_KEYS:
                errors.append(f'$.roles: at most {MAX_KEYS} keys')
            for kid in document['revoked_key_ids']:
                if kid in seen:
                    errors.append(f'$.revoked_key_ids: {kid} is revoked but still delegated')
            if len(set(document['revoked_key_ids'])) != len(document['revoked_key_ids']):
                errors.append('$.revoked_key_ids: values must be unique')
    except TrustError as error:
        errors.append(str(error))
    return errors


# ---------------------------------------------------------------------------
# Verification (the updater's algorithm, plus tooling-only time checks)


class VerifiedTrust:
    def __init__(self, root: dict, root_payload: bytes, delegations: dict, delegations_payload: bytes):
        self.root = root
        self.root_payload = root_payload
        self.delegations = delegations
        self.delegations_payload = delegations_payload

    @property
    def root_key_ids(self) -> set[str]:
        return {key['key_id'] for key in self.root['keys']}

    @property
    def delegations_sha256(self) -> str:
        return hashlib.sha256(self.delegations_payload).hexdigest()

    @property
    def root_sha256(self) -> str:
        return 'sha256:' + hashlib.sha256(self.root_payload).hexdigest()

    def role_key(self, role: str, kid: str, *, at: str | None = None) -> bytes:
        """The public key of `kid` for `role`, or a TrustError with the updater's code."""
        if kid in self.delegations['revoked_key_ids']:
            raise TrustError('signer_revoked', f'{kid} is revoked')
        entries = {entry['key_id']: entry for entry in self.delegations['roles'][role]}
        entry = entries.get(kid)
        if entry is None:
            other = [name for name in ROLES if name != role
                     and any(e['key_id'] == kid for e in self.delegations['roles'][name])]
            if other or kid in self.root_key_ids:
                raise TrustError('signer_role', f'{kid} is a {other[0] if other else "root"} key, not a {role} key')
            raise TrustError('signer_untrusted', f'{kid} is not a delegated {role} key')
        if at is not None:
            moment = parse_time(at)
            if not parse_time(entry['not_before']) <= moment < parse_time(entry['not_after']):
                raise TrustError('signer_outside_window',
                                 f'{kid} may sign from {entry["not_before"]} until {entry["not_after"]}')
        return base64.b64decode(entry['public_key'])


def verify_bundle(bundle: Any, pinned_root: bytes, min_delegations_version: int = 1, *,
                  at: str | None = None, production: bool = False) -> VerifiedTrust:
    """Verifies `bundle` (bytes or a parsed document) against the compiled pin.

    `at` adds the checks only tooling makes (expiry and validity windows):
    the updater never fails a startup on its clock.
    """
    if isinstance(bundle, (bytes, bytearray)):
        if len(bundle) > MAX_BUNDLE_BYTES:
            raise TrustError('trust_malformed', 'release-trust bundle is too large')
        try:
            bundle = strict_loads(bytes(bundle))
        except CatalogError as error:
            raise TrustError('trust_malformed', str(error)) from None
    root = parse_payload(pinned_root, 'release-root')
    root_payload = pinned_root
    schema = _schema('release-trust.v2.schema.json')
    errors = schema_errors(bundle, schema, schema)
    if errors:
        raise TrustError('trust_malformed', 'release-trust: ' + '; '.join(errors))
    if len(bundle['root_chain']) > MAX_ROOT_CHAIN:
        raise TrustError('trust_malformed', f'at most {MAX_ROOT_CHAIN} root versions')
    for index, item in enumerate(bundle['root_chain']):
        payload, signatures = open_envelope(item, f'root_chain[{index}]')
        candidate = parse_payload(payload, 'release-root')
        if candidate['version'] <= root['version']:
            continue
        if candidate['version'] != root['version'] + 1:
            raise TrustError('trust_unverified', f'root version {candidate["version"]} does not follow '
                                                 f'{root["version"]}')
        old_keys = _keys(root['keys'], 'root')
        new_keys = _keys(candidate['keys'], 'root')
        if (count_valid(ROOT_DOMAIN, payload, signatures, old_keys) < root['threshold']
                or count_valid(ROOT_DOMAIN, payload, signatures, new_keys) < candidate['threshold']):
            raise TrustError('trust_unverified', f'root version {candidate["version"]} is not signed by '
                                                 'thresholds of both the previous and its own keys')
        root, root_payload = candidate, payload
    payload, signatures = open_envelope(bundle['delegations'], 'delegations')
    delegations = parse_payload(payload, 'release-delegations')
    if delegations['root_version'] != root['version']:
        raise TrustError('trust_unverified', f'delegations are for root version {delegations["root_version"]}, '
                                             f'the chain ends at {root["version"]}')
    if count_valid(DELEGATIONS_DOMAIN, payload, signatures, _keys(root['keys'], 'root')) < root['threshold']:
        raise TrustError('trust_unverified', 'delegations are not signed by a threshold of root keys')
    if delegations['version'] < min_delegations_version:
        raise TrustError('trust_rollback', f'delegations version {delegations["version"]} is below the '
                                           f'compiled minimum {min_delegations_version}')
    root_ids = {key['key_id'] for key in root['keys']}
    for role in ROLES:
        for entry in delegations['roles'][role]:
            if entry['key_id'] in root_ids:
                raise TrustError('trust_malformed', f'{entry["key_id"]} is both a root key and a {role} key')
    trust = VerifiedTrust(root, root_payload, delegations, payload)
    if at is not None:
        moment = parse_time(at)
        if moment >= parse_time(root['expires_at']):
            raise TrustError('trust_expired', f'root version {root["version"]} expired at {root["expires_at"]}')
        if not parse_time(delegations['issued_at']) <= moment < parse_time(delegations['expires_at']):
            raise TrustError('trust_expired', f'delegations version {delegations["version"]} are valid from '
                                              f'{delegations["issued_at"]} until {delegations["expires_at"]}')
    if production:
        development = development_root_key_ids() & (root_ids | {k['key_id'] for k in
                                                                 parse_payload(pinned_root, 'release-root')['keys']})
        if development:
            raise TrustError('trust_development', 'the root contains a DEVELOPMENT-ONLY key; '
                                                  'production releases need an operator-held root')
    return trust


# ---------------------------------------------------------------------------
# Authoring


def root_payload(keys: list[bytes], *, version: int, threshold: int, expires_at: str) -> dict:
    return {
        'schema': ROOT_SCHEMA,
        'contract_version': CONTRACT_VERSION,
        'version': version,
        'threshold': threshold,
        'keys': [{'key_id': key_id(public), 'public_key': base64.b64encode(public).decode('ascii')}
                 for public in keys],
        'expires_at': expires_at,
    }


def sign_root(payload: dict, seeds: list[bytes], previous: dict | None = None,
              previous_seeds: list[bytes] = ()) -> dict:
    errors = validate_payload(payload, 'release-root')
    if errors:
        raise TrustError('trust_malformed', '; '.join(errors))
    own = {key['key_id'] for key in payload['keys']}
    signers = list(seeds)
    if previous is None:
        if payload['version'] != 1:
            raise TrustError('trust_malformed', 'a root above version 1 needs --previous')
    else:
        if payload['version'] != previous['version'] + 1:
            raise TrustError('trust_malformed', f'the next root after {previous["version"]} is '
                                                f'{previous["version"] + 1}')
        old = {key['key_id'] for key in previous['keys']}
        if len({key_id(ed25519_public_key(s)) for s in previous_seeds} & old) < previous['threshold']:
            raise TrustError('trust_unverified', 'not enough previous root keys to rotate')
        signers += [s for s in previous_seeds if key_id(ed25519_public_key(s)) not in own]
    if len({key_id(ed25519_public_key(s)) for s in seeds} & own) < payload['threshold']:
        raise TrustError('trust_unverified', 'not enough of the new root keys to meet its threshold')
    return envelope(payload, ROOT_DOMAIN, signers)


def delegated_key(public: bytes, not_before: str, not_after: str) -> dict:
    return {
        'key_id': key_id(public),
        'public_key': base64.b64encode(public).decode('ascii'),
        'not_before': not_before,
        'not_after': not_after,
    }


def delegations_payload(*, version: int, root_version: int, issued_at: str, expires_at: str,
                        roles: dict[str, list[dict]], revoked: list[str]) -> dict:
    return {
        'schema': DELEGATIONS_SCHEMA,
        'contract_version': CONTRACT_VERSION,
        'version': version,
        'root_version': root_version,
        'issued_at': issued_at,
        'expires_at': expires_at,
        'roles': {role: roles.get(role, []) for role in ROLES},
        'revoked_key_ids': sorted(set(revoked)),
    }


def sign_delegations(payload: dict, root: dict, root_seeds: list[bytes], previous: dict | None = None) -> dict:
    errors = validate_payload(payload, 'release-delegations')
    if errors:
        raise TrustError('trust_malformed', '; '.join(errors))
    if payload['root_version'] != root['version']:
        raise TrustError('trust_malformed', f'root_version must be {root["version"]}')
    root_ids = {key['key_id'] for key in root['keys']}
    for role in ROLES:
        for entry in payload['roles'][role]:
            if entry['key_id'] in root_ids:
                raise TrustError('trust_malformed', f'{entry["key_id"]} is a root key; root keys sign only metadata')
    if previous is not None:
        if payload['version'] <= previous['version']:
            raise TrustError('trust_rollback', f'the next delegations version must exceed {previous["version"]}')
        # A revocation is permanent: it is carried into every later version.
        dropped = set(previous['revoked_key_ids']) - set(payload['revoked_key_ids'])
        if dropped:
            raise TrustError('trust_malformed', f'revocations cannot be withdrawn: {sorted(dropped)}')
    if len({key_id(ed25519_public_key(s)) for s in root_seeds} & root_ids) < root['threshold']:
        raise TrustError('trust_unverified', 'not enough root keys to meet the threshold')
    return envelope(payload, DELEGATIONS_DOMAIN, [s for s in root_seeds if key_id(ed25519_public_key(s)) in root_ids])


def bundle(root_chain: list[dict], delegations: dict) -> dict:
    return {
        'schema': TRUST_SCHEMA,
        'contract_version': CONTRACT_VERSION,
        'root_chain': root_chain,
        'delegations': delegations,
    }


def pin_fields(root_envelope: dict) -> dict:
    payload, _ = open_envelope(root_envelope, 'root')
    root = parse_payload(payload, 'release-root')
    return {
        'release_root': base64.b64encode(payload).decode('ascii'),
        'release_root_sha256': 'sha256:' + hashlib.sha256(payload).hexdigest(),
        'release_root_version': root['version'],
        'development': bool(development_root_key_ids() & {k['key_id'] for k in root['keys']}),
    }


# ---------------------------------------------------------------------------
# CLI


def _role_arg(value: str) -> tuple[str, Path, str | None, str | None]:
    """ROLE=PEM[@NOT_BEFORE..NOT_AFTER]"""
    match = re.fullmatch(r'([a-z-]+)=([^@]+)(?:@(\S+)\.\.(\S+))?', value)
    if match is None or match.group(1) not in ROLES:
        raise argparse.ArgumentTypeError(f'expected ROLE=PEM[@NOT_BEFORE..NOT_AFTER] with ROLE in {ROLES}')
    return match.group(1), Path(match.group(2)), match.group(3), match.group(4)


def _load(path: Path) -> Any:
    try:
        return strict_loads(path.read_bytes())
    except CatalogError as error:
        raise TrustError('trust_malformed', f'{path}: {error}') from None


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _sign_command(args: argparse.Namespace) -> int:
    """External signer protocol: `sign --key-id ID --message TEXT`, base64 signature on stdout."""
    seed = load_private_seed(args.key)
    public = ed25519_public_key(seed)
    if args.key_id != key_id(public):
        print(f'error: this signer holds {key_id(public)}, not {args.key_id}', file=sys.stderr)
        return 2
    message = args.message.encode('ascii')
    domain = ROLE_DOMAINS[args.role]
    if not message.startswith(domain) or not re.fullmatch(rb'[0-9a-f]{64}', message[len(domain):]):
        print(f'error: a {args.role} key signs only {domain.decode()}<sha256 hex> messages', file=sys.stderr)
        return 3
    sys.stdout.write(base64.b64encode(ed25519_sign(seed, message)).decode('ascii') + '\n')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)

    root = sub.add_parser('root', help='sign a release-root version')
    root.add_argument('--key', type=Path, action='append', required=True,
                      help='private PEM of a root key of this version (repeat)')
    root.add_argument('--public-key', type=Path, action='append', default=[],
                      help='public (or private) PEM of a further root key that does not sign now')
    root.add_argument('--threshold', type=int, required=True)
    root.add_argument('--version', type=int, default=1)
    root.add_argument('--expires', required=True)
    root.add_argument('--previous', type=Path, help='the previous root envelope (rotation)')
    root.add_argument('--previous-key', type=Path, action='append', default=[])
    root.add_argument('--output', type=Path, required=True)

    delegate = sub.add_parser('delegate', help='sign a release-delegations version')
    delegate.add_argument('--root', type=Path, required=True, help='root envelope that signs')
    delegate.add_argument('--root-key', type=Path, action='append', required=True)
    delegate.add_argument('--version', type=int, required=True)
    delegate.add_argument('--issued', default=None, help='default: now')
    delegate.add_argument('--expires', required=True)
    delegate.add_argument('--role', type=_role_arg, action='append', required=True,
                          help='ROLE=PEM[@NOT_BEFORE..NOT_AFTER] (repeat)')
    delegate.add_argument('--revoke', action='append', default=[], help='key ID to revoke (repeat)')
    delegate.add_argument('--previous', type=Path, help='previous delegations envelope (carries revocations)')
    delegate.add_argument('--output', type=Path, required=True)

    make_bundle = sub.add_parser('bundle', help='assemble a release-trust bundle')
    make_bundle.add_argument('--root', type=Path, action='append', required=True,
                             help='root envelopes, oldest first (repeat)')
    make_bundle.add_argument('--delegations', type=Path, required=True)
    make_bundle.add_argument('--output', type=Path, required=True)

    pin = sub.add_parser('pin', help='print the compiled pin of a root envelope')
    pin.add_argument('root', type=Path)

    verify = sub.add_parser('verify', help='verify a release-trust bundle against a pin')
    verify.add_argument('bundle', type=Path)
    verify.add_argument('--root-pin', required=True, help='base64 root payload (GN shorebird_release_root)')
    verify.add_argument('--min-delegations-version', type=int, default=1)
    verify.add_argument('--role', choices=ROLES)
    verify.add_argument('--key-id')
    verify.add_argument('--at', help='also check expiry and key windows at this time ("now" for now)')
    verify.add_argument('--production', action='store_true', help='refuse DEVELOPMENT-ONLY roots')

    sign = sub.add_parser('sign', help='reference external signer backed by a PEM key')
    sign.add_argument('--key', type=Path, required=True)
    sign.add_argument('--role', choices=ROLES, default='build-manifest')
    sign.add_argument('--key-id', required=True)
    sign.add_argument('--message', required=True, help='the ASCII message to sign (domain + sha256 hex)')

    args = parser.parse_args(argv)
    try:
        if args.command == 'sign':
            return _sign_command(args)
        if args.command == 'root':
            seeds = [load_private_seed(path) for path in args.key]
            publics = [ed25519_public_key(seed) for seed in seeds] + [load_public_key(p) for p in args.public_key]
            payload = root_payload(publics, version=args.version, threshold=args.threshold,
                                   expires_at=args.expires)
            previous = None
            if args.previous:
                data, _ = open_envelope(_load(args.previous), str(args.previous))
                previous = parse_payload(data, 'release-root')
            document = sign_root(payload, seeds, previous, [load_private_seed(p) for p in args.previous_key])
            _write(args.output, document)
            print(f'{args.output}: release root version {args.version}')
        elif args.command == 'delegate':
            data, _ = open_envelope(_load(args.root), str(args.root))
            root_document = parse_payload(data, 'release-root')
            roles: dict[str, list[dict]] = {name: [] for name in ROLES}
            issued = args.issued or now_text()
            for role, path, not_before, not_after in args.role:
                roles[role].append(delegated_key(load_public_key(path), not_before or issued,
                                                 not_after or args.expires))
            previous = None
            revoked = list(args.revoke)
            if args.previous:
                previous_data, _ = open_envelope(_load(args.previous), str(args.previous))
                previous = parse_payload(previous_data, 'release-delegations')
                revoked += previous['revoked_key_ids']
            payload = delegations_payload(version=args.version, root_version=root_document['version'],
                                          issued_at=issued, expires_at=args.expires, roles=roles,
                                          revoked=revoked)
            document = sign_delegations(payload, root_document,
                                        [load_private_seed(p) for p in args.root_key], previous)
            _write(args.output, document)
            print(f'{args.output}: delegations version {args.version} under root version {root_document["version"]}')
        elif args.command == 'bundle':
            document = bundle([_load(p) for p in args.root], _load(args.delegations))
            # Prove the bundle is self-consistent from its oldest root.
            first, _ = open_envelope(document['root_chain'][0], 'root_chain[0]')
            verify_bundle(document, first)
            _write(args.output, document)
            print(f'{args.output}: release trust with {len(args.root)} root version(s)')
        elif args.command == 'pin':
            fields = pin_fields(_load(args.root))
            print(json.dumps(fields, indent=2))
            print(f'# GN: shorebird_release_root = "{fields["release_root"]}"', file=sys.stderr)
        else:
            pinned = _b64decode(args.root_pin, '--root-pin')
            at = now_text() if args.at == 'now' else args.at
            trust = verify_bundle(args.bundle.read_bytes(), pinned, args.min_delegations_version,
                                  at=at, production=args.production)
            if args.role or args.key_id:
                if not (args.role and args.key_id):
                    parser.error('--role and --key-id go together')
                trust.role_key(args.role, args.key_id, at=at)
            print(f'{args.bundle}: verified root version {trust.root["version"]}, delegations version '
                  f'{trust.delegations["version"]} (sha256 {trust.delegations_sha256})')
    except (TrustError, CatalogError, ContractError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

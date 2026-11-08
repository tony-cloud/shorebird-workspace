#!/usr/bin/env python3
"""Generate, sign and verify Open Shorebird artifact catalogs (contract v1).

See docs/HARDENING_CONTRACTS.md section 8. A published catalog is the UTF-8
canonical JSON of an `artifact-catalog.v1` document, byte for byte. Its
detached signature is an `artifact-catalog-signature.v1` document over

    b"open-shorebird.artifact-catalog-signature.v1:" + <catalog sha256 hex>

with an Ed25519 key pinned in an `artifact-catalog-trust.v1` document. The
trust document shipped with the toolchain is
`flutter/bin/internal/shorebird_artifact_trust.json`.

Subcommands:
  generate   build a catalog from an assembled artifact mirror
  sign       write a detached signature (OpenSSL or the pure-Python signer)
  verify     check catalog, signature and trust under a security profile
  trust-entry print the trust entry for a private or public PEM key

Signing with the pure-Python implementation is meant for the development key
and tests; CI signs production catalogs with `--backend openssl`. Both give
identical bytes, because Ed25519 signatures are deterministic.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_hardening_contracts import (  # noqa: E402
    ContractError,
    canonical_json,
    requires_custom,
    schema_errors,
    validate_artifact_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / 'docs' / 'schemas'
SIGNATURE_DOMAIN = b'open-shorebird.artifact-catalog-signature.v1:'
CATALOG_FILE_NAME = 'artifact-catalog.v1.json'
SIGNATURE_FILE_NAME = 'artifact-catalog.v1.json.sig'
DEVELOPMENT_KEY = ROOT / 'scripts' / 'testdata' / 'artifact-catalog-signing-key.DEVELOPMENT-ONLY.pem'
TRUST_FILE = ROOT / 'flutter' / 'bin' / 'internal' / 'shorebird_artifact_trust.json'
# PKCS#8 prefix of an Ed25519 private key (RFC 8410): the 32-byte seed follows.
_PKCS8_ED25519_PREFIX = bytes.fromhex('302e020100300506032b657004220420')
# SubjectPublicKeyInfo prefix of an Ed25519 public key: the 32-byte key follows.
_SPKI_ED25519_PREFIX = bytes.fromhex('302a300506032b6570032100')


class CatalogError(ValueError):
    """A catalog, signature or trust document is not admissible."""


# ---------------------------------------------------------------------------
# Ed25519 (RFC 8032 section 5.1), verification and deterministic signing.

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(p1: tuple, p2: tuple) -> tuple:
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % _P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % _P
    c = 2 * p1[3] * p2[3] * _D % _P
    d = 2 * p1[2] * p2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(p1: tuple, p2: tuple) -> bool:
    return ((p1[0] * p2[2] - p2[0] * p1[2]) % _P == 0
            and (p1[1] * p2[2] - p2[1] * p1[2]) % _P == 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point: tuple) -> bytes:
    zinv = _inv(point[2])
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, 'little')


def _decompress(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, 'little')
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _sha512_mod_l(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), 'little') % _L


def ed25519_public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise CatalogError('an Ed25519 private key seed is 32 bytes')
    digest = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, _G))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    digest = hashlib.sha512(seed).digest()
    a = int.from_bytes(digest[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    public = _compress(_mul(a, _G))
    r = _sha512_mod_l(digest[32:] + message)
    encoded_r = _compress(_mul(r, _G))
    h = _sha512_mod_l(encoded_r + public + message)
    s = (r + h * a) % _L
    return encoded_r + int.to_bytes(s, 32, 'little')


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 verification. Rejects non-canonical points and S >= L."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], 'little')
    if s >= _L:
        return False
    h = _sha512_mod_l(signature[:32] + public + message)
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# ---------------------------------------------------------------------------
# Keys


def _pem_body(text: str, label: str) -> bytes | None:
    match = re.search(rf'-----BEGIN {label}-----(.*?)-----END {label}-----', text, re.S)
    return base64.b64decode(''.join(match.group(1).split())) if match else None


def load_private_seed(path: Path) -> bytes:
    der = _pem_body(path.read_text(encoding='ascii'), 'PRIVATE KEY')
    if der is None or len(der) != 48 or not der.startswith(_PKCS8_ED25519_PREFIX):
        raise CatalogError(f'{path}: not a PKCS#8 Ed25519 private key')
    return der[len(_PKCS8_ED25519_PREFIX):]


def load_public_key(path: Path) -> bytes:
    text = path.read_text(encoding='ascii')
    der = _pem_body(text, 'PUBLIC KEY')
    if der is not None:
        if len(der) != 44 or not der.startswith(_SPKI_ED25519_PREFIX):
            raise CatalogError(f'{path}: not an Ed25519 public key')
        return der[len(_SPKI_ED25519_PREFIX):]
    return ed25519_public_key(load_private_seed(path))


def key_id(public: bytes) -> str:
    return 'sha256:' + hashlib.sha256(public).hexdigest()


def trust_entry(public: bytes, usage: str, description: str) -> dict:
    return {
        'key_id': key_id(public),
        'algorithm': 'ed25519',
        'public_key': base64.b64encode(public).decode('ascii'),
        'usage': usage,
        'description': description,
    }


# ---------------------------------------------------------------------------
# Documents


def strict_loads(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise CatalogError(f'duplicate JSON key {key}')
            result[key] = value
        return result

    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError(f'invalid JSON: {error}') from None


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding='utf-8'))


def validate_trust(trust: Any) -> list[str]:
    schema = _schema('artifact-catalog-trust.v1.schema.json')
    errors = schema_errors(trust, schema, schema)
    if errors:
        return errors
    seen = set()
    for index, key in enumerate(trust['keys']):
        where = f'$.keys[{index}]'
        public = base64.b64decode(key['public_key'])
        if len(public) != 32 or _decompress(public) is None:
            errors.append(f'{where}.public_key: not a valid Ed25519 public key')
            continue
        if key['key_id'] != key_id(public):
            errors.append(f'{where}.key_id: must be sha256 over the 32-byte public key ({key_id(public)})')
        if key['key_id'] in seen:
            errors.append(f'{where}.key_id: duplicate key')
        seen.add(key['key_id'])
    return errors


def validate_signature_document(signature: Any) -> list[str]:
    schema = _schema('artifact-catalog-signature.v1.schema.json')
    errors = schema_errors(signature, schema, schema)
    if not errors and len(base64.b64decode(signature['signature'])) != 64:
        errors.append('$.signature: an Ed25519 signature is 64 bytes')
    return errors


def check_trust_store(trust: Any) -> list[str]:
    """Errors for a trust store shipped in a toolchain checkout.

    Besides the v1 contract, the non-production development key must never be
    granted production trust: a catalog signed with a key whose private half is
    in this repository must not satisfy the hardened profile.
    """
    errors = validate_trust(trust)
    if errors:
        return errors
    development = key_id(load_public_key(DEVELOPMENT_KEY))
    for index, key in enumerate(trust['keys']):
        if key['key_id'] == development and key['usage'] != 'development':
            errors.append(f'$.keys[{index}].usage: the repository development key may only have usage "development"')
    return errors


def signed_message(catalog_sha256: str) -> bytes:
    return SIGNATURE_DOMAIN + catalog_sha256.encode('ascii')


def verify_catalog(
    catalog_bytes: bytes,
    signature_bytes: bytes | None,
    trust: dict,
    *,
    profile: str,
    engine_revision: str | None = None,
    allow_unsigned: bool = False,
    release_trust: Any = None,
) -> tuple[dict, str]:
    """Returns (catalog, trust) where trust is production/development/unsigned.

    Under `hardened`, `release_trust` (a `release_trust.VerifiedTrust`) is
    required and the signer must be its current `artifact-catalog` delegate
    (contract v2, section 10), as flutter_tools requires.

    Raises CatalogError with a precise reason when the catalog must not be used.
    """
    if profile not in ('legacy', 'hardened'):
        raise CatalogError(f'unsupported security profile {profile}')
    trust_errors = validate_trust(trust)
    if trust_errors:
        raise CatalogError('trust document: ' + '; '.join(trust_errors))
    catalog = strict_loads(catalog_bytes)
    if canonical_json(catalog) != catalog_bytes:
        raise CatalogError('catalog is not in canonical JSON form; its bytes are what the signature covers')
    errors = validate_artifact_catalog(catalog)
    if errors:
        raise CatalogError('catalog: ' + '; '.join(errors))
    if engine_revision is not None and catalog['engine_revision'] != engine_revision:
        raise CatalogError(f'catalog is for engine {catalog["engine_revision"]}, not {engine_revision}')
    if profile == 'hardened' and catalog['security_profile'] != 'hardened':
        raise CatalogError('a hardened toolchain requires a hardened catalog')
    if profile == 'hardened' and release_trust is None:
        raise CatalogError('a hardened toolchain verifies the catalog signer under the release root '
                           '(--release-trust and --root-pin)')
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    if signature_bytes is None:
        if profile == 'hardened':
            raise CatalogError('unsigned catalog: a hardened toolchain requires a production signature')
        if not allow_unsigned:
            raise CatalogError('unsigned catalog: set the explicit development override to accept it')
        return catalog, 'unsigned'
    signature = strict_loads(signature_bytes)
    errors = validate_signature_document(signature)
    if errors:
        raise CatalogError('signature: ' + '; '.join(errors))
    if signature['catalog_sha256'] != digest:
        raise CatalogError('signature covers a different catalog digest')
    key = next((k for k in trust['keys'] if k['key_id'] == signature['key_id']), None)
    if key is None:
        raise CatalogError(f'catalog signed by untrusted key {signature["key_id"]}')
    if not ed25519_verify(base64.b64decode(key['public_key']), signed_message(digest),
                          base64.b64decode(signature['signature'])):
        raise CatalogError('catalog signature does not verify')
    if key['usage'] != 'production' and profile == 'hardened':
        raise CatalogError('a hardened toolchain rejects a catalog signed by a development key')
    if profile == 'hardened':
        try:
            release_trust.role_key('artifact-catalog', signature['key_id'])
        except ValueError as error:  # release_trust.TrustError
            raise CatalogError(f'the catalog signer is not an artifact-catalog key under the release root: '
                               f'{error}') from None
    return catalog, key['usage']


# ---------------------------------------------------------------------------
# Generation


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def route_platform(path: str) -> str:
    """The catalog platform for a route: the target or host it serves."""
    match = re.search(r'/android-(arm64|arm|x64)-release/', path)
    if match:
        return f'android-{match.group(1)}'
    match = re.search(r'/io/flutter/([a-z0-9_]+)_release/', path)
    if match:
        return {'arm64_v8a': 'android-arm64', 'armeabi_v7a': 'android-arm',
                'x86_64': 'android-x64'}.get(match.group(1), 'any')
    if '/ios-release/' in path or path.endswith('/ios/artifacts.zip'):
        return 'ios-arm64'
    match = re.search(r'/(darwin|linux|windows)-(arm64|x64)(?:-release)?/', path)
    if match:
        return f'{match.group(1)}-{match.group(2)}'
    match = re.search(r'/dart-sdk-(darwin|linux|windows)-(arm64|x64)\.zip$', path)
    if match:
        return f'{match.group(1)}-{match.group(2)}'
    return 'any'


def read_manifest(path: Path) -> tuple[str, str, list[str]]:
    """(flutter_engine_revision, storage_bucket, artifact_overrides)."""
    revision = bucket = None
    overrides: list[str] = []
    in_overrides = False
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.startswith('flutter_engine_revision:'):
            revision = line.split(':', 1)[1].strip().strip('"\'')
            in_overrides = False
        elif line.startswith('storage_bucket:'):
            bucket = line.split(':', 1)[1].strip().strip('"\'')
            in_overrides = False
        elif line.startswith('artifact_overrides:'):
            in_overrides = True
        elif in_overrides and line.lstrip().startswith('- '):
            overrides.append(line.lstrip()[2:].strip().strip('"\''))
        else:
            raise CatalogError(f'{path}: unexpected manifest line {line!r}')
    if revision is None or bucket is None or not overrides:
        raise CatalogError(f'{path}: manifest needs flutter_engine_revision, storage_bucket and artifact_overrides')
    return revision, bucket, overrides


def generate_catalog(
    *,
    mirror_root: Path,
    engine_revision: str,
    toolchain_id: str,
    custom_base_url: str,
    security_profile: str = 'legacy',
    official_base_url: str = 'https://storage.googleapis.com',
    official_entries: list[dict] | None = None,
) -> dict:
    """A complete catalog for one engine revision of an assembled mirror.

    Every custom route is an `artifact_overrides` entry of the mirror's
    `shorebird/<engine>/artifacts_manifest.yaml`; a missing file is an error,
    never a silently shorter catalog.
    """
    manifest = mirror_root / 'shorebird' / engine_revision / 'artifacts_manifest.yaml'
    upstream, bucket, overrides = read_manifest(manifest)
    artifacts = []
    missing = []
    for route in overrides:
        file = mirror_root / bucket / route.replace('$engine', engine_revision)
        if not file.is_file():
            missing.append(route)
            continue
        sha256, size = _sha256_file(file)
        artifacts.append({
            'path': route,
            'origin': 'project-artifacts',
            'class': 'custom-toolchain',
            'revision': engine_revision,
            'platform': route_platform(route),
            'sha256': sha256,
            'size': size,
            'required': True,
        })
    if missing:
        raise CatalogError('missing custom artifacts for engine ' + engine_revision + ':\n  '
                           + '\n  '.join(missing))
    for entry in official_entries or []:
        artifacts.append({**entry, 'origin': 'flutter-official', 'class': 'official-unchanged',
                          'revision': upstream})
    artifacts.sort(key=lambda item: item['path'])
    catalog = {
        'schema': 'open-shorebird.artifact-catalog',
        'contract_version': 1,
        'toolchain_id': toolchain_id,
        'security_profile': security_profile,
        'diversification_profile_id': None,
        'engine_revision': engine_revision,
        'upstream_engine_revision': upstream,
        'origins': {
            'flutter-official': {'class': 'official', 'base_url': official_base_url.rstrip('/')},
            'project-artifacts': {'class': 'custom-project', 'base_url': custom_base_url.rstrip('/')},
        },
        'artifacts': artifacts,
    }
    errors = validate_artifact_catalog(catalog)
    if errors:
        raise CatalogError('generated catalog is invalid: ' + '; '.join(errors))
    check_completeness(catalog, overrides, upstream)
    return catalog


def check_completeness(catalog: dict, overrides: list[str], upstream: str) -> None:
    """Contract section 4: custom routes equal the proxy manifest overrides."""
    custom = sorted(a['path'] for a in catalog['artifacts'] if a['class'] == 'custom-toolchain')
    if custom != sorted(set(overrides)):
        extra = sorted(set(custom) - set(overrides))
        absent = sorted(set(overrides) - set(custom))
        raise CatalogError(f'catalog custom routes differ from artifact_overrides: extra={extra} missing={absent}')
    if catalog['upstream_engine_revision'] != upstream:
        raise CatalogError('catalog upstream_engine_revision differs from the manifest flutter_engine_revision')
    for artifact in catalog['artifacts']:
        if artifact['class'] == 'official-unchanged' and requires_custom(artifact['path']):
            raise CatalogError(f'{artifact["path"]}: an official artifact must never satisfy a custom route')


def toolchain_id_from_checkout(root: Path, security_profile: str) -> str:
    """Contract section 3 toolchain_id for the checkout at root."""
    from validate_hardening_contracts import toolchain_id

    def head(path: Path) -> str:
        return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()

    def url(name: str) -> str:
        return subprocess.check_output(
            ['git', '-C', str(root), 'config', '-f', '.gitmodules', f'submodule.{name}.url'], text=True).strip()

    fork = {
        'workspace': {'repository': 'https://github.com/tony-cloud/shorebird-workspace.git', 'revision': head(root)},
        'flutter': {'repository': url('flutter'), 'revision': head(root / 'flutter')},
        'dart_sdk': {'repository': url('dart-sdk'), 'revision': head(root / 'dart-sdk')},
        'updater': {'repository': url('updater'), 'revision': head(root / 'updater')},
        'shorebird_cli': {'repository': url('shorebird'), 'revision': head(root / 'shorebird')},
    }
    if (root / 'shorebird-server' / '.git').exists():
        fork['shorebird_server'] = {'repository': url('shorebird-server'),
                                    'revision': head(root / 'shorebird-server')}
    engine = (root / 'flutter' / 'bin' / 'internal' / 'engine.version').read_text().strip()
    return toolchain_id({
        'contract_version': 1,
        'security_profile': security_profile,
        'diversification_profile_id': None,
        'toolchain': {'engine_revision': engine},
        'fork': fork,
    })


# ---------------------------------------------------------------------------
# Signing


def sign_catalog(catalog_bytes: bytes, key_path: Path, *, backend: str = 'python') -> dict:
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    message = signed_message(digest)
    public = load_public_key(key_path)
    if backend == 'python':
        signature = ed25519_sign(load_private_seed(key_path), message)
    elif backend == 'openssl':
        openssl = shutil.which('openssl')
        if openssl is None:
            raise CatalogError('openssl is not installed')
        with tempfile.TemporaryDirectory() as tmp:
            message_path = Path(tmp) / 'message'
            message_path.write_bytes(message)
            signature = subprocess.run(
                [openssl, 'pkeyutl', '-sign', '-rawin', '-inkey', str(key_path), '-in', str(message_path)],
                check=True, capture_output=True).stdout
    else:
        raise CatalogError(f'unknown signing backend {backend}')
    if not ed25519_verify(public, message, signature):
        raise CatalogError('the produced signature does not verify')
    return {
        'schema': 'open-shorebird.artifact-catalog-signature',
        'contract_version': 1,
        'algorithm': 'ed25519',
        'key_id': key_id(public),
        'catalog_sha256': digest,
        'signature': base64.b64encode(signature).decode('ascii'),
    }


# ---------------------------------------------------------------------------
# Command line


def _write_canonical(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(document))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)

    generate = sub.add_parser('generate', help='write <mirror>/shorebird/<engine>/artifact-catalog.v1.json')
    generate.add_argument('--mirror-root', type=Path, required=True)
    generate.add_argument('--engine-revision', required=True)
    generate.add_argument('--custom-base-url', required=True,
                          help='https URL of the immutable custom artifact store, including the bucket')
    generate.add_argument('--security-profile', default='legacy', choices=('legacy', 'hardened'))
    generate.add_argument('--toolchain-id', help='defaults to the workspace checkout identity')
    generate.add_argument('--workspace-root', type=Path, default=ROOT)
    generate.add_argument('--output', type=Path)

    sign = sub.add_parser('sign', help='write a detached catalog signature')
    sign.add_argument('--catalog', type=Path, required=True)
    sign.add_argument('--key', type=Path, required=True)
    sign.add_argument('--backend', choices=('python', 'openssl'), default='python')
    sign.add_argument('--output', type=Path)

    verify = sub.add_parser('verify', help='verify catalog, signature and trust')
    verify.add_argument('--catalog', type=Path, required=True)
    verify.add_argument('--signature', type=Path)
    verify.add_argument('--trust', type=Path, default=TRUST_FILE)
    verify.add_argument('--profile', choices=('legacy', 'hardened'), default='legacy')
    verify.add_argument('--engine-revision')
    verify.add_argument('--allow-unsigned', action='store_true')
    verify.add_argument('--release-trust', type=Path, help='hardened: the release-trust bundle')
    verify.add_argument('--root-pin', help='hardened: the compiled release-root pin (base64)')
    verify.add_argument('--min-delegations-version', type=int, default=1)

    entry = sub.add_parser('trust-entry', help='print the trust entry for a PEM key')
    entry.add_argument('key', type=Path)
    entry.add_argument('--usage', choices=('production', 'development'), required=True)
    entry.add_argument('--description', required=True)

    check = sub.add_parser('check-trust', help='validate a shipped catalog trust store')
    check.add_argument('trust', type=Path, nargs='?', default=TRUST_FILE)

    args = parser.parse_args(argv)
    try:
        if args.command == 'generate':
            identity = args.toolchain_id or toolchain_id_from_checkout(args.workspace_root, args.security_profile)
            catalog = generate_catalog(
                mirror_root=args.mirror_root, engine_revision=args.engine_revision,
                toolchain_id=identity, custom_base_url=args.custom_base_url,
                security_profile=args.security_profile)
            output = args.output or (args.mirror_root / 'shorebird' / args.engine_revision / CATALOG_FILE_NAME)
            _write_canonical(output, catalog)
            print(f'{output}: {len(catalog["artifacts"])} artifacts, sha256 '
                  f'{hashlib.sha256(output.read_bytes()).hexdigest()}')
        elif args.command == 'sign':
            document = sign_catalog(args.catalog.read_bytes(), args.key, backend=args.backend)
            output = args.output or args.catalog.with_name(args.catalog.name + '.sig')
            _write_canonical(output, document)
            print(f'{output}: signed by {document["key_id"]}')
        elif args.command == 'verify':
            trust = strict_loads(args.trust.read_bytes())
            signature = args.signature.read_bytes() if args.signature else None
            release = None
            if args.release_trust or args.root_pin:
                if not (args.release_trust and args.root_pin):
                    raise CatalogError('--release-trust and --root-pin go together')
                import release_trust as rt  # noqa: PLC0415 (imports this module)
                try:
                    release = rt.verify_bundle(args.release_trust.read_bytes(), base64.b64decode(args.root_pin),
                                               args.min_delegations_version)
                except rt.TrustError as error:
                    raise CatalogError(f'release trust: {error}') from None
            catalog, usage = verify_catalog(
                args.catalog.read_bytes(), signature, trust, profile=args.profile,
                engine_revision=args.engine_revision, allow_unsigned=args.allow_unsigned,
                release_trust=release)
            if usage != 'production':
                print(f'warning: {args.catalog} is {usage}-signed; never use it for a production release',
                      file=sys.stderr)
            print(f'{args.catalog}: verified ({usage}) for engine {catalog["engine_revision"]}')
        elif args.command == 'check-trust':
            errors = check_trust_store(strict_loads(args.trust.read_bytes()))
            if errors:
                raise CatalogError(f'{args.trust}: ' + '; '.join(errors))
            print(f'{args.trust}: valid catalog trust store')
        else:
            public = load_public_key(args.key)
            print(json.dumps(trust_entry(public, args.usage, args.description), indent=2))
    except (CatalogError, ContractError, OSError, subprocess.CalledProcessError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

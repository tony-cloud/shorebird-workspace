#!/usr/bin/env python3
"""Validate hardening contract documents (docs/HARDENING_CONTRACTS.md).

Kinds: security-policy (v1, v2, v3), build-identity (v1, v3),
artifact-catalog (v1, v3), build-manifest (v1, v2, v3), the contract v2
release-trust documents (release-root, release-delegations, release-trust,
release-record v2/v3), and the contract v3 diversification documents
(diversification-profile, diversification-seed). Each document is
checked against its JSON Schema in docs/schemas/ (with a dependency-free
validator for the keyword subset those schemas use) and then against the
cross-field rules JSON Schema cannot express: reserved profiles, recomputed
toolchain identity, origin/class/revision agreement, and the artifact-proxy
rule that custom runtime routes never resolve to official bytes.

Usage:
  validate_hardening_contracts.py security-policy FILE [--production]
  validate_hardening_contracts.py build-identity FILE
  validate_hardening_contracts.py artifact-catalog FILE [--build-identity FILE]
  validate_hardening_contracts.py build-manifest FILE
  validate_hardening_contracts.py release-root|release-delegations FILE   (signed envelope or payload)
  validate_hardening_contracts.py release-trust FILE
  validate_hardening_contracts.py release-record FILE
  validate_hardening_contracts.py diversification-profile|diversification-seed FILE

Release-trust documents are checked for shape here; their signatures are
verified against a pinned root by scripts/release_trust.py verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inventory_release_binaries import REQUIRES_CUSTOM_ROUTE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / 'docs' / 'schemas'
CONTRACT_VERSION = 3
# Every schema file per kind, by contract version. A document of a versioned
# kind is checked against the schema its own contract_version names.
SCHEMAS = {
    'security-policy': {1: 'security-policy.v1.schema.json', 2: 'security-policy.v2.schema.json',
                        3: 'security-policy.v3.schema.json'},
    'build-identity': {1: 'build-identity.v1.schema.json', 3: 'build-identity.v3.schema.json'},
    'artifact-catalog': {1: 'artifact-catalog.v1.schema.json', 3: 'artifact-catalog.v3.schema.json'},
    'build-manifest': {1: 'build-manifest.v1.schema.json', 2: 'build-manifest.v2.schema.json',
                       3: 'build-manifest.v3.schema.json'},
    'release-root': {2: 'release-root.v2.schema.json'},
    'release-delegations': {2: 'release-delegations.v2.schema.json'},
    'release-trust': {2: 'release-trust.v2.schema.json'},
    'release-record': {2: 'release-record.v2.schema.json', 3: 'release-record.v3.schema.json'},
    'diversification-profile': {3: 'diversification-profile.v3.schema.json'},
    'diversification-seed': {3: 'diversification-seed.v3.schema.json'},
}
# The newest schema per kind.
KINDS = {kind: files[max(files)] for kind, files in SCHEMAS.items()}
# Reserved before contract v3 (P5 defines it in v3 only).
RESERVED_PROFILES = {'hardened-diversified'}
DIVERSIFIED_CONTRACT_VERSION = 3


class ContractError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Canonical form and identities


def _reject_floats(value: Any, path: str = '$') -> None:
    if isinstance(value, float):
        raise ContractError(f'{path}: floating-point values are not allowed in canonical documents')
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f'{path}[{index}]')


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8, integers only."""
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sha256_id(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(canonical_json(value)).hexdigest()


def toolchain_id(identity: dict) -> str:
    """The toolchain identity: fork sources, engine revision, and profile.

    Output digests are deliberately excluded so the compiled policy can embed
    this value without a self-referential hash (plan section 5.2).
    """
    return sha256_id({
        'contract_version': identity['contract_version'],
        'security_profile': identity['security_profile'],
        'diversification_profile_id': identity['diversification_profile_id'],
        'engine_revision': identity['toolchain']['engine_revision'],
        'fork': identity['fork'],
    })


# ---------------------------------------------------------------------------
# JSON Schema subset


def _type_ok(value: Any, expected: str) -> bool:
    if expected == 'object':
        return isinstance(value, dict)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'null':
        return value is None
    raise ContractError(f'unsupported schema type {expected}')


SUPPORTED_KEYWORDS = {
    '$schema', '$id', '$defs', '$ref', 'title', 'description', 'type', 'const', 'enum', 'pattern',
    'minLength', 'minimum', 'properties', 'required', 'additionalProperties', 'propertyNames',
    'minProperties', 'items', 'minItems', 'uniqueItems', 'allOf', 'if', 'then',
}


def schema_errors(value: Any, schema: dict, root: dict, path: str = '$') -> list[str]:
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise ContractError(f'schema uses unsupported keywords: {sorted(unknown)}')
    if '$ref' in schema:
        reference = schema['$ref']
        if not reference.startswith('#/$defs/'):
            raise ContractError(f'unsupported $ref {reference}')
        return schema_errors(value, root['$defs'][reference.split('/')[-1]], root, path)
    errors: list[str] = []
    if 'type' in schema:
        types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(_type_ok(value, expected) for expected in types):
            return [f'{path}: expected {"/".join(types)}']
    if 'const' in schema and (value != schema['const'] or type(value) is not type(schema['const'])):
        errors.append(f'{path}: must be {json.dumps(schema["const"])}')
    if 'enum' in schema and not any(value == item and type(value) is type(item) for item in schema['enum']):
        errors.append(f'{path}: must be one of {json.dumps(schema["enum"])}')
    if isinstance(value, str):
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(f'{path}: does not match {schema["pattern"]}')
        if len(value) < schema.get('minLength', 0):
            errors.append(f'{path}: too short')
    if isinstance(value, int) and not isinstance(value, bool) and 'minimum' in schema and value < schema['minimum']:
        errors.append(f'{path}: below minimum {schema["minimum"]}')
    if isinstance(value, dict):
        for name in schema.get('required', []):
            if name not in value:
                errors.append(f'{path}: missing required property {name}')
        if len(value) < schema.get('minProperties', 0):
            errors.append(f'{path}: needs at least {schema["minProperties"]} properties')
        properties = schema.get('properties', {})
        for name, item in value.items():
            if 'propertyNames' in schema:
                errors.extend(schema_errors(name, schema['propertyNames'], root, f'{path}.<name {name}>'))
            if name in properties:
                errors.extend(schema_errors(item, properties[name], root, f'{path}.{name}'))
            elif schema.get('additionalProperties') is False:
                errors.append(f'{path}: unknown property {name}')
            elif isinstance(schema.get('additionalProperties'), dict):
                errors.extend(schema_errors(item, schema['additionalProperties'], root, f'{path}.{name}'))
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errors.append(f'{path}: needs at least {schema["minItems"]} items')
        if schema.get('uniqueItems'):
            seen = [canonical_json(item) for item in value]
            if len(set(seen)) != len(seen):
                errors.append(f'{path}: items must be unique')
        if 'items' in schema:
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, schema['items'], root, f'{path}[{index}]'))
    for clause in schema.get('allOf', []):
        if 'if' in clause:
            if not schema_errors(value, clause['if'], root, path):
                errors.extend(schema_errors(value, clause.get('then', {}), root, path))
        else:
            errors.extend(schema_errors(value, clause, root, path))
    return errors


def load_schema(kind: str, version: int | None = None) -> dict:
    name = KINDS[kind] if version is None else SCHEMAS[kind][version]
    return json.loads((SCHEMA_DIR / name).read_text(encoding='utf-8'))


def _versioned_schema(document: Any, kind: str) -> tuple[dict | None, list[str]]:
    versions = sorted(SCHEMAS[kind])
    version = document.get('contract_version') if isinstance(document, dict) else None
    if len(versions) == 1:
        return load_schema(kind, versions[0]), []
    if type(version) is not int or version not in versions:
        return None, [f'$.contract_version: must be one of {json.dumps(versions)}']
    return load_schema(kind, version), []


# ---------------------------------------------------------------------------
# Cross-field rules


def _common(document: dict, kind: str) -> list[str]:
    schema, errors = _versioned_schema(document, kind)
    if schema is None:
        return errors
    errors = schema_errors(document, schema, schema)
    if errors:
        return errors
    version = document['contract_version']
    if version >= DIVERSIFIED_CONTRACT_VERSION:
        # The v3 schemas pin hardened-diversified and a sha256: profile id.
        return errors
    if document['security_profile'] in RESERVED_PROFILES:
        errors.append(f'$.security_profile: {document["security_profile"]} is reserved and not admissible '
                      f'under contract v{version}')
    if document['diversification_profile_id'] is not None:
        errors.append(f'$.diversification_profile_id: must be null under contract v{version}')
    return errors


def _development_profile_rules(document: dict, production: bool) -> list[str]:
    """The DEVELOPMENT-ONLY diversification profile (contract section 11.2)."""
    identifier = document.get('diversification_profile_id')
    if identifier is None or identifier not in _diversification().development_profile_ids():
        return []
    if production:
        return ['$.diversification_profile_id: production packaging rejects the DEVELOPMENT-ONLY '
                'diversification profile']
    if not document.get('development_exceptions'):
        return ['$.diversification_profile_id: the DEVELOPMENT-ONLY diversification profile requires '
                'development_exceptions']
    return []


def validate_security_policy(policy: dict, *, production: bool = False) -> list[str]:
    errors = _common(policy, 'security-policy')
    if errors:
        return errors
    if (policy['signatures'] == 'required') != bool(policy['trust_roots']):
        errors.append('$.signatures: "required" exactly when at least one trust root is pinned')
    key_ids = [root['key_id'] for root in policy['trust_roots']]
    if len(set(key_ids)) != len(key_ids):
        errors.append('$.trust_roots: key_id values must be unique')
    if production and policy['development_exceptions']:
        errors.append('$.development_exceptions: production packaging rejects development-only exceptions')
    release_root = policy.get('release_root')
    if release_root is not None:
        if release_root['threshold'] > len(release_root['key_ids']):
            errors.append('$.release_root.threshold: cannot exceed the number of root keys')
        # Contract v2 role separation: a release-root key never signs patches.
        shared = sorted(set(key_ids) & set(release_root['key_ids']))
        if shared:
            errors.append(f'$.release_root.key_ids: {shared} also pinned as patch-signing trust roots; '
                          'the release root and patch signing use separate keys')
        if set(release_root['key_ids']) & _release_trust().development_root_key_ids():
            if production:
                errors.append('$.release_root: production packaging rejects the DEVELOPMENT-ONLY release root')
            elif not policy['development_exceptions']:
                errors.append('$.release_root: the DEVELOPMENT-ONLY release root requires development_exceptions')
    errors.extend(_development_profile_rules(policy, production))
    return errors


def validate_build_identity(identity: dict) -> list[str]:
    errors = _common(identity, 'build-identity')
    if errors:
        return errors
    expected = toolchain_id(identity)
    if identity['toolchain']['toolchain_id'] != expected:
        errors.append(f'$.toolchain.toolchain_id: expected {expected}')
    names = [artifact['name'] for artifact in identity['artifacts']]
    if len(set(names)) != len(names):
        errors.append('$.artifacts: names must be unique')
    return errors


def requires_custom(path: str) -> bool:
    return any(pattern.search('/' + path) for pattern in REQUIRES_CUSTOM_ROUTE)


def validate_artifact_catalog(catalog: dict, identity: dict | None = None) -> list[str]:
    errors = _common(catalog, 'artifact-catalog')
    if errors:
        return errors
    origins = catalog['origins']
    paths = set()
    for index, artifact in enumerate(catalog['artifacts']):
        where = f'$.artifacts[{index}]'
        if artifact['path'] in paths:
            errors.append(f'{where}.path: duplicate route {artifact["path"]}')
        paths.add(artifact['path'])
        origin = origins.get(artifact['origin'])
        if origin is None:
            errors.append(f'{where}.origin: unknown origin {artifact["origin"]}')
            continue
        if artifact['class'] == 'official-unchanged':
            if origin['class'] != 'official':
                errors.append(f'{where}: official-unchanged artifacts must use an official origin')
            if artifact['revision'] != catalog['upstream_engine_revision']:
                errors.append(f'{where}.revision: official-unchanged artifacts use upstream_engine_revision')
            if requires_custom(artifact['path']):
                errors.append(f'{where}: {artifact["path"]} is a custom runtime/SDK route; '
                              'an official artifact must never satisfy it')
        else:
            if origin['class'] != 'custom-project':
                errors.append(f'{where}: custom-toolchain artifacts must use a custom-project origin')
            if artifact['revision'] != catalog['engine_revision']:
                errors.append(f'{where}.revision: custom-toolchain artifacts use engine_revision')
    if identity is not None:
        identity_errors = validate_build_identity(identity)
        if identity_errors:
            return errors + [f'build identity: {error}' for error in identity_errors]
        pairs = (
            ('toolchain_id', identity['toolchain']['toolchain_id']),
            ('engine_revision', identity['toolchain']['engine_revision']),
            ('upstream_engine_revision', identity['upstream']['flutter_engine']['revision']),
            ('security_profile', identity['security_profile']),
            ('diversification_profile_id', identity['diversification_profile_id']),
        )
        for field, expected in pairs:
            if catalog[field] != expected:
                errors.append(f'$.{field}: must equal the build identity ({expected})')
    return errors


def validate_build_manifest(manifest: dict) -> list[str]:
    """Hardening P3/P4 build manifest (the decoded JSON of the shorebird.yaml line)."""
    schema, errors = _versioned_schema(manifest, 'build-manifest')
    if schema is None:
        return errors
    errors = schema_errors(manifest, schema, schema)
    if errors:
        return errors
    targets = [(module['platform'], module['arch']) for module in manifest['modules']]
    if len(set(targets)) != len(targets):
        errors.append('$.modules: one entry per platform/arch')
    for field in ('android_signing_cert_sha256', 'apple_team_ids', 'windows_signing_cert_sha256'):
        values = manifest['package_identity'].get(field, [])
        if len(set(values)) != len(values):
            errors.append(f'$.package_identity.{field}: values must be unique')
    errors.extend(_development_profile_rules(manifest, production=False))
    return errors


def _diversification():
    # Lazy: diversification_profile imports this module for canonical_json.
    import diversification_profile
    return diversification_profile


def validate_diversification_profile(profile: Any) -> list[str]:
    """Shape and permutation rules; the opcode table is checked when the Dart
    SDK checkout is present (scripts/diversification_profile.py verify)."""
    dp = _diversification()
    names = dp.opcode_names() if dp.CONSTANTS_KBC.is_file() else None
    return dp.validate_profile(profile, names)


def validate_diversification_seed(record: Any) -> list[str]:
    return _diversification().validate_seed_record(record)


def _release_trust():
    # Lazy: release_trust imports this module for canonical_json/schema_errors.
    import release_trust
    return release_trust


def validate_release_payload(document: Any, kind: str) -> list[str]:
    """A release-root or release-delegations document: a signed envelope
    (whose canonical payload is checked) or the bare payload."""
    rt = _release_trust()
    if isinstance(document, dict) and 'payload' in document:
        try:
            payload, _ = rt.open_envelope(document, kind)
            rt.parse_payload(payload, kind)
        except rt.TrustError as error:
            return [str(error)]
        return []
    try:
        return rt.validate_payload(document, kind)
    except rt.TrustError as error:
        return [str(error)]


def validate_release_trust(bundle: Any) -> list[str]:
    """Shape of a release-trust bundle and of every payload it carries."""
    schema = load_schema('release-trust')
    errors = schema_errors(bundle, schema, schema)
    if errors:
        return errors
    for index, item in enumerate(bundle['root_chain']):
        errors.extend(f'$.root_chain[{index}]: {error}' for error in validate_release_payload(item, 'release-root'))
    errors.extend(f'$.delegations: {error}'
                  for error in validate_release_payload(bundle['delegations'], 'release-delegations'))
    return errors


def validate_release_record(record: Any) -> list[str]:
    """What a hardened release stores as UpdateReleaseMetadata.security_record
    (v2), or a hardened-diversified release (v3)."""
    schema, errors = _versioned_schema(record, 'release-record')
    if schema is None:
        return errors
    errors = schema_errors(record, schema, schema)
    if errors:
        return errors
    digests = [entry['manifest_sha256'] for entry in record['build_manifests']]
    if len(set(digests)) != len(digests):
        errors.append('$.build_manifests: one entry per manifest')
    for index, entry in enumerate(record['build_manifests']):
        targets = [(module['platform'], module['arch']) for module in entry['modules']]
        if len(set(targets)) != len(targets):
            errors.append(f'$.build_manifests[{index}].modules: one entry per platform/arch')
    return errors


VALIDATORS = {
    'security-policy': validate_security_policy,
    'build-identity': validate_build_identity,
    'artifact-catalog': validate_artifact_catalog,
    'build-manifest': validate_build_manifest,
    'release-root': lambda document: validate_release_payload(document, 'release-root'),
    'release-delegations': lambda document: validate_release_payload(document, 'release-delegations'),
    'release-trust': validate_release_trust,
    'release-record': validate_release_record,
    'diversification-profile': validate_diversification_profile,
    'diversification-seed': validate_diversification_seed,
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'),
                      object_pairs_hook=_reject_duplicate_keys)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f'duplicate JSON key {key}')
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('kind', choices=sorted(KINDS))
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--production', action='store_true',
                        help='security-policy: also apply production packaging admission')
    parser.add_argument('--build-identity', type=Path,
                        help='artifact-catalog: also require agreement with this build identity')
    args = parser.parse_args(argv)
    failed = False
    for path in args.files:
        try:
            document = _load(path)
            if args.kind == 'security-policy':
                errors = validate_security_policy(document, production=args.production)
            elif args.kind == 'artifact-catalog':
                identity = _load(args.build_identity) if args.build_identity else None
                errors = validate_artifact_catalog(document, identity)
            else:
                errors = VALIDATORS[args.kind](document)
        except (OSError, json.JSONDecodeError, ContractError) as error:
            errors = [str(error)]
        if errors:
            failed = True
            for error in errors:
                print(f'{path}: {error}', file=sys.stderr)
        else:
            version = document.get('contract_version') if isinstance(document, dict) else None
            print(f'{path}: valid {args.kind} (contract v{version or CONTRACT_VERSION})')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

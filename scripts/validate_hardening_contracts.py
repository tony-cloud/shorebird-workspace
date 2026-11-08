#!/usr/bin/env python3
"""Validate hardening contract v1 documents (docs/HARDENING_CONTRACTS.md).

Kinds: security-policy, build-identity, artifact-catalog. Each document is
checked against its JSON Schema in docs/schemas/ (with a dependency-free
validator for the keyword subset those schemas use) and then against the
cross-field rules JSON Schema cannot express: reserved profiles, recomputed
toolchain identity, origin/class/revision agreement, and the artifact-proxy
rule that custom runtime routes never resolve to official bytes.

Usage:
  validate_hardening_contracts.py security-policy FILE [--production]
  validate_hardening_contracts.py build-identity FILE
  validate_hardening_contracts.py artifact-catalog FILE [--build-identity FILE]
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
CONTRACT_VERSION = 1
KINDS = {
    'security-policy': 'security-policy.v1.schema.json',
    'build-identity': 'build-identity.v1.schema.json',
    'artifact-catalog': 'artifact-catalog.v1.schema.json',
    'build-manifest': 'build-manifest.v1.schema.json',
}
RESERVED_PROFILES = {'hardened-diversified'}


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


def load_schema(kind: str) -> dict:
    return json.loads((SCHEMA_DIR / KINDS[kind]).read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# Cross-field rules


def _common(document: dict, kind: str) -> list[str]:
    schema = load_schema(kind)
    errors = schema_errors(document, schema, schema)
    if errors:
        return errors
    if document['security_profile'] in RESERVED_PROFILES:
        errors.append(f'$.security_profile: {document["security_profile"]} is reserved and not admissible under contract v1')
    if document['diversification_profile_id'] is not None:
        errors.append('$.diversification_profile_id: must be null under contract v1')
    return errors


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
    """Hardening P3 build manifest (the decoded JSON of the shorebird.yaml line)."""
    schema = load_schema('build-manifest')
    errors = schema_errors(manifest, schema, schema)
    if errors:
        return errors
    targets = [(module['platform'], module['arch']) for module in manifest['modules']]
    if len(set(targets)) != len(targets):
        errors.append('$.modules: one entry per platform/arch')
    for field in ('android_signing_cert_sha256', 'apple_team_ids'):
        values = manifest['package_identity'][field]
        if len(set(values)) != len(values):
            errors.append(f'$.package_identity.{field}: values must be unique')
    return errors


VALIDATORS = {
    'security-policy': validate_security_policy,
    'build-identity': validate_build_identity,
    'artifact-catalog': validate_artifact_catalog,
    'build-manifest': validate_build_manifest,
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
            elif args.kind == 'build-manifest':
                errors = validate_build_manifest(document)
            else:
                errors = validate_build_identity(document)
        except (OSError, json.JSONDecodeError, ContractError) as error:
            errors = [str(error)]
        if errors:
            failed = True
            for error in errors:
                print(f'{path}: {error}', file=sys.stderr)
        else:
            print(f'{path}: valid {args.kind} (contract v{CONTRACT_VERSION})')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

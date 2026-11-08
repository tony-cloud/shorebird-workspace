#!/usr/bin/env python3
"""Tests for the hardening contract v1 schemas, examples, and validator."""

from __future__ import annotations

import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_hardening_contracts as v  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'docs/schemas/examples'
CONTRACT_DOC = ROOT / 'docs/HARDENING_CONTRACTS.md'


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))


def rehash(identity: dict) -> dict:
    identity['toolchain']['toolchain_id'] = v.toolchain_id(identity)
    return identity


class SchemaFilesTest(unittest.TestCase):
    def test_schemas_are_strict_and_documented(self):
        doc = CONTRACT_DOC.read_text(encoding='utf-8')
        for kind, file_name in v.KINDS.items():
            schema = v.load_schema(kind)
            self.assertEqual(schema['additionalProperties'], False, kind)
            self.assertEqual(schema['properties']['contract_version'], {'const': 1}, kind)
            self.assertIn(f'docs/schemas/{file_name}', doc)
            # The subset validator refuses unknown keywords, so validating the
            # examples also proves the schema stays inside the supported subset.

    def test_examples_are_valid(self):
        self.assertEqual(v.validate_security_policy(load('security-policy.legacy-default.json'), production=True), [])
        self.assertEqual(v.validate_security_policy(load('security-policy.hardened.json'), production=True), [])
        identity = load('build-identity.baseline-ae8a61a.json')
        self.assertEqual(v.validate_build_identity(identity), [])
        self.assertEqual(v.validate_artifact_catalog(load('artifact-catalog.baseline-ae8a61a.json'), identity), [])

    def test_optional_reference_validator_agrees(self):
        try:
            import jsonschema  # type: ignore
        except ImportError:
            self.skipTest('jsonschema is not installed; the built-in subset validator is authoritative')
        for kind, name in (('security-policy', 'security-policy.hardened.json'),
                           ('build-identity', 'build-identity.baseline-ae8a61a.json'),
                           ('artifact-catalog', 'artifact-catalog.baseline-ae8a61a.json')):
            jsonschema.validate(load(name), v.load_schema(kind))


class SecurityPolicyTest(unittest.TestCase):
    def assertRejected(self, policy: dict, fragment: str, **kwargs):
        errors = v.validate_security_policy(policy, **kwargs)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_hardened_floor_cannot_be_lowered(self):
        base = load('security-policy.hardened.json')
        for field, value in (('signatures', 'optional'), ('patch_verification', 'install_only'),
                             ('patch_response_verification', 'optional'), ('update_origin_policy', 'any'),
                             ('trust_roots', []), ('toolchain_id', None)):
            with self.subTest(field=field):
                policy = copy.deepcopy(base)
                policy[field] = value
                self.assertNotEqual(v.validate_security_policy(policy), [])

    def test_reserved_and_versioning(self):
        policy = load('security-policy.hardened.json')
        policy['security_profile'] = 'hardened-diversified'
        self.assertRejected(policy, 'reserved')
        for field, value, fragment in (('contract_version', 2, 'must be 1'),
                                       ('contract_version', '1', 'must be 1'),
                                       ('contract_version', True, 'must be 1'),
                                       ('security_profile', 'Hardened', 'must be one of')):
            policy = load('security-policy.legacy-default.json')
            policy[field] = value
            self.assertRejected(policy, fragment)
        policy = load('security-policy.legacy-default.json')
        policy['future_field'] = 1
        self.assertRejected(policy, 'unknown property future_field')
        policy = load('security-policy.legacy-default.json')
        del policy['trust_roots']
        self.assertRejected(policy, 'missing required property trust_roots')

    def test_diversification_and_development_rules(self):
        policy = load('security-policy.legacy-default.json')
        policy['diversification_profile_id'] = 'seed-1'
        self.assertRejected(policy, 'diversification_profile_id')
        policy = load('security-policy.legacy-default.json')
        policy['development_exceptions'] = True
        self.assertRejected(policy, 'development_exceptions')
        policy = load('security-policy.hardened.json')
        policy['development_exceptions'] = True
        self.assertEqual(v.validate_security_policy(policy), [])
        self.assertRejected(policy, 'production packaging', production=True)

    def test_signatures_match_trust_roots(self):
        policy = load('security-policy.legacy-default.json')
        policy['signatures'] = 'required'
        self.assertRejected(policy, 'trust root')
        policy = load('security-policy.hardened.json')
        policy['trust_roots'].append(dict(policy['trust_roots'][0], role='patch-signing'))
        self.assertRejected(policy, 'unique')
        policy = load('security-policy.hardened.json')
        policy['trust_roots'][0]['key_id'] = 'sha256:ABC'
        self.assertRejected(policy, 'does not match')


class BuildIdentityTest(unittest.TestCase):
    def test_toolchain_id_binds_fork_engine_and_profile(self):
        base = load('build-identity.baseline-ae8a61a.json')
        for mutate in (lambda d: d['fork']['updater'].update(revision='0' * 40),
                       lambda d: d['toolchain'].update(engine_revision='1' * 40),
                       lambda d: d.update(security_profile='hardened')):
            identity = copy.deepcopy(base)
            mutate(identity)
            self.assertTrue(any('toolchain_id' in e for e in v.validate_build_identity(identity)))
            self.assertEqual(v.validate_build_identity(rehash(identity)), [])

    def test_output_digests_and_upstream_do_not_change_toolchain_id(self):
        base = load('build-identity.baseline-ae8a61a.json')
        identity = copy.deepcopy(base)
        identity['artifacts'][0]['sha256'] = 'f' * 64
        identity['upstream']['shorebird']['revision'] = 'e' * 40
        self.assertEqual(v.toolchain_id(identity), base['toolchain']['toolchain_id'])

    def test_shape_rules(self):
        base = load('build-identity.baseline-ae8a61a.json')
        identity = copy.deepcopy(base)
        identity['artifacts'].append(dict(identity['artifacts'][0]))
        self.assertIn('$.artifacts: names must be unique', v.validate_build_identity(identity))
        identity = copy.deepcopy(base)
        del identity['upstream']['dart']
        self.assertTrue(v.validate_build_identity(identity))
        identity = copy.deepcopy(base)
        identity['fork']['flutter']['repository'] = 'http://github.com/tony-cloud/flutter.git'
        self.assertTrue(v.validate_build_identity(identity))
        identity = copy.deepcopy(base)
        identity['upstream']['flutter']['revision'] = '737ba8f'
        self.assertTrue(v.validate_build_identity(identity))
        identity = copy.deepcopy(base)
        identity['diversification_profile_id'] = 'p1'
        self.assertTrue(any('diversification' in e for e in v.validate_build_identity(rehash(identity))))

    def test_canonical_json_rejects_floats(self):
        with self.assertRaises(v.ContractError):
            v.canonical_json({'size': 1.0})
        self.assertEqual(v.canonical_json({'b': 1, 'a': ['é']}), '{"a":["é"],"b":1}'.encode('utf-8'))


class ArtifactCatalogTest(unittest.TestCase):
    def setUp(self):
        self.identity = load('build-identity.baseline-ae8a61a.json')
        self.catalog = load('artifact-catalog.baseline-ae8a61a.json')

    def errors(self, catalog: dict, identity: dict | None = None) -> list[str]:
        return v.validate_artifact_catalog(catalog, identity)

    def test_official_bytes_never_satisfy_custom_routes(self):
        for route in ('android-arm64-release/artifacts.zip', 'darwin-arm64-release/FlutterMacOS.framework.zip',
                      'linux-x64/font-subset.zip', 'dart-sdk-linux-x64.zip', 'flutter_patched_sdk_product.zip',
                      'windows-x64-release/windows-x64-flutter.zip'):
            catalog = copy.deepcopy(self.catalog)
            official = next(a for a in catalog['artifacts'] if a['class'] == 'official-unchanged')
            official['path'] = 'flutter_infra_release/flutter/$engine/' + route
            catalog['artifacts'] = [a for a in catalog['artifacts'] if a is official or a['path'] != official['path']]
            with self.subTest(route=route):
                self.assertTrue(any('never satisfy' in e for e in self.errors(catalog)))
        catalog = copy.deepcopy(self.catalog)
        maven = next(a for a in catalog['artifacts'] if a['class'] == 'official-unchanged')
        maven['path'] = 'download.flutter.io/io/flutter/arm64_v8a_release/1.0.0-$engine/arm64_v8a_release-1.0.0-$engine.jar'
        self.assertTrue(any('never satisfy' in e for e in self.errors(catalog)))

    def test_origin_class_and_revision_agree(self):
        cases = []
        catalog = copy.deepcopy(self.catalog)
        catalog['artifacts'][0]['origin'] = 'flutter-official'
        cases.append((catalog, 'custom-project origin'))
        catalog = copy.deepcopy(self.catalog)
        catalog['artifacts'][0]['revision'] = catalog['upstream_engine_revision']
        cases.append((catalog, 'use engine_revision'))
        catalog = copy.deepcopy(self.catalog)
        official = next(a for a in catalog['artifacts'] if a['class'] == 'official-unchanged')
        official['revision'] = catalog['engine_revision']
        cases.append((catalog, 'use upstream_engine_revision'))
        catalog = copy.deepcopy(self.catalog)
        official = next(a for a in catalog['artifacts'] if a['class'] == 'official-unchanged')
        official['origin'] = 'project-artifacts'
        cases.append((catalog, 'official origin'))
        catalog = copy.deepcopy(self.catalog)
        catalog['artifacts'][0]['origin'] = 'mirror'
        cases.append((catalog, 'unknown origin'))
        catalog = copy.deepcopy(self.catalog)
        catalog['artifacts'].append(dict(catalog['artifacts'][0]))
        cases.append((catalog, 'duplicate route'))
        catalog = copy.deepcopy(self.catalog)
        catalog['origins']['project-artifacts']['base_url'] = 'http://localhost:8080/artifacts'
        cases.append((catalog, 'does not match'))
        catalog = copy.deepcopy(self.catalog)
        catalog['origins']['shorebird-hosted'] = {'class': 'implicit-shorebird-hosted', 'base_url': 'https://x.dev'}
        cases.append((catalog, 'must be one of'))
        catalog = copy.deepcopy(self.catalog)
        catalog['artifacts'][0]['path'] = 'flutter_infra_release/flutter/44a553c3b3c7797c7f0f20961d46a8b17e06444c/x.zip'
        cases.append((catalog, 'does not match'))
        for catalog, fragment in cases:
            with self.subTest(fragment=fragment):
                errors = self.errors(catalog)
                self.assertTrue(any(fragment in e for e in errors), errors)

    def test_catalog_binds_to_build_identity(self):
        self.assertEqual(self.errors(self.catalog, self.identity), [])
        identity = rehash(copy.deepcopy(self.identity) | {'fork': {
            **self.identity['fork'], 'updater': {**self.identity['fork']['updater'], 'revision': 'a' * 40}}})
        self.assertTrue(any('toolchain_id' in e for e in self.errors(self.catalog, identity)))
        identity = copy.deepcopy(self.identity)
        identity['upstream']['flutter_engine']['revision'] = 'b' * 40
        self.assertTrue(any('upstream_engine_revision' in e for e in self.errors(self.catalog, identity)))


class CliTest(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            status = v.main(list(args))
        return status, stderr.getvalue()

    def test_cli(self):
        status, _ = self.run_cli('artifact-catalog', str(EXAMPLES / 'artifact-catalog.baseline-ae8a61a.json'),
                                 '--build-identity', str(EXAMPLES / 'build-identity.baseline-ae8a61a.json'))
        self.assertEqual(status, 0)
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / 'dup.json'
            duplicate.write_text('{"schema": "a", "schema": "b"}')
            status, stderr = self.run_cli('security-policy', str(duplicate))
            self.assertEqual(status, 1)
            self.assertIn('duplicate JSON key schema', stderr)
            dev = Path(directory) / 'dev.json'
            dev.write_text(json.dumps(load('security-policy.hardened.json') | {'development_exceptions': True}))
            self.assertEqual(self.run_cli('security-policy', str(dev))[0], 0)
            self.assertEqual(self.run_cli('security-policy', str(dev), '--production')[0], 1)


if __name__ == '__main__':
    unittest.main()

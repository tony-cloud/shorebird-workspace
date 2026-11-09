#!/usr/bin/env python3
"""Tests for the hardening contract (v1, v2) schemas, examples, and validator."""

from __future__ import annotations

import base64
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_trust as rt  # noqa: E402
import validate_hardening_contracts as v  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'docs/schemas/examples'
CONTRACT_DOC = ROOT / 'docs/HARDENING_CONTRACTS.md'
RELEASE_TRUST_TESTDATA = ROOT / 'scripts/testdata/release-trust'


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))


def rehash(identity: dict) -> dict:
    identity['toolchain']['toolchain_id'] = v.toolchain_id(identity)
    return identity


class SchemaFilesTest(unittest.TestCase):
    def test_schemas_are_strict_and_documented(self):
        doc = CONTRACT_DOC.read_text(encoding='utf-8')
        for kind, files in v.SCHEMAS.items():
            for version, file_name in files.items():
                schema = v.load_schema(kind, version)
                self.assertEqual(schema['additionalProperties'], False, kind)
                self.assertEqual(schema['properties']['contract_version'], {'const': version}, file_name)
                self.assertIn(f'.v{version}.schema.json', file_name)
                self.assertIn(f'docs/schemas/{file_name}', doc)
            # The subset validator refuses unknown keywords, so validating the
            # examples also proves the schema stays inside the supported subset.

    def test_examples_are_valid(self):
        self.assertEqual(v.validate_security_policy(load('security-policy.legacy-default.json'), production=True), [])
        self.assertEqual(v.validate_security_policy(load('security-policy.hardened.json'), production=True), [])
        identity = load('build-identity.baseline-ae8a61a.json')
        self.assertEqual(v.validate_build_identity(identity), [])
        self.assertEqual(v.validate_artifact_catalog(load('artifact-catalog.baseline-ae8a61a.json'), identity), [])
        self.assertEqual(v.validate_build_manifest(load('build-manifest.linux-x64.json')), [])
        # Contract v2 (hardening P4).
        for name in ('security-policy.v2.legacy-default.json', 'security-policy.v2.hardened.json'):
            self.assertEqual(v.validate_security_policy(load(name), production=True), [], name)
        self.assertEqual(v.validate_build_manifest(load('build-manifest.v2.linux-x64.json')), [])
        self.assertEqual(v.validate_release_record(load('release-record.v2.json')), [])
        for kind, name in (('release-root', 'root.v1.DEVELOPMENT-ONLY.json'),
                           ('release-delegations', 'delegations.v1.DEVELOPMENT-ONLY.json'),
                           ('release-trust', 'release-trust.DEVELOPMENT-ONLY.json')):
            document = json.loads((RELEASE_TRUST_TESTDATA / name).read_text(encoding='utf-8'))
            self.assertEqual(v.VALIDATORS[kind](document), [], name)

    def test_v2_examples_agree(self):
        """The record, manifest and policy examples describe one release."""
        policy = load('security-policy.v2.hardened.json')
        manifest = load('build-manifest.v2.linux-x64.json')
        record = load('release-record.v2.json')
        entry = record['build_manifests'][0]
        self.assertEqual(record['release_root_sha256'], policy['release_root']['root_sha256'])
        self.assertEqual(record['toolchain_id'], policy['toolchain_id'])
        self.assertEqual(record['delegations_sha256'], manifest['delegations_sha256'])
        self.assertEqual(entry['manifest_sha256'], hashlib.sha256(v.canonical_json(manifest)).hexdigest())
        for field in ('signer_key_id', 'config_sha256', 'modules'):
            self.assertEqual(entry[field], manifest[field], field)
    def test_optional_reference_validator_agrees(self):
        try:
            import jsonschema  # type: ignore
        except ImportError:
            self.skipTest('jsonschema is not installed; the built-in subset validator is authoritative')
        for kind, version, name in (('security-policy', 1, 'security-policy.hardened.json'),
                                    ('security-policy', 2, 'security-policy.v2.hardened.json'),
                                    ('build-identity', 1, 'build-identity.baseline-ae8a61a.json'),
                                    ('artifact-catalog', 1, 'artifact-catalog.baseline-ae8a61a.json'),
                                    ('build-manifest', 1, 'build-manifest.linux-x64.json'),
                                    ('build-manifest', 2, 'build-manifest.v2.linux-x64.json'),
                                    ('release-record', 2, 'release-record.v2.json'),
                                    ('security-policy', 3, 'security-policy.v3.hardened-diversified.json'),
                                    ('build-manifest', 3, 'build-manifest.v3.ios-arm64.json'),
                                    ('release-record', 3, 'release-record.v3.json')):
            jsonschema.validate(load(name), v.load_schema(kind, version))


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
        for field, value, fragment in (('contract_version', 4, 'must be one of [1, 2, 3]'),
                                       ('contract_version', 3, 'must be "hardened-diversified"'),
                                       ('contract_version', 2, 'missing required property release_root'),
                                       ('contract_version', '1', 'must be one of [1, 2, 3]'),
                                       ('contract_version', True, 'must be one of [1, 2, 3]'),
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

    def test_v1_documents_cannot_carry_a_release_root(self):
        policy = load('security-policy.legacy-default.json') | {'release_root': None}
        self.assertRejected(policy, 'unknown property release_root')

    def test_v2_release_root_rules(self):
        base = load('security-policy.v2.hardened.json')
        self.assertRejected(base | {'release_root': None}, 'release_root')
        legacy = load('security-policy.v2.legacy-default.json')
        self.assertRejected(legacy | {'release_root': base['release_root']}, 'release_root')
        for change, fragment in (({'threshold': 2}, 'cannot exceed'),
                                 ({'min_delegations_version': 0}, 'below minimum'),
                                 ({'key_ids': []}, 'at least 1'),
                                 ({'root_sha256': 'ab' * 32}, 'does not match'),
                                 ({'extra': 1}, 'unknown property extra')):
            policy = copy.deepcopy(base)
            policy['release_root'] |= change
            self.assertRejected(policy, fragment)
        # Role separation: a root key is never also a patch-signing root.
        policy = copy.deepcopy(base)
        policy['release_root']['key_ids'] = [policy['trust_roots'][0]['key_id']]
        self.assertRejected(policy, 'separate keys')
        # The DEVELOPMENT-ONLY root needs development exceptions and never ships.
        policy = copy.deepcopy(base)
        policy['release_root']['key_ids'] = sorted(rt.development_root_key_ids())
        self.assertRejected(policy, 'requires development_exceptions')
        policy['development_exceptions'] = True
        self.assertEqual(v.validate_security_policy(policy), [])
        self.assertRejected(policy, 'production packaging rejects the DEVELOPMENT-ONLY release root', production=True)

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


class BuildManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load('build-manifest.linux-x64.json')

    def test_only_hardened_and_exact_fields(self):
        for change in ({'security_profile': 'legacy'}, {'contract_version': 2},
                       {'extra': 1}, {'config_sha256': 'AB' * 32},
                       {'toolchain_id': 'sha256:1'}):
            self.assertNotEqual(v.validate_build_manifest(self.manifest | change), [], change)

    def test_modules_and_identities(self):
        module = self.manifest['modules'][0]
        self.assertNotEqual(v.validate_build_manifest(self.manifest | {'modules': [module, module]}), [])
        self.assertNotEqual(v.validate_build_manifest(self.manifest | {'modules': [module | {'platform': 'web'}]}), [])
        identity = {'android_signing_cert_sha256': ['ab' * 32], 'apple_team_ids': ['ABCDE12345']}
        self.assertEqual(v.validate_build_manifest(self.manifest | {'package_identity': identity}), [])
        for bad in ({'android_signing_cert_sha256': ['AB' * 32], 'apple_team_ids': []},
                    {'android_signing_cert_sha256': [], 'apple_team_ids': ['abcde12345']},
                    {'android_signing_cert_sha256': ['ab' * 32, 'ab' * 32], 'apple_team_ids': []}):
            self.assertNotEqual(v.validate_build_manifest(self.manifest | {'package_identity': bad}), [], bad)


class ReleaseDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.record = load('release-record.v2.json')
        self.bundle = json.loads((RELEASE_TRUST_TESTDATA / 'release-trust.DEVELOPMENT-ONLY.json')
                                 .read_text(encoding='utf-8'))

    def test_v2_manifest_binds_delegations(self):
        manifest = load('build-manifest.v2.linux-x64.json')
        without = {key: value for key, value in manifest.items() if key != 'delegations_sha256'}
        self.assertNotEqual(v.validate_build_manifest(without), [])
        self.assertNotEqual(v.validate_build_manifest(manifest | {'delegations_sha256': 'AB' * 32}), [])
        # A v1 manifest has no delegations binding.
        self.assertNotEqual(v.validate_build_manifest(manifest | {'contract_version': 1}), [])

    def test_release_record_rules(self):
        entry = self.record['build_manifests'][0]
        for change in ({'security_profile': 'legacy'}, {'contract_version': 1}, {'build_manifests': []},
                       {'build_manifests': [entry, entry]}, {'signing_key': 'x'},
                       {'build_manifests': [entry | {'modules': entry['modules'] * 2}]},
                       {'delegations_sha256': 'sha256:' + 'ab' * 32}):
            self.assertNotEqual(v.validate_release_record(self.record | change), [], change)

    def test_release_trust_payloads_are_checked(self):
        bundle = copy.deepcopy(self.bundle)
        bundle['delegations']['payload'] = bundle['delegations']['payload'][:-4]
        self.assertNotEqual(v.validate_release_trust(bundle), [])
        root = json.loads((RELEASE_TRUST_TESTDATA / 'root.v1.DEVELOPMENT-ONLY.json').read_text(encoding='utf-8'))
        # A root envelope is not a delegations document.
        self.assertTrue(any('trust_malformed' in error
                            for error in v.validate_release_payload(root, 'release-delegations')))
        payload = json.loads(base64.b64decode(root['payload']))
        self.assertEqual(v.validate_release_payload(payload, 'release-root'), [])
        self.assertNotEqual(v.validate_release_payload(payload | {'threshold': 2}, 'release-root'), [])
        self.assertNotEqual(v.validate_release_trust(self.bundle | {'contract_version': 1}), [])


DIVERSIFICATION_TESTDATA = ROOT / 'scripts/testdata/diversification'
DEVELOPMENT_PROFILE_ID = 'sha256:832856d0bed27833434e3f26b65d03cedc313f9850c2577ca7565c4dc644f46d'


class DiversifiedContractTest(unittest.TestCase):
    """Contract v3 (P5): hardened-diversified documents."""

    def assertErrors(self, errors: list[str], fragment: str):
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_v3_examples_are_valid_and_agree(self):
        policy = load('security-policy.v3.hardened-diversified.json')
        manifest = load('build-manifest.v3.ios-arm64.json')
        record = load('release-record.v3.json')
        self.assertEqual(v.validate_security_policy(policy, production=True), [])
        self.assertEqual(v.validate_build_manifest(manifest), [])
        self.assertEqual(v.validate_release_record(record), [])
        for document in (manifest, record):
            self.assertEqual(document['diversification_profile_id'], policy['diversification_profile_id'])
            self.assertEqual(document['toolchain_id'], policy['toolchain_id'])
        entry = record['build_manifests'][0]
        self.assertEqual(entry['manifest_sha256'], hashlib.sha256(v.canonical_json(manifest)).hexdigest())
        for name in ('profile.DEVELOPMENT-ONLY.json', 'seed.DEVELOPMENT-ONLY.json'):
            kind = 'diversification-profile' if name.startswith('profile') else 'diversification-seed'
            document = json.loads((DIVERSIFICATION_TESTDATA / name).read_text(encoding='utf-8'))
            self.assertEqual(v.VALIDATORS[kind](document), [], name)

    def test_v3_is_only_for_hardened_diversified(self):
        policy = load('security-policy.v3.hardened-diversified.json')
        for profile in ('hardened', 'legacy'):
            self.assertErrors(v.validate_security_policy(policy | {'security_profile': profile}),
                              'must be "hardened-diversified"')
        self.assertErrors(v.validate_security_policy(policy | {'diversification_profile_id': None}),
                          '$.diversification_profile_id')
        self.assertErrors(v.validate_security_policy(policy | {'diversification_profile_id': 'seed-1'}),
                          'does not match')
        # Hardened requirements still apply.
        for field, value in (('signatures', 'optional'), ('patch_verification', 'install_only'),
                             ('patch_response_verification', 'optional'), ('update_origin_policy', 'any'),
                             ('trust_roots', []), ('toolchain_id', None), ('release_root', None)):
            with self.subTest(field=field):
                self.assertNotEqual(v.validate_security_policy(policy | {field: value}), [])
        # A v2 (or v1) document can neither name the profile nor carry an id.
        hardened = load('security-policy.v2.hardened.json')
        self.assertErrors(v.validate_security_policy(hardened | {'security_profile': 'hardened-diversified'}),
                          'reserved')
        self.assertErrors(v.validate_security_policy(hardened | {'diversification_profile_id': DEVELOPMENT_PROFILE_ID}),
                          '$.diversification_profile_id')

    def test_development_profile_rules(self):
        policy = load('security-policy.v3.hardened-diversified.json') | {
            'diversification_profile_id': DEVELOPMENT_PROFILE_ID}
        self.assertErrors(v.validate_security_policy(policy), 'requires development_exceptions')
        policy['development_exceptions'] = True
        self.assertEqual(v.validate_security_policy(policy), [])
        self.assertErrors(v.validate_security_policy(policy, production=True),
                          'rejects the DEVELOPMENT-ONLY diversification profile')
        manifest = load('build-manifest.v3.ios-arm64.json') | {'diversification_profile_id': DEVELOPMENT_PROFILE_ID}
        self.assertErrors(v.validate_build_manifest(manifest), 'requires development_exceptions')
        self.assertEqual(v.validate_build_manifest(manifest | {'development_exceptions': True}), [])

    def test_v3_manifest_and_record_shape(self):
        manifest = load('build-manifest.v3.ios-arm64.json')
        missing = {k: val for k, val in manifest.items() if k != 'diversification_profile_id'}
        self.assertErrors(v.validate_build_manifest(missing), 'missing required property diversification_profile_id')
        self.assertErrors(v.validate_build_manifest(manifest | {'security_profile': 'hardened'}),
                          'must be "hardened-diversified"')
        v2 = load('build-manifest.v2.linux-x64.json')
        self.assertErrors(v.validate_build_manifest(v2 | {'diversification_profile_id': DEVELOPMENT_PROFILE_ID}),
                          'unknown property diversification_profile_id')
        record = load('release-record.v3.json')
        missing = {k: val for k, val in record.items() if k != 'diversification_profile_id'}
        self.assertErrors(v.validate_release_record(missing), 'missing required property diversification_profile_id')
        self.assertErrors(v.validate_release_record(record | {'contract_version': 4}), 'must be one of [2, 3]')
        v2record = load('release-record.v2.json')
        self.assertErrors(v.validate_release_record(v2record | {'diversification_profile_id': DEVELOPMENT_PROFILE_ID}),
                          'unknown property diversification_profile_id')

    def test_v3_build_identity_and_catalog(self):
        identity = load('build-identity.baseline-ae8a61a.json')
        catalog = load('artifact-catalog.baseline-ae8a61a.json')
        profile_id = load('security-policy.v3.hardened-diversified.json')['diversification_profile_id']
        identity.update(contract_version=3, security_profile='hardened-diversified',
                        diversification_profile_id=profile_id)
        rehash(identity)
        self.assertEqual(v.validate_build_identity(identity), [])
        # The toolchain id binds the profile id: another profile is another toolchain.
        other = copy.deepcopy(identity)
        other['diversification_profile_id'] = DEVELOPMENT_PROFILE_ID
        self.assertNotEqual(v.toolchain_id(other), identity['toolchain']['toolchain_id'])
        self.assertErrors(v.validate_build_identity(other), '$.toolchain.toolchain_id')
        catalog.update(contract_version=3, security_profile='hardened-diversified',
                       diversification_profile_id=profile_id,
                       toolchain_id=identity['toolchain']['toolchain_id'])
        self.assertEqual(v.validate_artifact_catalog(catalog, identity), [])
        self.assertErrors(v.validate_artifact_catalog(catalog | {'diversification_profile_id': DEVELOPMENT_PROFILE_ID},
                                                      identity), 'must equal the build identity')
        self.assertErrors(v.validate_artifact_catalog(catalog | {'security_profile': 'hardened'}),
                          'must be "hardened-diversified"')
        self.assertErrors(v.validate_artifact_catalog(catalog | {'diversification_profile_id': None}),
                          '$.diversification_profile_id')
        v1 = load('artifact-catalog.baseline-ae8a61a.json')
        self.assertErrors(v.validate_artifact_catalog(v1 | {'security_profile': 'hardened-diversified'}), 'reserved')


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
        for kind, path in (('release-trust', RELEASE_TRUST_TESTDATA / 'release-trust.DEVELOPMENT-ONLY.json'),
                           ('release-record', EXAMPLES / 'release-record.v2.json'),
                           ('build-manifest', EXAMPLES / 'build-manifest.v2.linux-x64.json'),
                           ('release-record', EXAMPLES / 'release-record.v3.json'),
                           ('build-manifest', EXAMPLES / 'build-manifest.v3.ios-arm64.json'),
                           ('diversification-profile', DIVERSIFICATION_TESTDATA / 'profile.DEVELOPMENT-ONLY.json'),
                           ('diversification-seed', DIVERSIFICATION_TESTDATA / 'seed.DEVELOPMENT-ONLY.json')):
            self.assertEqual(self.run_cli(kind, str(path)), (0, ''), kind)


if __name__ == '__main__':
    unittest.main()

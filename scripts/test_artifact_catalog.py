#!/usr/bin/env python3
"""Tests for scripts/artifact_catalog.py and scripts/dart_sdk_lock.py (P2)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_catalog as ac  # noqa: E402
import dart_sdk_lock as lock  # noqa: E402
from validate_hardening_contracts import canonical_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = '44a553c3b3c7797c7f0f20961d46a8b17e06444c'
UPSTREAM = 'cf636b1dfb818dd6a3ff16e390e93b1a036969c8'
TOOLCHAIN = 'sha256:' + '2' * 64
# A production key that exists only for these tests (seed is public).
PRODUCTION_SEED = hashlib.sha256(b'open-shorebird test production key').digest()

# RFC 8032 section 7.1, TEST 1-3.
RFC8032 = [
    ('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
     'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
     '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72',
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    ('c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
     'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025', 'af82',
     '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
]


def openssl_ed25519() -> bool:
    openssl = shutil.which('openssl')
    if openssl is None:
        return False
    result = subprocess.run([openssl, 'genpkey', '-algorithm', 'ed25519'], capture_output=True)
    return result.returncode == 0


def production_trust() -> dict:
    trust = ac.strict_loads(ac.TRUST_FILE.read_bytes())
    trust = copy.deepcopy(trust)
    trust['keys'].append(ac.trust_entry(ac.ed25519_public_key(PRODUCTION_SEED), 'production', 'test only'))
    return trust


OPERATOR_ROOT_SEED = bytes([2]) * 32


def release_trust_for(catalog_seeds=(PRODUCTION_SEED,), manifest_seeds=(bytes([7]) * 32,), revoked=()):
    """(bundle, root pin bytes) under an operator test root delegating the given keys."""
    import release_trust as rt  # noqa: PLC0415
    root = rt.root_payload([ac.ed25519_public_key(OPERATOR_ROOT_SEED)], version=1, threshold=1,
                           expires_at='2036-11-09T00:00:00Z')
    keys = lambda seeds: [rt.delegated_key(ac.ed25519_public_key(s), '2026-11-01T00:00:00Z',  # noqa: E731
                                           '2036-11-01T00:00:00Z') for s in seeds]
    payload = rt.delegations_payload(version=1, root_version=1, issued_at='2026-11-01T00:00:00Z',
                                     expires_at='2036-11-01T00:00:00Z',
                                     roles={'build-manifest': keys(manifest_seeds),
                                            'artifact-catalog': keys(catalog_seeds)},
                                     revoked=list(revoked))
    bundle = rt.bundle([], rt.sign_delegations(payload, root, [OPERATOR_ROOT_SEED]))
    return bundle, canonical_json(root)


def verified_release_trust(**kwargs):
    import release_trust as rt  # noqa: PLC0415
    bundle, pin = release_trust_for(**kwargs)
    return rt.verify_bundle(bundle, pin)


def sign_with_seed(catalog_bytes: bytes, seed: bytes) -> bytes:
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    public = ac.ed25519_public_key(seed)
    return canonical_json({
        'schema': 'open-shorebird.artifact-catalog-signature',
        'contract_version': 1,
        'algorithm': 'ed25519',
        'key_id': ac.key_id(public),
        'catalog_sha256': digest,
        'signature': base64.b64encode(ac.ed25519_sign(seed, ac.signed_message(digest))).decode(),
    })


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for secret, public, message, signature in RFC8032:
            seed, msg = bytes.fromhex(secret), bytes.fromhex(message)
            self.assertEqual(ac.ed25519_public_key(seed).hex(), public)
            self.assertEqual(ac.ed25519_sign(seed, msg).hex(), signature)
            self.assertTrue(ac.ed25519_verify(bytes.fromhex(public), msg, bytes.fromhex(signature)))

    def test_rejects_modified_message_signature_and_key(self):
        _, public, message, signature = RFC8032[2]
        pub, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
        self.assertFalse(ac.ed25519_verify(pub, msg + b'\0', sig))
        self.assertFalse(ac.ed25519_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1])))
        self.assertFalse(ac.ed25519_verify(bytes.fromhex(RFC8032[0][1]), msg, sig))

    def test_rejects_non_canonical_s(self):
        _, public, message, signature = RFC8032[0]
        sig = bytes.fromhex(signature)
        s = int.from_bytes(sig[32:], 'little') + ac._L
        self.assertFalse(ac.ed25519_verify(bytes.fromhex(public), bytes.fromhex(message),
                                           sig[:32] + s.to_bytes(32, 'little')))

    def test_rejects_non_canonical_point(self):
        _, _, message, signature = RFC8032[0]
        not_a_point = (2**255 - 1).to_bytes(32, 'little')  # y >= p
        self.assertFalse(ac.ed25519_verify(not_a_point, bytes.fromhex(message), bytes.fromhex(signature)))

    @unittest.skipUnless(openssl_ed25519(), 'OpenSSL without Ed25519')
    def test_openssl_and_python_signers_agree(self):
        catalog = ac.DEVELOPMENT_KEY.read_bytes()  # any bytes work as a catalog payload
        python = ac.sign_catalog(catalog, ac.DEVELOPMENT_KEY, backend='python')
        openssl = ac.sign_catalog(catalog, ac.DEVELOPMENT_KEY, backend='openssl')
        self.assertEqual(python, openssl)


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='catalog-test-'))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.mirror = self.tmp / 'mirror'
        engine_dir = self.mirror / 'shorebird' / ENGINE
        engine_dir.mkdir(parents=True)
        self.overrides = [
            'flutter_infra_release/flutter/$engine/dart-sdk-linux-x64.zip',
            'flutter_infra_release/flutter/$engine/linux-x64-release/artifacts.zip',
            'flutter_infra_release/flutter/$engine/linux-x64/font-subset.zip',
            'download.flutter.io/io/flutter/arm64_v8a_release/1.0.0-$engine/arm64_v8a_release-1.0.0-$engine.jar',
        ]
        (engine_dir / 'artifacts_manifest.yaml').write_text(
            f'flutter_engine_revision: {UPSTREAM}\nstorage_bucket: shorebird\nartifact_overrides:\n'
            + ''.join(f'  - {route}\n' for route in self.overrides))
        for index, route in enumerate(self.overrides):
            path = self.mirror / 'shorebird' / route.replace('$engine', ENGINE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'artifact %d' % index)

    def generate(self, **kwargs) -> dict:
        return ac.generate_catalog(mirror_root=self.mirror, engine_revision=ENGINE, toolchain_id=TOOLCHAIN,
                                   custom_base_url='https://artifacts.example.invalid/shorebird', **kwargs)


class GenerateTest(CatalogFixture):
    def test_generated_catalog_is_complete_and_valid(self):
        catalog = self.generate()
        custom = {a['path']: a for a in catalog['artifacts']}
        self.assertEqual(sorted(custom), sorted(self.overrides))
        self.assertEqual(catalog['upstream_engine_revision'], UPSTREAM)
        jar = custom[self.overrides[3]]
        self.assertEqual(jar['platform'], 'android-arm64')
        self.assertEqual(jar['sha256'], hashlib.sha256(b'artifact 3').hexdigest())
        self.assertEqual(custom[self.overrides[1]]['platform'], 'linux-x64')
        self.assertEqual(custom[self.overrides[0]]['platform'], 'linux-x64')

    def test_missing_custom_artifact_is_a_precise_error(self):
        (self.mirror / 'shorebird' / self.overrides[1].replace('$engine', ENGINE)).unlink()
        with self.assertRaisesRegex(ac.CatalogError, r'missing custom artifacts for engine .*\n  .*linux-x64-release/artifacts.zip'):
            self.generate()

    def test_official_entry_never_satisfies_a_custom_route(self):
        entry = {'path': 'flutter_infra_release/flutter/$engine/linux-x64-release/linux-x64-flutter-gtk.zip',
                 'platform': 'linux-x64', 'sha256': '0' * 64, 'size': 1, 'required': True}
        with self.assertRaisesRegex(ac.CatalogError, 'official artifact must never satisfy'):
            self.generate(official_entries=[entry])

    def test_completeness_rejects_dropped_routes(self):
        catalog = self.generate()
        catalog['artifacts'] = catalog['artifacts'][1:]
        with self.assertRaisesRegex(ac.CatalogError, 'differ from artifact_overrides'):
            ac.check_completeness(catalog, self.overrides, UPSTREAM)


class VerifyTest(CatalogFixture):
    def setUp(self):
        super().setUp()
        self.catalog = self.generate()
        self.bytes = canonical_json(self.catalog)
        self.trust = production_trust()
        self.dev_signature = canonical_json(ac.sign_catalog(self.bytes, ac.DEVELOPMENT_KEY))
        self.prod_signature = sign_with_seed(self.bytes, PRODUCTION_SEED)

    def verify(self, catalog_bytes=None, signature=b'', **kwargs):
        kwargs.setdefault('profile', 'legacy')
        if kwargs['profile'] == 'hardened':
            kwargs.setdefault('release_trust', verified_release_trust())
        return ac.verify_catalog(self.bytes if catalog_bytes is None else catalog_bytes,
                                 self.prod_signature if signature == b'' else signature, self.trust, **kwargs)

    def test_production_signature_is_accepted(self):
        catalog, usage = self.verify(engine_revision=ENGINE)
        self.assertEqual((catalog['engine_revision'], usage), (ENGINE, 'production'))

    def test_development_signature_legacy_only(self):
        self.assertEqual(self.verify(signature=self.dev_signature)[1], 'development')
        hardened = copy.deepcopy(self.catalog)
        hardened['security_profile'] = 'hardened'
        data = canonical_json(hardened)
        with self.assertRaisesRegex(ac.CatalogError, 'rejects a catalog signed by a development key'):
            self.verify(data, canonical_json(ac.sign_catalog(data, ac.DEVELOPMENT_KEY)), profile='hardened')
        self.assertEqual(self.verify(data, sign_with_seed(data, PRODUCTION_SEED), profile='hardened')[1], 'production')

    def test_hardened_signer_must_be_the_release_root_catalog_delegate(self):
        import release_trust as rt  # noqa: PLC0415
        hardened = copy.deepcopy(self.catalog)
        hardened['security_profile'] = 'hardened'
        data = canonical_json(hardened)
        signature = sign_with_seed(data, PRODUCTION_SEED)
        with self.assertRaisesRegex(ac.CatalogError, 'under the release root'):
            self.verify(data, signature, profile='hardened', release_trust=None)
        production_id = ac.key_id(ac.ed25519_public_key(PRODUCTION_SEED))
        for trust, code in (
            # Not delegated; delegated as a build-manifest key (role
            # confusion); revoked.
            (verified_release_trust(catalog_seeds=()), 'signer_untrusted'),
            (verified_release_trust(catalog_seeds=(), manifest_seeds=(PRODUCTION_SEED,)), 'signer_role'),
            (rt.verify_bundle(*release_trust_for(catalog_seeds=(), revoked=(production_id,))), 'signer_revoked'),
        ):
            with self.assertRaisesRegex(ac.CatalogError, code):
                self.verify(data, signature, profile='hardened', release_trust=trust)
        # Legacy never consults the release root.
        self.assertEqual(self.verify(release_trust=None)[1], 'production')

    def test_unsigned_catalog(self):
        with self.assertRaisesRegex(ac.CatalogError, 'unsigned catalog: set the explicit development override'):
            self.verify(signature=None)
        self.assertEqual(self.verify(signature=None, allow_unsigned=True)[1], 'unsigned')
        with self.assertRaisesRegex(ac.CatalogError, 'hardened toolchain requires a hardened catalog'):
            self.verify(signature=None, profile='hardened', allow_unsigned=True)
        hardened = copy.deepcopy(self.catalog)
        hardened['security_profile'] = 'hardened'
        with self.assertRaisesRegex(ac.CatalogError, 'unsigned catalog: a hardened toolchain requires'):
            self.verify(canonical_json(hardened), None, profile='hardened', allow_unsigned=True)

    def test_digest_mismatch_and_tampering(self):
        tampered = copy.deepcopy(self.catalog)
        tampered['artifacts'][0]['sha256'] = 'f' * 64
        with self.assertRaisesRegex(ac.CatalogError, 'signature covers a different catalog digest'):
            self.verify(canonical_json(tampered))
        envelope = json.loads(self.prod_signature)
        envelope['catalog_sha256'] = hashlib.sha256(canonical_json(tampered)).hexdigest()
        with self.assertRaisesRegex(ac.CatalogError, 'does not verify'):
            self.verify(canonical_json(tampered), canonical_json(envelope))

    def test_non_canonical_and_duplicate_keys(self):
        with self.assertRaisesRegex(ac.CatalogError, 'not in canonical JSON form'):
            self.verify(json.dumps(self.catalog, indent=1).encode())
        duplicate = self.bytes.replace(b'{"artifacts":', b'{"schema":"x","artifacts":', 1)
        with self.assertRaisesRegex(ac.CatalogError, 'duplicate JSON key schema'):
            self.verify(duplicate)

    def test_untrusted_key(self):
        other = hashlib.sha256(b'someone else').digest()
        with self.assertRaisesRegex(ac.CatalogError, 'untrusted key'):
            self.verify(signature=sign_with_seed(self.bytes, other))

    def test_wrong_engine(self):
        with self.assertRaisesRegex(ac.CatalogError, 'catalog is for engine'):
            self.verify(engine_revision='0' * 40)

    def test_custom_route_classified_official_is_rejected(self):
        bad = copy.deepcopy(self.catalog)
        bad['artifacts'][1].update(origin='flutter-official', **{'class': 'official-unchanged'}, revision=UPSTREAM)
        data = canonical_json(bad)
        with self.assertRaisesRegex(ac.CatalogError, 'must never satisfy it'):
            self.verify(data, sign_with_seed(data, PRODUCTION_SEED))

    def test_trust_document_rules(self):
        trust = copy.deepcopy(self.trust)
        trust['keys'][0]['key_id'] = 'sha256:' + '0' * 64
        self.assertTrue(any('key_id' in e for e in ac.validate_trust(trust)))
        trust = copy.deepcopy(self.trust)
        trust['keys'][0]['usage'] = 'root'
        self.assertTrue(ac.validate_trust(trust))
        trust = copy.deepcopy(self.trust)
        trust['keys'][0]['extra'] = 1
        self.assertTrue(ac.validate_trust(trust))

    def test_shipped_trust_has_only_the_development_key(self):
        shipped = ac.strict_loads(ac.TRUST_FILE.read_bytes())
        self.assertEqual(ac.validate_trust(shipped), [])
        dev = ac.key_id(ac.load_public_key(ac.DEVELOPMENT_KEY))
        self.assertEqual([(k['key_id'], k['usage']) for k in shipped['keys']], [(dev, 'development')])
        self.assertEqual(ac.check_trust_store(shipped), [])
        self.assertEqual(ac.main(['check-trust']), 0)

    def test_check_trust_refuses_production_trust_for_the_development_key(self):
        promoted = copy.deepcopy(ac.strict_loads(ac.TRUST_FILE.read_bytes()))
        promoted['keys'][0]['usage'] = 'production'
        self.assertTrue(any('development' in e for e in ac.check_trust_store(promoted)))
        path = self.tmp / 'promoted-trust.json'
        path.write_bytes(canonical_json(promoted))
        self.assertEqual(ac.main(['check-trust', str(path)]), 1)

    def test_cli_round_trip(self):
        output = self.tmp / 'cat.json'
        self.assertEqual(ac.main(['generate', '--mirror-root', str(self.mirror), '--engine-revision', ENGINE,
                                  '--custom-base-url', 'https://artifacts.example.invalid/shorebird',
                                  '--toolchain-id', TOOLCHAIN, '--output', str(output)]), 0)
        self.assertEqual(ac.main(['sign', '--catalog', str(output), '--key', str(ac.DEVELOPMENT_KEY)]), 0)
        self.assertEqual(ac.main(['verify', '--catalog', str(output), '--signature', str(output) + '.sig',
                                  '--engine-revision', ENGINE]), 0)
        self.assertEqual(ac.main(['verify', '--catalog', str(output), '--signature', str(output) + '.sig',
                                  '--profile', 'hardened']), 1)
        self.assertEqual(ac.main(['verify', '--catalog', str(output)]), 1)


class PublishScriptTest(CatalogFixture):
    """scripts/publish_artifact_catalog.sh, as the artifact-mirror CI job runs it."""

    SCRIPT = ROOT / 'scripts' / 'publish_artifact_catalog.sh'

    def run_script(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        environment = {k: v for k, v in os.environ.items()
                       if k not in ('OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY',
                                    'OPEN_SHOREBIRD_ARTIFACT_CATALOG_TRUST', 'GITHUB_ACTIONS',
                                    'OPEN_SHOREBIRD_RELEASE_TRUST', 'OPEN_SHOREBIRD_RELEASE_ROOT')}
        environment.update(env)
        return subprocess.run([str(self.SCRIPT), str(self.mirror), ENGINE, 'https://artifacts.example.invalid/shorebird'],
                              capture_output=True, text=True, env=environment)

    def catalog_files(self) -> tuple[Path, Path]:
        catalog = self.mirror / 'shorebird' / ENGINE / ac.CATALOG_FILE_NAME
        return catalog, catalog.with_name(catalog.name + '.sig')

    def production_pem(self) -> Path:
        der = ac._PKCS8_ED25519_PREFIX + PRODUCTION_SEED
        pem = self.tmp / 'production.pem'
        pem.write_text('-----BEGIN PRIVATE KEY-----\n' + base64.b64encode(der).decode() + '\n-----END PRIVATE KEY-----\n')
        return pem

    def test_without_the_secret_signs_with_the_development_key_loudly(self):
        result = self.run_script({'GITHUB_ACTIONS': 'true'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning title=Development-signed artifact catalog::', result.stdout)
        catalog, signature = self.catalog_files()
        trust = ac.strict_loads(ac.TRUST_FILE.read_bytes())
        _, usage = ac.verify_catalog(catalog.read_bytes(), signature.read_bytes(), trust,
                                     profile='legacy', engine_revision=ENGINE)
        self.assertEqual(usage, 'development')
        with self.assertRaises(ac.CatalogError):
            ac.verify_catalog(catalog.read_bytes(), signature.read_bytes(), trust, profile='hardened',
                              release_trust=verified_release_trust())
        for path in (catalog, signature):
            digest = Path(str(path) + '.sha256').read_text().split()[0]
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    @unittest.skipUnless(openssl_ed25519(), 'openssl without Ed25519 support')
    def test_secret_key_signs_a_production_catalog_when_pinned(self):
        trust = self.tmp / 'trust.json'
        trust.write_bytes(canonical_json(production_trust()))
        bundle, pin = release_trust_for()
        environment = {'OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY': self.production_pem().read_text(),
                       'OPEN_SHOREBIRD_ARTIFACT_CATALOG_TRUST': str(trust)}
        # Without the release root, a hardened catalog is refused here.
        refused = self.run_script(environment)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('OPEN_SHOREBIRD_RELEASE_TRUST', refused.stderr)
        result = self.run_script({**environment,
                                  'OPEN_SHOREBIRD_RELEASE_TRUST': json.dumps(bundle),
                                  'OPEN_SHOREBIRD_RELEASE_ROOT': base64.b64encode(pin).decode()})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verified (production)', result.stdout)
        self.assertNotIn('DEVELOPMENT-ONLY', result.stdout + result.stderr)
        catalog, signature = self.catalog_files()
        parsed, usage = ac.verify_catalog(catalog.read_bytes(), signature.read_bytes(), production_trust(),
                                          profile='hardened', engine_revision=ENGINE,
                                          release_trust=verified_release_trust())
        self.assertEqual((parsed['security_profile'], usage), ('hardened', 'production'))

    def test_hardened_catalog_without_the_secret_is_refused(self):
        result = self.run_script({'OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE': 'hardened'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs the production key', result.stderr)
        self.assertFalse(self.catalog_files()[0].exists())

    @unittest.skipUnless(openssl_ed25519(), 'openssl without Ed25519 support')
    def test_secret_key_not_pinned_in_the_trust_store_fails(self):
        bundle, pin = release_trust_for()
        result = self.run_script({'OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY': self.production_pem().read_text(),
                                  'OPEN_SHOREBIRD_RELEASE_TRUST': json.dumps(bundle),
                                  'OPEN_SHOREBIRD_RELEASE_ROOT': base64.b64encode(pin).decode()})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not verify', result.stderr)

    def test_missing_custom_artifact_fails(self):
        (self.mirror / 'shorebird' / self.overrides[1].replace('$engine', ENGINE)).unlink()
        result = self.run_script({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing custom artifacts for engine', result.stderr)
        self.assertFalse(self.catalog_files()[0].exists())


class DartSdkLockTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='lock-test-'))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / 'bin' / 'internal').mkdir(parents=True)
        (self.root / 'bin' / 'internal' / 'engine.version').write_text(ENGINE + '\n')
        (self.root / 'DEPS').write_text('vars = {\n  "dart_sdk_revision": "%s",\n}\n' % ('d' * 40))

    def test_unpublished_round_trip_and_check(self):
        self.assertEqual(lock.main(['write-unpublished', str(self.root)]), 0)
        parsed = lock.check(self.root)
        self.assertEqual((parsed['engine_revision'], parsed['assets']), (ENGINE, {}))

    def test_published_round_trip(self):
        assets = self.root / 'assets'
        assets.mkdir()
        (assets / 'dart-sdk-linux-x64.zip').write_bytes(b'zip')
        base = 'https://github.com/tony-cloud/shorebird-workspace/releases/download/dart-sdk-x-r1'
        self.assertEqual(lock.main(['write-published', str(self.root), '--base-url', base,
                                    '--assets-dir', str(assets)]), 0)
        parsed = lock.check(self.root)
        self.assertEqual(parsed['assets']['dart-sdk-linux-x64.zip'],
                         {'sha256': hashlib.sha256(b'zip').hexdigest(), 'size': 3})

    def test_identity_drift_is_detected(self):
        lock.main(['write-unpublished', str(self.root)])
        (self.root / 'DEPS').write_text('vars = {\n  "dart_sdk_revision": "%s",\n}\n' % ('e' * 40))
        with self.assertRaisesRegex(lock.LockError, 'dart_sdk_revision'):
            lock.check(self.root)

    def test_rejections(self):
        head = f'schema {lock.SCHEMA}\ncontract_version 1\nengine_revision {ENGINE}\ndart_sdk_revision {"d" * 40}\n'
        asset = f'asset dart-sdk-linux-x64.zip {"a" * 64} 5\n'
        cases = {
            'mutable': head + 'base_url https://github.com/o/r/releases/latest/download\n' + asset,
            'http': head + 'base_url http://example.com/r\n' + asset,
            'assets without base': head + asset,
            'base without assets': head + 'base_url https://example.com/r\n',
            'duplicate asset': head + 'base_url https://example.com/r\n' + asset + asset,
            'unknown asset': head + 'base_url https://example.com/r\nasset other.zip ' + 'a' * 64 + ' 5\n',
            'unknown field': head + 'mirror https://example.com\n',
            'short revision': head.replace(ENGINE, 'abc'),
            'wrong schema': head.replace(lock.SCHEMA, 'other'),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(lock.LockError):
                lock.parse(text)

    def test_shipped_lock_matches_the_checkout(self):
        flutter = ROOT / 'flutter'
        if not (flutter / lock.LOCK_PATH).exists():
            self.skipTest('flutter checkout not present')
        lock.check(flutter)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Tests for scripts/release_trust.py (hardening contract v2, P4)."""

from __future__ import annotations

import base64
import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_catalog as ac  # noqa: E402
import release_trust as rt  # noqa: E402
from validate_hardening_contracts import canonical_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TESTDATA = ROOT / 'scripts' / 'testdata' / 'release-trust'
SEED = {name: bytes([value]) * 32 for name, value in
        (('root', 1), ('next_root', 2), ('catalog', 3), ('manifest', 7), ('next_manifest', 8), ('stranger', 9))}
WINDOW = ('2026-11-01T00:00:00Z', '2036-11-01T00:00:00Z')


def public(name: str) -> bytes:
    return ac.ed25519_public_key(SEED[name])


def kid(name: str) -> str:
    return ac.key_id(public(name))


def root(version=1, names=('root',), threshold=1) -> dict:
    return rt.root_payload([public(n) for n in names], version=version, threshold=threshold,
                           expires_at='2036-11-09T00:00:00Z')


def delegations(version=1, root_version=1, manifest=('manifest',), catalog=('catalog',), revoked=(),
                window=WINDOW) -> dict:
    return rt.delegations_payload(
        version=version, root_version=root_version, issued_at=WINDOW[0], expires_at=WINDOW[1],
        roles={'build-manifest': [rt.delegated_key(public(n), *window) for n in manifest],
               'artifact-catalog': [rt.delegated_key(public(n), *window) for n in catalog]},
        revoked=list(revoked))


def signed(payload: dict, domain: bytes, signers: tuple[str, ...]) -> dict:
    return rt.envelope(payload, domain, [SEED[n] for n in signers])


def trust(chain=(), delegations_payload=None, signers=('root',)) -> dict:
    return rt.bundle(list(chain), signed(delegations_payload or delegations(), rt.DELEGATIONS_DOMAIN, signers))


PIN = canonical_json(root())


def verify(bundle, pin=PIN, minimum=1, **kwargs):
    return rt.verify_bundle(bundle, pin, minimum, **kwargs)


class VerifyTest(unittest.TestCase):
    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(rt.TrustError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_accepts_a_bundle_and_names_role_keys(self):
        verified = verify(trust())
        self.assertEqual(verified.root['version'], 1)
        self.assertEqual(verified.role_key('build-manifest', kid('manifest')), public('manifest'))
        self.assertEqual(verified.role_key('artifact-catalog', kid('catalog')), public('catalog'))

    def test_role_confusion_and_unknown_keys(self):
        verified = verify(trust())
        for confused in ('catalog', 'root'):
            self.assertCode('signer_role', verified.role_key, 'build-manifest', kid(confused))
        self.assertCode('signer_role', verified.role_key, 'artifact-catalog', kid('manifest'))
        self.assertCode('signer_untrusted', verified.role_key, 'build-manifest', kid('stranger'))

    def test_revoked_rotated_out_and_rollback(self):
        rotated = trust(delegations_payload=delegations(2, manifest=('next_manifest',),
                                                        revoked=(kid('manifest'),)))
        verified = verify(rotated, minimum=2)
        self.assertCode('signer_revoked', verified.role_key, 'build-manifest', kid('manifest'))
        verified.role_key('build-manifest', kid('next_manifest'))
        dropped = verify(trust(delegations_payload=delegations(2, manifest=('next_manifest',))), minimum=2)
        self.assertCode('signer_untrusted', dropped.role_key, 'build-manifest', kid('manifest'))
        self.assertCode('trust_rollback', verify, trust(), minimum=2)

    def test_delegations_need_the_root_and_its_domain(self):
        for signers in (('manifest',), ('stranger',)):
            self.assertCode('trust_unverified', verify, trust(signers=signers))
        cross_domain = rt.bundle([], signed(delegations(), rt.ROOT_DOMAIN, ('root',)))
        self.assertCode('trust_unverified', verify, cross_domain)
        two = canonical_json(root(names=('root', 'next_root'), threshold=2))
        self.assertCode('trust_unverified', verify, trust(), pin=two)
        verify(trust(signers=('root', 'next_root')), pin=two)

    def test_root_rotation(self):
        next_root = root(2, ('next_root',))
        moved = delegations(root_version=2, catalog=())
        chain = [signed(root(), rt.ROOT_DOMAIN, ('root',)),
                 signed(next_root, rt.ROOT_DOMAIN, ('root', 'next_root'))]
        verified = verify(trust(chain, moved, ('next_root',)))
        self.assertEqual(verified.root['version'], 2)
        self.assertCode('signer_role', verified.role_key, 'build-manifest', kid('next_root'))
        for signers in (('root',), ('next_root',)):
            self.assertCode('trust_unverified', verify,
                            trust([signed(next_root, rt.ROOT_DOMAIN, signers)], moved, ('next_root',)))
        gap = signed(root(3, ('next_root',)), rt.ROOT_DOMAIN, ('root', 'next_root'))
        self.assertCode('trust_unverified', verify, trust([gap], moved, ('next_root',)))
        # Delegations of the old root after the chain moved on.
        self.assertCode('trust_unverified', verify, trust(chain[1:], delegations(), ('root',)))

    def test_role_separation_and_malformed_documents(self):
        bad_payloads = [
            delegations(manifest=('manifest',), catalog=('manifest',)),
            delegations(manifest=('manifest',), revoked=(kid('manifest'),)),
            delegations(manifest=()),
        ]
        for payload in bad_payloads:
            self.assertCode('trust_malformed', verify, rt.bundle([], rt.envelope(
                payload, rt.DELEGATIONS_DOMAIN, [SEED['root']])))
        self.assertCode('trust_malformed', verify, trust(delegations_payload=delegations(manifest=('root',))))
        good = trust()
        for mutate in (lambda b: {**b, 'contract_version': 1}, lambda b: {**b, 'x': 1},
                       lambda b: {**b, 'root_chain': [{'payload': 'AA==', 'signatures': []}]}):
            self.assertCode('trust_malformed', verify, mutate(good))
        noncanonical = dict(good['delegations'])
        payload = base64.b64decode(noncanonical['payload']).replace(b',', b', ', 1)
        noncanonical['payload'] = base64.b64encode(payload).decode()
        self.assertCode('trust_malformed', verify, {**good, 'delegations': noncanonical})
        self.assertCode('trust_malformed', verify, b'{"schema":1,"schema":2}')
        self.assertCode('trust_malformed', verify, good, pin=b'{}')

    def test_tooling_only_time_checks(self):
        narrow = verify(trust(delegations_payload=delegations(window=('2027-01-01T00:00:00Z',
                                                                      '2028-01-01T00:00:00Z'))))
        narrow.role_key('build-manifest', kid('manifest'), at='2027-06-01T00:00:00Z')
        for outside in ('2026-12-01T00:00:00Z', '2028-01-01T00:00:00Z'):
            self.assertCode('signer_outside_window', narrow.role_key, 'build-manifest', kid('manifest'), at=outside)
        self.assertCode('trust_expired', verify, trust(), at='2036-11-02T00:00:00Z')
        # The device never checks time: without `at` an old bundle verifies.
        verify(trust())

    def test_production_refuses_the_development_root(self):
        development = rt._load(TESTDATA / 'release-trust.DEVELOPMENT-ONLY.json')
        pin, _ = rt.open_envelope(rt._load(TESTDATA / 'root.v1.DEVELOPMENT-ONLY.json'), 'root')
        verify(development, pin)
        self.assertCode('trust_development', verify, development, pin, production=True)
        verify(trust(signers=('next_root',)), canonical_json(root(names=('next_root',))), production=True)


class AuthoringTest(unittest.TestCase):
    def test_signing_rules(self):
        root_document = root()
        with self.assertRaises(rt.TrustError):
            rt.sign_delegations(delegations(manifest=('root',)), root_document, [SEED['root']])
        with self.assertRaises(rt.TrustError):
            rt.sign_delegations(delegations(), root_document, [SEED['stranger']])
        previous = delegations(revoked=(kid('stranger'),))
        with self.assertRaisesRegex(rt.TrustError, 'cannot be withdrawn'):
            rt.sign_delegations(delegations(2), root_document, [SEED['root']], previous)
        with self.assertRaisesRegex(rt.TrustError, 'must exceed'):
            rt.sign_delegations(delegations(1, revoked=(kid('stranger'),)), root_document, [SEED['root']], previous)
        with self.assertRaisesRegex(rt.TrustError, 'not enough previous'):
            rt.sign_root(root(2, ('next_root',)), [SEED['next_root']], root_document, [SEED['stranger']])

    def test_cli_round_trip_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            pems = {}
            for name, seed in SEED.items():
                pems[name] = out / f'{name}.pem'
                pems[name].write_text('-----BEGIN PRIVATE KEY-----\n'
                                      + base64.b64encode(ac._PKCS8_ED25519_PREFIX + seed).decode()
                                      + '\n-----END PRIVATE KEY-----\n')
            quiet = contextlib.redirect_stdout(io.StringIO())
            with quiet, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rt.main(['root', '--key', str(pems['next_root']), '--threshold', '1',
                                          '--expires', '2036-11-09T00:00:00Z', '--output', str(out / 'r1.json')]), 0)
                self.assertEqual(rt.main(['root', '--key', str(pems['stranger']), '--threshold', '1', '--version',
                                          '2', '--expires', '2036-11-09T00:00:00Z', '--previous',
                                          str(out / 'r1.json'), '--previous-key', str(pems['next_root']),
                                          '--output', str(out / 'r2.json')]), 0)
                self.assertEqual(rt.main(['delegate', '--root', str(out / 'r2.json'), '--root-key',
                                          str(pems['stranger']), '--version', '1', '--issued', WINDOW[0],
                                          '--expires', WINDOW[1], '--role', f'build-manifest={pems["manifest"]}',
                                          '--role', f'artifact-catalog={pems["catalog"]}',
                                          '--output', str(out / 'd1.json')]), 0)
                self.assertEqual(rt.main(['delegate', '--root', str(out / 'r2.json'), '--root-key',
                                          str(pems['stranger']), '--version', '2', '--issued', WINDOW[0],
                                          '--expires', WINDOW[1], '--role',
                                          f'build-manifest={pems["next_manifest"]}', '--revoke', kid('manifest'),
                                          '--previous', str(out / 'd1.json'), '--output', str(out / 'd2.json')]), 0)
                self.assertEqual(rt.main(['bundle', '--root', str(out / 'r1.json'), '--root', str(out / 'r2.json'),
                                          '--delegations', str(out / 'd2.json'), '--output', str(out / 't.json')]), 0)
            # The repository root key is DEVELOPMENT-ONLY; these roots are not.
            development = rt.pin_fields(rt._load(TESTDATA / 'root.v1.DEVELOPMENT-ONLY.json'))
            self.assertTrue(development['development'])
            fields = rt.pin_fields(rt._load(out / 'r1.json'))
            self.assertFalse(fields['development'])
            with quiet, contextlib.redirect_stderr(io.StringIO()):
                ok = rt.main(['verify', str(out / 't.json'), '--root-pin', fields['release_root'],
                              '--min-delegations-version', '2', '--role', 'build-manifest',
                              '--key-id', kid('next_manifest'), '--at', '2027-01-01T00:00:00Z', '--production'])
                revoked = rt.main(['verify', str(out / 't.json'), '--root-pin', fields['release_root'],
                                   '--role', 'build-manifest', '--key-id', kid('manifest')])
                rollback = rt.main(['verify', str(out / 't.json'), '--root-pin', fields['release_root'],
                                    '--min-delegations-version', '3'])
            self.assertEqual((ok, revoked, rollback), (0, 1, 1))

    def test_reference_signer_signs_only_its_role_domain(self):
        script = Path(rt.__file__)
        message = rt.MANIFEST_DOMAIN + b'a' * 64
        argv = [sys.executable, str(script), 'sign', '--key',
                str(TESTDATA / 'build-manifest-1.DEVELOPMENT-ONLY.pem'), '--key-id', kid('manifest')]
        result = subprocess.run([*argv, '--message', message.decode()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ac.ed25519_verify(public('manifest'), message, base64.b64decode(result.stdout.strip())))
        for bad in (ac.SIGNATURE_DOMAIN + b'a' * 64, rt.DELEGATIONS_DOMAIN + b'a' * 64, b'anything'):
            result = subprocess.run([*argv, '--message', bad.decode()], capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, bad)
        wrong = subprocess.run([*argv[:-1], kid('stranger'), '--message', message.decode()],
                               capture_output=True, text=True)
        self.assertEqual(wrong.returncode, 2)


class FixtureConsistencyTest(unittest.TestCase):
    """The DEVELOPMENT-ONLY fixtures the updater and flutter_tools pin agree."""

    def test_committed_development_material(self):
        bundle = rt._load(TESTDATA / 'release-trust.DEVELOPMENT-ONLY.json')
        root_envelope = rt._load(TESTDATA / 'root.v1.DEVELOPMENT-ONLY.json')
        self.assertEqual(bundle['root_chain'], [root_envelope])
        self.assertEqual(bundle['delegations'], rt._load(TESTDATA / 'delegations.v1.DEVELOPMENT-ONLY.json'))
        pin = rt.pin_fields(root_envelope)
        self.assertTrue(pin['development'])
        verified = verify(bundle, base64.b64decode(pin['release_root']))
        self.assertEqual(verified.role_key('build-manifest', kid('manifest')), public('manifest'))
        self.assertEqual(rt.development_root_public_keys(), {base64.b64encode(public('root')).decode()})
        compact = canonical_json(bundle).decode()
        updater = ROOT / 'updater/library/testdata/release-trust.DEVELOPMENT-ONLY.json'
        self.assertEqual(updater.read_text(), compact)
        rust = (ROOT / 'updater/library/src/release_trust.rs').read_text()
        self.assertIn(pin['release_root'], rust)
        self.assertIn(verified.delegations_sha256, rust)
        dart = (ROOT / 'flutter/packages/flutter_tools/test/general.shard/shorebird/'
                'shorebird_release_trust_fixtures.dart').read_text()
        self.assertIn(compact, dart)
        self.assertIn(pin['release_root'], dart)
        # Every place that refuses the development root names the same key.
        development = base64.b64encode(public('root')).decode()
        for path in ('updater/library/src/security_inputs.rs',
                     'flutter/engine/src/flutter/shell/common/shorebird/build_rust_updater.py',
                     'flutter/packages/flutter_tools/lib/src/shorebird/shorebird_release_trust.dart'):
            self.assertIn(development, (ROOT / path).read_text(), path)

    def test_development_keys_are_named_development_only(self):
        for path in TESTDATA.iterdir():
            self.assertIn('.DEVELOPMENT-ONLY.', path.name)
        pems = sorted(p.name for p in TESTDATA.glob('*.pem'))
        self.assertEqual(pems, ['build-manifest-1.DEVELOPMENT-ONLY.pem', 'root-1.DEVELOPMENT-ONLY.pem'])
        for name in pems:
            self.assertTrue(re.search(r'BEGIN PRIVATE KEY', (TESTDATA / name).read_text()))


if __name__ == '__main__':
    unittest.main()

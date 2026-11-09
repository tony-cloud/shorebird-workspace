#!/usr/bin/env python3
"""Tests for scripts/hardened_toolchain.py (HARDENING_CONTRACTS.md section 12)."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diversification_profile as dp  # noqa: E402
import hardened_toolchain as ht  # noqa: E402
import release_trust as rt  # noqa: E402
from artifact_catalog import ed25519_public_key, toolchain_id_from_checkout  # noqa: E402

TOOLCHAIN = 'sha256:' + '7' * 64
HAS_OPCODES = dp.CONSTANTS_KBC.is_file()


def production_root_pin() -> str:
    """A non-development root (test key, never pinned anywhere)."""
    seed = bytes(range(32))
    payload = rt.root_payload([ed25519_public_key(seed)], version=1, threshold=1,
                              expires_at='2040-01-01T00:00:00Z')
    return rt.pin_fields(rt.sign_root(payload, [seed]))['release_root']


def run(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    clean = {k: v for k, v in os.environ.items()
             if k not in ('OPEN_SHOREBIRD_RELEASE_ROOT', 'OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE')}
    with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ht.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def describe(*argv: str, env: dict[str, str] | None = None) -> dict:
    code, out, err = run('--toolchain-id', TOOLCHAIN, *argv, 'describe', env=env)
    if code != 0:
        raise AssertionError(err)
    return json.loads(out)


class LegacyTest(unittest.TestCase):
    def test_legacy_changes_nothing(self):
        code, out, _ = run('--profile', 'legacy', 'gn-args')
        self.assertEqual((code, out), (0, '\n'))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'toolchain_security.json'
            code, out, _ = run('--profile', 'legacy', 'toolchain-security', '--output', str(target))
            self.assertEqual(code, 0)
            self.assertFalse(target.exists())
        code, out, _ = run('--profile', 'legacy', 'github-env')
        self.assertIn('SHOREBIRD_SECURITY_GN_ARGS=\n', out)


class HardenedTest(unittest.TestCase):
    def test_development_root_builds_a_visible_development_toolchain(self):
        d = describe('--profile', 'hardened', '--development')
        self.assertTrue(d['development'])
        self.assertEqual(d['release_root_source'], 'development')
        self.assertIs(d['gn']['shorebird_security_development_exceptions'], True)
        self.assertNotIn('dart_bytecode_profile', d['gn'])
        security = d['toolchain_security']
        # Exactly the CLI's contract v2 keys (security_profile.dart _keysV2).
        self.assertEqual(set(security), {'schema', 'contract_version', 'security_profile', 'toolchain_id',
                                         'development_exceptions', 'release_root', 'min_delegations_version'})
        self.assertEqual((security['contract_version'], security['development_exceptions']), (2, True))
        self.assertEqual(security['toolchain_id'], TOOLCHAIN)

    def test_development_inputs_need_an_explicit_choice_and_never_pass_production(self):
        code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', 'describe')
        self.assertEqual(code, 1)
        self.assertIn('needs a release root', err)
        dev_pin = describe('--profile', 'hardened', '--development')['gn']['shorebird_release_root']
        code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--release-root', dev_pin,
                           'describe')
        self.assertEqual(code, 1)
        self.assertIn('pass --development', err)
        code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--release-root', dev_pin,
                           '--production', 'describe')
        self.assertEqual(code, 1)
        self.assertIn('--production refuses', err)

    def test_production_root_from_the_ci_variable_or_release_pins(self):
        pin = production_root_pin()
        d = describe('--profile', 'hardened', '--production', env={'OPEN_SHOREBIRD_RELEASE_ROOT': pin})
        self.assertFalse(d['development'])
        self.assertNotIn('shorebird_security_development_exceptions', d['gn'])
        self.assertEqual(d['gn']['shorebird_release_root'], pin)
        self.assertEqual(d['toolchain_security']['development_exceptions'], False)
        with tempfile.TemporaryDirectory() as tmp:
            pins = Path(tmp) / 'release-pins.json'
            pins.write_text(json.dumps({'schema': 'open-shorebird.release-pins', 'gn_args': {
                'shorebird_release_root': pin, 'shorebird_release_min_delegations_version': 3}}))
            d = describe('--profile', 'hardened', '--release-pins', str(pins))
            self.assertEqual(d['min_delegations_version'], 3)
            self.assertEqual(d['gn']['shorebird_release_min_delegations_version'], 3)
            self.assertEqual(d['release_root_source'], 'release-pins')
            pins.write_text(json.dumps({'schema': 'other'}))
            code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--release-pins',
                               str(pins), 'describe')
            self.assertEqual(code, 1)

    def test_malformed_pins_and_floors_are_refused(self):
        pin = production_root_pin()
        for argv in (('--release-root', 'not base64!'),
                     ('--release-root', base64.b64encode(b'{"schema":"x"}').decode()),
                     ('--release-root', pin.rstrip('=') + 'A'),
                     ('--release-root', pin, '--min-delegations-version', '-1')):
            code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', *argv, 'describe')
            self.assertEqual(code, 1, argv)
        code, _, err = run('--toolchain-id', 'sha256:ABC', '--profile', 'hardened', '--release-root', pin,
                           'describe')
        self.assertEqual(code, 1)

    def test_gn_fragment_round_trips_through_a_generated_args_gn(self):
        d = describe('--profile', 'hardened', '--development')
        fragment = d['gn_args']
        # What `gn gen` writes, with the long values wrapped like the engine's tools/gn output.
        lines = ['is_debug = false']
        for key, value in d['gn'].items():
            rendered = ht._gn_value(value)
            lines.append(f'{key} =\n    {rendered}' if len(rendered) > 60 else f'{key} = {rendered}')
        args_gn = '\n'.join(lines) + '\n'
        self.assertEqual(ht.verify_args(d, args_gn, interpreter=True), [])
        self.assertEqual(ht.verify_args(d, args_gn.replace('"hardened"', '"legacy"'), interpreter=True),
                         ['shorebird_security_profile is \'"legacy"\', expected \'"hardened"\''])
        self.assertTrue(fragment.startswith('shorebird_security_profile="hardened" shorebird_toolchain_id='))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'args.gn'
            path.write_text(args_gn.replace(TOOLCHAIN, 'sha256:' + '8' * 64))
            code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--development',
                               'verify-args', str(path))
            self.assertEqual(code, 1)
            self.assertIn('shorebird_toolchain_id', err)

    def test_a_legacy_engine_must_not_carry_hardened_args(self):
        legacy = {'security_profile': 'legacy', 'gn': {}}
        self.assertEqual(ht.verify_args(legacy, 'shorebird_security_profile = "legacy"\n', True), [])
        self.assertNotEqual(ht.verify_args(legacy, 'shorebird_security_profile = "hardened"\n', True), [])

    def test_writes_the_toolchain_security_file_and_github_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'bin' / 'internal' / 'toolchain_security.json'
            code, out, _ = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--development',
                               'toolchain-security', '--output', str(target))
            self.assertEqual(code, 0)
            self.assertIn('DEVELOPMENT toolchain', out)
            self.assertEqual(json.loads(target.read_text())['security_profile'], 'hardened')
        code, out, _ = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--development', 'github-env')
        env = dict(line.split('=', 1) for line in out.splitlines())
        self.assertEqual(env['SHOREBIRD_SECURITY_PROFILE_BUILD'], 'hardened')
        self.assertEqual(env['SHOREBIRD_SECURITY_GN_ARGS'], env['SHOREBIRD_SECURITY_NATIVE_GN_ARGS'])
        self.assertEqual(env['SHOREBIRD_SECURITY_DEVELOPMENT'], 'true')

    @unittest.skipUnless((ht.ROOT / '.git').exists() and (ht.ROOT / 'flutter' / '.git').exists(),
                         'needs the workspace checkout')
    def test_toolchain_id_is_the_checkout_identity_shared_with_the_catalog(self):
        code, out, err = run('--profile', 'hardened', '--development', 'describe')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['toolchain_id'], toolchain_id_from_checkout(ht.ROOT, 'hardened'))


@unittest.skipUnless(HAS_OPCODES, 'needs the dart-sdk checkout (constants_kbc.h)')
class DiversifiedTest(unittest.TestCase):
    def test_development_profile_and_v3_file(self):
        d = describe('--profile', 'hardened-diversified', '--development')
        profile = dp.load_json(dp.DEVELOPMENT_PROFILE)
        self.assertEqual(d['diversification_profile_id'], dp.profile_id(profile))
        self.assertEqual(d['gn']['dart_bytecode_profile'], dp.encode_profile(profile))
        self.assertEqual(d['toolchain_security']['contract_version'], 3)
        self.assertEqual(d['toolchain_security']['diversification_profile_id'], dp.profile_id(profile))
        self.assertTrue(d['development'])
        # Non-interpreter engines of a diversified toolchain stay legacy.
        self.assertEqual(ht.engine_gn_args(d, interpreter=False), '')
        self.assertEqual(ht.verify_args(d, 'shorebird_security_profile = "legacy"\n', interpreter=False), [])

    def test_production_profile_with_a_production_root(self):
        profile = dp.derive_profile(bytes([5]) * 32, dp.opcode_names())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'diversification-profile.json'
            path.write_text(json.dumps(profile))
            d = describe('--profile', 'hardened-diversified', '--production', '--diversification-profile',
                         str(path), env={'OPEN_SHOREBIRD_RELEASE_ROOT': production_root_pin()})
            self.assertFalse(d['development'])
            self.assertEqual(d['diversification_profile_id'], dp.profile_id(profile))
            # Inline JSON (a CI variable) is the same profile.
            inline = describe('--profile', 'hardened-diversified', '--production',
                              env={'OPEN_SHOREBIRD_RELEASE_ROOT': production_root_pin(),
                                   'OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE': json.dumps(profile)})
            self.assertEqual(inline['gn'], d['gn'])

    def test_refusals(self):
        pin = production_root_pin()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.json'
            profile = dp.derive_profile(bytes([5]) * 32, dp.opcode_names())
            profile['bytecode']['wire_opcodes'][1], profile['bytecode']['wire_opcodes'][2] = \
                profile['bytecode']['wire_opcodes'][1], profile['bytecode']['wire_opcodes'][1]
            bad.write_text(json.dumps(profile))
            dup = Path(tmp) / 'dup.json'
            dup.write_text('{"a": 1, "a": 2}')
            for argv in ((),  # no profile at all
                         ('--diversification-profile', str(bad)),
                         ('--diversification-profile', str(dup)),
                         ('--diversification-profile', '/no/such/profile.json')):
                code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened-diversified',
                                   '--release-root', pin, *argv, 'describe')
                self.assertEqual(code, 1, argv)
        # The DEVELOPMENT-ONLY profile is refused for production.
        code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened-diversified', '--release-root',
                           pin, '--diversification-profile', str(dp.DEVELOPMENT_PROFILE), '--production',
                           'describe')
        self.assertEqual(code, 1)
        # A profile is only for hardened-diversified.
        code, _, err = run('--toolchain-id', TOOLCHAIN, '--profile', 'hardened', '--release-root', pin,
                           '--diversification-profile', str(dp.DEVELOPMENT_PROFILE), 'describe')
        self.assertEqual(code, 1)
        self.assertIn('only for hardened-diversified', err)


class CommandLineTest(unittest.TestCase):
    def test_runs_as_a_script(self):
        result = subprocess.run([sys.executable, str(Path(ht.__file__)), '--profile', 'legacy', 'describe'],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['security_profile'], 'legacy')


if __name__ == '__main__':
    unittest.main()

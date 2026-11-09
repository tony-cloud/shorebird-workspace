#!/usr/bin/env python3
"""Tests for scripts/diversification_profile.py (contract v3, P5).

They pin the reference derivation, the profile rules, and agreement with the
two other implementations that consume a profile: the Dart VM build generator
(dart-sdk/runtime/tools/bytecode_profile.py) and dart2bytecode's opcode enum
(the Dart derivation itself is pinned by
dart-sdk/pkg/dart2bytecode/test/bytecode_profile_test.dart).
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diversification_profile as dp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VM_GENERATOR = ROOT / 'dart-sdk/runtime/tools/bytecode_profile.py'
DBC_DART = ROOT / 'dart-sdk/pkg/dart2bytecode/lib/dbc.dart'
DEVELOPMENT_ID = 'sha256:832856d0bed27833434e3f26b65d03cedc313f9850c2577ca7565c4dc644f46d'


def seed(n: int) -> bytes:
    return hashlib.sha256(f'diversification test seed {n}'.encode()).digest()


def run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = dp.main(list(args))
    return status, out.getvalue(), err.getvalue()


def vm_generator(encoded: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VM_GENERATOR), 'gn', '--profile', encoded,
                           '--constants', str(dp.CONSTANTS_KBC)],
                          capture_output=True, text=True, check=False)


class DerivationTest(unittest.TestCase):
    def setUp(self):
        self.names = dp.opcode_names()

    def test_opcode_table_matches_dart2bytecode(self):
        enum = re.search(r'enum Opcode \{(.*?)\}', DBC_DART.read_text(encoding='utf-8'), re.S).group(1)
        dart_names = [name[1:] for name in re.findall(r'^\s*(k\w+),', enum, re.M)]
        self.assertEqual(dart_names, self.names)
        self.assertEqual(self.names[0], 'Trap')
        self.assertEqual(len(self.names), 176)

    def test_development_fixture_is_the_derivation_of_its_published_seed(self):
        record = dp.load_json(dp.DEVELOPMENT_SEED_RECORD)
        self.assertEqual(base64.b64decode(record['seed']),
                         hashlib.sha256(b'open-shorebird DEVELOPMENT-ONLY diversification seed v1').digest())
        profile = dp.profile_from_seed_record(record, self.names)
        self.assertEqual(profile, dp.load_json(dp.DEVELOPMENT_PROFILE))
        self.assertEqual(dp.profile_id(profile), DEVELOPMENT_ID)
        # Golden prefix: a derivation change must be deliberate.
        self.assertEqual(profile['bytecode']['wire_opcodes'][:12],
                         [0, 150, 65, 2, 44, 126, 70, 93, 149, 142, 68, 35])
        self.assertEqual(dp.development_profile_ids(), {DEVELOPMENT_ID})

    def test_sixteen_seeds(self):
        ids = set()
        for n in range(16):
            with self.subTest(seed=n):
                profile = dp.derive_profile(seed(n), self.names)
                self.assertEqual(dp.derive_profile(seed(n), self.names), profile)
                self.assertEqual(dp.validate_profile(profile, self.names), [])
                wires = profile['bytecode']['wire_opcodes']
                self.assertEqual(sorted(wires), list(range(len(self.names))))
                self.assertEqual(wires[0], 0)
                ids.add(dp.profile_id(profile))
        self.assertEqual(len(ids), 16)

    def test_unbiased_bound_helper(self):
        stream = dp._Stream(b'k' * 32)
        values = [stream.below(3) for _ in range(3000)]
        for value in range(3):
            self.assertGreater(values.count(value), 850)
        with self.assertRaises(dp.ProfileError):
            dp.derive_wire_opcodes(b'short', 10)
        with self.assertRaises(dp.ProfileError):
            dp.derive_wire_opcodes(seed(0), 257)


class ProfileRulesTest(unittest.TestCase):
    def setUp(self):
        self.names = dp.opcode_names()
        self.profile = dp.load_json(dp.DEVELOPMENT_PROFILE)

    def edited(self, edit) -> dict:
        profile = copy.deepcopy(self.profile)
        edit(profile)
        return profile

    def assertRefused(self, profile, fragment):
        errors = dp.validate_profile(profile, self.names)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_rules(self):
        wires = self.profile['bytecode']['wire_opcodes']
        trap_moved = list(wires)
        trap_moved[0], trap_moved[9] = trap_moved[9], trap_moved[0]
        weak = list(range(len(wires)))
        weak[3], weak[4] = weak[4], weak[3]
        cases = [
            (lambda p: p.update(schema='x'), 'must be'),
            (lambda p: p.update(contract_version=2), 'must be 3'),
            (lambda p: p.update(native_layout={}), 'expected null'),
            (lambda p: p.update(extra=1), 'unknown property extra'),
            (lambda p: p['bytecode'].update(scheme='other'), 'must be'),
            (lambda p: p['bytecode'].update(format_version=4), 'must be 3'),
            (lambda p: p['bytecode'].update(opcode_table_sha256='0' * 64), "opcode table"),
            (lambda p: p['bytecode'].update(wire_opcodes=wires[1:]), 'permutation'),
            (lambda p: p['bytecode'].update(wire_opcodes=[*wires[:5], wires[6], *wires[6:]]), 'permutation'),
            (lambda p: p['bytecode'].update(wire_opcodes=trap_moved), 'Trap must keep wire byte 0'),
            (lambda p: p['bytecode'].update(wire_opcodes=weak), 'fixed points'),
            (lambda p: p['bytecode'].update(wire_opcodes=list(range(257))), 'at most 256'),
            (lambda p: p['bytecode'].update(wire_opcodes=[-1, *wires[1:]]), 'below minimum'),
        ]
        for edit, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRefused(self.edited(edit), fragment)
        self.assertEqual(dp.validate_profile(self.profile, self.names), [])

    def test_check_and_production(self):
        self.assertEqual(dp.check(self.profile, names=self.names), DEVELOPMENT_ID)
        with self.assertRaisesRegex(dp.ProfileError, 'does not match the expected'):
            dp.check(self.profile, names=self.names, expect_id='sha256:' + '0' * 64)
        with self.assertRaisesRegex(dp.ProfileError, 'DEVELOPMENT-ONLY'):
            dp.check(self.profile, names=self.names, production=True)
        other = dp.derive_profile(seed(1), self.names)
        self.assertEqual(dp.check(other, names=self.names, production=True), dp.profile_id(other))

    def test_encoded_form(self):
        encoded = dp.encode_profile(self.profile)
        self.assertEqual(dp.decode_profile(encoded), self.profile)
        for bad in (encoded + '=', base64.b64encode(json.dumps(self.profile).encode()).decode(),
                    base64.b64encode(b'{"a":1,"a":2}').decode(), 'not base64!', 'A' * 9000):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(dp.ProfileError):
                    dp.decode_profile(bad)

    def test_seed_record_rules(self):
        record = dp.load_json(dp.DEVELOPMENT_SEED_RECORD)
        self.assertEqual(dp.validate_seed_record(record), [])
        for edit, fragment in ((lambda r: r.update(seed='A' * 43 + '='), None),
                               (lambda r: r.update(seed='short='), 'does not match'),
                               (lambda r: r.update(derivation='other'), 'must be'),
                               (lambda r: r.update(extra=1), 'unknown property')):
            changed = copy.deepcopy(record)
            edit(changed)
            errors = dp.validate_seed_record(changed)
            if fragment is None:
                # Well-formed but another seed: the derivation does not match the id.
                self.assertEqual(errors, [])
                with self.assertRaisesRegex(dp.ProfileError, 'does not match the derivation'):
                    dp.profile_from_seed_record(changed, dp.opcode_names())
            else:
                self.assertTrue(any(fragment in error for error in errors), errors)


class CrossRepoFixtureTest(unittest.TestCase):
    """Every consumer refuses the same published DEVELOPMENT-ONLY profile."""

    CONSUMERS = (
        'updater/library/src/security_inputs.rs',
        'flutter/engine/src/flutter/shell/common/shorebird/build_rust_updater.py',
        'flutter/packages/flutter_tools/lib/src/shorebird/shorebird_yaml.dart',
        'shorebird/packages/shorebird_cli/lib/src/security_profile.dart',
    )

    def test_development_id_is_pinned_by_every_consumer(self):
        self.assertEqual(dp.development_profile_ids(), {DEVELOPMENT_ID})
        for relative in self.CONSUMERS:
            with self.subTest(relative):
                self.assertIn(DEVELOPMENT_ID.split(':', 1)[1],
                              (ROOT / relative).read_text(encoding='utf-8'))

    def test_cli_fixture_copy_is_identical(self):
        copy_path = (ROOT / 'shorebird/packages/shorebird_cli/test/fixtures/'
                     'diversification/profile.DEVELOPMENT-ONLY.json')
        self.assertEqual(copy_path.read_bytes(), dp.DEVELOPMENT_PROFILE.read_bytes())


class VmGeneratorAgreementTest(unittest.TestCase):
    """The VM build accepts exactly what the reference accepts."""

    def test_accepts_derived_profiles_with_identical_values(self):
        names = dp.opcode_names()
        for n in range(4):
            profile = dp.derive_profile(seed(n), names)
            result = vm_generator(dp.encode_profile(profile))
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(re.findall(r'^(\w+) = (.*)$', result.stdout, re.M))
            self.assertEqual(values['count'], str(len(names)))
            self.assertEqual(values['wire'], '"' + ','.join(map(str, profile['bytecode']['wire_opcodes'])) + '"')
            self.assertEqual(values['id'], f'"{dp.profile_id(profile)}"')
            digest = bytes(int(x, 16) for x in values['digest'].strip('"').split(','))
            self.assertEqual('sha256:' + digest.hex(), dp.profile_id(profile))

    def test_refuses_what_the_reference_refuses(self):
        profile = dp.load_json(dp.DEVELOPMENT_PROFILE)
        wires = profile['bytecode']['wire_opcodes']
        trap_moved = list(wires)
        trap_moved[0], trap_moved[9] = trap_moved[9], trap_moved[0]
        bad = []
        for edit in (lambda p: p.update(native_layout={}),
                     lambda p: p.update(extra=1),
                     lambda p: p['bytecode'].update(opcode_table_sha256='0' * 64),
                     lambda p: p['bytecode'].update(wire_opcodes=trap_moved),
                     lambda p: p['bytecode'].update(wire_opcodes=list(range(len(wires))))):
            changed = copy.deepcopy(profile)
            edit(changed)
            bad.append(base64.b64encode(dp.canonical_json(changed)).decode())
        bad.append(base64.b64encode(json.dumps(profile, indent=1).encode()).decode())  # not canonical
        bad.append(base64.b64encode(dp.canonical_json(profile)).decode() + '=')
        for encoded in bad:
            result = vm_generator(encoded)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('dart_bytecode_profile:', result.stderr)


class CliTest(unittest.TestCase):
    def test_new_derive_verify_and_gn_args(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / 'lineage'
            status, out, err = run_cli('new', '--output-dir', str(private), '--development')
            self.assertEqual(status, 0, err)
            identifier = out.strip()
            seed_path = private / 'diversification-seed.json'
            profile_path = private / 'diversification-profile.json'
            if os.name == 'posix':
                self.assertEqual(seed_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            self.assertEqual(run_cli('verify', str(profile_path), '--production', '--expect-id', identifier)[0], 0)
            self.assertEqual(run_cli('verify', str(profile_path), '--expect-id', DEVELOPMENT_ID)[0], 1)
            # A lineage keeps its first seed.
            self.assertEqual(run_cli('new', '--output-dir', str(private), '--development')[0], 1)
            # Re-derivation reproduces the profile byte for byte.
            derived = Path(directory) / 'derived.json'
            self.assertEqual(run_cli('derive', str(seed_path), '--output', str(derived))[0], 0)
            self.assertEqual(derived.read_bytes(), profile_path.read_bytes())
            status, out, _ = run_cli('gn-args', str(profile_path), '--production')
            self.assertEqual(status, 0)
            self.assertIn('shorebird_security_profile = "hardened-diversified"', out)
            self.assertIn(f'dart_bytecode_profile = "{dp.encode_profile(dp.load_json(profile_path))}"', out)
            self.assertIn(identifier, out)
            self.assertEqual(run_cli('id', str(profile_path))[1].strip(), identifier)
            self.assertEqual(run_cli('encode', str(profile_path), '--production')[1].strip(),
                             dp.encode_profile(dp.load_json(profile_path)))

    def test_refusals(self):
        # Inside a git work tree without --development.
        status, _, err = run_cli('new', '--output-dir', str(ROOT / 'build' / 'p5-should-not-exist'))
        self.assertEqual(status, 1)
        self.assertIn('git work tree', err)
        self.assertFalse((ROOT / 'build' / 'p5-should-not-exist').exists())
        status, _, err = run_cli('gn-args', str(dp.DEVELOPMENT_PROFILE), '--production')
        self.assertEqual(status, 1)
        self.assertIn('DEVELOPMENT-ONLY', err)
        self.assertEqual(run_cli('verify', str(dp.DEVELOPMENT_PROFILE))[0], 0)
        self.assertEqual(run_cli('encode', str(dp.DEVELOPMENT_PROFILE), '--production')[0], 1)


if __name__ == '__main__':
    unittest.main()

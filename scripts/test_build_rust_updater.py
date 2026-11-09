#!/usr/bin/env python3
"""Check Windows updater compiler/CRT settings without building the updater."""

import base64
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
_MODULE = runpy.run_path(
    str(ROOT / 'flutter/engine/src/flutter/shell/common/shorebird/build_rust_updater.py')
)
CONFIGURE = _MODULE['_configure_msvc_env']
CONFIGURE_SECURITY = _MODULE['_configure_security_env']
ARM64 = 'aarch64_pc_windows_msvc'
X64 = 'x86_64_pc_windows_msvc'


class UpdaterMsvcEnvironmentTest(unittest.TestCase):
    def test_arm64_uses_one_gnu_driver_family_for_ring_and_other_c_dependencies(self):
        env = {'CC': 'cl.exe', 'CXX': 'cl.exe'}
        CONFIGURE(env, 'aarch64-pc-windows-msvc')
        self.assertEqual(env[f'CC_{ARM64}'], 'clang')
        self.assertEqual(env[f'CXX_{ARM64}'], 'clang++')
        self.assertEqual(env[f'CFLAGS_{ARM64}'], '-fms-runtime-lib=static')
        self.assertEqual(env[f'CXXFLAGS_{ARM64}'], '-fms-runtime-lib=static')
        self.assertEqual(env['CC'], 'cl.exe')

    def test_arm64_preserves_explicit_native_visual_studio_compilers(self):
        compiler = r'C:\Program Files\Microsoft Visual Studio\18\Enterprise\VC\Tools\Llvm\ARM64\bin\clang.exe'
        compilerxx = compiler.replace('clang.exe', 'clang++.exe')
        env = {f'CC_{ARM64}': compiler, f'CXX_{ARM64}': compilerxx}
        CONFIGURE(env, 'aarch64-pc-windows-msvc')
        self.assertEqual(env[f'CC_{ARM64}'], compiler)
        self.assertEqual(env[f'CXX_{ARM64}'], compilerxx)

    def test_x64_keeps_msvc_driver_and_static_crt(self):
        env = {f'CC_{X64}': 'cl.exe'}
        CONFIGURE(env, 'x86_64-pc-windows-msvc')
        self.assertEqual(env, {f'CC_{X64}': 'cl.exe', f'CFLAGS_{X64}': '/MT', f'CXXFLAGS_{X64}': '/MT'})

    @unittest.skipUnless(shutil.which('clang'), 'clang is required for the driver regression probe')
    def test_real_clang_preprocessor_selects_arm64_static_crt_and_rejects_old_flag(self):
        env = {}
        CONFIGURE(env, 'aarch64-pc-windows-msvc')
        for language, flag in [('c', 'CFLAGS'), ('c++', 'CXXFLAGS')]:
            with self.subTest(language=language):
                command = [shutil.which('clang'), '--target=aarch64-pc-windows-msvc', '-dM', '-E', '-x', language, '-']
                result = subprocess.run(command + [env[f'{flag}_{ARM64}']], input='', text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('#define _WIN32 1', result.stdout)
                self.assertIn('#define __aarch64__ 1', result.stdout)
                self.assertIn('#define _MT 1', result.stdout)
                self.assertNotIn('#define _DLL ', result.stdout)
                old = subprocess.run(command + ['/MT'], input='', text=True, capture_output=True)
                self.assertNotEqual(old.returncode, 0)
                self.assertIn('/MT', old.stderr)


class UpdaterSecurityEnvironmentTest(unittest.TestCase):
    """The compiled-policy inputs come from GN args only (P1, P3, P4)."""

    TOOLCHAIN = 'sha256:' + '70229e40' * 8

    @staticmethod
    def root(public_key='gTl3Dqh9F19Wo1Rmw0x+zMuNipG07jeiXfYPW4/Js5Q=', **overrides):
        payload = {
            'schema': 'open-shorebird.release-root',
            'contract_version': 2,
            'version': 1,
            'threshold': 1,
            'keys': [{'key_id': 'sha256:' + '6a' * 32, 'public_key': public_key}],
            'expires_at': '2036-11-09T00:00:00Z',
        }
        payload.update(overrides)
        return base64.b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode()

    @staticmethod
    def args(**overrides):
        values = dict(
            security_profile='legacy',
            toolchain_id='',
            security_development_exceptions='false',
            trust_root_key_id=[],
            release_root='',
            release_min_delegations_version='1',
            diversification_profile='',
        )
        values.update(overrides)
        return type('Args', (), values)

    def hardened(self, **overrides):
        values = dict(security_profile='hardened', toolchain_id=self.TOOLCHAIN, release_root=self.root())
        values.update(overrides)
        return self.args(**values)

    def test_hardened_requires_a_release_root(self):
        error = CONFIGURE_SECURITY({}, self.hardened(release_root=''))
        self.assertIn('--release-root', error)

    def test_hardened_passes_the_pin_and_scrubs_inherited_inputs(self):
        env = {
            'SHOREBIRD_BUILD_MANIFEST_KEYS': 'inherited',
            'SHOREBIRD_RELEASE_ROOT': 'inherited',
            'SHOREBIRD_RELEASE_MIN_DELEGATIONS_VERSION': '9',
        }
        self.assertIsNone(CONFIGURE_SECURITY(env, self.hardened(release_min_delegations_version='3')))
        self.assertEqual(env['SHOREBIRD_RELEASE_ROOT'], self.root())
        self.assertEqual(env['SHOREBIRD_RELEASE_MIN_DELEGATIONS_VERSION'], '3')
        self.assertNotIn('SHOREBIRD_BUILD_MANIFEST_KEYS', env)

    def test_malformed_pins_and_floors_are_refused(self):
        not_canonical = base64.b64encode(base64.b64decode(self.root()).replace(b',', b', ', 1)).decode()
        for pin in ('AAAA', self.root().rstrip('='), not_canonical,
                    self.root(schema='open-shorebird.release-delegations'),
                    self.root(contract_version=1), self.root(threshold=2), self.root(keys=[])):
            self.assertIsNotNone(CONFIGURE_SECURITY({}, self.hardened(release_root=pin)), pin)
        for floor in ('0', '-1', '01', 'x', ''):
            self.assertIsNotNone(
                CONFIGURE_SECURITY({}, self.hardened(release_min_delegations_version=floor)), floor)

    def test_development_root_needs_development_exceptions(self):
        development = self.root(public_key='iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w=')
        error = CONFIGURE_SECURITY({}, self.hardened(release_root=development))
        self.assertIn('DEVELOPMENT-ONLY', error)
        self.assertIsNone(CONFIGURE_SECURITY(
            {}, self.hardened(release_root=development, security_development_exceptions='true')))

    def test_legacy_refuses_release_trust_inputs_and_drops_inherited_ones(self):
        env = {'SHOREBIRD_BUILD_MANIFEST_KEYS': 'inherited', 'SHOREBIRD_RELEASE_ROOT': 'inherited'}
        self.assertIsNone(CONFIGURE_SECURITY(env, self.args()))
        self.assertNotIn('SHOREBIRD_BUILD_MANIFEST_KEYS', env)
        self.assertNotIn('SHOREBIRD_RELEASE_ROOT', env)
        self.assertNotIn('SHOREBIRD_RELEASE_MIN_DELEGATIONS_VERSION', env)
        self.assertIsNotNone(CONFIGURE_SECURITY({}, self.args(release_root=self.root())))
        self.assertIsNotNone(CONFIGURE_SECURITY({}, self.args(release_min_delegations_version='2')))


class UpdaterDiversificationEnvironmentTest(unittest.TestCase):
    """hardened-diversified (P5, contract v3): the profile comes from GN only."""

    TOOLCHAIN = UpdaterSecurityEnvironmentTest.TOOLCHAIN
    root = staticmethod(UpdaterSecurityEnvironmentTest.root)
    args = staticmethod(UpdaterSecurityEnvironmentTest.args)
    hardened = UpdaterSecurityEnvironmentTest.hardened

    DEVELOPMENT_PROFILE = ROOT / 'scripts/testdata/diversification/profile.DEVELOPMENT-ONLY.json'
    DEVELOPMENT_ID = 'sha256:832856d0bed27833434e3f26b65d03cedc313f9850c2577ca7565c4dc644f46d'

    @staticmethod
    def encode(profile):
        return base64.b64encode(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).decode()

    @classmethod
    def development(cls):
        return json.loads(cls.DEVELOPMENT_PROFILE.read_text(encoding='utf-8'))

    @classmethod
    def profile(cls, **overrides):
        """An operator profile: the development one with its wires rotated
        (Trap stays 0, no fixed points), so its ID is not the development ID."""
        profile = cls.development()
        wires = profile['bytecode']['wire_opcodes']
        profile['bytecode']['wire_opcodes'] = [0] + wires[2:] + wires[1:2]
        for key, value in overrides.items():
            if key in profile['bytecode']:
                profile['bytecode'][key] = value
            else:
                profile[key] = value
        return profile

    def diversified(self, **overrides):
        values = dict(security_profile='hardened-diversified', toolchain_id=self.TOOLCHAIN,
                      release_root=self.root(), diversification_profile=self.encode(self.profile()))
        values.update(overrides)
        return self.args(**values)

    def test_diversified_passes_the_profile_and_the_hardened_inputs(self):
        env = {'SHOREBIRD_DIVERSIFICATION_PROFILE': 'inherited', 'SHOREBIRD_RELEASE_ROOT': 'inherited'}
        self.assertIsNone(CONFIGURE_SECURITY(env, self.diversified()))
        self.assertEqual(env['SHOREBIRD_SECURITY_PROFILE'], 'hardened-diversified')
        self.assertEqual(env['SHOREBIRD_DIVERSIFICATION_PROFILE'], self.encode(self.profile()))
        self.assertEqual(env['SHOREBIRD_RELEASE_ROOT'], self.root())
        self.assertEqual(env['SHOREBIRD_TOOLCHAIN_ID'], self.TOOLCHAIN)

    def test_diversified_has_every_hardened_requirement(self):
        self.assertIn('--diversification-profile',
                      CONFIGURE_SECURITY({}, self.diversified(diversification_profile='')))
        self.assertIn('--toolchain-id', CONFIGURE_SECURITY({}, self.diversified(toolchain_id='')))
        self.assertIn('--release-root', CONFIGURE_SECURITY({}, self.diversified(release_root='')))

    def test_the_profile_is_refused_and_scrubbed_outside_diversified(self):
        encoded = self.encode(self.profile())
        for args in (self.args(diversification_profile=encoded),
                     self.hardened(diversification_profile=encoded)):
            self.assertIn('hardened-diversified', CONFIGURE_SECURITY({}, args))
        for args in (self.args(), self.hardened()):
            env = {'SHOREBIRD_DIVERSIFICATION_PROFILE': encoded}
            self.assertIsNone(CONFIGURE_SECURITY(env, args))
            self.assertNotIn('SHOREBIRD_DIVERSIFICATION_PROFILE', env)

    def test_development_profile_needs_development_exceptions(self):
        development = self.encode(self.development())
        error = CONFIGURE_SECURITY({}, self.diversified(diversification_profile=development))
        self.assertIn('DEVELOPMENT-ONLY', error)
        self.assertIsNone(CONFIGURE_SECURITY({}, self.diversified(
            diversification_profile=development, security_development_exceptions='true')))

    def test_matches_the_reference_encoding(self):
        # The committed profile's canonical bytes hash to its published ID.
        raw = base64.b64decode(self.encode(self.development()))
        self.assertEqual('sha256:' + hashlib.sha256(raw).hexdigest(), self.DEVELOPMENT_ID)

    def test_malformed_profiles_are_refused(self):
        good = json.dumps(self.profile(), sort_keys=True, separators=(',', ':'))
        identity = list(range(176))
        sixteen = [0] + list(range(1, 17)) + list(range(18, 176)) + [17]
        cases = {
            'pretty': base64.b64encode(json.dumps(self.profile(), sort_keys=True, indent=1).encode()).decode(),
            'unsorted': base64.b64encode(json.dumps(
                dict(reversed(list(self.profile().items()))), separators=(',', ':')).encode()).decode(),
            'duplicate key': base64.b64encode(
                good.replace('"contract_version":3,', '"contract_version":3,"contract_version":3,').encode()
            ).decode(),
            'trailing newline': self.encode(self.profile()) + '\n',
            'url-safe alphabet': self.encode(self.profile()).replace('/', '_').replace('+', '-') + '_',
            'not base64': 'not base64!',
            'oversized': base64.b64encode(b' ' * 8192).decode(),
            'extra field': self.encode(self.profile(extra=1)),
            'native layout': self.encode(self.profile(native_layout={})),
            'contract': self.encode(self.profile(contract_version=2)),
            'scheme': self.encode(self.profile(scheme='opcode-permutation-v2')),
            'format': self.encode(self.profile(format_version=4)),
            'table hash': self.encode(self.profile(opcode_table_sha256='AB' * 32)),
            'one opcode': self.encode(self.profile(wire_opcodes=[0])),
            'too many': self.encode(self.profile(wire_opcodes=[0] + list(range(256, 0, -1)))),
            'trap moved': self.encode(self.profile(wire_opcodes=[1, 0] + list(range(3, 176)) + [2])),
            'not a permutation': self.encode(self.profile(wire_opcodes=[0, 2, 2])),
            'float': self.encode(self.profile(wire_opcodes=[0, 2.0, 1])),
            'identity': self.encode(self.profile(wire_opcodes=identity)),
            'sixteen fixed points': self.encode(self.profile(wire_opcodes=sixteen)),
        }
        for name, encoded in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(
                    CONFIGURE_SECURITY({}, self.diversified(diversification_profile=encoded)), name)
        fifteen = [0] + list(range(1, 16)) + list(range(17, 176)) + [16]
        self.assertIsNone(CONFIGURE_SECURITY({}, self.diversified(
            diversification_profile=self.encode(self.profile(wire_opcodes=fifteen)))))


if __name__ == '__main__':
    unittest.main()

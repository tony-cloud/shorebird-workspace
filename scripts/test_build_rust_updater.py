#!/usr/bin/env python3
"""Check Windows updater compiler/CRT settings without building the updater."""

import base64
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


if __name__ == '__main__':
    unittest.main()

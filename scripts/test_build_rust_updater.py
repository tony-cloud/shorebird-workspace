#!/usr/bin/env python3
"""Check Windows updater compiler/CRT settings without building the updater."""

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
    """The compiled-policy inputs come from GN args only (P1, P3)."""

    TOOLCHAIN = 'sha256:' + '70229e40' * 8
    KEY = 'A' * 43 + '='

    @staticmethod
    def args(**overrides):
        values = dict(
            security_profile='legacy',
            toolchain_id='',
            security_development_exceptions='false',
            trust_root_key_id=[],
            build_manifest_key=[],
        )
        values.update(overrides)
        return type('Args', (), values)

    def test_hardened_requires_a_build_manifest_key(self):
        env = {}
        error = CONFIGURE_SECURITY(
            env, self.args(security_profile='hardened', toolchain_id=self.TOOLCHAIN)
        )
        self.assertIn('--build-manifest-key', error)

    def test_hardened_passes_manifest_keys_and_scrubs_inherited_ones(self):
        env = {'SHOREBIRD_BUILD_MANIFEST_KEYS': 'inherited'}
        error = CONFIGURE_SECURITY(
            env,
            self.args(
                security_profile='hardened',
                toolchain_id=self.TOOLCHAIN,
                build_manifest_key=[self.KEY, 'B' * 43 + '='],
            ),
        )
        self.assertIsNone(error)
        self.assertEqual(env['SHOREBIRD_BUILD_MANIFEST_KEYS'], f"{self.KEY},{'B' * 43}=")

    def test_legacy_refuses_manifest_keys_and_drops_inherited_ones(self):
        env = {'SHOREBIRD_BUILD_MANIFEST_KEYS': 'inherited'}
        self.assertIsNone(CONFIGURE_SECURITY(env, self.args()))
        self.assertNotIn('SHOREBIRD_BUILD_MANIFEST_KEYS', env)
        self.assertIsNotNone(
            CONFIGURE_SECURITY({}, self.args(build_manifest_key=[self.KEY]))
        )


if __name__ == '__main__':
    unittest.main()

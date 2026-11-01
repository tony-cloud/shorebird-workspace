#!/usr/bin/env python3
"""Exercise generated sync configurations without changing checkout state."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read_config(path):
    namespace = {}
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    return namespace


def dependency_enabled(path, dependency, custom_vars, host_os, host_cpu):
    namespace = {}
    overrides = dict(custom_vars, host_os=host_os, host_cpu=host_cpu)
    namespace['Var'] = lambda name: overrides[name] if name in overrides else namespace['vars'][name]
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    values = dict(namespace['vars'], **overrides)
    return eval(namespace['deps'][dependency]['condition'], {'__builtins__': {}}, values)


class GclientConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'flutter').mkdir()
        shutil.copyfile(ROOT / 'scripts/write_gclient.sh', self.root / 'scripts/write_gclient.sh')

    def generate(self, platform, engine='1', android=None):
        env = dict(os.environ, INCLUDE_ENGINE_DEPS=engine)
        env.pop('INCLUDE_ANDROID_DEPS', None)
        if android is not None:
            env['INCLUDE_ANDROID_DEPS'] = android
        return subprocess.run(
            ['bash', str(self.root / 'scripts/write_gclient.sh'), platform],
            env=env, text=True, capture_output=True,
        )

    def test_linux_desktop_excludes_unavailable_arm64_packages_in_both_syncs(self):
        result = self.generate('linux', android='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        configs = [read_config(self.root / name) for name in ('.gclient', 'flutter/.gclient')]
        for config in configs:
            self.assertEqual(config['target_os'], ['linux'])
            self.assertFalse(config['solutions'][0]['custom_vars']['download_android_deps'])
        dart_vars, flutter_vars = [config['solutions'][0]['custom_vars'] for config in configs]
        cases = [
            ('dart-sdk/DEPS', 'dart-sdk/third_party/android_tools', dart_vars),
            ('dart-sdk/DEPS', 'dart-sdk/third_party/openjdk', dart_vars),
            ('flutter/DEPS', 'engine/src/flutter/third_party/android_tools', flutter_vars),
            ('flutter/DEPS', 'engine/src/flutter/third_party/java/openjdk', flutter_vars),
        ]
        for filename, dependency, variables in cases:
            with self.subTest(dependency=dependency):
                self.assertFalse(dependency_enabled(ROOT / filename, dependency, variables, 'linux', 'arm64'))
        # Native engine configuration and its linked source exclusions remain.
        self.assertFalse(flutter_vars['download_dart_sdk'])
        self.assertIn('engine/src/flutter/third_party/dart', configs[1]['solutions'][0]['custom_deps'])

    def test_android_build_defaults_are_preserved(self):
        for platform, targets in [('linux', ['linux', 'android']), ('macos', ['mac', 'ios', 'android'])]:
            with self.subTest(platform=platform):
                result = self.generate(platform)
                self.assertEqual(result.returncode, 0, result.stderr)
                for filename in ('.gclient', 'flutter/.gclient'):
                    config = read_config(self.root / filename)
                    self.assertEqual(config['target_os'], targets)
                    self.assertTrue(config['solutions'][0]['custom_vars']['download_android_deps'])

    def test_windows_preserves_separate_dart_and_engine_defaults(self):
        self.assertEqual(self.generate('windows').returncode, 0)
        dart = read_config(self.root / '.gclient')
        flutter = read_config(self.root / 'flutter/.gclient')
        self.assertEqual(dart['target_os'], ['win'])
        self.assertFalse(dart['solutions'][0]['custom_vars']['download_android_deps'])
        self.assertTrue(flutter['solutions'][0]['custom_vars']['download_android_deps'])

    def test_sdk_only_removes_stale_engine_configuration(self):
        self.assertEqual(self.generate('linux').returncode, 0)
        self.assertEqual(self.generate('linux', engine='0').returncode, 0)
        config = read_config(self.root / '.gclient')
        self.assertEqual(config['target_os'], ['linux'])
        self.assertFalse(config['solutions'][0]['custom_vars']['download_android_deps'])
        self.assertFalse((self.root / 'flutter/.gclient').exists())

    def test_invalid_android_setting_does_not_write_configuration(self):
        result = self.generate('linux', android='sometimes')
        self.assertEqual(result.returncode, 64)
        self.assertIn('INCLUDE_ANDROID_DEPS must be 0 or 1', result.stderr)
        self.assertFalse((self.root / '.gclient').exists())
        self.assertFalse((self.root / 'flutter/.gclient').exists())


if __name__ == '__main__':
    unittest.main()

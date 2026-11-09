#!/usr/bin/env python3
"""Controls for architecture-specific macOS gtest inventory validation."""
from pathlib import Path
import tempfile
import unittest

from verify_macos_interpreter_engine_tests import REQUIRED, SUITE_ARCHES, required_tests
from verify_native_cache_engine_tests import listed_tests


class MacosInventoryTest(unittest.TestCase):
    def inventory(self, names):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'inventory.txt'
        grouped = {}
        for name in sorted(names):
            suite, case = name.split('.', 1)
            grouped.setdefault(suite, []).append(case)
        path.write_text(''.join(suite + '.\n' + ''.join('  ' + case + '\n' for case in cases)
                                for suite, cases in grouped.items()))
        return path

    def test_both_architectures_require_prepared_base_and_target_controls(self):
        controls = {
            'DesktopInterpreterConfigTest.V2CheckedInitFailureRefusesUnpreparedBase',
            'DesktopInterpreterConfigTest.ValidV2NoGrantBeginsPreparedFreeBase',
            'MacArchitectures/MacInterpreterSelectionV2Test.WrongScopeOsOrArchNeverBeginsOrReleasesKey/X64',
            'MacArchitectures/MacInterpreterSelectionV2Test.FullProfileBeginsBeforeMatchingKeyRelease/Arm64',
        }
        for arch in ('arm64', 'x64'):
            for control in controls:
                with self.subTest(arch=arch, control=control), self.assertRaisesRegex(ValueError, 'Missing'):
                    listed_tests(self.inventory(REQUIRED['shorebird'] - {control}),
                                 platform='macos', required=required_tests('shorebird', arch))

    def test_both_architectures_require_source_commitment_controls(self):
        control = 'InterpreterSelectionV2Test.ConcurrentBaseWinDiscardsInstalledPatchBeforeMain'
        for arch in ('arm64', 'x64'):
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, control):
                listed_tests(self.inventory(REQUIRED['shorebird'] - {control}),
                             platform='macos', required=required_tests('shorebird', arch))

    def test_both_architectures_require_complete_same_inventory(self):
        for suite in set(REQUIRED) - set(SUITE_ARCHES):
            self.assertEqual(required_tests(suite, 'arm64'), required_tests(suite, 'x64'))
            self.assertEqual(listed_tests(self.inventory(REQUIRED[suite]), platform='macos',
                                         required=required_tests(suite, 'x64')), REQUIRED[suite])

    def test_hardened_suite_is_arm64_and_keeps_every_legacy_control(self):
        # P6: the hardened-toolchain shorebird_unittests run (arm64 only).
        self.assertLess(REQUIRED['shorebird'], REQUIRED['shorebird-hardened'])
        control = 'UpdaterIntegrity.HardenedInitFailureRecordsTheTypedReason'
        with self.assertRaisesRegex(ValueError, control):
            listed_tests(self.inventory(REQUIRED['shorebird-hardened'] - {control}),
                         platform='macos', required=required_tests('shorebird-hardened', 'arm64'))
        with self.assertRaisesRegex(ValueError, 'does not run on x64'):
            required_tests('shorebird-hardened', 'x64')

    def test_hardened_suite_requires_the_hardened_profile_in_its_log(self):
        import subprocess, sys
        script = Path(__file__).with_name('verify_macos_interpreter_engine_tests.py')
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            (evidence / 'shorebird-hardened-tests.log').write_text(
                "updater security profile legacy differs from the engine's legacy; not initializing\n")
            result = subprocess.run([sys.executable, str(script), str(evidence), '--arch=arm64',
                                     '--suite=shorebird-hardened'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not built with the hardened profile', result.stderr)

    def test_suite_selection_is_limited_to_known_suites(self):
        import subprocess, sys
        script = Path(__file__).with_name('verify_macos_interpreter_engine_tests.py')
        result = subprocess.run([sys.executable, str(script), '.', '--arch=arm64', '--suite=bogus'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid choice', result.stderr)

    def test_unknown_architecture_is_rejected(self):
        with self.assertRaises(ValueError):
            required_tests('shorebird', 'unknown')


if __name__ == '__main__':
    unittest.main()

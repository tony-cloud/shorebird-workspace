"""Pure fixture/report tests; no Flutter, build, network, server or device use."""

import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import ios_device_e2e as runner


class FullFfiReportTest(unittest.TestCase):
    def report(self, phase='update'):
        base = phase == 'base'
        return {
            'schema': runner.FFI_SCHEMA, 'phase': phase,
            'license': 'free' if base else 'pro',
            'pro_feature_enabled': not base, 'structural_verified': True,
            'ffi_verified': True,
            'rendered_text': ('license:free full-replacement:verified value:10 full-ffi:v1-base'
                              if base else
                              'license:pro full-replacement:verified value:28 full-ffi:v2-update'),
            'checks': list(runner.FFI_BASE_CHECKS if base else runner.FFI_UPDATE_CHECKS),
            'workers_started': 0 if base else 2, 'workers_joined': True,
            'callbacks_closed': True, 'static_callbacks_retained': 0 if base else 1,
        }

    def test_completed_base_and_update_are_distinct_and_fit_transport(self):
        for phase in ('base', 'update'):
            with self.subTest(phase=phase):
                report = self.report(phase)
                content = json.dumps(report, separators=(',', ':'))
                body = content.encode()
                self.assertLessEqual(len(body), 1024)
                decoded = runner.read_report_body(io.BytesIO(body), {'Content-Length': str(len(body))})
                self.assertEqual(runner.validate_launch_report(decoded, {'full_replacement_ffi': True}), report)

    def test_old_success_cannot_satisfy_ffi_session(self):
        for content in ('license:free\npro-feature:off\n',
                        'license:pro\npro-feature:enabled\n'):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    runner.validate_launch_report(content, {'full_replacement_ffi': True})
                self.assertEqual(runner.validate_launch_report(content, {}), {'schema': 'license-v1'})

    def test_ffi_status_cannot_be_misread_as_old_structural_success(self):
        with self.assertRaises(ValueError):
            runner.validate_launch_report(json.dumps(self.report()), {'full_replacement': True})

    def test_incomplete_or_mismatched_evidence_is_rejected(self):
        original = self.report()
        changes = {
            'unjoined': {'workers_joined': False},
            'not-closed': {'callbacks_closed': False},
            'not-rendered': {'rendered_text': 'license:pro full-replacement:verified value:28'},
            'not-structural': {'structural_verified': False},
            'not-ffi': {'ffi_verified': False},
            'wrong-count': {'workers_started': 1},
            'boolean-count': {'workers_started': True},
            'wrong-license': {'license': 'free'},
            'wrong-phase': {'phase': 'base'},
            'wrong-static-lifetime': {'static_callbacks_retained': 0},
            'missing-native-check': {'checks': list(runner.FFI_UPDATE_CHECKS[:-1])},
            'duplicate-check': {'checks': [*runner.FFI_UPDATE_CHECKS[:-1], 'native-version']},
        }
        for label, values in changes.items():
            with self.subTest(case=label):
                report = {**copy.deepcopy(original), **values}
                with self.assertRaises(ValueError):
                    runner.validate_launch_report(json.dumps(report), {'full_replacement_ffi': True})

    def test_macho_export_check_requires_every_released_symbol(self):
        exports = '\n'.join(f'0x1234 _{symbol}' for symbol in runner.FFI_EXPORTS)
        self.assertEqual(runner.verified_ffi_exports(exports), list(runner.FFI_EXPORTS))
        with self.assertRaisesRegex(RuntimeError, 'IosFullFfiChangeRecordV2'):
            runner.verified_ffi_exports(exports.replace('_IosFullFfiChangeRecordV2\n', '_IosFullFfiChangeRecordV2Wrong\n'))


class FullFfiTemplateTest(unittest.TestCase):
    def test_patch_removes_old_library_and_keeps_native_and_unchanged_consumer(self):
        with tempfile.TemporaryDirectory(prefix='ios-ffi-template-') as directory:
            work = Path(directory)
            fixture = runner.FIXTURE / 'tool/full_replacement_ffi'
            shutil.copytree(fixture, work / 'app/tool/full_replacement_ffi')
            lib = work / 'app/lib'
            lib.mkdir(parents=True)
            support = lib / 'launch_status_io.dart'
            support.write_text('unchanged support')
            native = work / 'app/ios/Runner/FullFfiFixture.c'
            native.parent.mkdir(parents=True)
            shutil.copyfile(runner.FIXTURE / 'ios/Runner/FullFfiFixture.c', native)
            original_native = native.read_bytes()
            runner.apply_fixture(work, 'base', 'full_replacement_ffi')
            self.assertTrue((lib / 'removed_library.dart').exists())
            self.assertNotIn('NativeCallable', (lib / 'ffi_plugin.dart').read_text())
            consumer = (lib / 'unchanged_caller.dart').read_bytes()
            runner.apply_fixture(work, 'patch', 'full_replacement_ffi')
            self.assertFalse((lib / 'removed_library.dart').exists())
            self.assertTrue((lib / 'pro_feature.dart').exists())
            self.assertEqual((lib / 'unchanged_caller.dart').read_bytes(), consumer)
            self.assertEqual(native.read_bytes(), original_native)
            self.assertEqual(support.read_text(), 'unchanged support')

    def test_existing_profiles_keep_their_selection(self):
        self.assertEqual(runner.fixture_profile({'structural_updates': True}), 'structural')
        self.assertEqual(runner.fixture_profile({'full_replacement': True}), 'full_replacement')
        self.assertEqual(runner.fixture_profile({'full_replacement': True, 'full_replacement_ffi': True}), 'full_replacement_ffi')


if __name__ == '__main__':
    unittest.main()

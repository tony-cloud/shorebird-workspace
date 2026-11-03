"""Pure/preparation controls only: no build, network, signing, app or device."""
import copy
import errno
import json
import os
from pathlib import Path
import plistlib
import tempfile
import time
import types
import unittest
from unittest import mock

import macos_interpreter_e2e as runner
import test_ios_device_e2e as shared_tests


class PreparedFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='macos-interpreter-preparation-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / 'session'
        self.args = types.SimpleNamespace(
            cli=Path('/private/caller/shorebird'), flutter_root=Path('/private/caller/flutter'),
            engine_src=Path('/private/caller/engine'), engine_name='macos_release_arm64',
            host_engine_name='macos_release_arm64', server=Path('/private/caller/server'),
            release_version='1.0.0+1')
        # Preparation must not probe network sockets, even for ephemeral ports.
        with mock.patch.object(runner, 'unique_ports', return_value=(43101, 43102)):
            self.state = runner.prepare(self.args, self.work)

    def test_native_membership_and_registration_precede_first_project(self):
        app = self.work / 'app'
        window = (app / 'macos/Runner/MainFlutterWindow.swift').read_text()
        self.assertLess(window.index('MacosInterpreterFixture.beforeEngine()'),
                        window.index('FlutterViewController()'))
        helper = (app / 'macos/Runner/PatchTestKeys.swift').read_text()
        self.assertIn(self.state['key_service'], helper)
        self.assertIn(self.state['key_id'], helper)
        self.assertNotIn('aes_key_hex', helper)
        self.assertIn('guard keyID == account else { return nil }', helper)
        self.assertIn('Refusing another Keychain account', helper)
        self.assertIn('case "delete":', helper)
        self.assertIn('if action == "delete"', helper)
        self.assertIn('precondition(IosFullFfiRetain() == 13', helper)
        project = (app / 'macos/Runner.xcodeproj/project.pbxproj').read_text()
        self.assertEqual(project.count('MACOSX_DEPLOYMENT_TARGET = 12.0;'), 3)
        self.assertNotIn('MACOSX_DEPLOYMENT_TARGET = 10.15;', project)
        original = (runner.FIXTURE / 'macos/Runner.xcodeproj/project.pbxproj').read_text()
        old = 'MACOSX_DEPLOYMENT_TARGET = 10.15;'
        for changed in (original.replace(old, '', 1), original + '\n' + old,
                        original.replace(old, 'MACOSX_DEPLOYMENT_TARGET = 11.0;', 1)):
            with self.subTest(deployment_target=changed.count(old)), self.assertRaises(ValueError):
                runner.normalize_macos_minimum(changed)
        sources = project.split('33CC10E92044A3C60003C045 /* Sources */ = {', 1)[1].split('};', 1)[0]
        for name in ('FullFfiFixture.c', 'PatchTestKeys.swift'):
            self.assertIn(name + ' in Sources', sources)
            self.assertEqual(project.count(name + ' in Sources'), 2)
        release = (app / 'macos/Runner/Configs/Release.xcconfig').read_text()
        for setting in ('ENABLE_HARDENED_RUNTIME = YES', '-Wl,-export_dynamic',
                        'STRIP_STYLE = non-global', 'SWIFT_OBJC_BRIDGING_HEADER'):
            self.assertIn(setting, release)
        self.assertEqual((app / 'macos/Runner/FullFfiFixture.c').read_bytes(),
                         (runner.FIXTURE / 'ios/Runner/FullFfiFixture.c').read_bytes())
        self.assertEqual(plistlib.loads((app / 'macos/Runner/Release.entitlements').read_bytes()),
                         runner.APP_ENTITLEMENTS)
        self.assertEqual((self.work / 'secrets.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o700)
        self.assertFalse('app_id' in self.state)
        self.assertNotIn('base_built', self.state)

    def test_fixture_switch_preserves_native_and_unchanged_caller(self):
        hashes = runner.native_hashes(self.work)
        caller = (self.work / 'app/lib/unchanged_caller.dart').read_bytes()
        self.assertTrue((self.work / 'app/lib/removed_library.dart').exists())
        runner.apply_profile(self.work, 'patch')
        self.assertEqual(runner.native_hashes(self.work), hashes)
        self.assertEqual((self.work / 'app/lib/unchanged_caller.dart').read_bytes(), caller)
        self.assertFalse((self.work / 'app/lib/removed_library.dart').exists())
        status = (self.work / 'app/lib/ffi_status.dart').read_text()
        self.assertIn('shorebird-macos-fixture/ffi-status.json', status)
        self.assertIn("'schema': 'full-replacement-ffi-v1'", status)
        with self.assertRaises(ValueError):
            runner.prepare(self.args, self.work)

    def test_commands_are_real_macos_arm64_cli_and_no_frozen_output(self):
        for phase in ('base', 'update'):
            command = runner.cli_command(self.work, self.state, phase)
            self.assertIn('macos', command)
            self.assertNotIn('ios', command)
            self.assertEqual(command[-2:], ['--', '--target-platform=darwin-arm64'])
            self.assertIn('--no-codesign', command)
            self.assertIn('--local-engine=macos_release_arm64', command)
            self.assertIn('--local-engine-host=macos_release_arm64', command)
        args = copy.copy(self.args)
        args.engine_src = runner.ROOT / 'flutter/engine/src'
        with self.assertRaisesRegex(ValueError, 'Frozen engine'):
            runner.prepare(args, Path(self.temporary.name) / 'frozen')

    def test_cli_and_direct_flutter_storage_urls_are_both_explicit(self):
        path = self.work / 'secrets.json'
        credentials = json.loads(path.read_text())
        credentials['access_token'] = 'disposable-test-token'
        runner.shared.write_json(path, credentials)
        env = runner.build_environment(self.work, self.state, 'base')
        self.assertEqual(env['SHOREBIRD_FLUTTER_STORAGE_BASE_URL'], 'https://storage.googleapis.com')
        self.assertEqual(env['FLUTTER_STORAGE_BASE_URL'], 'https://storage.googleapis.com')
        self.assertEqual(env['SHOREBIRD_ARTIFACT_BASE_URL'], self.state['api_url'])
        self.assertNotIn('SHOREBIRD_AOT_PATCH_KEY_HEX', env)

    def test_foreign_paths_and_redirected_scope_rejected(self):
        foreign = Path(self.temporary.name) / 'foreign'
        foreign.mkdir()
        redirected = self.work / 'foreign-link'
        redirected.symlink_to(foreign, target_is_directory=True)
        for path in (foreign, self.work, redirected / 'anything'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                runner.contained(self.work, path)
        with self.assertRaises(ValueError):
            runner.app_identity(self.work, self.state, foreign)
        foreign_app = self.work / 'foreign.app/Contents'
        foreign_app.mkdir(parents=True)
        (foreign_app / 'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'org.foreign.app', 'CFBundleExecutable': 'Runner'}))
        with self.assertRaisesRegex(ValueError, 'foreign application'):
            runner.app_identity(self.work, self.state, foreign_app.parent)

    def test_status_requires_fresh_exact_phase_and_full_ffi_schema(self):
        report = shared_tests.FullFfiReportTest().report('update')
        path = self.work / 'fresh-report.json'
        path.write_text(json.dumps(report))
        now = time.time()
        os.utime(path, (now, now))
        # setUp may have run within 10 ms; the launch must not predate preparation.
        launched = max(self.state['created'], now - .01)
        result = runner.validate_status(self.work, self.state, path, launched, 'update')
        self.assertEqual(len(result['verification']['checks']), 16)
        with self.assertRaisesRegex(ValueError, 'stale'):
            runner.validate_status(self.work, self.state, path, now + 1, 'update')
        with self.assertRaisesRegex(ValueError, 'phase'):
            runner.validate_status(self.work, self.state, path, self.state['created'], 'base')
        path.write_text('license:pro\npro-feature:enabled\n')
        with self.assertRaises(ValueError):
            runner.validate_status(self.work, self.state, path, self.state['created'], 'update')
        outside = Path(self.temporary.name) / 'unrelated.json'
        outside.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            runner.validate_status(self.work, self.state, outside, 0, 'update')

    def test_container_record_and_timestamps_are_bound_to_session(self):
        record = {'schema': 'macos-interpreter-container-v1', 'bundle_id': self.state['bundle_id'],
                  'home': str(runner.container_root(self.state) / 'Data'),
                  'status_path': str(runner.container_file(self.state, 'ffi-status.json')),
                  'time': time.time(), 'pid': 123}
        runner.validate_container_record(self.state, record, self.state['created'])
        for change in ({'bundle_id': 'foreign'}, {'home': '/tmp/foreign'}, {'pid': True},
                       {'status_path': '/etc/foreign'}, {'time': float('nan')},
                       {'time': self.state['created'] - 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_container_record(self.state, {**record, **change}, self.state['created'])
        for after in (float('nan'), float('inf'), self.state['created'] - 1):
            with self.subTest(after=after), self.assertRaises(ValueError):
                runner.validate_status(self.work, self.state, self.work / 'unused', after, 'base')

    def test_child_key_controls_are_private_scoped_and_absent_offline(self):
        offline = runner.app_environment(self.work, self.state)
        self.assertFalse(any(key.startswith('OPEN_SHOREBIRD_TEST_') for key in offline))
        provisioning = runner.app_environment(self.work, self.state, action='provision')
        self.assertEqual(provisioning['OPEN_SHOREBIRD_TEST_KEY_ID'], self.state['key_id'])
        self.assertEqual(len(provisioning['OPEN_SHOREBIRD_TEST_KEY_HEX']), 64)
        deletion = runner.app_environment(self.work, self.state, action='delete', control_only=True)
        self.assertNotIn('OPEN_SHOREBIRD_TEST_KEY_HEX', deletion)
        self.assertEqual(deletion['OPEN_SHOREBIRD_TEST_KEY_CONTROL_ONLY'], '1')
        with self.assertRaises(ValueError):
            runner.app_environment(self.work, self.state, action='delete-unrelated')

    def test_signing_plan_signs_every_nested_framework_before_outer_app(self):
        app = self.work / 'released/Test.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': self.state['bundle_id'], 'CFBundleExecutable': 'Runner'}))
        # Only code-inventory/command planning: these bytes are not executable
        # and are never submitted to a signing tool or counted as runtime proof.
        (app / 'Contents/MacOS/Runner').write_bytes(b'\xcf\xfa\xed\xfe' + bytes(16))
        frameworks = []
        for name in ('App', 'FlutterMacOS'):
            framework = app / f'Contents/Frameworks/{name}.framework'
            binary = framework / f'Versions/A/{name}'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'\xcf\xfa\xed\xfe' + bytes(16))
            (framework / 'Versions/Current').symlink_to('A', target_is_directory=True)
            frameworks.append(framework)
        commands = runner.signing_plan(self.work, self.state, app, 'DEVELOPER-ID')
        self.assertEqual({command[-1] for command in commands[:-1]}, {str(path.resolve()) for path in frameworks})
        self.assertEqual(commands[-1][-1], str(app.resolve()))
        for command in commands:
            self.assertIn('runtime', command)
            self.assertNotIn('--deep', command)
            self.assertIn('--force-library-entitlements', command)
        (app / 'Contents/Frameworks/foreign').symlink_to(Path(self.temporary.name) / 'outside')
        with self.assertRaises(ValueError):
            runner.signing_plan(self.work, self.state, app, 'DEVELOPER-ID')

    def test_cleanup_refuses_missing_foreign_or_stale_key_deletion(self):
        runner.begin_cleanup(self.work, self.state)
        receipt = {'schema': 'macos-interpreter-key-control-v1', 'action': 'delete',
                   'result': 'deleted', 'bundle_id': self.state['bundle_id'],
                   'service': self.state['key_service'], 'account': self.state['key_id'],
                   'time': time.time()}
        runner.validate_cleanup_receipt(self.state, receipt)
        for change in ({'account': 'foreign'}, {'service': 'foreign'}, {'action': 'overwrite'},
                       {'time': self.state['created'] - 1}, {'result': 'failed'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_cleanup_receipt(self.state, {**receipt, **change})
        receipt_file = self.work / 'key-delete.json'
        receipt_file.write_text(json.dumps(receipt))
        with mock.patch.object(runner, 'port_open', return_value=True):
            with self.assertRaisesRegex(ValueError, 'no saved PID'):
                runner.cleanup(self.work, self.state, receipt_file)
        self.assertTrue((self.work / 'secrets.json').exists())

    def key_receipt(self, action='delete', result='deleted'):
        return {'schema': 'macos-interpreter-key-control-v1', 'action': action,
                'result': result, 'bundle_id': self.state['bundle_id'],
                'service': self.state['key_service'], 'account': self.state['key_id'],
                'time': time.time()}

    def test_cleanup_rejects_copied_delete_after_native_reprovision(self):
        home = self.work.resolve() / 'host-home'
        with mock.patch.object(runner.Path, 'home', return_value=home), \
                mock.patch.object(runner, 'port_open', return_value=False):
            runner.begin_cleanup(self.work, self.state)
            canonical = runner.container_file(self.state, 'key-control.json')
            canonical.parent.mkdir(parents=True)
            old_delete = self.key_receipt()
            copied = self.work / 'old-deletion.json'
            runner.shared.write_json(canonical, old_delete)
            runner.shared.write_json(copied, old_delete)
            # Native provisioning replaces the canonical record after deletion.
            runner.shared.write_json(canonical, self.key_receipt('provision', 'stored'))
            with self.assertRaisesRegex(ValueError, 'fresh deletion'):
                runner.cleanup(self.work, self.state, copied)
            self.assertTrue((self.work / 'secrets.json').exists())
            self.assertTrue(runner.container_root(self.state).exists())
            self.assertNotIn('cleanup', self.state)

    def test_cleanup_requires_fence_and_current_canonical_receipt(self):
        with self.assertRaisesRegex(ValueError, 'Begin cleanup'):
            runner.validate_cleanup_receipt(self.state, self.key_receipt())
        home = self.work.resolve() / 'host-home'
        with mock.patch.object(runner.Path, 'home', return_value=home), \
                mock.patch.object(runner, 'port_open', return_value=False):
            copied = self.work / 'copied-delete.json'
            old_delete = self.key_receipt()
            runner.shared.write_json(copied, old_delete)
            runner.begin_cleanup(self.work, self.state)
            with self.assertRaises(FileNotFoundError):
                runner.cleanup(self.work, self.state, copied)
            canonical = runner.container_file(self.state, 'key-control.json')
            canonical.parent.mkdir(parents=True)
            runner.shared.write_json(canonical, old_delete)
            with self.assertRaisesRegex(ValueError, 'fresh deletion'):
                runner.cleanup(self.work, self.state, copied)
            # Even an otherwise valid old copy cannot replace a different latest
            # successful deletion. Only an exact copy of canonical is accepted.
            runner.shared.write_json(canonical, self.key_receipt())
            with self.assertRaisesRegex(ValueError, 'not the latest'):
                runner.cleanup(self.work, self.state, copied)
            self.assertTrue((self.work / 'secrets.json').exists())

    def test_fresh_canonical_deleted_and_absent_receipts_allow_cleanup(self):
        for result in ('deleted', 'absent'):
            with self.subTest(result=result):
                work = self.work / result
                work.mkdir()
                state = copy.deepcopy(self.state)
                state['work_dir'] = str(work.resolve())
                runner.save(work, state)
                runner.shared.private_write(work / 'secrets.json', '{}')
                home = work.resolve() / 'host-home'
                with mock.patch.object(runner.Path, 'home', return_value=home), \
                        mock.patch.object(runner, 'port_open', return_value=False):
                    runner.begin_cleanup(work, state)
                    canonical = runner.container_file(state, 'key-control.json')
                    canonical.parent.mkdir(parents=True)
                    runner.shared.write_json(canonical, self.key_receipt(result=result))
                    copied = work / 'fresh-copy.json'
                    copied.write_bytes(canonical.read_bytes())
                    runner.cleanup(work, state, copied)
                    self.assertTrue(state['cleanup']['key_receipt_verified'])
                    self.assertTrue(state['cleanup']['cleanup_complete'])
                    self.assertFalse(runner.container_root(state).exists())
                    self.assertFalse((work / 'secrets.json').exists())
                    saved = Path(state['cleanup']['key_deletion_evidence'])
                    self.assertEqual(saved.read_bytes(), copied.read_bytes())
                    self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_only_os_metadata_shell_is_recorded_as_incomplete_after_payload_cleanup(self):
        home = self.work.resolve() / 'host-home'
        with mock.patch.object(runner.Path, 'home', return_value=home), \
                mock.patch.object(runner, 'port_open', return_value=False):
            app = self.work / 'released/Test.app'
            (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': self.state['bundle_id'], 'CFBundleExecutable': 'Runner'}))
            (app / 'Contents/MacOS/Runner').write_bytes(b'fixture; never launched')
            self.state['released_app'] = str(app)
            runner.save(self.work, self.state)
            runner.begin_cleanup(self.work, self.state)
            canonical = runner.container_file(self.state, 'key-control.json')
            canonical.parent.mkdir(parents=True)
            runner.shared.write_json(canonical, self.key_receipt())
            native_bytes = canonical.read_bytes()
            root = runner.container_root(self.state)
            metadata = root / '.com.apple.containermanagerd.metadata.plist'
            metadata.write_bytes(b'OS managed fixture metadata')
            config = self.work / 'config'
            config.mkdir()
            (config / 'credentials').write_text('disposable')
            stale_state = copy.deepcopy(self.state)
            original_unlink = runner.Path.unlink
            failure_errno = errno.EPERM

            def protected_metadata(path, *args, **kwargs):
                if path == metadata:
                    self.assertFalse(app.exists())
                    self.assertFalse((root / 'Data').exists())
                    self.assertEqual((self.work / 'cleanup-evidence/native-key-deletion.json').read_bytes(), native_bytes)
                    raise PermissionError(failure_errno, 'OS-managed metadata protected', str(path))
                return original_unlink(path, *args, **kwargs)

            # Both permitted errno values are narrowly accepted, including a
            # retry after the canonical Data receipt has already been removed.
            for failure_errno in (errno.EPERM, errno.EACCES):
                with mock.patch.object(runner.Path, 'unlink', new=protected_metadata):
                    runner.cleanup(self.work, self.state, canonical)
                result = self.state['cleanup']
                self.assertFalse(result['cleanup_complete'])
                self.assertFalse(result['container_removed'])
                self.assertFalse(result['owned_app_and_container_removed'])
                self.assertTrue(result['owned_app_removed'])
                self.assertTrue(result['container_payload_removed'])
                self.assertEqual(result['os_metadata_shell_remaining'], str(root))
                self.assertEqual(list(root.iterdir()), [metadata])
                self.assertFalse((self.work / 'secrets.json').exists())
                self.assertFalse(config.exists())
            with self.assertRaisesRegex(ValueError, 'cannot launch'):
                runner.app_environment(self.work, stale_state, action='provision')
            with self.assertRaisesRegex(ValueError, 'cannot launch'):
                runner.app_launch_command(self.work, stale_state)
            with self.assertRaisesRegex(ValueError, 'cannot launch'):
                runner.begin_cleanup(self.work, stale_state)
            # Even a cleanup checkpoint cannot override a later native control.
            canonical.parent.mkdir(parents=True)
            runner.shared.write_json(canonical, self.key_receipt('provision', 'stored'))
            with self.assertRaisesRegex(ValueError, 'Latest native key control changed'):
                runner.cleanup(self.work, self.state, canonical)

    def test_other_container_permission_errors_remain_fatal_with_saved_receipt(self):
        home = self.work.resolve() / 'host-home'
        with mock.patch.object(runner.Path, 'home', return_value=home), \
                mock.patch.object(runner, 'port_open', return_value=False):
            runner.begin_cleanup(self.work, self.state)
            canonical = runner.container_file(self.state, 'key-control.json')
            canonical.parent.mkdir(parents=True)
            runner.shared.write_json(canonical, self.key_receipt())
            root = runner.container_root(self.state)
            original_rmtree = runner.shutil.rmtree

            def protected_data(path, *args, **kwargs):
                if Path(path) == root / 'Data':
                    raise PermissionError(errno.EACCES, 'Payload removal refused', str(path))
                return original_rmtree(path, *args, **kwargs)

            with mock.patch.object(runner.shutil, 'rmtree', new=protected_data):
                with self.assertRaises(PermissionError):
                    runner.cleanup(self.work, self.state, canonical)
            self.assertNotIn('cleanup', self.state)
            self.assertTrue((self.work / 'secrets.json').exists())
            saved = Path(self.state['cleanup_in_progress']['receipt_evidence'])
            self.assertEqual(saved.read_bytes(), canonical.read_bytes())
            with self.assertRaisesRegex(ValueError, 'cannot launch'):
                runner.app_environment(self.work, self.state, action='overwrite')

    def test_metadata_exception_rejects_wrong_errno_or_other_remaining_content(self):
        home = self.work.resolve() / 'host-home'
        with mock.patch.object(runner.Path, 'home', return_value=home):
            root = runner.container_root(self.state)
            root.mkdir(parents=True)
            metadata = root / '.com.apple.containermanagerd.metadata.plist'
            metadata.write_bytes(b'OS metadata')
            original_unlink = runner.Path.unlink

            def wrong_errno(path, *args, **kwargs):
                if path == metadata:
                    raise PermissionError(errno.EIO, 'Unexpected error', str(path))
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(runner.Path, 'unlink', new=wrong_errno):
                with self.assertRaises(PermissionError):
                    runner.remove_owned_container(root)

            def another_entry(path, *args, **kwargs):
                if path == metadata:
                    (root / 'unexpected').write_text('still owned payload')
                    raise PermissionError(errno.EPERM, 'Protected metadata', str(path))
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(runner.Path, 'unlink', new=another_entry):
                with self.assertRaises(PermissionError):
                    runner.remove_owned_container(root)




class SigningAndMetadataTest(unittest.TestCase):
    details = ('CodeDirectory v=20500 flags=0x10000(runtime) hashes=2\n'
               'Authority=Developer ID Application: Fixture (TEAM123456)\n'
               'TeamIdentifier=TEAM123456\n')

    def test_exact_non_executable_application_entitlements_and_empty_libraries(self):
        for application, expected in ((True, runner.APP_ENTITLEMENTS), (False, {})):
            self.assertEqual(runner.parse_signature(self.details,
                             plistlib.dumps(expected).decode(), application=application)['entitlements'], expected)
            for key in ('com.apple.security.cs.allow-jit',
                        'com.apple.security.cs.allow-unsigned-executable-memory',
                        'com.apple.security.cs.disable-library-validation',
                        'com.apple.security.get-task-allow', 'get-task-allow',
                        'com.apple.security.network.server'):
                for value in (False, True):
                    with self.subTest(key=key, application=application, value=value), self.assertRaises(ValueError):
                        runner.parse_signature(self.details, plistlib.dumps({**expected, key: value}).decode(),
                                               application=application)
        for details in (self.details.replace('runtime', 'adhoc').replace('10000', '2'),
                        self.details.replace('Developer ID Application:', 'Apple Development:'),
                        self.details.replace('TeamIdentifier=TEAM123456', 'TeamIdentifier=not set')):
            with self.assertRaises(ValueError):
                runner.parse_signature(details, plistlib.dumps({}).decode(), application=False)
        for xml in ('', '<plist><dict>', '<?xml version="1.0"?><plist><dict></plist>', plistlib.dumps([]).decode()):
            with self.assertRaises((ValueError, plistlib.InvalidFileException)):
                runner.parse_signature(self.details, xml, application=False)

    def test_macos_metadata_preflight_cannot_accept_ios_or_other_release(self):
        state = {'app_id': 'app', 'release_version': '1.0+1', 'key_id': 'fixture-key'}
        envelope = {'format': 'open-aot-vmcode-encrypted-v1', 'payload_kind': 'full-snapshot',
                    'metadata': {'app_id': 'app', 'app_build_id': '1.0+1', 'target_os': 'macos',
                                 'target_arch': 'arm64', 'runtime_mode': 'dart-bytecode-interpreter'},
                    'encryption': {'algorithm': 'AES-256-GCM', 'key_id': 'fixture-key'}}
        self.assertEqual(runner.validate_envelope_metadata(envelope, state)['target_os'], 'macos')
        for key, value in (('target_os', 'ios'), ('target_arch', 'x64'), ('app_id', 'foreign'),
                           ('app_build_id', 'other'), ('runtime_mode', 'native-aot')):
            altered = copy.deepcopy(envelope)
            altered['metadata'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate_envelope_metadata(altered, state)
        for change in ({'payload_kind': 'delta'}, {'format': 'native-image'},
                       {'encryption': {'algorithm': 'AES-256-GCM', 'key_id': 'foreign'}}):
            with self.assertRaises(ValueError):
                runner.validate_envelope_metadata({**envelope, **change}, state)


if __name__ == '__main__':
    unittest.main()

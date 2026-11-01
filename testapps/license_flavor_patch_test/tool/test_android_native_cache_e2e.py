"""Pure Android lifecycle contracts: no SDK, build, server or device operations."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

SPEC = importlib.util.spec_from_file_location('android_native_cache_e2e',
    Path(__file__).with_name('android_native_cache_e2e.py'))
e2e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e2e)


def context():
    return {'app_id': '7cd314d0-8ace-46a2-b21b-81a92bcd0738', 'release_id': 17,
            'patch_number': 3, 'patch_hash': 'a' * 64, 'transport_hash': 'b' * 64,
            'platform': 'android', 'arch': 'aarch64', 'device_id_hash': 'c' * 64}


def entry(path, *, data=b'{}', parsed=None):
    result = {'path': path, 'size': len(data), 'sha256': e2e.sha256(data), 'prefix_hex': data[:4].hex()}
    if parsed is not None:
        result['json'] = parsed
    return result


def inventory():
    # The state directory and cache directory are deliberately distinct.
    return {'updater_roots': [
        {'label': 'files', 'present': True, 'files': [entry('state.json', parsed={'client_id': 'real-client'})]},
        {'label': 'code-cache', 'present': True, 'files': [
            entry('pointers.json', parsed={'next_boot_patch': 3}),
            entry('patches/3/state.json', parsed={'kind': 'InstalledEncryptedTransportV1',
                'signature': 'managed-signature', 'ciphertext_size': 9, 'size': 100}),
            entry('patches/3/transport.aesgcm', data=b'encrypted')]}],
        'provider_files': [{'path': 'd' * 64 + '.wrapped', 'size': 64, 'sha256': 'e' * 64}],
        'alias_present': True, 'key_scan': 'passed', 'inventory_scope': 'final-files-only'}


def native(operation='ready', nonce='a' * 48):
    return {'schema': e2e.CONTROL_SCHEMA, 'nonce': nonce, 'pid': 42,
        'process_token': 'b' * 32, 'operation': operation, 'payload': {'provider_registered': True}}


class ContextTest(unittest.TestCase):
    def test_android_exact_context_and_independent_hmac_vector(self):
        import hashlib
        import hmac
        aad = e2e.canonical_aad(context())
        self.assertEqual(e2e.parse_aad(aad), {k: str(v) for k, v in context().items()})
        self.assertIn('\nplatform=android\narch=aarch64\n', aad)
        key_id, key = e2e.delivery_material(b'fixture-secret', aad)
        self.assertEqual(key, hmac.new(b'fixture-secret', b'key\0' + aad.encode(), hashlib.sha256).digest())
        self.assertEqual(len(key_id), 16)
        authority = e2e.KeyAuthority(b'fixture-secret', 'token')
        request = json.dumps({'key_id': key_id, 'aad': aad}).encode()
        with self.assertRaises(ValueError):
            authority.provision('Bearer token', request)
        authority.allow(context())
        self.assertEqual(authority.provision('Bearer token', request), key.hex().encode())
        self.assertEqual(authority.accepted, 1)
        for authorization, body in ((None, request), ('Bearer wrong', request),
                ('Bearer token', b'{"aad":"x","aad":"y","key_id":"z"}'),
                ('Bearer token', request.replace(b'patch_number=3', b'patch_number=4'))):
            with self.subTest(authorization=authorization), self.assertRaises(ValueError):
                authority.provision(authorization, body)
        self.assertEqual(authority.rejected, 5)

    def test_rejects_other_platform_arch_noncanonical_and_extra_context(self):
        aad = e2e.canonical_aad(context())
        for value in (aad.replace('platform=android', 'platform=linux'),
                      aad.replace('arch=aarch64', 'arch=arm64'), aad + '\n',
                      aad.replace('release_id=17', 'release_id=017'),
                      aad.replace('app_id=7cd', 'app_id=7CD'), None, []):
            with self.subTest(value=repr(value)[:60]), self.assertRaises(ValueError):
                e2e.parse_aad(value)
        with self.assertRaises(ValueError):
            e2e.canonical_aad(dict(context(), extra=True))


class ReceiptTest(unittest.TestCase):
    def test_post_frame_requires_actual_pid_token_patch_and_rendered_text(self):
        report = {'schema': e2e.SCHEMA, 'nonce': 'a' * 48, 'pid': 42, 'process_token': 'b' * 32,
                  'post_frame': True, 'rendered': ['license:pro', 'pro-feature:enabled'], 'current_patch': 3}
        self.assertIs(e2e.validate_report(report, 'a' * 48, 42, 'b' * 32, 3, 'U'), report)
        for field, value in (('schema', e2e.portable.SCHEMA), ('nonce', 'stale'), ('pid', True),
                             ('pid', 41), ('post_frame', False), ('current_patch', 0),
                             ('current_patch', True), ('rendered', ['license:pro']),
                             ('process_token', 'c' * 32)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                e2e.validate_report(dict(report, **{field: value}), 'a' * 48, 42, 'b' * 32, 3, 'U')
        with self.assertRaises(ValueError):
            e2e.validate_report(dict(report, extra=True), 'a' * 48, 42, 'b' * 32, 3, 'U')

    def test_android_dart_uses_actual_engine_export_and_render_objects(self):
        for marker in ('addPostFrameCallback', 'object is! RenderParagraph', 'object.text.toPlainText()',
                       "DynamicLibrary.open('libflutter.so')", 'shorebird_current_boot_patch_number',
                       'SHOREBIRD_E2E_PROCESS_TOKEN', e2e.SCHEMA):
            self.assertIn(marker, e2e.DART_FIXTURE)
        self.assertNotIn('flutter_windows.dll', e2e.DART_FIXTURE)

    def test_native_report_exact_fields_and_process_identity(self):
        report = native()
        self.assertIs(e2e.validate_native(report, 'a' * 48, 'ready', pid=42, process_token='b' * 32), report)
        for field, value in (('schema', 'other'), ('nonce', 'stale'), ('pid', True), ('pid', 0),
                             ('process_token', 'bad'), ('operation', 'error'), ('payload', {})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                e2e.validate_native(dict(report, **{field: value}), 'a' * 48, 'ready')
        with self.assertRaises(ValueError):
            e2e.validate_native(report, 'a' * 48, 'ready', process_token='c' * 32)
        with self.assertRaises(ValueError):
            e2e.validate_native(dict(report, extra=True), 'a' * 48, 'ready')

    def test_native_probe_and_clear_are_strict(self):
        report = dict(native(), operation='probe', payload={'mac_hex': 'c' * 64, 'native_thread_attached': True})
        e2e.validate_native(report, 'a' * 48, 'probe')
        with self.assertRaises(ValueError):
            e2e.validate_native(dict(report, payload=dict(report['payload'], native_thread_attached=False)), 'a' * 48, 'probe')
        clear = dict(native(), operation='clear-provider', payload={'records': 0, 'alias_present': False})
        e2e.validate_native(clear, 'a' * 48, 'clear-provider')
        with self.assertRaises(ValueError):
            e2e.validate_native(dict(clear, payload={'records': False, 'alias_present': False}), 'a' * 48, 'clear-provider')


class InventoryTest(unittest.TestCase):
    def test_separate_context_roots_produce_exact_ciphertext_candidate(self):
        files = e2e.inspect_inventory(inventory(), patch_hash='f' * 64, require_key=True)
        self.assertEqual(set(files), {'state.json', 'pointers.json', 'patches/3/state.json', 'patches/3/transport.aesgcm'})
        evidence = e2e.candidate_evidence(files, 3)
        self.assertEqual(evidence['transport_sha256'], e2e.sha256(b'encrypted'))
        self.assertEqual(evidence['kind'], 'InstalledEncryptedTransportV1')
        self.assertTrue(evidence['signature_present'])

    def test_inspection_rejects_unsafe_names_executable_magic_and_plaintext_hash(self):
        for item in (entry('../escape', data=b'no'), entry('/absolute', data=b'no'),
                     entry('patches/3/app.so', data=b'no'), entry('patches/3/transport.aesgcm', data=b'\x7fELF'),
                     entry('patches/3/transport.aesgcm', data=b'MZ\x00\x00')):
            payload = inventory()
            payload['updater_roots'][1]['files'].append(item)
            with self.subTest(path=item['path']), self.assertRaises(ValueError):
                e2e.inspect_inventory(payload)
        with self.assertRaises(ValueError):
            e2e.inspect_inventory(inventory(), patch_hash=e2e.sha256(b'encrypted'))

    def test_rejects_conflicting_or_duplicate_roots_files_and_unbounded_inventory(self):
        mutations = [
            lambda p: p['updater_roots'][1].update(label='files'),
            lambda p: p['updater_roots'][1].update(present=False),
            lambda p: p['updater_roots'][0]['files'].append(copy.deepcopy(p['updater_roots'][0]['files'][0])),
            lambda p: p['updater_roots'][0]['files'].append(entry('pointers.json', parsed={'next_boot_patch': 99})),
            lambda p: p['updater_roots'][0]['files'][0].update(size=268435457),
            lambda p: p['updater_roots'][0]['files'][0].update(prefix_hex='fffff'),
            lambda p: p['updater_roots'][0]['files'][0].pop('json'),
            lambda p: p['provider_files'][0].update(size=32),
            lambda p: p['provider_files'][0].update(path='raw.key'),
            lambda p: p.update(inventory_scope='transient'),
        ]
        for mutation in mutations:
            payload = inventory()
            mutation(payload)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                e2e.inspect_inventory(payload)
        payload = inventory()
        payload['provider_files'] = [dict(payload['provider_files'][0], path=f'{n:064x}.wrapped') for n in range(1025)]
        with self.assertRaises(ValueError):
            e2e.inspect_inventory(payload)

    def test_cached_key_scan_cannot_silently_be_unavailable(self):
        payload = inventory()
        payload['key_scan'] = 'unavailable'
        e2e.inspect_inventory(payload)
        with self.assertRaises(ValueError):
            e2e.inspect_inventory(payload, require_key=True)

    def test_candidate_requires_signature_size_and_exact_integer_pointer(self):
        files = e2e.inspect_inventory(inventory())
        for mutation in (lambda f: f['patches/3/state.json']['json'].update(signature=None),
                         lambda f: f['patches/3/state.json']['json'].update(ciphertext_size=8),
                         lambda f: f['patches/3/state.json']['json'].update(kind='Installed'),
                         lambda f: f['pointers.json']['json'].update(next_boot_patch=True)):
            changed = copy.deepcopy(files)
            mutation(changed)
            with self.assertRaises(ValueError):
                e2e.candidate_evidence(changed, 3)

    def test_sanitized_results_omit_state_client_signatures_and_urls(self):
        payload = inventory()
        payload['updater_roots'][0]['files'][0]['json']['signed_url'] = 'private-url'
        sanitized = e2e.sanitized_inventory(payload)
        encoded = json.dumps(sanitized)
        for private in ('real-client', 'managed-signature', 'private-url', '"json"'):
            self.assertNotIn(private, encoded)
        self.assertEqual(sanitized['inventory_scope'], 'final-files-only')
        self.assertIn('client_id', payload['updater_roots'][0]['files'][0]['json'])


class ToolchainAlignmentTest(unittest.TestCase):
    def staged(self, directory):
        root = Path(directory)
        args = SimpleNamespace(cli=root / 'cli', flutter=root / 'flutter', source_record=None)
        for folder in (args.cli / 'bin/internal', args.flutter / 'bin/internal'):
            folder.mkdir(parents=True)
        (args.cli / 'bin/internal/flutter.version').write_text('a' * 40 + '\n')
        (args.cli / 'bin/internal/engine.version').write_text('b' * 40 + '\n')
        (args.flutter / 'bin/internal/engine.version').write_text('b' * 40 + '\n')
        return args

    def test_exact_git_head_and_engine_pins_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.staged(directory)
            with mock.patch.object(e2e.subprocess, 'run', return_value=SimpleNamespace(stdout=b'a' * 40 + b'\n')) as git:
                e2e.verify_toolchain_alignment(args)
            git.assert_called_once_with(['git', '-C', str(args.flutter), 'rev-parse', '--verify', 'HEAD'],
                stdin=e2e.subprocess.DEVNULL, stdout=e2e.subprocess.PIPE, stderr=e2e.subprocess.DEVNULL,
                check=True, timeout=15)

    def test_cli_flutter_pin_mismatch_rejects_instead_of_relabeling_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.staged(directory)
            with mock.patch.object(e2e.subprocess, 'run', return_value=SimpleNamespace(stdout=b'c' * 40 + b'\n')), \
                    self.assertRaisesRegex(e2e.FixtureError, 'Flutter pin differs'):
                e2e.verify_toolchain_alignment(args)
            self.assertEqual((args.cli / 'bin/internal/flutter.version').read_text(), 'a' * 40 + '\n')
            self.assertFalse((args.cli / 'bin/cache').exists())

    def test_engine_mismatch_fails_before_any_git_or_device_process(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.staged(directory)
            (args.flutter / 'bin/internal/engine.version').write_text('c' * 40)
            with mock.patch.object(e2e.subprocess, 'run') as process, \
                    self.assertRaisesRegex(e2e.FixtureError, 'engine pins differ'):
                e2e.verify_toolchain_alignment(args)
            process.assert_not_called()

    def test_source_record_must_match_the_exact_selected_flutter(self):
        record = {'root_commit': 'd' * 40, 'components': {name: 'e' * 40 for name in
            ('dart', 'flutter', 'shorebird', 'updater', 'shorebird-server')}}
        with tempfile.TemporaryDirectory() as directory:
            args = self.staged(directory)
            args.source_record = Path(directory) / 'sources.json'
            args.source_record.write_text(json.dumps(record))
            with mock.patch.object(e2e.subprocess, 'run', return_value=SimpleNamespace(stdout=b'a' * 40)), \
                    self.assertRaisesRegex(e2e.FixtureError, 'source record differs'):
                e2e.verify_toolchain_alignment(args)
            record['components']['flutter'] = 'a' * 40
            args.source_record.write_text(json.dumps(record))
            with mock.patch.object(e2e.subprocess, 'run', return_value=SimpleNamespace(stdout=b'a' * 40)):
                e2e.verify_toolchain_alignment(args)

    def test_preflight_invokes_alignment_before_lifecycle_or_work_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.staged(directory)
            args.serial = 'emulator-5558'
            for name in ('android_sdk', 'java_home', 'server_source', 'artifact_mirror', 'pub_cache'):
                setattr(args, name, Path(directory) / name)
            args.artifact_mirror.mkdir()
            args.pub_cache.mkdir()
            args.tooling_dart = Path(directory) / 'tooling-dart'
            args.header = Path(directory) / 'header.h'
            args.artifact_proxy = Path(directory) / 'artifact-proxy'
            for path in (args.cli / 'bin/shorebird', args.flutter / 'bin/flutter', args.tooling_dart,
                         args.android_sdk / 'platform-tools/adb', args.java_home / 'bin/java',
                         args.header, args.artifact_proxy, args.server_source / 'go.mod'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with mock.patch.object(e2e.shutil, 'which', return_value='/tool'), \
                    mock.patch.object(e2e, 'verify_toolchain_alignment', side_effect=e2e.FixtureError('mismatch')) as alignment, \
                    mock.patch.object(e2e.Lifecycle, '__init__', side_effect=AssertionError('lifecycle constructed')):
                with self.assertRaises(e2e.FixtureError):
                    e2e.preflight(args)
            alignment.assert_called_once_with(args)


class DriverTest(unittest.TestCase):
    def bare(self):
        runner = object.__new__(e2e.Lifecycle)
        runner.args = mock.Mock(runtime_timeout=1, serial='emulator-5558', android_sdk=Path('/sdk'))
        runner.active = native()
        runner.process_tokens = set()
        runner.reverse_pending = set()
        runner.package = 'dev.shorebird.e2e.a123'
        runner.control = 'sensitive-control'
        runner.api = runner.provision = None
        runner.observer = mock.Mock(base='http://127.0.0.1:32100')
        runner.stop_app = mock.Mock()
        runner.pid = mock.Mock(return_value=42)
        runner.adb = mock.Mock(return_value='')
        runner.stage = mock.Mock()
        return runner

    def test_cli_builds_actual_arm64_apk_and_patches_aab_without_local_engine_flags(self):
        release = e2e.build_command('/cli/bin/shorebird')
        patch = e2e.build_command('/cli/bin/shorebird', patch=True, release_version='1.0.0+1')
        self.assertIn('--artifact=apk', release)
        self.assertIn('release', release)
        self.assertIn('patch', patch)
        self.assertNotIn('--artifact=apk', patch)
        for command in (release, patch):
            self.assertIn('--target-platform=android-arm64', command)
            self.assertFalse(any('local-engine' in str(value) for value in command))
        self.assertIn('--dart-define=LICENSE_TYPE=free', release)
        self.assertIn('--dart-define=LICENSE_TYPE=pro', patch)
        with self.assertRaises(e2e.FixtureError):
            e2e.build_command('/cli', patch=True)

    def test_native_control_preserves_multiline_aad_and_never_logs_intent(self):
        import shlex
        runner = self.bare()
        runner.observer.get.return_value = dict(native(), operation='inspect', payload={})
        with mock.patch.object(e2e.secrets, 'token_hex', return_value='a' * 48):
            runner.control_command('inspect', fresh=False, key_id='id', aad='line1\nline2')
        runner.stop_app.assert_not_called()
        arguments = shlex.split(runner.adb.call_args.args[1])
        self.assertEqual(arguments[arguments.index('aad') + 1], 'line1\nline2')
        self.assertIn('sensitive-control', arguments)
        runner.stage.assert_not_called()

    def test_cold_control_force_stops_and_requires_services_for_online(self):
        runner = self.bare()
        with self.assertRaises(e2e.FixtureError):
            runner.control_command('launch', online=True)
        runner.stop_app.assert_called_once()
        runner.api = mock.Mock()
        with self.assertRaises(e2e.FixtureError):
            runner.control_command('launch', online=False)
        runner.adb.assert_not_called()

    def test_running_inspection_requires_same_process_token(self):
        runner = self.bare()
        runner.observer.get.return_value = dict(native(), operation='inspect', process_token='c' * 32, payload={})
        with mock.patch.object(e2e.secrets, 'token_hex', return_value='a' * 48), self.assertRaises(e2e.FixtureError):
            runner.control_command('inspect', fresh=False)

    def test_existing_reverse_mapping_is_never_rebound(self):
        runner = self.bare()
        runner.reverses = set()
        runner.adb.return_value = 'host-7 tcp:32100 tcp:32100'
        with self.assertRaises(e2e.FixtureError):
            runner.reverse('http://127.0.0.1:32100/native')
        self.assertEqual(runner.adb.call_count, 1)
        self.assertFalse(runner.reverses)

    def test_owned_reverse_uses_no_rebind_and_records_only_success(self):
        runner = self.bare()
        runner.reverses = set()
        runner.reverse('http://127.0.0.1:32100/native')
        self.assertEqual(runner.adb.call_args, mock.call('reverse', '--no-rebind', 'tcp:32100', 'tcp:32100'))
        self.assertEqual(runner.reverses, {'tcp:32100'})

    def test_owned_reverse_can_be_reused_without_rebinding_after_server_restart(self):
        runner = self.bare()
        runner.reverses = {'tcp:32100'}
        runner.adb.return_value = 'host-7 tcp:32100 tcp:32100'
        runner.reverse('http://127.0.0.1:32100/key')
        runner.adb.assert_called_once_with('reverse', '--list')

    def test_cold_control_rejects_reused_process_token(self):
        runner = self.bare()
        runner.process_tokens.add('b' * 32)
        runner.observer.get.return_value = native()
        with mock.patch.object(e2e.secrets, 'token_hex', return_value='a' * 48), self.assertRaises(e2e.FixtureError):
            runner.control_command('launch')
        runner.stop_app.assert_called_once()

    def test_lifecycle_keeps_base_apk_through_offline_failures_and_signed_rollback(self):
        runner = self.bare()
        runner.manifest = {'result': 'failed'}
        runner.app_id = context()['app_id']
        runner.release_id, runner.release_version = 17, '1.0.0+1'
        runner.patch_id, runner.number = 21, 3
        runner.patch_hash, runner.transport_hash = 'a' * 64, 'b' * 64
        runner.apk_hash = 'f' * 64
        runner.authority = e2e.KeyAuthority(b'secret', 'token')
        for name in ('prepare', 'start_api', 'bootstrap', 'start_provision', 'build', 'probe_context',
                     'go_offline', 'assert_apk'):
            setattr(runner, name, mock.Mock())
        runner.boot_confirmed = mock.Mock(return_value=True)
        runner.rows = mock.Mock(return_value=[('aarch64', 'android', runner.release_version)])
        runner.candidate = mock.Mock(return_value={'transport_sha256': e2e.sha256(b'encrypted')})
        runner.inspect = mock.Mock(side_effect=[
            ({'state.json': {'json': {'client_id': 'real-client'}}}, inventory()),
            ({}, inventory()),
            ({'pointers.json': {'json': {'next_boot_patch': None, 'last_booted_patch': None,
                                       'currently_booting_patch': None}}}, inventory()),
            ({}, inventory())])
        calls = []
        def launch(label, phase, number, **kwargs):
            calls.append((label, phase, number, kwargs))
            if label == 'online-BASE-download':
                runner.authority.accepted += 1
        runner.launch = mock.Mock(side_effect=launch)
        runner.request = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            runner.work = Path(directory)
            (runner.work / 'objects/patches').mkdir(parents=True)
            (runner.work / 'objects/patches/managed-ciphertext').write_bytes(b'encrypted')
            runner.execute()
        self.assertEqual(runner.manifest['result'], 'passed')
        self.assertEqual(runner.build.call_args_list, [mock.call(), mock.call(patch=True)])
        self.assertEqual([(p, n) for _, p, n, _ in calls], [
            ('BASE', 0), ('BASE', 0), ('U', 3), ('BASE', 0), ('U', 3),
            ('BASE', 0), ('U', 3), ('U', 3), ('BASE', 0)])
        self.assertEqual(calls[3][3], {'mode': 'missing'})
        self.assertEqual(calls[5][3], {'mode': 'wrong'})
        self.assertEqual(runner.go_offline.call_count, 2)
        runner.request.assert_called_once_with('POST',
            f'/api/v1/apps/{runner.app_id}/releases/17/patches/21/rollback')
        runner.assert_apk.assert_called_once()
        self.assertEqual(e2e.parse_aad(runner.authority.allowed_aad)['arch'], 'aarch64')

    def test_uncertain_creation_claims_only_exact_pair_before_cleanup(self):
        runner = self.bare()
        runner.reverses = set()
        mapping = 'host-7 tcp:32100 tcp:32100'
        runner.adb.side_effect = ['', TimeoutError('private transport detail'), mapping, mapping, '']
        runner.reverse('http://127.0.0.1:32100/native')
        self.assertEqual(runner.reverses, {'tcp:32100'})
        self.assertFalse(runner.reverse_pending)
        runner.remove_reverse('tcp:32100')
        self.assertFalse(runner.reverses)
        self.assertEqual(runner.adb.call_args_list, [mock.call('reverse', '--list'),
            mock.call('reverse', '--no-rebind', 'tcp:32100', 'tcp:32100'),
            mock.call('reverse', '--list'), mock.call('reverse', '--list'),
            mock.call('reverse', '--remove', 'tcp:32100')])

    def test_uncertain_creation_without_entry_fails_without_removal_ownership(self):
        runner = self.bare()
        runner.reverses = set()
        runner.adb.side_effect = ['', TimeoutError(), '']
        with self.assertRaises(e2e.FixtureError):
            runner.reverse('http://127.0.0.1:32100/native')
        self.assertFalse(runner.reverses)
        self.assertFalse(runner.reverse_pending)
        self.assertFalse(any('--remove' in call.args for call in runner.adb.call_args_list))
        runner.adb.reset_mock(side_effect=True)
        with self.assertRaises(e2e.FixtureError):
            runner.remove_reverse('tcp:32100')
        runner.adb.assert_not_called()

    def test_uncertain_creation_with_changed_destination_is_never_removed(self):
        runner = self.bare()
        runner.reverses = set()
        runner.adb.side_effect = ['', TimeoutError(), 'host-7 tcp:32100 tcp:9999']
        with self.assertRaises(e2e.FixtureError):
            runner.reverse('http://127.0.0.1:32100/native')
        self.assertFalse(runner.reverses)
        self.assertFalse(runner.reverse_pending)
        self.assertFalse(any('--remove' in call.args for call in runner.adb.call_args_list))

    def test_uncertain_creation_failed_query_remains_pending_for_later_reconciliation(self):
        runner = self.bare()
        runner.reverses = set()
        runner.adb.side_effect = ['', TimeoutError(), TimeoutError()]
        with self.assertRaises(e2e.FixtureError):
            runner.reverse('http://127.0.0.1:32100/native')
        self.assertFalse(runner.reverses)
        self.assertEqual(runner.reverse_pending, {'tcp:32100'})
        runner.adb.side_effect = ['host-7 tcp:32100 tcp:32100', '']
        runner.remove_reverse('tcp:32100')
        self.assertFalse(runner.reverse_pending)
        self.assertEqual(runner.adb.call_args, mock.call('reverse', '--remove', 'tcp:32100'))

    def test_cleanup_leaves_externally_rebound_mapping_and_reports_failure(self):
        runner = self.bare()
        runner.installed = False
        runner.reverses = {'tcp:32100'}
        runner.adb.return_value = 'host-7 tcp:32100 tcp:9999'
        runner.job = mock.Mock()
        runner.provision = runner.observer = runner.mirror = None
        runner.logs = []
        runner.work_owned = False
        with self.assertRaises(e2e.FixtureError):
            runner.cleanup()
        runner.adb.assert_called_once_with('reverse', '--list')
        runner.job.close.assert_called_once()
        self.assertFalse(runner.reverses)

    def test_cleanup_absent_mapping_is_a_noop_and_malformed_inventory_fails_closed(self):
        runner = self.bare()
        runner.reverses = {'tcp:32100'}
        runner.remove_reverse('tcp:32100')
        runner.adb.assert_called_once_with('reverse', '--list')
        self.assertFalse(runner.reverses)
        runner.reverses.add('tcp:32100')
        runner.adb.reset_mock(return_value=True)
        runner.adb.return_value = 'malformed inventory'
        with self.assertRaises(e2e.FixtureError):
            runner.remove_reverse('tcp:32100')
        runner.adb.assert_called_once_with('reverse', '--list')

    def test_release_native_archive_requires_engine_jni_and_aot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'app.apk'
            with zipfile.ZipFile(path, 'w') as archive:
                for name in ('libapp.so', 'libflutter.so', 'libshorebird_e2e_fixture.so'):
                    archive.writestr('lib/arm64-v8a/' + name, name.encode())
                archive.writestr('lib/x86_64/libapp.so', b'other architecture')
            result = e2e.archive_native_entries(path)
            self.assertEqual(len(result), 3)
            self.assertEqual(result['libapp.so'], e2e.sha256(b'libapp.so'))
            with self.assertRaises(e2e.FixtureError):
                e2e.archive_native_entries(path, aab=True)

    def test_plan_is_read_only_and_does_not_construct_windows_or_host_owner(self):
        with mock.patch.object(e2e.portable.Lifecycle, '__init__', side_effect=AssertionError('Windows constructor')), \
                mock.patch.object(e2e.subprocess, 'Popen', side_effect=AssertionError('process')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(e2e.main(['plan']), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['read_only'])
        self.assertEqual(plan['schema'], e2e.SCHEMA)
        self.assertFalse(plan['transient_write_trace'])

    def test_environment_isolates_gradle_home_and_removes_proxies_secrets(self):
        runner = self.bare()
        runner.work = Path('/private/run')
        runner.args.java_home = Path('/java')
        runner.api_url = 'http://127.0.0.1:1234'
        runner.token = None
        runner.mirror = mock.Mock(url='http://127.0.0.1:2345')
        runner.proxy_url = None
        with mock.patch.dict(e2e.os.environ, {'SHOREBIRD_TOKEN': 'ambient', 'HTTPS_PROXY': 'private',
                'FLUTTER_ROOT': '/source', 'GRADLE_USER_HOME': '/shared', 'PATH': '/bin'}, clear=True):
            env = runner.environment()
        self.assertNotIn('SHOREBIRD_TOKEN', env)
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertEqual(env['GRADLE_USER_HOME'], '/private/run/gradle')
        self.assertEqual(env['HOME'], '/private/run/home')
        self.assertEqual(env['GRADLE_OPTS'], '-Dorg.gradle.daemon=false')
        self.assertEqual(env['SHOREBIRD_DOWNLOAD_URL'], 'http://127.0.0.1:2345')

    def test_mirror_tool_urls_and_flutter_proxy_urls_are_separate(self):
        runner = self.bare()
        runner.work = Path('/private/run')
        runner.args.java_home = Path('/java')
        runner.api_url = 'http://127.0.0.1:1234'
        runner.token = None
        runner.mirror = mock.Mock(url='http://127.0.0.1:2345')
        runner.proxy_url = 'http://127.0.0.1:3456'
        env = runner.environment()
        self.assertEqual(env['SHOREBIRD_ARTIFACT_BASE_URL'], runner.mirror.url)
        self.assertEqual(env['SHOREBIRD_DOWNLOAD_URL'], runner.proxy_url)
        self.assertEqual(env['FLUTTER_STORAGE_BASE_URL'], runner.proxy_url)

    def test_packaged_source_record_is_whitelisted_and_hashes_exact_bytes(self):
        record = {'root_commit': 'a' * 40, 'components': {name: 'b' * 40 for name in
            ('dart', 'flutter', 'shorebird', 'updater', 'shorebird-server')},
            'ci_run_id': 123, 'artifact_manifest_sha256': 'c' * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sources.json'
            raw = json.dumps(record).encode()
            path.write_bytes(raw)
            result = e2e.source_record(path)
            self.assertEqual(result['source_record_sha256'], e2e.sha256(raw))
            self.assertEqual(result['components'], record['components'])
            for bad in (dict(record, credential='sensitive'), dict(record, root_commit='main'),
                        dict(record, ci_run_id=True), dict(record, artifact_manifest_sha256='bad'),
                        dict(record, components={'flutter': 'b' * 40})):
                path.write_text(json.dumps(bad))
                with self.subTest(fields=set(bad)), self.assertRaises(e2e.FixtureError):
                    e2e.source_record(path)

    def test_cleanup_uninstalls_only_owned_package_and_removes_only_owned_mappings(self):
        runner = self.bare()
        runner.installed = True
        runner.reverses = {'tcp:32100'}
        runner.job = mock.Mock()
        runner.provision = runner.observer = runner.mirror = None
        runner.logs = []
        runner.work_owned = False
        runner.adb.side_effect = ['', 'host-7 tcp:32100 tcp:32100', '']
        runner.cleanup()
        self.assertEqual(runner.adb.call_args_list, [mock.call('uninstall', runner.package),
            mock.call('reverse', '--list'), mock.call('reverse', '--remove', 'tcp:32100')])
        runner.job.close.assert_called_once()
        self.assertFalse(any('kill-server' in str(call) or 'emu' in str(call) for call in runner.adb.call_args_list))


if __name__ == '__main__':
    unittest.main()

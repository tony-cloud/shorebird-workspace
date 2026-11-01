"""Scoped adversarial fixture tests. No server or device is started."""
import base64
import copy
import hashlib
import io
import json
import os
import plistlib
import time
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import ios_variant_e2e as runner
import test_ios_variant_e2e as baseline


class AdversarialTest(unittest.TestCase):
    def setUp(self):
        self.fixture = baseline.IosVariantFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.work, self.state = self.fixture.work, self.fixture.state

    def cache(self, artifact='b' * 64, signature='c' * 64):
        return dict(schema='ios-variant-cache-v1', files={
            'patches/2/dlc.vmcode': dict(size=128, sha256=artifact),
            'patches/2/state.json': dict(size=96, sha256=signature, kind='Installed'),
            'selections-v2/' + 'f' * 64 + '.json': dict(size=512, sha256='d' * 64,
                version=2, epoch='1', digest='a' * 64, time_floor=10)})

    def paid(self):
        self.state['assignments'] = [self.fixture.assignment('pro')]
        self.state['installation_id'] = 'real-installation'
        self.state['installed_app_sha256'] = '1' * 64
        self.state['previous_cache'] = self.cache()
        runner.save(self.work, self.state)

    def tampered_report(self, launch):
        self.fixture.nonce = launch['nonce']
        report = self.fixture.report('free', '1', 'a' * 64)
        report['selection']['accepted'] = self.fixture.identity('pro', '1', 'a' * 64)
        report['selection']['next_boot'] = dict(variant_id='free', reason='content_unavailable')
        report['cache_before'] = self.cache()
        report['cache_after_control'] = self.cache(artifact='e' * 64)
        report['cache_after'] = self.cache(artifact='e' * 64)
        report['cache_control'] = dict(schema='ios-variant-cache-control-v1', session=self.state['session'],
            bundle_id=self.state['bundle_id'], nonce=launch['nonce'], pid=report['pid'], time=report['native_boot']['time'],
            action='tamper-ciphertext', result='mutated', path='patches/2/dlc.vmcode', patch_number='2',
            expected_sha256='b' * 64, before_sha256='b' * 64, after_sha256='e' * 64, changed_bytes=1, size=128)
        return report

    def test_native_cache_mutation_fence_survives_lost_command_and_rejects_incomplete_receipt(self):
        self.paid()
        with mock.patch.object(runner.ios, 'run', side_effect=RuntimeError('lost command reply')):
            with self.assertRaises(RuntimeError): runner.launch(self.work, self.state,
                cache_action='tamper-ciphertext', cache_patch='2', cache_sha256='b' * 64)
        launch = runner.load(self.work)['pending_cache_launch']
        self.assertEqual(launch['cache_patch'], '2')
        with mock.patch.object(runner.ios, 'run') as execute, mock.patch.object(runner.secrets, 'token_hex') as nonce:
            with self.assertRaisesRegex(ValueError, 'cache-control launch unresolved'):
                runner.launch(self.work, self.state, action='delete', variant='pro')
            execute.assert_not_called()
            nonce.assert_not_called()
        report = self.tampered_report(launch)
        path = self.work / 'tamper-report.json'
        for change in ('foreign-path', 'wrong-before', 'wrong-nonce', 'journal-changed', 'ffi', 'not-cold'):
            value = copy.deepcopy(report)
            if change == 'foreign-path': value['cache_control']['path'] = 'state.json'
            elif change == 'wrong-before': value['cache_control']['before_sha256'] = '0' * 64
            elif change == 'wrong-nonce': value['cache_control']['nonce'] = '0' * 48
            elif change == 'journal-changed': value['cache_after']['files']['selections-v2/' + 'f' * 64 + '.json']['sha256'] = '0' * 64
            elif change == 'ffi': value['ffi']['callbacks_closed'] = False
            runner.ios.write_json(path, value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.read_report(self.work, self.state, variant='free', path=path,
                    cold=change != 'not-cold', fallback=change != 'not-cold')
            self.assertIn('pending_cache_launch', runner.load(self.work))
        runner.ios.write_json(path, report)
        runner.read_report(self.work, self.state, variant='free', path=path, cold=True, fallback=True)
        self.assertNotIn('pending_cache_launch', runner.load(self.work))
        self.assertEqual(self.state['previous_cache'], report['cache_after'])

    def test_mutation_inputs_are_fixed_current_paid_artifact_and_last_observed_hash(self):
        self.paid()
        for options in (dict(cache_patch='1'), dict(cache_patch='3'), dict(cache_sha256='0' * 64),
                dict(action='delete', variant='pro'), dict(check=True), dict(cache_action='restore')):
            arguments = dict(cache_action='tamper-ciphertext', cache_patch='2', cache_sha256='b' * 64)
            arguments.update(options)
            with self.subTest(options=options), mock.patch.object(runner.ios, 'run') as execute:
                with self.assertRaises(ValueError): runner.launch(self.work, self.state, **arguments)
                execute.assert_not_called()
        env, _ = runner.launch_environment(self.work, self.state, cache_action='tamper-signature', cache_patch='2', cache_sha256='c' * 64)
        self.assertEqual(env['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_CACHE_PATCH'], '2')
        self.assertFalse(any('KEY_HEX' in key for key in env))

    def test_rejected_old_response_and_real_expiry_keep_authority_and_journal(self):
        self.paid()
        expiry = str(int(self.state['created']) - 1)
        self.state['assignments'][0]['selection']['offline_valid_until'] = expiry
        launch = dict(nonce=self.fixture.nonce, after=self.state['created'], check=True, rejected_check=True,
            action=None, report_route='native', status_route='public')
        self.state['launches'] = [launch]
        runner.save(self.work, self.state)
        report = self.tampered_report(launch)
        report['cache_control'] = None
        report['cache_after_control'] = report['cache_after'] = copy.deepcopy(report['cache_before'])
        report.update(stage='checked', update_status='upToDate')
        report['selection']['next_boot']['reason'] = 'expired'
        path = self.work / 'rejection.json'
        runner.ios.write_json(path, report)
        # Ordinary checked acceptance deliberately rejects this expired-paid
        # combination; explicit rejection mode requires unchanged authority.
        with self.assertRaises(ValueError): runner.read_report(self.work, self.state, variant='free', path=path,
            checked=True, cold=True, fallback=True)
        with self.assertRaisesRegex(ValueError, 'replay-bound'):
            runner.read_report(self.work, self.state, variant='free', path=path, checked=True, rejected_check=True,
                cold=True, fallback=True, expired_after=expiry)
        launch['replay'] = dict(id='1' * 32, started=launch['after'] - 1,
            capture_sha256='c' * 64, accepted=self.fixture.identity('pro', '1', 'a' * 64))
        runner.save(self.work, self.state)
        receipt = dict(schema='ios-replay-delivery-v1', session=self.state['session'], replay_id='1' * 32,
            launch_nonce=launch['nonce'], request_nonce='real-request', accepted=launch['replay']['accepted'],
            capture_sha256='c' * 64, response_bytes=100, completed=True, started=report['started'], time=report['started'])
        delivery = self.work / 'replay-deliveries' / (launch['nonce'] + '.json')
        with self.assertRaisesRegex(ValueError, 'No completed authentic replay'):
            runner.read_report(self.work, self.state, variant='free', path=path, checked=True, rejected_check=True,
                cold=True, fallback=True, expired_after=expiry)
        for change in ('nonce', 'replay', 'capture', 'incomplete', 'time', 'authority'):
            altered = copy.deepcopy(receipt)
            if change == 'nonce': altered['launch_nonce'] = '0' * 48
            elif change == 'replay': altered['replay_id'] = '0' * 32
            elif change == 'capture': altered['capture_sha256'] = '0' * 64
            elif change == 'incomplete': altered['completed'] = False
            elif change == 'time': altered['started'] = report['started'] - 1
            else: altered['accepted']['epoch'] = '0'
            runner.ios.write_json(delivery, altered)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'fresh launch/check'):
                runner.read_report(self.work, self.state, variant='free', path=path, checked=True, rejected_check=True,
                    cold=True, fallback=True, expired_after=expiry)
            self.assertNotIn('validated_nonce', self.state)
        runner.ios.write_json(delivery, receipt)
        runner.read_report(self.work, self.state, variant='free', path=path, checked=True, rejected_check=True,
            cold=True, fallback=True, expired_after=expiry)
        for change in ('time', 'deadline', 'reason'):
            value = copy.deepcopy(report)
            deadline = expiry
            if change == 'time': value['native_boot']['time'] = int(expiry)
            elif change == 'deadline': deadline = str(int(expiry) - 1)
            else: value['selection']['next_boot']['reason'] = 'quarantined'
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.adversarial.validate_expiry(self.state, value, deadline)

    def test_observer_only_never_starts_api_or_touches_secrets(self):
        with mock.patch.object(runner.selection.v1, 'port_open', return_value=False), \
                mock.patch.object(runner, 'report_observer') as factory, mock.patch.object(runner, 'OwnedHostProcesses') as processes:
            server = factory.return_value
            server.serve_forever.side_effect = KeyboardInterrupt
            runner.observe(self.work, self.state)
            server.server_close.assert_called_once()
            processes.assert_not_called()

    def test_capture_is_real_ios_nonce_bound_and_body_is_private(self):
        self.paid()
        def request(state, method, path, body):
            self.assertEqual((method, path, body['platform'], body['arch']), ('POST', '/api/v1/patches/check', 'ios', 'aarch64'))
            self.assertEqual(body['client_id'], 'real-installation')
            value = self.fixture.assignment()
            context = {k: v for k, v in body.items() if k not in ('client_id', 'selection_protocol')}
            context['client_id_hash'] = hashlib.sha256(b'real-installation').hexdigest()
            value['authentication'] = dict(signature='real-server-test-placeholder', payload=base64.b64encode(json.dumps(
                dict(selection_digest='a' * 64, request=context, selection=value['selection'])).encode()).decode())
            value['delivery'] = {'url': 'private-test-url-not-a-log'}
            return value
        with mock.patch.object(runner.ios, 'request', side_effect=request):
            capture = runner.adversarial.capture_response(runner, self.work, self.state)
        path = Path(capture['path'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), capture['sha256'])
        self.assertNotIn('private-test-url', json.dumps(capture))
        self.assertNotIn('authentication', capture)

    def test_rollback_lost_response_reconciles_via_admin_query_without_mutation_replay(self):
        self.paid()
        patch = dict(patch_number=2, patch_id=17, is_rolled_back=False)
        calls = []
        def api(work, state, method, suffix, body=None):
            if suffix == '/releases': return {'releases': [{'id': 9, 'version': state['release_version'], 'selection_protocol': 2}]}
            if method == 'GET': return {'patches': [copy.deepcopy(patch)]}
            calls.append(suffix)
            patch['is_rolled_back'] = True
            raise OSError('lost response')
        with mock.patch.object(runner, 'api', side_effect=api):
            with self.assertRaises(OSError): runner.adversarial.patch_control(runner, self.work, self.state, 'pro', 'rollback')
            self.assertIn('pending_patch_control', runner.load(self.work))
            with self.assertRaises(ValueError): runner.adversarial.patch_control(runner, self.work, self.state, 'pro', 'rollforward')
            receipt = runner.adversarial.patch_control(runner, self.work, self.state, 'pro', 'rollback')
        self.assertTrue(receipt['is_rolled_back'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('pending_patch_control', runner.load(self.work))

    def test_replay_serves_exact_older_signed_body_only_to_newer_owned_request(self):
        self.paid()
        private = self.work / 'private-response-captures' / 'capture.json'
        body = b'{"authentication":{"signature":"old-real-capture-placeholder"}}'
        runner.ios.private_write(private, body)
        capture = dict(path=str(private), session=self.state['session'], sha256=hashlib.sha256(body).hexdigest(),
            accepted=self.fixture.identity('pro', '1', 'a' * 64), nonce='old-server-request-nonce')
        self.state['assignments'].append(self.fixture.assignment('pro', '2', 'e' * 64))
        runner.save(self.work, self.state)
        handler_type = []
        server = mock.Mock()
        def server_factory(address, handler):
            self.assertEqual(address[0], self.state['bind_address'])
            handler_type.append(handler)
            return server
        with mock.patch.object(runner.selection.v1, 'port_open', return_value=False), \
                mock.patch.object(runner.adversarial, 'ThreadingHTTPServer', side_effect=server_factory), \
                mock.patch.object(runner.adversarial.threading, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            with runner.adversarial.replay_endpoint(runner, self.work, self.state, capture) as requests:
                with mock.patch.object(runner.ios, 'run', return_value=b''):
                    launch = runner.launch(self.work, self.state, check=True, rejected_check=True)
                for mode in ('foreign', 'disconnect', 'short', 'flush-failed', 'delivered'):
                    foreign = mode == 'foreign'
                    request = dict(app_id=self.state['app_id'], release_version=self.state['release_version'],
                        platform='ios', arch='aarch64', client_id='real-installation', nonce='fresh-request-nonce',
                        selection_protocol=2, channel='stable',
                        accepted_selection_epoch='1' if foreign else '2', accepted_selection_digest='e' * 64)
                    request.update({k:self.state['release_scope'][k] for k in ('base_artifact_hash','host_abi_hash','runtime_profile')})
                    raw = json.dumps(request).encode()
                    handler = object.__new__(handler_type[0])
                    handler.connection = mock.Mock()
                    handler.headers = {'Content-Length': str(len(raw))}
                    handler.path = '/api/v1/patches/check'
                    handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
                    if mode != 'delivered':
                        handler.wfile = mock.Mock()
                        if mode == 'short': handler.wfile.write.return_value = len(body) - 1
                        elif mode == 'flush-failed':
                            handler.wfile.write.return_value = len(body)
                            handler.wfile.flush.side_effect = BrokenPipeError()
                        else: handler.wfile.write.side_effect = BrokenPipeError()
                    handler.send_error = mock.Mock()
                    handler.send_response = mock.Mock()
                    handler.send_header = mock.Mock()
                    handler.end_headers = mock.Mock()
                    handler.do_POST()
                    if foreign:
                        handler.send_error.assert_called_once_with(400)
                    elif mode == 'delivered':
                        self.assertEqual(handler.wfile.getvalue(), body)
                    else:
                        self.assertFalse((self.work / 'replay-deliveries' / (launch['nonce'] + '.json')).exists())
                        self.assertEqual(requests, [])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['launch_nonce'], launch['nonce'])
                self.assertEqual(requests[0]['capture_sha256'], capture['sha256'])
                self.assertTrue(requests[0]['completed'])
                self.assertTrue((self.work / 'replay-deliveries' / (launch['nonce'] + '.json')).is_file())
        server.server_close.assert_called_once()
        self.assertNotIn('authentication', (self.work / 'replay-requests.json').read_text())

    def test_corrupted_cache_crash_can_delete_keys_and_uninstall_without_resolving_or_replaying_test(self):
        self.paid()
        pending = dict(nonce='9' * 48, action='tamper-ciphertext', unknown_outcome=True)
        self.state['pending_cache_launch'] = copy.deepcopy(pending)
        self.state['pending_key_launch'] = dict(nonce='8' * 48, action='provision', unknown_outcome=True)
        runner.save(self.work, self.state)
        launches = []
        def native_launch(command, **kwargs):
            saved = runner.load(self.work)
            self.assertEqual(saved['pending_cache_launch'], pending)
            self.assertIn('pending_key_launch', saved)
            self.assertIn('pending_cleanup_launch', saved)
            environment = kwargs['env']
            self.assertFalse(any(key.endswith(('KEY_ACTION', 'KEY_HEX', 'CACHE_ACTION', 'CACHE_SHA256')) for key in environment))
            launches.append(environment)
            if len(launches) == 1:
                self.assertEqual(environment['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_CLEANUP_KEYS_ONLY'], '1')
                raise RuntimeError('lost cleanup launch response')
            self.assertEqual(environment['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_CLEANUP_RECEIPT_ONLY'], '1')
            self.assertNotIn('DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_CLEANUP_KEYS_ONLY', environment)
            return b''
        with mock.patch.object(runner.ios, 'run', side_effect=native_launch):
            with self.assertRaises(RuntimeError): runner.cleanup_keys(self.work, self.state)
            original = self.state['pending_cleanup_launch']
            with self.assertRaisesRegex(ValueError, 'outcome uncertain'): runner.cleanup_keys(self.work, self.state)
            runner.cleanup_keys(self.work, self.state, resend=True)
        self.assertEqual(launches[0]['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_NONCE'], launches[1]['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_NONCE'])
        with self.assertRaises(ValueError): runner.launch(self.work, self.state, action='delete', variant='pro')
        receipt = dict(schema='ios-variant-native-cleanup-v1', session=self.state['session'], nonce=original['nonce'],
            bundle_id=self.state['bundle_id'], service=self.state['key_service'], pid=99, time=__import__('time').time(),
            mode='native-keychain-cleanup-only', keys={v:dict(account=k, result='deleted') for v,k in self.state['key_ids'].items()})
        for change in ('foreign-account', 'one-key', 'foreign-nonce'):
            altered = copy.deepcopy(receipt)
            if change == 'foreign-account': altered['keys']['pro']['account'] = 'foreign'
            elif change == 'one-key': altered['keys'].pop('enterprise')
            else: altered['nonce'] = '0' * 48
            with self.subTest(change=change), self.assertRaises(ValueError): runner.adversarial.validate_cleanup_receipt(self.state, altered)
        runner.receive_report(self.work, json.dumps(receipt).encode())
        runner.read_cleanup(self.work, self.state)
        self.assertNotIn('observed_selection', self.state)
        self.assertEqual(self.state['pending_cache_launch'], pending)
        self.assertIn('pending_key_launch', self.state)
        with mock.patch.object(runner.selection.v1, 'port_open', return_value=False), mock.patch.object(runner.ios, 'run', return_value=b'') as execute:
            result = runner.cleanup(self.work, self.state)
        self.assertEqual(execute.call_args.args[0][-1], self.state['bundle_id'])
        self.assertTrue(result['all_variant_keys_deleted'])
        self.assertTrue(result['unresolved_runtime_controls_preserved'])
        self.assertEqual(self.state['pending_cache_launch'], pending)
        self.assertFalse((self.work / 'secrets.json').exists())

    def test_native_cleanup_branch_precedes_all_flutter_construction_and_restored_scene_bootstrap(self):
        source = (self.work / 'app/ios/Runner/AppDelegate.swift').read_text()
        branch = source.index('if env["OPEN_SHOREBIRD_TEST_CLEANUP_KEYS_ONLY"]')
        self.assertLess(branch, source.index('cacheBefore = try IosVariantCache.snapshot'))
        self.assertLess(source.index('cleanupReceipt = receipt'), source.index('setShorebirdAotPatchKeyProviderV2'))
        app = source[source.index('@main'):source.index('@objc class IosVariantSceneDelegate:')]
        self.assertIn('class AppDelegate: UIResponder, UIApplicationDelegate', app)
        self.assertNotIn('class AppDelegate: FlutterAppDelegate', app)
        self.assertLess(app.index('IosVariantFixture.beforeEngine()'), app.index('flutterDelegate = FlutterAppDelegate()'))
        self.assertIn('if !IosVariantFixture.cleanupOnly { flutterDelegate = FlutterAppDelegate() }', app)
        self.assertIn('willFinishLaunchingWithOptions', app)
        self.assertNotIn('super.application', app)
        self.assertIn('configuration.delegateClass = IosVariantSceneDelegate.self', app)
        self.assertIn('configuration.storyboard = nil', app)
        scene = source[source.index('@objc class IosVariantSceneDelegate:'):]
        self.assertIn('class IosVariantSceneDelegate: UIResponder, UIWindowSceneDelegate', scene)
        self.assertLess(scene.index('if IosVariantFixture.cleanupOnly {'), scene.index('let delegate = FlutterSceneDelegate()'))
        self.assertLess(scene.index('return\n    }'), scene.index('UIStoryboard(name: "Main"'))
        info = plistlib.loads((self.work / 'app/ios/Runner/Info.plist').read_bytes())
        self.assertNotIn('UIMainStoryboardFile', info)
        runner.validate_scene_bootstrap(info)
        for location in ('root-storyboard', 'scene-storyboard', 'flutter-delegate', 'extra-scene'):
            unsafe = copy.deepcopy(info)
            entries = unsafe['UIApplicationSceneManifest']['UISceneConfigurations']['UIWindowSceneSessionRoleApplication']
            if location == 'root-storyboard': unsafe['UIMainStoryboardFile'] = 'Main'
            elif location == 'scene-storyboard': entries[0]['UISceneStoryboardFile'] = 'Main'
            elif location == 'flutter-delegate': entries[0]['UISceneDelegateClassName'] = 'Runner.SceneDelegate'
            else: entries.append(copy.deepcopy(entries[0]))
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, 'UIKit bootstrap'):
                runner.validate_scene_bootstrap(unsafe)
        configurations = info['UIApplicationSceneManifest']['UISceneConfigurations']['UIWindowSceneSessionRoleApplication']
        self.assertEqual(len(configurations), 1)
        self.assertEqual(configurations[0]['UISceneDelegateClassName'], '$(PRODUCT_MODULE_NAME).IosVariantSceneDelegate')
        self.assertNotIn('UISceneStoryboardFile', configurations[0])
        self.assertEqual((self.work / 'app/ios/Runner/SceneDelegate.swift').read_text(),
            'import UIKit\nclass SceneDelegate: IosVariantSceneDelegate {}\n')

    def test_rejected_check_cannot_launch_without_armed_replay_endpoint(self):
        self.paid()
        with mock.patch.object(runner.ios, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'Start owned replay endpoint'):
                runner.launch(self.work, self.state, check=True, rejected_check=True)
            execute.assert_not_called()
        self.assertEqual(self.state['launches'], [])

    def test_diagnostic_records_cannot_accept_reports_or_clear_native_fences(self):
        self.paid()
        with mock.patch.object(runner.ios, 'run', return_value=b''):
            launch = runner.launch(self.work, self.state, cache_action='inspect')
        diagnostic = dict(schema='ios-variant-diagnostic-v1', session=self.state['session'],
            nonce=launch['nonce'], pid=12, stage='function-complete', time=time.time())
        with self.assertRaisesRegex(ValueError, 'schema'):
            runner.receive_report(self.work, json.dumps(diagnostic).encode())
        path = self.work / 'diagnostic-only.json'
        runner.ios.write_json(path, diagnostic)
        with self.assertRaisesRegex(ValueError, 'foreign nonce-bound'):
            runner.read_report(self.work, self.state, variant='pro', path=path)
        self.assertEqual(runner.load(self.work)['pending_cache_launch'], launch)
        self.assertNotIn('validated_nonce', self.state)
        native = (self.work / 'app/ios/Runner/AppDelegate.swift').read_text()
        start = native.index('@_cdecl("IosVariantDiagnosticStage")')
        end = native.index('@_cdecl("IosVariantSubmitReport")')
        self.assertNotIn('URLSession', native[start:end])
        helper = (runner.TEMPLATES / 'Diagnostics.swift.template').read_text()
        self.assertNotIn('URLSession', helper)
        self.assertIn('handle.synchronize()', helper)
        self.assertIn('guard !IosVariantFixture.cleanupOnly else { return }', native[start:end])
        project = (self.work / 'app/ios/Runner.xcodeproj/project.pbxproj').read_text()
        self.assertEqual(project.count('-Wl,-u,_IosVariantDiagnosticStage'), 3)

    def test_inspection_requires_the_retained_diagnostic_export(self):
        app = self.work / 'fake-original.app'
        app.mkdir()
        info = plistlib.loads((self.work / 'app/ios/Runner/Info.plist').read_bytes())
        info['CFBundleExecutable'] = 'Runner'
        (app / 'Info.plist').write_bytes(plistlib.dumps(info))
        (app / 'Runner').write_bytes(b'not an executable; mocked inspection only')
        export_log = self.work / 'ffi-native-exports.log'
        export_log.write_text('_IosVariantSubmitReport\n')
        with mock.patch.object(runner.ios, 'verified_app', return_value=app), mock.patch.object(runner, 'validate_compiled_scope'):
            with self.assertRaisesRegex(ValueError, 'IosVariantDiagnosticStage'):
                runner.inspect_app(self.work, self.state, app)
            export_log.write_text('_IosVariantSubmitReport\n_IosVariantDiagnosticStage\n')
            inspected, digest = runner.inspect_app(self.work, self.state, app)
        self.assertEqual(inspected, app)
        self.assertEqual(digest, hashlib.sha256((app / 'Runner').read_bytes()).hexdigest())

    def test_diagnostics_surround_normal_constructor_and_status_without_switching_routes(self):
        source = (self.work / 'app/lib/variant_status.dart').read_text()
        for before, operation, after in ((2, 'final updater = ShorebirdUpdater();', 3),
                (4, 'final installation = nativeStatus ?', 5),
                (6, 'final initial = nativeStatus ?', 7), (8, 'final data = updateFixtureData();', 9)):
            with self.subTest(operation=operation):
                self.assertLess(source.index(f'_nativeDiagnosticStage({before}, flags);'), source.index(operation))
                self.assertLess(source.index(operation), source.index(f'_nativeDiagnosticStage({after}, flags'))
        self.assertIn('Timer.periodic', source)
        self.assertIn('heartbeat?.cancel()', source)
        self.assertIn("symbol: 'IosVariantDiagnosticStage'", source)
        self.assertIn('on Object catch (error, stack)', source)
        self.assertIn('variantDiagnosticErrorDetails(error, stack)', source)
        self.assertNotIn('error.toString()', source.replace('// Never include error.toString()', ''))


@unittest.skipUnless(shutil.which('xcrun'), 'Native helper tests require the installed Apple Swift toolchain')
class NativeCacheControlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ios-native-cache-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name).resolve()
        source = cls.work / 'Cache.swift'
        source.write_text((runner.TEMPLATES / 'CacheControls.swift.template').read_text() + '''
@main struct TestMain {
  static func main() {
    let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    do {
      let result = CommandLine.arguments[2] == "inspect" ? try IosVariantCache.snapshot(root) :
        try IosVariantCache.tamper(root, action: CommandLine.arguments[2], number: CommandLine.arguments[3], expected: CommandLine.arguments[4])
      print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    } catch { print("{\\"error\\":\\"rejected\\"}") }
  }
}
''')
        cls.binary = cls.work / 'cache-test'
        result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5', '-module-cache-path',
            str(cls.work / 'module-cache'), str(source), '-o', str(cls.binary)], capture_output=True)
        if result.returncode:
            raise AssertionError('Native helper failed compilation: ' + result.stderr.decode()[:3000])

    def setUp(self):
        self.temp_case = tempfile.TemporaryDirectory(prefix='ios-native-case-')
        self.addCleanup(self.temp_case.cleanup)
        self.root = Path(self.temp_case.name).resolve()
        (self.root / 'patches/2').mkdir(parents=True)
        self.artifact = self.root / 'patches/2/dlc.vmcode'
        self.artifact.write_bytes(b'{"encrypted_payload_base64":"AQIDBA==","raw_key":"never-export-this-marker"}')
        self.metadata = self.root / 'patches/2/state.json'
        self.metadata.write_text('{"kind":"Installed","signature":"AQIDBA==","size":77}')
        (self.root / 'selections-v2').mkdir()
        self.journal = self.root / 'selections-v2' / ('f' * 64 + '.json')
        self.journal.write_text(json.dumps(dict(version=2, epoch='1', digest='a' * 64, time_floor=10,
            authentication={'private': 'never-export-this-authentication'})))

    def run_helper(self, action='inspect', number='2', expected='0' * 64):
        value = subprocess.check_output([str(self.binary), str(self.root), action, number, expected], text=True)
        return json.loads(value)

    def test_actual_native_one_byte_ciphertext_and_signature_cas_preserve_journal(self):
        journal = self.journal.read_bytes()
        for action, path in (('tamper-ciphertext', self.artifact), ('tamper-signature', self.metadata)):
            before = path.read_bytes()
            result = self.run_helper(action, expected=hashlib.sha256(before).hexdigest())
            after = path.read_bytes()
            self.assertEqual(result['result'], 'mutated')
            self.assertEqual(len(before), len(after))
            self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
            self.assertEqual(result['after_sha256'], hashlib.sha256(after).hexdigest())
            self.assertEqual(self.journal.read_bytes(), journal)

    def test_actual_native_rejects_stale_hash_foreign_patch_symlink_and_hardlink(self):
        before = self.artifact.read_bytes()
        for action, number, expected in (('tamper-ciphertext', '2', '0' * 64), ('tamper-ciphertext', '../2', hashlib.sha256(before).hexdigest()),
                ('restore', '2', hashlib.sha256(before).hexdigest())):
            self.assertIn('error', self.run_helper(action, number, expected))
            self.assertEqual(self.artifact.read_bytes(), before)
        source = self.root / 'foreign'
        source.write_bytes(before)
        self.artifact.unlink()
        self.artifact.symlink_to(source)
        self.assertIn('error', self.run_helper('tamper-ciphertext', expected=hashlib.sha256(before).hexdigest()))
        self.assertEqual(source.read_bytes(), before)
        self.artifact.unlink()
        os.link(source, self.artifact)
        self.assertIn('error', self.run_helper('tamper-ciphertext', expected=hashlib.sha256(before).hexdigest()))
        self.assertEqual(source.read_bytes(), before)

    def test_actual_native_snapshot_exports_no_artifact_signature_or_authentication_bytes(self):
        result = self.run_helper()
        encoded = json.dumps(result)
        for secret in ('never-export-this', 'AQIDBA', 'authentication', 'signature'):
            self.assertNotIn(secret, encoded)
        runner.adversarial.snapshot(result)
        self.assertEqual(result['files']['patches/2/state.json']['kind'], 'Installed')





@unittest.skipUnless(shutil.which('xcrun'), 'Native diagnostics require the installed Apple Swift toolchain')
class NativeDiagnosticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ios-native-diagnostic-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name).resolve()
        source = cls.work / 'Diagnostic.swift'
        source.write_text((runner.TEMPLATES / 'Diagnostics.swift.template').read_text() + r'''
@main struct TestMain {
  static func main() throws {
    let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    let context: [String: Any] = ["session": "test-session", "pid": 42, "nonce": String(repeating: "a", count: 48),
      "home_present": false, "status_route": "public", "report_route": "native", "KEY_HEX": "never-log-this-secret"]
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 0, value: 0)
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 1, value: 0)
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 26, value: 154)
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 2, value: 154)
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 22, value: 1)
    let detail = CommandLine.arguments[2]
    _ = IosVariantDiagnostics.record(root: root, context: context, stage: 90, value: 0)
    for byte in detail.utf8 { _ = IosVariantDiagnostics.record(root: root, context: context, stage: 91, value: Int64(byte)) }
    let result = IosVariantDiagnostics.record(root: root, context: context, stage: 92, value: 0)
    print(result ? "accepted-detail" : "refused-detail")
  }
}
''')
        cls.binary = cls.work / 'diagnostic-test'
        result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5',
            '-module-cache-path', str(cls.work / 'module-cache'), str(source), '-o', str(cls.binary)], capture_output=True)
        if result.returncode: raise AssertionError('Native diagnostics failed compilation: ' + result.stderr.decode()[:3000])

    def test_actual_native_flushed_nonce_stages_and_sanitized_stack_without_home(self):
        with tempfile.TemporaryDirectory(prefix='ios-diagnostic-case-') as directory:
            payload = dict(error_type='StateError', frames=['variant_status.dart:4:2', 'updater_bindings.g.dart:2375'])
            result = subprocess.run([self.binary, directory, json.dumps(payload)], capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), b'accepted-detail')
            path = Path(directory) / ('diagnostic-' + 'a' * 48 + '.jsonl')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([x['stage'] for x in rows], ['native-ready', 'variant-entry', 'dart-environment-read', 'constructor-before',
                'root-isolate-heartbeat', 'error-detail-before', 'error-detail-after'])
            self.assertEqual([x['sequence'] for x in rows], list(range(len(rows))))
            self.assertTrue(all(x['pid'] == 42 and x['nonce'] == 'a' * 48 for x in rows))
            self.assertFalse(rows[1]['native_environment']['home_present'])
            self.assertEqual(rows[-1]['error'], payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('never-log-this-secret', path.read_text())

    def test_actual_native_refuses_unsanitized_error_payload_and_arbitrary_frame_uri(self):
        for payload in (dict(error_type='StateError', frames=[], message='never-log-this-secret'),
                dict(error_type='StateError', frames=['https://example.invalid/?token=never-log-this-secret'])):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory(prefix='ios-diagnostic-refusal-') as directory:
                result = subprocess.run([self.binary, directory, json.dumps(payload)], capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), b'refused-detail')
                path = Path(directory) / ('diagnostic-' + 'a' * 48 + '.jsonl')
                self.assertNotIn('never-log-this-secret', path.read_text())
                self.assertNotIn('"error"', path.read_text())


if __name__ == '__main__':
    unittest.main()

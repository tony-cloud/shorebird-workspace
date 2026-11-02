"""Pure/mocked fixture tests; no builds, server, signing or device commands."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest import mock

import ios_variant_e2e as runner


class IosVariantFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ios-variant-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        dependency = self.root / 'updater'
        (dependency / 'lib/src').mkdir(parents=True)
        (dependency / 'lib/src/selection_status.dart').touch()
        self.args = argparse.Namespace(cli=self.root / 'cli', flutter_root=self.root / 'flutter',
            engine_src=self.root / 'engine', server=self.root / 'server', updater_package=dependency,
            release_version='1.0.0+1', device='owned-device', team='OWNEDTEAM',
            host_address='192.0.2.20', bind_address='0.0.0.0')
        self.args.cli.write_bytes(b'test prebuilt CLI placeholder; never executed')
        self.args.server.write_bytes(b'test prebuilt server placeholder; never executed')
        self.work = self.root / 'work'
        self.state = runner.prepare(self.args, self.work)
        self.state['app_id'] = 'real-app'
        self.scope = dict(app_id='real-app', release_version='1.0.0+1', platform='ios', arch='aarch64',
            base_artifact_hash='d' * 64, host_abi_hash='e' * 64, runtime_profile='interpreter-full')
        self.state['release_scope'] = self.scope
        runner.configure_native(self.work, self.state)
        runner.save(self.work, self.state)
        self.nonce = '1' * 48

    def identity(self, variant='free', epoch='0', digest=None):
        return dict(epoch=epoch, digest=digest, variant_id=variant, license_type=variant,
            patch_number=runner.selection.EXPECTED_NUMBERS[variant])

    def ffi(self, variant):
        base = variant == 'free'
        return dict(schema=runner.ios.FFI_SCHEMA, phase='base' if base else 'update',
            license='free' if base else 'pro', pro_feature_enabled=not base, structural_verified=True,
            ffi_verified=True, rendered_text=('license:free full-replacement:verified value:10 full-ffi:v1-base'
                if base else 'license:pro full-replacement:verified value:28 full-ffi:v2-update'),
            checks=list(runner.ios.FFI_BASE_CHECKS if base else runner.ios.FFI_UPDATE_CHECKS),
            workers_started=0 if base else 2, workers_joined=True, callbacks_closed=True,
            static_callbacks_retained=0 if base else 1)

    def report(self, variant='free', epoch='0', digest=None):
        data = dict(session=self.state['session'], sentinel=self.state['sentinel'], schema=1 if variant == 'free' else 2 if variant == 'pro' else 3,
            launches=1, history=[variant])
        if data['schema'] >= 2: data['pro_note'] = 'pro-preserved'
        if data['schema'] >= 3: data['enterprise_note'] = 'enterprise-preserved'
        identity = self.identity(variant, epoch, digest)
        started = time.time()
        empty = dict(schema='ios-variant-cache-v1', files={})
        return dict(schema=runner.SCHEMA, session=self.state['session'], nonce=self.nonce,
            variant=variant, new_code_result={'free': 'free:base', 'pro': 'pro:42', 'enterprise': 'enterprise:24'}[variant],
            installation_id='real-installation', pid=12, started=started, completed=started, stage='boot', error=None,
            update_status=None, data=data, post_frame=True, status_route='public', report_route='native',
            ffi=self.ffi(variant), key_control=None, provider=None, cache_control=None,
            native_boot=dict(session=self.state['session'], nonce=self.nonce, pid=12, time=started),
            cache_before=copy.deepcopy(empty), cache_after_control=copy.deepcopy(empty), cache_after=copy.deepcopy(empty),
            selection=dict(version=2, current=copy.deepcopy(identity), accepted=copy.deepcopy(identity),
                next_boot=dict(variant_id=variant), restart_required=False))

    def start_http_observer(self):
        self.state['launches'] = [dict(nonce=self.nonce, after=self.state['created'], check=True,
            report_route='native', status_route='public')]
        self.state.update(bind_address='127.0.0.1', report_url='http://127.0.0.1:0/report/' + self.state['session'])
        runner.save(self.work, self.state)
        server = runner.report_observer(self.work, self.state)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        def post(body):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('POST', '/report/' + self.state['session'], body=body,
                    headers={'Content-Type': 'application/json'})
                response = connection.getresponse(); response.read()
                return response.status
            finally: connection.close()
        return post

    def test_real_http_observer_receipt_precedes_terminal_publish_and_late_boot_cannot_replace_it(self):
        post = self.start_http_observer()
        boot = self.report(); boot_raw = json.dumps(boot).encode()
        self.assertEqual(post(boot_raw), 204)
        launch = self.state['launches'][-1]
        runner.read_http_receipt(self.work, launch, boot)
        checked = dict(boot, stage='checked', update_status='upToDate')
        checked_raw = json.dumps(checked).encode()
        receipt_written, release = threading.Event(), threading.Event()
        original = runner.adversarial.atomic_receipt
        def paused(fixture, path, value):
            original(fixture, path, value)
            if path.name == 'checked.json':
                receipt_written.set()
                self.assertTrue(release.wait(5))
        statuses = {}
        with mock.patch.object(runner.adversarial, 'atomic_receipt', side_effect=paused):
            terminal = threading.Thread(target=lambda: statuses.update(checked=post(checked_raw)))
            terminal.start()
            try:
                self.assertTrue(receipt_written.wait(5))
                self.assertEqual((self.work / 'reports' / (self.nonce + '.json')).read_bytes(), boot_raw)
                self.assertTrue(runner.http_receipt_path(self.work, self.nonce, 'checked').is_file())
                late = threading.Thread(target=lambda: statuses.update(boot=post(boot_raw)))
                late.start()
            finally: release.set()
            terminal.join(5); late.join(5)
            self.assertFalse(terminal.is_alive() or late.is_alive())
        self.assertEqual(statuses, {'checked': 204, 'boot': 400})
        receipt = runner.read_http_receipt(self.work, launch, checked)
        path = self.work / 'reports' / (self.nonce + '.json')
        receipt_bytes = runner.http_receipt_path(self.work, self.nonce, 'checked').read_bytes()
        before = path.stat()
        self.assertEqual(post(checked_raw), 204)
        self.assertEqual(runner.http_receipt_path(self.work, self.nonce, 'checked').read_bytes(), receipt_bytes)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(post(json.dumps(dict(checked, stage='check_failed')).encode()), 400)
        self.assertEqual(post(json.dumps(dict(checked, pid=999)).encode()), 400)
        self.assertEqual(receipt['body_sha256'], hashlib.sha256(checked_raw).hexdigest())

    def test_http_receipt_rejects_missing_non_http_tampered_identity_body_rawfile_and_old_launch(self):
        post = self.start_http_observer()
        report = dict(self.report(), stage='checked', update_status='upToDate')
        raw = json.dumps(report).encode(); launch = self.state['launches'][-1]
        runner.receive_report(self.work, raw)  # Direct/USB persistence is not HTTP proof.
        with self.assertRaisesRegex(ValueError, 'host HTTP report receipt'):
            runner.read_http_receipt(self.work, launch, report)
        self.assertEqual(post(raw), 204)
        path = runner.http_receipt_path(self.work, self.nonce, 'checked')
        original = json.loads(path.read_bytes())
        for key, value in (('pid', 99), ('nonce', '0' * 48), ('session', 'foreign'), ('stage', 'boot'),
                ('body_sha256', '0' * 64), ('body_size', 1), ('received_at', time.time() + 60), ('recorded_at', 1),
                ('raw_file', {**original['raw_file'], 'inode': original['raw_file']['inode'] + 1})):
            altered = {**original, key: value}
            runner.ios.write_json(path, altered)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'identity/body/host timing'):
                runner.read_http_receipt(self.work, launch, report)
        runner.ios.write_json(path, original)
        current = runner.load(self.work)
        current['launches'].append({**launch, 'nonce': 'b' * 48})
        runner.save(self.work, current)
        with self.assertRaisesRegex(ValueError, 'current launch'):
            runner.read_http_receipt(self.work, launch, report)
        runner.save(self.work, self.state)
        target = self.work / 'reports' / (self.nonce + '.json')
        target.write_bytes(raw)  # Same bytes, foreign file-write observation.
        with self.assertRaisesRegex(ValueError, 'identity/body/host timing'):
            runner.read_http_receipt(self.work, launch, report)

    def validate(self, report, **kwargs):
        return runner.validate_report(self.state, report, nonce=self.nonce,
            after=self.state['created'], variant=report['variant'], **kwargs)

    def assignment(self, variant='pro', epoch='1', digest='a' * 64, body=None):
        body = body or {}
        return dict(selection_digest=digest, selection=dict(epoch=epoch, variant_id=variant, license_type=variant,
            channel='stable', entitlement_revision=body.get('entitlement_revision', self.state['session'] + ':revision'),
            not_before=body.get('not_before', '0'), offline_valid_until=body.get('offline_valid_until'),
            scope={**self.scope, 'client_id_hash': hashlib.sha256(b'real-installation').hexdigest()},
            target=dict(patch_number=runner.selection.EXPECTED_NUMBERS[variant])))

    def test_prepare_is_fresh_private_and_does_not_embed_keys(self):
        with self.assertRaisesRegex(ValueError, 'fresh work'):
            runner.prepare(self.args, self.work)
        self.assertNotEqual(self.state['key_ids']['pro'], self.state['key_ids']['enterprise'])
        secrets = json.loads((self.work / 'secrets.json').read_text())
        self.assertEqual((self.work / 'secrets.json').stat().st_mode & 0o777, 0o600)
        for path in (self.work / 'app/lib/variant_status.dart', self.work / 'app/ios/Runner/AppDelegate.swift', self.work / 'app/shorebird.yaml'):
            for key in secrets['variant_keys'].values(): self.assertNotIn(key, path.read_text())
        self.assertIn('selection_protocol: 2\n', (self.work / 'app/shorebird.yaml').read_text())
        self.assertIn('auto_update: false\n', (self.work / 'app/shorebird.yaml').read_text())
        self.args.host_address = '127.0.0.1'
        with self.assertRaisesRegex(ValueError, 'device-reachable'):
            runner.prepare(self.args, self.root / 'bad-host')

    def test_paid_profiles_preserve_structural_and_all_ffi_checks(self):
        native = runner.native_hashes(self.work)
        consumer = (self.work / 'app/lib/unchanged_caller.dart').read_bytes()
        for phase in ('pro_seed', 'pro', 'enterprise', 'base'):
            runner.apply_profile(self.work, self.state, phase)
            self.assertEqual(runner.native_hashes(self.work), native)
            main = (self.work / 'app/lib/main.dart').read_text()
            self.assertLess(main.index('writeFullFfiStatus('), main.index('await writeVariantStatus();'))
            self.assertIn('variantRendered.text.toPlainText()', main)
            expected = runner.ios.FFI_BASE_CHECKS if phase == 'base' else runner.ios.FFI_UPDATE_CHECKS
            ffi = (self.work / 'app/lib/ffi_plugin.dart').read_text()
            for check in expected: self.assertIn("'" + check + "'", ffi)
            if phase != 'base':
                self.assertEqual((self.work / 'app/lib/unchanged_caller.dart').read_bytes(), consumer)
                self.assertFalse((self.work / 'app/lib/removed_library.dart').exists())
            template = (self.work / 'app/lib/variant_status.dart').read_text()
            self.assertNotIn('__SESSION__', template)
            self.assertNotIn('__VARIANT_CODE__', template)

    def test_native_sources_sealed_after_release(self):
        self.state['base_built'] = True
        path = self.work / 'app/ios/Runner/AppDelegate.swift'
        path.write_text(path.read_text() + '\n// changed')
        with self.assertRaisesRegex(ValueError, 'native sources changed'):
            runner.configure_native(self.work, self.state)

    def test_changed_prepared_live_sources_cannot_generate_or_execute_another_build(self):
        original_read = Path.read_bytes
        main = self.work / 'app/lib/main.dart'
        before = main.read_bytes()
        for template in ('AppDelegate.swift.template', 'variant_status.dart.template'):
            target = runner.TEMPLATES / template
            def changed_read(path):
                contents = original_read(path)
                return contents + b'\nchanged live source' if path == target else contents
            with self.subTest(template=template), mock.patch.object(Path, 'read_bytes', changed_read), mock.patch.object(runner.ios, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'fixture source seal changed'):
                    runner.build_phase(self.work, self.state, 'base')
                run.assert_not_called()
            self.assertEqual(main.read_bytes(), before)

    def test_changed_copied_templates_or_tools_fail_before_execution(self):
        for name in ('copied-template', 'cli', 'server'):
            target = (self.work / 'app/tool/full_replacement_ffi/patch/main.dart.template'
                if name == 'copied-template' else Path(self.state[name]))
            before = target.read_bytes()
            target.write_bytes(before + b'\nchanged prepared input')
            with self.subTest(input=name), mock.patch.object(runner.ios, 'run') as run:
                with self.assertRaisesRegex(ValueError, '(template|tool) seal changed'):
                    runner.build_phase(self.work, self.state, 'base')
                run.assert_not_called()
            target.write_bytes(before)
        added = self.work / 'app/tool/full_replacement_ffi/patch/extra.dart.template'
        added.write_text('unexpected new copied template')
        with mock.patch.object(runner.ios, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'copied template seal changed'):
                runner.build_phase(self.work, self.state, 'base')
            run.assert_not_called()

    def test_compiled_scope_binds_baseline_host_abi_and_strict_signature_policy(self):
        assets = self.work / 'compiled/App.app/Frameworks/App.framework/flutter_assets'
        assets.mkdir(parents=True)
        baseline = assets / 'shorebird_replacement_base.sbr'
        baseline.write_bytes(b'non-executable scope test data')
        scope = {**self.scope, 'base_artifact_hash': hashlib.sha256(baseline.read_bytes()).hexdigest()}
        self.state.pop('release_scope')
        scope_file = assets / 'shorebird_release_scope_ios_aarch64.json'
        runner.ios.write_json(scope_file, scope)
        policy = runner.selection.yaml_source(self.state) + ('patch_public_key: public-test-key\n'
            'aot_patch_base_snapshot_hash: ' + scope['base_artifact_hash'] + '\n'
            'aot_patch_host_abi: ' + scope['host_abi_hash'] + '\n')
        (assets / 'shorebird.yaml').write_text(policy)
        runner.validate_compiled_scope(self.state, self.work / 'compiled/App.app')
        for change in ('baseline', 'abi', 'signature', 'raw-key', 'platform'):
            with self.subTest(change=change):
                altered = copy.deepcopy(scope)
                text = policy
                baseline.write_bytes(b'non-executable scope test data')
                if change == 'baseline': baseline.write_bytes(b'changed original baseline')
                elif change == 'abi': altered['host_abi_hash'] = 'f' * 64
                elif change == 'signature': text = text.replace('patch_verification: strict', 'patch_verification: off')
                elif change == 'raw-key': text += 'aot_patch_key_hex: ' + 'a' * 64 + '\n'
                else: altered['platform'] = 'macos'
                runner.ios.write_json(scope_file, altered)
                (assets / 'shorebird.yaml').write_text(text)
                with self.assertRaises(ValueError): runner.validate_compiled_scope(self.state, self.work / 'compiled/App.app')

    def test_real_ios_cli_signed_publication_and_independent_keys(self):
        secrets = json.loads((self.work / 'secrets.json').read_text())
        secrets['access_token'] = 'ephemeral-token'
        runner.ios.write_json(self.work / 'secrets.json', secrets)
        for phase in runner.BUILD_ORDER:
            command = runner.cli_command(self.work, self.state, phase)
            self.assertEqual(command[5], 'release' if phase == 'base' else 'patch')
            self.assertIn('ios', command)
            self.assertNotIn('--no-codesign', command)
            env = runner.build_environment(self.work, self.state, phase)
            if phase == 'base':
                self.assertTrue(any(x.startswith('--export-options-plist=') for x in command))
                self.assertNotIn('SHOREBIRD_AOT_PATCH_KEY_HEX', env)
            else:
                variant = 'pro' if phase == 'pro_seed' else phase
                self.assertIn('--variant=' + variant, command)
                self.assertEqual(env['SHOREBIRD_AOT_PATCH_KEY_HEX'], secrets['variant_keys'][variant])
                self.assertEqual(env['SHOREBIRD_AOT_PATCH_KEY_ID'], self.state['key_ids'][variant])

    def test_nonce_report_rejects_historical_incomplete_and_route_missing(self):
        self.validate(self.report())
        for label in ('nonce', 'schema', 'ffi', 'render', 'installation', 'route', 'time'):
            value = self.report()
            if label == 'nonce': value['nonce'] = '2' * 48
            elif label == 'schema': value['schema'] = runner.selection.SCHEMA
            elif label == 'ffi': value['ffi']['workers_joined'] = False
            elif label == 'render': value['post_frame'] = False
            elif label == 'installation': value['installation_id'] = ''
            elif label == 'route': value.pop('status_route')
            else: value['started'] = self.state['created'] - 1
            with self.subTest(label=label), self.assertRaises(ValueError): self.validate(value)

    def test_same_content_new_epoch_requires_cold_restart(self):
        self.state['assignments'] = [self.assignment('pro', '2')]
        report = self.report('pro', '1', 'b' * 64)
        report.update(stage='checked', update_status='restartRequired')
        report['selection']['accepted'] = self.identity('pro', '2', 'a' * 64)
        report['selection']['restart_required'] = True
        self.validate(report, checked=True)
        with self.assertRaises(ValueError): self.validate(report, checked=True, cold=True)
        report['selection']['restart_required'] = False
        with self.assertRaisesRegex(ValueError, 'require restart'): self.validate(report, checked=True)

    def test_cold_paid_requires_exact_native_authenticated_context_receipt(self):
        self.state['assignments'] = [self.assignment('enterprise', '3')]
        report = self.report('enterprise', '3', 'a' * 64)
        report['provider'] = dict(schema='ios-variant-provider-v2', session=self.state['session'], nonce=self.nonce,
            variant='enterprise', account=self.state['key_ids']['enterprise'], selection_digest='a' * 64, epoch='3', pid=12)
        self.validate(report, cold=True)
        for change in ({'nonce': '2' * 48}, {'account': self.state['key_ids']['pro']}, {'epoch': '2'}, {'pid': 99}):
            value = copy.deepcopy(report)
            value['provider'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'native key-context'): self.validate(value, cold=True)

    def test_free_fallback_retains_paid_epoch_and_additive_enterprise_data(self):
        self.state['assignments'] = [self.assignment('enterprise', '3')]
        previous = self.report('enterprise', '3', 'a' * 64)['data']
        report = self.report('free', '3', 'a' * 64)
        report['selection']['accepted'] = self.identity('enterprise', '3', 'a' * 64)
        report['data'] = {**previous, 'launches': 2, 'history': ['enterprise', 'free']}
        self.validate(report, previous_data=previous, cold=True, fallback=True)
        value = copy.deepcopy(report)
        value['data'].pop('enterprise_note')
        with self.assertRaises(ValueError): self.validate(value, previous_data=previous, cold=True, fallback=True)
        value = copy.deepcopy(report)
        value['selection']['current']['patch_number'] = '2'
        with self.assertRaises(ValueError): self.validate(value, cold=True, fallback=True)

    def test_assignment_cas_persists_retry_and_checks_complete_original_scope(self):
        self.state['installation_id'] = 'real-installation'
        runner.save(self.work, self.state)
        calls = []
        def api(work, state, method, suffix, body=None):
            if method == 'GET': return {'releases': [{'id': 9, 'version': state['release_version'], 'selection_protocol': 2}]}
            calls.append(copy.deepcopy(body))
            if len(calls) == 1: raise OSError('lost response')
            return self.assignment(body=body)
        with mock.patch.object(runner, 'api', side_effect=api):
            with self.assertRaises(OSError): runner.assign(self.work, self.state, 'pro')
            self.assertEqual(runner.load(self.work)['pending_assignment'], calls[0])
            runner.assign(self.work, self.state, 'pro')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0]['platform'], 'ios')
        self.assertEqual(calls[0]['expected_epoch'], '0')
        self.assertNotIn('pending_assignment', self.state)
        self.state['assignments'] = []
        def foreign_api(work, state, method, suffix, body=None):
            if method == 'GET': return {'releases': [{'id': 9, 'version': state['release_version'], 'selection_protocol': 2}]}
            value = self.assignment(body=body)
            value['selection']['scope']['host_abi_hash'] = 'f' * 64
            return value
        with mock.patch.object(runner, 'api', side_effect=foreign_api), self.assertRaisesRegex(ValueError, 'installed release'):
            runner.assign(self.work, self.state, 'pro')

    def test_assignment_does_not_accept_altered_or_missing_requested_lease(self):
        self.state['installation_id'] = 'real-installation'
        runner.save(self.work, self.state)
        for change in ('not-before', 'expiry', 'unbounded', 'missing-not-before', 'missing-expiry'):
            self.state.pop('pending_assignment', None)
            runner.save(self.work, self.state)
            requested = []
            def api(work, state, method, suffix, body=None):
                if method == 'GET': return {'releases': [{'id': 9, 'version': state['release_version'], 'selection_protocol': 2}]}
                requested.append(copy.deepcopy(body))
                result = self.assignment(body=body)
                if change == 'not-before': result['selection']['not_before'] = '1700000001'
                elif change == 'expiry': result['selection']['offline_valid_until'] = '1700000200'
                elif change == 'unbounded': result['selection']['offline_valid_until'] = None
                elif change == 'missing-not-before': result['selection'].pop('not_before')
                else: result['selection'].pop('offline_valid_until')
                return result
            with self.subTest(change=change), mock.patch.object(runner, 'api', side_effect=api):
                with self.assertRaisesRegex(ValueError, 'persisted lease request'):
                    runner.assign(self.work, self.state, 'pro', not_before='1700000000', offline_valid_until='1700000100')
            saved = runner.load(self.work)
            self.assertEqual(saved['pending_assignment'], requested[0])
            self.assertEqual(saved['assignments'], [])
        self.state.pop('pending_assignment')
        runner.save(self.work, self.state)
        def valid_api(work, state, method, suffix, body=None):
            if method == 'GET': return {'releases': [{'id': 9, 'version': state['release_version'], 'selection_protocol': 2}]}
            return self.assignment(body=body)
        with mock.patch.object(runner, 'api', side_effect=valid_api):
            result = runner.assign(self.work, self.state, 'pro', not_before='1700000000', offline_valid_until='1700000100')
        self.assertEqual(result['selection']['offline_valid_until'], '1700000100')

    def test_device_launch_sanitizes_controls_and_records_nonce_before_command(self):
        self.state['installed_app_sha256'] = 'a' * 64
        runner.save(self.work, self.state)
        with mock.patch.dict(os.environ, {'DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_HEX': 'foreign', 'DYLD_INSERT_LIBRARIES': 'foreign'}):
            env, nonce = runner.launch_environment(self.work, self.state)
            self.assertNotIn('DYLD_INSERT_LIBRARIES', env)
            self.assertNotIn('DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_HEX', env)
            self.assertEqual(len(nonce), 48)
        def run(command, **kwargs):
            launch = runner.load(self.work)['launches'][-1]
            self.assertEqual(kwargs['env']['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_NONCE'], launch['nonce'])
            self.assertIn('--terminate-existing', command)
            self.assertEqual(command[-1], self.state['bundle_id'])
            self.assertNotIn('--console', command)
            return b''
        with mock.patch.object(runner.ios, 'run', side_effect=run):
            runner.launch(self.work, self.state, action='provision', variant='pro')

    def test_lost_native_key_launch_response_blocks_new_nonce_until_its_receipt(self):
        self.state['installed_app_sha256'] = 'a' * 64
        runner.save(self.work, self.state)
        stale = copy.deepcopy(self.state)
        def lost(command, **kwargs):
            saved = runner.load(self.work)
            pending = saved['pending_key_launch']
            self.assertEqual(pending, saved['launches'][-1])
            self.assertEqual(pending['nonce'], kwargs['env']['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_NONCE'])
            raise RuntimeError('lost key-control launch response')
        with mock.patch.object(runner.ios, 'run', side_effect=lost):
            with self.assertRaises(RuntimeError): runner.launch(self.work, self.state, action='provision', variant='pro')
        saved = runner.load(self.work)
        self.assertEqual(len(saved['launches']), 1)
        for state, action in ((stale, None), (saved, 'provision'), (saved, 'delete')):
            with self.subTest(action=action), mock.patch.object(runner.ios, 'run') as run, mock.patch.object(runner.secrets, 'token_hex') as nonce:
                with self.assertRaisesRegex(ValueError, 'key-control launch unresolved'):
                    runner.launch(self.work, state, action=action, variant='pro')
                run.assert_not_called()
                nonce.assert_not_called()
        self.state = runner.load(self.work)
        self.nonce = self.state['pending_key_launch']['nonce']
        report = self.report()
        report['key_control'] = dict(schema='ios-variant-key-control-v2', nonce=self.nonce,
            session=self.state['session'], bundle_id=self.state['bundle_id'], service=self.state['key_service'],
            account=self.state['key_ids']['pro'], action='provision', result='stored', pid=report['pid'], time=time.time())
        path = self.work / 'reconciled.json'
        for change in ('wrong-nonce', 'incomplete-ffi', 'wrong-process'):
            value = copy.deepcopy(report)
            if change == 'wrong-nonce': value['key_control']['nonce'] = '2' * 48
            elif change == 'incomplete-ffi': value['ffi']['callbacks_closed'] = False
            else: value['key_control']['pid'] = report['pid'] + 1
            runner.ios.write_json(path, value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.read_report(self.work, self.state, variant='free', path=path, cold=True)
            self.assertIn('pending_key_launch', runner.load(self.work))
        runner.ios.write_json(path, report)
        runner.read_report(self.work, self.state, variant='free', path=path, cold=True)
        self.assertNotIn('pending_key_launch', runner.load(self.work))
        with mock.patch.object(runner.ios, 'run', return_value=b''):
            next_launch = runner.launch(self.work, self.state)
        self.assertNotEqual(next_launch['nonce'], self.nonce)

    def test_successful_key_launch_command_also_requires_native_receipt_before_cleanup(self):
        self.state['installed_app_sha256'] = 'a' * 64
        runner.save(self.work, self.state)
        with mock.patch.object(runner.ios, 'run', return_value=b''):
            record = runner.launch(self.work, self.state, action='delete', variant='enterprise')
        self.assertEqual(runner.load(self.work)['pending_key_launch']['nonce'], record['nonce'])
        with self.assertRaisesRegex(ValueError, 'key-control launch unresolved'):
            runner.begin_cleanup(self.work, self.state)
        with mock.patch.object(runner.ios, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'key-control launch unresolved'):
                runner.launch(self.work, self.state)
            run.assert_not_called()

    def test_report_read_requires_full_ffi_before_advancing_authorization(self):
        launch = dict(nonce=self.nonce, after=self.state['created'], check=False, action=None,
            report_route='native', status_route='public')
        self.state['launches'] = [launch]
        runner.save(self.work, self.state)
        path = self.work / 'report.json'
        report = self.report()
        report['ffi']['callbacks_closed'] = False
        runner.ios.write_json(path, report)
        with self.assertRaises(ValueError): runner.read_report(self.work, self.state, variant='free', path=path)
        self.assertNotIn('installation_id', runner.load(self.work))
        report['ffi']['callbacks_closed'] = True
        runner.ios.write_json(path, report)
        runner.read_report(self.work, self.state, variant='free', path=path, cold=True)
        self.assertEqual(runner.load(self.work)['installation_id'], 'real-installation')
        with self.assertRaises(ValueError): runner.read_report(self.work, self.state, variant='free', path=path)

    def test_observer_is_nonce_scoped_and_late_boot_cannot_replace_checked_evidence(self):
        self.state['launches'] = [dict(nonce=self.nonce)]
        runner.save(self.work, self.state)
        report = self.report()
        runner.receive_report(self.work, json.dumps(report).encode())
        report['stage'] = 'checked'
        runner.receive_report(self.work, json.dumps(report).encode())
        path = self.work / 'reports' / (self.nonce + '.json')
        for change in ({'stage': 'boot'}, {'nonce': '2' * 48}, {'session': 'foreign'}, {'schema': 'license-v1'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.receive_report(self.work, json.dumps({**report, **change}).encode())
            self.assertEqual(json.loads(path.read_text())['stage'], 'checked')
        with self.assertRaises(ValueError): runner.receive_report(self.work, b'x' * (runner.MAX_REPORT + 1))
        self.assertNotIn('installation_id', runner.load(self.work))

    def test_cleanup_fences_provisioning_and_requires_both_native_key_deletes(self):
        runner.begin_cleanup(self.work, self.state)
        for action in (None, 'provision', 'overwrite'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                runner.launch_environment(self.work, self.state, action=action, variant='pro')
        with self.assertRaises(ValueError): runner.launch_environment(self.work, self.state, action='delete', variant='pro', check=True)
        runner.launch_environment(self.work, self.state, action='delete', variant='pro')
        with mock.patch.object(runner.ios, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'both independent keys'): runner.cleanup(self.work, self.state)
            run.assert_not_called()
        launch = dict(nonce=self.nonce, after=time.time(), action='delete', variant='pro')
        receipt = dict(schema='ios-variant-key-control-v2', nonce=self.nonce, session=self.state['session'],
            bundle_id=self.state['bundle_id'], service=self.state['key_service'], account=self.state['key_ids']['pro'],
            action='delete', result='deleted', pid=12, time=time.time())
        runner.validate_key_receipt(self.state, receipt, launch)
        for change in ({'account': 'foreign'}, {'nonce': '2' * 48}, {'time': float('nan')}, {'time': self.state['created'] - 1}, {'action': 'provision'}):
            with self.subTest(change=change), self.assertRaises(ValueError): runner.validate_key_receipt(self.state, {**receipt, **change}, launch)

    def test_installation_response_uncertainty_cannot_replay_mutation(self):
        with mock.patch.object(runner, 'inspect_app', return_value=(self.root / 'App.app', 'b' * 64)), mock.patch.object(runner.ios, 'run', side_effect=RuntimeError('lost install response')) as run:
            with self.assertRaises(RuntimeError): runner.install(self.work, self.state, self.root / 'App.app')
            self.assertTrue(runner.load(self.work)['installation_uncertain'])
            with self.assertRaisesRegex(ValueError, 'uncertain'): runner.install(self.work, self.state, self.root / 'App.app')
            self.assertEqual(run.call_count, 1)

    def test_cleanup_uninstalls_only_owned_bundle_and_does_not_replay_uncertain_response(self):
        runner.begin_cleanup(self.work, self.state)
        self.state['deleted_keys'] = {}
        for index, variant in enumerate(('pro', 'enterprise')):
            nonce = str(index + 1) * 48
            launch = dict(nonce=nonce, after=time.time(), action='delete', variant=variant)
            self.state['launches'].append(launch)
            self.state['deleted_keys'][variant] = dict(schema='ios-variant-key-control-v2', nonce=nonce,
                session=self.state['session'], bundle_id=self.state['bundle_id'], service=self.state['key_service'],
                account=self.state['key_ids'][variant], action='delete', result='deleted', pid=12, time=time.time())
        runner.save(self.work, self.state)
        with mock.patch.object(runner.selection.v1, 'port_open', return_value=False), mock.patch.object(runner.ios, 'run', side_effect=RuntimeError('lost response')) as run:
            with self.assertRaises(RuntimeError): runner.cleanup(self.work, self.state)
            self.assertTrue(runner.load(self.work)['uninstall_uncertain'])
            with self.assertRaisesRegex(ValueError, 'uncertain'): runner.cleanup(self.work, self.state)
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertEqual(command[-1], self.state['bundle_id'])
            self.assertEqual(command[-2], self.state['device'])
            self.assertTrue((self.work / 'secrets.json').exists())

    def test_native_observer_never_blocks_ui_or_requires_network_to_provision(self):
        source = (self.work / 'app/ios/Runner/AppDelegate.swift').read_text()
        self.assertIn('@_cdecl("IosVariantSubmitReport")', source)
        self.assertIn('URLSession.shared.dataTask', source)
        self.assertNotIn('DispatchSemaphore', source)
        self.assertNotIn('.wait(', source)
        self.assertLess(source.index('switch action'), source.index('setShorebirdAotPatchKeyProviderV2'))
        for scope in ('base_artifact_hash', 'host_abi_hash', 'client_id_hash', 'artifact_key_id'):
            self.assertIn(scope, source)
        self.assertFalse(runner.plan()['runtime_acceptance'])


if __name__ == '__main__':
    unittest.main()

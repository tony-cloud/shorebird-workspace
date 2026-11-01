"""Source/preparation contract tests only. No signing, native build or runtime."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

import macos_variant_e2e as runner


class VariantFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='macos-variant-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'session'
        dependency = self.root / 'private-updater/shorebird_code_push'
        (dependency / 'lib/src').mkdir(parents=True)
        (dependency / 'lib/src/selection_status.dart').write_text('// Test preparation path only')
        self.args = types.SimpleNamespace(
            cli=Path('/private/caller/shorebird'), flutter_root=Path('/private/caller/flutter'),
            engine_src=Path('/private/caller/engine'), engine_name='macos_release_arm64',
            host_engine_name='macos_release_arm64', server=Path('/private/caller/server'),
            release_version='1.0.0+1', updater_package=dependency)
        with mock.patch.object(runner.v1, 'unique_ports', return_value=(43101, 43102)):
            self.state = runner.prepare(self.args, self.work)
        credentials = json.loads((self.work / 'secrets.json').read_text())
        credentials['access_token'] = 'synthetic-preparation-token'
        runner.v1.shared.write_json(self.work / 'secrets.json', credentials)
        self.state['app_id'] = '11111111-1111-4111-8111-111111111111'
        runner.configure_native(self.work, self.state)
        runner.v1.save(self.work, self.state)

    def test_private_dependency_protocol_and_native_key_separation(self):
        self.assertEqual(runner.load(self.work)['variant_schema'], runner.SCHEMA)
        self.assertNotEqual(self.state['key_ids']['pro'], self.state['key_ids']['enterprise'])
        source = (self.work / 'app/pubspec.yaml').read_text()
        self.assertIn(str(self.args.updater_package), source)
        yaml = runner.yaml_source(self.state)
        for item in ('selection_protocol: 2', 'aot_patch_app_build_id: 1.0.0+1',
                     'patch_verification: strict', 'patch_response_verification: required', 'auto_update: false'):
            self.assertIn(item, yaml)
        swift = (self.work / 'app/macos/Runner/PatchTestKeys.swift').read_text()
        self.assertIn('setShorebirdAotPatchKeyProviderV2', swift)
        self.assertNotIn('setShorebirdAotPatchKeyProvider {', swift)
        for item in ('scope["app_id"]', 'scope["release_version"]', 'scope["platform"]',
                     'scope["arch"]', 'descriptor["license_type"]', 'target["artifact_key_id"]',
                     'revision.hasPrefix(session + ":")', 'allowed[variant] == keyID'):
            self.assertIn(item, swift)
        credentials = json.loads((self.work / 'secrets.json').read_text())
        self.assertNotIn('aes_key_hex', credentials)
        self.assertNotEqual(*credentials['variant_keys'].values())
        for secret in credentials['variant_keys'].values():
            for path in (self.work / 'app').rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertEqual((self.work / 'secrets.json').stat().st_mode & 0o777, 0o600)
        self.args.updater_package = runner.v1.ROOT / 'updater/shorebird_code_push'
        with self.assertRaisesRegex(ValueError, 'private v2 updater clone'):
            runner.prepare(self.args, self.root / 'bad')

    def test_paid_profiles_retain_full_ffi_before_new_code_data_and_public_apis(self):
        for phase, marker in (('pro', 'class ProVariantFeature'), ('enterprise', 'class EnterpriseVariantFeature')):
            with self.subTest(phase=phase):
                runner.apply_profile(self.work, self.state, phase)
                source = (self.work / 'app/lib/main.dart').read_text()
                self.assertLess(source.index('writeFullFfiStatus('), source.index('await writeVariantStatus();'))
                ffi = (self.work / 'app/lib/ffi_plugin.dart').read_text()
                for check in runner.v1.shared.FFI_UPDATE_CHECKS:
                    self.assertIn("'" + check + "'", ffi)
                status = (self.work / 'app/lib/variant_status.dart').read_text()
                for value in (marker, 'readInstallationId()', 'readSelectionStatus()',
                              "data['history']", "data.putIfAbsent('enterprise_note'", 'temporary.renameSync'):
                    self.assertIn(value, status)
                self.assertNotIn('__SESSION__', status)
                self.assertIn('--variant=' + phase, runner.cli_command(self.work, self.state, phase))
                env = runner.build_environment(self.work, self.state, phase)
                self.assertEqual(env['SHOREBIRD_AOT_PATCH_KEY_ID'], self.state['key_ids'][phase])
        self.state['base_built'] = True
        native = self.work / 'app/macos/Runner/PatchTestKeys.swift'
        native.write_text(native.read_text() + '\n// changed')
        with self.assertRaisesRegex(ValueError, 'Released native sources changed'):
            runner.configure_native(self.work, self.state)

    def test_compiled_scope_requires_exact_original_baseline_and_signature_policy(self):
        app = self.work / 'compiled/Test.app'
        assets = app / 'Contents/Frameworks/App.framework/Versions/A/Resources/flutter_assets'
        assets.mkdir(parents=True)
        baseline = assets / 'shorebird_replacement_base.sbr'
        baseline.write_bytes(b'not executable; scope preflight test only')
        scope = {'app_id': self.state['app_id'], 'release_version': self.state['release_version'],
                 'platform': 'macos', 'arch': 'aarch64', 'runtime_profile': 'interpreter-full',
                 'base_artifact_hash': runner.v1.digest(baseline), 'host_abi_hash': 'a' * 64}
        path = assets / 'shorebird_release_scope_macos_aarch64.json'
        path.write_text(json.dumps(scope))
        policy = assets / 'shorebird.yaml'
        policy.write_text(runner.yaml_source(self.state) + 'patch_public_key: nonsecret-preflight-placeholder\n' +
                          'aot_patch_base_snapshot_hash: ' + scope['base_artifact_hash'] + '\n' +
                          'aot_patch_host_abi: ' + scope['host_abi_hash'] + '\n')
        runner.validate_compiled_scope(self.state, app)
        baseline.write_bytes(b'different original baseline')
        with self.assertRaisesRegex(ValueError, 'base_artifact_hash'):
            runner.validate_compiled_scope(self.state, app)
        baseline.write_bytes(b'not executable; scope preflight test only')
        policy.write_text(runner.yaml_source(self.state))
        with self.assertRaisesRegex(ValueError, 'signature requirement'):
            runner.validate_compiled_scope(self.state, app)

    def test_build_sequence_rejects_skips_before_running_cli(self):
        with mock.patch.object(runner.v1.shared, 'run') as run:
            for phase in ('pro', 'enterprise', 'pro_seed'):
                with self.assertRaisesRegex(ValueError, 'in order'):
                    runner.build_phase(self.work, self.state, phase)
            run.assert_not_called()

    def test_assignment_uses_real_observed_id_cas_and_lower_number(self):
        with self.assertRaisesRegex(ValueError, 'real installed updater ID'):
            runner.assign(self.work, self.state, 'pro')
        runner.remember_installation(self.work, self.state, {'installation_id': 'real-updater-installation'})
        self.state['assignments'] = [self.assignment('enterprise', '12')]
        def api(work, state, method, path, body=None):
            if method == 'GET':
                return {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
            self.assertEqual(path, '/releases/9/selections')
            self.assertEqual(body['expected_epoch'], '12')
            self.assertEqual(body['client_id'], 'real-updater-installation')
            self.assertEqual(body['arch'], 'aarch64')
            self.assertTrue(body['entitlement_revision'].startswith(state['session'] + ':'))
            uuid = __import__('uuid').UUID(body['idempotency_key'])
            self.assertEqual(uuid.version, 4)
            return self.assignment('pro', '13', installation=body['client_id'])
        with mock.patch.object(runner, 'api', side_effect=api):
            self.assertEqual(runner.assign(self.work, self.state, 'pro')['selection']['epoch'], '13')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            runner.remember_installation(self.work, self.state, {'installation_id': 'another-installation'})

    def test_uncertain_assignment_reuses_identical_persisted_idempotency_body(self):
        runner.remember_installation(self.work, self.state, {'installation_id': 'real-installation'})
        bodies = []
        def api(work, state, method, path, body=None):
            if method == 'GET':
                return {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
            bodies.append(copy.deepcopy(body))
            if len(bodies) == 1:
                raise OSError('Response lost after real server may have committed')
            return self.assignment('pro', '1')
        with mock.patch.object(runner, 'api', side_effect=api):
            with self.assertRaises(OSError):
                runner.assign(self.work, self.state, 'pro')
            self.assertIn('pending_assignment', runner.load(self.work))
            with self.assertRaisesRegex(ValueError, 'persisted idempotent assignment'):
                runner.assign(self.work, self.state, 'enterprise')
            # A committed paid grant may already have expired during the lost
            # response. Retrying the old idempotency key must still return its
            # original epoch, even after the updater observes server free=2.
            self.state['observed_selection'] = self.identity('free', '2', 'c' * 64)
            runner.v1.save(self.work, self.state)
            runner.assign(self.work, self.state, 'pro')
        self.assertEqual(bodies[0], bodies[1])
        self.assertNotIn('pending_assignment', runner.load(self.work))
        self.assertEqual(runner.latest_selection(self.state)['epoch'], '2')

    def identity(self, variant, epoch, digest='a' * 64):
        return {'epoch': epoch, 'digest': None if epoch == '0' else digest,
                'variant_id': variant, 'license_type': variant,
                'patch_number': runner.EXPECTED_NUMBERS[variant]}

    def assignment(self, variant, epoch, digest='a' * 64, installation='real-installation'):
        return {'selection': {'epoch': epoch, 'variant_id': variant, 'license_type': variant,
                             'scope': {'client_id_hash': hashlib.sha256(installation.encode()).hexdigest()},
                             'target': {'patch_number': runner.EXPECTED_NUMBERS[variant]}},
                'selection_digest': digest}

    def read_owned_report(self, report, **kwargs):
        path = self.work / 'synthetic-owned-report.json'
        runner.v1.shared.write_json(path, report)
        with mock.patch.object(runner.v1, 'container_file', return_value=path), \
                mock.patch.object(runner.v1, 'validate_status') as ffi:
            result = runner.read_report(self.work, self.state, after=self.state['created'],
                                        variant=report['variant'], **kwargs)
            ffi.assert_called_once()
            return result

    def test_initial_server_free_status_pins_epoch_for_first_paid_assignment(self):
        report = self.report('free', current_epoch='0', accepted_epoch='1')
        report['selection']['current']['digest'] = None
        self.read_owned_report(report, checked=True)
        self.assertEqual(runner.load(self.work)['observed_selection'], report['selection']['accepted'])
        cold = copy.deepcopy(report)
        cold['selection']['current'] = copy.deepcopy(cold['selection']['accepted'])
        cold['selection']['restart_required'] = False
        runner.validate_cold_activation(self.state, cold)
        def api(work, state, method, path, body=None):
            if method == 'GET':
                return {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
            self.assertEqual(body['expected_epoch'], '1')
            return self.assignment('pro', '2')
        with mock.patch.object(runner, 'api', side_effect=api):
            runner.assign(self.work, self.state, 'pro')

    def test_explicit_automatic_free_status_advances_cas_and_cold_expectation(self):
        self.state['assignments'] = [self.assignment('pro', '12')]
        report = self.report('pro', current_epoch='12', accepted_epoch='13')
        report['selection']['current']['digest'] = 'a' * 64
        report['selection']['accepted'] = self.identity('free', '13', 'c' * 64)
        report['selection']['next_boot']['variant_id'] = 'free'
        with self.assertRaisesRegex(ValueError, 'Latest signed assignment'):
            self.read_owned_report(report, checked=True)
        self.read_owned_report(report, checked=True, accepted_variant='free')
        self.assertEqual(runner.load(self.work)['observed_selection']['epoch'], '13')
        cold = self.report('free', current_epoch='13', accepted_epoch='13')
        cold['selection']['current']['digest'] = cold['selection']['accepted']['digest'] = 'c' * 64
        cold['selection']['restart_required'] = False
        runner.validate_cold_activation(self.state, cold)
        runner.validate_report(self.state, cold, after=self.state['created'], variant='free', checked=True)
        def api(work, state, method, path, body=None):
            if method == 'GET':
                return {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
            self.assertEqual(body['expected_epoch'], '13')
            return self.assignment('enterprise', '14')
        with mock.patch.object(runner, 'api', side_effect=api):
            runner.assign(self.work, self.state, 'enterprise')

    def test_observed_authorization_rejects_wrong_epoch_digest_and_unassigned_paid(self):
        self.state['assignments'] = [self.assignment('pro', '12')]
        self.state['observed_selection'] = self.identity('free', '13', 'c' * 64)
        report = self.report('free', current_epoch='13', accepted_epoch='13')
        report['selection']['current']['digest'] = report['selection']['accepted']['digest'] = 'c' * 64
        report['selection']['restart_required'] = False
        for change in ('older', 'jumped', 'digest', 'unassigned-paid'):
            altered = copy.deepcopy(report)
            if change == 'older':
                altered['selection']['accepted']['epoch'] = '12'
            elif change == 'jumped':
                altered['selection']['accepted']['epoch'] = '15'
            elif change == 'digest':
                altered['selection']['current']['digest'] = altered['selection']['accepted']['digest'] = 'd' * 64
            else:
                altered['selection']['accepted'] = self.identity('enterprise', '14')
                altered['selection']['next_boot']['variant_id'] = 'enterprise'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read_owned_report(altered, checked=True)
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_cold_activation(self.state, altered)
            self.assertEqual(self.state['observed_selection'], self.identity('free', '13', 'c' * 64))

    def test_rejected_ffi_report_does_not_advance_observed_authorization(self):
        report = self.report('free', current_epoch='0', accepted_epoch='1')
        report['selection']['current']['digest'] = None
        path = self.work / 'synthetic-owned-report.json'
        runner.v1.shared.write_json(path, report)
        with mock.patch.object(runner.v1, 'container_file', return_value=path), \
                mock.patch.object(runner.v1, 'validate_status', side_effect=ValueError('FFI incomplete')):
            with self.assertRaisesRegex(ValueError, 'FFI incomplete'):
                runner.read_report(self.work, self.state, after=self.state['created'], variant='free', checked=True)
        self.assertNotIn('observed_selection', runner.load(self.work))

    def test_assignment_rejects_wrong_epoch_and_invalid_or_conflicting_digest(self):
        runner.remember_installation(self.work, self.state, {'installation_id': 'real-installation'})
        responses = [self.assignment('pro', '2'), self.assignment('pro', '1', 'invalid-digest')]
        for response in responses:
            with self.subTest(response=response):
                def api(work, state, method, path, body=None):
                    if method == 'GET':
                        return {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
                    return response
                with mock.patch.object(runner, 'api', side_effect=api), self.assertRaises(ValueError):
                    runner.assign(self.work, self.state, 'pro')
                self.assertEqual(self.state['assignments'], [])
                self.assertEqual(runner.load(self.work)['pending_assignment']['expected_epoch'], '0')
        # When the lost response has since been seen through updater status,
        # its digest remains pinned even though the idempotent retry is older.
        self.state['observed_selection'] = self.identity('pro', '1', 'c' * 64)
        with mock.patch.object(runner, 'api', side_effect=lambda work, state, method, path, body=None:
                {'releases': [{'version': state['release_version'], 'id': 9, 'selection_protocol': 2}]}
                if method == 'GET' else self.assignment('pro', '1')), self.assertRaisesRegex(
                    ValueError, 'conflicts with a previously observed'):
            runner.assign(self.work, self.state, 'pro')
        self.assertEqual(self.state['assignments'], [])

    def report(self, variant='pro', current_epoch='11', accepted_epoch='12'):
        data = {'session': self.state['session'], 'sentinel': self.state['sentinel'], 'schema': 3,
                'launches': 4, 'history': ['free', 'pro', 'enterprise', variant],
                'pro_note': 'pro-preserved', 'enterprise_note': 'enterprise-preserved'}
        identity = {'epoch': current_epoch, 'digest': 'b' * 64, 'variant_id': variant,
                    'license_type': variant, 'patch_number': runner.EXPECTED_NUMBERS[variant]}
        accepted = {**identity, 'epoch': accepted_epoch, 'digest': 'a' * 64}
        return {'schema': runner.SCHEMA, 'session': self.state['session'], 'variant': variant,
                'new_code_result': {'free': 'free:base', 'pro': 'pro:42', 'enterprise': 'enterprise:24'}[variant],
                'installation_id': 'real-installation', 'pid': 1234, 'started': time.time(),
                'stage': 'checked', 'update_status': 'restartRequired', 'error': None, 'data': data,
                'selection': {'version': 2, 'current': identity, 'accepted': accepted,
                              'next_boot': {'variant_id': variant}, 'restart_required': True}}

    def test_same_content_new_epoch_requires_restart_and_preserves_enterprise_data(self):
        report = self.report()
        self.state['assignments'] = [self.assignment('pro', '12')]
        previous = {**report['data'], 'history': ['free', 'pro', 'enterprise'], 'launches': 3}
        args = dict(after=self.state['created'], variant='pro', previous_data=previous, checked=True)
        runner.validate_report(self.state, report, **args)
        for change in ('restart', 'data', 'epoch', 'foreign', 'stale', 'simulated'):
            altered = copy.deepcopy(report)
            if change == 'restart': altered['selection']['restart_required'] = False
            elif change == 'data': altered['data'].pop('enterprise_note')
            elif change == 'epoch': altered['selection']['accepted']['epoch'] = '10'
            elif change == 'foreign': altered['session'] = 'another'
            elif change == 'stale': altered['started'] = self.state['created'] - 1
            elif change == 'simulated': altered['schema'] = 'license-v1'
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_report(self.state, altered, **args)

    def test_cold_free_downgrade_forbids_previous_paid_fallback(self):
        report = self.report(variant='free', current_epoch='12')
        report['selection']['current']['digest'] = 'a' * 64
        report['selection']['restart_required'] = False
        self.state['assignments'] = [self.assignment('free', '12')]
        runner.validate_cold_activation(self.state, report)
        paid = copy.deepcopy(report)
        paid['variant'] = 'pro'
        paid['selection']['current']['patch_number'] = '2'
        with self.assertRaises(ValueError):
            runner.validate_cold_activation(self.state, paid)
        # Explicit missing-key/expiry fallback retains accepted paid epoch.
        self.state['assignments'] = [self.assignment('enterprise', '12')]
        report['selection']['accepted'] = self.identity('enterprise', '12')
        paid['selection']['accepted'] = self.identity('enterprise', '12')
        runner.validate_cold_activation(self.state, report, fallback=True)
        with self.assertRaises(ValueError):
            runner.validate_cold_activation(self.state, paid, fallback=True)

    def receipt(self, variant):
        return {'schema': 'macos-variant-key-control-v2', 'session': self.state['session'],
                'bundle_id': self.state['bundle_id'], 'service': self.state['key_service'],
                'account': self.state['key_ids'][variant], 'action': 'delete', 'result': 'deleted',
                'time': time.time(), 'pid': 123}

    def test_cleanup_fences_all_keys_and_never_accepts_old_or_foreign_receipt(self):
        offline = runner.app_environment(self.work, self.state)
        self.assertFalse(any(key.startswith('OPEN_SHOREBIRD_TEST_') for key in offline))
        env = runner.app_environment(self.work, self.state, variant='enterprise', action='provision')
        self.assertEqual(env['OPEN_SHOREBIRD_TEST_KEY_ID'], self.state['key_ids']['enterprise'])
        runner.begin_cleanup(self.work, self.state)
        with self.assertRaisesRegex(ValueError, 'Cleanup has started'):
            runner.app_environment(self.work, self.state, variant='pro', action='provision')
        runner.app_environment(self.work, self.state, variant='pro', action='delete', control_only=True)
        for variant in ('pro', 'enterprise'):
            receipt = self.receipt(variant)
            runner.validate_key_deletion(self.state, receipt, variant)
            for change in ({'account': 'foreign'}, {'time': self.state['created'] - 1},
                           {'action': 'provision'}, {'session': 'foreign'}, {'time': float('nan')}):
                with self.subTest(variant=variant, change=change), self.assertRaises(ValueError):
                    runner.validate_key_deletion(self.state, {**receipt, **change}, variant)

    def test_cleanup_requires_both_canonical_deletes_and_pins_retries(self):
        host_home = self.root / 'host-home'
        with mock.patch.object(runner.v1.Path, 'home', return_value=host_home), mock.patch.object(runner.v1, 'port_open', return_value=False):
            runner.begin_cleanup(self.work, self.state)
            canonical = {}
            for variant, account in self.state['key_ids'].items():
                canonical[variant] = runner.v1.container_file(self.state, 'key-control-' + account + '.json')
                canonical[variant].parent.mkdir(parents=True, exist_ok=True)
            runner.v1.shared.write_json(canonical['pro'], self.receipt('pro'))
            with self.assertRaises(FileNotFoundError):
                runner.cleanup(self.work, self.state)
            self.assertTrue((self.work / 'secrets.json').exists())
            runner.v1.shared.write_json(canonical['enterprise'], self.receipt('enterprise'))
            with mock.patch.object(runner.v1, 'remove_owned_container', side_effect=PermissionError('test payload failure')):
                with self.assertRaises(PermissionError):
                    runner.cleanup(self.work, self.state)
            self.assertIn('cleanup_in_progress', self.state)
            changed = self.receipt('enterprise')
            changed['action'] = 'provision'
            runner.v1.shared.write_json(canonical['enterprise'], changed)
            with self.assertRaisesRegex(ValueError, 'Latest native key control changed'):
                runner.cleanup(self.work, self.state)
            saved = Path(self.state['cleanup_in_progress']['receipts']['enterprise']['path'])
            canonical['enterprise'].write_bytes(saved.read_bytes())
            runner.cleanup(self.work, self.state)
            self.assertTrue(self.state['cleanup']['all_variant_keys_deleted'])
            self.assertFalse((self.work / 'secrets.json').exists())
            self.assertFalse(runner.v1.container_root(self.state).exists())

    def test_plan_cannot_be_mistaken_for_runtime_acceptance(self):
        plan = runner.plan()
        self.assertFalse(plan['runtime_acceptance'])
        self.assertFalse(plan['automatic_runtime_injection'])
        self.assertEqual(plan['cold_lifecycle'], ['free', 'pro', 'enterprise', 'pro', 'free', 'enterprise', 'free'])


if __name__ == '__main__':
    unittest.main()

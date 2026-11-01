"""Control and evidence tests only. No signing, application or server execution."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import macos_variant_lifecycle as runner


class LifecycleControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='macos-lifecycle-controls-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.state = {'released_app': str(self.work / 'released/Test.app'), 'session': 'test-session',
                      'app_id': '123-test', 'built_phases': list(runner.fixture.BUILD_ORDER),
                      'key_ids': {'pro': 'pro-test', 'enterprise': 'enterprise-test'},
                      'api_url': 'http://127.0.0.1:43101', 'report_url': 'http://127.0.0.1:43102/report',
                      'assignments': []}
        (self.work / 'secrets.json').write_text(json.dumps({'variant_keys': {'pro': 'a1' * 32, 'enterprise': 'b2' * 32}}))
        self.container = self.work / 'container'
        self.container.mkdir()
        self.root_patch = mock.patch.object(runner.v1, 'container_root', return_value=self.container)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.file_patch = mock.patch.object(runner.v1, 'container_file', side_effect=lambda _, name: self.container / name)
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)

    def create_runner(self):
        return runner.Lifecycle(self.work, copy.deepcopy(self.state), 'A' * 40, timeout=.001)

    def test_default_plan_does_not_read_or_mutate_fixture(self):
        with mock.patch.object(runner.fixture, 'load') as load, mock.patch.object(runner, 'Lifecycle') as lifecycle:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.main(['--work-dir', '/missing/unused'])
            self.assertFalse(json.loads(output.getvalue())['runtime_acceptance'])
            load.assert_not_called()
            lifecycle.assert_not_called()

    def test_fresh_gate_rejects_every_previous_runtime_marker(self):
        with mock.patch.object(runner.fixture, 'require_open'), mock.patch.object(runner.fixture, 'validate_compiled_scope'):
            for field, value in (('installation_id', 'installed'), ('assignments', [{'epoch': '1'}]),
                                 ('observed_selection', {'epoch': '1'}), ('pending_assignment', {'variant': 'pro'}),
                                 ('built_phases', ['base'])):
                state = dict(self.state, **{field: value})
                with self.subTest(field=field), self.assertRaises(ValueError):
                    runner.require_fresh_fixture(self.work, state)
            (self.work / 'runtime-old.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'evidence already exists'):
                runner.require_fresh_fixture(self.work, self.state)

    def test_server_readiness_requires_a_new_checkpoint_and_owns_stop(self):
        lifecycle = self.create_runner()
        lifecycle.state['serve_started'] = 1
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        lifecycle.owner = mock.Mock()
        lifecycle.owner.owned = {}
        lifecycle.owner.spawn.return_value = process
        states = iter([dict(self.state, serve_started=1), dict(self.state, serve_started=101)])
        def reload():
            lifecycle.state = next(states)
        lifecycle.reload = reload
        with mock.patch.object(lifecycle, 'require_offline') as offline, \
             mock.patch.object(runner.time, 'time', return_value=100), \
             mock.patch.object(runner.time, 'monotonic', side_effect=[1, 1, 1]), \
             mock.patch.object(runner.time, 'sleep'), mock.patch.object(runner.v1, 'port_open', return_value=True):
            lifecycle.start_server()
            self.assertIs(lifecycle.server, process)
            lifecycle.stop_server()
            lifecycle.owner.stop.assert_called_once_with(process)
            self.assertEqual(offline.call_count, 2)

    def test_offline_gate_checks_both_api_and_observer(self):
        lifecycle = self.create_runner()
        with mock.patch.object(runner.v1, 'port_open', side_effect=[False, True]) as port:
            with self.assertRaisesRegex(ValueError, 'observer endpoint'):
                lifecycle.require_offline()
            self.assertEqual(port.call_args_list, [mock.call(43101), mock.call(43102)])

    def test_lease_wait_crosses_wall_clock_deadline(self):
        now = mock.Mock(side_effect=[99, 100, 101, 101.1, 101.2])
        sleep = mock.Mock()
        self.assertGreater(runner.wait_past_deadline(100, now=now, sleep=sleep), 101)
        self.assertTrue(sleep.called)
        self.assertTrue(all(0 < call.args[0] <= 1 for call in sleep.call_args_list))

    def test_replay_bool_api_rejection_requires_unchanged_authority(self):
        accepted = {'epoch': '3', 'digest': 'a' * 64, 'variant_id': 'free', 'license_type': 'free', 'patch_number': None}
        report = {'stage': 'checked', 'update_status': 'upToDate', 'error': None,
                  'selection': {'accepted': accepted}}
        runner.validate_rejected_check(report, accepted)
        for field, value in (('error', 'Exception'), ('stage', 'boot'), ('update_status', 'outdated')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner.validate_rejected_check(dict(report, **{field: value}), accepted)
        with self.assertRaisesRegex(ValueError, 'changed authorization'):
            runner.validate_rejected_check(report, dict(accepted, epoch='4'))

    def test_cache_snapshot_rejects_keys_and_redirects_and_pins_journal(self):
        root = runner.cache_root(self.state)
        (root / 'patches/2').mkdir(parents=True)
        (root / 'selections-v2').mkdir()
        artifact = root / 'patches/2/dlc.vmcode'
        artifact.write_bytes(b'ciphertext')
        (root / 'selections-v2/journal.json').write_text('{"epoch":"2"}')
        before = runner.cache_snapshot(self.state)
        runner.assert_cache_preserved(before, runner.cache_snapshot(self.state))
        runner.assert_journal_preserved(before, runner.cache_snapshot(self.state))
        journal = root / 'selections-v2/journal.json'
        journal.write_text('{ "epoch": "2" }')
        with self.assertRaisesRegex(ValueError, 'journal bytes changed'):
            runner.assert_journal_preserved(before, runner.cache_snapshot(self.state))
        journal.write_text('{"epoch":"2"}')
        artifact.write_bytes(b'different ciphertext')
        with self.assertRaisesRegex(ValueError, 'changed ciphertext'):
            runner.assert_cache_preserved(before, runner.cache_snapshot(self.state))
        artifact.write_bytes(bytes.fromhex('a1' * 32))
        with self.assertRaisesRegex(ValueError, 'Plaintext'):
            runner.cache_snapshot(self.state, ('a1' * 32,))
        artifact.unlink()
        artifact.symlink_to(self.work / 'secrets.json')
        with self.assertRaisesRegex(ValueError, 'Redirected'):
            runner.cache_snapshot(self.state)

    def test_failed_launch_keeps_raw_reports_and_app_data_then_stops_owned_pid(self):
        lifecycle = self.create_runner()
        lifecycle.tree = {'unchanged': True}
        lifecycle.reload = mock.Mock()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        lifecycle.owner = mock.Mock()
        lifecycle.owner.owned = {}
        lifecycle.owner.spawn.return_value = process
        for name in ('variant-status.json', 'ffi-status.json', 'container.json', 'user-data.json'):
            (self.container / name).write_text('{"evidence":"keep even if invalid"}')
        original = (self.container / 'user-data.json').read_bytes()
        with mock.patch.object(runner.fixture, 'require_open'), \
             mock.patch.object(runner, 'bundle_tree', return_value=lifecycle.tree), \
             mock.patch.object(runner.v1, 'inspect_app', return_value={}), \
             mock.patch.object(runner.fixture, 'app_launch_command', return_value=['owned-app']), \
             mock.patch.object(runner.fixture, 'app_environment', return_value={}), \
             mock.patch.object(runner.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'No valid fresh report'):
                lifecycle.launch('failed-report', 'free')
        lifecycle.owner.stop.assert_called_once_with(process)
        self.assertEqual((self.container / 'user-data.json').read_bytes(), original)
        directory = self.work / 'lifecycle/failed-report'
        self.assertEqual((directory / 'user-data.json').read_bytes(), original)
        self.assertFalse(json.loads((directory / 'launch.json').read_text())['validated'])
        self.assertIn('raw_reports', lifecycle.launches[-1])

    def test_cleanup_fences_then_deletes_both_keys_and_preserves_incomplete_os_cleanup(self):
        lifecycle = self.create_runner()
        lifecycle.tree = {'signed': True}
        lifecycle.reload = mock.Mock()
        lifecycle.owner = mock.Mock()
        order = []
        def fence(*_):
            order.append('fence')
            lifecycle.state['cleanup_fence'] = 100
        lifecycle.key = lambda action, variant, **_: order.append(action + '-' + variant)
        incomplete = {'cleanup_complete': False, 'container_payload_removed': True, 'metadata_removal_errno': 1}
        with mock.patch.object(lifecycle, 'stop_server'), \
             mock.patch.object(runner.fixture, 'begin_cleanup', side_effect=fence), \
             mock.patch.object(runner.fixture, 'cleanup', return_value=incomplete):
            self.assertEqual(lifecycle.cleanup(), [])
        self.assertEqual(order, ['fence', 'delete-pro', 'delete-enterprise'])
        self.assertFalse(lifecycle.result['cleanup']['cleanup_complete'])

    def test_successful_launch_pins_pid_ffi_data_and_independent_raw_reports(self):
        lifecycle = self.create_runner()
        lifecycle.tree = {'unchanged': True}
        lifecycle.reload = mock.Mock()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        lifecycle.owner = mock.Mock()
        lifecycle.owner.owned = {}
        lifecycle.owner.spawn.return_value = process
        accepted = {'epoch': '1', 'digest': 'a' * 64, 'variant_id': 'free',
                    'license_type': 'free', 'patch_number': None}
        data = {'launches': 1, 'history': ['free']}
        report = {'pid': 123, 'data': data, 'selection': {'accepted': accepted}}
        for name in ('variant-status.json', 'ffi-status.json', 'container.json', 'user-data.json'):
            (self.container / name).write_text('{}')
        with mock.patch.object(runner.fixture, 'require_open'), \
             mock.patch.object(runner, 'bundle_tree', return_value=lifecycle.tree), \
             mock.patch.object(runner.v1, 'inspect_app', return_value={'signatures': ['mocked control only']}), \
             mock.patch.object(runner.fixture, 'app_launch_command', return_value=['owned-app']), \
             mock.patch.object(runner.fixture, 'app_environment', return_value={}), \
             mock.patch.object(runner.time, 'time', return_value=100), \
             mock.patch.object(runner.fixture, 'validate_report', return_value=report), \
             mock.patch.object(runner.v1, 'validate_container_record', return_value={'pid': 123}), \
             mock.patch.object(runner.v1, 'validate_status', return_value={'mocked_ffi': True}) as ffi, \
             mock.patch.object(runner.fixture, 'remember_installation') as remember, \
             mock.patch.object(runner.v1, 'save'):
            result = lifecycle.launch('valid-report', 'free')
        self.assertTrue(result['validated'])
        self.assertTrue(result['signed_bundle_unchanged'])
        self.assertEqual(result['pid'], 123)
        self.assertEqual(lifecycle.previous_data, data)
        self.assertEqual(lifecycle.state['observed_selection'], accepted)
        self.assertEqual(len(result['raw_reports']), 4)
        self.assertEqual(ffi.call_args.args[-1], 'base')
        remember.assert_called_once()
        lifecycle.owner.stop.assert_called_once_with(process)
        # A unit-controlled successful launch must not certify real runtime.
        self.assertFalse(lifecycle.result['runtime_acceptance'])

    def test_tamper_recovery_never_restores_cache_files(self):
        lifecycle = self.create_runner()
        root = runner.cache_root(self.state)
        (root / 'patches/2').mkdir(parents=True)
        (root / 'selections-v2').mkdir()
        (root / 'selections-v2/journal.json').write_text('{"epoch":"2"}')
        artifact = root / 'patches/2/dlc.vmcode'
        original = b'{"encrypted_payload_base64":"AAAA"}'
        artifact.write_bytes(original)
        lifecycle.launch = mock.Mock(return_value={'cache': runner.cache_snapshot(self.state)})
        lifecycle.start_server = mock.Mock()
        lifecycle.stop_server = mock.Mock()
        lifecycle.switch = mock.Mock(side_effect=[{}, RuntimeError('quarantine prevents repair')])
        with self.assertRaisesRegex(RuntimeError, 'quarantine prevents repair'):
            lifecycle.tamper('ciphertext')
        self.assertNotEqual(artifact.read_bytes(), original)
        self.assertEqual(lifecycle.switch.call_args_list, [
            mock.call('free', 'free', label='ciphertext-explicit-free'),
            mock.call('pro', 'free', label='ciphertext-reauthorized-pro')])


if __name__ == '__main__':
    unittest.main()

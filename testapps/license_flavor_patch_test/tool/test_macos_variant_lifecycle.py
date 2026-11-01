"""Control and bounded HTTP socket units; no signing, app or Go server execution."""
import contextlib
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import socket
import threading
import time
import tempfile
import types
import unittest
import uuid
from unittest import mock

import macos_variant_lifecycle as runner


class LifecycleControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='macos-lifecycle-controls-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.state = {'released_app': str(self.work / 'released/Test.app'), 'session': 'test-session',
                      'app_id': '123-test', 'built_phases': list(runner.fixture.BUILD_ORDER),
                      'bundle_id':'test.fixture.bundle','release_version':'1.0.0+1',
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

    def replay_fixture(self):
        lifecycle=self.create_runner()
        with socket.socket() as api, socket.socket() as observer:
            api.bind(('127.0.0.1',0)); observer.bind(('127.0.0.1',0))
            lifecycle.state.update(api_url=f'http://127.0.0.1:{api.getsockname()[1]}',
                report_url=f'http://127.0.0.1:{observer.getsockname()[1]}/report')
        lifecycle.state.update(release_version='1.0.0+1',installation_id='actual-installation',
            release_scope={'base_artifact_hash':'b'*64,'host_abi_hash':'c'*64,'runtime_profile':'interpreter-full'})
        captured_nonce=str(uuid.uuid4())
        body=json.dumps({'authentication':{'payload':base64.b64encode(json.dumps(
            {'request':{'nonce':captured_nonce}}).encode()).decode()}}).encode()
        old={'epoch':'1','digest':'a'*64,'variant_id':'pro','license_type':'pro','patch_number':'2'}
        accepted=dict(old,epoch='2',digest='d'*64)
        request=dict(selection_protocol=2,app_id=lifecycle.state['app_id'],release_version='1.0.0+1',
            platform='macos',arch='aarch64',channel='stable',client_id='actual-installation',nonce=str(uuid.uuid4()),
            base_artifact_hash='b'*64,host_abi_hash='c'*64,runtime_profile='interpreter-full',
            accepted_selection_epoch='2',accepted_selection_digest='d'*64)
        return lifecycle,body,old,accepted,request,captured_nonce

    def test_actual_replay_delivery_binds_complete_body_fresh_request_and_high_water(self):
        import urllib.request
        lifecycle,body,old,accepted,request,captured_nonce=self.replay_fixture()
        records=[]; after=time.time()
        raw=json.dumps(request,indent=1).encode()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with lifecycle.replay_endpoint(body,records):
            with opener.open(urllib.request.Request(lifecycle.state['api_url']+'/api/v1/patches/check',
                    raw,method='POST'),timeout=2) as response:
                self.assertEqual(response.read(),body)
        before=time.time()
        runner.validate_replay_delivery(lifecycle.state,records,body,old,accepted,after=after,before=before)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['request_body_sha256'],hashlib.sha256(raw).hexdigest())
        for fault in ('client','scope','epoch','digest','old-nonce','request-body','response-body','before-launch','after-launch','duplicate'):
            bad=copy.deepcopy(records); record=bad[0]
            if fault in ('client','scope','epoch','digest','old-nonce'):
                field={'client':'client_id','scope':'host_abi_hash','epoch':'accepted_selection_epoch',
                       'digest':'accepted_selection_digest','old-nonce':'nonce'}[fault]
                record['request'][field]=captured_nonce if fault=='old-nonce' else 'wrong'
                changed=json.dumps(record['request']).encode()
                record['request_body_base64']=base64.b64encode(changed).decode()
                record['request_body_sha256']=hashlib.sha256(changed).hexdigest()
            elif fault=='request-body': record['request_body_sha256']='e'*64
            elif fault=='response-body': record['response_body_sha256']='e'*64
            elif fault=='before-launch': record['received_at']=after-1
            elif fault=='after-launch': record['completed_at']=before+1
            else: bad.append(copy.deepcopy(record))
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                runner.validate_replay_delivery(lifecycle.state,bad,body,old,accepted,after=after,before=before)
        with self.assertRaises(ValueError):
            runner.validate_replay_delivery(lifecycle.state,[],body,old,accepted,after=after,before=before)

    def test_actual_replay_cleanup_closes_stalled_headers_without_waiting_for_peer(self):
        lifecycle,body,*_=self.replay_fixture()
        ready=threading.Event(); release=threading.Event(); done=threading.Event(); errors=[]; records=[]
        def listen():
            try:
                with lifecycle.replay_endpoint(body,records):
                    ready.set(); release.wait(3)
            except Exception as error: errors.append(error)
            finally: done.set()
        worker=threading.Thread(target=listen,daemon=True); worker.start(); connection=None
        try:
            self.assertTrue(ready.wait(2))
            connection=socket.create_connection(('127.0.0.1',lifecycle.ports()[0]),timeout=1)
            connection.sendall(b'POST /api/v1/patches/check HTTP/1.1\r\nHost: localhost\r\n')
            connection.settimeout(.1)
            with self.assertRaises(TimeoutError): connection.recv(1)
            release.set()
            self.assertTrue(done.wait(2),'Replay cleanup blocked waiting for incomplete headers')
            self.assertEqual(errors,[]); self.assertEqual(records,[])
        finally:
            release.set()
            if connection: connection.close()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_actual_replay_body_write_or_flush_failure_never_counts_as_delivery(self):
        import http.client
        import socketserver
        import urllib.request
        lifecycle,body,_,_,request,_=self.replay_fixture()
        original_write=socketserver._SocketWriter.write
        original_flush=socketserver._SocketWriter.flush
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for fault in ('write','flush'):
            records=[]; attempted=threading.Event(); written=set()
            def write(stream,data):
                if data==body:
                    if fault=='write':
                        attempted.set(); raise BrokenPipeError('Injected body failure')
                    written.add(id(stream))
                return original_write(stream,data)
            def flush(stream):
                if id(stream) in written:
                    written.remove(id(stream))
                    attempted.set(); raise OSError('Injected flush failure')
                return original_flush(stream)
            with self.subTest(fault=fault),mock.patch.object(socketserver._SocketWriter,'write',write), \
                 mock.patch.object(socketserver._SocketWriter,'flush',flush):
                with lifecycle.replay_endpoint(body,records):
                    with opener.open(urllib.request.Request(lifecycle.state['api_url']+'/api/v1/patches/check',
                            json.dumps(request).encode(),method='POST'),timeout=2) as response:
                        if fault=='write':
                            with self.assertRaises(http.client.IncompleteRead): response.read()
                        else: self.assertEqual(response.read(),body)
                    self.assertTrue(attempted.wait(1))
                self.assertEqual(records,[],'A failed body write/flush counted as replay delivery')

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
        report = {'pid': 123, 'data': data,'installation_id':'actual-installation',
                  'selection': {'accepted': accepted,'current':accepted}}
        for name in ('variant-status.json', 'ffi-status.json', 'container.json', 'user-data.json'):
            (self.container / name).write_text('{}')
        def spawn(*args,**kwargs):
            ledger=self.provider_ledger(report,nonce=kwargs['env']['OPEN_SHOREBIRD_TEST_PROVIDER_NONCE'])
            (self.container/'provider.json').write_text(json.dumps(ledger))
            return process
        lifecycle.owner.spawn.side_effect=spawn
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
        self.assertEqual(len(result['raw_reports']), 5)
        self.assertEqual(result['provider_counts'],{'attempts':0,'successful_releases':0,'refused':0})
        self.assertEqual(ffi.call_args.args[-1], 'base')
        remember.assert_called_once()
        lifecycle.owner.stop.assert_called_once_with(process)
        # A unit-controlled successful launch must not certify real runtime.
        self.assertFalse(lifecycle.result['runtime_acceptance'])

    def provider_ledger(self,report,*,nonce='launch-nonce',events=None):
        return {'schema':'macos-variant-provider-ledger-v2','session':self.state['session'],
            'bundle_id':self.state['bundle_id'],'pid':123,'nonce':nonce,'started':100,'time':101,
            'events':events or [],'overflow':False}

    def test_fresh_provider_ledger_exact_identity_and_distinct_key_failure_semantics(self):
        paid={'epoch':'2','digest':'a'*64,'variant_id':'pro','license_type':'pro','patch_number':'2'}
        free=dict(paid,variant_id='free',license_type='free',patch_number=None)
        report={'installation_id':'actual-installation','selection':{'current':paid,'accepted':paid}}
        event={'sequence':1,'outcome':'released','reason':'key_available','time':100.5,
            'scope':{'app_id':self.state['app_id'],'release_version':self.state['release_version'],
                'platform':'macos','arch':'aarch64','runtime_profile':'interpreter-full',
                'client_id_hash':hashlib.sha256(b'actual-installation').hexdigest()},
            'epoch':'2','selection_digest':'a'*64,'variant_id':'pro','license_type':'pro',
            'patch_number':'2','key_id':self.state['key_ids']['pro']}
        ledger=self.provider_ledger(report,events=[event])
        def validate(value,receipt=report,**kwargs):
            return runner.validate_provider_ledger(self.state,receipt,value,pid=123,nonce='launch-nonce',after=99,**kwargs)
        self.assertEqual(validate(ledger)['successful_releases'],1)
        for field,value in (('pid',124),('nonce','prior-launch'),('session','prior-session'),('started',98),('overflow',True)):
            with self.subTest(field=field),self.assertRaises(ValueError): validate(dict(ledger,**{field:value}))
        for field,value in (('epoch','1'),('selection_digest','b'*64),('key_id','foreign-key'),('sequence',2),('private_key','secret')):
            bad=copy.deepcopy(ledger); bad['events'][0][field]=value
            with self.subTest(event=field),self.assertRaises(ValueError): validate(bad)
        fallback=dict(report,selection={'current':free,'accepted':paid})
        empty=self.provider_ledger(fallback)
        self.assertEqual(validate(empty,receipt=fallback)['successful_releases'],0)
        with self.assertRaises(ValueError): validate(ledger,receipt=fallback)
        # A valid 32-byte wrong key is released, then rejected by the engine.
        self.assertEqual(validate(ledger,receipt=fallback,key_failure='wrong')['successful_releases'],1)
        with self.assertRaises(ValueError): validate(empty,receipt=fallback,key_failure='wrong')
        refusal=copy.deepcopy(ledger)
        refusal['events'][0].update(outcome='refused',reason='key_unavailable')
        self.assertEqual(validate(refusal,receipt=fallback,key_failure='missing')['refused'],1)
        with self.assertRaises(ValueError): validate(ledger,receipt=fallback,key_failure='missing')
        with self.assertRaises(ValueError): validate(refusal,receipt=fallback)

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

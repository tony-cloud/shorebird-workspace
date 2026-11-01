#!/usr/bin/env python3
"""Run an already-built disposable macOS protocol-2 fixture.

Default is a read-free plan. --execute signs (or verifies a pre-signed bundle)
and launches only the private app in --work-dir, uses real server assignments,
and tears down its own resources.
No engine/build/device operation exists here. Unit tests are not acceptance.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from contextlib import contextmanager
import hashlib
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import platform
from pathlib import Path
import re
import socket
import sys
import threading
import time
import uuid

import macos_variant_e2e as fixture
from owned_host_processes import OwnedHostProcesses

v1 = fixture.v1
SCHEMA = 'macos-variant-lifecycle-v2'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def require_native_host(state):
    """Execution is native-only; cross-target local preparation is separate."""
    target = v1.target_architecture(state)
    require(sys.platform == 'darwin' and platform.machine() == target['machine'],
            'Lifecycle execution requires a native ' + target['machine'] + ' Mac')
    try:
        system = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        probe = system.sysctlbyname
        probe.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                          ctypes.c_void_p, ctypes.c_size_t]
        probe.restype = ctypes.c_int
        translated, size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        ctypes.set_errno(0)
        result = probe(b'sysctl.proc_translated', ctypes.byref(translated), ctypes.byref(size), None, 0)
        if result == -1 and ctypes.get_errno() == errno.ENOENT:
            return  # Apple's documented result when translation is unavailable.
        require(result == 0 and size.value == ctypes.sizeof(ctypes.c_int) and translated.value == 0,
                'Rosetta or an invalid translation probe cannot establish native lifecycle proof')
    except (OSError, AttributeError):
        raise ValueError('Native lifecycle translation status could not be verified') from None


def bundle_tree(app):
    result = {}
    for directory, subdirs, names in os.walk(app, followlinks=False):
        for name in [*subdirs, *names]:
            path = Path(directory) / name
            relative = str(path.relative_to(app))
            if path.is_symlink():
                result[relative] = {'symlink': os.readlink(path)}
            elif path.is_file():
                result[relative] = {'sha256': v1.digest(path), 'mode': path.stat().st_mode & 0o777}
    return result


def cache_root(state):
    require(re.fullmatch(r'[A-Za-z0-9-]+', state['app_id']) is not None, 'Invalid cache app ID')
    path = v1.container_root(state) / 'Data/Library/Application Support/shorebird/shorebird_updater' / state['app_id']
    require(path.resolve() == path.absolute(), 'Redirected updater cache')
    return path


def cache_snapshot(state, keys=()):
    root = cache_root(state)
    result = {'files': {}, 'journals': {}, 'plaintext_keys_present': False}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Redirected updater cache entry')
        if not path.is_file():
            continue
        data = path.read_bytes()
        name = str(path.relative_to(root))
        require(not any(bytes.fromhex(key) in data or key.encode() in data for key in keys),
                'Plaintext artifact key found in updater cache')
        result['files'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        if 'selections-v2' in path.relative_to(root).parts:
            result['journals'][name] = json.loads(data)
    return result


def assert_cache_preserved(before, after):
    names = {name for name in before['files'] if name.startswith('patches/') or 'selections-v2/' in name}
    require(names, 'No ciphertext/journal evidence to compare')
    require(any(name.startswith('selections-v2/') for name in names), 'Missing authorization journal')
    require(all(after['files'].get(name) == before['files'][name] for name in names),
            'Key failure/recovery changed ciphertext, metadata or authorization journal')


def assert_journal_preserved(before, after):
    files = {name: value for name, value in before['files'].items() if 'selections-v2/' in name}
    require(bool(files), 'Missing authorization journal evidence')
    current = {name: value for name, value in after['files'].items() if 'selections-v2/' in name}
    require(current == files, 'Authorization journal bytes changed')


def validate_rejected_check(report, accepted):
    require(report['stage'] == 'checked' and report['update_status'] == 'upToDate' and report['error'] is None,
            'Rejected response must leave public bool check API upToDate without an exception')
    require(fixture.selection_identity(report['selection']['accepted']) == accepted,
            'Rejected response changed authorization')


def validate_replay_delivery(state, records, body, old_selection, accepted, *, after, before):
    """Bind completed HTTP writes to fresh actual requests and authority."""
    require(bool(records) and int(old_selection['epoch']) < int(accepted['epoch']),
            'Replay did not target a newer accepted authorization')
    old = json.loads(body)
    old_payload = json.loads(base64.b64decode(old['authentication']['payload'], validate=True))
    captured_nonce = old_payload['request']['nonce']
    scope = state['release_scope']
    expected = dict(selection_protocol=2, app_id=state['app_id'], release_version=state['release_version'],
        platform='macos', arch=v1.target_architecture(state)['wire'], channel='stable', client_id=state['installation_id'],
        base_artifact_hash=scope['base_artifact_hash'], host_abi_hash=scope['host_abi_hash'],
        runtime_profile=scope['runtime_profile'], accepted_selection_epoch=accepted['epoch'],
        accepted_selection_digest=accepted['digest'])
    nonces = set()
    for record in records:
        request = record['request']
        nonce = request['nonce']
        require(isinstance(nonce, str) and str(uuid.UUID(nonce)) == nonce and uuid.UUID(nonce).version == 4 and
                nonce != captured_nonce and nonce not in nonces, 'Replay request did not use a fresh updater nonce')
        nonces.add(nonce)
        raw = base64.b64decode(record['request_body_base64'], validate=True)
        require(json.loads(raw) == request and hashlib.sha256(raw).hexdigest() == record['request_body_sha256'] and
                record['response_body_sha256'] == hashlib.sha256(body).hexdigest(),
                'Replay evidence does not match actual request/response body bytes')
        require(all(request.get(key) == value for key, value in expected.items()),
                'Replay request differs from actual installation/scope/accepted high-water mark')
        received, completed = record['received_at'], record['completed_at']
        require(type(received) in (int, float) and type(completed) in (int, float) and
                math.isfinite(received) and math.isfinite(completed) and after <= received <= completed <= before,
                'Replay response was not completed during this fresh app launch')
    return records


def validate_provider_ledger(state, report, ledger, *, pid, nonce, after, key_failure=None):
    require(key_failure in (None, 'missing', 'wrong'), 'Invalid provider failure control')
    require(type(ledger) is dict and set(ledger) == {'schema','session','bundle_id','pid','nonce','started','time','events','overflow'} and
            ledger['schema'] == 'macos-variant-provider-ledger-v2' and ledger['session'] == state['session'] and
            ledger['bundle_id'] == state['bundle_id'] and ledger['pid'] == pid and type(ledger['pid']) is int and
            ledger['nonce'] == nonce and ledger['overflow'] is False, 'Stale or foreign provider ledger')
    require(all(type(ledger[key]) in (int,float) and math.isfinite(ledger[key]) for key in ('started','time')) and
            after <= ledger['started'] <= ledger['time'] <= time.time()+2, 'Invalid provider ledger launch fence')
    events = ledger['events']
    require(type(events) is list and len(events) <= 128, 'Unbounded provider ledger')
    current = fixture.selection_identity(report['selection']['current'])
    accepted = fixture.selection_identity(report['selection']['accepted'])
    expected = accepted if key_failure else current
    scope = dict(app_id=state['app_id'],release_version=state['release_version'],platform='macos',arch=v1.target_architecture(state)['wire'],
        runtime_profile='interpreter-full',client_id_hash=hashlib.sha256(report['installation_id'].encode()).hexdigest())
    for index,event in enumerate(events):
        require(type(event) is dict and event.get('sequence') == index+1 and type(event['sequence']) is int and
                event.get('outcome') in ('released','refused') and
                event.get('reason') in ('key_available','key_unavailable','context_rejected') and
                type(event.get('time')) in (int,float) and math.isfinite(event['time']) and
                ledger['started'] <= event['time'] <= ledger['time'], 'Invalid provider event')
        base = {'sequence','outcome','reason','time'}
        if event['reason'] == 'context_rejected':
            require(set(event) == base and event['outcome'] == 'refused', 'Rejected context leaked unvalidated identity')
        else:
            require(set(event) == base | {'scope','epoch','selection_digest','variant_id','license_type','patch_number','key_id'} and
                    event['scope'] == scope and event['key_id'] == state['key_ids'].get(expected['variant_id']) and
                    fixture.selection_identity(dict(epoch=event['epoch'],digest=event['selection_digest'],
                        variant_id=event['variant_id'],license_type=event['license_type'],patch_number=event['patch_number'])) == expected and
                    event['outcome'] == ('released' if event['reason'] == 'key_available' else 'refused'),
                    'Provider event differs from actual process-current authorization')
    releases = [event for event in events if event['outcome'] == 'released']
    if key_failure == 'missing':
        require(current['variant_id'] == 'free' and accepted['variant_id'] != 'free' and bool(events) and
                all(event['reason'] == 'key_unavailable' for event in events), 'Missing key did not produce native refusals')
    elif key_failure == 'wrong':
        require(current['variant_id'] == 'free' and accepted['variant_id'] != 'free' and bool(releases) and
                len(releases) == len(events), 'Wrong-key fallback did not exercise actual 32-byte key release')
    elif current['variant_id'] == 'free':
        require(not events, 'Effective BASE attempted paid provider key access')
    else:
        require(bool(releases) and len(releases) == len(events), 'Paid process lacks matching native provider releases')
    return {'attempts':len(events),'successful_releases':len(releases),'refused':len(events)-len(releases)}


def wait_past_deadline(deadline, *, now=time.time, sleep=time.sleep):
    require(type(deadline) in (int, float) and math.isfinite(deadline), 'Invalid lease deadline')
    # Use wall clock, because authorization expiry uses wall clock. A monotonic
    # duration calculated before other work is not proof of post-expiry launch.
    current = now()
    while current <= deadline + 1:
        sleep(min(1, max(.01, deadline + 1.01 - current)))
        current = now()
    return current


def require_fresh_fixture(work, state, *, preserve_scope=False):
    fixture.require_open(work, state)
    require(state.get('built_phases') == list(fixture.BUILD_ORDER), 'Build all four fixture phases first')
    require(not state.get('installation_id') and not state.get('assignments') and
            not state.get('observed_selection') and not state.get('pending_assignment'),
            'Lifecycle requires a fresh fixture with no installation or assignments')
    require(not (work / 'lifecycle').exists() and not list(work.glob('runtime-*.json')),
            'Lifecycle evidence already exists; use a fresh fixture')
    require(not cache_root(state).exists(), 'Updater cache already exists; use a fresh fixture')
    for name in ('variant-status.json', 'ffi-status.json', 'user-data.json', 'container.json', 'provider.json'):
        require(not v1.container_file(state, name).exists(), 'Existing runtime evidence; use a fresh fixture')
    app = v1.contained(work, state['released_app'])
    require(app.is_dir() and not app.is_symlink(), 'Missing private released app')
    require(not app.is_relative_to(v1.ROOT), 'Released app must be outside the source workspace')
    if preserve_scope:
        require(isinstance(state.get('release_scope'),dict), 'Pre-signed app requires its original pinned release scope')
        actual = fixture.validate_compiled_scope(dict(state), app)
        require(actual == state['release_scope'], 'Pre-signed app differs from its original pinned release scope')
    else:
        fixture.validate_compiled_scope(state, app)
    return app


class Lifecycle:
    def __init__(self, work, state, identity, *, timeout=60, lease_seconds=90, verify_presigned=False):
        require_native_host(state)
        require(type(verify_presigned) is bool, 'Invalid app signing mode')
        require(re.fullmatch(r'[a-fA-F0-9]{40}', identity or '') is not None,
                'Pin the expected Developer ID certificate SHA-1')
        self.work, self.state, self.identity = work, state, identity
        self.verify_presigned = verify_presigned
        self.app = Path(state['released_app'])
        self.evidence = work / 'lifecycle'
        self.evidence.mkdir(mode=0o700)
        self.owner = OwnedHostProcesses()
        self.server = None
        self.server_log = None
        self.timeout, self.lease_seconds = timeout, lease_seconds
        self.previous_data = None
        self.tree = None
        self.launches = []
        self.keys = tuple(json.loads((work / 'secrets.json').read_text())['variant_keys'].values())
        self.result = {'schema': SCHEMA, 'runtime_acceptance': False, 'launches': self.launches,
                       'signing_mode': 'verify_presigned' if verify_presigned else 'sign_locally',
                       'bundle_verified': False,
                       'work_dir': str(work), 'session': state['session']}
        self.save()

    def write(self, name, value):
        v1.shared.write_json(self.evidence / name, value)

    def save(self):
        self.write('result.json', self.result)

    def reload(self):
        self.state = fixture.load(self.work)

    def ports(self):
        return [v1.urlparse(self.state[name]).port for name in ('api_url', 'report_url')]

    def require_offline(self):
        require(not any(v1.port_open(port) for port in self.ports()), 'Offline API/observer endpoint is reachable')

    def sign(self):
        self.tree = None
        self.result['bundle_verified'] = False
        before = None
        if self.verify_presigned:
            require(self.app.is_dir() and not self.app.is_symlink(), 'Missing private pre-signed app')
            v1.app_identity(self.work, self.state, self.app)
            before = bundle_tree(self.app)
            require(bool(before) and isinstance(self.state.get('release_scope'),dict),
                    'Pre-signed app requires its original pinned release scope')
            actual = fixture.validate_compiled_scope(dict(self.state), self.app)
            require(actual == self.state['release_scope'], 'Pre-signed app differs from its original pinned release scope')
        else:
            for index, command in enumerate(v1.signing_plan(self.work, self.state, self.app, self.identity)):
                v1.checked(command, self.evidence / f'sign-{index}.log')
        inspection = v1.inspect_app(self.work, self.state, self.app, self.identity)
        after = bundle_tree(self.app)
        if self.verify_presigned:
            require(after == before, 'Pre-signed app changed during read-only verification')
        self.write('signed-bundle.json', inspection)
        self.tree = after
        self.write('signed-bundle-tree.json', self.tree)
        self.result['bundle_verified'] = True
        self.save()

    def start_server(self):
        require(self.server is None, 'Server already owned')
        self.require_offline()
        previous = self.state.get('serve_started')
        started = time.time()
        self.server_log = (self.evidence / 'server-controller.log').open('ab')
        self.server = self.owner.spawn([sys.executable, str(Path(fixture.__file__).resolve()),
            'serve', '--execute', '--work-dir', str(self.work)], stdout=self.server_log, stderr=self.server_log)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            require(self.server.poll() is None, 'Owned server exited before readiness')
            self.reload()
            ready = self.state.get('serve_started')
            if ready != previous and isinstance(ready, (int, float)) and ready >= started:
                require(v1.port_open(self.ports()[0]), 'Ready server API is unavailable')
                return
            time.sleep(.1)
        raise RuntimeError('No fresh serve_started readiness checkpoint')

    def stop_server(self):
        if self.server is not None:
            self.owner.stop(self.server)
            self.server = None
        if self.server_log is not None:
            self.server_log.close()
            self.server_log = None
        self.require_offline()

    def raw_reports(self, directory):
        """Keep diagnostics even when validation fails; never reset app data."""
        captured = {}
        for name in ('variant-status.json', 'ffi-status.json', 'container.json', 'user-data.json', 'provider.json'):
            path = v1.container_file(self.state, name)
            if path.exists():
                require(not path.is_symlink() and path.stat().st_size <= 1024 * 1024, 'Unsafe report snapshot')
                data = path.read_bytes()
                v1.shared.private_write(directory / name, data)
                captured[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'mtime': path.stat().st_mtime}
        return captured

    def launch(self, label, variant=None, *, check=False, cold=False, fallback=False,
               offline=False, accepted_variant=None, rejected=False, key=None,
               key_variant=None, wrong=False, key_failure=None):
        require(re.fullmatch(r'[A-Za-z0-9_-]+', label) is not None, 'Unsafe launch label')
        self.reload()
        fixture.require_open(self.work, self.state, deletion=key == 'delete')
        directory = self.evidence / label
        directory.mkdir(mode=0o700)  # Refuse overwriting an earlier attempt.
        entry = {'label': label, 'started': time.time(), 'expected_variant': variant,
                 'offline_endpoints_closed': offline, 'validated': False}
        self.launches.append(entry)
        nonce = uuid.uuid4().hex
        if key is None:
            entry['provider_nonce'] = nonce
        process = None
        error = None
        try:
            require(all(owned is self.server for owned in self.owner.owned.values()),
                    'A previous owned app has not finished cleanup')
            require(bundle_tree(self.app) == self.tree, 'Signed bundle changed before launch')
            self.write(label + '/signature.json', v1.inspect_app(self.work, self.state, self.app, self.identity))
            if offline:
                self.require_offline()
            with (directory / 'app.log').open('wb') as log:
                environment = fixture.app_environment(self.work, self.state, variant=key_variant, action=key,
                    control_only=key is not None, wrong_key=wrong, check=check)
                if key is None:
                    environment['OPEN_SHOREBIRD_TEST_PROVIDER_NONCE'] = nonce
                process = self.owner.spawn(fixture.app_launch_command(self.work, self.state, deletion=key == 'delete'),
                    env=environment, stdout=log, stderr=log)
                entry['pid'] = process.pid
                if key is not None:
                    require(process.wait(timeout=30) == 0, 'Native key control failed')
                    receipt = json.loads(v1.container_file(self.state, 'key-control-' + self.state['key_ids'][key_variant] + '.json').read_text())
                    require(receipt['time'] >= entry['started'] and receipt['pid'] == process.pid and
                            receipt['action'] == key and receipt['account'] == self.state['key_ids'][key_variant] and
                            receipt['session'] == self.state['session'] and
                            receipt['schema'] == 'macos-variant-key-control-v2' and
                            receipt['bundle_id'] == self.state['bundle_id'] and
                            receipt['service'] == self.state['key_service'] and
                            receipt['time'] <= time.time() + 2 and
                            receipt['result'] in (('deleted', 'absent') if key == 'delete' else ('stored',)),
                            'Stale or foreign native key receipt')
                    if self.state.get('cleanup_fence'):
                        fixture.validate_key_deletion(self.state, receipt, key_variant)
                    entry['key_control'] = receipt
                    self.write(label + '/key-control.json', receipt)
                else:
                    deadline = time.monotonic() + self.timeout
                    accepted = fixture.latest_selection(self.state)
                    while True:
                        require(process.poll() is None, 'App exited before fresh valid report; inspect app.log')
                        try:
                            path = v1.container_file(self.state, 'variant-status.json')
                            require(not path.is_symlink() and path.stat().st_size <= 64 * 1024 and
                                    path.stat().st_mtime >= entry['started'], 'Stale or unsafe variant report')
                            report = fixture.validate_report(self.state, json.loads(path.read_text()),
                                after=entry['started'], variant=variant, previous_data=self.previous_data,
                                checked=check and not rejected, accepted_variant=accepted_variant)
                            require(report['pid'] == process.pid, 'Wrong report PID')
                            if rejected:
                                validate_rejected_check(report, accepted)
                            container = v1.validate_container_record(self.state,
                                json.loads(v1.container_file(self.state, 'container.json').read_text()), entry['started'])
                            require(container['pid'] == process.pid, 'Wrong container PID')
                            ffi = v1.validate_status(self.work, self.state, v1.container_file(self.state, 'ffi-status.json'),
                                entry['started'], 'base' if variant == 'free' else 'update')
                            if cold:
                                fixture.validate_cold_activation(self.state, report, fallback=fallback)
                            provider_path = v1.container_file(self.state, 'provider.json')
                            require(not provider_path.is_symlink() and provider_path.stat().st_size <= 65536 and
                                    provider_path.stat().st_mtime >= entry['started'], 'Stale or unsafe provider ledger')
                            provider = json.loads(provider_path.read_text())
                            provider_counts = validate_provider_ledger(self.state,report,provider,pid=process.pid,
                                nonce=nonce,after=entry['started'],key_failure=key_failure)
                            fixture.remember_installation(self.work, self.state, report)
                            self.state['observed_selection'] = fixture.selection_identity(report['selection']['accepted'])
                            v1.save(self.work, self.state)
                            self.previous_data = report['data']
                            entry.update(report=report, container=container, ffi=ffi,provider=provider,
                                         provider_counts=provider_counts)
                            break
                        except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError) as invalid:
                            if time.monotonic() >= deadline:
                                raise RuntimeError('No valid fresh report: ' + str(invalid)) from invalid
                            time.sleep(.2)
            if offline:
                self.require_offline()
            require(bundle_tree(self.app) == self.tree, 'Signed bundle changed during launch')
            entry.update(validated=True, signed_bundle_unchanged=True)
        except Exception as caught:
            error = caught
            entry['error'] = f'{type(caught).__name__}: {caught}'
        finally:
            # Snapshot before process teardown; preserve raw data even if any
            # semantic validator failed. Snapshot/cleanup errors stay visible.
            for name, action in (
                ('raw_reports', lambda: self.raw_reports(directory)),
                ('cache', lambda: cache_snapshot(self.state, self.keys))):
                try:
                    value = action()
                    entry[name] = value
                    self.write(label + '/' + name + '.json', value)
                except Exception as caught:
                    entry[name + '_error'] = f'{type(caught).__name__}: {caught}'
                    error = error or caught
            try:
                if process is not None:
                    self.owner.stop(process)
                require(bundle_tree(self.app) == self.tree, 'Signed bundle changed during process teardown')
            except Exception as caught:
                entry['process_cleanup_error'] = str(caught)
                error = error or caught
            if error:
                entry['validated'] = False
            self.write(label + '/launch.json', entry)
            self.save()
        if error:
            raise error
        return entry

    def key(self, action, variant='pro', *, wrong=False, label=None):
        return self.launch(label or f'key-{action}-{variant}-{len(self.launches)}', key=action,
                           key_variant=variant, wrong=wrong)

    def switch(self, target, current, *, label=None, offline_valid_until=None):
        self.reload()
        assigned = fixture.assign(self.work, self.state, target, offline_valid_until=offline_valid_until)
        epoch = assigned['selection']['epoch']
        prefix = label or f'switch-{target}-e{epoch}'
        self.launch(prefix + '-check', current, check=True)
        return self.launch(prefix + '-cold', target, cold=True)

    def capture_response(self):
        """A real server-signed response for the currently accepted selection.

        This uses an independent UUID nonce. Replaying it later tests complete
        old-response rejection (nonce and epoch), not isolated epoch rejection.
        """
        self.reload()
        accepted = fixture.latest_selection(self.state)
        scope = self.state['release_scope']
        request = dict(selection_protocol=2, app_id=self.state['app_id'],
            release_version=self.state['release_version'], platform='macos', arch=v1.target_architecture(self.state)['wire'],
            channel='stable', client_id=self.state['installation_id'], nonce=str(uuid.uuid4()),
            base_artifact_hash=scope['base_artifact_hash'], host_abi_hash=scope['host_abi_hash'],
            runtime_profile=scope['runtime_profile'], accepted_selection_epoch=accepted['epoch'],
            accepted_selection_digest=accepted['digest'])
        response = v1.shared.request(self.state, 'POST', '/api/v1/patches/check', request)
        require(response['selection']['epoch'] == accepted['epoch'] and
                response['selection_digest'] == accepted['digest'] and
                response.get('authentication', {}).get('signature'), 'No authentic response for accepted authority')
        # Check only capture consistency. Python does not certify signatures.
        payload = json.loads(base64.b64decode(response['authentication']['payload'], validate=True))
        require(payload['selection_digest'] == accepted['digest'], 'Captured signed payload differs')
        self.write('replay-capture.json', {'request': request, 'response': response, 'accepted': accepted,
            'scope': 'Authentic server response for already accepted selection; independent nonce; not an epoch-only test'})
        return json.dumps(response).encode(), accepted

    @contextmanager
    def replay_endpoint(self, body, requests):
        self.require_offline()
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def __init__(self, *args):
                self.connections = set()
                self.connections_lock = threading.Lock()
                super().__init__(*args)

            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(5)  # Before request-line/header parsing.
                with self.connections_lock:
                    self.connections.add(connection)
                return connection, address

            def shutdown_request(self, connection):
                with self.connections_lock:
                    self.connections.discard(connection)
                super().shutdown_request(connection)

            def close_connections(self):
                with self.connections_lock:
                    connections = tuple(self.connections)
                    self.connections.clear()
                for connection in connections:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection.close()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                try:
                    received = time.time()
                    length = int(self.headers.get('Content-Length', '0'))
                    require(0 < length < 65536, 'Invalid replay request length')
                    raw = self.rfile.read(length)
                    require(len(raw) == length, 'Incomplete replay request body')
                    request = json.loads(raw)
                    if self.path == '/api/v1/patches/check':
                        require(type(request) is dict and request.get('selection_protocol') == 2,
                                'Replay request is not protocol 2')
                        response = body
                    elif self.path == '/api/v1/patches/events':
                        response = b'{}'
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    self.wfile.flush()
                    if self.path == '/api/v1/patches/check':
                        requests.append({'request': request, 'request_body_base64': base64.b64encode(raw).decode(),
                            'request_body_sha256': hashlib.sha256(raw).hexdigest(),
                            'response_body_sha256': hashlib.sha256(response).hexdigest(),
                            'received_at': received, 'completed_at': time.time()})
                except (ValueError, KeyError, TypeError):
                    try:
                        self.send_error(400)
                    except OSError:
                        pass
                except OSError:
                    pass  # A failed body write/flush is not delivery evidence.
        server = Server(('127.0.0.1', self.ports()[0]), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            server.close_connections()
            server.server_close()
            thread.join(timeout=5)
            require(not thread.is_alive(), 'Owned replay endpoint thread did not stop')
            self.require_offline()

    def patch_control(self, variant, action):
        release = fixture.release_id(self.work, self.state)
        patches = fixture.api(self.work, self.state, 'GET', f'/releases/{release}/patches')['patches']
        matches = [item for item in patches if str(item['patch_number']) == fixture.EXPECTED_NUMBERS[variant]]
        require(len(matches) == 1, 'Missing unique real patch for rollback')
        result = fixture.api(self.work, self.state, 'POST',
            f'/releases/{release}/patches/{matches[0]["patch_id"]}/{action}')
        self.write(action + '-' + str(len(self.launches)) + '.json',
                   {'variant': variant, 'patch_id': matches[0]['patch_id'], 'response': result})

    def tamper(self, case):
        root = cache_root(self.state)
        path = root / ('patches/2/dlc.vmcode' if case == 'ciphertext' else 'patches/2/state.json')
        require(path.is_file() and not path.is_symlink(), 'Missing owned pro cache artifact')
        before = cache_snapshot(self.state, self.keys)
        data = path.read_bytes()
        if case == 'ciphertext':
            match = re.search(rb'"encrypted_payload_base64"\s*:\s*"', data)
            require(match is not None, 'Encrypted payload not found')
            index = match.end()
            changed = data[:index] + (b'B' if data[index:index + 1] == b'A' else b'A') + data[index + 1:]
        else:
            value = json.loads(data)
            signature = value['signature']
            require(isinstance(signature, str) and bool(signature), 'Missing stored signature')
            value['signature'] = ('B' if signature[0] == 'A' else 'A') + signature[1:]
            changed = json.dumps(value).encode()
        path.write_bytes(changed)
        rejected = self.launch(case + '-tamper-fallback', 'free', cold=True, fallback=True, offline=True)
        assert_journal_preserved(before, rejected['cache'])
        self.write(case + '-tamper.json', {'path': str(path.relative_to(root)), 'before': before,
            'modified_sha256': hashlib.sha256(changed).hexdigest(),
            'recovery': 'Explicit signed free selection then paid reauthorization; no cache restoration'})
        self.start_server()
        self.switch('free', 'free', label=case + '-explicit-free')
        # A real download must repair the corrupted cache. If quarantine blocks
        # it, fail and retain evidence; never silently reset cache state.
        self.switch('pro', 'free', label=case + '-reauthorized-pro')
        self.stop_server()
        self.launch(case + '-repaired-offline-pro', 'pro', cold=True, offline=True)

    def run_cases(self):
        self.sign()
        self.start_server()
        fixture.map_free(self.work, self.state)
        self.launch('initial-free', 'free')
        self.launch('initial-signed-free', 'free', check=True)
        self.launch('initial-cold-free', 'free', cold=True)
        self.key('provision', 'pro')
        self.key('provision', 'enterprise')
        current = 'free'
        for target in fixture.LIFECYCLE[1:]:
            self.switch(target, current)
            current = target
        self.switch('pro', 'free', label='same-content-first')
        self.switch('pro', 'pro', label='same-content-higher-epoch')
        self.stop_server()
        self.launch('offline-paid', 'pro', cold=True, offline=True)
        for case, action, wrong in (('missing', 'delete', False), ('wrong', 'overwrite', True)):
            before = cache_snapshot(self.state, self.keys)
            self.key(action, wrong=wrong, label=case + '-key-control')
            self.launch(case + '-key-free', 'free', cold=True, fallback=True, offline=True,key_failure=case)
            assert_cache_preserved(before, cache_snapshot(self.state, self.keys))
            self.key('provision' if case == 'missing' else 'overwrite', label=case + '-key-restore')
            self.launch(case + '-key-recovered-pro', 'pro', cold=True, offline=True)
            assert_cache_preserved(before, cache_snapshot(self.state, self.keys))
        for case in ('ciphertext', 'signature'):
            self.tamper(case)
        self.start_server()
        old_response, old_selection = self.capture_response()
        expires = int(time.time()) + self.lease_seconds
        self.switch('pro', 'pro', label='leased-pro', offline_valid_until=str(expires))
        self.stop_server()
        self.launch('lease-valid-offline-pro', 'pro', cold=True, offline=True)
        require(time.time() < expires, 'Lease expired before valid-offline observation')
        started_after = wait_past_deadline(expires)
        expired = self.launch('lease-expired-offline-free', 'free', cold=True, fallback=True, offline=True)
        require(expired['started'] > expires, 'Expiry launch preceded lease deadline')
        self.write('lease-expiry.json', {'deadline': expires, 'wait_completed': started_after,
                                       'launch_started': expired['started']})
        before = cache_snapshot(self.state, self.keys)
        requests = []
        try:
            with self.replay_endpoint(old_response, requests):
                replayed = self.launch('old-response-rejected', 'free', check=True, cold=True, fallback=True, rejected=True)
            accepted = fixture.latest_selection(self.state)
            validate_replay_delivery(self.state, requests, old_response, old_selection, accepted,
                                     after=replayed['started'], before=time.time())
            assert_journal_preserved(before, cache_snapshot(self.state, self.keys))
        finally:
            self.write('replay-requests.json', requests)
        self.start_server()
        self.launch('online-expired-server-free', 'free', check=True, accepted_variant='free')
        self.launch('cold-server-free', 'free', cold=True)
        self.switch('enterprise', 'free', label='before-rollback-enterprise')
        self.patch_control('enterprise', 'rollback')
        self.launch('rollback-accept-free', 'enterprise', check=True, accepted_variant='free')
        self.launch('rollback-cold-free', 'free', cold=True)
        self.patch_control('enterprise', 'rollforward')
        self.launch('rollforward-no-implicit-paid', 'free', check=True)
        self.launch('rollforward-still-free', 'free', cold=True)
        self.switch('enterprise', 'free', label='explicit-reauthorization')
        self.switch('free', 'enterprise', label='final-free')
        self.stop_server()
        self.launch('final-offline-free', 'free', cold=True, offline=True)
        self.result['runtime_acceptance'] = True
        self.save()

    def cleanup(self):
        errors = []
        try:
            self.stop_server()
        except Exception as error:
            errors.append('server: ' + str(error))
        try:
            self.owner.close()
        except Exception as error:
            errors.append('processes: ' + str(error))
        self.reload()
        if self.tree is not None:
            try:
                if not self.state.get('cleanup_fence'):
                    fixture.begin_cleanup(self.work, self.state)
                for variant in ('pro', 'enterprise'):
                    try:
                        self.key('delete', variant, label='final-delete-' + variant)
                    except Exception as error:
                        errors.append(variant + ' key: ' + str(error))
                self.result['cleanup'] = fixture.cleanup(self.work, self.state)
            except Exception as error:
                errors.append('payload: ' + str(error))
        else:
            errors.append('App signing/verification did not finish; final native deletion could not be safely launched')
        try:
            self.owner.close()
        except Exception as error:
            errors.append('final processes: ' + str(error))
        if errors:
            self.result['cleanup_errors'] = errors
        self.save()
        return errors


def plan(target_arch='arm64', *, verify_presigned=False):
    return {'schema': SCHEMA, 'runtime_acceptance': False, 'execute_required': True,
            'target': v1.target_architecture({'arch':target_arch}),
            'signing_mode': 'verify_presigned' if verify_presigned else 'sign_locally',
            'prerequisite': 'Fresh disposable macos_variant_e2e v2 fixture; base/pro_seed/pro/enterprise built; build server stopped',
            'cold_lifecycle': list(fixture.LIFECYCLE),
            'cases': ['same-content higher-epoch restart', 'offline paid', 'missing/wrong key and offline recovery',
                'ciphertext/signature fallback and explicit free/paid repair without reset',
                'lease expiry after wall-clock deadline', 'authentic older signed HTTP response replay',
                'rollback/rollforward without implicit paid reauthorization'],
            'evidence': 'Per-launch raw reports, data, FFI, PID, signatures, immutable bundle tree and cache journal',
            'cleanup': 'Only owned processes; cleanup fence, both native key deletions and existing fixture cleanup',
            'limits': 'Replay also has an old nonce; not isolated epoch-only evidence. No builds or devices.'}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--work-dir', type=Path, required=True)
    result.add_argument('--target-arch', choices=('arm64','x64'),
                        help='Optional check against prepared target; plan default: arm64')
    result.add_argument('--signing-identity', help='Explicit Developer ID certificate SHA-1')
    result.add_argument('--verify-presigned', action='store_true',
                        help='Verify the existing signed bundle using only the public certificate pin; never sign it')
    result.add_argument('--execute', action='store_true')
    result.add_argument('--timeout', type=float, default=60)
    result.add_argument('--lease-seconds', type=int, default=90)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.execute:
        print(json.dumps(plan(args.target_arch or 'arm64',verify_presigned=args.verify_presigned), indent=2))
        return
    require(re.fullmatch(r'[a-fA-F0-9]{40}', args.signing_identity or '') is not None,
            '--execute requires a pinned Developer ID certificate SHA-1')
    require(math.isfinite(args.timeout) and 0 < args.timeout <= 300 and 30 <= args.lease_seconds <= 3600,
            'Invalid timeout or lease duration')
    work = args.work_dir.expanduser().resolve()
    state = fixture.load(work)
    if args.target_arch is not None:
        require(args.target_arch == v1.target_architecture(state)['arch'], 'Requested target differs from prepared app')
    require_native_host(state)  # Before lifecycle construction, signing or any launch.
    require_fresh_fixture(work, state, preserve_scope=args.verify_presigned)
    runner = Lifecycle(work, state, args.signing_identity, timeout=args.timeout, lease_seconds=args.lease_seconds,
                       verify_presigned=args.verify_presigned)
    error = None
    try:
        runner.run_cases()
    except BaseException as caught:
        error = caught
        runner.result['error'] = f'{type(caught).__name__}: {caught}'
        runner.save()
    finally:
        cleanup_errors = runner.cleanup()
    if error:
        raise error
    if cleanup_errors:
        raise RuntimeError('Owned cleanup incomplete; inspect lifecycle/result.json')
    print(json.dumps({'runtime_acceptance': runner.result['runtime_acceptance'],
                      'cleanup': runner.result.get('cleanup'), 'evidence': str(runner.evidence)}, indent=2))


if __name__ == '__main__':
    main()

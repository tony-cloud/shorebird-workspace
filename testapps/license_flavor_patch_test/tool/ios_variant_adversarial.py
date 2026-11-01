"""iOS fixture diagnostics and real API helpers; importing starts no resources."""
import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse
import uuid


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot(value):
    require(isinstance(value, dict) and set(value) == {'schema', 'files'} and value['schema'] == 'ios-variant-cache-v1' and
            isinstance(value['files'], dict) and len(value['files']) <= 40, 'Missing bounded native cache snapshot')
    for name, entry in value['files'].items():
        require(isinstance(name, str) and re.fullmatch(r'(state\.json|pointers\.json|patches/[23]/(state\.json|dlc\.vmcode)|downloads/[23]|selections-v2/[a-f0-9]{64}\.json)', name), 'Foreign cache path')
        require(isinstance(entry, dict) and set(entry) <= {'size', 'sha256', 'kind', 'version', 'epoch', 'digest', 'time_floor'} and
                type(entry.get('size')) is int and 0 <= entry['size'] <= 512 * 1024 * 1024 and
                isinstance(entry.get('sha256'), str) and re.fullmatch('[a-f0-9]{64}', entry['sha256']), 'Invalid or unsanitized cache projection')
        if name.startswith('selections-v2/'):
            require(entry.get('version') == 2 and isinstance(entry.get('epoch'), str) and re.fullmatch('[1-9][0-9]*', entry['epoch']) and
                    isinstance(entry.get('digest'), str) and re.fullmatch('[a-f0-9]{64}', entry['digest']) and
                    type(entry.get('time_floor')) is int and entry['time_floor'] >= 0, 'Incomplete journal diagnostic projection')
        if 'kind' in entry:
            require(entry['kind'] in ('Installed', 'InstalledEncryptedTransportV1', 'Bad', 'Downloading', 'Downloaded'), 'Unknown patch-state projection')
    return value


def journals(value):
    return {k: v for k, v in snapshot(value)['files'].items() if k.startswith('selections-v2/')}


def assert_journal_preserved(before, after):
    require(bool(journals(before)) and journals(before) == journals(after), 'Authorization journal bytes changed or missing')


def assert_cache_preserved(before, after):
    def protected(value):
        return {k: v for k, v in snapshot(value)['files'].items() if k.startswith(('patches/', 'selections-v2/'))}
    require(bool(protected(before)) and protected(before) == protected(after), 'Ciphertext/metadata/journal changed or missing')


def validate_native(state, report, *, after, cache_action=None, cache_patch=None, cache_sha256=None):
    boot = report.get('native_boot')
    require(isinstance(boot, dict) and set(boot) == {'session', 'nonce', 'pid', 'time'} and
            boot['session'] == state['session'] and boot['nonce'] == report['nonce'] and type(boot['pid']) is int and boot['pid'] == report['pid'] and
            type(boot['time']) in (int, float) and math.isfinite(boot['time']) and
            after <= boot['time'] <= report['started'], 'Fresh same-process native pre-engine time required')
    before, controlled, current = [snapshot(report.get(name)) for name in ('cache_before', 'cache_after_control', 'cache_after')]
    receipt = report.get('cache_control')
    if cache_action is None:
        require(receipt is None and before == controlled, 'Unrequested native cache control')
        return
    require(isinstance(receipt, dict) and receipt.get('schema') == 'ios-variant-cache-control-v1' and
            receipt.get('session') == state['session'] and receipt.get('bundle_id') == state['bundle_id'] and
            receipt.get('nonce') == report['nonce'] and receipt.get('pid') == report['pid'] and
            receipt.get('time') == boot['time'] and receipt.get('action') == cache_action,
            'Fresh native cache-control receipt required')
    context = {'schema', 'session', 'bundle_id', 'nonce', 'pid', 'time', 'action', 'result'}
    if cache_action == 'inspect':
        require(set(receipt) == context and receipt['result'] == 'inspected' and before == controlled, 'Inspection changed updater cache')
        return
    require(cache_action in ('tamper-ciphertext', 'tamper-signature') and cache_patch in ('2', '3'), 'Unsupported cache mutation')
    name = 'patches/' + cache_patch + ('/dlc.vmcode' if cache_action == 'tamper-ciphertext' else '/state.json')
    old, new = before['files'].get(name), controlled['files'].get(name)
    require(isinstance(old, dict) and isinstance(new, dict) and
            set(receipt) == context | {'path', 'patch_number', 'expected_sha256', 'before_sha256', 'after_sha256', 'changed_bytes', 'size'} and
            receipt['result'] == 'mutated' and receipt['path'] == name and receipt['patch_number'] == cache_patch and
            receipt['expected_sha256'] == receipt['before_sha256'] == cache_sha256 == old['sha256'] and
            receipt['after_sha256'] == new['sha256'] != old['sha256'] and
            receipt['changed_bytes'] == 1 and type(receipt['changed_bytes']) is int and
            receipt['size'] == old['size'] == new['size'] and
            {k: v for k, v in before['files'].items() if k != name} == {k: v for k, v in controlled['files'].items() if k != name},
            'Native mutation did not match fixed-path CAS/one-byte contract')
    assert_journal_preserved(before, current)


def validate_expiry(state, report, deadline):
    require(isinstance(deadline, str) and re.fullmatch('0|[1-9][0-9]*', deadline), 'Canonical lease deadline required')
    accepted = report['selection']['accepted']
    matches = [x['selection'] for x in state['assignments'] if x['selection_digest'] == accepted['digest'] and x['selection']['epoch'] == accepted['epoch']]
    require(len(matches) == 1 and matches[0].get('offline_valid_until') == deadline and
            accepted['variant_id'] in ('pro', 'enterprise') and report['variant'] == 'free' and
            report['native_boot']['time'] > int(deadline) and report['selection']['next_boot'].get('reason') == 'expired',
            'Lease expiry requires exact signed deadline and post-deadline native free boot')


def validate_cleanup_receipt(state, receipt):
    launch = state.get('pending_cleanup_launch') or state.get('native_cleanup_launch')
    require(isinstance(launch, dict) and isinstance(receipt, dict) and
            set(receipt) == {'schema', 'session', 'nonce', 'bundle_id', 'service', 'pid', 'time', 'mode', 'keys'} and
            receipt['schema'] == 'ios-variant-native-cleanup-v1' and receipt['session'] == state['session'] and
            receipt['nonce'] == launch['nonce'] and receipt['bundle_id'] == state['bundle_id'] and receipt['service'] == state['key_service'] and
            type(receipt['pid']) is int and receipt['pid'] > 0 and type(receipt['time']) in (int, float) and math.isfinite(receipt['time']) and
            max(state.get('cleanup_fence', float('inf')), launch['after']) <= receipt['time'] <= time.time() + 2 and
            receipt['mode'] == 'native-keychain-cleanup-only' and isinstance(receipt['keys'], dict) and
            set(receipt['keys']) == set(state['key_ids']), 'Fresh independent native cleanup receipt required')
    for variant, entry in receipt['keys'].items():
        require(isinstance(entry, dict) and set(entry) == {'account', 'result'} and
                entry['account'] == state['key_ids'][variant] and entry['result'] in ('deleted', 'absent'), 'Both exact native key accounts must be absent')


def capture_response(fixture, work, state):
    fixture.require_open(work, state)
    fixture.require_key_launch_reconciled(work)
    accepted = fixture.selection.latest_selection(state)
    require(accepted['epoch'] != '0' and state.get('installation_id'), 'Accept real installation authority before capture')
    request = dict(selection_protocol=2, app_id=state['app_id'], release_version=state['release_version'],
        platform='ios', arch='aarch64', channel='stable', client_id=state['installation_id'], nonce=str(uuid.uuid4()),
        **{k: state['release_scope'][k] for k in ('base_artifact_hash', 'host_abi_hash', 'runtime_profile')},
        accepted_selection_epoch=accepted['epoch'], accepted_selection_digest=accepted['digest'])
    result = fixture.ios.request(state, 'POST', '/api/v1/patches/check', request)
    require(result['selection']['epoch'] == accepted['epoch'] and result['selection_digest'] == accepted['digest'] and
            bool(result.get('authentication', {}).get('signature')), 'Capture must return current authentic server response')
    payload = json.loads(base64.b64decode(result['authentication']['payload'], validate=True))
    context = {k: v for k, v in request.items() if k not in ('client_id', 'selection_protocol')}
    context['client_id_hash'] = hashlib.sha256(state['installation_id'].encode()).hexdigest()
    require(payload['selection_digest'] == accepted['digest'] and payload['request'] == context and
            payload['selection'] == result['selection'] and result['selection']['scope'] == {
                **state['release_scope'], 'client_id_hash': context['client_id_hash']}, 'Capture payload binding differs')
    # Exact bytes are private replay input, never logs or public diagnostic
    # receipts: a signed delivery response can contain a temporary artifact URL.
    body = json.dumps(result).encode()
    path = work / 'private-response-captures' / (request['nonce'] + '.json')
    fixture.ios.private_write(path, body)
    receipt = dict(schema='ios-authentic-response-capture-v1', session=state['session'], path=str(path),
        sha256=hashlib.sha256(body).hexdigest(), accepted=accepted, nonce=request['nonce'], time=time.time())
    fixture.ios.write_json(work / 'latest-response-capture.json', receipt)
    return receipt


def atomic_receipt(fixture, path, value):
    temporary = path.with_suffix('.next.json')
    fixture.ios.write_json(temporary, value)
    temporary.replace(path)


def replay_launch(fixture, work, state):
    path = work / 'replay-active.json'
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16384, 'Start owned replay endpoint before rejected-check launch')
    active = json.loads(path.read_text())
    require(set(active) == {'schema', 'session', 'id', 'started', 'capture_sha256', 'accepted'} and
            active['schema'] == 'ios-replay-session-v1' and active['session'] == state['session'] and
            re.fullmatch('[a-f0-9]{32}', active['id']) and re.fullmatch('[a-f0-9]{64}', active['capture_sha256']) and
            type(active['started']) in (int, float) and math.isfinite(active['started']) and
            active['started'] <= time.time() and active['accepted'] == fixture.selection.latest_selection(state),
            'Replay session does not match current owned authority')
    return active


def validate_replay_delivery(fixture, work, state, launch, report):
    replay = launch.get('replay')
    require(isinstance(replay, dict), 'Rejected response acceptance requires a replay-bound launch')
    path = work / 'replay-deliveries' / (launch['nonce'] + '.json')
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16384,
            'No completed authentic replay delivery for this launch; offline/error is not replay evidence')
    receipt = json.loads(path.read_text())
    require(set(receipt) == {'schema', 'session', 'replay_id', 'launch_nonce', 'request_nonce', 'accepted',
            'capture_sha256', 'response_bytes', 'completed', 'started', 'time'} and
            receipt['schema'] == 'ios-replay-delivery-v1' and receipt['session'] == state['session'] and
            receipt['replay_id'] == replay['id'] and receipt['launch_nonce'] == launch['nonce'] and
            isinstance(receipt['request_nonce'], str) and bool(receipt['request_nonce']) and
            receipt['accepted'] == replay['accepted'] == fixture.selection.selection_identity(report['selection']['accepted']) and
            receipt['capture_sha256'] == replay['capture_sha256'] and receipt['completed'] is True and
            type(receipt['response_bytes']) is int and 0 < receipt['response_bytes'] <= 1024 * 1024 and
            all(type(value) in (int, float) and math.isfinite(value) for value in (receipt['started'], receipt['time'], report.get('completed'))) and
            replay['started'] <= launch['after'] <= report['started'] <= receipt['started'] <= report['completed'] <= time.time() + 2 and
            receipt['started'] <= receipt['time'] <= time.time() + 2,
            'Authentic replay delivery is not bound to the fresh launch/check window')


@contextmanager
def replay_endpoint(fixture, work, state, capture):
    fixture.require_open(work, state)
    fixture.require_key_launch_reconciled(work)
    path = Path(capture['path'])
    require(path.resolve().parent == (work / 'private-response-captures').resolve() and not path.is_symlink() and
            path.stat().st_size <= 1024 * 1024 and capture.get('session') == state['session'], 'Foreign replay capture')
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == capture['sha256'], 'Captured response bytes changed')
    latest = fixture.selection.latest_selection(state)
    require(int(capture['accepted']['epoch']) < int(latest['epoch']), 'Replay requires a newer accepted high-water epoch')
    port = urlparse(state['api_url']).port
    require(not fixture.selection.v1.port_open(port), 'Stop owned update API before replay; no process stopped')
    requests = []
    replay = dict(schema='ios-replay-session-v1', session=state['session'], id=uuid.uuid4().hex,
        started=time.time(), capture_sha256=capture['sha256'], accepted=latest)
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.connection.settimeout(5)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                require(not self.headers.get('Transfer-Encoding') and 0 < size <= 65536, 'Invalid replay request framing')
                raw = self.rfile.read(size)
                require(len(raw) == size, 'Truncated request')
                value = json.loads(raw)
                if self.path == '/api/v1/patches/check':
                    require(isinstance(value, dict) and value.get('selection_protocol') == 2 and
                            value.get('app_id') == state['app_id'] and value.get('platform') == 'ios' and value.get('arch') == 'aarch64' and
                            value.get('channel') == 'stable' and all(value.get(k) == state['release_scope'][k] for k in ('base_artifact_hash', 'host_abi_hash', 'runtime_profile')) and
                            value.get('client_id') == state['installation_id'] and value.get('release_version') == state['release_version'] and
                            value.get('accepted_selection_epoch') == latest['epoch'] and value.get('accepted_selection_digest') == latest['digest'] and
                            value.get('nonce') != capture['nonce'], 'Replay request did not come from newer owned installation')
                    current = fixture.load(work)
                    require(bool(current.get('launches')), 'No armed replay launch')
                    launch = current['launches'][-1]
                    require(launch.get('replay') == replay and launch.get('check') is True and launch.get('rejected_check') is True and
                            re.fullmatch('[a-f0-9]{48}', launch['nonce']) and launch['after'] >= replay['started'],
                            'Replay request lacks a fresh armed launch')
                    request_nonce = value['nonce']
                    require(isinstance(request_nonce, str) and bool(request_nonce) and len(request_nonce) <= 128,
                            'Missing fresh request nonce')
                    response = body
                elif self.path == '/api/v1/patches/events': response = b'{}'
                else:
                    self.send_error(404)
                    return
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            delivery_started = time.time()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            try:
                written = self.wfile.write(response)
                require(written == len(response), 'Incomplete replay body write')
                self.wfile.flush()
            except (OSError, ValueError):
                return  # Disconnected/short/unflushed bodies are never evidence.
            if self.path == '/api/v1/patches/check':
                receipt = dict(schema='ios-replay-delivery-v1', session=state['session'], replay_id=replay['id'],
                    launch_nonce=launch['nonce'], request_nonce=request_nonce, accepted=latest,
                    capture_sha256=capture['sha256'], response_bytes=len(response), completed=True, started=delivery_started, time=time.time())
                with lock:
                    # Persist before a later Dart receipt can be accepted; this
                    # remains usable while the endpoint is running or stopped.
                    atomic_receipt(fixture, work / 'replay-deliveries' / (launch['nonce'] + '.json'), receipt)
                    requests.append(receipt)
    server = ThreadingHTTPServer((state['bind_address'], port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        atomic_receipt(fixture, work / 'replay-active.json', replay)
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        require(not thread.is_alive(), 'Owned replay thread did not stop')
        active_path = work / 'replay-active.json'
        if active_path.is_file() and json.loads(active_path.read_text()).get('id') == replay['id']:
            active_path.unlink()
        fixture.ios.write_json(work / 'replay-requests.json', dict(capture_sha256=capture['sha256'], requests=requests,
            scope='Authentic complete older response; nonce and epoch replay, not isolated epoch rejection'))


def patch_control(fixture, work, state, variant, action):
    fixture.require_open(work, state)
    fixture.require_key_launch_reconciled(work)
    require(variant in ('pro', 'enterprise') and action in ('rollback', 'rollforward'), 'Unsupported administrative patch control')
    release = fixture.release_id(work, state)
    suffix = f'/releases/{release}/patches'
    def find():
        matches = [x for x in fixture.api(work, state, 'GET', suffix)['patches'] if str(x['patch_number']) == fixture.selection.EXPECTED_NUMBERS[variant]]
        require(len(matches) == 1 and type(matches[0].get('is_rolled_back')) is bool, 'Unique real patch lifecycle state required')
        return matches[0]
    patch = find()
    request = dict(variant=variant, action=action, patch_id=patch['patch_id'], release_id=release)
    pending = state.get('pending_patch_control')
    require(pending is None or pending == request, 'Reconcile previous administrative patch control before changing intent')
    if pending is None:
        state['pending_patch_control'] = request
        fixture.save(work, state)
    elif patch['is_rolled_back'] != (action == 'rollback'):
        raise ValueError('Previous administrative outcome remains uncertain; no mutation replayed')
    if pending is None and patch['is_rolled_back'] != (action == 'rollback'):
        fixture.api(work, state, 'POST', suffix + '/' + str(patch['patch_id']) + '/' + action)
        patch = find()
    require(patch['is_rolled_back'] == (action == 'rollback'), 'Administrative patch control did not reach intended state')
    receipt = {**request, 'is_rolled_back': patch['is_rolled_back'], 'time': time.time()}
    state.setdefault('patch_controls', []).append(receipt)
    state.pop('pending_patch_control', None)
    fixture.save(work, state)
    return receipt

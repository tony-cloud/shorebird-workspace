#!/usr/bin/env python3
"""Owned cache controls and public v2 oracles; never changes a journal.

Replayed captures have an independent nonce: rejection covers the complete old
response and does not isolate epoch or signature verification. Capture checks
server provenance and payload consistency, not RSA validity.
"""
import base64
from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from urllib.parse import urlparse
import uuid

import native_variant_e2e as protocol

CASES = ('owned-ciphertext-offline-content-unavailable', 'signed-free-same-pro-real-redownload',
         'cached-signature-keeps-valid-pro', 'actual-paid-lease-platform-clock-expiry',
         'server-returned-older-response-rejected-independent-nonce', 'corrupt-stale-response-combined-rejection',
         'enterprise-rollback-signed-free', 'rollforward-alone-stays-free',
         'explicit-enterprise-reauthorization', 'final-offline-free')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _regular(root, relative):
    root = Path(root).absolute()
    require(root == root.resolve() and root.is_dir(), 'Redirected cache root')
    path = root / relative
    require(root in path.parents and path == path.resolve(), 'Redirected cache entry')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 268435456,
            'Cache control requires a bounded unlinked regular file')
    return path, info


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def cache_snapshot(root):
    root = Path(root).absolute()
    require(root == root.resolve() and root.is_dir(), 'Redirected cache root')
    files = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Redirected cache entry')
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        path, info = _regular(root, relative)
        files[relative] = {'size': info.st_size, 'sha256': _digest(path)}
    journals = {name: value for name, value in files.items()
                if re.fullmatch(r'selections-v2/[a-f0-9]{64}\.json', name)}
    require(bool(journals), 'Missing authorization journal evidence')
    return {'files': files, 'journals': journals}


def assert_journal_preserved(before, after):
    require(bool(before['journals']) and before['journals'] == after['journals'],
            'Authorization journal bytes changed')


def mutate_cache(root, number, kind):
    """Flip owned ciphertext or unauthoritative metadata, with no restoration."""
    protocol.decimal(str(number))
    require(kind in ('ciphertext', 'signature'), 'Unsupported cache mutation')
    relative = f'patches/{number}/' + ('transport.aesgcm' if kind == 'ciphertext' else 'state.json')
    before = cache_snapshot(root)
    path, info = _regular(root, relative)
    require(info.st_size > 0, 'Empty mutation input')
    signature_bytes = None
    if kind == 'signature':
        require(info.st_size <= 1024 * 1024, 'Oversized native cache metadata')
        value = protocol.strict_json(path.read_bytes())
        signature = value.get('signature')
        require(value.get('kind') == 'InstalledEncryptedTransportV1' and
                isinstance(signature, str) and bool(signature), 'Missing native cached signature')
        value['signature'] = ('B' if signature[0] == 'A' else 'A') + signature[1:]
        signature_bytes = json.dumps(value, separators=(',', ':')).encode()
    # The file must still be the exact inode/bytes inspected above. Never open a
    # journal, replace an entire cache directory, or retain restoration bytes.
    with path.open('r+b') as stream:
        opened = os.fstat(stream.fileno())
        require((opened.st_dev, opened.st_ino, opened.st_nlink) ==
                (info.st_dev, info.st_ino, 1) and _digest(path) == before['files'][relative]['sha256'],
                'Mutation input changed during inspection')
        if kind == 'ciphertext':
            stream.seek(info.st_size // 2)
            byte = stream.read(1)
            stream.seek(-1, os.SEEK_CUR)
            stream.write(bytes([byte[0] ^ 1]))
        else:
            stream.write(signature_bytes)
            stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    after = cache_snapshot(root)
    assert_journal_preserved(before, after)
    changed = [name for name in before['files'] if before['files'][name] != after['files'].get(name)]
    require(changed == [relative] and set(before['files']) == set(after['files']), 'Mutation escaped its one cache file')
    return {'kind': kind, 'path': relative, 'before': before['files'][relative],
            'after': after['files'][relative], 'journals': before['journals']}


def validate_fallback(report, accepted, reason):
    require(reason in ('expired', 'content_unavailable'), 'Unsupported native fallback reason')
    protocol.validate_cold_activation(report, accepted, fallback=True)
    status = report['selection']
    require(status['next_boot'] == {'variant_id': 'free', 'license_type': 'free',
            'patch_number': None, 'reason': reason} and status['restart_required'] is False,
            'Failed/expired content did not remain effective BASE with exact reason')
    return report


def validate_automatic_free(report, previous):
    old = protocol.selection_identity(previous)
    new = protocol.selection_identity(report['selection']['accepted'])
    require(old['patch_number'] is not None and new['variant_id'] == 'free' and
            int(new['epoch']) == int(old['epoch']) + 1 and new['digest'] != old['digest'],
            'Server invalidation did not accept a fresh signed free authority')
    require(report['selection']['next_boot'] == {'variant_id': 'free', 'license_type': 'free',
            'patch_number': None, 'reason': 'selected_base'}, 'Automatic free did not select BASE')
    return new


def validate_rejected_check(report, accepted):
    require(report['stage'] == 'checked' and report.get('check_status') == 'upToDate' and
            'check_error' in report and report['check_error'] is None,
            'Rejected response did not leave public check API upToDate without error')
    require(protocol.selection_identity(report['selection']['accepted']) == protocol.selection_identity(accepted),
            'Rejected response changed accepted authorization')
    return report


def capture_response(request, session, scope):
    accepted = copy.deepcopy(session.latest)
    body = dict(selection_protocol=2, app_id=session.app_id, release_version=session.release_version,
        platform=session.platform, arch=session.arch, channel='stable', client_id=session.installation_id,
        nonce=str(uuid.uuid4()), base_artifact_hash=scope['base_artifact_hash'],
        host_abi_hash=scope['host_abi_hash'], runtime_profile=scope['runtime_profile'],
        accept_encrypted_patch=True,
        accepted_selection_epoch=accepted['epoch'], accepted_selection_digest=accepted['digest'])
    response = request('POST', '/api/v1/patches/check', body)
    require(response['selection']['epoch'] == accepted['epoch'] and
            response['selection_digest'] == accepted['digest'] and
            bool(response.get('authentication', {}).get('signature')), 'Missing server-returned capture for accepted grant')
    payload = protocol.strict_json(base64.b64decode(response['authentication']['payload'], validate=True))
    require(payload['selection_digest'] == accepted['digest'] and payload['request']['nonce'] == body['nonce'] and
            payload['selection'] == response['selection'] and payload['delivery'] == response['delivery'],
            'Captured signed payload differs from request/authority')
    evidence = {'request': body, 'response': response, 'accepted': accepted,
                'limitation': 'Independent capture nonce and stale epoch: combined response rejection, not isolated epoch or signature verification; capture is server-returned and consistency-checked, not locally RSA-verified'}
    return json.dumps(response, separators=(',', ':')).encode(), accepted, evidence


def corrupt_response_signature(body):
    value = protocol.strict_json(body)
    signature = value['authentication']['signature']
    require(isinstance(signature, str) and bool(signature), 'Missing response signature')
    value['authentication']['signature'] = ('B' if signature[0] == 'A' else 'A') + signature[1:]
    return json.dumps(value, separators=(',', ':')).encode()


def capture_summary(body, accepted, capture):
    """Allowlisted export: raw capture stays in the owning process only."""
    return {'response_sha256': hashlib.sha256(body).hexdigest(),
            'payload_sha256': hashlib.sha256(base64.b64decode(
                capture['response']['authentication']['payload'], validate=True)).hexdigest(),
            'request_sha256': hashlib.sha256(json.dumps(capture['request'], sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(),
            'nonce_sha256': hashlib.sha256(capture['request']['nonce'].encode()).hexdigest(),
            'accepted': protocol.selection_identity(accepted), 'limitation': capture['limitation']}


def replay_requests_summary(requests):
    return [{'request_sha256': hashlib.sha256(json.dumps(row, sort_keys=True,
                 separators=(',', ':')).encode()).hexdigest(),
             'nonce_sha256': hashlib.sha256(row['nonce'].encode()).hexdigest(),
             'accepted_selection_epoch': row['accepted_selection_epoch'],
             'accepted_selection_digest': row['accepted_selection_digest']}
            for row in requests]


@contextmanager
def replay_endpoint(api_url, body, requests):
    parsed = urlparse(api_url)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port and
            parsed.path in ('', '/') and not parsed.username and not parsed.query and not parsed.fragment,
            'Replay endpoint must use the owned loopback API address')
    protocol.strict_json(body)
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

        def __init__(self, *args):
            self.connections = set()
            self.connections_lock = threading.Lock()
            super().__init__(*args)

        def get_request(self):
            connection, address = super().get_request()
            # BaseHTTPRequestHandler parses headers before do_POST runs.
            connection.settimeout(5)
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
                self.connection.settimeout(5)
                length = int(self.headers.get('Content-Length', '0'))
                require(0 < length <= protocol.MAX_BODY, 'Invalid replay request size')
                value = protocol.strict_json(self.rfile.read(length))
                if self.path == '/api/v1/patches/check':
                    require(value.get('selection_protocol') == 2, 'Replay request is not protocol 2')
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
                # A disconnected client/network error is not replay evidence.
                # Record only a complete body successfully written and flushed.
                if self.path == '/api/v1/patches/check':
                    requests.append(value)
            except (ValueError, TypeError, KeyError):
                try:
                    self.send_error(400)
                except OSError:
                    pass
            except OSError:
                pass
    server = Server(('127.0.0.1', parsed.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.close_connections()
        server.server_close()
        thread.join(timeout=5)
        require(not thread.is_alive(), 'Owned replay thread did not stop')


def wait_past_deadline(deadline, *, now=time.time, sleep=time.sleep):
    require(type(deadline) in (int, float) and math.isfinite(deadline), 'Invalid lease deadline')
    def clock():
        value = now()
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                'Invalid platform wall clock')
        return value
    current = clock()
    while current <= deadline + 1:
        sleep(min(1, max(.01, deadline + 1.01 - current)))
        current = clock()
    return current

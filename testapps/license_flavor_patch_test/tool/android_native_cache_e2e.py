#!/usr/bin/env python3
"""Disposable Android signed/encrypted native lifecycle; plan and tests are pure.

Run uses pre-staged packaged tooling and an explicitly owned emulator. Only a
sanitized results.json survives cleanup. No updater state is seeded. The cache
oracle covers final files only; it makes no transient-write or hardware claim.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import threading
import urllib.parse
import uuid
import zipfile

_spec = importlib.util.spec_from_file_location('android_portable',
    Path(__file__).with_name('windows_native_cache_e2e.py'))
portable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portable)
ROOT, FIXTURE = portable.ROOT, portable.FIXTURE
FixtureError = portable.FixtureError
sha256, file_hash = portable.sha256, portable.file_hash
strict_json, private_write = portable.strict_json, portable.private_write
wait_for, free_port, tree_hashes = portable.wait_for, portable.free_port, portable.tree_hashes
SCHEMA = 'android-native-cache-e2e-v1'
CONTROL_SCHEMA = 'android-native-control-v1'
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
TOKEN = re.compile(r'[0-9a-f]{32}\Z')
NONCE = re.compile(r'[0-9a-f]{48}\Z')


def parse_aad(aad):
    if not isinstance(aad, str) or '\nplatform=android\narch=aarch64\n' not in aad:
        raise ValueError('Unsupported Android runtime context')
    result = portable.parse_aad(aad.replace('\nplatform=android\narch=aarch64\n',
                                          '\nplatform=windows\narch=x86_64\n'))
    result.update(platform='android', arch='aarch64')
    return result


def canonical_aad(context):
    if set(context) != set(portable.AAD_FIELDS):
        raise ValueError('Unknown delivery fields')
    aad = portable.AAD_PREFIX + ''.join('\n' + name + '=' + str(context[name])
                                      for name in portable.AAD_FIELDS)
    if parse_aad(aad) != {name: str(context[name]) for name in portable.AAD_FIELDS}:
        raise ValueError('Invalid delivery context')
    return aad


def delivery_material(secret, aad):
    parse_aad(aad)
    def digest(label):
        return hmac.new(secret, label + b'\0' + aad.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest(b'key-id')[:12]).rstrip(b'=').decode(), digest(b'key')


class KeyAuthority(portable.KeyAuthority):
    def allow(self, context):
        self.allowed_aad = canonical_aad(context)

    def provision(self, authorization, body):
        with self.lock:
            try:
                if not isinstance(authorization, str) or not hmac.compare_digest(
                        authorization, 'Bearer ' + self.token) or len(body) > portable.MAX_BODY:
                    raise ValueError('Unauthorized request')
                request = strict_json(body.decode('utf-8'))
                if not isinstance(request, dict) or set(request) != {'key_id', 'aad'}:
                    raise ValueError('Invalid request')
                aad, key_id = request['aad'], request['key_id']
                parse_aad(aad)
                if aad != self.allowed_aad or not isinstance(key_id, str):
                    raise ValueError('Unregistered context')
                expected, key = delivery_material(self.secret, aad)
                if not hmac.compare_digest(key_id, expected):
                    raise ValueError('Mismatching identifier')
                self.accepted += 1
                return key.hex().encode()
            except (ValueError, UnicodeError, TypeError, AttributeError):
                self.rejected += 1
                raise ValueError('Provisioning rejected') from None


DART_FIXTURE = portable.DART_FIXTURE.replace("DynamicLibrary.open('flutter_windows.dll')",
    "DynamicLibrary.open('libflutter.so')").replace(portable.SCHEMA, SCHEMA).replace(
    "'pid': pid, 'post_frame': true", "'pid': pid, 'process_token': Platform.environment['SHOREBIRD_E2E_PROCESS_TOKEN'], 'post_frame': true")


def validate_report(report, nonce, pid, process_token, expected_patch, phase):
    if (not isinstance(report, dict) or set(report) != {'schema', 'nonce', 'pid',
            'process_token', 'post_frame', 'rendered', 'current_patch'} or
            report.get('schema') != SCHEMA or not isinstance(process_token, str) or
            TOKEN.fullmatch(process_token) is None or report.get('process_token') != process_token):
        raise ValueError('Invalid Android rendered receipt')
    transformed = {k: v for k, v in report.items() if k != 'process_token'}
    transformed['schema'] = portable.SCHEMA
    portable.validate_report(transformed, nonce, pid, expected_patch, phase)
    return report


def validate_native(report, nonce, operation, *, pid=None, process_token=None):
    if (not isinstance(report, dict) or set(report) != {'schema', 'nonce', 'pid',
            'process_token', 'operation', 'payload'} or report.get('schema') != CONTROL_SCHEMA or
            report.get('nonce') != nonce or type(report.get('pid')) is not int or report['pid'] <= 0 or
            not isinstance(report.get('process_token'), str) or TOKEN.fullmatch(report['process_token']) is None or
            report.get('operation') != operation or not isinstance(report.get('payload'), dict)):
        raise ValueError('Invalid native control report')
    if ((pid is not None and report['pid'] != pid) or
            (process_token is not None and report['process_token'] != process_token)):
        raise ValueError('Native process identity mismatch')
    payload = report['payload']
    if operation == 'ready' and payload != {'provider_registered': True}:
        raise ValueError('Provider not registered before Flutter')
    if operation == 'probe' and (set(payload) != {'mac_hex', 'native_thread_attached'} or
            not isinstance(payload.get('mac_hex'), str) or HEX64.fullmatch(payload['mac_hex']) is None or
            payload.get('native_thread_attached') is not True):
        raise ValueError('Invalid native probe')
    if operation == 'clear-provider' and (set(payload) != {'records', 'alias_present'} or
            type(payload['records']) is not int or payload['records'] != 0 or payload['alias_present'] is not False):
        raise ValueError('Provider was not cleared')
    if operation not in ('ready', 'probe', 'clear-provider', 'inspect'):
        raise ValueError('Unknown report operation')
    return report


def _file_entry(entry, *, provider=False):
    expected = {'path', 'size', 'sha256'} if provider else {'path', 'size', 'sha256', 'prefix_hex'}
    if not isinstance(entry, dict) or set(entry) not in (expected, expected | {'json'}):
        raise ValueError('Malformed inventory entry')
    path = entry.get('path')
    if (not isinstance(path, str) or not path or '\\' in path or
            str(PurePosixPath(path)) != path or PurePosixPath(path).is_absolute() or
            any(part in ('.', '..') for part in path.split('/'))):
        raise ValueError('Unsafe inventory path')
    if (type(entry.get('size')) is not int or not 0 <= entry['size'] <= 268435456 or
            not isinstance(entry.get('sha256'), str) or HEX64.fullmatch(entry['sha256']) is None):
        raise ValueError('Invalid inventory size or hash')
    if provider:
        if set(entry) != expected or not re.fullmatch(r'[0-9a-f]{64}\.wrapped', path) or entry['size'] != 64:
            raise ValueError('Invalid wrapped key record')
    else:
        prefix = entry.get('prefix_hex')
        if not isinstance(prefix, str) or re.fullmatch(r'[0-9a-f]{0,8}', prefix) is None or len(prefix) != min(entry['size'], 4) * 2:
            raise ValueError('Invalid inventory prefix')
        known = path in ('state.json', 'pointers.json') or re.fullmatch(r'patches/[1-9][0-9]*/state\.json', path)
        if known:
            if 'json' not in entry or not isinstance(entry['json'], dict) or entry['size'] > 1048576:
                raise ValueError('Invalid known updater JSON')
        elif 'json' in entry:
            raise ValueError('Unexpected parsed JSON')
    return entry


def inspect_inventory(payload, *, patch_hash=None, require_key=False):
    if (not isinstance(payload, dict) or set(payload) != {'updater_roots', 'provider_files', 'alias_present', 'key_scan', 'inventory_scope'} or
            payload['inventory_scope'] != 'final-files-only' or type(payload['alias_present']) is not bool or payload['key_scan'] not in ('passed', 'unavailable', 'not-requested') or
            (require_key and payload['key_scan'] != 'passed') or not isinstance(payload['updater_roots'], list) or
            len(payload['updater_roots']) != 2 or not isinstance(payload['provider_files'], list)):
        raise ValueError('Malformed private inventory')
    files, labels = {}, set()
    count = len(payload['provider_files'])
    for root in payload['updater_roots']:
        if (not isinstance(root, dict) or set(root) != {'label', 'present', 'files'} or
                root['label'] not in ('files', 'code-cache') or root['label'] in labels or
                type(root['present']) is not bool or not isinstance(root['files'], list) or
                (not root['present'] and root['files'])):
            raise ValueError('Malformed updater root')
        labels.add(root['label'])
        seen = set()
        count += len(root['files'])
        for item in root['files']:
            entry = _file_entry(item)
            path = entry['path']
            if path in seen:
                raise ValueError('Duplicate inventory path')
            seen.add(path)
            if not (path in ('state.json', 'pointers.json') or
                    re.fullmatch(r'patches/[1-9][0-9]*/(?:state.json|transport.aesgcm)', path)):
                raise ValueError('Unexpected updater cache file')
            if entry['sha256'] == patch_hash or entry['prefix_hex'] in ('7f454c46', '4d5a9000') or entry['prefix_hex'].startswith('4d5a'):
                raise ValueError('Plaintext executable persisted')
            # State and cache may live in different Context roots. Duplicate
            # copies must agree; silently selecting one masks stale state.
            if path in files and files[path] != entry:
                raise ValueError('Conflicting updater root entries')
            files[path] = entry
    seen = set()
    for entry in payload['provider_files']:
        _file_entry(entry, provider=True)
        if entry['path'] in seen:
            raise ValueError('Duplicate wrapped record')
        seen.add(entry['path'])
    if count > 1024:
        raise ValueError('Excessive inventory')
    return files


def sanitized_inventory(payload):
    """Never retain parsed updater JSON, client IDs, signatures or URLs."""
    return {'inventory_scope': 'final-files-only', 'key_scan': payload['key_scan'],
        'alias_present': payload['alias_present'], 'provider_files': payload['provider_files'],
        'updater_roots': [dict(root, files=[{k: v for k, v in entry.items() if k != 'json'}
                                          for entry in root['files']])
                          for root in payload['updater_roots']]}


def candidate_evidence(files, number):
    state = files.get(f'patches/{number}/state.json', {}).get('json', {})
    pointers = files.get('pointers.json', {}).get('json', {})
    transport = files.get(f'patches/{number}/transport.aesgcm', {})
    if (state.get('kind') != 'InstalledEncryptedTransportV1' or not isinstance(state.get('signature'), str) or
            not state['signature'] or type(state.get('ciphertext_size')) is not int or
            transport.get('size') != state['ciphertext_size'] or state['ciphertext_size'] <= 0 or
            type(pointers.get('next_boot_patch')) is not int or pointers['next_boot_patch'] != number):
        raise ValueError('Encrypted candidate not installed and selected')
    return {'kind': state['kind'], 'transport_sha256': transport['sha256'],
            'ciphertext_size': state['ciphertext_size'], 'signature_present': True, 'next_boot_patch': number,
            'state_sha256': files[f'patches/{number}/state.json']['sha256']}


def archive_native_entries(path, *, aab=False):
    prefix = 'base/lib/arm64-v8a/' if aab else 'lib/arm64-v8a/'
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise FixtureError('Duplicate packaged archive entries')
        result = {name[len(prefix):]: sha256(archive.read(name)) for name in names
                  if name.startswith(prefix) and name.endswith('.so') and '/' not in name[len(prefix):]}
    if not {'libapp.so', 'libflutter.so', 'libshorebird_e2e_fixture.so'} <= set(result):
        raise FixtureError('Packaged native AOT, engine or JNI library missing')
    return result


def build_command(cli, *, patch=False, release_version=None):
    command = [str(cli), '--json', 'patch' if patch else 'release', 'android',
               '--target-platform=android-arm64', '--dart-define=LICENSE_TYPE=' + ('pro' if patch else 'free')]
    if patch:
        if not release_version:
            raise FixtureError('Patch requires actual release version')
        command += ['--release-version=' + release_version, '--track=stable']
    else:
        command += ['--artifact=apk']
    return command


class Observer:
    """Bounded, loopback-only native and post-frame report endpoint."""
    def __init__(self):
        self.lock, self.reports = threading.Lock(), {}
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    if self.path not in ('/native', '/receipt') or self.headers.get('Transfer-Encoding'):
                        raise ValueError('Invalid report path')
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]) or not 0 < int(lengths[0]) <= 2097152:
                        raise ValueError('Invalid report length')
                    self.connection.settimeout(5)
                    body = self.rfile.read(int(lengths[0]))
                    if len(body) != int(lengths[0]):
                        raise ValueError('Truncated report')
                    report = strict_json(body.decode('utf-8'))
                    if not isinstance(report, dict) or not isinstance(report.get('nonce'), str) or NONCE.fullmatch(report['nonce']) is None:
                        raise ValueError('Invalid report nonce')
                    key = (self.path, report['nonce'])
                    with owner.lock:
                        if len(owner.reports) >= 256 or (key in owner.reports and owner.reports[key] != report):
                            raise ValueError('Duplicate or excessive report')
                        owner.reports[key] = report
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'ok')
                except (ValueError, OSError, UnicodeError):
                    self.send_error(403)
            def log_message(self, *_):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path, nonce):
        with self.lock:
            return self.reports.get((path, nonce))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def source_record(path):
    """Accept provenance identifiers only; never copy arbitrary staging metadata."""
    data = Path(path).read_bytes()
    if len(data) > 16384:
        raise FixtureError('Oversized packaged source record')
    record = strict_json(data.decode('utf-8'))
    required = {'root_commit', 'components'}
    optional = {'ci_run_id', 'artifact_manifest_sha256'}
    if (not isinstance(record, dict) or not required <= set(record) or set(record) - required - optional or
            not isinstance(record['root_commit'], str) or re.fullmatch(r'[0-9a-f]{40}', record['root_commit']) is None or
            not isinstance(record['components'], dict) or set(record['components']) != {
                'dart', 'flutter', 'shorebird', 'updater', 'shorebird-server'} or
            any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{40}', value) is None
                for value in record['components'].values())):
        raise FixtureError('Invalid packaged source record')
    if 'ci_run_id' in record and (type(record['ci_run_id']) is not int or record['ci_run_id'] <= 0):
        raise FixtureError('Invalid packaged CI identifier')
    if 'artifact_manifest_sha256' in record and (not isinstance(record['artifact_manifest_sha256'], str) or
            HEX64.fullmatch(record['artifact_manifest_sha256']) is None):
        raise FixtureError('Invalid packaged artifact manifest hash')
    return dict(record, source_record_sha256=sha256(data))


def verify_toolchain_alignment(args):
    """Read staged identities before creating work, caches or device state."""
    def pin(path):
        value = Path(path).read_text().strip()
        if re.fullmatch(r'[0-9a-f]{40}', value) is None:
            raise FixtureError('Invalid packaged toolchain revision pin')
        return value
    selected_flutter = pin(args.cli / 'bin/internal/flutter.version')
    selected_engine = pin(args.cli / 'bin/internal/engine.version')
    flutter_engine = pin(args.flutter / 'bin/internal/engine.version')
    if flutter_engine != selected_engine:
        raise FixtureError('Packaged CLI and staged Flutter engine pins differ')
    try:
        result = subprocess.run(['git', '-C', str(args.flutter), 'rev-parse', '--verify', 'HEAD'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, timeout=15)
        actual_flutter = result.stdout.decode('ascii').strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise FixtureError('Cannot verify staged Flutter source revision') from None
    if re.fullmatch(r'[0-9a-f]{40}', actual_flutter) is None or actual_flutter != selected_flutter:
        raise FixtureError('Packaged CLI Flutter pin differs from staged Flutter HEAD')
    if args.source_record is not None:
        if source_record(args.source_record)['components']['flutter'] != selected_flutter:
            raise FixtureError('Packaged source record differs from selected Flutter revision')


def preflight(args):
    if not re.fullmatch(r'emulator-[0-9]+', args.serial or ''):
        raise FixtureError('An explicitly owned emulator serial is required')
    required = [args.cli / 'bin/shorebird', args.cli / 'bin/internal/flutter.version', args.cli / 'bin/internal/engine.version',
                args.flutter / 'bin/internal/engine.version', args.flutter / 'bin/flutter', args.tooling_dart,
                args.android_sdk / 'platform-tools/adb', args.java_home / 'bin/java', args.header, args.artifact_proxy,
                args.server_source / 'go.mod']
    if any(not path.is_file() for path in required) or any(not path.is_dir() for path in (args.artifact_mirror, args.pub_cache)):
        raise FixtureError('Matching packaged tooling, SDK, mirror, caches or header missing')
    for name in ('go', 'git'):
        if shutil.which(name) is None:
            raise FixtureError('Required source tool missing: ' + name)
    verify_toolchain_alignment(args)


class Lifecycle:
    # Portable methods only: never construct or inherit the Windows lifecycle.
    stage = portable.Lifecycle.stage
    snapshot = portable.Lifecycle.snapshot
    rows = portable.Lifecycle.rows
    request = portable.Lifecycle.request

    def __init__(self, args):
        self.args, self.work = args, args.work
        self.api = self.provision = self.observer = self.mirror = self.proxy = None
        self.proxy_url = None
        self.logs, self.reverses, self.reverse_pending = [], set(), set()
        self.work_owned = self.installed = False
        self.active = None
        self.process_tokens = set()
        self.package = 'dev.shorebird.e2e.a' + secrets.token_hex(12)
        self.control = secrets.token_hex(32)
        self.secrets = {name: secrets.token_urlsafe(48) for name in ('master', 'jwt', 'password', 'provision')}
        self.secrets['signing'] = base64.b64encode(secrets.token_bytes(32)).decode()
        self.authority = KeyAuthority(self.secrets['master'].encode(), self.secrets['provision'])
        self.api_url = 'http://127.0.0.1:' + str(free_port())
        self.token = None
        self.manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [],
            'validation': 'real Android CLI/server/installed release lifecycle',
            'key_storage': 'AndroidKeyStore AES-GCM wrapped per-patch keys',
            'key_persistence_scope': 'cold app processes on owned emulator; hardware backing not claimed',
            'inventory_scope': 'final-files-only', 'transient_write_trace': False}
        # Root provides a macOS-safe retained ownership implementation. Import
        # only on execution, so plan and protocol tests need no host primitives.
        from owned_host_processes import OwnedHostProcesses
        self.job = OwnedHostProcesses()

    def environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ('SHOREBIRD_', 'PATCH_DELIVERY_', 'PATCH_SIGNING_', 'FLUTTER_', 'GRADLE_'))}
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(name, None)
        env.update({'HOME': str(self.work / 'home'), 'GRADLE_USER_HOME': str(self.work / 'gradle'),
            'GRADLE_OPTS': '-Dorg.gradle.daemon=false', 'JAVA_HOME': str(self.args.java_home),
            'ANDROID_HOME': str(self.args.android_sdk), 'ANDROID_SDK_ROOT': str(self.args.android_sdk),
            'PUB_CACHE': str(self.work / 'pub-cache'), 'GOCACHE': str(self.work / 'go-cache'),
            'GOPATH': str(self.work / 'go-path'), 'FLUTTER_ROOT': str(self.work / 'flutter'),
            'SHOREBIRD_HOSTED_URL': self.api_url, 'AUTH_SERVICE_URL': self.api_url + '/auth',
            'CI': 'true', 'BOT': 'true', 'FLUTTER_SUPPRESS_ANALYTICS': 'true',
            'DART_SUPPRESS_ANALYTICS': 'true', 'SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS': '0',
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
            'PATH': str(self.work / 'flutter/bin') + os.pathsep + str(self.args.java_home / 'bin') + os.pathsep + os.environ['PATH']})
        if self.mirror:
            env['SHOREBIRD_ARTIFACT_BASE_URL'] = self.mirror.url
            for name in ('SHOREBIRD_DOWNLOAD_URL', 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL', 'FLUTTER_STORAGE_BASE_URL'):
                env[name] = self.proxy_url or self.mirror.url
        if self.token:
            env['SHOREBIRD_TOKEN'] = 'sb_api_' + self.token
        return env

    def run_command(self, command, *, cwd=None, env=None, name='command'):
        self.manifest['operation'] = name
        with (self.work / (name + '.private.log')).open('ab') as log:
            child = self.job.spawn([str(v) for v in command], cwd=cwd or ROOT,
                env=env or self.environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                code = child.wait(timeout=self.args.build_timeout)
            finally:
                self.job.stop(child)
        if code:
            raise FixtureError(name + ' failed; private diagnostics removed during cleanup')

    def output(self, command, cwd=None, *, allow_failure=False):
        child = self.job.spawn([str(v) for v in command], cwd=cwd or ROOT, env=self.environment(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            value, _ = child.communicate(timeout=60)
            if child.returncode and not allow_failure:
                raise FixtureError('Owned source or device command failed')
            return value
        finally:
            self.job.stop(child)

    def adb(self, *arguments, allow_failure=False):
        return self.output([self.args.android_sdk / 'platform-tools/adb', '-s', self.args.serial,
                            *arguments], allow_failure=allow_failure).decode('utf-8', errors='strict').strip()

    def reverse_rows(self):
        # adb() always pins the explicitly owned serial and output() bounds the
        # query. A malformed inventory cannot establish removal ownership.
        rows = [line.split() for line in self.adb('reverse', '--list').splitlines()]
        if any(len(row) != 3 for row in rows):
            raise FixtureError('Malformed adb reverse inventory')
        return rows

    def reverse(self, url):
        port = urllib.parse.urlsplit(url).port
        name = f'tcp:{port}'
        matches = [row for row in self.reverse_rows() if name in row[1:]]
        if matches:
            if name in self.reverses and len(matches) == 1 and matches[0][1:] == [name, name]:
                return
            raise FixtureError('Refusing an existing adb reverse mapping')
        # Record the absent-before-write attempt before issuing the mutation.
        # adb can create the entry and then lose its response. Pending attempts
        # are never blindly removed: reconciliation must prove the exact pair.
        self.reverse_pending.add(name)
        try:
            self.adb('reverse', '--no-rebind', name, name)
        except Exception:
            try:
                current = [row for row in self.reverse_rows() if row[1] == name]
            except Exception:
                raise FixtureError('Owned adb reverse creation result is uncertain') from None
            if current == []:
                self.reverse_pending.discard(name)
                raise FixtureError('Owned adb reverse creation was not confirmed') from None
            if len(current) != 1 or current[0][2] != name:
                self.reverse_pending.discard(name)
                raise FixtureError('Refusing a changed adb reverse mapping') from None
            # An exact pair absent at preflight confirms the uncertain command
            # took effect; it can now be used and later cleaned up as owned.
        self.reverses.add(name)
        self.reverse_pending.discard(name)

    def remove_reverse(self, name):
        if name not in self.reverses | self.reverse_pending:
            raise FixtureError('Refusing to remove an unowned adb reverse mapping')
        current = [row for row in self.reverse_rows() if row[1] == name]
        if current:
            if len(current) != 1 or current[0][2] != name:
                self.reverses.discard(name)
                self.reverse_pending.discard(name)
                raise FixtureError('Refusing to remove a rebound adb reverse mapping')
            self.adb('reverse', '--remove', name)
        self.reverses.discard(name)
        self.reverse_pending.discard(name)

    def prepare(self):
        self.work.mkdir(mode=0o700)
        self.work_owned = True
        for name in ('home', 'gradle'):
            (self.work / name).mkdir(mode=0o700)
        if (self.adb('get-state') != 'device' or
                self.adb('shell', 'getprop', 'ro.product.cpu.abi') != 'arm64-v8a' or
                self.adb('shell', 'getprop', 'ro.kernel.qemu') != '1'):
            raise FixtureError('Owned emulator must be online Android arm64')
        sdk_level = self.adb('shell', 'getprop', 'ro.build.version.sdk')
        if not sdk_level.isdecimal() or int(sdk_level) < 24:
            raise FixtureError('Owned emulator must support the fixture minimum API')
        if self.adb('shell', 'pm', 'path', self.package, allow_failure=True):
            raise FixtureError('Refusing an existing Android package')
        for source, name in ((self.args.cli, 'cli'), (self.args.flutter, 'flutter'), (self.args.pub_cache, 'pub-cache')):
            shutil.copytree(source, self.work / name, symlinks=False)
        self.snapshot(self.args.server_source, self.work / 'server-src')
        from android_native_app import ArtifactMirror, inject_app
        self.mirror = ArtifactMirror(self.args.artifact_mirror)
        shutil.copy2(self.args.artifact_proxy, self.work / 'artifact-proxy')
        self.start_proxy()
        flutter_ref = (self.work / 'cli/bin/internal/flutter.version').read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{40}', flutter_ref):
            raise FixtureError('Invalid packaged Flutter revision pin')
        vended = self.work / 'cli/bin/cache/flutter' / flutter_ref
        if vended.exists():
            shutil.rmtree(vended)
        shutil.copytree(self.work / 'flutter', vended)
        self.cli = self.work / 'cli/bin/shorebird'
        self.run_command(['go', 'build', '-o', self.work / 'server', './cmd/server'], cwd=self.work / 'server-src', name='compile-server')
        shutil.copytree(FIXTURE, self.work / 'app', ignore=shutil.ignore_patterns(
            '.git', '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', '.cxx', '.externalNativeBuild', 'windows', 'linux', 'macos', 'ios', 'tool'))
        pubspec = self.work / 'app/pubspec.yaml'
        pubspec.write_text(re.sub(r'^version:.*\n?', '', pubspec.read_text(), flags=re.M) + '\nversion: 1.0.0+1\n')
        (self.work / 'app/lib/main.dart').write_text(DART_FIXTURE)
        inject_app(self.work / 'app', self.package, sha256(self.control.encode()), self.args.header)
        self.native_sources = tree_hashes(self.work / 'app/android')
        self.manifest.setdefault('sources', {})['packaged-tooling'] = {
            'cli_sha256': file_hash(self.cli), 'tooling_dart_sha256': file_hash(self.args.tooling_dart),
            'provider_header_sha256': file_hash(self.args.header),
            'artifact_proxy_sha256': file_hash(self.args.artifact_proxy),
            'cli_flutter_revision': flutter_ref,
            'flutter_revision': (self.work / 'flutter/bin/internal/engine.version').read_text().strip()}
        if self.args.source_record:
            self.manifest['packaged_sources'] = source_record(self.args.source_record)
        self.stage('isolated-packaged-toolchain')
        self.observer = Observer()
        self.reverse(self.observer.base)
        self.reverse(self.api_url)

    def start_proxy(self):
        port = free_port()
        self.proxy_url = 'http://127.0.0.1:' + str(port)
        env = dict(self.environment(), PORT=str(port), SHOREBIRD_ARTIFACT_BASE_URL=self.mirror.url,
                   ARTIFACT_PROXY_FLUTTER_BASE_URL='https://storage.googleapis.com')
        log = (self.work / 'artifact-proxy.private.log').open('ab')
        self.logs.append(log)
        self.proxy = self.job.spawn([str(self.work / 'artifact-proxy')], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        def ready():
            if self.proxy.poll() is not None:
                raise FixtureError('Owned packaged artifact proxy exited')
            with socket.socket() as connection:
                connection.settimeout(1)
                return connection.connect_ex(('127.0.0.1', port)) == 0
        wait_for(ready, 30, 'owned packaged artifact proxy readiness')

    def start_api(self):
        env = dict(self.environment(), DB_DRIVER='sqlite', DB_PATH=str(self.work / 'server.sqlite'),
            STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(self.work / 'objects'), SERVER_HOST='127.0.0.1',
            SERVER_PORT=self.api_url.rsplit(':', 1)[1], SERVER_BASE_URL=self.api_url, JWT_SECRET=self.secrets['jwt'],
            PATCH_SIGNING_MASTER_KEY=self.secrets['signing'], PATCH_DELIVERY_ENCRYPTION='per_device_aes_gcm',
            PATCH_DELIVERY_AES_SECRET=self.secrets['master'], PATCH_DELIVERY_CACHE_ENCRYPTED='true', SMTP_HOST='',
            DEFAULT_ADMIN_EMAIL='android-e2e@example.invalid', DEFAULT_ADMIN_PASSWORD=self.secrets['password'])
        log = (self.work / 'server.private.log').open('ab')
        self.logs.append(log)
        self.api = self.job.spawn([str(self.work / 'server')], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        def health():
            if self.api.poll() is not None:
                raise FixtureError('Disposable API exited')
            return self.request('GET', '/health', authenticated=False)
        wait_for(health, 30, 'real API readiness')

    def start_provision(self):
        self.provision = portable.LocalService(self.authority.provision, '/key')
        self.reverse(self.provision.url)

    def bootstrap(self):
        login = self.request('POST', '/auth/token', {'email': 'android-e2e@example.invalid',
            'password': self.secrets['password']}, authenticated=False)
        self.token = login['token']
        if login.get('must_change_password'):
            password = secrets.token_urlsafe(32)
            self.request('PATCH', '/api/v1/users/me/password', {'current_password': self.secrets['password'], 'new_password': password})
            self.secrets['password'] = password
        org = self.request('POST', '/api/v1/organizations', {'name': 'Disposable Android native E2E'})
        app = self.request('POST', '/api/v1/apps', {'organization_id': org['id'], 'display_name': 'Android native encrypted lifecycle'})
        self.app_id = str(uuid.UUID(app['id']))
        (self.work / 'app/shorebird.yaml').write_text(f'app_id: {self.app_id}\nbase_url: {self.api_url}\nauto_update: true\n'
            'patch_verification: strict\npatch_response_verification: required\nnative_patch_max_bytes: 268435456\n')

    def build(self, patch=False):
        if patch:
            current = tree_hashes(self.work / 'app/android')
            if any(current.get(path) != digest for path, digest in self.native_sources.items()
                   if not path.startswith(('.gradle/', 'local.properties'))):
                raise FixtureError('Native fixture sources changed between release and patch')
        kind = 'patch' if patch else 'release'
        self.run_command(build_command(self.cli, patch=patch, release_version=getattr(self, 'release_version', None)),
                         cwd=self.work / 'app', name=kind)
        if not patch:
            built = self.work / 'app/build/app/outputs/flutter-apk/app-release.apk'
            self.apk = self.work / 'baseline.apk'
            shutil.copy2(built, self.apk)
            self.apk_hash = file_hash(self.apk)
            self.native_entries = archive_native_entries(self.apk)
            from android_native_app import verify_apk_manifest
            self.stage('released-merged-manifest', verify_apk_manifest(self.apk, self.package, self.args.android_sdk, java_home=self.args.java_home))
            with zipfile.ZipFile(self.apk) as archive:
                config = archive.read('assets/flutter_assets/shorebird.yaml').decode()
            policy = self.work / 'policy-check/data/flutter_assets'
            policy.mkdir(parents=True)
            (policy / 'shorebird.yaml').write_text(config)
            self.stage('released-native-runtime-policy', portable.verify_release_policy(self.work / 'policy-check', self.app_id))
            release = self.rows('SELECT id, version FROM releases WHERE app_id=?', (self.app_id,))
            if len(release) != 1:
                raise FixtureError('Expected one actual Android CLI release')
            self.release_id, self.release_version = release[0]
            # The absent unique package establishes cleanup ownership before
            # install: an uncertain/partial command still needs uninstall.
            self.installed = True
            self.adb('install', '--no-streaming', str(self.apk))
            self.assert_apk()
            self.stage('actual-cli-release-free', {'release_version': self.release_version, 'apk_sha256': self.apk_hash,
                                                  'native_entries': self.native_entries})
        else:
            aab = self.work / 'app/build/app/outputs/bundle/release/app-release.aab'
            native = archive_native_entries(aab, aab=True)
            if {k: v for k, v in native.items() if k != 'libapp.so'} != {k: v for k, v in self.native_entries.items() if k != 'libapp.so'}:
                raise FixtureError('Packaged engine or JNI native bytes changed')
            rows = self.rows('SELECT p.id,p.number,a.hash,a.storage_key,a.arch,a.hash_signature FROM patches p '
                'JOIN patch_artifacts a ON a.patch_id=p.id WHERE p.release_id=? AND a.platform=?', (self.release_id, 'android'))
            if len(rows) != 1:
                raise FixtureError('Expected one real Android patch artifact')
            self.patch_id, self.number, self.patch_hash, storage, arch, signature = rows[0]
            if not signature or self.patch_hash != native['libapp.so']:
                raise FixtureError('Published signed patch differs from actual CLI AAB native image')
            self.transport_hash = file_hash(self.work / 'objects/releases' / storage)
            self.stage('actual-cli-patch-pro', {'number': self.number, 'hash': self.patch_hash,
                'transport_hash': self.transport_hash, 'artifact_arch': arch, 'managed_signature_present': True})
            self.stage('rebuilt-native-bytes-identical', {k: v for k, v in native.items() if k != 'libapp.so'})

    def assert_apk(self):
        if file_hash(self.apk) != self.apk_hash:
            raise FixtureError('Preserved baseline APK changed')
        paths = self.adb('shell', 'pm', 'path', self.package).splitlines()
        if len(paths) != 1 or not paths[0].startswith('package:/data/app/') or not paths[0].endswith('/base.apk'):
            raise FixtureError('Unexpected installed baseline APK path')
        path = paths[0][len('package:'):]
        import shlex
        actual = self.adb('shell', 'sha256sum ' + shlex.quote(path)).split()
        if len(actual) != 2 or actual[0] != self.apk_hash:
            raise FixtureError('Installed baseline APK changed')

    def pid(self):
        value = self.adb('shell', 'pidof', self.package, allow_failure=True)
        if not value:
            return None
        if not re.fullmatch(r'[1-9][0-9]*', value):
            raise FixtureError('Android app is not in one process')
        return int(value)

    def stop_app(self):
        if self.installed:
            self.adb('shell', 'am', 'force-stop', self.package)
            if self.pid() is not None:
                raise FixtureError('Owned package did not stop')
        self.active = None

    def control_command(self, operation, *, fresh=True, online=False, mode='correct', **fields):
        if fresh:
            self.stop_app()
        nonce = secrets.token_hex(24)
        extras = {'control': self.control, 'operation': operation, 'nonce': nonce,
                  'observer': self.observer.base + '/native'}
        if operation in ('launch', 'probe'):
            extras.update(mode=mode, receipt_url=self.observer.base + '/receipt')
            if online:
                if self.api is None or self.provision is None:
                    raise FixtureError('Online control requires live fixture services')
                extras.update(provision_url=self.provision.url, provision_token=self.secrets['provision'])
            elif self.api is not None or self.provision is not None:
                raise FixtureError('Offline control requires stopped fixture services')
        extras.update(fields)
        # Quote each extra for the device shell: AAD contains literal newlines.
        # adb concatenates shell arguments; host argv alone is insufficient.
        import shlex
        command = ['am', 'start', '-W', '-n', self.package + '/.ControlActivity']
        for name, value in extras.items():
            command += ['--es', name, value]
        self.adb('shell', ' '.join(shlex.quote(v) for v in command))
        expected = 'ready' if operation == 'launch' else operation
        def report():
            value = self.observer.get('/native', nonce)
            if value is not None:
                return validate_native(value, nonce, expected,
                    pid=None if fresh else self.active['pid'], process_token=None if fresh else self.active['process_token'])
        result = wait_for(report, self.args.runtime_timeout, 'native ' + expected)
        if self.pid() != result['pid']:
            raise FixtureError('Native report PID differs from actual package PID')
        if fresh:
            if result['process_token'] in self.process_tokens:
                raise FixtureError('Cold launch reused a previous process token')
            self.process_tokens.add(result['process_token'])
        return nonce, result

    def launch(self, label, phase, number, *, mode='correct', online=False):
        self.assert_apk()
        nonce, ready = self.control_command('launch', mode=mode, online=online)
        self.active = ready
        def receipt():
            value = self.observer.get('/receipt', nonce)
            if value is not None:
                if self.pid() != ready['pid']:
                    raise FixtureError('Post-frame report package PID changed')
                return validate_report(value, nonce, ready['pid'], ready['process_token'], number, phase)
        result = wait_for(receipt, self.args.runtime_timeout, label + ' rendered frame and boot patch')
        evidence = {k: v for k, v in result.items() if k not in ('nonce', 'pid', 'process_token')}
        evidence.update(native_ready_pid_matched=True, process_token_matched=True, fresh_process=True)
        self.stage(label, evidence)

    def inspect(self, *, require_key=False):
        fields = {}
        if getattr(self.authority, 'allowed_aad', None):
            fields = {'key_id': delivery_material(self.authority.secret, self.authority.allowed_aad)[0],
                      'aad': self.authority.allowed_aad}
        _, report = self.control_command('inspect', fresh=self.active is None, **fields)
        files = inspect_inventory(report['payload'], patch_hash=getattr(self, 'patch_hash', None), require_key=require_key)
        return files, report['payload']

    def candidate(self):
        return candidate_evidence(self.inspect()[0], self.number)

    def boot_confirmed(self):
        state = self.inspect(require_key=True)[0].get('pointers.json', {}).get('json', {})
        return (type(state.get('last_booted_patch')) is int and state['last_booted_patch'] == self.number and
                'currently_booting_patch' in state and state['currently_booting_patch'] is None)

    def go_offline(self):
        self.job.stop(self.api)
        self.api = None
        if self.provision:
            url = self.provision.url
            self.provision.close()
            self.provision = None
            urls = (self.api_url, url)
        else:
            urls = (self.api_url,)
        for url in urls:
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(('127.0.0.1', urllib.parse.urlsplit(url).port)) == 0:
                    raise FixtureError('Owned online service still accepting connections')
        self.stage('api-and-provisioning-stopped')

    def probe_context(self):
        aad = self.authority.allowed_aad
        key_id, key = delivery_material(self.authority.secret, aad)
        challenge = secrets.token_hex(32)
        expected = hmac.new(key, bytes.fromhex(challenge), hashlib.sha256).hexdigest()
        accepted = self.authority.accepted
        _, online = self.control_command('probe', online=True, key_id=key_id, aad=aad, challenge=challenge)
        if not hmac.compare_digest(online['payload']['mac_hex'], expected) or self.authority.accepted <= accepted:
            raise FixtureError('Native callback online challenge failed')
        accepted = self.authority.accepted
        self.go_offline()
        _, offline = self.control_command('probe', key_id=key_id, aad=aad, challenge=challenge)
        if (not hmac.compare_digest(offline['payload']['mac_hex'], expected) or
                offline['process_token'] == online['process_token'] or self.authority.accepted != accepted):
            raise FixtureError('Fresh-process Keystore callback challenge failed')
        _, cleared = self.control_command('clear-provider')
        self.stage('native-callback-online-and-offline', {'native_thread_attached': True,
            'fresh_process': True, 'probe_record_cleared_before_app_download': cleared['payload']['records'] == 0})
        self.start_api()
        self.start_provision()

    def execute(self):
        self.prepare()
        self.start_api()
        self.bootstrap()
        self.start_provision()
        self.build()
        self.launch('initial-online-BASE', 'BASE', 0, online=True)
        def registration():
            state = self.inspect()[0].get('state.json', {}).get('json', {})
            if not isinstance(state.get('client_id'), str) or not state['client_id']:
                return None
            rows = self.rows('SELECT DISTINCT arch,platform,release_version FROM patch_events WHERE app_id=? AND client_id=? AND event_type=?',
                             (self.app_id, state['client_id'], 'PatchCheck'))
            return (state, rows[0]) if len(rows) == 1 else None
        state, (arch, runtime_platform, version) = wait_for(registration, self.args.runtime_timeout, 'genuine updater context')
        if (arch, runtime_platform, version) != ('aarch64', 'android', self.release_version):
            raise FixtureError('Actual Android updater runtime context mismatch')
        self.stage('genuine-updater-registration', {'arch': arch, 'platform': runtime_platform})
        self.stop_app()
        self.build(patch=True)
        self.authority.allow({'app_id': self.app_id, 'release_id': self.release_id, 'patch_number': self.number,
            'patch_hash': self.patch_hash, 'transport_hash': self.transport_hash, 'platform': runtime_platform,
            'arch': arch, 'device_id_hash': sha256(state['client_id'].encode())})
        self.probe_context()
        accepted = self.authority.accepted
        self.launch('online-BASE-download', 'BASE', 0, online=True)
        candidate = wait_for(self.candidate, self.args.runtime_timeout, 'actual encrypted transport installation')
        if self.authority.accepted <= accepted:
            raise FixtureError('Real app did not provision delivery key after probe clear')
        objects = (self.work / 'objects/patches').rglob('*')
        if not any(path.is_file() and file_hash(path) == candidate['transport_sha256'] for path in objects):
            raise FixtureError('Installed ciphertext differs from actual managed delivery object')
        self.stage('ciphertext-installed', candidate)
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-U', 'U', self.number)
        wait_for(self.boot_confirmed, self.args.runtime_timeout, 'real U boot success')
        self.stop_app()
        for mode in ('missing', 'wrong'):
            self.launch('offline-' + mode + '-BASE', 'BASE', 0, mode=mode)
            if self.candidate() != candidate:
                raise FixtureError('Unavailable provider mutated encrypted candidate')
            self.stop_app()
            self.launch('offline-' + mode + '-recovery-U', 'U', self.number)
            wait_for(self.boot_confirmed, self.args.runtime_timeout, 'recovered U boot success')
            self.stop_app()
        _, inventory = self.inspect(require_key=True)
        self.stage('final-cache-before-rollback', sanitized_inventory(inventory))
        self.stop_app()
        self.start_api()
        self.start_provision()
        self.request('POST', f'/api/v1/apps/{self.app_id}/releases/{self.release_id}/patches/{self.patch_id}/rollback')
        self.launch('online-U-receive-signed-rollback', 'U', self.number, online=True)
        def rolled_back():
            state = self.inspect(require_key=True)[0].get('pointers.json', {}).get('json', {})
            return (set(('next_boot_patch', 'last_booted_patch', 'currently_booting_patch')) <= set(state) and
                    state['next_boot_patch'] is None and state['last_booted_patch'] is None and
                    state['currently_booting_patch'] is None)
        wait_for(rolled_back, self.args.runtime_timeout, 'real authenticated rollback')
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-rollback-BASE', 'BASE', 0)
        self.stage('final-cache-after-rollback', sanitized_inventory(self.inspect(require_key=True)[1]))
        self.stop_app()
        self.assert_apk()
        self.stage('installed-baseline-apk-unchanged', {'sha256': self.apk_hash})
        self.manifest['provisioning'] = {'accepted': self.authority.accepted, 'rejected': self.authority.rejected,
            'context_source': 'actual updater state, PatchCheck architecture and signed published artifact'}
        self.manifest['result'] = 'passed'

    def cleanup(self):
        failures = []
        def attempt(action):
            try:
                action()
            except Exception:
                failures.append(True)
        attempt(self.stop_app)
        if self.installed:
            attempt(lambda: self.adb('uninstall', self.package))
        for mapping in self.reverses | self.reverse_pending:
            attempt(lambda value=mapping: self.remove_reverse(value))
        attempt(self.job.close)
        for service in (self.provision, self.observer, self.mirror):
            if service:
                attempt(service.close)
        for log in self.logs:
            attempt(log.close)
        if self.work_owned:
            attempt(lambda: shutil.rmtree(self.work))
        if failures:
            raise FixtureError('Owned Android lifecycle cleanup failed')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('phase', choices=('plan', 'run'), nargs='?', default='plan')
    result.add_argument('--execute', action='store_true')
    for name in ('work', 'results', 'cli', 'flutter', 'tooling-dart', 'server-source',
                 'artifact-mirror', 'artifact-proxy', 'pub-cache', 'android-sdk', 'java-home'):
        result.add_argument('--' + name, type=Path)
    result.add_argument('--header', type=Path, default=ROOT / 'flutter/engine/src/flutter/shell/common/shorebird/shorebird_patch_encryption.h')
    result.add_argument('--source-record', type=Path)
    result.add_argument('--serial')
    result.add_argument('--runtime-timeout', type=int, default=120)
    result.add_argument('--build-timeout', type=int, default=1800)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.phase == 'plan':
        print(json.dumps({'schema': SCHEMA, 'read_only': True, 'requires': [
            'pre-staged matching packaged CLI, Flutter SDK, tooling Dart, artifact mirror/proxy and pub cache',
            'owned arm64 Android emulator serial, preinstalled SDK/NDK and Java', 'server source and Go'],
            'lifecycle': ['actual CLI APK release and AAB patch', 'genuine updater registration and architecture',
                'native callback provision and cold-process Keystore HMAC probe', 'ciphertext-only installation',
                'offline rendered patch, missing/wrong BASE plus recovery', 'signed rollback and offline BASE',
                'unchanged installed baseline APK'], 'inventory_scope': 'final-files-only',
            'transient_write_trace': False}, indent=2))
        return 0
    if not args.execute:
        raise SystemExit('run requires --execute')
    for name in ('work', 'results', 'cli', 'flutter', 'tooling_dart', 'server_source', 'artifact_mirror', 'artifact_proxy', 'pub_cache', 'android_sdk', 'java_home', 'header'):
        value = getattr(args, name)
        if value is None:
            raise SystemExit('run requires all explicit staged input paths')
        setattr(args, name, value.resolve())
    if args.source_record is not None:
        args.source_record = args.source_record.resolve()
    if (args.work.exists() or args.results.exists() or args.work == args.results or
            args.work in args.results.parents or args.results in args.work.parents):
        raise SystemExit('Use separate new work and results directories')
    if any(args.work == getattr(args, name) or args.work in getattr(args, name).parents or getattr(args, name) in args.work.parents
           for name in ('cli', 'flutter', 'server_source', 'artifact_mirror', 'pub_cache', 'android_sdk', 'java_home')):
        raise SystemExit('Disposable work must not contain staged inputs')
    args.results.mkdir(parents=True, mode=0o700)
    lifecycle = None
    manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [], 'transient_write_trace': False}
    try:
        preflight(args)
        lifecycle = Lifecycle(args)
        manifest = lifecycle.manifest
        lifecycle.execute()
    except Exception as error:
        manifest['failure'] = {'type': type(error).__name__, 'after_stage':
            manifest['stages'][-1]['name'] if manifest['stages'] else 'preflight-or-preparation'}
        if isinstance(error, FixtureError):
            manifest['failure']['reason'] = str(error)
        print('Android lifecycle failed; sanitized evidence retained.', flush=True)
    finally:
        try:
            if lifecycle:
                lifecycle.cleanup()
            manifest['cleanup'] = ('owned package, reverse mappings, host services and private work removed'
                if lifecycle else 'no lifecycle resources created; work untouched')
        except Exception as error:
            manifest['cleanup'] = 'failed: ' + type(error).__name__
            manifest['result'] = 'failed'
        private_write(args.results / 'results.json', json.dumps(manifest, indent=2))
    return 0 if manifest['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

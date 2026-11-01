#!/usr/bin/env python3
"""Real, isolated Windows native encrypted-cache lifecycle (Python stdlib only).

`plan` is read-only on every host. `run --execute` requires Windows, complete
prebuilt local engine/SDK artifacts, VS desktop C++ tooling, Go, Git, and a
working public updater patch.exe. No engine build or updater-state seeding.
Only results.json survives cleanup; raw build/server logs and credentials do not.
The cache audit covers final files, NOT transient process writes (no ETW claim).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / 'testapps/license_flavor_patch_test'
SCHEMA = 'windows-native-cache-e2e-v1'
AAD_PREFIX = 'open-aot-patch-delivery-v2'
AAD_FIELDS = ('app_id', 'release_id', 'patch_number', 'patch_hash',
              'transport_hash', 'platform', 'arch', 'device_id_hash')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
MAX_BODY = 32768
PUBLIC_FLUTTER_STORAGE = 'https://storage.googleapis.com'
FAILURE_SCAN_BYTES = 64 * 1024


class FixtureError(RuntimeError):
    """Only authored, secret-free diagnostic messages may survive cleanup."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def command_failure(name, code, timed_out, path):
    """Retain only bounded, predefined diagnostics; never log text or captures."""
    size = path.stat().st_size
    with path.open('rb') as source:
        head = source.read(FAILURE_SCAN_BYTES).lower()
        source.seek(max(0, size - FAILURE_SCAN_BYTES))
        tail = source.read(FAILURE_SCAN_BYTES).lower()
    def contains(value):
        return value in head or value in tail
    classification = 'unknown'
    if contains(b'could not find package sky_engine in the flutter sdk'):
        classification = 'missing_sky_engine'
    elif (contains(b'material_fonts') or contains(b'material fonts')) and contains(b'localhost:8080'):
        classification = 'material_fonts_local_artifact_server'
    elif contains(b'the flutter sdk is not available'):
        classification = 'flutter_sdk_unavailable'
    elif contains(b'version solving failed'):
        classification = 'pub_solver'
    return {'operation': name, 'exit_code': code, 'timed_out': timed_out,
            'log_bytes': size, 'log_sha256': file_hash(path),
            'classification': classification,
            'classification_scan_truncated': size > 2 * FAILURE_SCAN_BYTES}


def stage_local_engine_packages(host, flutter):
    # LocalEngineArtifacts skips FlutterSdk downloads; pub still searches this
    # cache for sky_engine. Copy generated contents before copying the vended SDK.
    source = host / 'gen/dart-pkg/sky_engine'
    if not (source / 'pubspec.yaml').is_file():
        raise FixtureError('Prebuilt generated sky_engine package missing')
    target = flutter / 'bin/cache/pkg/sky_engine'
    try:
        target.parent.resolve().relative_to(flutter.resolve())
    except ValueError:
        raise FixtureError('Private Flutter package cache escapes owned SDK') from None
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        remove_owned_tree(target)
    shutil.copytree(source, target)


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def canonical_aad(context):
    if set(context) != set(AAD_FIELDS):
        raise ValueError('Unknown delivery fields')
    aad = AAD_PREFIX + ''.join('\n' + name + '=' + str(context[name]) for name in AAD_FIELDS)
    if parse_aad(aad) != {name: str(context[name]) for name in AAD_FIELDS}:
        raise ValueError('Invalid delivery context')
    return aad


def parse_aad(aad):
    if not isinstance(aad, str) or len(aad.encode('utf-8')) > MAX_BODY:
        raise ValueError('Invalid AAD')
    lines = aad.split('\n')
    if len(lines) != 9 or lines[0] != AAD_PREFIX:
        raise ValueError('Invalid AAD structure')
    result = {}
    for name, line in zip(AAD_FIELDS, lines[1:]):
        key, separator, value = line.partition('=')
        if key != name or separator != '=' or not value or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid AAD field')
        result[name] = value
    if str(uuid.UUID(result['app_id'])) != result['app_id']:
        raise ValueError('Noncanonical app UUID')
    for name in ('release_id', 'patch_number'):
        if re.fullmatch(r'[1-9][0-9]{0,18}', result[name]) is None:
            raise ValueError('Invalid numeric context')
    for name in ('patch_hash', 'transport_hash', 'device_id_hash'):
        if HEX64.fullmatch(result[name]) is None:
            raise ValueError('Invalid digest')
    if result['platform'] != 'windows' or result['arch'] != 'x86_64':
        raise ValueError('Unsupported runtime context')
    return result


def delivery_material(secret, aad):
    """Matches Go device_delivery.go, which treats its env secret as raw UTF8."""
    parse_aad(aad)
    def digest(label):
        return hmac.new(secret, label + b'\0' + aad.encode(), hashlib.sha256).digest()
    key_id = base64.urlsafe_b64encode(digest(b'key-id')[:12]).rstrip(b'=').decode()
    return key_id, digest(b'key')


class KeyAuthority:
    """One exact allowlisted genuine updater context; never a general KDF API."""
    def __init__(self, secret, token):
        self.secret, self.token, self.allowed_aad = secret, token, None
        self.accepted = 0
        self.rejected = 0
        self.lock = threading.Lock()

    def allow(self, context):
        self.allowed_aad = canonical_aad(context)

    def provision(self, authorization, body):
        with self.lock:
            try:
                if not isinstance(authorization, str) or not hmac.compare_digest(
                        authorization, 'Bearer ' + self.token):
                    raise ValueError('Authorization rejected')
                if len(body) > MAX_BODY:
                    raise ValueError('Oversized request')
                request = strict_json(body.decode('utf-8'))
                if not isinstance(request, dict) or set(request) != {'key_id', 'aad'}:
                    raise ValueError('Invalid request fields')
                aad, key_id = request['aad'], request['key_id']
                parse_aad(aad)
                if not isinstance(key_id, str) or aad != self.allowed_aad:
                    raise ValueError('Unregistered context')
                expected, key = delivery_material(self.secret, aad)
                if not hmac.compare_digest(key_id, expected):
                    raise ValueError('Mismatching key identifier')
                self.accepted += 1
                return key.hex().encode('ascii')
            except (ValueError, UnicodeError, TypeError, AttributeError):
                self.rejected += 1
                raise ValueError('Provisioning rejected') from None


def read_body(handler):
    if handler.headers.get('Transfer-Encoding') is not None:
        raise ValueError('Transfer encoding forbidden')
    lengths = handler.headers.get_all('Content-Length', [])
    if len(lengths) != 1 or re.fullmatch(r'[0-9]+', lengths[0]) is None:
        raise ValueError('Missing or duplicate length')
    length = int(lengths[0])
    if not 0 < length <= MAX_BODY:
        raise ValueError('Invalid request length')
    content_types = handler.headers.get_all('Content-Type', [])
    if len(content_types) != 1 or content_types[0] not in (
            'application/json', 'application/json; charset=utf-8'):
        raise ValueError('Invalid content type')
    handler.connection.settimeout(5)
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError('Truncated body')
    return body


class LocalService:
    def __init__(self, callback, path):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != path:
                    self.send_error(404)
                    return
                try:
                    headers = self.headers.get_all('Authorization', [])
                    if len(headers) > 1:
                        raise ValueError('Duplicate authorization')
                    body = callback(headers[0] if headers else None, read_body(self))
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ValueError, OSError, UnicodeError):
                    self.send_error(403)
            def log_message(self, *_):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}{path}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def validate_report(report, nonce, process_id, expected_patch, phase):
    if not isinstance(report, dict) or set(report) != {
            'schema', 'nonce', 'pid', 'post_frame', 'rendered', 'current_patch'}:
        raise ValueError('Invalid rendered receipt')
    expected = ['license:pro', 'pro-feature:enabled'] if phase == 'U' else [
        'license:free', 'pro-feature:off']
    if (report['schema'] != SCHEMA or report['nonce'] != nonce or
            type(report['pid']) is not int or report['pid'] != process_id or
            report['post_frame'] is not True or report['rendered'] != expected or
            type(report['current_patch']) is not int or report['current_patch'] != expected_patch):
        raise ValueError('Rendered selection does not match expected launch')
    return report


# Same source is compiled into free and pro; only LICENSE_TYPE changes. The
# receipt reads actual RenderParagraph objects after layout/paint, plus the real
# updater running selection through the DLL public FFI (no Dart mock/plugin).
DART_FIXTURE = r'''import 'dart:async';
import 'dart:convert';
import 'dart:ffi';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'license.dart';

final licenseKey = GlobalKey();
final featureKey = GlobalKey();
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(MaterialApp(home: Scaffold(body: Column(children: [
    Text('license:${currentLicenseType().name}', key: licenseKey),
    Text(proFeatureEnabled ? 'pro-feature:enabled' : 'pro-feature:off', key: featureKey),
  ]))));
  WidgetsBinding.instance.addPostFrameCallback((_) { unawaited(reportFrame()); });
}
Future<void> reportFrame() async {
  final rendered = <String>[];
  for (final key in [licenseKey, featureKey]) {
    final object = key.currentContext?.findRenderObject();
    if (object is! RenderParagraph || !object.attached || !object.hasSize || object.size.isEmpty) {
      throw StateError('Missing rendered paragraph');
    }
    rendered.add(object.text.toPlainText());
  }
  final current = DynamicLibrary.open('flutter_windows.dll')
    .lookupFunction<UintPtr Function(), int Function()>('shorebird_current_boot_patch_number')();
  final receipt = jsonEncode({
    'schema': 'windows-native-cache-e2e-v1', 'nonce': Platform.environment['SHOREBIRD_E2E_RECEIPT'],
    'pid': pid, 'post_frame': true, 'rendered': rendered, 'current_patch': current,
  });
  for (var attempt = 0; attempt < 50; attempt++) {
    final client = HttpClient()..findProxy = (_) => 'DIRECT';
    try {
      final request = await client.postUrl(Uri.parse(Platform.environment['SHOREBIRD_E2E_OBSERVER']!));
      request.headers.contentType = ContentType.json;
      request.contentLength = utf8.encode(receipt).length;
      request.add(utf8.encode(receipt));
      final response = await request.close();
      await response.drain<void>();
      if (response.statusCode == 200) return;
    } catch (_) {} finally { client.close(force: true); }
    await Future<void>.delayed(const Duration(milliseconds: 100));
  }
  throw StateError('Receipt observer unavailable');
}
'''


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
        output.write(value.encode() if isinstance(value, str) else value)


def replace_once(path, marker, replacement):
    content = path.read_text()
    if content.count(marker) != 1:
        raise FixtureError(f'Isolated source injection marker changed: {path.name}')
    path.write_text(content.replace(marker, replacement, 1))


def inject_provider(app):
    runner = app / 'windows/runner'
    if (runner / 'runner_patch_key_provider.cpp').exists():
        raise FixtureError('Native provider was already injected')
    for name in ('runner_patch_key_provider.h', 'runner_patch_key_provider.cpp'):
        shutil.copy2(FIXTURE / 'tool/windows_native' / name, runner / name)
    replace_once(runner / 'main.cpp', '#include "flutter_window.h"',
                 '#include "runner_patch_key_provider.h"\n#include "flutter_window.h"')
    replace_once(runner / 'main.cpp', '  flutter::DartProject project(L"data");',
                 '  if (!RegisterWindowsNativeCacheTestProvider()) return EXIT_FAILURE;\n'
                 '  flutter::DartProject project(L"data");')
    replace_once(runner / 'CMakeLists.txt', '  "main.cpp"',
                 '  "main.cpp"\n  "runner_patch_key_provider.cpp"')
    with (runner / 'CMakeLists.txt').open('a') as output:
        output.write('\ntarget_link_libraries(${BINARY_NAME} PRIVATE winhttp crypt32 bcrypt)\n'
                     '# Fixed before the release; native SHA equality still decides acceptance.\n'
                     'target_link_options(${BINARY_NAME} PRIVATE\n'
                     '  "$<$<CONFIG:Release>:/Brepro>"\n'
                     '  "$<$<CONFIG:Release>:/INCREMENTAL:NO>"\n'
                     '  "$<$<CONFIG:Release>:/PDBALTPATH:%_PDB%>")\n')


def local_engine_flags(args):
    return ['--local-engine-src-path=' + str(args.engine_src),
            '--local-engine=' + args.target_engine,
            '--local-engine-host=' + args.host_engine]


def create_windows_command(args, work):
    # Select LocalEngineArtifacts before `create` runs pub get or precache;
    # otherwise Flutter may replace the private SDK's engine package artifacts.
    return [work / 'flutter/bin/cache/dart-sdk/bin/dart.exe',
            '--packages=' + str(work / 'flutter/packages/flutter_tools/.dart_tool/package_config.json'),
            work / 'flutter/packages/flutter_tools/bin/flutter_tools.dart',
            *local_engine_flags(args), 'create', '--platforms=windows',
            '--project-name=license_flavor_patch_test', '.']


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): file_hash(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def verify_release_policy(bundle, app_id):
    # Deliberately parse only known single-line scalars from the generated asset;
    # accepting arbitrary YAML constructs here would hide compiler-policy drift.
    content = (bundle / 'data/flutter_assets/shorebird.yaml').read_text()
    def scalar(name):
        matches = re.findall(r'^' + name + r':\s*([^\r\n]+)$', content, re.M)
        if len(matches) != 1:
            raise FixtureError('Released runtime policy missing or ambiguous: ' + name)
        return matches[0].strip().strip('\"\'')
    for name, expected in (('app_id', app_id), ('patch_verification', 'strict'),
                           ('patch_response_verification', 'required'),
                           ('native_patch_max_bytes', '268435456')):
        if scalar(name) != expected:
            raise FixtureError('Released runtime policy mismatch: ' + name)
    key = scalar('patch_public_key')
    try:
        if not base64.b64decode(key, validate=True):
            raise ValueError('Empty key')
    except ValueError:
        raise FixtureError('Released managed verification key missing') from None
    if re.search(r'^aot_patch_runtime_mode:', content, re.M):
        raise FixtureError('Native fixture unexpectedly selected a bytecode runtime')
    return {'patch_verification': 'strict', 'patch_response_verification': 'required',
            'native_patch_max_bytes': 268435456, 'managed_public_key_sha256': sha256(key.encode())}


def scan_cache(cache, key_cache, *, patch_hash, forbidden_secrets=()):
    """Final-file oracle only. This cannot observe files created then deleted."""
    files = []
    for root, key_only in ((cache, False), (key_cache, True)):
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise FixtureError('Unexpected cache symlink')
            if not path.is_file():
                continue
            digest = file_hash(path)
            with path.open('rb') as source:
                magic = source.read(4)
                # Keep bounded memory even for a corrupt unexpectedly large file.
                source.seek(0)
                overlap = b''
                longest = max((len(value) for value in forbidden_secrets), default=1)
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    combined = overlap + block
                    if any(value and value in combined for value in forbidden_secrets):
                        raise FixtureError('Plaintext secret persisted in app cache')
                    overlap = combined[-(longest - 1):] if longest > 1 else b''
            if (digest == patch_hash or magic.startswith(b'MZ') or magic == b'\x7fELF' or
                    path.name in ('app.so', 'dlc.vmcode')):
                raise FixtureError('Plaintext executable persisted in app cache')
            if key_only and (not re.fullmatch(r'[0-9a-f]{64}\.dpapi', path.name) or
                             not 0 < path.stat().st_size <= 16384):
                raise FixtureError('Unexpected DPAPI cache file')
            files.append({'root': 'dpapi' if key_only else 'updater',
                          'path': path.relative_to(root).as_posix(),
                          'sha256': digest, 'size': path.stat().st_size})
    return {'scope': 'final-files-only', 'transient_write_trace': False, 'files': files}


def installed_evidence(cache, number):
    directory = cache / 'patches' / str(number)
    state = strict_json((directory / 'state.json').read_text())
    pointers = strict_json((cache / 'pointers.json').read_text())
    if (state.get('kind') != 'InstalledEncryptedTransportV1' or
            not state.get('signature') or pointers.get('next_boot_patch') != number):
        raise ValueError('Encrypted candidate not installed and selected')
    transport = directory / 'transport.aesgcm'
    if not transport.is_file() or transport.stat().st_size != state['ciphertext_size']:
        raise ValueError('Ciphertext size mismatch')
    return {'kind': state['kind'], 'transport_sha256': file_hash(transport),
            'ciphertext_size': state['ciphertext_size'], 'signature_present': True,
            'next_boot_patch': number}


def wait_for(predicate, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, KeyError, sqlite3.OperationalError):
            pass
        time.sleep(.15)
    raise FixtureError('Timed out waiting for ' + label)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def preflight(args):
    host = args.engine_src / 'out' / args.host_engine
    target = args.engine_src / 'out' / args.target_engine
    required = [host / 'dart-sdk/bin/dart.exe', host / 'dart-sdk/bin/dartaotruntime.exe',
                host / 'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot',
                host / 'gen/const_finder.dart.snapshot', host / 'font-subset.exe',
                host / 'flutter_windows.dll', host / 'flutter_windows.dll.lib',
                host / 'icudtl.dat', host / 'flutter_windows.h',
                host / 'flutter_export.h', host / 'flutter_messenger.h',
                host / 'flutter_plugin_registrar.h', host / 'flutter_texture_registrar.h',
                host / 'cpp_client_wrapper/include/flutter/dart_project.h',
                host / 'gen/dart-pkg/sky_engine/pubspec.yaml',
                target / 'flutter_patched_sdk/platform_strong.dill',
                target / 'flutter_patched_sdk_product/platform_strong.dill',
                args.patch_tool, args.tooling_dart]
    missing = [str(path) for path in required if not path.is_file()]
    if not any((target / directory / 'gen_snapshot.exe').is_file() for directory in
               ('.', 'universal', 'clang_x64', 'clang_x86', 'clang_i386', 'clang_arm64', 'clang_riscv64')):
        missing.append(str(target / '[Flutter-supported directory]/gen_snapshot.exe') +
                       ' (if GN built gen_snapshot/gen_snapshot.exe, stage that prebuilt binary at out-root/gen_snapshot.exe)')
    for name in ('cli', 'flutter'):
        source = ROOT / ('shorebird' if name == 'cli' else name)
        config = (source / '.dart_tool/package_config.json' if name == 'cli' else
                  source / 'packages/flutter_tools/.dart_tool/package_config.json')
        if not config.is_file():
            missing.append(str(config))
    if missing:
        raise FixtureError('Prebuilt local engine/SDK inputs missing (no engine build is performed):\n' + '\n'.join(missing))
    for executable in ('git', 'go'):
        if shutil.which(executable) is None:
            raise FixtureError('Required executable missing: ' + executable)


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def remove_owned_tree(path):
    # Git marks pack files read-only on Windows. Only session-owned trees reach here.
    def writable_remove(function, filename, _error):
        os.chmod(filename, stat.S_IWRITE | stat.S_IREAD)
        function(filename)
    shutil.rmtree(path, onerror=writable_remove)


class OwnedWindowsJob:
    """Bound CLI/build/server/Runner descendants to this session's lifetime."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                        ('maximum_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IOCounters),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise FixtureError('Cannot create private Windows process job')
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise FixtureError('Cannot bind owned Windows process cleanup')

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            stop_process(process)
            raise FixtureError('Cannot assign owned process to private job')
        return process

    def resume_primary_thread(self, process_id):
        import ctypes
        from ctypes import wintypes
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                        ('thread_id', wintypes.DWORD), ('owner_pid', wintypes.DWORD),
                        ('base_priority', wintypes.LONG), ('delta_priority', wintypes.LONG),
                        ('flags', wintypes.DWORD)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise FixtureError('Cannot enumerate suspended owned process thread')
        identifiers = []
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.owner_pid == process_id:
                    identifiers.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                available = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        # CREATE_SUSPENDED prevents the primary thread from running loader code
        # or spawning a descendant. Refuse to resume an unexpected thread set.
        if len(identifiers) != 1:
            raise FixtureError('Suspended owned process lacks one primary thread')
        thread = self.kernel.OpenThread(0x0002, False, identifiers[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            raise FixtureError('Cannot open owned suspended primary thread')
        try:
            if self.kernel.ResumeThread(thread) != 1:
                raise FixtureError('Cannot resume owned primary thread exactly once')
        finally:
            self.kernel.CloseHandle(thread)

    def spawn(self, command, **kwargs):
        # Python closes CreateProcess's primary-thread handle before returning.
        # Recover that owned PID's sole thread with documented Toolhelp APIs.
        process = subprocess.Popen(command, creationflags=0x00000004, **kwargs)  # CREATE_SUSPENDED
        try:
            self.attach(process)
            self.resume_primary_thread(process.pid)
            return process
        except BaseException:
            stop_process(process)
            raise

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Lifecycle:
    def __init__(self, args):
        self.args, self.work = args, args.work
        self.api = self.provision = self.observer = self.app = None
        self.logs = []
        self.cache = None
        self.work_owned = False
        self.cache_owned = False
        self.receipts = {}
        self.receipt_lock = threading.Lock()
        self.secrets = {'master': secrets.token_urlsafe(48), 'jwt': secrets.token_urlsafe(48),
                        'password': secrets.token_urlsafe(32), 'provision': secrets.token_urlsafe(32),
                        'signing': base64.b64encode(secrets.token_bytes(32)).decode()}
        self.authority = KeyAuthority(self.secrets['master'].encode(), self.secrets['provision'])
        self.manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [],
                         'validation': 'real Windows CLI/server/Runner lifecycle',
                         'transient_write_trace': False}
        self.api_url = 'http://127.0.0.1:' + str(free_port())
        self.token = None
        self.job = OwnedWindowsJob()

    def stage(self, name, value=True):
        self.manifest['stages'].append({'name': name, 'evidence': value})
        print(name + ': verified', flush=True)

    def run_command(self, command, *, cwd=None, env=None, name='command'):
        self.manifest['operation'] = name
        path = self.work / (name + '.private.log')
        with path.open('ab') as log:
            process = self.job.spawn([str(x) for x in command], cwd=cwd or ROOT,
                                     env=env, stdout=log, stderr=log)
            code, timed_out = None, False
            try:
                code = process.wait(timeout=self.args.build_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                stop_process(process)
        if code or timed_out:
            self.manifest['command_failure'] = command_failure(name, code, timed_out, path)
            # Raw log/args can contain signed URLs or credentials. Never export them.
            raise FixtureError(name + ' failed (private log removed during cleanup)')
        if re.search(rb'\b(?:LNK4044|D9002)\b', path.read_bytes()):
            raise FixtureError(name + ' ignored a required toolchain option')

    def output(self, command, cwd=None):
        process = self.job.spawn([str(x) for x in command], cwd=cwd or ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _errors = process.communicate(timeout=60)
            if process.returncode:
                raise FixtureError('Source snapshot command failed')
            return output
        finally:
            stop_process(process)

    def snapshot(self, source, target):
        ref = self.output(['git', '-C', source, 'rev-parse', 'HEAD']).decode().strip()
        self.run_command(['git', 'clone', '--no-hardlinks', '--no-checkout', source, target], name='clone-' + source.name)
        self.run_command(['git', '-C', target, 'checkout', '--detach', ref], name='checkout-' + source.name)
        delta = self.output(['git', '-C', source, 'diff', '--binary', 'HEAD'])
        if delta:
            patch = self.work / (source.name + '.private.patch')
            private_write(patch, delta)
            self.run_command(['git', '-C', target, 'apply', patch], name='overlay-' + source.name)
        untracked = self.output(['git', '-C', source, 'ls-files', '--others', '--exclude-standard', '-z'])
        for raw in untracked.split(b'\0'):
            if raw:
                relative = Path(os.fsdecode(raw))
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, destination)
        self.manifest.setdefault('sources', {})[source.name] = {'head': ref, 'overlay_sha256': sha256(delta)}

    def remap_config(self, source, destination, mappings):
        config = strict_json(source.read_text())
        for package in config['packages']:
            uri = urllib.parse.urljoin(source.as_uri(), package['rootUri'])
            absolute = Path(urllib.request.url2pathname(urllib.parse.urlparse(uri).path)).resolve()
            for old, new in mappings:
                try:
                    absolute = new / absolute.relative_to(old.resolve())
                    break
                except ValueError:
                    pass
            package['rootUri'] = absolute.as_uri()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(config))

    def environment(self):
        env = dict(os.environ)
        # APPDATA redirects only this private CLI. Runner gets original known folders.
        for name in tuple(env):
            if name.startswith(('SHOREBIRD_', 'PATCH_DELIVERY_', 'PATCH_SIGNING_')):
                del env[name]
        env.update({'APPDATA': str(self.work / 'config'),
                    'SHOREBIRD_HOSTED_URL': self.api_url,
                    'AUTH_SERVICE_URL': self.api_url + '/auth',
                    'SHOREBIRD_ARTIFACT_BASE_URL': self.api_url,
                    'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': PUBLIC_FLUTTER_STORAGE,
                    'FLUTTER_STORAGE_BASE_URL': PUBLIC_FLUTTER_STORAGE,
                    'FLUTTER_ROOT': str(self.work / 'flutter'),
                    'PUB_CACHE': str(self.work / 'pub-cache'),
                    'PATH': str(self.work / 'flutter/bin') + os.pathsep + os.environ['PATH'],
                    'CI': 'true', 'BOT': 'true', 'FLUTTER_SUPPRESS_ANALYTICS': 'true',
                    'DART_SUPPRESS_ANALYTICS': 'true', 'SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS': '0'})
        if self.token:
            env['SHOREBIRD_TOKEN'] = 'sb_api_' + self.token
        return env

    def prepare(self):
        self.work.mkdir(mode=0o700)
        self.work_owned = True
        # Python's mode handling differs across Windows versions. Set and verify
        # a protected inheritable DACL for the actual account before writing logs,
        # tokens, copied credentials, or DPAPI blobs anywhere in the session.
        account = self.output(['whoami', '/user', '/fo', 'csv', '/nh']).decode(errors='replace')
        sids = re.findall(r'S-1-[0-9]+(?:-[0-9]+)+', account)
        if len(sids) != 1:
            raise FixtureError('Cannot identify the current Windows account SID')
        self.output(['icacls', self.work, '/inheritance:r', '/grant:r', '*' + sids[0] + ':(OI)(CI)F'])
        # Inherit no mutable checkout/cache aliases; copy snapshots and caches.
        self.snapshot(ROOT / 'shorebird', self.work / 'cli')
        self.snapshot(ROOT / 'flutter', self.work / 'flutter')
        shutil.copytree(ROOT / 'flutter/bin/cache', self.work / 'flutter/bin/cache', dirs_exist_ok=True)
        pub = self.args.pub_cache
        shutil.copytree(pub, self.work / 'pub-cache')
        sdk = self.work / 'flutter/bin/cache/dart-sdk'
        remove_owned_tree(sdk)
        shutil.copytree(self.args.engine_src / 'out' / self.args.host_engine / 'dart-sdk', sdk)
        stage_local_engine_packages(self.args.engine_src / 'out' / self.args.host_engine,
                                    self.work / 'flutter')
        mappings = [(ROOT / 'shorebird', self.work / 'cli'), (ROOT / 'flutter', self.work / 'flutter'),
                    (pub, self.work / 'pub-cache')]
        self.remap_config(ROOT / 'shorebird/.dart_tool/package_config.json',
                          self.work / 'cli/.dart_tool/package_config.json', mappings)
        self.remap_config(ROOT / 'flutter/packages/flutter_tools/.dart_tool/package_config.json',
                          self.work / 'flutter/packages/flutter_tools/.dart_tool/package_config.json', mappings)
        flutter_ref = self.manifest['sources']['flutter']['head']
        engine_id = (self.work / 'flutter/bin/internal/engine.version').read_text().strip()
        for name, value in (('flutter.version', flutter_ref), ('engine.version', engine_id)):
            (self.work / 'cli/bin/internal' / name).write_text(value + '\n')
        # Real CLI expects its vended SDK here. Copy, avoiding symlink privileges on Windows.
        vended = self.work / 'cli/bin/cache/flutter' / flutter_ref
        shutil.copytree(self.work / 'flutter', vended)
        # Bootstrap must not replace the prebuilt custom SDK. Use normal source entrypoint.
        command = '@echo off\r\n"%~dp0cache\\dart-sdk\\bin\\dart.exe" "--packages=%~dp0..\\packages\\flutter_tools\\.dart_tool\\package_config.json" "%~dp0..\\packages\\flutter_tools\\bin\\flutter_tools.dart" %*\r\n'
        for base in (self.work / 'flutter', vended):
            (base / 'bin/flutter.bat').write_text(command)
        self.cli = self.work / 'cli/bin/shorebird-e2e.exe'
        self.run_command([self.args.tooling_dart, 'compile', 'exe',
                          '--packages=' + str(self.work / 'cli/.dart_tool/package_config.json'),
                          self.work / 'cli/packages/shorebird_cli/bin/shorebird.dart', '-o', self.cli],
                         env=self.environment(), name='compile-cli')
        artifact = self.work / 'cli/bin/cache/artifacts/patch'
        artifact.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.args.patch_tool, artifact / 'patch.exe')
        (artifact / 'patch.exe.stamp').touch()
        self.run_command(['go', 'build', '-o', self.work / 'server.exe', './cmd/server'],
                         cwd=ROOT / 'shorebird-server', name='compile-server')
        shutil.copytree(FIXTURE, self.work / 'app', ignore=shutil.ignore_patterns(
            '.git', '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', 'windows'))
        pubspec = self.work / 'app/pubspec.yaml'
        content = re.sub(r'^version:.*\n?', '', pubspec.read_text(), flags=re.M)
        pubspec.write_text(content + '\nversion: 1.0.0+1\n')
        (self.work / 'app/lib/main.dart').write_text(DART_FIXTURE)
        self.run_command(create_windows_command(self.args, self.work),
                         cwd=self.work / 'app', env=self.environment(), name='generate-windows')
        inject_provider(self.work / 'app')
        self.native_sources = tree_hashes(self.work / 'app/windows')
        self.stage('isolated-toolchain', {'engine_dll_sha256': file_hash(self.args.engine_src / 'out' / self.args.host_engine / 'flutter_windows.dll'),
                                          'patch_tool_sha256': file_hash(self.args.patch_tool)})

    def request(self, method, path, body=None, *, authenticated=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if authenticated and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.api_url + path, data, headers, method=method)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
            content = response.read()
        return strict_json(content) if content else None

    def start_api(self):
        env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(self.work / 'server.sqlite'),
                   STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(self.work / 'objects'),
                   SERVER_HOST='127.0.0.1', SERVER_PORT=self.api_url.rsplit(':', 1)[1],
                   SERVER_BASE_URL=self.api_url, JWT_SECRET=self.secrets['jwt'],
                   PATCH_SIGNING_MASTER_KEY=self.secrets['signing'],
                   PATCH_DELIVERY_ENCRYPTION='per_device_aes_gcm',
                   PATCH_DELIVERY_AES_SECRET=self.secrets['master'], PATCH_DELIVERY_CACHE_ENCRYPTED='true',
                   SMTP_HOST='', DEFAULT_ADMIN_EMAIL='windows-e2e@example.invalid',
                   DEFAULT_ADMIN_PASSWORD=self.secrets['password'])
        log = (self.work / 'server.private.log').open('ab')
        self.logs.append(log)
        self.api = self.job.spawn([str(self.work / 'server.exe')], env=env, stdout=log, stderr=log)
        def health():
            if self.api.poll() is not None:
                raise FixtureError('Disposable API exited')
            return self.request('GET', '/health', authenticated=False)
        wait_for(health, 30, 'real Go API readiness')

    def bootstrap(self):
        login = self.request('POST', '/auth/token', {
            'email': 'windows-e2e@example.invalid', 'password': self.secrets['password']}, authenticated=False)
        self.token = login['token']
        if login.get('must_change_password'):
            password = secrets.token_urlsafe(32)
            self.request('PATCH', '/api/v1/users/me/password', {
                'current_password': self.secrets['password'], 'new_password': password})
            self.secrets['password'] = password
        org = self.request('POST', '/api/v1/organizations', {'name': 'Disposable Windows native E2E'})
        app = self.request('POST', '/api/v1/apps', {'organization_id': org['id'], 'display_name': 'Windows native encrypted lifecycle'})
        self.app_id = str(uuid.UUID(app['id']))
        # SHGetKnownFolderPath ignores LOCALAPPDATA env overrides. Scope cleanup to
        # the random server-created app UUID under the actual current user's folder.
        import ctypes
        from ctypes import wintypes
        folder = ctypes.c_void_p()
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
        shell = ctypes.WinDLL('shell32')
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(folder)) != 0:
            raise FixtureError('Cannot determine actual LocalAppData known folder')
        try:
            base = Path(ctypes.wstring_at(folder))
        finally:
            ole = ctypes.WinDLL('ole32')
            ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole.CoTaskMemFree(folder)
        candidate = base / 'shorebird_updater' / self.app_id
        # An empty directory does not seed any updater state. Exclusive creation
        # establishes ownership before the Runner can create genuine state files.
        try:
            candidate.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            raise FixtureError('Refusing existing updater app cache') from None
        self.cache = candidate
        self.cache_owned = True
        self.key_cache = self.work / 'dpapi'
        self.key_cache.mkdir()
        (self.work / 'app/shorebird.yaml').write_text(
            f'app_id: {self.app_id}\nbase_url: {self.api_url}\nauto_update: true\n'
            'patch_verification: strict\npatch_response_verification: required\n'
            'native_patch_max_bytes: 268435456\n')

    def build(self, patch=False):
        kind = 'patch' if patch else 'release'
        if patch:
            current = tree_hashes(self.work / 'app/windows')
            # Flutter creates ephemeral output below windows/flutter. Only sources
            # recorded before release participate in this immutability check.
            if any(current.get(name) != digest for name, digest in self.native_sources.items()):
                raise FixtureError('Native Runner fixture changed between release and patch')
        command = [self.cli, '--json', *local_engine_flags(self.args),
                   kind, 'windows', '--dart-define=LICENSE_TYPE=' + ('pro' if patch else 'free')]
        if patch:
            command.extend(['--release-version=' + self.release_version, '--track=stable'])
        self.run_command(command, cwd=self.work / 'app', env=self.environment(), name=kind)
        build = self.work / 'app/build/windows/x64/runner/Release'
        if not patch:
            self.bundle = self.work / 'released'
            shutil.copytree(build, self.bundle)
            self.bundle_hashes = tree_hashes(self.bundle)
            self.runner = self.bundle / 'license_flavor_patch_test.exe'
            if not self.runner.is_file():
                raise FixtureError('Expected generated released Runner missing')
            with (self.bundle / 'data/app.so').open('rb') as image:
                if image.read(4) != b'\x7fELF':
                    raise FixtureError('Released native AOT image is not an ELF snapshot')
            self.stage('released-native-runtime-policy', verify_release_policy(self.bundle, self.app_id))
            release = self.rows('SELECT id, version FROM releases WHERE app_id=?', (self.app_id,))
            if len(release) != 1:
                raise FixtureError('Expected one actual CLI release')
            self.release_id, self.release_version = release[0]
            cmake = (self.work / 'app/build/windows/x64/CMakeCache.txt').read_text()
            match = re.search(r'^CMAKE_LINKER:FILEPATH=(.+)$', cmake, re.M)
            if not match or not Path(match.group(1)).is_file():
                raise FixtureError('Cannot identify the exact Windows linker')
            linker = Path(match.group(1))
            version = self.output([linker, '/?']).decode(errors='replace').splitlines()[0]
            self.manifest['native_linker'] = {'sha256': file_hash(linker), 'version': version[:200],
                                               'release_options': ['/Brepro', '/INCREMENTAL:NO', '/PDBALTPATH:%_PDB%']}
            self.stage('actual-cli-release-free', {'release_version': self.release_version, 'bundle': self.bundle_hashes})
        else:
            built_hashes = tree_hashes(build)
            native = {name: digest for name, digest in self.bundle_hashes.items()
                      if Path(name).suffix.lower() in ('.exe', '.dll')}
            current_native = {name: digest for name, digest in built_hashes.items()
                              if Path(name).suffix.lower() in ('.exe', '.dll')}
            if not native or native != current_native:
                raise FixtureError('Actual rebuilt Runner/engine/plugin bytes changed')
            self.stage('rebuilt-native-bytes-identical', native)
            rows = self.rows('SELECT p.id, p.number, a.hash, a.storage_key, a.arch, a.hash_signature '
                             'FROM patches p JOIN patch_artifacts a ON a.patch_id=p.id '
                             'WHERE p.release_id=? AND a.platform=?', (self.release_id, 'windows'))
            if len(rows) != 1:
                raise FixtureError('Expected one real Windows patch artifact')
            self.patch_id, self.number, self.patch_hash, storage, artifact_arch, signature = rows[0]
            if not signature:
                raise FixtureError('Server-managed artifact signature missing')
            if file_hash(build / 'data/app.so') != self.patch_hash:
                raise FixtureError('Published patch does not match actual CLI-built native AOT image')
            transport = self.work / 'objects/releases' / storage
            if not transport.is_file():
                raise FixtureError('Published original patch transport missing')
            self.transport_hash = file_hash(transport)
            self.stage('actual-cli-patch-pro', {'number': self.number, 'hash': self.patch_hash,
                                              'transport_hash': self.transport_hash, 'artifact_arch': artifact_arch,
                                              'managed_signature_present': True})

    def rows(self, query, arguments=()):
        # Read-only DB access observes genuine CLI/server state; never writes it.
        with sqlite3.connect((self.work / 'server.sqlite').as_uri() + '?mode=ro', uri=True) as database:
            return database.execute(query, arguments).fetchall()

    def make_observer(self):
        def observe(_authorization, body):
            report = strict_json(body)
            if not isinstance(report, dict) or not isinstance(report.get('nonce'), str):
                raise ValueError('Invalid receipt')
            with self.receipt_lock:
                self.receipts[report['nonce']] = report
            return b'ok'
        self.observer = LocalService(observe, '/frame')

    def launch(self, label, phase, number, *, mode='correct', online=False):
        if tree_hashes(self.bundle) != self.bundle_hashes:
            raise FixtureError('Preserved released bundle changed')
        nonce = secrets.token_hex(24)
        env = dict(os.environ)
        for name in tuple(env):
            if name.startswith(('SHOREBIRD_', 'PATCH_DELIVERY_', 'PATCH_SIGNING_')):
                del env[name]
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(name, None)
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
        env.update({'SHOREBIRD_E2E_KEY_CACHE_DIR': str(self.key_cache), 'SHOREBIRD_E2E_KEY_MODE': mode,
                    'SHOREBIRD_E2E_OBSERVER': self.observer.url, 'SHOREBIRD_E2E_RECEIPT': nonce})
        if online:
            env.update({'SHOREBIRD_E2E_PROVISION_URL': self.provision.url,
                        'SHOREBIRD_E2E_PROVISION_TOKEN': self.secrets['provision']})
        elif self.api is not None or self.provision is not None:
            raise FixtureError('Offline launch requires stopped API and provisioning services')
        log = (self.work / (label + '.private.log')).open('ab')
        self.logs.append(log)
        self.app = self.job.spawn([str(self.runner)], cwd=self.bundle, env=env, stdout=log, stderr=log)
        def receipt():
            if self.app.poll() is not None:
                raise FixtureError('Released Runner exited before post-frame receipt')
            with self.receipt_lock:
                report = self.receipts.get(nonce)
            if report is not None:
                return validate_report(report, nonce, self.app.pid, number, phase)
        report = wait_for(receipt, self.args.runtime_timeout, label + ' rendered status/current patch')
        self.stage(label, {key: value for key, value in report.items() if key not in ('nonce', 'pid')})
        return report

    def stop_app(self):
        stop_process(self.app)
        self.app = None

    def go_offline(self):
        stop_process(self.api)
        self.api = None
        if self.provision:
            self.provision.close()
            self.provision = None
        # Listener closure is evidence of offline fixture services; no proxies.
        for url in (self.api_url, getattr(self, 'provision_url', self.api_url)):
            parsed = urllib.parse.urlparse(url)
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(('127.0.0.1', parsed.port)) == 0:
                    raise FixtureError('Owned online service is still accepting connections')
        self.stage('api-and-provisioning-stopped')

    def candidate(self):
        return installed_evidence(self.cache, self.number)

    def boot_confirmed(self):
        state = strict_json((self.cache / 'pointers.json').read_text())
        return state.get('last_booted_patch') == self.number and state.get('currently_booting_patch') is None

    def execute(self):
        self.prepare()
        self.start_api()
        self.bootstrap()
        self.make_observer()
        # No patch exists during first registration boot. Provisioning stays closed.
        self.provision = LocalService(self.authority.provision, '/key')
        self.provision_url = self.provision.url
        self.build()
        self.launch('initial-online-BASE', 'BASE', 0, online=True)
        def registration():
            state = strict_json((self.cache / 'state.json').read_text())
            events = self.rows('SELECT DISTINCT arch, platform, release_version FROM patch_events WHERE app_id=? AND client_id=? AND event_type=?',
                               (self.app_id, state['client_id'], 'PatchCheck'))
            if len(events) == 1:
                return state, events[0]
        state, (runtime_arch, runtime_platform, version) = wait_for(registration, self.args.runtime_timeout, 'genuine updater client and runtime architecture')
        if runtime_arch != 'x86_64' or runtime_platform != 'windows' or version != self.release_version:
            raise FixtureError('Genuine updater runtime context mismatch')
        self.stop_app()
        self.build(patch=True)
        self.authority.allow({'app_id': self.app_id, 'release_id': self.release_id, 'patch_number': self.number,
                              'patch_hash': self.patch_hash, 'transport_hash': self.transport_hash,
                              'platform': runtime_platform, 'arch': runtime_arch,
                              'device_id_hash': sha256(state['client_id'].encode())})
        self.launch('online-BASE-download', 'BASE', 0, online=True)
        candidate = wait_for(self.candidate, self.args.runtime_timeout, 'actual ciphertext installation')
        if self.authority.accepted < 1:
            raise FixtureError('Real updater did not provision a delivery key')
        # Exact cached ciphertext must be the server's managed encrypted object.
        objects = [p for p in (self.work / 'objects/patches').rglob('*') if p.is_file()]
        if not any(file_hash(p) == candidate['transport_sha256'] for p in objects):
            raise FixtureError('Installed ciphertext differs from real Go delivery')
        if not list(self.key_cache.glob('*.dpapi')):
            raise FixtureError('Online key provisioning did not persist DPAPI cache')
        self.stage('ciphertext-installed', candidate)
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-U', 'U', self.number)
        wait_for(self.boot_confirmed, self.args.runtime_timeout, 'U boot-success lifecycle')
        self.stop_app()
        for mode in ('missing', 'wrong'):
            self.launch('offline-' + mode + '-key-BASE', 'BASE', 0, mode=mode)
            if self.candidate() != candidate:
                raise FixtureError('Failed-key boot destroyed or changed candidate')
            self.stop_app()
            self.launch('offline-' + mode + '-recovery-U', 'U', self.number)
            wait_for(self.boot_confirmed, self.args.runtime_timeout, 'recovered U boot success')
            self.stop_app()
        _key_id, key = delivery_material(self.authority.secret, self.authority.allowed_aad)
        self.forbidden_secrets = (key, key.hex().encode(), self.secrets['master'].encode(),
                                  self.secrets['provision'].encode())
        self.stage('final-cache-before-rollback', scan_cache(
            self.cache, self.key_cache, patch_hash=self.patch_hash, forbidden_secrets=self.forbidden_secrets))
        self.start_api()
        self.request('POST', f'/api/v1/apps/{self.app_id}/releases/{self.release_id}/patches/{self.patch_id}/rollback')
        # API returns signed rollback in the real updater check; no state edits.
        self.provision = LocalService(self.authority.provision, '/key')
        self.provision_url = self.provision.url
        self.launch('online-U-receive-signed-rollback', 'U', self.number, online=True)
        def rolled_back():
            pointers = strict_json((self.cache / 'pointers.json').read_text())
            return pointers.get('next_boot_patch') is None and pointers.get('last_booted_patch') is None
        wait_for(rolled_back, self.args.runtime_timeout, 'authenticated rollback applied by updater')
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-rollback-BASE', 'BASE', 0)
        self.stop_app()
        self.stage('final-cache-after-rollback', scan_cache(
            self.cache, self.key_cache, patch_hash=self.patch_hash, forbidden_secrets=self.forbidden_secrets))
        if tree_hashes(self.bundle) != self.bundle_hashes:
            raise FixtureError('Original release bundle was mutated')
        self.stage('released-bundle-unchanged', self.bundle_hashes)
        self.manifest['provisioning'] = {'accepted': self.authority.accepted, 'rejected': self.authority.rejected,
                                         'context_source': 'genuine updater state + runtime PatchCheck + published signed DB/artifacts'}
        self.manifest['result'] = 'passed'

    def cleanup(self):
        failures = []
        actions = [self.stop_app, lambda: stop_process(self.api), self.job.close]
        actions += [service.close for service in (self.provision, self.observer) if service]
        actions += [log.close for log in self.logs]
        if self.cache_owned and self.cache is not None:
            actions.append(lambda: remove_owned_tree(self.cache) if self.cache.exists() else None)
        if self.work_owned:
            actions.append(lambda: remove_owned_tree(self.work) if self.work.exists() else None)
        for action in actions:
            try:
                action()
            except Exception as error:
                failures.append(type(error).__name__)
        if failures:
            raise FixtureError('Owned resource cleanup failed')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('phase', choices=('plan', 'run'), nargs='?', default='plan')
    result.add_argument('--execute', action='store_true')
    result.add_argument('--work', type=Path)
    result.add_argument('--results', type=Path, help='New directory for sanitized results.json only')
    result.add_argument('--engine-src', type=Path, default=ROOT / 'flutter/engine/src')
    result.add_argument('--host-engine', default='host_release')
    result.add_argument('--target-engine', default='host_release')
    result.add_argument('--tooling-dart', type=Path, default=ROOT / 'flutter/bin/cache/dart-sdk/bin/dart.exe')
    result.add_argument('--patch-tool', type=Path, default=ROOT / 'updater/target/release/patch.exe')
    result.add_argument('--pub-cache', type=Path, default=Path(os.environ.get('PUB_CACHE', Path.home() / 'AppData/Local/Pub/Cache')))
    result.add_argument('--runtime-timeout', type=int, default=120)
    result.add_argument('--build-timeout', type=int, default=1800)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.phase == 'plan':
        print(json.dumps({'schema': SCHEMA, 'read_only': True, 'host': platform.system(),
                          'requires': ['Windows x64 interactive desktop', 'VS desktop C++/CMake', 'Go', 'Git',
                                       'populated CLI/Flutter package configs and private-copyable pub/cache',
                                       'prebuilt host create_sdk Dart SDK/frontend_server/gen_snapshot/const_finder/font-subset',
                                       'Shorebird Windows DLL/import library/headers/cpp_client_wrapper and product platform dill',
                                       'public updater patch.exe'],
                          'lifecycle': ['actual CLI release free', 'real updater registration', 'actual CLI patch pro',
                                        'Go per-device AES-GCM managed signed artifact/response', 'real ciphertext install',
                                        'stop API/provision', 'new-process offline rendered U/current patch',
                                        'missing/wrong key BASE + retained candidate', 'correct offline recovery',
                                        'real signed rollback', 'offline new-process BASE'],
                          'audit': 'final app-cache files only; transient write audit unimplemented'}, indent=2))
        return 0
    if not args.execute:
        raise SystemExit('run requires --execute')
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Full execution requires Windows x64; only plan and pure unit checks are supported on this host')
    if args.work is None or args.results is None:
        raise SystemExit('run requires new --work and --results directories')
    for name in ('work', 'results', 'engine_src', 'tooling_dart', 'patch_tool', 'pub_cache'):
        setattr(args, name, getattr(args, name).resolve())
    if args.work.exists() or args.results.exists() or args.results == args.work or args.work in args.results.parents or args.results in args.work.parents:
        raise SystemExit('Use separate new session/results directories; existing paths are never overwritten')
    args.results.mkdir(parents=True, mode=0o700)
    lifecycle = None
    manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [],
                'validation': 'real Windows CLI/server/Runner lifecycle',
                'transient_write_trace': False}
    try:
        preflight(args)
        lifecycle = Lifecycle(args)
        manifest = lifecycle.manifest
        lifecycle.execute()
    except Exception as error:
        # Do not serialize raw exception strings: HTTP/build errors may carry tokens.
        manifest['failure'] = {'type': type(error).__name__,
                               'after_stage': manifest['stages'][-1]['name'] if manifest['stages'] else
                                              ('preparation' if lifecycle else 'preflight')}
        if isinstance(error, FixtureError):
            manifest['failure']['reason'] = str(error)
        print('Windows lifecycle failed; sanitized stage evidence retained.', flush=True)
    finally:
        try:
            if lifecycle:
                lifecycle.cleanup()
                manifest['cleanup'] = 'owned processes, ports, disposable app cache, private session removed'
            else:
                manifest['cleanup'] = 'no session or runtime resources created; work untouched'
        except Exception as error:
            manifest['cleanup'] = 'failed: ' + type(error).__name__
            manifest['result'] = 'failed'
        private_write(args.results / 'results.json', json.dumps(manifest, indent=2))
    return 0 if manifest['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

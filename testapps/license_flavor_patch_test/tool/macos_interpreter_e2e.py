#!/usr/bin/env python3
"""Isolated macOS full-interpreter fixture; plans are not acceptance.

prepare/base/update/serve/inspect/cleanup require --execute for mutations or
external commands. Signing and application launches are ALWAYS left to the
caller. No device operations, toolchain downloads or engine builds exist here.
The caller supplies a source CLI with isolated config-directory support and a
matching private Flutter SDK/engine. Credentials are disposable test bootstrap
material, not a production license-provisioning implementation.
For teardown, run cleanup --execute --begin, perform the final native delete,
then cleanup --execute. Receipt copies must match the current container record.
Once cleanup starts, launches are refused and retries use its pinned receipt.
An OS-protected metadata-only container shell is explicitly incomplete cleanup.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse
from xml.parsers.expat import ExpatError

import ios_device_e2e as shared
from owned_host_processes import OwnedHostProcesses

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / 'testapps/license_flavor_patch_test'
HELPERS = Path(__file__).with_name('macos_interpreter')
SCHEMA = 'macos-interpreter-e2e-v1'
APP_ENTITLEMENTS = {'com.apple.security.app-sandbox': True,
                    'com.apple.security.network.client': True}
EVIDENCE_RELATIVE = Path('Library/Application Support/shorebird-macos-fixture')
MACH_MAGICS = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
               b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
               b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}


def target_architecture(state):
    """Canonical app target, independent of the preparation tool's host CPU."""
    arch = state.get('arch', 'arm64')  # Preserve pre-option ARM64 state.
    if not isinstance(arch, str) or arch not in ('arm64', 'x64'):
        raise ValueError('Unsupported macOS fixture target architecture')
    return {'arch': arch, 'machine': 'arm64' if arch == 'arm64' else 'x86_64',
            'wire': 'aarch64' if arch == 'arm64' else 'x86_64',
            'flutter_target': 'darwin-' + arch, 'envelope': arch}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def contained(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Refusing path outside this owned scope')
    return path


def load(work):
    state = json.loads((work / 'state.json').read_text())
    if (state.get('schema') != SCHEMA or state.get('work_dir') != str(work.resolve()) or
            not re.fullmatch(r'org\.tonycloud\.shorebird-macos-ffi\.[a-f0-9]{12}',
                             state.get('bundle_id', ''))):
        raise ValueError('Not an owned macOS interpreter fixture')
    target_architecture(state)
    return state


def save(work, state):
    shared.write_json(work / 'state.json', state)


def unique_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(('127.0.0.1', 0))
        second.bind(('127.0.0.1', 0))
        return first.getsockname()[1], second.getsockname()[1]


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(.2)
        return connection.connect_ex(('127.0.0.1', port)) == 0


def normalize_macos_minimum(source):
    # Flutter's supported minimum is 12.0. Normalize the copied template before
    # sealing native hashes, so the first build cannot silently migrate them.
    old = 'MACOSX_DEPLOYMENT_TARGET = 10.15;'
    if source.count('MACOSX_DEPLOYMENT_TARGET') != 3 or source.count(old) != 3:
        raise ValueError('Review changed macOS deployment-target entries')
    return source.replace(old, 'MACOSX_DEPLOYMENT_TARGET = 12.0;')


def prepare_project(app, state):
    """Modify only a fresh copied macOS Runner, never the committed fixture."""
    runner = contained(app, app / 'macos/Runner')
    for name in ('FullFfiFixture.c', 'FullFfiFixture.h'):
        shutil.copyfile(FIXTURE / 'ios/Runner' / name, runner / name)
    helper = (HELPERS / 'PatchTestKeys.swift.template').read_text()
    for key, value in {'__SERVICE__': state['key_service'], '__KEY_ID__': state['key_id'],
                       '__BUNDLE_ID__': state['bundle_id']}.items():
        helper = helper.replace(key, value)
    shared.private_write(runner / 'PatchTestKeys.swift', helper)
    shared.private_write(runner / 'Runner-Bridging-Header.h', '#include "FullFfiFixture.h"\n')
    window = runner / 'MainFlutterWindow.swift'
    marker = '  override func awakeFromNib() {\n'
    source = window.read_text()
    if source.count(marker) != 1 or source.count('FlutterViewController()') != 1:
        raise ValueError('Review changed macOS engine-creation path')
    window.write_text(source.replace(marker, marker + '    MacosInterpreterFixture.beforeEngine()\n'))
    shared.private_write(runner / 'Release.entitlements', plistlib.dumps(APP_ENTITLEMENTS))
    info = plistlib.loads((runner / 'Info.plist').read_bytes())
    info['NSAppTransportSecurity'] = {'NSAllowsLocalNetworking': True}
    shared.private_write(runner / 'Info.plist', plistlib.dumps(info))
    app_info = runner / 'Configs/AppInfo.xcconfig'
    source, count = re.subn(r'^PRODUCT_BUNDLE_IDENTIFIER = .+$',
                           'PRODUCT_BUNDLE_IDENTIFIER = ' + state['bundle_id'],
                           app_info.read_text(), flags=re.MULTILINE)
    if count != 1:
        raise ValueError('Review changed app bundle settings')
    app_info.write_text(source)
    release = runner / 'Configs/Release.xcconfig'
    release.write_text(release.read_text() + '\n' +
                       'ENABLE_HARDENED_RUNTIME = YES\n'
                       'SWIFT_OBJC_BRIDGING_HEADER = Runner/Runner-Bridging-Header.h\n'
                       'OTHER_LDFLAGS = $(inherited) -Wl,-export_dynamic\n'
                       'STRIP_STYLE = non-global\n')
    project = app / 'macos/Runner.xcodeproj/project.pbxproj'
    source = normalize_macos_minimum(project.read_text())
    files = [('A0F100000000000000000001', 'A0F100000000000000000002',
              'FullFfiFixture.c', 'sourcecode.c.c'),
             ('A0F100000000000000000003', 'A0F100000000000000000004',
              'PatchTestKeys.swift', 'sourcecode.swift')]
    if 'A0F100000000000000000001' in source:
        raise ValueError('Runner already contains this fixture')
    build_entries, file_entries, group_entries, source_entries = '', '', '', ''
    for build_id, file_id, name, kind in files:
        build_entries += (f'\t\t{build_id} /* {name} in Sources */ = '
                          f'{{isa = PBXBuildFile; fileRef = {file_id} /* {name} */; }};\n')
        file_entries += (f'\t\t{file_id} /* {name} */ = {{isa = PBXFileReference; '
                         f'lastKnownFileType = {kind}; path = {name}; sourceTree = "<group>"; }};\n')
        group_entries += f'\t\t\t\t{file_id} /* {name} */,\n'
        source_entries += f'\t\t\t\t{build_id} /* {name} in Sources */,\n'
    for marker, addition in [('/* End PBXBuildFile section */', build_entries),
                             ('/* End PBXFileReference section */', file_entries)]:
        if source.count(marker) != 1:
            raise ValueError('Review changed Xcode project structure')
        source = source.replace(marker, addition + marker)
    for object_id, addition in [('33FAB671232836740065AC1E', group_entries),
                                ('33CC10E92044A3C60003C045', source_entries)]:
        pattern = r'(' + object_id + r' /\* [^*]+ \*/ = \{[^}]+?(?:children|files) = \(\n)'
        source, count = re.subn(pattern, lambda match: match[1] + addition, source, count=1)
        if count != 1:
            raise ValueError('Review changed Runner source/group membership')
    project.write_text(source)


def apply_profile(work, phase):
    shared.apply_fixture(work, phase, 'full_replacement_ffi')
    # Preserve the exact report schema/semantic checks, adding a deterministic
    # app-container copy because sandbox TMPDIR is not a stable host path.
    path = work / 'app/lib/ffi_status.dart'
    source = path.read_text()
    marker = '  try {\n    File(\n'
    if source.count(marker) != 1:
        raise ValueError('Review changed FFI report writer')
    source = source.replace(marker, '''  try {
    final home = Platform.environment['HOME'];
    if (home != null && home.isNotEmpty) {
      final fallback = File('$home/Library/Application Support/shorebird-macos-fixture/ffi-status.json');
      fallback.parent.createSync(recursive: true);
      fallback.writeAsStringSync(contents, flush: true);
    }
    File(
''')
    path.write_text(source)


def prepare(args, work):
    target = target_architecture({'arch': getattr(args, 'target_arch', 'arm64')})
    if work.exists() or work.is_symlink():
        raise ValueError('Use a fresh work directory')
    if work.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Private work must be outside the source workspace')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+', args.release_version):
        raise ValueError('Use an explicit numeric name+build release version')
    session = secrets.token_hex(6)
    api, observer = unique_ports()
    state = {'schema': SCHEMA, 'work_dir': str(work.resolve()), 'session': session,
             'created': time.time(), 'platform': 'macos', 'arch': target['arch'],
             'bundle_id': f'org.tonycloud.shorebird-macos-ffi.{session}',
             'key_service': f'org.tonycloud.shorebird-macos-key.{session}',
             'key_id': f'macos-e2e-{session}', 'release_version': args.release_version,
             'full_replacement': True, 'full_replacement_ffi': True,
             'api_url': f'http://127.0.0.1:{api}',
             'report_url': f'http://127.0.0.1:{observer}/report/{session}',
             'cli': str(args.cli.resolve()), 'flutter_root': str(args.flutter_root.resolve()),
             'engine_src': str(args.engine_src.resolve()),
             'engine_name': args.engine_name or 'macos_release_' + target['arch'],
             'host_engine_name': args.host_engine_name or 'macos_release_' + target['arch'],
             'server': str(args.server.resolve())}
    for name in ('engine_name', 'host_engine_name'):
        if not re.fullmatch(r'[a-zA-Z0-9_]+', state[name]):
            raise ValueError('Invalid local engine output name')
        output = Path(state['engine_src']) / 'out' / state[name]
        if output.resolve().is_relative_to((ROOT / 'flutter/engine/src/out').resolve()):
            raise ValueError('Frozen engine output cannot be used by this fixture')
    work.mkdir(parents=True, mode=0o700)
    shared.write_json(work / 'secrets.json', {
        'bootstrap_password': secrets.token_urlsafe(32), 'admin_password': secrets.token_urlsafe(32),
        'jwt_secret': secrets.token_urlsafe(48),
        'signing_master_key': base64.b64encode(secrets.token_bytes(32)).decode(),
        'aes_key_hex': secrets.token_hex(32)})
    shutil.copytree(FIXTURE, work / 'app', symlinks=False,
                    ignore=shutil.ignore_patterns('.git', '.dart_tool', 'build', 'Pods',
                                                 '.symlinks', '.gradle', 'ephemeral'))
    prepare_project(work / 'app', state)
    apply_profile(work, 'base')
    pubspec = work / 'app/pubspec.yaml'
    text = re.sub(r'^version:.*\n?', '', pubspec.read_text(), flags=re.MULTILINE)
    pubspec.write_text(text + '\nversion: ' + args.release_version + '\n')
    shared.private_write(work / 'empty-library-entitlements.plist', plistlib.dumps({}))
    for flavor in ('free', 'pro'):
        shared.write_json(work / f'{flavor}-defines.json',
                          {'LICENSE_TYPE': flavor, 'PATCH_TEST_REPORT_URL': state['report_url']})
    state['native_source_hashes'] = native_hashes(work)
    save(work, state)
    return state


def native_hashes(work):
    files = ['macos/Runner/MainFlutterWindow.swift', 'macos/Runner/PatchTestKeys.swift',
             'macos/Runner/FullFfiFixture.c', 'macos/Runner/FullFfiFixture.h',
             'macos/Runner/Runner-Bridging-Header.h', 'macos/Runner/Release.entitlements',
             'macos/Runner/Configs/Release.xcconfig', 'macos/Runner.xcodeproj/project.pbxproj']
    return {name: digest(contained(work, work / 'app' / name)) for name in files}


def cli_command(work, state, phase):
    command = [state['cli'], '--json', f"--local-engine-src-path={state['engine_src']}",
               f"--local-engine={state['engine_name']}",
               f"--local-engine-host={state['host_engine_name']}",
               'release' if phase == 'base' else 'patch', 'macos', '--no-codesign',
               f'--dart-define-from-file={work}/{"free" if phase == "base" else "pro"}-defines.json']
    if phase == 'update':
        command += [f"--release-version={state['release_version']}", '--track=stable']
    command += ['--', '--target-platform=' + target_architecture(state)['flutter_target']]
    return command


def build_environment(work, state, phase):
    credentials = json.loads((work / 'secrets.json').read_text())
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('SHOREBIRD_AOT_', 'SHOREBIRD_IOS_', 'DYLD_', 'OPEN_SHOREBIRD_TEST_'))}
    env.update({'SHOREBIRD_E2E_CONFIG_DIR': str(work / 'config'),
                'SHOREBIRD_HOSTED_URL': state['api_url'],
                'AUTH_SERVICE_URL': state['api_url'] + '/auth',
                'SHOREBIRD_TOKEN': 'sb_api_' + credentials['access_token'],
                'SHOREBIRD_ARTIFACT_BASE_URL': state['api_url'],
                'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': 'https://storage.googleapis.com',
                'FLUTTER_STORAGE_BASE_URL': 'https://storage.googleapis.com',
                'FLUTTER_ROOT': state['flutter_root'], 'PUB_CACHE': str(work / 'pub-cache'),
                'CI': 'true', 'BOT': 'true', 'FLUTTER_SUPPRESS_ANALYTICS': 'true',
                'DART_SUPPRESS_ANALYTICS': 'true', 'SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS': '0'})
    if phase == 'update':
        env.update({'SHOREBIRD_AOT_PATCH_KEY_HEX': credentials['aes_key_hex'],
                    'SHOREBIRD_AOT_PATCH_FLAVOR_ID': 'pro',
                    'SHOREBIRD_AOT_PATCH_LICENSE_TYPE': 'pro'})
    return env


def build_phase(work, state, phase):
    if not state.get('app_id'):
        raise ValueError('Start this session API/observer before building')
    if native_hashes(work) != state['native_source_hashes']:
        raise ValueError('Released native fixture sources changed')
    if phase == 'update':
        if not state.get('base_built'):
            raise ValueError('Build the baseline before its update')
        apply_profile(work, 'patch')
    command = cli_command(work, state, phase)
    shared.write_json(work / f'{phase}-command.json', {'command': command, 'cwd': str(work / 'app')})
    shared.run(command, cwd=work / 'app', env=build_environment(work, state, phase),
               log=work / f'{phase}.log')
    if phase == 'base':
        apps = list((work / 'app/build/macos/Build/Products/Release').glob('*.app'))
        if len(apps) != 1:
            raise ValueError('Expected one macOS Release app')
        destination = work / 'released' / apps[0].name
        destination.parent.mkdir()
        shutil.copytree(apps[0], destination, symlinks=True)
        state['released_app'] = str(destination)
        state['base_built'] = True
    else:
        state['update_built'] = True
    save(work, state)


def serve(work, state):
    """One owned API child plus a loopback observer; no foreign PID cleanup."""
    credentials = json.loads((work / 'secrets.json').read_text())
    api_port, report_port = urlparse(state['api_url']).port, urlparse(state['report_url']).port
    if port_open(api_port) or port_open(report_port):
        raise ValueError('A session port is already occupied; no process was stopped')
    report_path = urlparse(state['report_url']).path

    class Reports(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != report_path:
                self.send_error(404)
                return
            self.connection.settimeout(5)
            try:
                content = shared.read_report_body(self.rfile, self.headers)
                verification = shared.validate_launch_report(content, state)
            except (ValueError, TypeError, OSError, UnicodeError):
                self.send_error(400)
                return
            entry = {'time': time.time(), 'peer': self.client_address[0], 'verification': verification}
            descriptor = os.open(work / 'launch-status.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, 'a') as stream:
                stream.write(json.dumps(entry) + '\n')
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    observer = http.server.ThreadingHTTPServer(('127.0.0.1', report_port), Reports)
    observing = False
    owned = OwnedHostProcesses()
    old_handlers = {}
    def stop(*_):
        raise KeyboardInterrupt
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            old_handlers[name] = signal.signal(name, stop)
        env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(work / 'state.sqlite'),
                   STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(work / 'objects'),
                   SERVER_HOST='127.0.0.1', SERVER_PORT=str(api_port), SERVER_BASE_URL=state['api_url'],
                   JWT_SECRET=credentials['jwt_secret'], PATCH_SIGNING_MASTER_KEY=credentials['signing_master_key'],
                   PATCH_DELIVERY_ENCRYPTION='off', SMTP_HOST='',
                   DEFAULT_ADMIN_EMAIL='macos-fixture@example.invalid',
                   DEFAULT_ADMIN_PASSWORD=credentials['bootstrap_password'])
        with (work / 'server.log').open('ab') as log:
            server = owned.spawn([state['server']], env=env, stdout=log, stderr=log)
            for _ in range(100):
                if server.poll() is not None:
                    raise ValueError('Disposable API exited; inspect private server.log')
                try:
                    shared.request(state, 'GET', '/health')
                    break
                except (OSError, RuntimeError):
                    time.sleep(.1)
            else:
                raise ValueError('Disposable API did not become ready')
            login = shared.request(state, 'POST', '/auth/token', {
                'email': 'macos-fixture@example.invalid',
                'password': credentials['admin_password'] if credentials.get('admin_password_active') else credentials['bootstrap_password']})
            token = login['token']
            if login.get('must_change_password'):
                shared.request(state, 'PATCH', '/api/v1/users/me/password', {
                    'current_password': credentials['bootstrap_password'],
                    'new_password': credentials['admin_password']}, token)
                credentials['admin_password_active'] = True
                shared.write_json(work / 'secrets.json', credentials)
            credentials['access_token'] = token
            shared.write_json(work / 'secrets.json', credentials)
            if not state.get('app_id'):
                org = shared.request(state, 'POST', '/api/v1/organizations', {'name': 'Disposable macOS full FFI'}, token)
                app = shared.request(state, 'POST', '/api/v1/apps', {
                    'organization_id': org['id'], 'display_name': 'macOS ARM64 full interpreter fixture'}, token)
                state['app_id'] = app['id']
            yaml = (f"app_id: {state['app_id']}\nbase_url: {state['api_url']}\n"
                    'auto_update: true\naot_patch_runtime_mode: dart-bytecode-interpreter\n'
                    'aot_patch_base_flavor_id: free\naot_patch_base_license_type: free\n'
                    f"aot_patch_key_id: {state['key_id']}\n"
                    'patch_verification: strict\npatch_response_verification: required\n'
                    'aot_patch_full_replacement: true\n')
            shared.private_write(work / 'app/shorebird.yaml', yaml)
            state['serve_started'] = time.time()
            save(work, state)
            threading.Thread(target=observer.serve_forever, daemon=True).start()
            observing = True
            print('Disposable loopback API and full-FFI observer ready.', flush=True)
            server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if observing:
            observer.shutdown()
        observer.server_close()
        owned.close()
        for name, handler in old_handlers.items():
            signal.signal(name, handler)


def app_identity(work, state, app):
    app = contained(work, app)
    if app.suffix != '.app':
        raise ValueError('Expected an owned application bundle')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != state['bundle_id']:
        raise ValueError('Refusing a foreign application bundle')
    executable = info.get('CFBundleExecutable', '')
    if not executable or '/' in executable:
        raise ValueError('Invalid app executable')
    return app, contained(app, app / 'Contents/MacOS' / executable)


def code_inventory(app):
    files = set()
    for directory, subdirs, names in os.walk(app, followlinks=False):
        for name in [*subdirs, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                contained(app, path)
        for name in names:
            path = Path(directory) / name
            if path.is_file():
                with path.open('rb') as stream:
                    magic = stream.read(4)
                if magic in MACH_MAGICS:
                    files.add(contained(app, path))
    if not files:
        raise ValueError('App contains no Mach-O code')
    return sorted(files)


def signing_plan(work, state, app, identity):
    app, runner = app_identity(work, state, app)
    if not identity:
        raise ValueError('An explicit Developer ID identity is required')
    inventory = code_inventory(app)
    frameworks = {path for path in app.rglob('*.framework') if path.is_dir() and not path.is_symlink()}
    targets = set(frameworks)
    for binary in inventory:
        if binary != runner and not any(binary.is_relative_to(framework) for framework in frameworks):
            targets.add(binary)
    targets = sorted(targets, key=lambda path: (-len(path.parts), str(path)))
    commands = []
    for target in [*targets, app]:
        entitlement = (work / 'app/macos/Runner/Release.entitlements' if target == app
                       else work / 'empty-library-entitlements.plist')
        commands.append(['codesign', '--force', '--sign', identity, '--options', 'runtime',
                         '--timestamp=none', '--entitlements', str(entitlement),
                         '--force-library-entitlements', str(target)])
    return commands


def parse_signature(details, entitlement_output, *, application):
    flags = re.search(r'\bflags=0x([a-fA-F0-9]+)\(', details)
    team = re.search(r'^TeamIdentifier=([A-Za-z0-9]+)$', details, re.MULTILINE)
    if (not flags or not int(flags[1], 16) & 0x10000 or not team or team[1] == 'not' or
            not re.search(r'^Authority=Developer ID Application: .+$', details, re.MULTILINE)):
        raise ValueError('Requires hardened runtime and Developer ID Application signing')
    begin, end = entitlement_output.find('<?xml'), entitlement_output.find('</plist>')
    if begin < 0 or end < begin:
        raise ValueError('Missing explicit entitlement plist')
    try:
        entitlements = plistlib.loads(entitlement_output[begin:end + len('</plist>')].encode())
    except (ValueError, plistlib.InvalidFileException, ExpatError) as error:
        raise ValueError('Malformed explicit entitlement plist') from error
    if entitlements != (APP_ENTITLEMENTS if application else {}):
        raise ValueError('Unexpected entitlements; executable/debug/library exceptions are forbidden')
    return {'team_identifier': team[1], 'hardened_runtime': True, 'entitlements': entitlements}


def checked(command, log):
    result = subprocess.run([str(part) for part in command], capture_output=True)
    output = result.stdout + result.stderr
    shared.private_write(log, output)
    if result.returncode:
        raise ValueError('External verification failed; inspect private evidence log')
    return output.decode()


def inspect_app(work, state, app, identity_sha1):
    target_arch = target_architecture(state)
    app, runner = app_identity(work, state, app)
    if not re.fullmatch(r'[a-fA-F0-9]{40}', identity_sha1):
        raise ValueError('Pin the expected Developer ID certificate SHA-1')
    evidence = work / 'signing-evidence' / str(time.time_ns())
    evidence.mkdir(parents=True)
    reports = []
    for index, target in enumerate([app, *code_inventory(app)]):
        prefix = evidence / str(index)
        checked(['codesign', '--verify', '--strict', '--verbose=4', target], str(prefix) + '-verify.log')
        details = checked(['codesign', '--display', '--verbose=4', target], str(prefix) + '-details.log')
        entitlements = checked(['codesign', '--display', '--entitlements', ':-', target], str(prefix) + '-entitlements.log')
        signature = parse_signature(details, entitlements, application=target in (app, runner))
        certificate_prefix = str(prefix) + '-certificate-'
        checked(['codesign', '--display', '--extract-certificates=' + certificate_prefix, target], str(prefix) + '-certificate.log')
        certificate = Path(certificate_prefix + '0')
        fingerprint = hashlib.sha1(certificate.read_bytes()).hexdigest().upper()
        if fingerprint != identity_sha1.upper() or (reports and signature['team_identifier'] != reports[0]['team_identifier']):
            raise ValueError('Nested code does not share the pinned Developer ID certificate/team')
        arch = checked(['lipo', '-archs', target if target != app else runner], str(prefix) + '-arch.log').strip()
        if arch != target_arch['machine']:
            raise ValueError('Full-profile fixture requires ' + target_arch['machine'] + '-only code')
        dependencies = checked(['otool', '-L', target if target != app else runner], str(prefix) + '-dependencies.log')
        signature.update(path=str(target), certificate_sha1=fingerprint,
                         sha256=digest(runner if target == app else target), dependencies=dependencies)
        reports.append(signature)
    checked(['codesign', '--verify', '--deep', '--strict', '--verbose=4', app], evidence / 'bundle-verify.log')
    exports = checked(['xcrun', 'dyld_info', '-exports', runner], evidence / 'runner-exports.log')
    result = {'schema': SCHEMA, 'scope': 'signed bundle inspection only; no runtime acceptance',
              'evidence_directory': str(evidence),
              'signatures': reports, 'exports': shared.verified_ffi_exports(exports)}
    shared.write_json(evidence / 'inspection.json', result)
    return result


def validate_envelope_metadata(envelope, state):
    """Preflight only. The real SDK must authenticate metadata during launch."""
    if not isinstance(envelope, dict):
        raise ValueError('Invalid encrypted artifact metadata')
    metadata, encryption = envelope.get('metadata', {}), envelope.get('encryption', {})
    if not isinstance(metadata, dict) or not isinstance(encryption, dict):
        raise ValueError('Invalid encrypted artifact metadata')
    if (envelope.get('format') != 'open-aot-vmcode-encrypted-v1' or
            envelope.get('payload_kind') != 'full-snapshot' or
            metadata.get('target_os') != 'macos' or metadata.get('target_arch') != target_architecture(state)['envelope'] or
            metadata.get('runtime_mode') != 'dart-bytecode-interpreter' or
            metadata.get('app_id') != state.get('app_id') or
            metadata.get('app_build_id') != state['release_version'] or
            encryption.get('algorithm') != 'AES-256-GCM' or encryption.get('key_id') != state['key_id']):
        raise ValueError('Encrypted artifact is not bound to this macOS target release')
    return {key: metadata[key] for key in ('target_os', 'target_arch', 'runtime_mode', 'app_id', 'app_build_id')}


def container_root(state):
    return Path.home() / 'Library/Containers' / state['bundle_id']


def container_file(state, name):
    root = container_root(state)
    expected = root / 'Data' / EVIDENCE_RELATIVE / name
    # Resolve both against the Containers directory, not a potentially foreign
    # container symlink. Never follow a disposable app path into another tree.
    if root.is_symlink() or expected.resolve() != expected.absolute():
        raise ValueError('Refusing redirected app-container evidence')
    return expected


def validate_status(work, state, path, after, expected_phase):
    if not math.isfinite(after) or after < state['created']:
        raise ValueError('A fresh launch timestamp after preparation is required')
    path = Path(path)
    expected = container_file(state, 'ffi-status.json')
    if path.absolute() != expected.absolute():
        path = contained(work, path)
    if path.is_symlink() or path.stat().st_mtime < after or path.stat().st_mtime > time.time() + 2:
        raise ValueError('Report is stale, redirected or has an invalid timestamp')
    if path.stat().st_size > 1024:
        raise ValueError('Oversized full-FFI report')
    report = shared.validate_launch_report(path.read_text(), state)
    if report['phase'] != expected_phase:
        raise ValueError('Report phase does not match this launch')
    return {'verification': report, 'mtime': path.stat().st_mtime,
            'sha256': digest(path), 'path': str(path), 'acceptance_scope': 'fresh report only'}


def validate_container_record(state, record, after):
    if (record.get('schema') != 'macos-interpreter-container-v1' or
            record.get('bundle_id') != state['bundle_id'] or
            record.get('home') != str(container_root(state) / 'Data') or
            record.get('status_path') != str(container_file(state, 'ffi-status.json')) or
            type(record.get('time')) not in (int, float) or not math.isfinite(record['time']) or
            record['time'] < after or type(record.get('pid')) is not int or record['pid'] <= 0):
        raise ValueError('Container evidence is stale or belongs to another app')
    return record


def require_launch_open(work, state):
    # Re-read the checkpoint so an earlier in-memory state cannot authorize
    # provisioning/launch after cleanup removed its canonical native receipt.
    current = load(work)
    if current['bundle_id'] != state['bundle_id']:
        raise ValueError('Disposable session identity changed')
    if (current.get('cleanup_in_progress') or current.get('cleanup') or
            state.get('cleanup_in_progress') or state.get('cleanup')):
        raise ValueError('Cleanup has started; this disposable app cannot launch again')


def app_launch_command(work, state):
    """Caller launches this with OwnedHostProcesses; this helper starts nothing."""
    require_launch_open(work, state)
    _, runner = app_identity(work, state, Path(state['released_app']))
    return [str(runner)]


def app_environment(work, state, *, action=None, control_only=False, wrong_key=False):
    """Return private child env directly; never serialize or print this mapping."""
    require_launch_open(work, state)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DYLD_', 'OPEN_SHOREBIRD_TEST_'))}
    if action is not None:
        if action not in ('provision', 'overwrite', 'delete'):
            raise ValueError('Unsupported disposable Keychain action')
        env['OPEN_SHOREBIRD_TEST_KEY_ACTION'] = action
        env['OPEN_SHOREBIRD_TEST_KEY_ID'] = state['key_id']
        if action != 'delete':
            credentials = json.loads((work / 'secrets.json').read_text())
            env['OPEN_SHOREBIRD_TEST_KEY_HEX'] = '00' * 32 if wrong_key else credentials['aes_key_hex']
        if control_only:
            env['OPEN_SHOREBIRD_TEST_KEY_CONTROL_ONLY'] = '1'
    return env


def begin_cleanup(work, state):
    """Fence final native deletion; the caller must then run key action delete."""
    require_launch_open(work, state)
    state['cleanup_fence'] = time.time()
    state.pop('cleanup', None)
    save(work, state)
    return state['cleanup_fence']


def validate_cleanup_receipt(state, receipt):
    fence = state.get('cleanup_fence')
    if (type(fence) not in (int, float) or not math.isfinite(fence) or
            fence < state['created']):
        raise ValueError('Begin cleanup, then run a fresh native key deletion')
    if (receipt.get('schema') != 'macos-interpreter-key-control-v1' or
            receipt.get('action') != 'delete' or receipt.get('result') not in ('deleted', 'absent') or
            receipt.get('bundle_id') != state['bundle_id'] or receipt.get('service') != state['key_service'] or
            receipt.get('account') != state['key_id'] or
            type(receipt.get('time')) not in (int, float) or not math.isfinite(receipt['time']) or
            receipt['time'] < fence or receipt['time'] > time.time() + 2):
        raise ValueError('Missing fresh deletion receipt for this exact disposable Keychain item')


def pin_cleanup_receipt(work, state, receipt_path):
    expected = container_file(state, 'key-control.json')
    progress = state.get('cleanup_in_progress')
    if progress:
        if progress.get('fence') != state.get('cleanup_fence'):
            raise ValueError('Cleanup checkpoint does not match its deletion fence')
        saved = contained(work, progress['receipt_evidence'])
        current_bytes = saved.read_bytes()
        if hashlib.sha256(current_bytes).hexdigest() != progress['receipt_sha256']:
            raise ValueError('Pinned cleanup receipt changed')
        # A reappeared native record is never overridden by saved evidence.
        if expected.exists() and expected.read_bytes() != current_bytes:
            raise ValueError('Latest native key control changed after cleanup began')
    else:
        # On the first attempt ONLY the current canonical native record counts.
        # Copied earlier deletes cannot override a later provision/overwrite.
        current_bytes = expected.read_bytes()
        if expected.stat().st_mtime < state.get('cleanup_fence', float('inf')):
            raise ValueError('Canonical native key-control receipt predates cleanup')
    validate_cleanup_receipt(state, json.loads(current_bytes))
    receipt_path = Path(receipt_path)
    if receipt_path.absolute() != expected.absolute():
        receipt_path = contained(work, receipt_path)
        if receipt_path.read_bytes() != current_bytes:
            raise ValueError('Copied deletion receipt is not the latest native key control')
    if not progress:
        directory = contained(work, work / 'cleanup-evidence')
        directory.mkdir(mode=0o700, exist_ok=True)
        saved = contained(work, directory / 'native-key-deletion.json')
        if saved.exists() and saved.read_bytes() != current_bytes:
            raise ValueError('Existing deletion evidence differs from the canonical receipt')
        shared.private_write(saved, current_bytes)
        progress = {'fence': state['cleanup_fence'], 'receipt_evidence': str(saved),
                    'receipt_sha256': hashlib.sha256(current_bytes).hexdigest(),
                    'validated_at': time.time(), 'canonical_receipt': str(expected)}
        shared.write_json(directory / 'validation.json', progress)
        # Persist before deleting any app/container bytes. All launch helpers
        # reject this state, so a retry cannot authorize stale provisioning.
        state['cleanup_in_progress'] = progress
        save(work, state)
    return progress


def remove_owned_container(root):
    if root.is_symlink() or root.parent.resolve() != (Path.home() / 'Library/Containers').resolve():
        raise ValueError('Refusing foreign container cleanup')
    if not root.exists():
        return {'container_removed': True, 'container_payload_removed': True,
                'os_metadata_shell_remaining': None}
    metadata = root / '.com.apple.containermanagerd.metadata.plist'
    # Remove the owned payload first; never follow symlink entries out of it.
    for entry in sorted(root.iterdir()):
        if entry == metadata:
            continue
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
    if metadata.exists() or metadata.is_symlink():
        if metadata.is_symlink() or not metadata.is_file():
            raise ValueError('Unexpected container-manager metadata entry')
        try:
            metadata.unlink()
        except PermissionError as error:
            if (error.errno not in (errno.EPERM, errno.EACCES) or
                    (error.filename and Path(error.filename).absolute() != metadata.absolute()) or
                    list(root.iterdir()) != [metadata]):
                raise
            # macOS can protect this exact OS-managed file even for its owner.
            # Record the remaining shell explicitly; do not claim full removal.
            return {'container_removed': False, 'container_payload_removed': True,
                    'os_metadata_shell_remaining': str(root),
                    'os_metadata_file_remaining': str(metadata),
                    'metadata_removal_errno': error.errno}
    root.rmdir()  # Root/directory permission failures remain fatal.
    return {'container_removed': True, 'container_payload_removed': True,
            'os_metadata_shell_remaining': None}


def cleanup(work, state, receipt_path):
    latest = load(work)
    if latest['bundle_id'] != state['bundle_id']:
        raise ValueError('Disposable session identity changed')
    state.clear()
    state.update(latest)
    for url in (state['api_url'], state['report_url']):
        if port_open(urlparse(url).port):
            raise ValueError('Stop owned API/observer processes first; no saved PID will be signalled')
    # Validate destructive destinations before starting the cleanup checkpoint.
    released = state.get('released_app')
    app = contained(work, released) if released else None
    if app and app.exists():
        app_identity(work, state, app)
    config = contained(work, work / 'config')
    root = container_root(state)
    if root.is_symlink() or root.parent.resolve() != (Path.home() / 'Library/Containers').resolve():
        raise ValueError('Refusing foreign container cleanup')
    progress = pin_cleanup_receipt(work, state, receipt_path)
    # Close our authorized launch surface before removing the native receipt.
    if app and app.exists():
        shutil.rmtree(app)
    container = remove_owned_container(root)
    (work / 'secrets.json').unlink(missing_ok=True)
    if config.exists():
        shutil.rmtree(config)
    state['cleanup'] = {'time': time.time(), 'key_receipt_verified': True,
                        'key_deletion_evidence': progress['receipt_evidence'],
                        'owned_app_removed': True, **container,
                        'owned_app_and_container_removed': container['container_removed'],
                        'cleanup_complete': container['container_removed'],
                        'bootstrap_secrets_removed': True,
                        'servers_stopped_externally': True, 'private_build_evidence_retained': True}
    save(work, state)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('phase', choices=('prepare', 'serve', 'base', 'update', 'inspect', 'status', 'cleanup'))
    result.add_argument('--work-dir', required=True, type=Path)
    result.add_argument('--execute', action='store_true')
    result.add_argument('--cli', type=Path)
    result.add_argument('--flutter-root', type=Path)
    result.add_argument('--engine-src', type=Path)
    result.add_argument('--target-arch', choices=('arm64', 'x64'), default='arm64')
    result.add_argument('--engine-name', help='Target output (default: macos_release_<target-arch>)')
    result.add_argument('--host-engine-name', help='Compiler host output (default: macos_release_<target-arch>)')
    result.add_argument('--server', type=Path)
    result.add_argument('--release-version', default='1.0.0+1')
    result.add_argument('--app', type=Path)
    result.add_argument('--signing-identity')
    result.add_argument('--identity-sha1')
    result.add_argument('--report-file', type=Path)
    result.add_argument('--container-record', type=Path)
    result.add_argument('--after', type=float)
    result.add_argument('--expected-phase', choices=('base', 'update'))
    result.add_argument('--envelope', type=Path)
    result.add_argument('--key-delete-receipt', type=Path)
    result.add_argument('--begin', action='store_true',
                        help='cleanup only: fence a new native delete before final cleanup')
    return result


def main():
    args = parser().parse_args()
    work = args.work_dir.absolute()
    try:
        if args.phase == 'prepare':
            if any(getattr(args, name) is None for name in ('cli', 'flutter_root', 'engine_src', 'server')):
                raise ValueError('prepare requires --cli --flutter-root --engine-src --server')
            if not args.execute:
                print(json.dumps({'phase': 'prepare', 'work_dir': str(work), 'mutations': False,
                                  'requires': ['fresh private directory', 'private source CLI/Flutter SDK',
                                               'matching interpreter-enabled ' + args.target_arch + ' local engine',
                                               'caller CLI honors SHOREBIRD_E2E_CONFIG_DIR'],
                                  'starts': [], 'runtime_acceptance': False}, indent=2))
                return
            state = prepare(args, work)
            print(json.dumps({'prepared': state['bundle_id'], 'work_dir': str(work), 'runtime_acceptance': False}))
            return
        state = load(work)
        if args.phase in ('base', 'update'):
            command = cli_command(work, state, args.phase)
            if args.execute:
                build_phase(work, state, args.phase)
            print(json.dumps({'command': command, 'executed': args.execute,
                              'runtime_acceptance': False}, indent=2))
        elif args.phase == 'serve':
            if args.execute:
                serve(work, state)
            else:
                print(json.dumps({'api': state['api_url'], 'observer': state['report_url'],
                                  'server': state['server'], 'starts_without_execute': False}))
        elif args.phase == 'inspect':
            app = args.app or Path(state['released_app'])
            if args.execute:
                result = inspect_app(work, state, app, args.identity_sha1 or '')
                if args.envelope:
                    envelope = contained(work, args.envelope)
                    result['metadata_preflight'] = validate_envelope_metadata(json.loads(envelope.read_text()), state)
                    shared.write_json(work / 'envelope-metadata-preflight.json', result['metadata_preflight'])
                print(json.dumps(result, indent=2))
            else:
                print(json.dumps({'signing_commands_for_caller': signing_plan(
                    work, state, app, args.signing_identity), 'signing_executed': False}, indent=2))
        elif args.phase == 'status':
            if args.after is None or args.expected_phase is None:
                raise ValueError('status requires --after and --expected-phase')
            path = args.report_file or container_file(state, 'ffi-status.json')
            if args.container_record or not args.report_file:
                record_path = args.container_record or container_file(state, 'container.json')
                if record_path != container_file(state, 'container.json'):
                    record_path = contained(work, record_path)
                validate_container_record(state, json.loads(record_path.read_text()), args.after)
            print(json.dumps(validate_status(work, state, path, args.after, args.expected_phase), indent=2))
        elif args.phase == 'cleanup':
            if args.begin:
                if not args.execute:
                    raise ValueError('cleanup --begin requires --execute to record its fence')
                fence = begin_cleanup(work, state)
                print(json.dumps({'cleanup_armed': True, 'delete_required_after': fence,
                                  'key_action_required': 'delete', 'account': state['key_id']}))
                return
            receipt = args.key_delete_receipt or container_file(state, 'key-control.json')
            if args.execute:
                cleanup(work, state, receipt)
            print(json.dumps({'executed': args.execute, 'key_action_required': 'delete',
                              'account': state['key_id'], 'service': state['key_service'],
                              'receipt': str(receipt), 'signals_saved_pids': False,
                              'cleanup_fence': state.get('cleanup_fence')}))
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        # External logs/HTTP can contain short-lived credentials. Print only
        # authored ValueErrors; everything else remains a private diagnostic.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit('macOS fixture: ' + message) from None


if __name__ == '__main__':
    main()

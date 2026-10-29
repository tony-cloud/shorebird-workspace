#!/usr/bin/env python3
"""Isolated, explicitly phased CLI/server/physical-iOS fixture orchestration.

The default plan is read-only. Mutating phases require --execute. Engine builds
are never started here. No original app, SDK, CLI checkout, or device is changed
by prepare/toolchain/serve/release/patch. Device phases target only a fresh,
session-specific bundle id and refuse debug-signed apps.
"""

import argparse
import base64
import fnmatch
import hashlib
import http.server
import json
import os
from pathlib import Path
import plistlib
import secrets
import signal
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / 'testapps/license_flavor_patch_test'
DEVICE = '00008132-000C396801D9001C'
TEAM = '43FN6K7B4C'
BUNDLETOOL_SHA = 'a099cfa1543f55593bc2ed16a70a7c67fe54b1747bb7301f37fdfd6d91028e29'


def run(command, *, cwd=ROOT, env=None, log=None):
    result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                            capture_output=True)
    if log:
        private_write(log, result.stdout + result.stderr)
    if result.returncode:
        # Logs can contain short-lived signed URLs; never dump them or env vars.
        raise RuntimeError(f'{Path(str(command[0])).name} failed ({result.returncode}); '
                           f'inspect private log {log or "not recorded"}')
    return result.stdout


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content.encode() if isinstance(content, str) else content)


def write_json(path, value):
    private_write(path, json.dumps(value, indent=2))


def clone_tree(source, destination):
    """Independent APFS clone, never a mutable cache symlink/hard link."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise RuntimeError(f'Refusing to overwrite {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['cp', '-cR', str(source), str(destination)],
                            capture_output=True)
    if result.returncode:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=False)


def package_config(source, destination, mappings):
    source = Path(source).resolve()
    config = json.loads(source.read_text())
    for package in config['packages']:
        absolute = Path(urllib.parse.unquote(urllib.parse.urlparse(
            urllib.parse.urljoin(source.as_uri(), package['rootUri'])).path))
        for old, new in mappings:
            try:
                absolute = Path(new) / absolute.relative_to(old)
                break
            except ValueError:
                pass
        package['rootUri'] = absolute.as_uri()
    write_json(destination, config)


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def request(state, method, path, body=None, token=None):
    payload = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(state['api_url'] + path, payload, headers,
                                 method=method)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as response:
            content = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'{method} {path} returned HTTP {error.code}') from None
    return json.loads(content) if content else {}


def load(work):
    return json.loads((work / 'state.json').read_text())


def environment(work, state, *, patch=False):
    credentials = json.loads((work / 'secrets.json').read_text())
    env = dict(os.environ)
    # HOME and the user's Apple signing/account setup are preserved. The private
    # CLI-only config redirect below isolates auth config and logs on macOS.
    env.update({
        'SHOREBIRD_E2E_CONFIG_DIR': str(work / 'config'),
        'SHOREBIRD_HOSTED_URL': state['api_url'],
        'AUTH_SERVICE_URL': state['api_url'] + '/auth',
        'SHOREBIRD_TOKEN': 'sb_api_' + credentials['access_token'],
        'SHOREBIRD_ARTIFACT_BASE_URL': state['api_url'],
        'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': 'https://storage.googleapis.com',
        'FLUTTER_ROOT': str(work / 'flutter'),
        'PUB_CACHE': str(work / 'pub-cache'),
        'FLUTTER_SUPPRESS_ANALYTICS': 'true',
        'DART_SUPPRESS_ANALYTICS': 'true',
        'CI': 'true', 'BOT': 'true',
        'SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS': '0',
        'PATH': str(work / 'flutter/bin') + os.pathsep + os.environ['PATH'],
    })
    for name in list(env):
        if name.startswith('SHOREBIRD_AOT_') or name.startswith('SHOREBIRD_IOS_'):
            del env[name]
    if patch:
        env.update({
            'SHOREBIRD_AOT_PATCH_KEY_HEX': credentials['aes_key_hex'],
            'SHOREBIRD_AOT_PATCH_FLAVOR_ID': 'pro',
            'SHOREBIRD_AOT_PATCH_LICENSE_TYPE': 'pro',
        })
    return env


def apply_structural_fixture(work, phase):
    sources = sorted((work / 'app/tool/structural' / phase).glob('*.dart.template'))
    if not sources:
        raise RuntimeError(f'Missing structural {phase} fixture')
    for source in sources:
        shutil.copy2(source, work / 'app/lib' / source.name.removesuffix('.template'))


def prepare(args, work):
    if work.exists():
        raise RuntimeError('Use a new work directory; existing sessions are never overwritten')
    work.mkdir(parents=True, mode=0o700)
    cli_ref = run(['git', '-C', ROOT / 'shorebird', 'rev-parse', args.cli_ref]).decode().strip()
    flutter_ref = run(['git', '-C', ROOT / 'flutter', 'rev-parse', args.flutter_ref]).decode().strip()
    engine_id = args.engine_id or run([
        'git', '-C', ROOT / 'flutter', 'show',
        f'{flutter_ref}:bin/internal/engine.version']).decode().strip()
    session = secrets.token_hex(5)
    state = {
        'version': 1, 'cli_ref': cli_ref, 'flutter_ref': flutter_ref,
        'engine_id': engine_id, 'device': args.device, 'team': args.team,
        'structural_updates': args.structural_updates,
        'bundle_id': f'org.tonycloud.shorebird-license-e2e.{session}',
        'key_id': f'device-e2e-{session}', 'release_version': '1.0.0+1',
        'api_url': f'http://{args.host_address}:{free_port()}',
        'report_url': f'http://{args.host_address}:{free_port()}/report/{session}',
        'host_engine': str(args.engine_src.resolve() / 'out/host_release_arm64'),
        'engine_src': str(args.engine_src.resolve()),
        'fixture_hashes': {str(path.relative_to(FIXTURE)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in [FIXTURE / 'ios/Runner/AppDelegate.swift',
                                        FIXTURE / 'ios/Runner/SceneDelegate.swift',
                                        FIXTURE / 'ios/Runner/Info.plist',
                                        FIXTURE / 'lib/launch_status_io.dart'] +
                           (sorted((FIXTURE / 'tool/structural').rglob('*.template'))
                            if args.structural_updates else [])},
    }
    write_json(work / 'state.json', state)
    write_json(work / 'secrets.json', {
        'bootstrap_password': secrets.token_urlsafe(32),
        'admin_password': secrets.token_urlsafe(32),
        'jwt_secret': secrets.token_urlsafe(48),
        'signing_master_key': base64.b64encode(secrets.token_bytes(32)).decode(),
        'aes_key_hex': secrets.token_hex(32),
    })
    for name, revision in [('shorebird', cli_ref), ('flutter', flutter_ref)]:
        target = work / ('cli' if name == 'shorebird' else name)
        run(['git', 'clone', '--no-hardlinks', '--no-checkout', ROOT / name, target],
            log=work / f'clone-{name}.log')
        run(['git', '-C', target, 'checkout', '--detach', revision],
            log=work / f'checkout-{name}.log')
    shutil.copytree(FIXTURE, work / 'app', ignore=shutil.ignore_patterns(
        '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', '.git'))
    if state['structural_updates']:
        apply_structural_fixture(work, 'base')
    project = work / 'app/ios/Runner.xcodeproj/project.pbxproj'
    source = project.read_text().replace(
        'com.example.licenseflavorpatchtest.licenseFlavorPatchTest', state['bundle_id'])
    project.write_text(source)
    pubspec = work / 'app/pubspec.yaml'
    pubspec.write_text(pubspec.read_text() + '\nversion: 1.0.0+1\n')
    private_write(work / 'ExportOptions.plist', plistlib.dumps({
        'method': 'release-testing', 'teamID': state['team'],
        'signingStyle': 'automatic', 'manageAppVersionAndBuildNumber': False,
        'stripSwiftSymbols': True,
    }))
    print('Prepared independent source checkouts and app copy; no build/server/device action.')


def toolchain(args, work):
    if not args.host_engine_ready:
        raise RuntimeError('Wait for the coordinated host engine build, then pass --host-engine-ready')
    state = load(work)
    host = Path(state['host_engine'])
    for path in [host / 'dart-sdk/bin/dart', host / 'dartaotruntime',
                 host / 'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot',
                 host / 'dart-sdk/bin/snapshots/dart2bytecode.dart.snapshot',
                 host / 'gen/const_finder.dart.snapshot', host / 'font-subset']:
        if not path.is_file():
            raise RuntimeError(f'Required final engine artifact missing: {path}')
    if not (work / 'pub-cache').exists():
        clone_tree(Path.home() / '.pub-cache', work / 'pub-cache')
    if not (work / 'flutter/bin/cache').exists():
        clone_tree(ROOT / 'flutter/bin/cache', work / 'flutter/bin/cache')
    if not state.get('host_sdk_copied'):
        shutil.rmtree(work / 'flutter/bin/cache/dart-sdk')
        clone_tree(host / 'dart-sdk', work / 'flutter/bin/cache/dart-sdk')
        state['host_sdk_copied'] = True
        write_json(work / 'state.json', state)
    mappings = [(ROOT / 'shorebird', work / 'cli'), (ROOT / 'flutter', work / 'flutter'),
                (Path.home() / '.pub-cache', work / 'pub-cache')]
    package_config(ROOT / 'shorebird/.dart_tool/package_config.json',
                   work / 'cli/.dart_tool/package_config.json', mappings)
    package_config(ROOT / 'flutter/packages/flutter_tools/.dart_tool/package_config.json',
                   work / 'flutter/packages/flutter_tools/.dart_tool/package_config.json', mappings)
    # Direct source launch is Flutter's normal developer entrypoint. This avoids
    # a bootstrap replacing the deliberately copied custom SDK with a download.
    flutter_command = [work / 'flutter/bin/cache/dart-sdk/bin/dart',
                       f'--packages={work}/flutter/packages/flutter_tools/.dart_tool/package_config.json',
                       work / 'flutter/packages/flutter_tools/bin/flutter_tools.dart']
    private_write(work / 'flutter/bin/flutter', '#!/bin/sh\nexec ' +
                  shlex.join([str(x) for x in flutter_command]) + ' "$@"\n')
    (work / 'flutter/bin/flutter').chmod(0o755)
    private_write(work / 'flutter/bin/dart', '#!/bin/sh\nexec ' +
                  shlex.quote(str(work / 'flutter/bin/cache/dart-sdk/bin/dart')) + ' "$@"\n')
    (work / 'flutter/bin/dart').chmod(0o755)
    source = work / 'cli/packages/shorebird_cli/lib/src/shorebird_env.dart'
    marker = '  Directory get configDirectory {\n'
    contents = source.read_text()
    if contents.count(marker) != 1:
        raise RuntimeError('CLI config-directory hook changed; review the isolated redirect')
    if 'SHOREBIRD_E2E_CONFIG_DIR' not in contents:
        source.write_text(contents.replace(marker, marker +
        "    final isolated = platform.environment['SHOREBIRD_E2E_CONFIG_DIR'];\n"
        "    if (isolated != null) return Directory(isolated);\n", 1))
    private_write(work / 'cli/bin/internal/flutter.version', state['flutter_ref'] + '\n')
    private_write(work / 'cli/bin/internal/engine.version', state['engine_id'] + '\n')
    private_write(work / 'flutter/bin/internal/engine.version', state['engine_id'] + '\n')
    vended = work / 'cli/bin/cache/flutter'
    vended.mkdir(parents=True, exist_ok=True)
    # Only this private alias is mutable; it never points at the user's SDK.
    if not (vended / state['flutter_ref']).exists():
        (vended / state['flutter_ref']).symlink_to(work / 'flutter', target_is_directory=True)
    run([args.tooling_dart, 'compile', 'exe',
         f'--packages={work}/cli/.dart_tool/package_config.json',
         work / 'cli/packages/shorebird_cli/bin/shorebird.dart',
         '-o', work / 'cli/bin/shorebird-e2e'], log=work / 'compile-cli.log')
    run(['go', 'build', '-o', work / 'server', './cmd/server'],
        cwd=ROOT / 'shorebird-server', log=work / 'compile-server.log')
    def encoder_works(binary):
        probe = work / 'encoder-probe'
        probe.mkdir(exist_ok=True)
        (probe / 'empty').write_bytes(b'')
        (probe / 'full').write_bytes(bytes(range(256)))
        if not binary.is_file():
            return False
        result = subprocess.run([str(binary), str(probe / 'empty'), str(probe / 'full'),
                                 str(probe / 'transport')], capture_output=True)
        return (result.returncode == 0 and (probe / 'transport').is_file() and
                (probe / 'transport').stat().st_size > 17 and
                (probe / 'transport').read_bytes()[:4] == bytes.fromhex('28b52ffd'))

    binary = ROOT / 'updater/target/release/patch'
    if not encoder_works(binary):
        rust_env = dict(os.environ, RUSTUP_TOOLCHAIN='1.97.0')
        run(['cargo', 'build', '--locked', '-p', 'patch',
             '--manifest-path', ROOT / 'updater/Cargo.toml',
             '--target-dir', work / 'rust-target'], env=rust_env,
            log=work / 'compile-patch.log')
        binary = work / 'rust-target/debug/patch'
        if not encoder_works(binary):
            raise RuntimeError('Public patch tool failed the actual empty-base transport probe')
    state['patch_source_ref'] = run(['git', '-C', ROOT / 'updater', 'rev-parse', 'HEAD']).decode().strip()
    state['patch_binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
    write_json(work / 'state.json', state)
    cache = work / 'cli/bin/cache/artifacts'
    (cache / 'patch').mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, cache / 'patch/patch')
    (cache / 'patch/patch.stamp').touch()
    jar = cache / 'bundletool/bundletool.jar'
    jar.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen('https://github.com/google/bundletool/releases/download/1.18.3/bundletool-all-1.18.3.jar') as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != BUNDLETOOL_SHA:
        raise RuntimeError('Public bundletool checksum mismatch')
    jar.write_bytes(data)
    (jar.parent / 'bundletool.jar.stamp').touch()
    print('Private CLI/Flutter/cache/SDK, real Go server, and public patch tool ready.')


def serve(work):
    state = load(work)
    credentials = json.loads((work / 'secrets.json').read_text())
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    report = urllib.parse.urlparse(state['report_url'])

    class Reports(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != report.path:
                self.send_error(404)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 1024:
                self.send_error(400)
                return
            content = self.rfile.read(length).decode('utf-8')
            if content not in ('license:free\npro-feature:off\n',
                               'license:pro\npro-feature:enabled\n'):
                self.send_error(400)
                return
            with (work / 'launch-status.jsonl').open('a') as output:
                output.write(json.dumps({'time': time.time(), 'status': content}) + '\n')
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    observer = http.server.ThreadingHTTPServer(('0.0.0.0', report.port), Reports)
    observing = False
    env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(work / 'state.sqlite'),
               STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(work / 'objects'),
               SERVER_PORT=str(urllib.parse.urlparse(state['api_url']).port),
               SERVER_BASE_URL=state['api_url'], JWT_SECRET=credentials['jwt_secret'],
               PATCH_SIGNING_MASTER_KEY=credentials['signing_master_key'],
               PATCH_DELIVERY_ENCRYPTION='off', SMTP_HOST='',
               DEFAULT_ADMIN_EMAIL='device-e2e@example.invalid',
               DEFAULT_ADMIN_PASSWORD=credentials['bootstrap_password'])
    with (work / 'server.log').open('ab') as log:
        server = subprocess.Popen([str(work / 'server')], env=env, stdout=log, stderr=log)
        state['serve_pid'] = os.getpid()
        state['server_pid'] = server.pid
        write_json(work / 'state.json', state)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('Disposable server exited; inspect private server.log')
                try:
                    request(state, 'GET', '/health')
                    break
                except (OSError, RuntimeError):
                    time.sleep(0.1)
            else:
                raise RuntimeError('Disposable server did not become ready')
            login = request(state, 'POST', '/auth/token', {
                'email': 'device-e2e@example.invalid',
                'password': credentials['admin_password'] if state.get('app_id') else credentials['bootstrap_password'],
            })
            token = login['token']
            if login.get('must_change_password'):
                request(state, 'PATCH', '/api/v1/users/me/password', {
                    'current_password': credentials['bootstrap_password'],
                    'new_password': credentials['admin_password'],
                }, token)
            credentials['access_token'] = token
            write_json(work / 'secrets.json', credentials)
            if not state.get('app_id'):
                org = request(state, 'POST', '/api/v1/organizations', {'name': 'Disposable device E2E'}, token)
                app = request(state, 'POST', '/api/v1/apps', {
                    'organization_id': org['id'], 'display_name': 'Physical encrypted patch E2E',
                }, token)
                state['app_id'] = app['id']
                write_json(work / 'state.json', state)
            yaml = (f"app_id: {state['app_id']}\nbase_url: {state['api_url']}\n"
                    "auto_update: true\naot_patch_runtime_mode: dart-bytecode-interpreter\n"
                    "aot_patch_base_flavor_id: free\naot_patch_base_license_type: free\n"
                    f"aot_patch_key_id: {state['key_id']}\n"
                    "patch_verification: strict\npatch_response_verification: required\n")
            if state.get('structural_updates', False):
                yaml += 'aot_patch_structural_updates: true\n'
            private_write(work / 'app/shorebird.yaml', yaml)
            for license_type in ('free', 'pro'):
                write_json(work / f'{license_type}-defines.json', {
                    'LICENSE_TYPE': license_type, 'PATCH_TEST_REPORT_URL': state['report_url'],
                })
            threading.Thread(target=observer.serve_forever, daemon=True).start()
            observing = True
            print('Disposable API and status observer ready. Keep this terminal running.', flush=True)
            server.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if observing:
                observer.shutdown()
            observer.server_close()
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def build_phase(args, work):
    state = load(work)
    patch = args.phase == 'patch'
    if patch and 'release_codesign' not in state:
        raise RuntimeError('Complete the free release before patching')
    if patch and state.get('structural_updates', False):
        apply_structural_fixture(work, 'patch')
    signed = state['release_codesign'] if patch else args.signed
    command = [work / 'cli/bin/shorebird-e2e', '--json',
               f"--local-engine-src-path={state['engine_src']}",
               '--local-engine=ios_release', '--local-engine-host=host_release_arm64',
               'patch' if patch else 'release', 'ios',
               f'--dart-define-from-file={work}/{"pro" if patch else "free"}-defines.json']
    if not signed:
        command.append('--no-codesign')
    elif not patch:
        command.append(f'--export-options-plist={work}/ExportOptions.plist')
    if patch:
        command.extend([f"--release-version={state['release_version']}", '--track=stable'])
    run(command, cwd=work / 'app', env=environment(work, state, patch=patch),
        log=work / f'{args.phase}.log')
    if not patch:
        state['release_codesign'] = signed
        write_json(work / 'state.json', state)
    print(f'Actual CLI {args.phase} completed; private log retained.')


def verified_app(args, work, state):
    app = args.app.resolve()
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != state['bundle_id']:
        raise RuntimeError('Refusing an app outside this dedicated fixture session')
    profile = plistlib.loads(run(['security', 'cms', '-D', '-i', app / 'embedded.mobileprovision']))
    policy = profile['Entitlements']
    identity = policy.get('application-identifier', '')
    if (state['team'] not in profile.get('TeamIdentifier', []) or
            not fnmatch.fnmatch(state['team'] + '.' + state['bundle_id'], identity) or
            state['device'] not in profile.get('ProvisionedDevices', []) or
            policy.get('get-task-allow') is not False or
            profile['ExpirationDate'].timestamp() <= time.time()):
        raise RuntimeError('Requires a current dedicated ad-hoc profile permitting this iPad and get-task-allow=false')
    result = subprocess.run(['codesign', '-d', '--entitlements', ':-', str(app)], capture_output=True)
    entitlements = plistlib.loads(result.stdout)
    if result.returncode or entitlements.get('get-task-allow') is True:
        raise RuntimeError('Debug entitlement found; refusing physical validation')
    run(['codesign', '--verify', '--strict', app])
    strict_env = dict(os.environ, APP_STORE_STRICT='1', IOS_APP_BUNDLE=str(app),
                      IOS_ENGINE_DIR=str(Path(state['engine_src']) / 'out/ios_release'),
                      HOST_ENGINE_DIR=state['host_engine'])
    run(['bash', ROOT / 'scripts/verify_ios_interpreter_route.sh'], env=strict_env,
        log=work / 'strict-ios-route.log')
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['plan', 'prepare', 'toolchain', 'serve', 'release', 'patch', 'inspect-app', 'install', 'launch'])
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--host-address', default='192.168.93.205')
    parser.add_argument('--cli-ref', default='HEAD')
    parser.add_argument('--flutter-ref', default='HEAD')
    parser.add_argument('--engine-id', help='Defaults to the selected Flutter revision engine identity')
    parser.add_argument('--engine-src', type=Path, default=ROOT / 'flutter/engine/src')
    parser.add_argument('--tooling-dart', type=Path, default=ROOT / 'dart-sdk/tools/sdks/dart-sdk/bin/dart')
    parser.add_argument('--device', default=DEVICE)
    parser.add_argument('--team', default=TEAM)
    parser.add_argument('--host-engine-ready', action='store_true')
    parser.add_argument('--signed', action='store_true')
    parser.add_argument('--structural-updates', action='store_true',
                        help='Prepare a structural release and patch adding a Flutter widget and fields')
    parser.add_argument('--app', type=Path)
    parser.add_argument('--provision', action='store_true')
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if args.phase == 'plan':
        print('No action performed. Sequence (each mutation requires --execute):')
        for phase in ['prepare', 'toolchain', 'serve', 'release', 'patch']:
            extra = ['--host-engine-ready'] if phase == 'toolchain' else []
            if phase == 'prepare' and args.structural_updates:
                extra.append('--structural-updates')
            print(shlex.join(['python3', str(Path(__file__)), phase, '--work-dir', str(work), '--execute', *extra]))
        print('serve stays in its own terminal. release defaults to --no-codesign; add --signed only after provisioning is ready.')
        print('For device validation: inspect-app/install --app <exported distribution .app>, then launch --provision once; later launch without --provision.')
        return
    if args.phase == 'inspect-app':
        if not args.app:
            parser.error('--app is required')
        verified_app(args, work, load(work))
        print('PASS: dedicated bundle, distribution profile, device grant, and no debug entitlement')
        return
    if not args.execute:
        parser.error('Mutating phases require --execute; plan and inspect-app are read-only')
    if args.phase == 'prepare':
        prepare(args, work)
    elif args.phase == 'toolchain':
        toolchain(args, work)
    elif args.phase == 'serve':
        serve(work)
    elif args.phase in ('release', 'patch'):
        build_phase(args, work)
    else:
        state = load(work)
        if args.phase == 'install':
            if not args.app:
                parser.error('--app is required')
            app = verified_app(args, work, state)
            run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', state['device'], app], log=work / 'install.log')
        else:
            env = dict(os.environ)
            if args.provision:
                values = json.loads((work / 'secrets.json').read_text())
                env['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_HEX'] = values['aes_key_hex']
                env['DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_ID'] = state['key_id']
            run(['xcrun', 'devicectl', 'device', 'process', 'launch', '--quiet',
                 '--device', state['device'], '--terminate-existing', state['bundle_id']],
                env=env, log=work / 'launch.log')
        print(f'{args.phase} completed for the dedicated fixture; no debugger was attached.')


if __name__ == '__main__':
    main()

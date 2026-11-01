#!/usr/bin/env python3
"""Isolated protocol-2 macOS lifecycle fixture.

This driver prepares/builds real CLI artifacts and talks to a real disposable
self-hosted API. It never signs or launches an app. The caller owns sequential
native signing/launches through OwnedHostProcesses. Python tests and a plan are
NOT runtime acceptance. Keys, app processes, and both loopback ports are scoped
to this private session. No global Shorebird config, SDK or cache is modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import time
from urllib.parse import urlparse
import uuid

import macos_interpreter_e2e as v1
from owned_host_processes import OwnedHostProcesses

SCHEMA = 'macos-variant-e2e-v2'
TEMPLATES = Path(__file__).with_name('macos_variant')
VARIANTS = ('free', 'pro', 'enterprise')
BUILD_ORDER = ('base', 'pro_seed', 'pro', 'enterprise')
LIFECYCLE = ('free', 'pro', 'enterprise', 'pro', 'free', 'enterprise', 'free')
EXPECTED_NUMBERS = {'free': None, 'pro': '2', 'enterprise': '3'}


def load(work):
    state = v1.load(work)
    if state.get('variant_schema') != SCHEMA or set(state.get('key_ids', {})) != {'pro', 'enterprise'}:
        raise ValueError('Not an owned protocol-2 variant fixture')
    if len(set(state['key_ids'].values())) != 2:
        raise ValueError('Variants require independent Keychain IDs')
    return state


def require_open(work, state, *, deletion=False):
    current = load(work)
    if current['session'] != state['session']:
        raise ValueError('Session identity changed')
    if current.get('cleanup') or current.get('cleanup_in_progress') or (current.get('cleanup_fence') and not deletion):
        raise ValueError('Cleanup has started; no further application launches')


def prepare(args, work):
    dependency = args.updater_package.resolve()
    if dependency.is_relative_to(v1.ROOT.resolve()) or not (dependency / 'lib/src/selection_status.dart').is_file():
        raise ValueError('Pin the dependency to a private v2 updater clone outside the workspace')
    # Use the proven macOS scaffolding and ownership schema; v2 has a separate
    # marker so no legacy helper may silently treat its reports as acceptance.
    state = v1.prepare(args, work)
    state.update(variant_schema=SCHEMA, updater_package=str(dependency),
                 sentinel=secrets.token_hex(24),
                 key_ids={name: f'{state["session"]}-{name}-artifact' for name in ('pro', 'enterprise')},
                 built_phases=[], assignments=[])
    credentials = json.loads((work / 'secrets.json').read_text())
    credentials.pop('aes_key_hex')
    credentials['variant_keys'] = {name: secrets.token_hex(32) for name in ('pro', 'enterprise')}
    v1.shared.write_json(work / 'secrets.json', credentials)
    pubspec = work / 'app/pubspec.yaml'
    source = pubspec.read_text()
    marker = 'dependencies:\n'
    if len(re.findall(r'^dependencies:$', source, re.MULTILINE)) != 1 or 'shorebird_code_push:' in source:
        raise ValueError('Review changed fixture dependencies')
    # JSON string syntax is a YAML double-quoted scalar; no shell expansion.
    pubspec.write_text(source.replace(marker, marker + '  shorebird_code_push:\n    path: ' + json.dumps(str(dependency)) + '\n', 1))
    state['native_source_hashes'] = None  # Not sealed until real server app ID exists.
    apply_profile(work, state, 'base')
    v1.save(work, state)
    return state


def apply_profile(work, state, phase):
    if phase not in BUILD_ORDER:
        raise ValueError('Unsupported build profile')
    variant = 'free' if phase == 'base' else ('pro' if phase == 'pro_seed' else phase)
    v1.apply_profile(work, 'base' if phase == 'base' else 'patch')
    template = (TEMPLATES / 'variant_status.dart.template').read_text()
    code = {
        'free': "String variantSpecificCode() => 'free:base';",
        'pro_seed': "class ProSeedFeature { String value() => 'pro:seed'; }\nString variantSpecificCode() => ProSeedFeature().value();",
        'pro': "class ProVariantFeature { int calculate(int input) => input * 7; }\nString variantSpecificCode() => 'pro:${ProVariantFeature().calculate(6)}';",
        'enterprise': "class EnterpriseVariantFeature { final List<int> seats; EnterpriseVariantFeature(this.seats); int total() => seats.fold(0, (a, b) => a + b); }\nString enterpriseOnlyFunction() => 'enterprise:${EnterpriseVariantFeature([4, 8, 12]).total()}';\nString variantSpecificCode() => enterpriseOnlyFunction();",
    }[phase if phase != 'base' else 'free']
    for old, new in {'__SESSION__': state['session'], '__SENTINEL__': state['sentinel'],
                     '__VARIANT__': variant, '__DATA_SCHEMA__': str({'free': 1, 'pro': 2, 'enterprise': 3}[variant]),
                     '__VARIANT_CODE__': code}.items():
        template = template.replace(old, new)
    v1.shared.private_write(work / 'app/lib/variant_status.dart', template)
    main = work / 'app/lib/main.dart'
    source = "import 'variant_status.dart';\n" + main.read_text()
    # Preserve the existing exact FFI widget assertion, and independently render
    # and inspect the actual selected variant (the shared paid FFI fixture uses
    # a historical pro label even when enterprise code is being exercised).
    for marker in ('final statusKey = GlobalKey();', 'home: Scaffold(\n',
                   '  await firstFrame.future;'):
        if source.count(marker) != 1:
            raise ValueError('Review changed variant render assertion path')
    source = source.replace('final statusKey = GlobalKey();',
                            'final statusKey = GlobalKey();\nfinal variantStatusKey = GlobalKey();')
    source = source.replace('home: Scaffold(\n', '''home: Scaffold(
        appBar: AppBar(title: Text(
          'variant:$fixtureVariant ${variantSpecificCode()}', key: variantStatusKey)),
''')
    source = source.replace('  await firstFrame.future;', '''  await firstFrame.future;
  final variantRendered = variantStatusKey.currentContext?.findRenderObject();
  if (variantRendered is! RenderParagraph || !variantRendered.attached ||
      !variantRendered.hasSize || variantRendered.size.isEmpty ||
      variantRendered.text.toPlainText() != 'variant:$fixtureVariant ${variantSpecificCode()}') {
    throw StateError('Selected variant rendered assertion failed');
  }''')
    marker = '    evidence: evidence,\n  );'
    if source.count(marker) != 1:
        raise ValueError('Review changed FFI completion path')
    # Existing 3/16 checks and exact frame assertion must complete first.
    main.write_text(source.replace(marker, marker + '\n  await writeVariantStatus();'))
    v1.shared.write_json(work / f'{phase}-defines.json', {'LICENSE_TYPE': variant})


def configure_native(work, state):
    if state.get('base_built'):
        if v1.native_hashes(work) != state['native_source_hashes']:
            raise ValueError('Released native sources changed')
        return
    template = (v1.HELPERS / 'PatchTestKeysV2.swift.template').read_text()
    values = {'__SERVICE__': state['key_service'], '__BUNDLE_ID__': state['bundle_id'],
              '__SESSION__': state['session'], '__APP_ID__': state['app_id'],
              '__WIRE_ARCH__': v1.target_architecture(state)['wire'],
              '__RELEASE__': state['release_version'], '__PRO_KEY_ID__': state['key_ids']['pro'],
              '__ENTERPRISE_KEY_ID__': state['key_ids']['enterprise']}
    for old, new in values.items():
        template = template.replace(old, new)
    v1.shared.private_write(work / 'app/macos/Runner/PatchTestKeys.swift', template)
    state['native_source_hashes'] = v1.native_hashes(work)


def yaml_source(state):
    return (f"app_id: {state['app_id']}\nbase_url: {state['api_url']}\n"
            'auto_update: false\nselection_protocol: 2\n'
            'aot_patch_runtime_mode: dart-bytecode-interpreter\n'
            'aot_patch_base_flavor_id: free\naot_patch_base_license_type: free\n'
            f"aot_patch_app_build_id: {state['release_version']}\n"
            f"aot_patch_key_id: {state['key_ids']['pro']}\n"
            'patch_verification: strict\npatch_response_verification: required\n'
            'aot_patch_full_replacement: true\n')


def serve(work, state):
    """One owned real API process. No fixture response emulation or fake grants."""
    require_open(work, state)
    ports = [urlparse(state[name]).port for name in ('api_url', 'report_url')]
    if any(v1.port_open(port) for port in ports):
        raise ValueError('Session port occupied; no process was stopped')
    credentials = json.loads((work / 'secrets.json').read_text())
    env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(work / 'state.sqlite'),
               STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(work / 'objects'),
               SERVER_HOST='127.0.0.1', SERVER_PORT=str(ports[0]), SERVER_BASE_URL=state['api_url'],
               JWT_SECRET=credentials['jwt_secret'], PATCH_SIGNING_MASTER_KEY=credentials['signing_master_key'],
               PATCH_DELIVERY_ENCRYPTION='off', SMTP_HOST='',
               DEFAULT_ADMIN_EMAIL='macos-fixture@example.invalid', DEFAULT_ADMIN_PASSWORD=credentials['bootstrap_password'])
    owned = OwnedHostProcesses()
    previous = {}
    def stop(*_):
        raise KeyboardInterrupt
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, stop)
        with (work / 'server.log').open('ab') as log:
            child = owned.spawn([state['server']], env=env, stdout=log, stderr=log)
            for _ in range(100):
                if child.poll() is not None:
                    raise ValueError('Real fixture API exited; inspect private server.log')
                try:
                    v1.shared.request(state, 'GET', '/health')
                    break
                except (OSError, RuntimeError):
                    time.sleep(.1)
            else:
                raise ValueError('Real fixture API did not become ready')
            login = v1.shared.request(state, 'POST', '/auth/token', {
                'email': 'macos-fixture@example.invalid', 'password': credentials[
                    'admin_password' if credentials.get('admin_password_active') else 'bootstrap_password']})
            token = login['token']
            if login.get('must_change_password'):
                v1.shared.request(state, 'PATCH', '/api/v1/users/me/password', {
                    'current_password': credentials['bootstrap_password'], 'new_password': credentials['admin_password']}, token)
                credentials['admin_password_active'] = True
            credentials['access_token'] = token
            v1.shared.write_json(work / 'secrets.json', credentials)
            if not state.get('app_id'):
                org = v1.shared.request(state, 'POST', '/api/v1/organizations', {'name': 'Disposable macOS variants'}, token)
                app = v1.shared.request(state, 'POST', '/api/v1/apps', {
                    'organization_id': org['id'], 'display_name': 'macOS protocol 2 variants'}, token)
                state['app_id'] = app['id']
            configure_native(work, state)
            v1.shared.private_write(work / 'app/shorebird.yaml', yaml_source(state))
            state['serve_started'] = time.time()
            v1.save(work, state)
            print('Real protocol-2 API ready; app ID/native provider sealed.', flush=True)
            child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        owned.close()
        for name, handler in previous.items():
            signal.signal(name, handler)


def cli_command(work, state, phase):
    if phase not in BUILD_ORDER:
        raise ValueError('Unsupported build phase')
    result = v1.cli_command(work, state, 'base' if phase == 'base' else 'update')
    result = [f'--dart-define-from-file={work}/{phase}-defines.json' if arg.startswith('--dart-define-from-file=') else arg for arg in result]
    if phase != 'base':
        result.insert(result.index('--'), '--variant=' + ('pro' if phase == 'pro_seed' else phase))
    return result


def build_environment(work, state, phase):
    env = v1.build_environment(work, state, 'base')
    if phase != 'base':
        variant = 'pro' if phase == 'pro_seed' else phase
        credentials = json.loads((work / 'secrets.json').read_text())
        env.update(SHOREBIRD_AOT_PATCH_KEY_HEX=credentials['variant_keys'][variant],
                   SHOREBIRD_AOT_PATCH_KEY_ID=state['key_ids'][variant],
                   SHOREBIRD_AOT_PATCH_FLAVOR_ID=variant, SHOREBIRD_AOT_PATCH_LICENSE_TYPE=variant)
    return env


def build_phase(work, state, phase):
    require_open(work, state)
    completed = state['built_phases']
    if len(completed) >= len(BUILD_ORDER) or phase != BUILD_ORDER[len(completed)]:
        raise ValueError('Build exactly base, pro_seed, pro, enterprise in order')
    if not state.get('app_id') or v1.native_hashes(work) != state['native_source_hashes']:
        raise ValueError('Bootstrap server and seal unchanged native sources before building')
    apply_profile(work, state, phase)
    command = cli_command(work, state, phase)
    v1.shared.write_json(work / f'{phase}-command.json', {'command': command, 'cwd': str(work / 'app')})
    v1.shared.run(command, cwd=work / 'app', env=build_environment(work, state, phase), log=work / f'{phase}.log')
    if phase == 'base':
        apps = list((work / 'app/build/macos/Build/Products/Release').glob('*.app'))
        if len(apps) != 1:
            raise ValueError('Expected exactly one released application')
        destination = work / 'released' / apps[0].name
        destination.parent.mkdir()
        shutil.copytree(apps[0], destination, symlinks=True)
        state.update(released_app=str(destination), base_built=True)
        validate_compiled_scope(state, destination)
    completed.append(phase)
    v1.save(work, state)


def validate_compiled_scope(state, app):
    wire_arch = v1.target_architecture(state)['wire']
    scopes = list(Path(app).rglob('shorebird_release_scope_macos_*.json'))
    baselines = list(Path(app).rglob('shorebird_replacement_base.sbr'))
    if len(scopes) != 1 or scopes[0].name != 'shorebird_release_scope_macos_' + wire_arch + '.json' or len(baselines) != 1:
        raise ValueError('Missing unique compiled v2 scope/original full baseline')
    scope = json.loads(scopes[0].read_text())
    if set(scope) != {'app_id', 'release_version', 'platform', 'arch', 'base_artifact_hash', 'host_abi_hash', 'runtime_profile'}:
        raise ValueError('Incomplete compiled immutable release scope')
    for key, expected in {'app_id': state['app_id'], 'release_version': state['release_version'],
                          'platform': 'macos', 'arch': wire_arch, 'runtime_profile': 'interpreter-full',
                          'base_artifact_hash': v1.digest(baselines[0])}.items():
        if scope.get(key) != expected:
            raise ValueError('Compiled scope does not match original macOS release: ' + key)
    if not re.fullmatch('[a-f0-9]{64}', scope.get('host_abi_hash') or ''):
        raise ValueError('Full replacement must bind its original host ABI')
    configs = list(Path(app).rglob('shorebird.yaml'))
    if len(configs) != 1:
        raise ValueError('Missing unique compiled YAML')
    source = configs[0].read_text()
    for pattern in (r'^selection_protocol: 2$', r'^patch_verification: strict$',
                    r'^patch_response_verification: required$', r'^patch_public_key: .+',
                    '^aot_patch_app_build_id: ' + re.escape(state['release_version']) + '$',
                    '^aot_patch_base_snapshot_hash: ' + scope['base_artifact_hash'] + '$',
                    '^aot_patch_host_abi: ' + scope['host_abi_hash'] + '$'):
        if not re.search(pattern, source, re.MULTILINE):
            raise ValueError('Compiled protocol/signature requirement absent')
    if re.search(r'^aot_patch_key_hex:', source, re.MULTILINE):
        raise ValueError('Compiled YAML must never bundle raw encryption keys')
    state['release_scope'] = scope
    return scope


def validate_envelope_metadata(state, envelope, variant):
    """Preflight only; actual signature/decryption remains an engine runtime gate."""
    if variant not in state['key_ids']:
        raise ValueError('Unsupported paid artifact variant')
    scoped = {**state, 'key_id': state['key_ids'][variant]}
    v1.validate_envelope_metadata(envelope, scoped)
    metadata = envelope['metadata']
    expected = {'flavor_id': variant, 'license_type': variant,
                'base_snapshot_hash': state['release_scope']['base_artifact_hash']}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError('Paid artifact differs from its variant/license/original baseline')
    # Host ABI is authenticated inside the encrypted full-replacement wrapper;
    # only the real engine can validate it after decrypting.
    return metadata


def api(work, state, method, suffix, body=None):
    credentials = json.loads((work / 'secrets.json').read_text())
    return v1.shared.request(state, method, f"/api/v1/apps/{state['app_id']}" + suffix, body, credentials['access_token'])


def release_id(work, state):
    matches = [item for item in api(work, state, 'GET', '/releases')['releases'] if item['version'] == state['release_version']]
    if len(matches) != 1 or matches[0].get('selection_protocol') != 2:
        raise ValueError('Expected exactly one real protocol-2 release')
    return matches[0]['id']


def map_free(work, state):
    require_open(work, state)
    return api(work, state, 'PUT', f'/releases/{release_id(work, state)}/variants/free', {
        'platform': 'macos', 'arch': v1.target_architecture(state)['wire'], 'channel': 'stable', 'license_type': 'free',
        'target': {'kind': 'base', 'patch_number': None, 'artifact_hash': None,
                   'encryption': 'none', 'artifact_key_id': None}})


def remember_installation(work, state, report):
    installation = report['installation_id']
    if not isinstance(installation, str) or not installation or len(installation.encode()) > 1024:
        raise ValueError('Invalid real updater installation ID')
    if state.get('installation_id', installation) != installation:
        raise ValueError('Installed updater identity changed')
    state['installation_id'] = installation
    v1.save(work, state)


def selection_identity(value):
    """Validate routing/status fields; this does not verify a grant signature."""
    epoch, digest, variant = value.get('epoch'), value.get('digest'), value.get('variant_id')
    if (not isinstance(epoch, str) or not re.fullmatch(r'0|[1-9][0-9]*', epoch) or
            len(epoch) > 19 or int(epoch) > 9223372036854775807 or variant not in VARIANTS or
            value.get('license_type') != variant or value.get('patch_number') != EXPECTED_NUMBERS[variant] or
            (epoch == '0' and (digest is not None or variant != 'free')) or
            (epoch != '0' and (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)))):
        raise ValueError('Invalid updater selection identity')
    return {key: value[key] for key in ('epoch', 'digest', 'variant_id', 'license_type', 'patch_number')}


def known_selections(state):
    identities = [selection_identity({
        'epoch': item['selection']['epoch'], 'digest': item['selection_digest'],
        'variant_id': item['selection']['variant_id'],
        'license_type': item['selection']['license_type'],
        'patch_number': item['selection']['target']['patch_number'],
    }) for item in state['assignments']]
    if state.get('observed_selection'):
        identities.append(selection_identity(state['observed_selection']))
    return identities


def latest_selection(state):
    identities = known_selections(state)
    by_epoch = {}
    for identity in identities:
        if identity['epoch'] in by_epoch and by_epoch[identity['epoch']] != identity:
            raise ValueError('Conflicting known authorization at the same epoch')
        by_epoch[identity['epoch']] = identity
    return max(identities, key=lambda value: int(value['epoch'])) if identities else {
        'epoch': '0', 'digest': None, 'variant_id': 'free', 'license_type': 'free', 'patch_number': None}


def assign(work, state, variant, *, offline_valid_until=None, not_before=None, idempotency_key=None):
    require_open(work, state)
    if variant not in VARIANTS or not state.get('installation_id'):
        raise ValueError('Capture the real installed updater ID before assigning an allowed variant')
    expected = latest_selection(state)['epoch']
    body = {'client_id': state['installation_id'], 'platform': 'macos', 'arch': v1.target_architecture(state)['wire'],
            'channel': 'stable', 'variant_id': variant, 'entitlement_revision': f"{state['session']}:{uuid.uuid4()}",
            'not_before': str(int(time.time())) if not_before is None else not_before,
            'offline_valid_until': offline_valid_until, 'expected_epoch': expected,
            'idempotency_key': idempotency_key or str(uuid.uuid4())}
    pending = state.get('pending_assignment')
    if pending:
        if (pending['variant_id'] != variant or pending['offline_valid_until'] != offline_valid_until or
                (not_before is not None and pending['not_before'] != not_before) or
                (idempotency_key is not None and pending['idempotency_key'] != idempotency_key)):
            raise ValueError('Resolve the persisted idempotent assignment before changing its request')
        body = pending
    else:
        state['pending_assignment'] = body
        v1.save(work, state)
    # A retry may occur after a device check advanced the observed authorization.
    # Both the idempotent request and its response belong to the original CAS.
    expected = body['expected_epoch']
    result = api(work, state, 'POST', f'/releases/{release_id(work, state)}/selections', body)
    descriptor = result['selection']
    expected_scope = dict(state.get('release_scope', {}), app_id=state['app_id'],
        release_version=state['release_version'], platform='macos', arch=v1.target_architecture(state)['wire'],
        client_id_hash=hashlib.sha256(state['installation_id'].encode()).hexdigest())
    identity = selection_identity({'epoch': descriptor['epoch'], 'digest': result['selection_digest'],
                                   'variant_id': descriptor['variant_id'],
                                   'license_type': descriptor['license_type'],
                                   'patch_number': descriptor['target']['patch_number']})
    if (descriptor['epoch'] != str(int(expected) + 1) or descriptor['variant_id'] != variant or
            any(descriptor['scope'].get(key) != value for key,value in expected_scope.items()) or
            descriptor['target']['patch_number'] != EXPECTED_NUMBERS[variant]):
        raise ValueError('Real assignment did not bind this installation/epoch/content')
    if any(known['epoch'] == identity['epoch'] and known != identity for known in known_selections(state)):
        raise ValueError('Real assignment conflicts with a previously observed authorization')
    state['assignments'].append(result)
    state.pop('pending_assignment', None)
    v1.save(work, state)
    return result


def app_environment(work, state, *, variant=None, action=None, control_only=False, wrong_key=False, check=False):
    require_open(work, state, deletion=action == 'delete')
    env = {key: value for key, value in os.environ.items() if not key.startswith(('DYLD_', 'OPEN_SHOREBIRD_TEST_'))}
    if check:
        env['OPEN_SHOREBIRD_TEST_CHECK'] = '1'
    if action is not None:
        if action not in ('provision', 'overwrite', 'delete') or variant not in state['key_ids']:
            raise ValueError('Unsupported native variant key control')
        if state.get('cleanup_fence') and (action != 'delete' or not control_only or check):
            raise ValueError('Cleanup permits only final native control-only deletion')
        env.update(OPEN_SHOREBIRD_TEST_KEY_ACTION=action, OPEN_SHOREBIRD_TEST_KEY_ID=state['key_ids'][variant])
        if action != 'delete':
            credentials = json.loads((work / 'secrets.json').read_text())
            env['OPEN_SHOREBIRD_TEST_KEY_HEX'] = '00' * 32 if wrong_key else credentials['variant_keys'][variant]
        if control_only:
            env['OPEN_SHOREBIRD_TEST_KEY_CONTROL_ONLY'] = '1'
    return env


def app_launch_command(work, state, *, deletion=False):
    require_open(work, state, deletion=deletion)
    _, executable = v1.app_identity(work, state, Path(state['released_app']))
    return [str(executable)]


def validate_report(state, report, *, after, variant, previous_data=None, checked=False, accepted_variant=None):
    if not math.isfinite(after) or after < state['created']:
        raise ValueError('Fresh launch fence required')
    if (report.get('schema') != SCHEMA or report.get('session') != state['session'] or
            report.get('variant') != variant or type(report.get('pid')) is not int or report['pid'] <= 0 or
            type(report.get('started')) not in (int, float) or not math.isfinite(report['started']) or
            not after <= report['started'] <= time.time() + 2):
        raise ValueError('Stale or foreign v2 runtime report')
    expected_code = {'free': 'free:base', 'pro': 'pro:42', 'enterprise': 'enterprise:24'}[variant]
    selection, data = report['selection'], report['data']
    current = selection_identity(selection['current'])
    accepted = selection_identity(selection['accepted'])
    expected = latest_selection(state)
    if (int(current['epoch']) > int(accepted['epoch']) or
            (current['epoch'] == accepted['epoch'] and current['digest'] != accepted['digest'])):
        raise ValueError('Process status contradicts accepted authorization')
    if accepted_variant is not None and (not checked or accepted_variant not in VARIANTS):
        raise ValueError('Expected accepted variant requires a checked report')
    observed = state.get('observed_selection')
    if observed and int(accepted['epoch']) < int(observed['epoch']):
        raise ValueError('Updater accepted authorization moved behind its observed high-water epoch')
    if state.get('installation_id', report['installation_id']) != report['installation_id']:
        raise ValueError('Installed updater identity changed')
    if (report['new_code_result'] != expected_code or selection['version'] != 2 or
            selection['current']['variant_id'] != variant or selection['current']['license_type'] != variant or
            selection['current']['patch_number'] != EXPECTED_NUMBERS[variant]):
        raise ValueError('Running variant code or process-pinned status disagrees')
    if (data.get('session') != state['session'] or data.get('sentinel') != state['sentinel'] or
            type(data.get('schema')) is not int or data['schema'] < {'free': 1, 'pro': 2, 'enterprise': 3}[variant] or
            type(data.get('launches')) is not int or not isinstance(data.get('history'), list) or
            len(data['history']) != data['launches'] or not data['history'] or data['history'][-1] != variant):
        raise ValueError('Persistent application data/readback is incomplete')
    if previous_data:
        if (data['launches'] != previous_data['launches'] + 1 or
                data['history'] != previous_data['history'] + [variant] or data['schema'] < previous_data['schema'] or
                any(data.get(key) != value for key, value in previous_data.items() if key not in ('launches', 'history', 'schema'))):
            raise ValueError('Downgrade or variant switch lost app-owned data')
    if checked:
        target_variant = accepted_variant if accepted_variant is not None else expected['variant_id']
        # Only the updater verifies the signed grant. This owned report carries
        # readSelectionStatus output, never arbitrary descriptor/entitlement data.
        # Initial free and an explicitly expected server downgrade can advance
        # one epoch without an admin assignment; paid authority stays pinned to
        # the real assignment response. The new digest is pinned after FFI/report
        # validation in read_report, not treated as a Python signature check.
        automatic_free = (target_variant == 'free' and accepted['variant_id'] == 'free' and
                          int(accepted['epoch']) == int(expected['epoch']) + 1 and
                          (expected['epoch'] == '0' or expected['variant_id'] != 'free'))
        if (report['stage'] != 'checked' or report['error'] is not None or
                (accepted != expected and not automatic_free) or accepted['epoch'] == '0' or
                accepted['variant_id'] != target_variant or
                selection['next_boot']['variant_id'] != target_variant):
            raise ValueError('Latest signed assignment was not accepted by the installed updater')
        if selection['current']['epoch'] != accepted['epoch']:
            if selection['restart_required'] is not True or report['update_status'] != 'restartRequired':
                raise ValueError('New authorization epoch must require restart even for identical content')
    elif accepted != expected and accepted not in known_selections(state):
        raise ValueError('Unchecked report cannot introduce an unknown authorization')
    return report


def validate_cold_activation(state, report, *, fallback=False):
    """Require exact accepted authorization after a cold launch, or explicit BASE failure."""
    expected = latest_selection(state)
    status = report['selection']
    if selection_identity(status['accepted']) != expected:
        raise ValueError('Cold launch lost accepted authorization/high-water epoch')
    current = selection_identity(status['current'])
    if fallback:
        if (report['variant'] != 'free' or current['variant_id'] != 'free' or
                current['patch_number'] is not None or current['epoch'] != expected['epoch'] or
                current['digest'] != expected['digest']):
            raise ValueError('Failed/expired authorization must execute bundled free only')
    elif (current != expected or
          report['variant'] != expected['variant_id'] or status['restart_required'] is not False):
        raise ValueError('Cold process did not adopt the exact latest authorization')
    return report


def read_report(work, state, *, after, variant, previous_data=None, checked=False, accepted_variant=None):
    path = v1.container_file(state, 'variant-status.json')
    if path.is_symlink() or path.stat().st_size > 64 * 1024 or path.stat().st_mtime < after:
        raise ValueError('Stale/redirected/oversized report')
    report = validate_report(state, json.loads(path.read_text()), after=after, variant=variant,
                             previous_data=previous_data, checked=checked, accepted_variant=accepted_variant)
    # The existing exact FFI report is independently required on every launch.
    v1.validate_status(work, state, v1.container_file(state, 'ffi-status.json'), after,
                       'base' if variant == 'free' else 'update')
    remember_installation(work, state, report)
    state['observed_selection'] = selection_identity(report['selection']['accepted'])
    v1.save(work, state)
    return report


def begin_cleanup(work, state):
    require_open(work, state)
    state['cleanup_fence'] = time.time()
    v1.save(work, state)
    return state['cleanup_fence']


def validate_key_deletion(state, receipt, variant):
    if (receipt.get('schema') != 'macos-variant-key-control-v2' or receipt.get('session') != state['session'] or
            receipt.get('bundle_id') != state['bundle_id'] or receipt.get('service') != state['key_service'] or
            receipt.get('account') != state['key_ids'][variant] or receipt.get('action') != 'delete' or
            receipt.get('result') not in ('deleted', 'absent') or type(receipt.get('pid')) is not int or receipt['pid'] <= 0 or
            type(receipt.get('time')) not in (int, float) or
            not math.isfinite(receipt['time']) or not state.get('cleanup_fence', float('inf')) <= receipt['time'] <= time.time() + 2):
        raise ValueError('Every variant key requires a fresh canonical native deletion receipt')


def cleanup(work, state):
    latest = load(work)
    state.clear()
    state.update(latest)
    if any(v1.port_open(urlparse(state[name]).port) for name in ('api_url', 'report_url')):
        raise ValueError('Stop owned server processes before cleanup; no foreign PID is stopped')
    if not state.get('cleanup_fence'):
        raise ValueError('Begin cleanup before final native key deletion')
    progress = state.get('cleanup_in_progress')
    if not progress:
        receipts = {}
        for variant, account in state['key_ids'].items():
            canonical = v1.container_file(state, 'key-control-' + account + '.json')
            value = canonical.read_bytes()
            validate_key_deletion(state, json.loads(value), variant)
            if canonical.stat().st_mtime < state['cleanup_fence']:
                raise ValueError('Native deletion predates cleanup fence')
            receipts[variant] = value
        directory = work / 'cleanup-evidence'
        directory.mkdir(mode=0o700, exist_ok=True)
        progress = {'receipts': {}}
        for variant, value in receipts.items():
            saved = directory / (variant + '-native-key-deletion.json')
            v1.shared.private_write(saved, value)
            progress['receipts'][variant] = {'path': str(saved), 'sha256': hashlib.sha256(value).hexdigest()}
        state['cleanup_in_progress'] = progress
        v1.save(work, state)
    for variant, entry in progress['receipts'].items():
        value = v1.contained(work, entry['path']).read_bytes()
        if hashlib.sha256(value).hexdigest() != entry['sha256']:
            raise ValueError('Pinned native deletion evidence changed')
        validate_key_deletion(state, json.loads(value), variant)
        canonical = v1.container_file(state, 'key-control-' + state['key_ids'][variant] + '.json')
        if canonical.exists() and canonical.read_bytes() != value:
            raise ValueError('Latest native key control changed after cleanup started')
    if set(progress['receipts']) != set(state['key_ids']):
        raise ValueError('Missing one variant key teardown')
    app = v1.contained(work, state['released_app']) if state.get('released_app') else None
    if app and app.exists():
        v1.app_identity(work, state, app)
        shutil.rmtree(app)
    result = v1.remove_owned_container(v1.container_root(state))
    (work / 'secrets.json').unlink(missing_ok=True)
    for name in ('config',):
        path = v1.contained(work, work / name)
        if path.exists():
            shutil.rmtree(path)
    state['cleanup'] = dict(result, all_variant_keys_deleted=True, cleanup_complete=result['container_removed'],
                            bootstrap_secrets_removed=True, owned_app_removed=True)
    v1.save(work, state)
    return state['cleanup']


def plan(target_arch='arm64'):
    return {'schema': SCHEMA, 'runtime_acceptance': False,
            'target': v1.target_architecture({'arch':target_arch}),
            'build_order': list(BUILD_ORDER), 'cold_lifecycle': list(LIFECYCLE),
            'required_runtime_cases': ['same-content-higher-epoch-restart', 'offline-paid-continuation',
                'missing-key-base-and-recovery', 'wrong-key-base-and-recovery', 'ciphertext-tamper-base',
                'signature-tamper-base', 'older-epoch-response-rejected', 'expired-lease-base',
                'no-paid-fallback-after-free', 'all-variant-native-key-deletion'],
            'caller_owned': ['Developer ID signing and exact-entitlement inspection with v1 helpers',
                'all sequential launches and process cleanup through OwnedHostProcesses',
                'offline endpoint closure and signed app hash invariance',
                'owned cache-only tamper/rollback injection and evidence, no simulation as acceptance'],
            'automatic_runtime_injection': False}


def parser():
    result = v1.parser()
    result._actions[1].choices = ('prepare', 'serve', 'base', 'pro_seed', 'pro', 'enterprise', 'map-free', 'assign', 'plan', 'cleanup')
    result.add_argument('--updater-package', type=Path)
    result.add_argument('--variant', choices=VARIANTS)
    result.add_argument('--offline-valid-until')
    return result


def main():
    args = parser().parse_args()
    work = args.work_dir.expanduser().resolve()
    if args.phase == 'plan' or not args.execute:
        print(json.dumps(plan(args.target_arch), indent=2))
        return
    if args.phase == 'prepare':
        if not all((args.cli, args.flutter_root, args.engine_src, args.server, args.updater_package)):
            raise ValueError('prepare requires private CLI, Flutter/engine, real server and updater package paths')
        prepare(args, work)
        return
    state = load(work)
    if args.phase == 'serve':
        serve(work, state)
    elif args.phase in BUILD_ORDER:
        build_phase(work, state, args.phase)
    elif args.phase == 'map-free':
        map_free(work, state)
    elif args.phase == 'assign':
        print(json.dumps(assign(work, state, args.variant, offline_valid_until=args.offline_valid_until)))
    elif args.phase == 'cleanup':
        if args.begin:
            begin_cleanup(work, state)
        else:
            print(json.dumps(cleanup(work, state)))


if __name__ == '__main__':
    main()

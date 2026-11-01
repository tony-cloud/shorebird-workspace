#!/usr/bin/env python3
"""Explicit protocol-2 physical iOS fixture. Plans/tests are not device evidence.

Uses only a fresh external work directory and prebuilt private tools. No engine
or CLI build, signing-identity creation, provisioning, or device discovery is
implicit. Every mutation requires --execute. Distribution signing is checked
before the sole installation; later variant changes cold-launch that same app.
Native key-control launches remain fenced until read-report validates their
original nonce-bound native receipt, including when devicectl returns success.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import threading
import time
from urllib.parse import urlparse
import uuid

import ios_device_e2e as ios
import macos_variant_e2e as selection
from owned_host_processes import OwnedHostProcesses

SCHEMA = 'ios-variant-e2e-v2'
TEMPLATES = Path(__file__).with_name('ios_variant')
VARIANTS = selection.VARIANTS
BUILD_ORDER = selection.BUILD_ORDER
LIFECYCLE = selection.LIFECYCLE
MAX_REPORT = 64 * 1024


def save(work, state):
    temporary = work / 'state.next.json'
    ios.write_json(temporary, state)
    temporary.replace(work / 'state.json')


def load(work):
    state = ios.load(work)
    if (state.get('schema') != SCHEMA or state.get('work_dir') != str(work.resolve()) or
            not re.fullmatch(r'org\.tonycloud\.shorebird-ios-variant\.[a-f0-9]{12}', state.get('bundle_id', '')) or
            set(state.get('key_ids', {})) != {'pro', 'enterprise'} or len(set(state['key_ids'].values())) != 2):
        raise ValueError('Not an owned iOS protocol-2 fixture')
    return state


def require_open(work, state, *, deletion=False):
    current = load(work)
    if current['session'] != state['session'] or current.get('cleanup'):
        raise ValueError('Session closed or changed')
    if current.get('cleanup_fence') and not deletion:
        raise ValueError('Cleanup permits only final native key deletion')
    if current.get('cleanup_fence') != state.get('cleanup_fence'):
        raise ValueError('Reload the cleanup fence before a deletion launch')
    require_seals(work, state)


def fixture_sources():
    return [Path(__file__), TEMPLATES / 'AppDelegate.swift.template', TEMPLATES / 'variant_status.dart.template',
        Path(ios.__file__), Path(selection.__file__), Path(selection.v1.__file__),
        Path(__file__).with_name('owned_host_processes.py'),
        ios.FIXTURE / 'ios/Runner/FullFfiFixture.c', ios.FIXTURE / 'ios/Runner/FullFfiFixture.h',
        ios.FIXTURE / 'ios/Runner.xcodeproj/project.pbxproj',
        *sorted((ios.FIXTURE / 'tool/full_replacement_ffi').rglob('*.template'))]


def fixture_hashes():
    sources = fixture_sources()
    if any(p.is_symlink() or not p.is_file() for p in sources):
        raise ValueError('Fixture source alias or missing file')
    return {str(p.relative_to(ios.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}


def copied_template_hashes(work):
    sources = sorted((work / 'app/tool/full_replacement_ffi').rglob('*.template'))
    if not sources or any(p.is_symlink() or not p.is_file() for p in sources):
        raise ValueError('Copied fixture template alias or missing files')
    return {str(p.relative_to(work / 'app')): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}


def require_seals(work, state):
    """Fail before consuming changed inputs; recorded provenance is a gate."""
    if state.get('fixture_hashes') != fixture_hashes():
        raise ValueError('Prepared fixture source seal changed; use the original sources or a fresh session')
    if state.get('copied_template_hashes') != copied_template_hashes(work):
        raise ValueError('Prepared copied template seal changed')
    if set(state.get('tool_hashes', {})) != {'cli', 'server'}:
        raise ValueError('Prepared tool seal missing')
    for name, expected in state['tool_hashes'].items():
        path = Path(state[name])
        if path.is_symlink() or not path.is_file() or selection.v1.digest(path) != expected:
            raise ValueError('Prepared tool seal changed: ' + name)


def require_key_launch_reconciled(work):
    if load(work).get('pending_key_launch'):
        raise ValueError('Native key-control launch unresolved; read the original nonce-bound receipt before another launch')


def native_hashes(work):
    return {str(p.relative_to(work / 'app')): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((work / 'app/ios').rglob('*'))
            if p.is_file() and not any(x in p.parts for x in ('Pods', '.symlinks', 'Flutter'))}


def prepare(args, work):
    if work.exists() or work.is_symlink() or work.resolve().is_relative_to(ios.ROOT.resolve()):
        raise ValueError('Use a fresh work directory outside the workspace')
    paths = {name: getattr(args, name).resolve() for name in ('cli', 'flutter_root', 'engine_src', 'server', 'updater_package')}
    for name in ('cli', 'flutter_root', 'updater_package'):
        if paths[name].is_relative_to(ios.ROOT.resolve()):
            raise ValueError('Use pinned private CLI/Flutter/updater copies outside workspace')
    if not (paths['updater_package'] / 'lib/src/selection_status.dart').is_file():
        raise ValueError('V2 updater package required')
    for name in ('cli', 'server'):
        if not paths[name].is_file():
            raise ValueError('Supply an existing prebuilt ' + name + ' binary')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+', args.release_version):
        raise ValueError('Numeric release name+build required')
    host = args.host_address
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or host in ('0.0.0.0', 'localhost', '127.0.0.1'):
        raise ValueError('Advertise a device-reachable hostname or IPv4 address')
    work.mkdir(parents=True, mode=0o700)
    api_port, report_port = selection.v1.unique_ports()
    session = secrets.token_hex(6)
    state = dict(schema=SCHEMA, work_dir=str(work.resolve()), session=session, created=time.time(),
        platform='ios', arch='aarch64', device=args.device, team=args.team,
        bundle_id=f'org.tonycloud.shorebird-ios-variant.{session}',
        key_service=f'org.tonycloud.shorebird-ios-key.{session}',
        key_ids={v: f'{session}-{v}-artifact' for v in ('pro', 'enterprise')},
        sentinel=secrets.token_hex(24), release_version=args.release_version,
        api_url=f'http://{host}:{api_port}', report_url=f'http://{host}:{report_port}/report/{session}',
        bind_address=args.bind_address, full_replacement=True, full_replacement_ffi=True,
        host_engine=str(paths['engine_src'] / 'out/host_release_arm64'),
        built_phases=[], assignments=[], launches=[], **{k: str(v) for k, v in paths.items()})
    state['fixture_hashes'] = fixture_hashes()
    state['tool_hashes'] = {name: selection.v1.digest(paths[name]) for name in ('cli', 'server')}
    ios.write_json(work / 'secrets.json', dict(bootstrap_password=secrets.token_urlsafe(32),
        admin_password=secrets.token_urlsafe(32), jwt_secret=secrets.token_urlsafe(48),
        signing_master_key=base64.b64encode(secrets.token_bytes(32)).decode(),
        variant_keys={v: secrets.token_hex(32) for v in ('pro', 'enterprise')}))
    shutil.copytree(ios.FIXTURE, work / 'app', ignore=shutil.ignore_patterns(
        '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', '.git', '__pycache__'))
    state['copied_template_hashes'] = copied_template_hashes(work)
    project = work / 'app/ios/Runner.xcodeproj/project.pbxproj'
    old = 'com.example.licenseflavorpatchtest.licenseFlavorPatchTest'
    if old not in project.read_text():
        raise ValueError('Review changed fixture bundle settings')
    project.write_text(project.read_text().replace(old, state['bundle_id']))
    pubspec = work / 'app/pubspec.yaml'
    source = pubspec.read_text()
    if source.count('\ndependencies:\n') != 1 or 'shorebird_code_push:' in source or re.search(r'^version:', source, re.M):
        raise ValueError('Review changed fixture dependencies/version')
    pubspec.write_text(source.replace('\ndependencies:\n', '\ndependencies:\n  ffi: ^2.0.2\n  shorebird_code_push:\n    path: ' + json.dumps(state['updater_package']) + '\n') + '\nversion: ' + state['release_version'] + '\n')
    ios.private_write(work / 'ExportOptions.plist', plistlib.dumps(dict(method='release-testing',
        teamID=state['team'], signingStyle='automatic', manageAppVersionAndBuildNumber=False, stripSwiftSymbols=True)))
    apply_profile(work, state, 'base')
    save(work, state)
    return state


def apply_profile(work, state, phase):
    if phase not in BUILD_ORDER:
        raise ValueError('Unsupported build phase')
    require_seals(work, state)
    ios.apply_fixture(work, 'base' if phase == 'base' else 'patch', 'full_replacement_ffi')
    variant = 'free' if phase == 'base' else 'pro' if phase == 'pro_seed' else phase
    code = {
        'base': "String variantSpecificCode() => 'free:base';",
        'pro_seed': "class ProSeedFeature { String value() => 'pro:seed'; }\nString variantSpecificCode() => ProSeedFeature().value();",
        'pro': "class ProVariantFeature { int calculate(int value) => value * 7; }\nString variantSpecificCode() => 'pro:${ProVariantFeature().calculate(6)}';",
        'enterprise': "class EnterpriseVariantFeature { final List<int> seats; EnterpriseVariantFeature(this.seats); int total() => seats.fold(0, (a, b) => a + b); }\nString enterpriseOnlyFunction() => 'enterprise:${EnterpriseVariantFeature([4, 8, 12]).total()}';\nString variantSpecificCode() => enterpriseOnlyFunction();",
    }[phase]
    source = (TEMPLATES / 'variant_status.dart.template').read_text()
    for key, value in {'__SESSION__': state['session'], '__SENTINEL__': state['sentinel'],
            '__VARIANT__': variant, '__DATA_SCHEMA__': str({'free': 1, 'pro': 2, 'enterprise': 3}[variant]), '__VARIANT_CODE__': code}.items():
        source = source.replace(key, value)
    ios.private_write(work / 'app/lib/variant_status.dart', source)
    main = work / 'app/lib/main.dart'
    source = "import 'variant_status.dart';\n" + main.read_text()
    for marker in ('final statusKey = GlobalKey();', 'home: Scaffold(\n', '  await firstFrame.future;', '    evidence: evidence,\n  );'):
        if source.count(marker) != 1:
            raise ValueError('Review changed FFI/render completion path')
    source = source.replace('final statusKey = GlobalKey();', 'final statusKey = GlobalKey();\nfinal variantStatusKey = GlobalKey();')
    source = source.replace('home: Scaffold(\n', "home: Scaffold(\n        appBar: AppBar(title: Text('variant:$fixtureVariant ${variantSpecificCode()}', key: variantStatusKey)),\n")
    source = source.replace('  await firstFrame.future;', """  await firstFrame.future;
  final variantRendered = variantStatusKey.currentContext?.findRenderObject();
  if (variantRendered is! RenderParagraph || !variantRendered.attached ||
      !variantRendered.hasSize || variantRendered.size.isEmpty ||
      variantRendered.text.toPlainText() != 'variant:$fixtureVariant ${variantSpecificCode()}') {
    throw StateError('Selected variant rendered assertion failed');
  }""")
    main.write_text(source.replace('    evidence: evidence,\n  );', '    evidence: evidence,\n  );\n  await writeVariantStatus();'))
    # Carry the completed checks in this process. A previous temp file must
    # never satisfy the new nonce if filesystem export fails on a device.
    ffi_status = work / 'app/lib/ffi_status.dart'
    source = ffi_status.read_text()
    if source.count('class FfiEvidence {') != 1 or source.count('  try {\n    File(') != 1:
        raise ValueError('Review changed full FFI completion writer')
    source = source.replace('class FfiEvidence {', 'String? completedFfiStatus;\n\nclass FfiEvidence {')
    ffi_status.write_text(source.replace('  try {\n    File(', '  completedFfiStatus = contents;\n  try {\n    File('))
    ios.write_json(work / f'{phase}-defines.json', {'LICENSE_TYPE': variant})


def configure_native(work, state):
    require_seals(work, state)
    if state.get('base_built'):
        if native_hashes(work) != state['native_source_hashes']:
            raise ValueError('Released native sources changed')
        return
    source = (TEMPLATES / 'AppDelegate.swift.template').read_text()
    for key, value in {'__SERVICE__': state['key_service'], '__BUNDLE_ID__': state['bundle_id'],
            '__SESSION__': state['session'], '__APP_ID__': state['app_id'], '__RELEASE__': state['release_version'],
            '__PRO_KEY_ID__': state['key_ids']['pro'], '__ENTERPRISE_KEY_ID__': state['key_ids']['enterprise']}.items():
        source = source.replace(key, value)
    ios.private_write(work / 'app/ios/Runner/AppDelegate.swift', source)
    state['native_source_hashes'] = native_hashes(work)
    ios.private_write(work / 'app/shorebird.yaml', selection.yaml_source(state))


def api(work, state, method, suffix, body=None):
    token = json.loads((work / 'secrets.json').read_text())['access_token']
    return ios.request(state, method, f"/api/v1/apps/{state['app_id']}" + suffix, body, token)


def release_id(work, state):
    matches = [x for x in api(work, state, 'GET', '/releases')['releases'] if x['version'] == state['release_version']]
    if len(matches) != 1 or matches[0].get('selection_protocol') != 2:
        raise ValueError('Expected one real protocol-2 release')
    return matches[0]['id']


def map_free(work, state):
    require_open(work, state)
    return api(work, state, 'PUT', f'/releases/{release_id(work, state)}/variants/free', {
        'platform': 'ios', 'arch': 'aarch64', 'channel': 'stable', 'license_type': 'free',
        'target': {'kind': 'base', 'patch_number': None, 'artifact_hash': None, 'encryption': 'none', 'artifact_key_id': None}})


def assign(work, state, variant, *, offline_valid_until=None, not_before=None):
    require_open(work, state)
    if variant not in VARIANTS or not state.get('installation_id'):
        raise ValueError('Capture the real installation ID before assigning a variant')
    pending = state.get('pending_assignment')
    if pending:
        if pending['variant_id'] != variant or pending['offline_valid_until'] != offline_valid_until or (not_before is not None and pending['not_before'] != not_before):
            raise ValueError('Resolve persisted idempotent assignment first')
        body = pending
    else:
        body = dict(client_id=state['installation_id'], platform='ios', arch='aarch64', channel='stable',
            variant_id=variant, entitlement_revision=state['session'] + ':' + str(uuid.uuid4()),
            not_before=str(int(time.time())) if not_before is None else not_before, offline_valid_until=offline_valid_until,
            expected_epoch=selection.latest_selection(state)['epoch'], idempotency_key=str(uuid.uuid4()))
        state['pending_assignment'] = body
        save(work, state)
    result = api(work, state, 'POST', f'/releases/{release_id(work, state)}/selections', body)
    descriptor = result['selection']
    identity = selection.selection_identity(dict(epoch=descriptor['epoch'], digest=result['selection_digest'],
        variant_id=descriptor['variant_id'], license_type=descriptor['license_type'], patch_number=descriptor['target']['patch_number']))
    expected_scope = {**state['release_scope'], 'client_id_hash': hashlib.sha256(state['installation_id'].encode()).hexdigest()}
    if (identity['epoch'] != str(int(body['expected_epoch']) + 1) or identity['variant_id'] != variant or
            descriptor['scope'] != expected_scope or descriptor['channel'] != 'stable' or
            descriptor['entitlement_revision'] != body['entitlement_revision']):
        raise ValueError('Signed assignment response differs from installed release/epoch/variant')
    if (descriptor.get('not_before') != body['not_before'] or
            'offline_valid_until' not in descriptor or descriptor['offline_valid_until'] != body['offline_valid_until']):
        raise ValueError('Signed assignment response differs from persisted lease request')
    if any(x['epoch'] == identity['epoch'] and x != identity for x in selection.known_selections(state)):
        raise ValueError('Assignment conflicts with observed authorization')
    state['assignments'].append(result)
    state.pop('pending_assignment', None)
    save(work, state)
    return result


def cli_command(work, state, phase):
    if phase not in BUILD_ORDER:
        raise ValueError('Unsupported build phase')
    command = [state['cli'], '--json', '--local-engine-src-path=' + state['engine_src'],
        '--local-engine=ios_release', '--local-engine-host=host_release_arm64',
        'release' if phase == 'base' else 'patch', 'ios', f'--dart-define-from-file={work}/{phase}-defines.json']
    if phase == 'base':
        command.append(f'--export-options-plist={work}/ExportOptions.plist')
    else:
        command.extend(['--release-version=' + state['release_version'], '--track=stable', '--variant=' + ('pro' if phase == 'pro_seed' else phase)])
    return command


def build_environment(work, state, phase):
    credentials = json.loads((work / 'secrets.json').read_text())
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SHOREBIRD_AOT_', 'SHOREBIRD_IOS_', 'DYLD_'))}
    env.update(SHOREBIRD_E2E_CONFIG_DIR=str(work / 'config'), SHOREBIRD_HOSTED_URL=state['api_url'],
        AUTH_SERVICE_URL=state['api_url'] + '/auth', SHOREBIRD_TOKEN='sb_api_' + credentials['access_token'],
        SHOREBIRD_ARTIFACT_BASE_URL=state['api_url'], FLUTTER_ROOT=state['flutter_root'],
        SHOREBIRD_FLUTTER_STORAGE_BASE_URL='https://storage.googleapis.com', PUB_CACHE=str(work / 'pub-cache'),
        FLUTTER_SUPPRESS_ANALYTICS='true', DART_SUPPRESS_ANALYTICS='true', CI='true', BOT='true',
        SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS='0', PATH=state['flutter_root'] + '/bin' + os.pathsep + os.environ['PATH'])
    if phase != 'base':
        variant = 'pro' if phase == 'pro_seed' else phase
        env.update(SHOREBIRD_AOT_PATCH_KEY_HEX=credentials['variant_keys'][variant],
            SHOREBIRD_AOT_PATCH_KEY_ID=state['key_ids'][variant], SHOREBIRD_AOT_PATCH_FLAVOR_ID=variant,
            SHOREBIRD_AOT_PATCH_LICENSE_TYPE=variant)
    return env


def validate_compiled_scope(state, app):
    scopes = list(app.rglob('shorebird_release_scope_ios_aarch64.json'))
    baselines = list(app.rglob('shorebird_replacement_base.sbr'))
    if len(scopes) != 1 or len(baselines) != 1:
        raise ValueError('Unique original full baseline and compiled v2 scope required')
    scope = json.loads(scopes[0].read_text())
    expected = dict(app_id=state['app_id'], release_version=state['release_version'], platform='ios',
        arch='aarch64', runtime_profile='interpreter-full', base_artifact_hash=hashlib.sha256(baselines[0].read_bytes()).hexdigest())
    if set(scope) != set(expected) | {'host_abi_hash'} or any(scope.get(k) != v for k, v in expected.items()) or not re.fullmatch('[a-f0-9]{64}', scope.get('host_abi_hash') or ''):
        raise ValueError('Compiled scope differs from original iOS release')
    configs = list(app.rglob('shorebird.yaml'))
    if len(configs) != 1:
        raise ValueError('Unique signed release YAML required')
    source = configs[0].read_text()
    for pattern in (r'^selection_protocol: 2$', r'^auto_update: false$', r'^patch_verification: strict$',
            r'^patch_response_verification: required$', r'^patch_public_key: .+',
            '^aot_patch_app_build_id: ' + re.escape(state['release_version']) + '$',
            '^aot_patch_base_snapshot_hash: ' + scope['base_artifact_hash'] + '$', '^aot_patch_host_abi: ' + scope['host_abi_hash'] + '$'):
        if not re.search(pattern, source, re.M):
            raise ValueError('Compiled signature/scope requirement absent')
    if re.search(r'^aot_patch_key_hex:', source, re.M):
        raise ValueError('Raw encryption keys cannot be bundled')
    if state.get('release_scope', scope) != scope:
        raise ValueError('Original release scope changed')
    state['release_scope'] = scope
    return scope


def build_phase(work, state, phase):
    require_open(work, state)
    done = state['built_phases']
    if len(done) >= len(BUILD_ORDER) or phase != BUILD_ORDER[len(done)] or not state.get('app_id'):
        raise ValueError('Bootstrap then build base, pro_seed, pro, enterprise exactly in order')
    configure_native(work, state)
    apply_profile(work, state, phase)
    ios.run(cli_command(work, state, phase), cwd=work / 'app', env=build_environment(work, state, phase), log=work / f'{phase}.log')
    if phase == 'base':
        apps = list((work / 'app/build/ios/archive').glob('*.xcarchive/Products/Applications/*.app'))
        if len(apps) != 1:
            raise ValueError('Expected one real signed archive app')
        validate_compiled_scope(state, apps[0])
        state['base_built'] = True
    done.append(phase)
    save(work, state)


def validate_report(state, report, *, nonce, after, variant, previous_data=None, checked=False, accepted_variant=None, fallback=False, cold=False):
    if fallback and not cold:
        raise ValueError('Fallback acceptance requires cold activation')
    if report.get('schema') != SCHEMA or report.get('nonce') != nonce or not re.fullmatch('[a-f0-9]{48}', nonce):
        raise ValueError('Stale or foreign nonce-bound iOS report')
    # Reuse the independently exercised v2 data/epoch rules; this adaptation is
    # explicit and cannot make a historical Mac report satisfy the iOS schema.
    adapted = {**report, 'schema': selection.SCHEMA}
    selection.validate_report(state, adapted, after=after, variant=variant, previous_data=previous_data,
        checked=checked, accepted_variant=accepted_variant)
    installation = report.get('installation_id')
    if not isinstance(installation, str) or not 0 < len(installation.encode()) <= 1024:
        raise ValueError('Real public installation ID required')
    data = report['data']
    if ((data['schema'] >= 2 and data.get('pro_note') != 'pro-preserved') or
            (data['schema'] >= 3 and data.get('enterprise_note') != 'enterprise-preserved')):
        raise ValueError('Variant-owned additive data missing')
    ffi = ios.validate_launch_report(json.dumps(report['ffi']), {'full_replacement_ffi': True})
    if ffi['phase'] != ('base' if variant == 'free' else 'update') or report.get('post_frame') is not True:
        raise ValueError('Distinct completed full FFI/render assertion required')
    if report.get('status_route') not in ('public', 'native-ffi') or report.get('report_route') not in ('native', 'dart', 'file'):
        raise ValueError('Missing explicit native/public status and observer route')
    if cold:
        selection.validate_cold_activation(state, adapted, fallback=fallback)
        if variant != 'free':
            provider = report.get('provider')
            current = report['selection']['current']
            if (not isinstance(provider, dict) or provider.get('schema') != 'ios-variant-provider-v2' or
                    provider.get('session') != state['session'] or provider.get('nonce') != nonce or
                    provider.get('variant') != variant or provider.get('account') != state['key_ids'][variant] or
                    provider.get('selection_digest') != current['digest'] or provider.get('epoch') != current['epoch'] or
                    provider.get('pid') != report['pid']):
                raise ValueError('Cold paid launch lacks nonce-bound native key-context evidence')
    return report


def launch_environment(work, state, *, check=False, action=None, variant=None, wrong_key=False, nonce=None, report_route='native', status_route='public'):
    require_open(work, state, deletion=action == 'delete')
    require_key_launch_reconciled(work)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DYLD_', 'OPEN_SHOREBIRD_TEST_', 'DEVICECTL_CHILD_'))}
    nonce = nonce or secrets.token_hex(24)
    values = dict(NONCE=nonce, CHECK='1' if check else '0', OBSERVER=state['report_url'],
        REPORT_ROUTE=report_route, STATUS_ROUTE=status_route)
    if not re.fullmatch('[a-f0-9]{48}', nonce) or report_route not in ('native', 'dart', 'file') or status_route not in ('public', 'native-ffi'):
        raise ValueError('Invalid launch nonce or evidence route')
    if action is not None:
        if action not in ('provision', 'overwrite', 'delete') or variant not in state['key_ids']:
            raise ValueError('Unsupported scoped native key control')
        if state.get('cleanup_fence') and (action != 'delete' or check):
            raise ValueError('Cleanup allows only native deletion launches')
        values.update(KEY_ACTION=action, KEY_ID=state['key_ids'][variant])
        if action != 'delete':
            values['KEY_HEX'] = '00' * 32 if wrong_key else json.loads((work / 'secrets.json').read_text())['variant_keys'][variant]
    env.update({'DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_' + k: v for k, v in values.items()})
    return env, nonce


def launch(work, state, **options):
    if not state.get('installed_app_sha256'):
        raise ValueError('Inspect and install the signed original app exactly once first')
    env, nonce = launch_environment(work, state, **options)
    record = dict(nonce=nonce, after=time.time(), check=options.get('check', False),
        action=options.get('action'), variant=options.get('variant'),
        report_route=options.get('report_route', 'native'), status_route=options.get('status_route', 'public'))
    state['launches'].append(record)
    if record['action'] is not None:
        # A successful host command also cannot prove the native mutation ran.
        # Keep this fence through command errors/restarts until its exact
        # nonce-bound native receipt passes read_report's acceptance gates.
        state['pending_key_launch'] = dict(record)
    save(work, state)  # Lost device response never loses the freshness fence.
    ios.run(['xcrun', 'devicectl', 'device', 'process', 'launch', '--quiet', '--device', state['device'],
        '--terminate-existing', state['bundle_id']], env=env, log=work / f'launch-{nonce}.log')
    return record


def read_report(work, state, *, variant, path=None, checked=False, accepted_variant=None, cold=False, fallback=False):
    require_seals(work, state)
    current = load(work)
    if current.get('pending_key_launch') != state.get('pending_key_launch'):
        raise ValueError('Reload the unresolved native key-control launch before receipt reconciliation')
    if not state['launches']:
        raise ValueError('No owned launch exists')
    launch = state['launches'][-1]
    path = path or work / 'reports' / (launch['nonce'] + '.json')
    if path.is_symlink() or path.stat().st_size > MAX_REPORT or path.stat().st_mtime < launch['after']:
        raise ValueError('Redirected, stale, or oversized report')
    report = validate_report(state, json.loads(path.read_text()), nonce=launch['nonce'], after=launch['after'], variant=variant,
        previous_data=state.get('previous_data'), checked=checked, accepted_variant=accepted_variant, cold=cold, fallback=fallback)
    if report['status_route'] != launch['status_route'] or report['report_route'] != launch['report_route'] or checked != launch['check']:
        raise ValueError('Launch/report evidence route or check mode changed')
    if launch['action']:
        validate_key_receipt(state, report.get('key_control'), launch)
        if report['key_control']['pid'] != report['pid']:
            raise ValueError('Native key control and runtime report came from different processes')
        pending = state.get('pending_key_launch')
        if pending and pending != launch:
            raise ValueError('Receipt does not reconcile the unresolved native key-control launch')
    if state.get('validated_nonce') == launch['nonce']:
        raise ValueError('Launch already consumed; cold activation requires another process')
    state.update(installation_id=report['installation_id'], observed_selection=selection.selection_identity(report['selection']['accepted']),
        previous_data=report['data'], validated_nonce=launch['nonce'])
    if launch['action'] == 'delete' and state.get('cleanup_fence'):
        state.setdefault('deleted_keys', {})[launch['variant']] = report['key_control']
    if launch['action']:
        state.pop('pending_key_launch', None)
    save(work, state)
    return report


def validate_key_receipt(state, receipt, launch):
    import math
    if (not isinstance(receipt, dict) or receipt.get('schema') != 'ios-variant-key-control-v2' or
            receipt.get('session') != state['session'] or receipt.get('nonce') != launch['nonce'] or
            receipt.get('bundle_id') != state['bundle_id'] or receipt.get('service') != state['key_service'] or
            receipt.get('account') != state['key_ids'][launch['variant']] or receipt.get('action') != launch['action'] or
            receipt.get('result') not in (('deleted', 'absent') if launch['action'] == 'delete' else ('stored',)) or
            type(receipt.get('time')) not in (int, float) or not math.isfinite(receipt['time']) or
            not max(launch['after'], state.get('cleanup_fence', 0)) <= receipt['time'] <= time.time() + 2 or
            type(receipt.get('pid')) is not int or receipt['pid'] <= 0):
        raise ValueError('Fresh native scoped key-control receipt required')


def receive_report(work, body):
    """Persist transport evidence only; read_report remains the acceptance gate."""
    if not 1 <= len(body) <= MAX_REPORT:
        raise ValueError('Oversized or empty report')
    current = load(work)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Object report required')
    launch = current['launches'][-1]
    if value.get('nonce') != launch['nonce'] or value.get('session') != current['session'] or value.get('schema') != SCHEMA:
        raise ValueError('Foreign nonce/session/schema')
    if value.get('stage') not in ('boot', 'checked', 'check_failed'):
        raise ValueError('Unknown report stage')
    target = work / 'reports' / (launch['nonce'] + '.json')
    if target.exists() and json.loads(target.read_text()).get('stage') in ('checked', 'check_failed') and value['stage'] == 'boot':
        raise ValueError('Report stage regressed')
    temporary = target.with_suffix('.next.json')
    ios.private_write(temporary, body)
    temporary.replace(target)


def serve(work, state):
    require_open(work, state)
    ports = [urlparse(state[x]).port for x in ('api_url', 'report_url')]
    if any(selection.v1.port_open(p) for p in ports):
        raise ValueError('Session port occupied; no process stopped')
    credentials = json.loads((work / 'secrets.json').read_text())
    report_path = urlparse(state['report_url']).path
    report_lock = threading.Lock()
    class Reports(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(5)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.path != report_path or self.headers.get('Transfer-Encoding') or not 1 <= size <= MAX_REPORT:
                    raise ValueError('Invalid framing/path')
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError('Truncated body')
                with report_lock:
                    receive_report(work, body)
            except (ValueError, TypeError, KeyError, IndexError, OSError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()
        def log_message(self, *_):
            pass
    observer = http.server.ThreadingHTTPServer((state['bind_address'], ports[1]), Reports)
    owned = OwnedHostProcesses()
    handlers = {}
    observing = False
    def stop(*_):
        raise KeyboardInterrupt
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, stop)
        env = dict(os.environ, DB_DRIVER='sqlite', DB_PATH=str(work / 'state.sqlite'), STORAGE_DRIVER='local',
            STORAGE_LOCAL_DIR=str(work / 'objects'), SERVER_HOST=state['bind_address'], SERVER_PORT=str(ports[0]),
            SERVER_BASE_URL=state['api_url'], JWT_SECRET=credentials['jwt_secret'], PATCH_SIGNING_MASTER_KEY=credentials['signing_master_key'],
            PATCH_DELIVERY_ENCRYPTION='off', SMTP_HOST='', DEFAULT_ADMIN_EMAIL='ios-variant@example.invalid',
            DEFAULT_ADMIN_PASSWORD=credentials['bootstrap_password'])
        with (work / 'server.log').open('ab') as log:
            child = owned.spawn([state['server']], env=env, stdout=log, stderr=log)
            for _ in range(100):
                if child.poll() is not None:
                    raise ValueError('Owned API exited')
                try:
                    ios.request(state, 'GET', '/health')
                    break
                except (OSError, RuntimeError):
                    time.sleep(.1)
            else:
                raise ValueError('Owned API not ready')
            login = ios.request(state, 'POST', '/auth/token', dict(email='ios-variant@example.invalid',
                password=credentials['admin_password'] if credentials.get('admin_password_active') else credentials['bootstrap_password']))
            token = login['token']
            if login.get('must_change_password'):
                ios.request(state, 'PATCH', '/api/v1/users/me/password', dict(current_password=credentials['bootstrap_password'], new_password=credentials['admin_password']), token)
                credentials['admin_password_active'] = True
            credentials['access_token'] = token
            ios.write_json(work / 'secrets.json', credentials)
            if not state.get('app_id'):
                org = ios.request(state, 'POST', '/api/v1/organizations', {'name': 'Disposable iOS variants'}, token)
                state['app_id'] = ios.request(state, 'POST', '/api/v1/apps', dict(organization_id=org['id'], display_name='Physical iOS protocol 2 variants'), token)['id']
            configure_native(work, state)
            save(work, state)
            threading.Thread(target=observer.serve_forever, daemon=True).start()
            observing = True
            print('Owned API and nonce observer ready; device acceptance still required.', flush=True)
            child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if observing:
            observer.shutdown()
        observer.server_close()
        owned.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def inspect_app(work, state, app):
    require_seals(work, state)
    if app is None:
        raise ValueError('Supply an exported distribution .app')
    app = ios.verified_app(argparse.Namespace(app=app), work, state)
    exports = (work / 'ffi-native-exports.log').read_text()
    if not re.search(r'\b_?IosVariantSubmitReport\b', exports):
        raise ValueError('Distribution Mach-O lacks the native observer FFI export')
    validate_compiled_scope(state, app)
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    return app, hashlib.sha256((app / info['CFBundleExecutable']).read_bytes()).hexdigest()


def install(work, state, app):
    require_open(work, state)
    if state.get('installed_app_sha256'):
        raise ValueError('Same-installed-app session forbids reinstalling')
    app, digest = inspect_app(work, state, app)
    # Persist before the command: uncertain install responses require explicit
    # investigation rather than replaying a device mutation.
    state['installation_started'] = time.time()
    if state.get('installation_uncertain'):
        raise ValueError('Previous installation outcome uncertain; do not replay')
    state['installation_uncertain'] = True
    save(work, state)
    ios.run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', state['device'], app], log=work / 'install.log')
    state.update(installed_app_sha256=digest, installed_app=str(app), installation_uncertain=False)
    save(work, state)


def begin_cleanup(work, state):
    require_open(work, state)
    require_key_launch_reconciled(work)
    state['cleanup_fence'] = time.time()
    save(work, state)


def cleanup(work, state):
    require_seals(work, state)
    require_key_launch_reconciled(work)
    if not state.get('cleanup_fence') or set(state.get('deleted_keys', {})) != set(state['key_ids']):
        raise ValueError('Begin cleanup then validate native deletion of both independent keys')
    for variant, receipt in state['deleted_keys'].items():
        launch = next(x for x in state['launches'] if x['nonce'] == receipt['nonce'])
        validate_key_receipt(state, receipt, launch)
    if any(selection.v1.port_open(urlparse(state[x]).port) for x in ('api_url', 'report_url')):
        raise ValueError('Stop owned server and observer before cleanup')
    if not state.get('owned_app_uninstalled'):
        if state.get('uninstall_uncertain'):
            raise ValueError('Previous uninstall outcome uncertain; investigate without replaying')
        state['uninstall_uncertain'] = True
        save(work, state)
        ios.run(['xcrun', 'devicectl', 'device', 'uninstall', 'app', '--device', state['device'], state['bundle_id']], log=work / 'uninstall.log')
        state.update(owned_app_uninstalled=True, uninstall_uncertain=False)
        save(work, state)
    (work / 'secrets.json').unlink(missing_ok=True)
    if (work / 'config').exists():
        shutil.rmtree(work / 'config')
    state['cleanup'] = dict(all_variant_keys_deleted=True, owned_app_uninstalled=True, secrets_removed=True)
    save(work, state)
    return state['cleanup']


def plan():
    return dict(schema=SCHEMA, runtime_acceptance=False, build_order=list(BUILD_ORDER), cold_lifecycle=list(LIFECYCLE),
        automatic_runtime_injection=False, device_route='physical distribution, get-task-allow=false, no debugger',
        default_observer='asynchronous native URLSession; optional Dart HTTP or exported container file',
        key_control_reconciliation='validate the original nonce-bound native receipt through read-report before another launch; host command success is insufficient',
        required_runtime_cases=selection.plan()['required_runtime_cases'],
        caller_owned=['private pinned toolchain and Xcode/signing/provisioning', 'serial build/device operations',
            'offline endpoint closure, cache tamper/older signed response injection and evidence',
            'native-ffi status route is explicit diagnostic evidence; public API route must also run'])


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase', choices=('plan', 'prepare', 'serve', *BUILD_ORDER, 'map-free', 'assign', 'inspect-app', 'install', 'launch', 'read-report', 'begin-cleanup', 'cleanup'))
    p.add_argument('--work-dir', required=True, type=Path)
    p.add_argument('--execute', action='store_true')
    for name in ('cli', 'flutter-root', 'engine-src', 'server', 'updater-package', 'app', 'report-file'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--host-address', default='192.168.93.205')
    p.add_argument('--bind-address', default='0.0.0.0')
    p.add_argument('--device', default=ios.DEVICE)
    p.add_argument('--team', default=ios.TEAM)
    p.add_argument('--release-version', default='1.0.0+1')
    p.add_argument('--variant', choices=VARIANTS)
    p.add_argument('--accepted-variant', choices=VARIANTS)
    p.add_argument('--offline-valid-until')
    p.add_argument('--not-before')
    p.add_argument('--action', choices=('provision', 'overwrite', 'delete'))
    p.add_argument('--wrong-key', action='store_true')
    p.add_argument('--check', action='store_true')
    p.add_argument('--cold', action='store_true')
    p.add_argument('--fallback', action='store_true')
    p.add_argument('--report-route', choices=('native', 'dart', 'file'), default='native')
    p.add_argument('--status-route', choices=('public', 'native-ffi'), default='public')
    return p


def main():
    p = parser()
    args = p.parse_args()
    work = args.work_dir.expanduser().resolve()
    if args.phase == 'plan' or not args.execute:
        print(json.dumps(plan(), indent=2))
        return
    if args.phase == 'prepare':
        if not all((args.cli, args.flutter_root, args.engine_src, args.server, args.updater_package)):
            p.error('prepare requires prebuilt private CLI/Flutter/engine/server/updater paths')
        prepare(args, work)
        return
    state = load(work)
    if args.phase == 'serve': serve(work, state)
    elif args.phase in BUILD_ORDER: build_phase(work, state, args.phase)
    elif args.phase == 'map-free': map_free(work, state)
    elif args.phase == 'assign': assign(work, state, args.variant, offline_valid_until=args.offline_valid_until, not_before=args.not_before)
    elif args.phase == 'inspect-app': inspect_app(work, state, args.app)
    elif args.phase == 'install': install(work, state, args.app)
    elif args.phase == 'launch': print(json.dumps(launch(work, state, check=args.check, action=args.action, variant=args.variant, wrong_key=args.wrong_key, report_route=args.report_route, status_route=args.status_route)))
    elif args.phase == 'read-report': read_report(work, state, variant=args.variant, path=args.report_file, checked=args.check, accepted_variant=args.accepted_variant, cold=args.cold, fallback=args.fallback)
    elif args.phase == 'begin-cleanup': begin_cleanup(work, state)
    elif args.phase == 'cleanup': print(json.dumps(cleanup(work, state)))


if __name__ == '__main__':
    main()

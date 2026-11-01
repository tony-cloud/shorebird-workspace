#!/usr/bin/env python3
"""Opt-in native Android V2 variant harness. The default plan performs no I/O.

Run only against explicit pre-staged tooling and an explicitly owned adb serial.
This never starts an emulator. It installs one fresh unique package once, uses
real CLI scope staging/finalization and authenticated server assignments, and
observes public installation/selection APIs plus real rendered Dart code/data.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import secrets
import shutil
import shlex
import zipfile

import android_native_cache_e2e as v1
import android_native_app as wiring_v1
import android_variant_app as wiring
import native_variant_e2e as shared

SCHEMA = shared.SCHEMA
CONTROL_SCHEMA = 'android-variant-control-v2'
ROOT, FIXTURE, FixtureError = v1.ROOT, v1.FIXTURE, v1.FixtureError


def build_command(cli, phase, release_version, arch):
    if phase not in shared.BUILD_ORDER or arch not in wiring.ARCHES:
        raise ValueError('Unsupported native variant build target')
    args = [str(cli), '--json']
    if phase == 'base':
        return args + ['release', 'android', '--stage-only', '--artifact=apk',
                       '--target-platform=' + wiring.ARCHES[arch][1]]
    variant = 'pro' if phase == 'pro_seed' else phase
    # Target-platform belongs to Flutter for patch builds, after the separator.
    return args + ['patch', 'android', '--release-version=' + release_version,
                   '--track=stable', '--variant=' + variant, '--',
                   '--target-platform=' + wiring.ARCHES[arch][1]]


def archive_native_entries(path, arch, *, aab=False):
    prefix = ('base/lib/' if aab else 'lib/') + wiring.ARCHES[arch][0] + '/'
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate packaged native archive entry')
        result = {name[len(prefix):]: v1.sha256(archive.read(name)) for name in names
                  if name.startswith(prefix) and name.endswith('.so') and '/' not in name[len(prefix):]}
    if not {'libapp.so', 'libflutter.so', 'libshorebird_e2e_fixture.so'} <= set(result):
        raise ValueError('Missing actual packaged AOT/engine/V2 JNI library')
    return result


def packaged_scope(path, app_id, release_version, arch, *, aab=False):
    prefix = 'base/assets/flutter_assets/' if aab else 'assets/flutter_assets/'
    expected_name = prefix + 'shorebird_release_scope_android_' + arch + '.json'
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        scopes = [name for name in names if name.startswith(prefix + 'shorebird_release_scope_')]
        if scopes != [expected_name]:
            raise ValueError('Expected one exact native scope in actual Android runtime assets')
        value = shared.strict_json(archive.read(expected_name))
    expected = {'app_id': app_id, 'release_version': release_version, 'platform': 'android',
                'arch': arch, 'base_artifact_hash': archive_native_entries(path, arch, aab=aab)['libapp.so'],
                'host_abi_hash': None, 'runtime_profile': 'native-aot'}
    if value != expected:
        raise ValueError('Packaged native scope differs from actual final AOT library')
    return value


def validate_scopes(response, expected, *, published):
    if not isinstance(response, dict) or set(response) != {'scopes'} or not isinstance(response['scopes'], list):
        raise ValueError('Malformed authenticated scope API response')
    records = [record for record in response['scopes'] if record.get('scope', {}).get('platform') == 'android']
    if (len(records) != 1 or records[0].get('scope') != expected or
            records[0].get('published') is not published or
            type(records[0].get('release_artifact_id')) is not int or records[0]['release_artifact_id'] <= 0):
        raise ValueError('Actual CLI scope publication differs from final packaged native identity')
    return records[0]


def validate_native(report, nonce, operation, *, pid=None, process_token=None):
    if (not isinstance(report, dict) or set(report) != {'schema', 'nonce', 'pid', 'process_token', 'operation', 'payload'} or
            report.get('schema') != CONTROL_SCHEMA or report.get('nonce') != nonce or
            type(report.get('pid')) is not int or report['pid'] <= 0 or
            not isinstance(report.get('process_token'), str) or not re.fullmatch('[a-f0-9]{32}', report['process_token']) or
            report.get('operation') != operation or not isinstance(report.get('payload'), dict) or
            (pid is not None and report['pid'] != pid) or
            (process_token is not None and report['process_token'] != process_token)):
        raise ValueError('Native V2 control/PID identity mismatch')
    if operation == 'ready' and report['payload'] != {'provider_registered': True}:
        raise ValueError('V2 key provider was not registered before Flutter')
    if operation == 'read-provider' and (set(report['payload']) != {'receipts'} or
            not isinstance(report['payload']['receipts'], list) or len(report['payload']['receipts']) > 256):
        raise ValueError('Malformed native V2 provider receipts')
    return report


def provider_identities(receipts, contexts, patch_material, secret, release_id):
    """Match actual callback evidence to real assignments and native deliveries."""
    expected = []
    for context in contexts:
        descriptor = context['selection']
        if descriptor['target']['kind'] != 'patch':
            continue
        scope = descriptor['scope']
        material = patch_material[descriptor['variant_id']]
        aad = shared.canonical_aad({'app_id': scope['app_id'], 'release_id': release_id,
            'patch_number': descriptor['target']['patch_number'], 'patch_hash': material['hash'],
            'transport_hash': material['transport_hash'], 'platform': 'android', 'arch': scope['arch'],
            'device_id_hash': scope['client_id_hash']})
        key_id, _ = shared.delivery_material(secret, aad)
        expected.append(shared.validate_context(key_id, aad, context, contexts))
    for receipt in receipts:
        if receipt not in expected:
            raise ValueError('Native callback released a key outside an authenticated assignment')
    return receipts


def validate_launch_state(report, latest, *, online, mode, previous_current=None):
    """Cold starts must adopt the exact latest grant, including reused content."""
    status = report['selection']
    if online:
        if previous_current is not None and shared.selection_identity(status['current']) != previous_current:
            raise ValueError('Online check rewrote or skipped the actual process-pinned grant')
    else:
        shared.validate_cold_activation(report, latest, fallback=mode != 'correct')
        expected = dict(variant_id=latest['variant_id'], license_type=latest['license_type'],
                        patch_number=latest['patch_number'], reason='selected_base' if latest['patch_number'] is None else 'ready')
        if status['next_boot'] != expected:
            raise ValueError('Cold selection lost its effective authorized cached target')
        if mode != 'correct' and status['restart_required'] is not True:
            raise ValueError('Unavailable key did not preserve pending authorized content')
    return report


def validate_launch_releases(receipts, latest, current, *, online, mode):
    """Historical grants authorize a pinned online process, never a later cold boot."""
    allowed = [shared.selection_identity(latest)]
    if online:
        allowed.append(shared.selection_identity(current))
    for receipt in receipts:
        if shared.selection_identity(receipt) not in allowed:
            raise ValueError('Native callback used a historical grant outside this launch')
    if not online:
        if latest['patch_number'] is None or mode == 'missing':
            if receipts:
                raise ValueError('BASE/missing-key launch unexpectedly released a historical paid key')
        elif not receipts:
            raise ValueError('Cold paid/wrong-key launch never called the V2 provider')
    return receipts


def preflight(args):
    if not re.fullmatch(r'[A-Za-z0-9_.:\-]{1,128}', args.serial or ''):
        raise FixtureError('An explicit owned adb serial is required')
    required = [args.cli / 'bin/shorebird', args.cli / 'bin/internal/flutter.version',
                args.cli / 'bin/internal/engine.version', args.flutter / 'bin/internal/engine.version',
                args.flutter / 'bin/flutter', args.tooling_dart, args.android_sdk / 'platform-tools/adb',
                args.java_home / 'bin/java', args.header, args.artifact_proxy, args.server_source / 'go.mod',
                args.updater_package / 'lib/src/selection_status.dart']
    if any(not value.is_file() for value in required) or any(not value.is_dir() for value in (
            args.artifact_mirror, args.pub_cache)):
        raise FixtureError('Explicit matching packaged V2 inputs are required')
    if 'shorebird_set_patch_decryption_key_provider_v2' not in args.header.read_text():
        raise FixtureError('Current public native V2 callback header is required')
    for executable in ('go', 'git'):
        if shutil.which(executable) is None:
            raise FixtureError('Required staged-source tool missing')
    v1.verify_toolchain_alignment(args)


class Lifecycle(v1.Lifecycle):
    def __init__(self, args):
        super().__init__(args)
        self.session, self.sentinel = secrets.token_hex(16), secrets.token_hex(24)
        self.authority = shared.KeyAuthority(self.secrets['master'].encode(), self.secrets['provision'], 'android', args.arch)
        self.release_version, self.previous_data = '1.0.0+1', None
        self.patch_material, self.variants = {}, None
        self.last_current = None
        self.package_absent_before_install = False
        self.manifest.update(schema=SCHEMA, platform='android', arch=args.arch,
            validation='real staged CLI release, original native patches, authenticated V2 assignments and public API receipts',
            key_persistence_scope='cold owned app processes; AndroidKeyStore hardware backing not claimed')

    def environment(self):
        env = super().environment()
        # An inherited explicit GOMODCACHE otherwise bypasses the private GOPATH.
        env['GOMODCACHE'] = str(self.work / 'go-path/pkg/mod')
        return env

    def prepare(self):
        self.work.mkdir(mode=0o700); self.work_owned = True
        for name in ('home', 'gradle'):
            (self.work / name).mkdir(mode=0o700)
        if (self.adb('get-state') != 'device' or
                self.adb('shell', 'getprop', 'ro.product.cpu.abi') != wiring.ARCHES[self.args.arch][0]):
            raise FixtureError('Actual explicitly owned Android device architecture differs from --arch')
        if not self.args.allow_physical_device and self.adb('shell', 'getprop', 'ro.kernel.qemu') != '1':
            raise FixtureError('Physical device requires explicit --allow-physical-device')
        level = self.adb('shell', 'getprop', 'ro.build.version.sdk')
        if not level.isdecimal() or int(level) < 24:
            raise FixtureError('Owned Android target requires API 24 or later')
        if self.adb('shell', 'pm', 'path', self.package, allow_failure=True):
            raise FixtureError('Refusing any pre-existing package')
        self.package_absent_before_install = True
        for source, name in ((self.args.cli, 'cli'), (self.args.flutter, 'flutter'),
                             (self.args.pub_cache, 'pub-cache'), (self.args.updater_package, 'updater-package')):
            shutil.copytree(source, self.work / name, symlinks=False)
        metadata = shared.clear_private_flutter_metadata(self.work / 'flutter', self.work)
        self.stage('private-flutter-metadata', metadata)
        self.snapshot(self.args.server_source, self.work / 'server-src')
        self.mirror = wiring_v1.ArtifactMirror(self.args.artifact_mirror)
        shutil.copy2(self.args.artifact_proxy, self.work / 'artifact-proxy'); self.start_proxy()
        revision = (self.work / 'cli/bin/internal/flutter.version').read_text().strip()
        vended = self.work / 'cli/bin/cache/flutter' / revision
        if vended.exists():
            shutil.rmtree(vended)
        shutil.copytree(self.work / 'flutter', vended)
        self.stage('private-cli-flutter-metadata', shared.clear_private_flutter_metadata(vended, self.work))
        self.cli = self.work / 'cli/bin/shorebird'
        self.run_command(['go', 'build', '-o', self.work / 'server', './cmd/server'], cwd=self.work / 'server-src', name='compile-server')
        shutil.copytree(FIXTURE, self.work / 'app', ignore=shutil.ignore_patterns(
            '.git', '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', '.cxx', '.externalNativeBuild',
            'windows', 'linux', 'macos', 'ios', 'tool'))
        pubspec = self.work / 'app/pubspec.yaml'
        source = re.sub(r'^version:.*\n?', '', pubspec.read_text(), flags=re.M) + '\nversion: ' + self.release_version + '\n'
        if len(re.findall(r'^dependencies:$', source, re.M)) != 1 or 'shorebird_code_push:' in source:
            raise FixtureError('Review changed opt-in fixture dependency wiring')
        pubspec.write_text(source.replace('dependencies:\n', 'dependencies:\n  shorebird_code_push:\n    path: ' + json.dumps(str(self.work / 'updater-package')) + '\n', 1))
        self.observer = v1.Observer(); self.reverse(self.observer.base); self.reverse(self.api_url)
        self.stage('isolated-packaged-v2-toolchain', {'native_callback_header_sha256': v1.file_hash(self.args.header),
            'cli_flutter_revision': revision, 'arch': self.args.arch})
        if self.args.source_record:
            self.manifest['packaged_sources'] = v1.source_record(self.args.source_record)

    def bootstrap(self):
        super().bootstrap()
        wiring.inject_app(self.work / 'app', self.package, v1.sha256(self.control.encode()), self.args.header,
            app_id=self.app_id, release_version=self.release_version, session=self.session, arch=self.args.arch)
        self.native_sources = v1.tree_hashes(self.work / 'app/android')

    def apply(self, phase):
        shared.apply_profile(self.work / 'app', self.session, self.sentinel, phase)
        variant = 'free' if phase == 'base' else 'pro' if phase == 'pro_seed' else phase
        (self.work / 'app/shorebird.yaml').write_text(f'app_id: {self.app_id}\nbase_url: {self.api_url}\nauto_update: false\n'
            f'selection_protocol: 2\naot_patch_app_build_id: {self.release_version}\naot_patch_license_type: {variant}\n'
            'patch_verification: strict\npatch_response_verification: required\nnative_patch_max_bytes: 268435456\n')

    def build_variant(self, phase):
        self.apply(phase)
        if phase != 'base':
            current = v1.tree_hashes(self.work / 'app/android')
            if any(current.get(name) != digest for name, digest in self.native_sources.items()
                   if not name.startswith(('.gradle/', 'local.properties'))):
                raise FixtureError('Frozen native fixture source changed')
        self.run_command(build_command(self.cli, phase, self.release_version, self.args.arch),
                         cwd=self.work / 'app', name='variant-' + phase)
        aab = self.work / 'app/build/app/outputs/bundle/release/app-release.aab'
        native = archive_native_entries(aab, self.args.arch, aab=True)
        if phase == 'base':
            self.apk = self.work / 'baseline.apk'
            shutil.copy2(self.work / 'app/build/app/outputs/flutter-apk/app-release.apk', self.apk)
            self.apk_hash = v1.file_hash(self.apk)
            self.native_entries = archive_native_entries(self.apk, self.args.arch)
            expected = packaged_scope(aab, self.app_id, self.release_version, self.args.arch, aab=True)
            if packaged_scope(self.apk, self.app_id, self.release_version, self.args.arch) != expected or native != self.native_entries:
                raise FixtureError('Installable APK differs from final AAB native baseline')
            rows = self.rows('SELECT id,version,selection_protocol FROM releases WHERE app_id=?', (self.app_id,))
            if len(rows) != 1 or rows[0][1:] != (self.release_version, 2):
                raise FixtureError('CLI did not create exactly one real protocol-2 release')
            self.release_id = rows[0][0]
            self.variants = shared.NativeVariantSession(self.app_id, self.release_id, self.release_version,
                'android', self.args.arch, self.session, self.request)
            path = self.variants.path('/selection-scopes')
            validate_scopes(self.request('GET', path), expected, published=False)
            self.stage('actual-cli-scopes-staged', expected)
            self.run_command([self.cli, '--json', 'release-finalize', '--platform=android',
                '--release-version=' + self.release_version, '--arch=' + self.args.arch], cwd=self.work / 'app', name='finalize')
            validate_scopes(self.request('GET', path), expected, published=True)
            self.stage('actual-cli-scopes-finalized', expected)
            wiring_v1.verify_apk_manifest(self.apk, self.package, self.args.android_sdk, java_home=self.args.java_home)
            self.installed = True; self.adb('install', '--no-streaming', str(self.apk)); self.assert_apk()
        else:
            if {k:v for k,v in native.items() if k != 'libapp.so'} != {k:v for k,v in self.native_entries.items() if k != 'libapp.so'}:
                raise FixtureError('Published patch changed frozen native engine/plugin/JNI libraries')
            rows = self.rows('SELECT p.number,a.hash,a.storage_key,a.hash_signature FROM patches p JOIN patch_artifacts a '
                'ON a.patch_id=p.id WHERE p.release_id=? AND a.platform=? AND a.arch=? ORDER BY p.number',
                (self.release_id, 'android', self.args.arch))
            number = {'pro_seed':1,'pro':2,'enterprise':3}[phase]
            if len(rows) != number or rows[-1][0] != number or rows[-1][1] != native['libapp.so'] or not rows[-1][3]:
                raise FixtureError('Real CLI native patch number/hash/signature differs from final AAB')
            if phase != 'pro_seed':
                self.patch_material[phase] = {'hash': rows[-1][1], 'transport_hash': v1.file_hash(self.work / 'objects/releases' / rows[-1][2])}
            self.stage('actual-cli-variant-' + phase, {'number': number, 'artifact_hash': rows[-1][1]})

    def control_command(self, operation, *, fresh=True, online=False, mode='correct', **fields):
        if fresh:
            self.stop_app()
        nonce = secrets.token_hex(24)
        extras = {'control':self.control,'operation':operation,'nonce':nonce,'observer':self.observer.base + '/native'}
        if operation == 'launch':
            extras.update(mode=mode,receipt_url=self.observer.base + '/receipt',release_id=str(self.release_id),
                contexts=json.dumps(self.variants.contexts,separators=(',',':')),check='1' if online else '0')
            if online:
                if self.api is None or self.provision is None:
                    raise FixtureError('Online launch requires owned API and authenticated provisioning')
                extras.update(provision_url=self.provision.url,provision_token=self.secrets['provision'])
            elif self.api is not None or self.provision is not None:
                raise FixtureError('Offline launch requires both owned services stopped')
        extras.update(fields)
        command = ['am','start','-W','-n',self.package + '/.ControlActivity']
        for name,value in extras.items():
            command += ['--es',name,value]
        self.adb('shell',' '.join(shlex.quote(value) for value in command))
        expected = 'ready' if operation == 'launch' else operation
        def receive():
            report = self.observer.get('/native',nonce)
            if report is None:
                return None
            if report.get('operation') == 'error':
                raise FixtureError('Native V2 control rejected its private operation')
            return validate_native(report,nonce,expected,pid=None if fresh else self.active['pid'],
                process_token=None if fresh else self.active['process_token'])
        report = v1.wait_for(receive,self.args.runtime_timeout,'native V2 ' + expected)
        if self.pid() != report['pid']:
            raise FixtureError('Native control differs from actual adb process')
        if fresh:
            if report['process_token'] in self.process_tokens:
                raise FixtureError('Cold native V2 launch reused process identity')
            self.process_tokens.add(report['process_token'])
        return nonce,report

    def launch_variant(self, label, variant, *, online=False, mode='correct', require_provider=False):
        self.assert_apk()
        nonce,ready = self.control_command('launch',online=online,mode=mode); self.active = ready
        def receive():
            report = self.observer.get('/receipt',nonce)
            if report is None:
                return None
            if self.pid() != ready['pid']:
                raise FixtureError('Public Dart receipt PID differs from actual installed app')
            accepted = None if online and self.variants.latest['epoch'] == '0' else self.variants.latest
            return shared.validate_report(report,nonce,ready['pid'],variant,self.previous_data,accepted)
        report = v1.wait_for(receive,self.args.runtime_timeout,label + ' public rendered V2 receipt')
        if report['stage'] != ('checked' if online else 'boot') or report['data'].get('session') != self.session or report['data'].get('sentinel') != self.sentinel:
            raise FixtureError('Receipt did not execute requested check or preserve configured app data')
        validate_launch_state(report, self.variants.latest, online=online, mode=mode, previous_current=self.last_current)
        self.variants.remember(report)
        self.last_current = shared.selection_identity(report['selection']['current'])
        self.previous_data = copy.deepcopy(report['data'])
        _,evidence = self.control_command('read-provider',fresh=False)
        receipts = provider_identities(evidence['payload']['receipts'],self.variants.contexts,self.patch_material,
                                       self.authority.secret,self.release_id)
        validate_launch_releases(receipts, self.variants.latest, self.last_current, online=online, mode=mode)
        if require_provider and not any(value['digest'] == self.variants.latest['digest'] for value in receipts):
            raise FixtureError('Actual V2 native callback did not authorize this cold cached grant')
        self.stage(label, {'variant':variant,'new_code_result':report['new_code_result'],'selection':report['selection'],
            'data':copy.deepcopy(report['data']),'public_installation_sha256':v1.sha256(report['installation_id'].encode()),
            'provider_receipts':receipts,'actual_pid_matched':True,'fresh_process':True,'mode':mode})
        return report

    def allow_assignment(self, result):
        descriptor = result['selection']
        if descriptor['target']['kind'] == 'base':
            return
        material = self.patch_material[descriptor['variant_id']]
        if descriptor['target']['artifact_hash'] != material['hash']:
            raise FixtureError('Assignment differs from the actual CLI-published native artifact')
        self.authority.allow({'app_id':self.app_id,'release_id':self.release_id,
            'patch_number':descriptor['target']['patch_number'],'patch_hash':material['hash'],
            'transport_hash':material['transport_hash'],'platform':'android','arch':self.args.arch,
            'device_id_hash':descriptor['scope']['client_id_hash']})

    def execute(self):
        self.prepare(); self.start_api(); self.bootstrap()
        for phase in shared.BUILD_ORDER:
            self.build_variant(phase)
        self.variants.map_free(); self.start_provision()
        # The first check may create the public free grant, so accept its actual
        # observed epoch rather than manufacturing a descriptor or client ID.
        self.launch_variant('initial-free-checked','free',online=True)
        self.stop_app(); self.go_offline()
        self.launch_variant('initial-free-cold','free')
        current = 'free'
        for index,variant in enumerate(shared.LIFECYCLE[1:],1):
            self.stop_app(); self.start_api(); self.start_provision()
            self.allow_assignment(self.variants.assign(variant))
            self.launch_variant(f'{index}-accept-{variant}',current,online=True)
            self.stop_app(); self.go_offline()
            self.launch_variant(f'{index}-cold-{variant}',variant,require_provider=variant != 'free')
            current = variant
            if index in (1,2):
                for mode in ('missing','wrong'):
                    self.launch_variant(f'{index}-{mode}-fallback','free',mode=mode)
                    self.launch_variant(f'{index}-{mode}-recovery',variant,require_provider=True)
        self.stop_app(); self.assert_apk()
        self.stage('baseline-package-preserved',{'sha256':self.apk_hash})
        self.manifest['provisioning'] = {'accepted':self.authority.accepted,'rejected':self.authority.rejected}
        self.manifest['result'] = 'passed'


    def cleanup(self):
        ledger = {'schema':'android-variant-cleanup-v2','serial':self.args.serial,'package':self.package,
            'package_absent_before_install':self.package_absent_before_install,'install_attempted':self.installed,
            'baseline_apk_sha256':getattr(self,'apk_hash',None),
            'app_removal':'pending' if self.installed else 'not-installed',
            'confirmed_owned_reverse_mappings':sorted(self.reverses),'pending_owned_reverse_mappings':sorted(self.reverse_pending),
            'actions':[],'retained_work':self.work_owned,'owned_work':str(self.work)}
        def save():
            self.manifest['cleanup_ledger'] = copy.deepcopy(ledger)
            temporary = self.args.results / ('cleanup-' + secrets.token_hex(12) + '.tmp')
            v1.private_write(temporary,json.dumps(ledger,indent=2))
            temporary.replace(self.args.results / 'cleanup-ledger.json')
        def attempt(name, action):
            try:
                action(); ledger['actions'].append({'action':name,'result':'completed'}); save(); return True
            except Exception as error:
                ledger['actions'].append({'action':name,'result':'uncertain','error_type':type(error).__name__}); save(); return False
        save()
        attempt('stop-owned-app', self.stop_app)
        device_clean = not self.installed
        if self.installed:
            attempt('uninstall-owned-package', lambda: self.adb('uninstall',self.package))
            # A sentinel proves that a remote shell actually ran. Empty stdout
            # from a disconnected adb command can never prove package absence.
            try:
                remote = self.adb('shell', 'listed=$(pm list packages ' + shlex.quote(self.package) +
                    ' 2>/dev/null); code=$?; if [ "$code" -ne 0 ]; then printf "owned-package-unknown"; '
                    'elif printf "%s\n" "$listed" | grep -Fqx ' + shlex.quote('package:' + self.package) +
                    '; then printf "owned-package-present"; else printf "owned-package-absent"; fi')
                if remote != 'owned-package-absent':
                    raise FixtureError('Owned package removal is not confirmed')
                ledger['app_removal'] = 'confirmed-absent'; self.installed = False; device_clean = True
            except Exception:
                ledger['app_removal'] = 'uncertain'; device_clean = False
            save()
        for mapping in tuple(self.reverses | self.reverse_pending):
            if not attempt('remove-owned-reverse-' + mapping, lambda value=mapping:self.remove_reverse(value)):
                device_clean = False
        ledger['confirmed_owned_reverse_mappings'] = sorted(self.reverses)
        ledger['pending_owned_reverse_mappings'] = sorted(self.reverse_pending)
        host_clean = attempt('stop-owned-host-processes', self.job.close)
        for name,service in (('provision',self.provision),('observer',self.observer),('mirror',self.mirror)):
            if service is not None:
                host_clean = attempt('close-owned-' + name, service.close) and host_clean
        for index,log in enumerate(self.logs):
            host_clean = attempt('close-private-log-' + str(index), log.close) and host_clean
        if self.work_owned and device_clean and host_clean:
            if attempt('remove-owned-private-work', lambda:v1.remove_owned_work(self.work)):
                self.work_owned = False
            else:
                host_clean = False
        ledger['retained_work'] = self.work_owned
        save()
        if not device_clean or not host_clean:
            raise FixtureError('Owned cleanup is uncertain; sanitized ledger and private work retained')


def parser():
    result = v1.parser()
    result.description = __doc__
    result.add_argument('--arch',choices=tuple(wiring.ARCHES),default='aarch64')
    result.add_argument('--allow-physical-device',action='store_true')
    result.add_argument('--updater-package',type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.phase == 'plan':
        print(json.dumps({'schema':SCHEMA,'platform':'android','read_only':True,'lifecycle':shared.LIFECYCLE,
            'build_order':shared.BUILD_ORDER,'architectures':list(wiring.ARCHES),
            'requires':['explicit matching pre-staged V2 CLI/Flutter/SDK/header/public Dart API package',
                        'explicit owned adb serial; no emulator startup','authenticated disposable server/provisioning'],
            'release':'actual CLI --stage-only then release-finalize and real scope API verification',
            'runtime_validation':'UNRUN until explicit run --execute','baseline_policy':'one fresh install; no updater state injection'},indent=2))
        return 0
    if not args.execute:
        raise SystemExit('run requires --execute')
    paths = ('work','results','cli','flutter','tooling_dart','server_source','artifact_mirror','artifact_proxy',
             'pub_cache','android_sdk','java_home','header','updater_package')
    for name in paths:
        value = getattr(args,name)
        if value is None:
            raise SystemExit('run requires all explicit staged input paths')
        setattr(args,name,value.resolve())
    if args.source_record:
        args.source_record = args.source_record.resolve()
    if (args.work.exists() or args.results.exists() or args.work == args.results or
            args.work in args.results.parents or args.results in args.work.parents):
        raise SystemExit('Use distinct new private work and result directories')
    if any(args.work == getattr(args,name) or args.work in getattr(args,name).parents or getattr(args,name) in args.work.parents
           for name in ('cli','flutter','server_source','artifact_mirror','pub_cache','android_sdk','java_home','updater_package')):
        raise SystemExit('Work must not overlap pre-staged inputs')
    args.results.mkdir(parents=True,mode=0o700)
    lifecycle = None
    manifest = {'schema':SCHEMA,'result':'failed','stages':[],'runtime_validation':'not completed'}
    try:
        preflight(args); lifecycle = Lifecycle(args); manifest = lifecycle.manifest; lifecycle.execute()
    except Exception as error:
        manifest['failure'] = {'type':type(error).__name__,'after_stage':manifest['stages'][-1]['name'] if manifest['stages'] else 'preflight'}
        print('Android V2 variant lifecycle failed; sanitized evidence retained.',flush=True)
    finally:
        try:
            if lifecycle:
                lifecycle.cleanup()
            manifest['cleanup'] = 'owned package, adb mappings, processes/services and private work removed' if lifecycle else 'no lifecycle resources created'
        except Exception as error:
            manifest['cleanup'] = 'uncertain; see cleanup-ledger.json and retained private work'; manifest['result'] = 'failed'
        v1.private_write(args.results / 'results.json',json.dumps(manifest,indent=2))
    return 0 if manifest['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

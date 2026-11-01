#!/usr/bin/env python3
"""Opt-in native v2 desktop orchestration; platform classes retain ownership."""
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time

import native_variant_e2e as protocol
import native_variant_adversarial as adversarial

TOOL = Path(__file__).resolve().parent


class NativeDesktopVariant:
    def __init__(self, args):
        super().__init__(args)
        self.session_id, self.sentinel = secrets.token_hex(16), secrets.token_hex(24)
        self.previous_data = None
        self.last_current_identity = None
        self.artifacts = {}
        self.protocol = None
        self.phase = 'base'
        self.manifest.update(schema=protocol.SCHEMA, validation='real native CLI/server/application-owned v2 provider lifecycle',
                             platform=self.target_os, runtime_acceptance=False)
        self.authority = protocol.KeyAuthority(self.secrets['master'].encode(), self.secrets['provision'],
                                               self.target_os, self.module.runtime_arch(args))

    def prepare(self):
        super().prepare()
        if not getattr(self,'_private_metadata_cleared',False):
            raise self.module.FixtureError('Private metadata cleanup hook did not precede Flutter generation')
        dependency = self.work / 'dependencies/shorebird_code_push'
        shutil.copytree(self.module.ROOT / 'updater/shorebird_code_push', dependency,
                        ignore=shutil.ignore_patterns('.git','.dart_tool','build','.packages'))
        pubspec = self.work / 'app/pubspec.yaml'
        text = pubspec.read_text()
        if '\ndependencies:\n' not in text or 'shorebird_code_push:' in text:
            raise self.module.FixtureError('Unexpected app dependency injection marker')
        pubspec.write_text(text.replace('\ndependencies:\n', '\ndependencies:\n  shorebird_code_push:\n    path: ../dependencies/shorebird_code_push\n', 1))
        runner = self.work / 'app' / self.target_os / 'runner'
        original = runner / 'runner_patch_key_provider.cpp'
        original.rename(runner / 'legacy_provider.cpp')
        shutil.copy2(TOOL / (self.target_os + '_variant_provider.cpp'), original)
        shutil.copy2(TOOL / 'native_variant_context.h', runner / 'native_variant_context.h')
        shutil.copy2(self.module.ROOT / 'flutter/engine/src/flutter/shell/common/shorebird/shorebird_patch_encryption.h',
                     runner / 'native_variant_api.h')
        self.native_sources = self.module.tree_hashes(self.work / 'app' / self.target_os)
        self.data_directory = self.work / 'application-data'
        self.data_directory.mkdir(mode=0o700)
        self.provider_receipt = self.work / 'provider-calls.jsonl'
        self.manifest['public_api_package'] = {'source': 'updater/shorebird_code_push',
            'head': self.output(['git','-C',self.module.ROOT / 'updater','rev-parse','HEAD']).decode().strip(),
            'files': self.module.tree_hashes(dependency)}

    def clear_private_metadata(self):
        flutter_ref = self.manifest['sources']['flutter']['head']
        self.manifest['private_flutter_metadata'] = {
            name: protocol.clear_private_flutter_metadata(path,self.work)
            for name,path in (('tooling',self.work / 'flutter'),
                ('vended',self.work / 'cli/bin/cache/flutter' / flutter_ref))}
        self._private_metadata_cleared = True

    def run_command(self,*args,**kwargs):
        if kwargs.get('name') in ('generate-linux','generate-windows'):
            self.clear_private_metadata()
        return super().run_command(*args,**kwargs)

    def bootstrap(self):
        super().bootstrap()
        self.base_policy = (f'app_id: {self.app_id}\nbase_url: {self.api_url}\nauto_update: false\n'
            'selection_protocol: 2\naot_patch_runtime_mode: native-aot\n'
            'aot_patch_app_build_id: 1.0.0+1\n'
            'patch_verification: strict\npatch_response_verification: required\nnative_patch_max_bytes: 268435456\n')
        (self.work / 'app/shorebird.yaml').write_text(self.base_policy)

    def build_variant(self, phase):
        if phase not in protocol.BUILD_ORDER:
            raise self.module.FixtureError('Unknown native build phase')
        patch = phase != 'base'
        variant = 'free' if not patch else 'pro' if phase == 'pro_seed' else phase
        current_sources = self.module.tree_hashes(self.work / 'app' / self.target_os)
        if any(current_sources.get(path) != value for path,value in self.native_sources.items()
               if not path.startswith(('flutter/ephemeral/', '.gradle/', 'local.properties'))):
            raise self.module.FixtureError('Native application provider sources changed')
        protocol.apply_profile(self.work / 'app', self.session_id, self.sentinel, phase)
        (self.work / 'app/shorebird.yaml').write_text(self.base_policy + (f'aot_patch_license_type: {variant}\n' if patch else ''))
        command = [self.cli,'--json',*self.module.local_engine_flags(self.args),
            'patch' if patch else 'release',self.target_os,'--dart-define=LICENSE_TYPE=' + variant]
        if patch:
            command += ['--release-version=' + self.release_version,'--track=stable','--variant=' + variant]
        else:
            command += ['--stage-only']
        self.run_command(command,cwd=self.work / 'app',env=self.environment(),name='variant-build-' + phase)
        build = self.module.bundle_directory(self.args,self.work,self.target_os)
        image = build / ('lib/libapp.so' if self.target_os == 'linux' else 'data/app.so')
        self.module.verify_binary(image,self.args)
        if not patch:
            releases = self.request('GET',f'/api/v1/apps/{self.app_id}/releases')['releases']
            if len(releases) != 1 or releases[0].get('selection_protocol') != 2:
                raise self.module.FixtureError('Expected one staged protocol-2 CLI release')
            self.release_id, self.release_version = releases[0]['id'], releases[0]['version']
            self.base_hash = self.module.file_hash(image)
            self.assert_scope(published=False)
            self.stage('actual-cli-staged-native-scope', {'base_artifact_hash':self.base_hash})
            self.run_command([self.cli,'--json','release-finalize','--platform=' + self.target_os,
                '--release-version=' + self.release_version,'--arch=' + self.module.runtime_arch(self.args)],
                cwd=self.work / 'app',env=self.environment(),name='variant-release-finalize')
            self.assert_scope(published=True)
            self.bundle = self.work / 'released'
            shutil.copytree(build,self.bundle)
            self.bundle_hashes = self.module.tree_hashes(self.bundle)
            self.runner = self.bundle / ('license_flavor_patch_test' + ('.exe' if self.target_os == 'windows' else ''))
            engine = self.bundle / ('lib/libflutter_linux_gtk.so' if self.target_os == 'linux' else 'flutter_windows.dll')
            self.module.verify_binary(self.runner,self.args)
            self.module.verify_binary(engine,self.args)
            expected_engine = self.args.engine_src / 'out' / self.args.host_engine / engine.name
            if self.module.file_hash(engine) != self.module.file_hash(expected_engine):
                raise self.module.FixtureError('Packaged engine differs from selected native engine')
            self.verify_policy()
            self.protocol = protocol.NativeVariantSession(self.app_id,self.release_id,self.release_version,
                self.target_os,self.module.runtime_arch(self.args),self.session_id,self.request)
            self.protocol.map_free()
            self.stage('actual-cli-release-free', {'version':self.release_version,'bundle':self.bundle_hashes})
        else:
            current = self.module.tree_hashes(build)
            native = {name:value for name,value in self.bundle_hashes.items()
                      if (name.startswith('lib/') or (self.target_os == 'windows' and (name.endswith('.dll') or name.endswith('.exe')))
                          or name == 'license_flavor_patch_test') and name not in ('lib/libapp.so','data/app.so')}
            if not native or any(current.get(name) != value for name,value in native.items()):
                raise self.module.FixtureError('Native Runner/engine/plugin changed between variants')
            rows = self.rows('SELECT p.id,p.number,a.hash,a.storage_key,a.arch,a.hash_signature FROM patches p '
                'JOIN patch_artifacts a ON a.patch_id=p.id WHERE p.release_id=? AND a.platform=? ORDER BY p.number',
                (self.release_id,self.target_os))
            expected = protocol.BUILD_ORDER.index(phase)
            if len(rows) != expected or rows[-1][1] != expected:
                raise self.module.FixtureError('CLI did not create the next real variant artifact')
            patch_id,number,image_hash,storage,arch,signature = rows[-1]
            if not signature or arch != self.module.runtime_arch(self.args) or image_hash != self.module.file_hash(image):
                raise self.module.FixtureError('Published signed variant differs from built native image')
            transport = self.work / 'objects/releases' / storage
            if not transport.is_file():
                raise self.module.FixtureError('Actual uploaded native patch transport missing')
            self.artifacts[variant] = {'number':number,'hash':image_hash,'transport_hash':self.module.file_hash(transport)}
            self.assert_scope(published=True)
            self.stage('actual-cli-patch-' + phase,dict(self.artifacts[variant],native_bytes_unchanged=True))

    def assert_scope(self,published):
        records = self.request('GET',f'/api/v1/apps/{self.app_id}/releases/{self.release_id}/selection-scopes')['scopes']
        records = [record for record in records if record['scope']['platform'] == self.target_os]
        if len(records) != 1:
            raise self.module.FixtureError('Native scope architecture set is not exact')
        record,scope = records[0],records[0]['scope']
        if (record.get('published') is not published or scope != {'app_id':self.app_id,
            'release_version':self.release_version,'platform':self.target_os,'arch':self.module.runtime_arch(self.args),
            'base_artifact_hash':self.base_hash,'host_abi_hash':None,'runtime_profile':'native-aot'}):
            raise self.module.FixtureError('Actual staged/published server scope differs from released native AOT')
        self.scope = scope

    def verify_policy(self):
        assets = self.bundle / 'data/flutter_assets'
        value = (assets / 'shorebird.yaml').read_text()
        def scalar(name):
            matches = re.findall('^' + re.escape(name) + r':\s*([^\r\n]+)$',value,re.M)
            if len(matches) != 1: raise self.module.FixtureError('Missing/ambiguous native policy scalar')
            return matches[0].strip().strip('"\'')
        for name,expected in (('app_id',self.app_id),('selection_protocol','2'),('aot_patch_runtime_mode','native-aot'),
            ('patch_verification','strict'),('patch_response_verification','required'),('native_patch_max_bytes','268435456'),
            ('aot_patch_app_build_id',self.release_version)):
            if scalar(name) != expected:
                raise self.module.FixtureError('Compiled native v2 runtime policy missing or ambiguous')
        try:
            if not base64.b64decode(scalar('patch_public_key'),validate=True): raise ValueError('Empty key')
        except ValueError:
            raise self.module.FixtureError('Missing managed verification key') from None
        asset = assets / f'shorebird_release_scope_{self.target_os}_{self.module.runtime_arch(self.args)}.json'
        if protocol.strict_json(asset.read_text()) != self.scope:
            raise self.module.FixtureError('Actual packaged native scope asset differs from published server scope')
        if 'SHOREBIRD_NATIVE_SELECTION_MANIFEST' in value or 'original_baseline_path' in value:
            raise self.module.FixtureError('Private native producer paths leaked into runtime policy')

    def runtime_environment(self, *, mode='correct'):
        if self.target_os == 'linux':
            return super().runtime_environment(mode=mode)
        if mode not in ('correct','missing','wrong'):
            raise self.module.FixtureError('Invalid native provider mode')
        # Windows V1 keeps this logic inside launch(), rather than exposing the
        # Linux runtime_environment hook. Preserve its actual DPAPI controls.
        env = dict(os.environ)
        for name in tuple(env):
            if name.startswith(('SHOREBIRD_','PATCH_DELIVERY_','PATCH_SIGNING_')):
                del env[name]
        for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env.pop(name,None)
        env.update(NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost',
            SHOREBIRD_E2E_KEY_CACHE_DIR=str(self.key_cache),SHOREBIRD_E2E_KEY_MODE=mode)
        return env

    def launch_variant(self,label,variant,*,check=False,online=False,mode='correct',cold=False,fallback=False,
                       fallback_reason=None,replay=False,automatic_free=False):
        if self.module.tree_hashes(self.bundle) != self.bundle_hashes:
            raise self.module.FixtureError('Released native application bundle changed')
        if cold and not online and (self.api is not None or self.provision is not None):
            raise self.module.FixtureError('Offline cold proof requires stopped owned services')
        if replay and (online or self.api is not None or self.provision is not None or not check):
            raise self.module.FixtureError('Replay check requires stopped real API and provisioning services')
        if fallback_reason is not None and (not fallback or not cold):
            raise self.module.FixtureError('Content/lease fallback requires a cold BASE proof')
        if automatic_free and (not check or not online or replay):
            raise self.module.FixtureError('Automatic signed free requires a real online check')
        accepted_before = dict(self.protocol.latest)
        prior_current = getattr(self,'last_current_identity',None)
        if check and prior_current is None:
            if variant != 'free':
                raise self.module.FixtureError('First checked launch must begin from unassigned bundled BASE')
            prior_current = {'epoch':'0','digest':None,'variant_id':'free','license_type':'free','patch_number':None}
        nonce = secrets.token_hex(24)
        env = self.runtime_environment(mode=mode)
        allowed = {(self.protocol.latest['epoch'],self.protocol.latest['digest'])}
        if check and getattr(self,'last_current_identity',None) is not None:
            current = self.last_current_identity
            allowed.add((current['epoch'],current['digest']))
        contexts = [context for context in self.protocol.contexts
            if (context['selection']['epoch'],context['selection_digest']) in allowed]
        env.update(SHOREBIRD_E2E_RECEIPT=nonce,SHOREBIRD_E2E_OBSERVER=self.observer.url,
            SHOREBIRD_VARIANT_APP_DATA_DIR=str(self.data_directory),
            SHOREBIRD_VARIANT_CONTEXTS=json.dumps(contexts,separators=(',',':')),
            SHOREBIRD_VARIANT_PROVIDER_RECEIPT=str(self.provider_receipt))
        if online:
            env.update(SHOREBIRD_E2E_PROVISION_URL=self.provision.url,SHOREBIRD_E2E_PROVISION_TOKEN=self.secrets['provision'])
        if check:
            if not online and not replay: raise self.module.FixtureError('Online check requires owned online services')
            env['SHOREBIRD_VARIANT_CHECK'] = '1'
        else:
            env.pop('SHOREBIRD_VARIANT_CHECK',None)
        log = (self.work / (label + '.private.log')).open('ab')
        self.logs.append(log)
        self.app = self.job.spawn([str(self.runner)],cwd=self.bundle,env=env,stdout=log,stderr=log)
        def observed():
            if self.app.poll() is not None: raise self.module.FixtureError('Native variant app exited before public status receipt')
            with self.receipt_lock: report = self.receipts.get(nonce)
            if report is None: return None
            expected = adversarial.validate_automatic_free(report,accepted_before) if automatic_free else self.protocol.latest
            protocol.validate_report(report,nonce,self.app.pid,variant,self.previous_data,expected)
            if report['stage'] != ('checked' if check else 'boot'):
                raise self.module.FixtureError('Wrong public check receipt stage')
            if report['data'].get('session') != self.session_id or report['data'].get('sentinel') != self.sentinel:
                raise self.module.FixtureError('App-owned session data differs from fixture')
            if check and protocol.selection_identity(report['selection']['current']) != prior_current:
                raise self.module.FixtureError('Online check rewrote the process-pinned current authorization')
            if check and report.get('check_error') is not None:
                raise self.module.FixtureError('Public native check/update failed')
            if replay:
                adversarial.validate_rejected_check(report,accepted_before)
            if cold:
                protocol.validate_cold_activation(report,self.protocol.latest,fallback=fallback)
                if fallback_reason is not None:
                    adversarial.validate_fallback(report,accepted_before,fallback_reason)
                elif fallback:
                    expected = self.protocol.latest
                    next_boot = {key:expected[key] for key in ('variant_id','license_type','patch_number')}
                    next_boot['reason'] = 'ready'
                    if (expected['patch_number'] is None or report['selection']['next_boot'] != next_boot or
                            report['selection']['restart_required'] is not True):
                        raise self.module.FixtureError('Retryable key refusal lost paid next-boot readiness/restart state')
            return report
        report = self.module.wait_for(observed,self.args.runtime_timeout,label + ' public API/rendered/data proof')
        self.protocol.remember(report)
        self.last_current_identity = protocol.selection_identity(report['selection']['current'])
        self.previous_data = report['data']
        callbacks = []
        if self.provider_receipt.exists():
            callbacks = [protocol.strict_json(line) for line in self.provider_receipt.read_text().splitlines() if line]
            callbacks = [row for row in callbacks if row.get('nonce') == nonce and row.get('pid') == self.app.pid]
        if cold and variant != 'free':
            expected = self.protocol.latest
            if not any(all(row.get(key) == value for key,value in expected.items()) for row in callbacks):
                raise self.module.FixtureError('No actual app-owned v2 provider callback for cold selected variant')
        if cold and variant == 'free' and (not fallback or fallback_reason is not None) and callbacks:
            raise self.module.FixtureError('Signed free selection attempted paid key release')
        self.stage(label, {'variant':variant,'selection':report['selection'],'data':report['data'],
                           'provider_callbacks':callbacks,'fresh_native_process':True,'rendered':report['rendered'],
                           'successful_provider_releases':len(callbacks),
                           'check_status':report.get('check_status'),'check_error':report.get('check_error'),
                           'real_api_online':online,'replay_listener':replay})
        return report

    def online(self):
        if self.api is None: self.start_api()
        if self.provision is None:
            self.provision = self.module.LocalService(self.authority.provision,'/key')
            self.provision_url = self.provision.url

    def authorize(self,variant,**lease):
        result = self.protocol.assign(variant,**lease)
        descriptor = result['selection']
        scope = {key:value for key,value in descriptor['scope'].items() if key != 'client_id_hash'}
        if scope != self.scope:
            raise self.module.FixtureError('Signed assignment scope differs from original published release')
        if variant != 'free':
            artifact = self.artifacts[variant]
            if descriptor['target'] != {'kind':'patch','patch_number':str(artifact['number']),
                'artifact_hash':artifact['hash'],'encryption':'transport-aes-256-gcm','artifact_key_id':None}:
                raise self.module.FixtureError('Signed assignment differs from actual CLI-produced native variant')
            self.authority.allow({'app_id':self.app_id,'release_id':self.release_id,'patch_number':artifact['number'],
                'patch_hash':artifact['hash'],'transport_hash':artifact['transport_hash'],'platform':self.target_os,
                'arch':self.module.runtime_arch(self.args),'device_id_hash':hashlib.sha256(self.protocol.installation_id.encode()).hexdigest()})
        elif descriptor['target'] != {'kind':'base','patch_number':None,'artifact_hash':None,'encryption':'none','artifact_key_id':None}:
            raise self.module.FixtureError('Signed free assignment does not select original bundled base')
        self.stage('authenticated-assignment-' + variant,{'selection':result['selection'],'selection_digest':result['selection_digest']})
        return result

    def ciphertext(self,variant):
        artifact = self.artifacts[variant]
        directory = self.cache / 'patches' / str(artifact['number'])
        state = protocol.strict_json((directory / 'state.json').read_text())
        path = directory / 'transport.aesgcm'
        if (state.get('kind') != 'InstalledEncryptedTransportV1' or not state.get('signature') or
            not path.is_file() or path.stat().st_size != state.get('ciphertext_size')):
            raise self.module.FixtureError('Real encrypted native variant was not installed')
        value = self.module.file_hash(path)
        if not any(self.module.file_hash(obj) == value for obj in (self.work / 'objects/patches').rglob('*') if obj.is_file()):
            raise self.module.FixtureError('Cached ciphertext differs from actual Go delivery')
        self.stage('ciphertext-installed-' + variant,{'patch_number':artifact['number'],'sha256':value,'signature_present':True})

    def audit_cache(self):
        images = {artifact['hash'] for artifact in self.artifacts.values()}
        forbidden = [self.secrets['master'].encode(),self.secrets['provision'].encode()]
        for aad in self.authority.allowed:
            _,key = protocol.delivery_material(self.authority.secret,aad)
            forbidden += [key,key.hex().encode()]
        for path in self.cache.rglob('*'):
            if path.is_symlink(): raise self.module.FixtureError('Unexpected updater cache link')
            if not path.is_file(): continue
            name = path.relative_to(self.cache).as_posix()
            if not (name in ('state.json','pointers.json') or
                re.fullmatch(r'patches/[1-9][0-9]*/(?:state.json|transport.aesgcm)',name) or
                re.fullmatch(r'selections-v2/[0-9a-f]{64}\.json',name)):
                raise self.module.FixtureError('Unknown native v2 cache file')
            if self.module.file_hash(path) in images:
                raise self.module.FixtureError('Plaintext native variant persisted')
        # Reuse the existing bounded key/magic scan without V1's filename-only
        # Linux/Android allowlist. The v2 journal namespace above is explicit.
        scanner = self.module.portable.scan_cache if self.target_os == 'linux' else self.module.scan_cache
        inventory = scanner(self.cache,self.work / 'dpapi' if self.target_os == 'windows' else self.cache / '.no-keys',
                            patch_hash=next(iter(images)),forbidden_secrets=forbidden)
        self.stage('native-v2-final-cache',inventory)

    def switch_variant(self,label,target,current,**lease):
        self.online(); self.authorize(target,**lease)
        report = self.launch_variant(label + '-check',current,check=True,online=True)
        if target != 'free': self.ciphertext(target)
        self.stop_app(); self.go_offline()
        self.launch_variant(label + '-cold',target,cold=True)
        self.stop_app()
        return report

    def patch_control(self,variant,action):
        if self.api is None or action not in ('rollback','rollforward'):
            raise self.module.FixtureError('Patch control requires an authenticated owned API')
        patches = self.request('GET',self.protocol.path('/patches'))['patches']
        matches = [row for row in patches if str(row['patch_number']) == protocol.EXPECTED_NUMBERS[variant]]
        if len(matches) != 1:
            raise self.module.FixtureError('Missing unique actual CLI patch for control')
        result = self.request('POST',self.protocol.path(f'/patches/{matches[0]["patch_id"]}/{action}'))
        self.stage(action + '-' + variant,{'patch_id':matches[0]['patch_id'],'response':result})

    def adversarial_cases(self):
        """Real cache/clock/server failures; no journal writes or cache resets."""
        self.switch_variant('adversarial-select-pro','pro','free')
        mutation = adversarial.mutate_cache(self.cache,self.artifacts['pro']['number'],'ciphertext')
        self.stage('owned-ciphertext-flip',mutation)
        before = adversarial.cache_snapshot(self.cache)
        self.launch_variant('ciphertext-offline-free','free',cold=True,fallback=True,fallback_reason='content_unavailable')
        self.stop_app()
        adversarial.assert_journal_preserved(before,adversarial.cache_snapshot(self.cache))
        # Explicit signed free and then the same immutable Pro content. The
        # updater must actually request/download it; restoring bytes is forbidden.
        self.switch_variant('ciphertext-signed-free','free','free')
        repaired = self.switch_variant('ciphertext-same-pro-redownload','pro','free')
        if repaired.get('check_status') != 'outdated' or self.module.file_hash(
                self.cache / mutation['path']) == mutation['after']['sha256']:
            raise self.module.FixtureError('Corrupted transport was not repaired by a real update/download')
        self.stage('ciphertext-real-redownload',{'patch_number':self.artifacts['pro']['number'],
            'sha256':self.module.file_hash(self.cache / mutation['path']),'check_status':repaired['check_status']})

        mutation = adversarial.mutate_cache(self.cache,self.artifacts['pro']['number'],'signature')
        self.stage('untrusted-cached-signature-flip',mutation)
        before = adversarial.cache_snapshot(self.cache)
        self.launch_variant('cached-signature-still-valid-pro','pro',cold=True)
        self.stop_app()
        after = adversarial.cache_snapshot(self.cache)
        adversarial.assert_journal_preserved(before,after)
        transport = f'patches/{self.artifacts["pro"]["number"]}/transport.aesgcm'
        if before['files'][transport] != after['files'].get(transport):
            raise self.module.FixtureError('Untrusted cached signature changed valid transport')

        self.online()
        old_response,old_selection,capture = adversarial.capture_response(self.request,self.protocol,self.scope)
        self.stage('server-returned-old-response-capture',adversarial.capture_summary(old_response,old_selection,capture))
        deadline = int(time.time()) + getattr(self.args,'lease_seconds',90)
        self.switch_variant('actual-leased-pro','pro','pro',offline_valid_until=str(deadline))
        if time.time() >= deadline:
            raise self.module.FixtureError('Paid lease expired before valid offline proof')
        before = adversarial.cache_snapshot(self.cache)
        waited = adversarial.wait_past_deadline(deadline)
        started = time.time()
        self.launch_variant('platform-clock-expired-free','free',cold=True,fallback=True,fallback_reason='expired')
        self.stop_app()
        if started <= deadline or waited <= deadline:
            raise self.module.FixtureError('Expiry proof did not follow actual platform wall-clock deadline')
        adversarial.assert_journal_preserved(before,adversarial.cache_snapshot(self.cache))
        self.stage('platform-lease-expiry',{'deadline':deadline,'wait_completed':waited,'launch_started':started,
            'accepted':dict(self.protocol.latest),'clock':'native platform time.time; no clock override'})

        for label,body in (('old-signed-response',old_response),
                           ('corrupt-stale-response-combined',adversarial.corrupt_response_signature(old_response))):
            if self.api is not None or self.provision is not None:
                raise self.module.FixtureError('Replay may not overlap real online services')
            before = adversarial.cache_snapshot(self.cache)
            requests = []
            with adversarial.replay_endpoint(self.api_url,body,requests):
                self.launch_variant(label + '-rejected','free',check=True,replay=True,
                    cold=True,fallback=True,fallback_reason='expired')
                self.stop_app()
            accepted = self.protocol.latest
            if (not requests or int(old_selection['epoch']) >= int(accepted['epoch']) or
                any(row.get('accepted_selection_epoch') != accepted['epoch'] or
                    row.get('accepted_selection_digest') != accepted['digest'] or
                    row.get('nonce') == capture['request']['nonce'] for row in requests)):
                raise self.module.FixtureError('Replay did not exercise fresh requests against newer accepted authority')
            adversarial.assert_journal_preserved(before,adversarial.cache_snapshot(self.cache))
            self.stage(label + '-requests',{'requests':adversarial.replay_requests_summary(requests),
                'delivered_check_responses':len(requests),'real_api_and_provisioning_stopped':True,
                'limitation':capture['limitation'],'journals':before['journals']})

        self.online()
        self.launch_variant('expired-server-signed-free','free',check=True,online=True,automatic_free=True)
        self.stop_app(); self.go_offline()
        self.launch_variant('expired-server-free-cold','free',cold=True); self.stop_app()
        self.switch_variant('before-enterprise-rollback','enterprise','free')
        self.online(); self.patch_control('enterprise','rollback')
        self.launch_variant('rollback-server-signed-free','enterprise',check=True,online=True,automatic_free=True)
        self.stop_app(); self.go_offline()
        self.launch_variant('rollback-cold-free','free',cold=True); self.stop_app()
        self.online(); self.patch_control('enterprise','rollforward')
        self.launch_variant('rollforward-alone-still-free','free',check=True,online=True)
        self.stop_app(); self.go_offline()
        self.launch_variant('rollforward-cold-free','free',cold=True); self.stop_app()
        self.switch_variant('explicit-enterprise-restored','enterprise','free')
        self.switch_variant('adversarial-final-signed-free','free','enterprise')
        self.launch_variant('adversarial-final-offline-free','free',cold=True); self.stop_app()

    def execute(self):
        self.prepare(); self.start_api(); self.bootstrap(); self.make_observer(); self.online()
        self.build_variant('base')
        self.launch_variant('initial-free-public-installation','free')
        self.stop_app()
        for phase in protocol.BUILD_ORDER[1:]: self.build_variant(phase)
        current = 'free'
        for index,variant in enumerate(protocol.LIFECYCLE[1:]):
            self.online(); self.authorize(variant)
            self.launch_variant(f'online-{index}-{current}-accept-{variant}',current,check=True,online=True)
            if variant != 'free': self.ciphertext(variant)
            self.stop_app(); self.go_offline()
            self.launch_variant(f'offline-cold-{index}-{variant}',variant,cold=True)
            self.stop_app(); current = variant
            if index == 0:
                # Same content, strictly higher authorization epoch, real restart.
                self.online(); self.authorize('pro')
                report = self.launch_variant('same-content-higher-epoch','pro',check=True,online=True)
                if report['selection']['current']['patch_number'] != report['selection']['accepted']['patch_number'] or report['selection']['restart_required'] is not True:
                    raise self.module.FixtureError('Same-content authorization epoch did not require restart')
                self.stop_app(); self.go_offline()
                self.launch_variant('same-content-cold-adoption','pro',cold=True); self.stop_app()
                for mode in ('missing','wrong'):
                    self.launch_variant('offline-' + mode + '-key-free','free',mode=mode,cold=True,fallback=True)
                    self.stop_app()
                    self.launch_variant('offline-' + mode + '-key-recovery-pro','pro',cold=True)
                    self.stop_app()
        self.adversarial_cases()
        self.manifest['adversarial_cases'] = list(adversarial.CASES)
        if self.module.tree_hashes(self.bundle) != self.bundle_hashes:
            raise self.module.FixtureError('Released bundle changed during variant transitions')
        # Public journal/ciphertext inventories are evidence only; never seed or
        # rewrite updater state to simulate an assignment or process selection.
        self.stage('released-bundle-unchanged',self.bundle_hashes)
        self.audit_cache()
        self.manifest.update(result='passed',runtime_acceptance=True,
            provisioning={'accepted':self.authority.accepted,'rejected':self.authority.rejected},
            final_application_data=self.previous_data)


def run_driver(module, lifecycle_type, argv=None):
    parser = module.parser()
    parser.add_argument('--lease-seconds',type=int,default=90)
    args = parser.parse_args(argv)
    if not 30 <= getattr(args,'lease_seconds',90) <= 3600:
        raise SystemExit('Use a lease duration between 30 and 3600 seconds')
    if args.phase == 'plan':
        print(json.dumps({'schema':protocol.SCHEMA,'runtime_acceptance':False,'platform':lifecycle_type.target_os,
            'build_order':protocol.BUILD_ORDER,'cold_lifecycle':protocol.LIFECYCLE,'arch':args.arch,
            'adversarial_cases':adversarial.CASES,'lease_seconds':getattr(args,'lease_seconds',90),
            'replay_limitation':'Independent capture nonce and stale epoch; combined response rejection, not isolated epoch or signature verification'}))
        return 0
    if not args.execute: raise SystemExit('Native v2 run requires --execute')
    if args.work is None or args.results is None: raise SystemExit('Use new --work and --results directories')
    for name in ('work','results','engine_src','tooling_dart','patch_tool','provider_probe','pub_cache'):
        value = getattr(args,name,None)
        if value is not None: setattr(args,name,value.resolve())
    if args.work.exists() or args.results.exists() or args.work == args.results or args.work in args.results.parents or args.results in args.work.parents:
        raise SystemExit('Owned native session/results roots must be separate and absent')
    module.architecture.require_native_host(lifecycle_type.target_os,args.arch)
    args.results.mkdir(mode=0o700,parents=True)
    lifecycle = None
    manifest = {'schema':protocol.SCHEMA,'result':'failed','runtime_acceptance':False,'stages':[]}
    try:
        machines = module.preflight(args)
        lifecycle = lifecycle_type(args)
        manifest = lifecycle.manifest
        manifest.update(target_arch=args.arch,runtime_arch=module.runtime_arch(args),binary_architectures=machines)
        lifecycle.execute()
    except Exception as error:
        manifest['failure'] = {'type':type(error).__name__,'after_stage':manifest['stages'][-1]['name'] if manifest['stages'] else 'preparation'}
    finally:
        try:
            if lifecycle: lifecycle.cleanup()
            manifest['cleanup'] = 'owned platform processes, services, private work, app cache and provider keys removed'
        except Exception as error:
            manifest.update(result='failed',runtime_acceptance=False,cleanup='failed: ' + type(error).__name__)
        module.private_write(args.results / 'results.json',json.dumps(manifest,indent=2))
    return 0 if manifest.get('result') == 'passed' else 1

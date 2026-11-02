"""Pure V2 Android orchestration, package binding and native receipt contracts."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import android_variant_e2e as e2e
import native_variant_e2e as shared
from test_android_variant_app import APP_ID, fixture_context


def aad_context():
    return {'app_id': APP_ID, 'release_id': 17, 'patch_number': '2', 'patch_hash': 'a' * 64,
            'transport_hash': 'b' * 64, 'platform': 'android', 'arch': 'aarch64',
            'device_id_hash': 'c' * 64}


class CommandsTest(unittest.TestCase):
    def test_plan_is_read_only_and_preserves_shared_lifecycle_contract(self):
        with patch.object(e2e, 'Lifecycle', side_effect=AssertionError('Must not create resources')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(e2e.main(['plan']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['schema'], shared.SCHEMA)
        self.assertEqual(result['lifecycle'], list(shared.LIFECYCLE))
        self.assertEqual(result['build_order'], list(shared.BUILD_ORDER))
        self.assertEqual(result['adversarial_cases'],list(e2e.adversarial.CASES))
        self.assertTrue(result['read_only'])
        self.assertIn('UNRUN', result['runtime_validation'])
        with self.assertRaises(SystemExit):
            e2e.main(['run'])

    def test_release_stages_final_aab_and_patch_uses_original_version_variant_and_flutter_arch_flag(self):
        for arch, (_, target) in e2e.wiring.ARCHES.items():
            release = e2e.build_command('/staged/shorebird', 'base', '1.0.0+1', arch)
            self.assertIn('--stage-only', release)
            self.assertIn('--artifact=apk', release)
            self.assertIn('--target-platform=' + target, release)
            for phase in ('pro_seed', 'pro', 'enterprise'):
                command = e2e.build_command('/staged/shorebird', phase, '1.0.0+1', arch)
                self.assertIn('--release-version=1.0.0+1', command)
                self.assertIn('--variant=' + ('pro' if phase == 'pro_seed' else phase), command)
                self.assertGreater(command.index('--target-platform=' + target), command.index('--'))
                self.assertFalse(any('flavor' in value for value in command))
                self.assertNotIn('--stage-only', command)
        with self.assertRaises(ValueError):
            e2e.build_command('/staged/shorebird', 'enterprise', '1', 'arm64')


class PackageTest(unittest.TestCase):
    def package(self, directory, arch, aab=False, scope_arch=None, hash_override=None):
        prefix = 'base/assets/flutter_assets/' if aab else 'assets/flutter_assets/'
        libs = ('base/lib/' if aab else 'lib/') + e2e.wiring.ARCHES[arch][0] + '/'
        library = b'final stripped libapp AOT image'
        scope = {'app_id': APP_ID, 'release_version': '1.0.0+1', 'platform': 'android',
                 'arch': arch, 'base_artifact_hash': hash_override or hashlib.sha256(library).hexdigest(),
                 'host_abi_hash': None, 'runtime_profile': 'native-aot'}
        package = Path(directory) / ('release.aab' if aab else 'release.apk')
        with zipfile.ZipFile(package, 'w') as archive:
            for name in ('libapp.so', 'libflutter.so', 'libshorebird_e2e_fixture.so'):
                archive.writestr(libs + name, library if name == 'libapp.so' else name.encode())
            archive.writestr(prefix + 'shorebird_release_scope_android_' + (scope_arch or arch) + '.json', json.dumps(scope))
        return package, scope

    def test_installed_apk_and_final_aab_bind_same_exact_arch_and_baseline(self):
        for arch in e2e.wiring.ARCHES:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as directory:
                apk, scope = self.package(directory, arch)
                aab, _ = self.package(directory, arch, True)
                self.assertEqual(e2e.packaged_scope(apk, APP_ID, '1.0.0+1', arch), scope)
                self.assertEqual(e2e.packaged_scope(aab, APP_ID, '1.0.0+1', arch, aab=True), scope)
                self.assertEqual(e2e.archive_native_entries(apk, arch), e2e.archive_native_entries(aab, arch, aab=True))
                for app_id, version in ((APP_ID, 'another-version'), ('22222222-2222-4222-8222-222222222222', '1.0.0+1')):
                    with self.assertRaises(ValueError):
                        e2e.packaged_scope(aab, app_id, version, arch, aab=True)

    def test_wrong_scope_filename_changed_final_library_hash_and_missing_native_parts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for values in ({'scope_arch': 'x86_64'}, {'hash_override': 'f' * 64}):
                apk, _ = self.package(directory, 'aarch64', **values)
                with self.assertRaises(ValueError):
                    e2e.packaged_scope(apk, APP_ID, '1.0.0+1', 'aarch64')
            apk, _ = self.package(directory, 'aarch64')
            with self.assertRaises(ValueError):
                e2e.archive_native_entries(apk, 'x86_64')

    def test_actual_scope_registration_must_transition_from_draft_to_exact_published_arch(self):
        expected = fixture_context()['selection']['scope'].copy()
        expected.pop('client_id_hash')
        record = {'scope': expected, 'release_artifact_id': 7, 'supplement_artifact_id': None, 'published': False}
        response = {'scopes': [record]}
        self.assertIs(e2e.validate_scopes(response, expected, published=False), record)
        with self.assertRaises(ValueError):
            e2e.validate_scopes(response, expected, published=True)
        record['published'] = True
        self.assertIs(e2e.validate_scopes(response, expected, published=True), record)
        for changed in ({'release_artifact_id': True}, {'published': 1}, {'scope': dict(expected, arch='x86_64')}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                e2e.validate_scopes({'scopes': [dict(record, **changed)]}, expected, published=True)
        with self.assertRaises(ValueError):
            e2e.validate_scopes({'scopes': [record, dict(record, scope=dict(expected, arch='arm'))]}, expected, published=True)


class ReceiptTest(unittest.TestCase):
    def test_actual_callback_identity_is_bound_to_authenticated_context_and_transport_kdf(self):
        context = fixture_context()
        aad = shared.canonical_aad(aad_context())
        key_id, _ = shared.delivery_material(b'fixture-secret', aad)
        identity = shared.validate_context(key_id, aad, context, [context])
        material = {'pro': {'hash': 'a' * 64, 'transport_hash': 'b' * 64}}
        self.assertEqual(e2e.provider_identities([identity], [context], material, b'fixture-secret', 17), [identity])
        for name, value in (('epoch', '1'), ('digest', 'f' * 64), ('variant_id', 'enterprise'),
                            ('license_type', 'free'), ('patch_number', '3'), ('key_id', 'AAAAAAAAAAAAAAAA')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                e2e.provider_identities([dict(identity, **{name: value})], [context], material, b'fixture-secret', 17)
        with self.assertRaises(ValueError):
            e2e.provider_identities([dict(identity, unknown=True)], [context], material, b'fixture-secret', 17)

    def test_native_control_requires_exact_fresh_nonce_pid_and_process_token(self):
        report = {'schema': e2e.CONTROL_SCHEMA, 'nonce': 'a' * 48, 'pid': 42,
                  'process_token': 'b' * 32, 'operation': 'ready', 'payload': {'provider_registered': True}}
        self.assertIs(e2e.validate_native(report, 'a' * 48, 'ready', pid=42, process_token='b' * 32), report)
        for name, value in (('schema', e2e.v1.CONTROL_SCHEMA), ('nonce', 'stale'), ('pid', True),
                            ('pid', 43), ('process_token', 'c' * 32), ('payload', {'provider_registered': False})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                e2e.validate_native(dict(report, **{name: value}), 'a' * 48, 'ready', pid=42, process_token='b' * 32)
        providers = dict(report, operation='read-provider', payload={'receipts': []})
        self.assertIs(e2e.validate_native(providers, 'a' * 48, 'read-provider'), providers)
        with self.assertRaises(ValueError):
            e2e.validate_native(dict(providers, payload={'receipts': 'forged'}), 'a' * 48, 'read-provider')


class ColdOracleTest(unittest.TestCase):
    def identity(self, epoch='4', variant='pro'):
        return dict(epoch=epoch,digest='d' * 64,variant_id=variant,license_type=variant,
                    patch_number=shared.EXPECTED_NUMBERS[variant])

    def report(self, identity, *, current=None, variant=None, restart=False):
        return {'variant':variant or identity['variant_id'], 'selection':{
            'accepted':dict(identity),'current':dict(current or identity),'restart_required':restart,
            'next_boot':dict(variant_id=identity['variant_id'],license_type=identity['license_type'],
                             patch_number=identity['patch_number'],reason='selected_base' if identity['variant_id']=='free' else 'ready')}}

    def test_cold_same_content_cannot_retain_old_grant_and_free_requires_latest_epoch(self):
        for variant in ('pro','free'):
            latest=self.identity(variant=variant)
            good=self.report(latest)
            self.assertIs(e2e.validate_launch_state(good,latest,online=False,mode='correct'),good)
            stale=self.report(latest,current=self.identity(epoch='2',variant=variant),restart=True)
            with self.assertRaises(ValueError):
                e2e.validate_launch_state(stale,latest,online=False,mode='correct')

    def test_unavailable_key_base_fallback_preserves_grant_and_ready_cached_target(self):
        latest=self.identity()
        fallback=dict(latest,variant_id='free',license_type='free',patch_number=None)
        report=self.report(latest,current=fallback,variant='free',restart=True)
        self.assertIs(e2e.validate_launch_state(report,latest,online=False,mode='missing'),report)
        for current in (self.identity(epoch='0',variant='free'), self.identity(epoch='2',variant='free')):
            with self.assertRaises(ValueError):
                e2e.validate_launch_state(self.report(latest,current=current,variant='free',restart=True),latest,online=False,mode='wrong')
        changed=self.report(latest,current=fallback,variant='free',restart=True)
        changed['selection']['next_boot']['reason']='quarantined'
        with self.assertRaises(ValueError):
            e2e.validate_launch_state(changed,latest,online=False,mode='missing')

    def test_cold_callback_releases_cannot_use_old_grant_or_any_paid_key_under_base(self):
        latest=self.identity()
        receipt=dict(latest,key_id='AbCdEfGhIjKlMnOp')
        self.assertEqual(e2e.validate_launch_releases([receipt],latest,latest,online=False,mode='correct'),[receipt])
        for receipts in ([dict(receipt,epoch='2')], [receipt,dict(receipt,epoch='2')], []):
            with self.assertRaises(ValueError):
                e2e.validate_launch_releases(receipts,latest,latest,online=False,mode='correct')
        with self.assertRaises(ValueError):
            e2e.validate_launch_releases([receipt],self.identity(variant='free'),self.identity(variant='free'),online=False,mode='correct')
        with self.assertRaises(ValueError):
            e2e.validate_launch_releases([receipt],latest,latest,online=False,mode='missing')
        self.assertEqual(e2e.validate_launch_releases([],latest,latest,online=False,mode='missing'),[])
        previous=self.identity(epoch='2')
        self.assertEqual(e2e.validate_launch_releases([receipt,dict(previous,key_id=receipt['key_id'])],latest,previous,online=True,mode='correct'),
                         [receipt,dict(previous,key_id=receipt['key_id'])])


class HarnessFlowTest(unittest.TestCase):
    def test_actual_build_entry_writes_exact_build_id_before_cli_stage(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory); (work/'app/lib').mkdir(parents=True)
            lifecycle=object.__new__(e2e.Lifecycle)
            lifecycle.work=work; lifecycle.args=SimpleNamespace(arch='aarch64')
            lifecycle.cli=Path('/explicit/shorebird'); lifecycle.app_id=APP_ID; lifecycle.api_url='http://127.0.0.1:1'
            lifecycle.release_version='1.0.0+1'; lifecycle.session='c'*32; lifecycle.sentinel='e'*48
            class StopBeforeBuild(Exception): pass
            def command(arguments,**kwargs):
                policy=(work/'app/shorebird.yaml').read_text()
                self.assertEqual(policy.count('aot_patch_app_build_id: 1.0.0+1\n'),1)
                self.assertIn('selection_protocol: 2\n',policy)
                self.assertIn('--stage-only',arguments)
                self.assertIn('native-variant-e2e-v2',(work/'app/lib/main.dart').read_text())
                raise StopBeforeBuild()
            lifecycle.run_command=command
            with self.assertRaises(StopBeforeBuild): lifecycle.build_variant('base')

    def test_actual_go_child_environment_uses_private_module_cache_and_preserves_goflags(self):
        import os
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory); captured={}
            lifecycle=object.__new__(e2e.Lifecycle)
            lifecycle.work=work; lifecycle.args=SimpleNamespace(java_home=work/'jdk',android_sdk=work/'sdk',build_timeout=10)
            lifecycle.api_url='http://127.0.0.1:1'; lifecycle.mirror=None; lifecycle.token=None
            lifecycle.manifest={}
            class Child:
                def wait(self,timeout): return 0
            class Job:
                def spawn(self,command,**kwargs): captured.update(kwargs); return Child()
                def stop(self,child): pass
            lifecycle.job=Job()
            with patch.dict(os.environ,{'GOMODCACHE':'/unrelated/modules','GOFLAGS':'-trimpath'}):
                lifecycle.run_command(['go','build','-o',work/'server','./cmd/server'],cwd=work,name='compile-server')
            self.assertEqual(captured['env']['GOMODCACHE'],str(work/'go-path/pkg/mod'))
            self.assertEqual(captured['env']['GOFLAGS'],'-trimpath')

    def test_private_metadata_purge_precedes_first_command_and_covers_both_flutter_copies(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); inputs=root/'inputs'; inputs.mkdir()
            for name in ('cli','flutter','pub-cache','updater-package','server-src','mirror'):
                (inputs/name).mkdir()
            (inputs/'cli/bin/internal').mkdir(parents=True)
            (inputs/'cli/bin/internal/flutter.version').write_text('a'*40)
            (inputs/'cli/bin/shorebird').write_text('staged CLI')
            (inputs/'flutter/bin/cache').mkdir(parents=True)
            (inputs/'flutter/bin/internal').mkdir()
            (inputs/'flutter/bin/internal/engine.version').write_text('b'*40)
            (inputs/'flutter/bin/cache/engine.stamp').write_text('b'*40)
            for name in ('engine_stamp.json','flutter.version.json'):
                (inputs/'flutter/bin/cache'/name).write_text('{"stale":"metadata"}')
            (inputs/'proxy').write_text('staged proxy')
            (inputs/'public-header.h').write_text('staged public v2 provider header')
            fixture=root/'fixture'; (fixture/'lib').mkdir(parents=True); (fixture/'android').mkdir()
            (fixture/'pubspec.yaml').write_text('name: example\ndependencies:\n  flutter:\n    sdk: flutter\n')
            work=root/'owned'; lifecycle=object.__new__(e2e.Lifecycle)
            lifecycle.work=work; lifecycle.work_owned=False; lifecycle.package='dev.shorebird.e2e.fixture'
            lifecycle.args=SimpleNamespace(arch='aarch64',allow_physical_device=False,cli=inputs/'cli',flutter=inputs/'flutter',
                pub_cache=inputs/'pub-cache',updater_package=inputs/'updater-package',server_source=inputs/'server-src',
                artifact_mirror=inputs/'mirror',artifact_proxy=inputs/'proxy',source_record=None,header=inputs/'public-header.h')
            lifecycle.release_version='1.0.0+1'; lifecycle.api_url='http://127.0.0.1:1'; lifecycle.manifest={'stages':[]}
            lifecycle.adb=lambda *args,**kwargs: {('get-state',):'device',('shell','getprop','ro.product.cpu.abi'):'arm64-v8a',
                ('shell','getprop','ro.kernel.qemu'):'1',('shell','getprop','ro.build.version.sdk'):'35'}.get(args,'')
            lifecycle.snapshot=lambda source,destination: destination.mkdir()
            lifecycle.start_proxy=lambda: None; lifecycle.reverse=lambda _: None
            def command(*args,**kwargs):
                for sdk in (work/'flutter',work/'cli/bin/cache/flutter'/('a'*40)):
                    self.assertFalse((sdk/'bin/cache/engine_stamp.json').exists())
                    self.assertFalse((sdk/'bin/cache/flutter.version.json').exists())
                    self.assertEqual((sdk/'bin/cache/engine.stamp').read_text(),'b'*40)
            lifecycle.run_command=command
            with patch.object(e2e,'FIXTURE',fixture),patch.object(e2e.wiring_v1,'ArtifactMirror',return_value=SimpleNamespace(url='http://127.0.0.1:2')), \
                    patch.object(e2e.v1,'Observer',return_value=SimpleNamespace(base='http://127.0.0.1:3')):
                lifecycle.prepare()
            self.assertTrue(lifecycle.package_absent_before_install)
            self.assertTrue((inputs/'flutter/bin/cache/engine_stamp.json').exists())


class CleanupTest(unittest.TestCase):
    def lifecycle(self,root,remote):
        from types import SimpleNamespace
        work=Path(root)/'owned'; work.mkdir(); (work/'baseline.apk').write_bytes(b'preserved APK')
        results=Path(root)/'results'; results.mkdir()
        lifecycle=object.__new__(e2e.Lifecycle)
        lifecycle.args=SimpleNamespace(serial='emulator-5554',results=results)
        lifecycle.work=work; lifecycle.work_owned=True; lifecycle.installed=True
        lifecycle.package='dev.shorebird.e2e.cleanup'; lifecycle.package_absent_before_install=True
        lifecycle.apk_hash=hashlib.sha256(b'preserved APK').hexdigest(); lifecycle.manifest={}
        lifecycle.reverses={'tcp:12345'}; lifecycle.reverse_pending=set()
        lifecycle.stop_app=lambda: None
        def adb(*arguments):
            if arguments[0]=='uninstall': raise OSError('uncertain adb response')
            self.assertIn('pm list packages',arguments[1]); self.assertIn('code=$?',arguments[1])
            self.assertIn('owned-package-unknown',arguments[1]); self.assertNotIn('pm path',arguments[1])
            return remote
        lifecycle.adb=adb
        lifecycle.remove_reverse=lambda mapping:lifecycle.reverses.remove(mapping)
        lifecycle.job=SimpleNamespace(close=lambda:None)
        lifecycle.provision=lifecycle.observer=lifecycle.mirror=None; lifecycle.logs=[]
        return lifecycle

    def test_uncertain_device_or_package_manager_failure_retains_private_work_and_retry_ledger(self):
        for remote in ('','owned-package-present','owned-package-unknown'):
            with self.subTest(remote=remote),tempfile.TemporaryDirectory() as directory:
                lifecycle=self.lifecycle(directory,remote)
                with self.assertRaises(e2e.FixtureError): lifecycle.cleanup()
                ledger=json.loads((lifecycle.args.results/'cleanup-ledger.json').read_text())
                self.assertEqual(ledger['serial'],'emulator-5554')
                self.assertEqual(ledger['package'],lifecycle.package)
                self.assertEqual(ledger['app_removal'],'uncertain')
                self.assertTrue(ledger['retained_work'])
                self.assertTrue(lifecycle.work.is_dir())
                self.assertEqual(ledger['confirmed_owned_reverse_mappings'],[])
                self.assertNotIn('uncertain adb response',json.dumps(ledger))

    def test_opt_in_retain_keeps_private_work_of_a_failed_run_only(self):
        for result, retained in (('failed', True), ('passed', False)):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory, \
                    patch.dict('os.environ', {'SHOREBIRD_E2E_RETAIN_WORK_ON_FAILURE': '1'}):
                lifecycle=self.lifecycle(directory,'owned-package-absent')
                lifecycle.manifest['result']=result
                lifecycle.cleanup()
                ledger=json.loads((lifecycle.args.results/'cleanup-ledger.json').read_text())
                self.assertEqual(ledger['retained_work'],retained)
                self.assertEqual(lifecycle.work.exists(),retained)

    def test_confirmed_absence_resolves_uncertain_uninstall_without_replaying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            lifecycle=self.lifecycle(directory,'owned-package-absent')
            lifecycle.cleanup()
            ledger=json.loads((lifecycle.args.results/'cleanup-ledger.json').read_text())
            self.assertEqual(ledger['app_removal'],'confirmed-absent')
            self.assertFalse(ledger['retained_work'])
            self.assertFalse(lifecycle.work.exists())
            self.assertEqual(len([action for action in ledger['actions'] if action['action']=='uninstall-owned-package']),1)


class ControlStartCommandTest(unittest.TestCase):
    def test_non_fresh_control_gets_its_own_task_in_the_running_process(self):
        fresh = e2e.control_start_command('p.q', {'operation': 'launch'}, True)
        again = e2e.control_start_command('p.q', {'operation': 'read-provider'}, False)
        self.assertNotIn('--activity-multiple-task', fresh)
        self.assertEqual(again[:4], ['am', 'start', '-W', '--activity-multiple-task'])
        self.assertEqual(again[4:7], ['-n', 'p.q/.ControlActivity', '--es'])
        self.assertEqual(again[-2:], ['operation', 'read-provider'])


class PrivateFailureLogTest(unittest.TestCase):
    def test_diagnostic_is_opt_in_private_and_outside_results(self):
        error = e2e.FixtureError('specific private reason')
        self.assertIsNone(e2e.record_private_failure(error, {}))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'failure.log'
            path = e2e.record_private_failure(error, {'SHOREBIRD_E2E_PRIVATE_FAILURE_LOG': str(target)})
            self.assertEqual(path, target)
            self.assertIn('specific private reason', target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class AdversarialOracleTest(unittest.TestCase):
    identity=ColdOracleTest.identity
    report=ColdOracleTest.report
    def test_expired_historical_context_cannot_fail_zero_or_current_valid_receipts(self):
        import copy
        live=fixture_context(); expired=copy.deepcopy(live)
        expired['selection']['epoch']='1'; expired['selection_digest']='e'*64
        expired['selection']['offline_valid_until']='2'
        aad=shared.canonical_aad(aad_context()); key,_=shared.delivery_material(b'secret',aad)
        with patch.object(shared.time,'time',return_value=1000):
            identity=shared.validate_context(key,aad,live,[live])
            self.assertEqual(e2e.provider_identities([], [expired,live], {}, b'secret',17),[])
            self.assertEqual(e2e.provider_identities([identity],[expired,live],{'pro':{'hash':'a'*64,'transport_hash':'b'*64}},b'secret',17),[identity])
            with self.assertRaises(ValueError):
                e2e.provider_identities([dict(identity,epoch='1',digest='e'*64)],[expired,live],{'pro':{'hash':'a'*64,'transport_hash':'b'*64}},b'secret',17)

    def test_failed_content_and_expiry_are_base_without_retryable_key_restart_or_release(self):
        latest=self.identity(); free=dict(latest,variant_id='free',license_type='free',patch_number=None)
        for reason in ('expired','content_unavailable'):
            report=self.report(latest,current=free,variant='free',restart=False)
            report['selection']['next_boot']=dict(variant_id='free',license_type='free',patch_number=None,reason=reason)
            self.assertIs(e2e.validate_launch_state(report,latest,online=False,mode='correct',fallback_reason=reason),report)
            self.assertEqual(e2e.validate_launch_releases([],latest,free,online=False,mode='correct',fallback_reason=reason),[])
            with self.assertRaises(ValueError):
                e2e.validate_launch_releases([dict(latest,key_id='AbCdEfGhIjKlMnOp')],latest,free,online=False,mode='correct',fallback_reason=reason)
            with self.assertRaises(ValueError):
                e2e.validate_launch_state(report,latest,online=False,mode='missing')
            report['selection']['restart_required']=True
            with self.assertRaises(ValueError):
                e2e.validate_launch_state(report,latest,online=False,mode='correct',fallback_reason=reason)


class CacheControlReceiptTest(unittest.TestCase):
    def payload(self):
        return {'scope':'hashes-only-no-key-reads','roots':[
            {'label':'files','present':True,'files':[
                {'path':'patches/2/state.json','size':111,'sha256':'a'*64},
                {'path':'patches/2/transport.aesgcm','size':32,'sha256':'b'*64},
                {'path':'selections-v2/'+'c'*64+'.json','size':333,'sha256':'d'*64}]},
            {'label':'code-cache','present':False,'files':[]}]}

    def test_hash_only_snapshot_rejects_unsafe_duplicates_unknown_and_unbounded_entries(self):
        import copy
        good=self.payload(); self.assertEqual(len(e2e.cache_snapshot(good)['journals']),1)
        for path in ('../state.json','/state.json','selections-v2/authority.json','keys/key.wrapped'):
            changed=copy.deepcopy(good); changed['roots'][0]['files'][0]['path']=path
            with self.subTest(path=path),self.assertRaises(ValueError): e2e.cache_snapshot(changed)
        for values in ({'size':True},{'size':268435457},{'sha256':'bad'},{'json':{'untrusted':'must not cross control'}}):
            changed=copy.deepcopy(good);changed['roots'][0]['files'][0].update(values)
            with self.subTest(values=values),self.assertRaises(ValueError): e2e.cache_snapshot(changed)
        changed=copy.deepcopy(good);changed['roots'][0]['files'].append(changed['roots'][0]['files'][0])
        with self.assertRaises(ValueError): e2e.cache_snapshot(changed)

    def test_mutation_receipt_requires_exact_cas_target_and_byte_identical_journal(self):
        import copy
        before=self.payload();after=copy.deepcopy(before);after['roots'][0]['files'][1]['sha256']='e'*64
        receipt={'root':'files','path':'patches/2/transport.aesgcm','before_sha256':'b'*64,'after_sha256':'e'*64,'before':before,'after':after}
        snapshot=e2e.cache_snapshot(before)
        self.assertEqual(e2e.validate_cache_mutation(receipt,snapshot,'ciphertext','2'),e2e.cache_snapshot(after))
        for key,value in (('path','selections-v2/'+'c'*64+'.json'),('before_sha256','f'*64),('after_sha256','b'*64)):
            with self.subTest(key=key),self.assertRaises(ValueError): e2e.validate_cache_mutation(dict(receipt,**{key:value}),snapshot,'ciphertext','2')
        for index in (0,2):
            changed=copy.deepcopy(receipt);changed['after']['roots'][0]['files'][index]['sha256']='f'*64
            with self.assertRaises(ValueError): e2e.validate_cache_mutation(changed,snapshot,'ciphertext','2')


class AdversarialHarnessTest(unittest.TestCase):
    def test_replay_launch_controls_use_check_without_real_service_or_provision_credentials(self):
        import shlex
        from types import SimpleNamespace
        life=object.__new__(e2e.Lifecycle);life.api=life.provision=None;life.replay_active=True
        life.control='a'*64;life.package='dev.shorebird.e2e.test';life.release_id=17
        life.observer=SimpleNamespace(base='http://127.0.0.1:1');life.last_current=None
        context=fixture_context();identity=shared.selection_identity({'epoch':'2','digest':'d'*64,'variant_id':'pro','license_type':'pro','patch_number':'2'})
        life.variants=SimpleNamespace(latest=identity,contexts=[context]);life.stop_app=lambda:None
        captured=[]
        def adb(*args): captured.append(shlex.split(args[1]));raise RuntimeError('stop after actual adb construction')
        life.adb=adb
        with self.assertRaisesRegex(RuntimeError,'stop after actual adb'):
            life.control_command('launch',check=True,replay=True)
        command=captured[0];extras={command[index+1]:command[index+2] for index,value in enumerate(command) if value=='--es'}
        self.assertEqual(extras['check'],'1');self.assertNotIn('provision_url',extras);self.assertNotIn('provision_token',extras)
        for api,provision,active in ((object(),None,True),(None,object(),True),(None,None,False)):
            life.api,life.provision,life.replay_active=api,provision,active
            with self.assertRaises(e2e.FixtureError):life.control_command('launch',check=True,replay=True)

    def test_actual_launch_allows_only_exact_automatic_free_and_pins_running_identity(self):
        import copy
        from types import SimpleNamespace
        old=dict(epoch='4',digest='d'*64,variant_id='enterprise',license_type='enterprise',patch_number='3')
        new=dict(epoch='5',digest='f'*64,variant_id='free',license_type='free',patch_number=None)
        for automatic,changed in ((True,{}),(False,{}),(True,{'epoch':'6'})):
            life=object.__new__(e2e.Lifecycle);life.assert_apk=lambda:None;life.session='session';life.sentinel='sentinel';life.previous_data=None
            life.last_current=old.copy();life.args=SimpleNamespace(runtime_timeout=0.1);life.authority=SimpleNamespace(secret=b'secret');life.release_id=17
            life.variants=SimpleNamespace(latest=old.copy(),contexts=[]);life.patch_material={}
            life.variants.remember=lambda report:setattr(life.variants,'latest',shared.selection_identity(report['selection']['accepted']))
            report=dict(schema=shared.SCHEMA,nonce='nonce',pid=42,post_frame=True,rendered=['variant:enterprise enterprise:24'],variant='enterprise',new_code_result='enterprise:24',installation_id='installation',stage='checked',check_status='upToDate',check_error=None,
                data={'session':'session','sentinel':'sentinel','launches':1,'history':['enterprise'],'schema':3},
                selection={'version':2,'current':old.copy(),'accepted':dict(new,**changed),'restart_required':True,'next_boot':dict(variant_id='free',license_type='free',patch_number=None,reason='selected_base')})
            life.observer=SimpleNamespace(get=lambda *args:copy.deepcopy(report));life.pid=lambda:42;life.stage=lambda *args:None
            life.control_command=lambda operation,**kwargs:('nonce',{'pid':42,'payload':{'receipts':[]}})
            if automatic and not changed:
                self.assertEqual(life.launch_variant('auto','enterprise',online=True,automatic_free=True)['selection']['accepted'],new)
            else:
                with self.assertRaises((ValueError,e2e.FixtureError)):life.launch_variant('auto','enterprise',online=True,automatic_free=automatic)

    def test_android_adversarial_sequence_uses_explicit_reauthorization_clock_and_replay_flags(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        life=object.__new__(e2e.Lifecycle);events=[]
        identity=dict(epoch='4',digest='d'*64,variant_id='pro',license_type='pro',patch_number='2')
        life.variants=SimpleNamespace(latest=identity,installation_id='installation');life.api=life.provision=None
        life.scope={};life.request=lambda *args:None;life.api_url='http://127.0.0.1:1';life.args=SimpleNamespace(lease_seconds=90,runtime_timeout=10)
        snapshot={'files':{'files/patches/2/transport.aesgcm':{'size':32,'sha256':'a'*64}},'journals':{'journal':{'size':3,'sha256':'b'*64}}}
        cache={'pruned':False}
        def switch(variant,current,label,**kwargs):
            events.append(('switch',variant,current,label,kwargs));cache['pruned']=variant=='free'
        life.switch=switch;life.stop_app=lambda:None;life.online=lambda:None;life.go_offline=lambda:None
        life.inspect_cache=lambda:dict(snapshot,files={} if cache['pruned'] else snapshot['files'])
        life.assert_delivery_ciphertext=lambda *args:snapshot;life.mutate_cache=lambda *args:(snapshot,snapshot)
        life.launch_variant=lambda label,variant,**kwargs:events.append(('launch',label,variant,kwargs))
        life.stage=lambda *args:None;life.patch_control=lambda variant,action:events.append(('patch',variant,action))
        clocks=iter((1000,1000,1092));life.device_time=lambda:next(clocks)
        @contextmanager
        def replay(url,body,requests):
            requests.append(dict(accepted_selection_epoch='4',accepted_selection_digest='d'*64,client_id='installation',nonce='new'))
            yield
        old=dict(identity,epoch='1');evidence={'request':{'nonce':'old'},'limitation':'Independent capture nonce'}
        with patch.object(e2e.adversarial,'capture_response',return_value=(b'{"authentication":{"signature":"AbC"}}',old,evidence)), \
             patch.object(e2e.adversarial,'replay_endpoint',side_effect=replay),patch.object(e2e.adversarial,'wait_past_deadline',return_value=1092), \
             patch.object(e2e.time,'time',return_value=1000):
            life.adversarial_cases()
        labels={event[1]:event[3] for event in events if event[0]=='launch'}
        for label in ('authentic-old-response-rejected','tampered-response-signature-rejected'):
            self.assertEqual(labels[label],{'check':True,'replay':True,'fallback_reason':'expired'})
        self.assertEqual(labels['ciphertext-tamper-free'],{'fallback_reason':'content_unavailable'})
        self.assertEqual(labels['online-expired-server-free'],{'online':True,'automatic_free':True})
        self.assertIn(('patch','enterprise','rollback'),events);self.assertIn(('patch','enterprise','rollforward'),events)
        self.assertTrue(any(event[:4]==('switch','pro','free','ciphertext-reauthorized-pro') for event in events))
        self.assertTrue(any(event[:4]==('switch','enterprise','free','explicit-reauthorization') for event in events))


if __name__ == '__main__':
    unittest.main()

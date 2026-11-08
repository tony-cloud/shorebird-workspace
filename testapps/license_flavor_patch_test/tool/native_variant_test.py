#!/usr/bin/env python3
"""Focused native-v2 protocol, orchestration and stub-cache adapter controls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import native_variant_e2e as v2
import native_variant_adversarial as adversarial
import native_variant_desktop as desktop
from native_variant_desktop import NativeDesktopVariant, run_driver
import linux_variant_e2e
import windows_variant_e2e

APP = '7cd314d0-8ace-46a2-b21b-81a92bcd0738'
INSTALLATION = 'real-public-installation'
CLIENT_HASH = hashlib.sha256(INSTALLATION.encode()).hexdigest()


def grant(variant='pro',epoch='1',platform='linux',arch='x86_64'):
    number = v2.EXPECTED_NUMBERS[variant]
    return {'selection':{'epoch':epoch,'scope':{'app_id':APP,'release_version':'1.0.0+1',
        'platform':platform,'arch':arch,'base_artifact_hash':'a'*64,'host_abi_hash':None,
        'runtime_profile':'native-aot','client_id_hash':CLIENT_HASH},'channel':'stable','variant_id':variant,
        'license_type':variant,'entitlement_revision':'session:policy', 'not_before':'0',
        'offline_valid_until':None,'fallback':{'kind':'base','variant_id':'free','license_type':'free'},
        'target':{'kind':'patch' if number else 'base','patch_number':number,'artifact_hash':'b'*64 if number else None,
                  'encryption':'transport-aes-256-gcm' if number else 'none','artifact_key_id':None}},
        'selection_digest':hashlib.sha256((epoch+variant).encode()).hexdigest(),'authentication':{'actual':'server-test-double'}}


def identity(result):
    c = v2.context_for(result)
    s = c['selection']
    return {'epoch':s['epoch'],'digest':c['selection_digest'],'variant_id':s['variant_id'],
            'license_type':s['license_type'],'patch_number':s['target']['patch_number']}


def aad_context(platform='linux',arch='x86_64'):
    return {'app_id':APP,'release_id':17,'patch_number':2,'patch_hash':'b'*64,'transport_hash':'c'*64,
            'platform':platform,'arch':arch,'device_id_hash':CLIENT_HASH}


def report(variant='free',current=None,accepted=None,previous=None,nonce='abc',pid=10):
    initial = {'epoch':'0','digest':None,'variant_id':'free','license_type':'free','patch_number':None}
    current,accepted = current or initial,accepted or current or initial
    data = dict(previous or {'session':'session','sentinel':'sentinel','schema':1,'launches':0,'history':[]})
    data.update(schema=max(data['schema'],{'free':1,'pro':2,'enterprise':3}[variant]),
                launches=data['launches']+1,history=data['history']+[variant])
    if variant != 'free': data.setdefault('pro_note','pro-preserved')
    if variant == 'enterprise': data.setdefault('enterprise_note','enterprise-preserved')
    code = {'free':'free:base','pro':'pro:42','enterprise':'enterprise:24'}[variant]
    return {'schema':v2.SCHEMA,'nonce':nonce,'pid':pid,'post_frame':True,'variant':variant,'new_code_result':code,
        'rendered':['variant:'+variant+' '+code],'installation_id':INSTALLATION,'stage':'boot','data':data,
        'selection':{'version':2,'current':current,'accepted':accepted,
            'next_boot':dict({key:accepted[key] for key in ('variant_id','license_type','patch_number')},reason='ready' if accepted['patch_number'] else 'selected_base'),
            'restart_required':current != accepted}}


class ProtocolTest(unittest.TestCase):
    def test_real_assignment_context_and_aad_binding_all_native_targets(self):
        for platform,arch in (('android','aarch64'),('linux','x86_64'),('linux','aarch64'),('windows','x86_64'),('windows','aarch64')):
            with self.subTest(platform=platform,arch=arch):
                context = v2.context_for(grant(platform=platform,arch=arch))
                aad = v2.canonical_aad(aad_context(platform,arch))
                key_id,_ = v2.delivery_material(b'fixture',aad)
                value = v2.validate_context(key_id,aad,context,[context])
                self.assertEqual(value['variant_id'],'pro')
                self.assertEqual(value['patch_number'],'2')
                with self.assertRaises(ValueError): v2.validate_context(key_id,aad,context,[])
                for mutate in ('scope','target','profile','key','epoch'):
                    altered = copy.deepcopy(context)
                    if mutate=='scope': altered['selection']['scope']['client_id_hash']='d'*64
                    elif mutate=='target': altered['selection']['target']['artifact_hash']='d'*64
                    elif mutate=='profile': altered['selection']['scope']['runtime_profile']='interpreter-full'
                    elif mutate=='key': altered['selection']['target']['artifact_key_id']='artifact-key'
                    else: altered['selection']['epoch']='01'
                    with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                        v2.validate_context(key_id,aad,altered,[altered])

    def test_multiple_real_crypto_contexts_authentication_and_duplicate_json(self):
        authority = v2.KeyAuthority(b'private-master','private-token','linux','x86_64')
        for number in (2,3):
            context = dict(aad_context(),patch_number=number)
            aad = authority.allow(context)
            key_id,key = v2.delivery_material(authority.secret,aad)
            body = json.dumps({'key_id':key_id,'aad':aad}).encode()
            self.assertEqual(authority.provision('Bearer private-token',body),key.hex().encode())
            with self.assertRaises(ValueError): authority.provision('Bearer wrong',body)
        with self.assertRaises(ValueError): authority.provision('Bearer private-token',b'{"key_id":"x","key_id":"y","aad":"bad"}')
        self.assertEqual(authority.accepted,2)
        self.assertEqual(authority.rejected,3)

    def test_public_status_same_content_higher_epoch_and_data_downgrade(self):
        pro1,pro2 = identity(grant(epoch='1')),identity(grant(epoch='2'))
        first = report('pro',pro1,pro2)
        v2.validate_report(first,'abc',10,'pro',accepted=pro2)
        invalid = copy.deepcopy(first)
        invalid['selection']['restart_required']=False
        with self.assertRaises(ValueError): v2.validate_report(invalid,'abc',10,'pro',accepted=pro2)
        enterprise = report('enterprise',identity(grant('enterprise','3')),previous=first['data'])
        free = report('free',identity(grant('free','4')),previous=enterprise['data'])
        v2.validate_report(free,'abc',10,'free',previous_data=enterprise['data'])
        self.assertEqual(free['data']['enterprise_note'],'enterprise-preserved')
        invalid = copy.deepcopy(free)
        invalid['data'].pop('enterprise_note')
        with self.assertRaises(ValueError): v2.validate_report(invalid,'abc',10,'free',previous_data=enterprise['data'])
        for field in ('nonce','pid','installation_id','rendered'):
            invalid = copy.deepcopy(first)
            invalid[field] = None
            with self.subTest(field=field),self.assertRaises((ValueError,TypeError)):
                v2.validate_report(invalid,'abc',10,'pro')

    def test_real_api_routes_cas_idempotent_retry_and_lower_number_selection(self):
        calls=[]
        fail=[True]
        def request(method,path,body):
            calls.append((method,path,copy.deepcopy(body)))
            if method=='PUT': return {}
            if fail.pop() if fail else False: raise OSError('uncertain request result')
            result = grant(body['variant_id'],str(int(body['expected_epoch'])+1))
            result['selection'].update({key:body[key] for key in ('channel','entitlement_revision','not_before','offline_valid_until')})
            return result
        session=v2.NativeVariantSession(APP,17,'1.0.0+1','linux','x86_64','session',request)
        session.map_free()
        session.remember(report())
        with self.assertRaises(OSError): session.assign('pro')
        session.assign('pro')
        self.assertEqual(calls[1],calls[2])
        self.assertEqual(calls[1][1],f'/api/v1/apps/{APP}/releases/17/selections')
        session.assign('enterprise'); session.assign('pro')
        self.assertEqual(session.latest['epoch'],'3')
        self.assertEqual(session.latest['patch_number'],'2')
        self.assertEqual(len(session.contexts),3)
        with self.assertRaises(ValueError): session.remember(report())

    def test_cold_requires_exact_epoch_digest_and_fallback_preserves_highwater(self):
        old,new = identity(grant(epoch='2')),identity(grant(epoch='4'))
        pending = report('pro',old,new)
        v2.validate_report(pending,'abc',10,'pro',accepted=new)
        with self.assertRaises(ValueError): v2.validate_cold_activation(pending,new)
        current = report('pro',new,new)
        v2.validate_cold_activation(current,new)
        fallback = report('free',dict(new,variant_id='free',license_type='free',patch_number=None),new)
        v2.validate_cold_activation(fallback,new,fallback=True)
        fallback['selection']['current'].update(epoch='0',digest=None)
        with self.assertRaises(ValueError): v2.validate_cold_activation(fallback,new,fallback=True)

    def test_provider_lease_gate_rejects_future_expired_and_noncanonical_before_release(self):
        aad=v2.canonical_aad(aad_context()); key_id,_=v2.delivery_material(b'fixture',aad)
        for start,end in (('101',None),('0','100'),('010',None),('0','253402300800'),('50','49')):
            context=v2.context_for(grant())
            context['selection'].update(not_before=start,offline_valid_until=end)
            with self.subTest(start=start,end=end),mock.patch.object(v2.time,'time',return_value=100),self.assertRaises(ValueError):
                v2.validate_context(key_id,aad,context,[context])

    def test_assignment_response_must_match_actual_requested_lease_channel_and_policy(self):
        for wrong in ('channel','entitlement_revision','not_before','offline_valid_until'):
            def request(method,path,body):
                result=grant()
                result['selection'].update({key:body[key] for key in ('channel','entitlement_revision','not_before','offline_valid_until')})
                result['selection'][wrong]='different'
                return result
            session=v2.NativeVariantSession(APP,17,'1.0.0+1','linux','x86_64','session',request)
            session.remember(report())
            with self.subTest(wrong=wrong),self.assertRaises(ValueError): session.assign('pro')

    def test_private_metadata_cleanup_is_contained_and_retains_pinned_engine_files(self):
        with tempfile.TemporaryDirectory() as temp:
            owned=Path(temp).resolve(); flutter=owned/'flutter'
            (flutter/'bin/cache').mkdir(parents=True); (flutter/'bin/internal').mkdir()
            stamp='f'*40+'\n'
            (flutter/'bin/cache/engine.stamp').write_text(stamp)
            (flutter/'bin/internal/engine.version').write_text(stamp)
            for name in ('engine_stamp.json','flutter.version.json'):
                (flutter/'bin/cache'/name).write_text('{"stale":"informational"}')
            value=v2.clear_private_flutter_metadata(flutter,owned)
            self.assertEqual(set(value['removed_inherited_metadata']),{'engine_stamp.json','flutter.version.json'})
            self.assertEqual((flutter/'bin/cache/engine.stamp').read_text(),stamp)
            with self.assertRaises(ValueError): v2.clear_private_flutter_metadata(flutter,flutter)
            (flutter/'bin/cache/engine.stamp').write_text('0'*40)
            with self.assertRaises(ValueError): v2.clear_private_flutter_metadata(flutter,owned)

    def test_structural_profiles_use_real_public_api_and_distinct_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp); (app/'lib').mkdir()
            sources=[]
            for phase in v2.BUILD_ORDER:
                v2.apply_profile(app,'session','sentinel',phase)
                source=(app/'lib/main.dart').read_text()
                for marker in ('readInstallationId()', 'readSelectionStatus()', 'RenderParagraph','persistData()',
                               'user-data.json','SHOREBIRD_VARIANT_CHECK','updater.update()'):
                    self.assertIn(marker,source)
                sources.append(source)
            self.assertEqual(len(set(sources)),4)
            self.assertIn('ProVariantFeature',sources[2])
            self.assertIn('EnterpriseVariantFeature',sources[3])
            with self.assertRaises(ValueError): v2.apply_profile(app,"quote'",'sentinel','base')


class DesktopTest(unittest.TestCase):
    def test_actual_staged_build_method_uses_cli_finalization_and_binds_packaged_scope(self):
        native=linux_variant_e2e.native
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            app=work/'app'; (app/'lib').mkdir(parents=True); (app/'linux').mkdir()
            build=work/'app/build/linux/x64/release/bundle'
            (build/'lib').mkdir(parents=True); (build/'data/flutter_assets').mkdir(parents=True)
            (build/'lib/libapp.so').write_bytes(b'actual-built-native-AOT-test-double')
            (build/'lib/libflutter_linux_gtk.so').write_bytes(b'engine')
            (build/'license_flavor_patch_test').write_bytes(b'runner')
            engine=work/'engine/out/host'; engine.mkdir(parents=True)
            (engine/'libflutter_linux_gtk.so').write_bytes(b'engine')
            lifecycle=linux_variant_e2e.Lifecycle.__new__(linux_variant_e2e.Lifecycle)
            lifecycle.work=work; lifecycle.args=SimpleNamespace(arch='x64',engine_src=work/'engine',host_engine='host',target_engine='host')
            lifecycle.session_id='session'; lifecycle.sentinel='sentinel'; lifecycle.app_id=APP
            lifecycle.api_url='http://127.0.0.1:12345'
            lifecycle.cli=work/'cli'; lifecycle.native_sources={}
            with mock.patch.object(native.Lifecycle,'bootstrap'):
                lifecycle.bootstrap()
            lifecycle.environment=lambda: {}; lifecycle.stage=mock.Mock()
            published=[False]; calls=[]
            basehash=native.file_hash(build/'lib/libapp.so')
            scope={'app_id':APP,'release_version':'1.0.0+1','platform':'linux','arch':'x86_64',
                   'base_artifact_hash':basehash,'host_abi_hash':None,'runtime_profile':'native-aot'}
            (build/'data/flutter_assets/shorebird_release_scope_linux_x86_64.json').write_text(json.dumps(scope))
            (build/'data/flutter_assets/shorebird.yaml').write_text(f'app_id: {APP}\nselection_protocol: 2\n'
                'aot_patch_runtime_mode: native-aot\npatch_verification: strict\npatch_response_verification: required\n'
                'native_patch_max_bytes: 268435456\naot_patch_app_build_id: 1.0.0+1\npatch_public_key: a2V5\n')
            def request(method,path,body=None):
                calls.append((method,path,body))
                if path.endswith('/releases'): return {'releases':[{'id':17,'version':'1.0.0+1','selection_protocol':2}]}
                if path.endswith('/selection-scopes'): return {'scopes':[{'scope':scope,'published':published[0]}]}
                return {}
            lifecycle.request=request
            def command(argv,**kwargs):
                calls.append(('CLI',list(map(str,argv)),kwargs['name']))
                self.assertIn('aot_patch_app_build_id: 1.0.0+1',(app/'shorebird.yaml').read_text())
                if kwargs['name']=='variant-release-finalize': published[0]=True
            lifecycle.run_command=command
            with mock.patch.object(native,'verify_binary'):
                lifecycle.build_variant('base')
            cli=[item for item in calls if item[0]=='CLI']
            self.assertIn('--stage-only',cli[0][1])
            self.assertIn('release-finalize',cli[1][1])
            self.assertIn('--arch=x86_64',cli[1][1])
            self.assertTrue(published[0])
            self.assertEqual(lifecycle.base_hash,basehash)
            self.assertTrue(any(item[0]=='PUT' and item[1].endswith('/variants/free') for item in calls))

    def test_windows_actual_subclass_launch_uses_dpapi_sanitized_env_and_public_receipt(self):
        native=windows_variant_e2e.native
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); bundle=root/'released'; bundle.mkdir(); (bundle/'app.exe').write_bytes(b'app')
            lifecycle=windows_variant_e2e.Lifecycle.__new__(windows_variant_e2e.Lifecycle)
            lifecycle.work=root; lifecycle.bundle=bundle; lifecycle.bundle_hashes=native.tree_hashes(bundle)
            lifecycle.runner=bundle/'app.exe'; lifecycle.key_cache=root/'dpapi'; lifecycle.data_directory=root/'data'
            lifecycle.provider_receipt=root/'provider.jsonl'; lifecycle.previous_data=None
            lifecycle.protocol=v2.NativeVariantSession(APP,17,'1.0.0+1','windows','x86_64','session',mock.Mock())
            lifecycle.observer=SimpleNamespace(url='http://127.0.0.1:12345/frame')
            lifecycle.session_id='session'; lifecycle.sentinel='sentinel'; lifecycle.logs=[]
            lifecycle.receipts={}; lifecycle.receipt_lock=threading.Lock(); lifecycle.args=SimpleNamespace(runtime_timeout=1)
            lifecycle.stage=mock.Mock(); lifecycle.api=lifecycle.provision=None
            def spawn(argv,**kwargs):
                env=kwargs['env']
                self.assertEqual(env['SHOREBIRD_E2E_KEY_CACHE_DIR'],str(root/'dpapi'))
                self.assertEqual(env['SHOREBIRD_E2E_KEY_MODE'],'correct')
                self.assertNotIn('HTTP_PROXY',env)
                self.assertNotIn('SHOREBIRD_TOKEN',env)
                self.assertNotIn('PATCH_SIGNING_MASTER_KEY',env)
                nonce=env['SHOREBIRD_E2E_RECEIPT']
                lifecycle.receipts[nonce]=report(nonce=nonce,pid=123)
                return mock.Mock(pid=123,poll=mock.Mock(return_value=None))
            lifecycle.job=mock.Mock(spawn=mock.Mock(side_effect=spawn))
            with mock.patch.dict(os.environ,{'HTTP_PROXY':'foreign','SHOREBIRD_TOKEN':'secret','PATCH_SIGNING_MASTER_KEY':'secret'}):
                value=lifecycle.launch_variant('initial-free','free')
            self.assertEqual(value['installation_id'],INSTALLATION)
            self.assertEqual(lifecycle.protocol.installation_id,INSTALLATION)
            older,newer=grant(epoch='2',platform='windows'),grant(epoch='4',platform='windows')
            latest=identity(newer)
            lifecycle.protocol.observed=latest
            lifecycle.protocol.contexts=[v2.context_for(older),v2.context_for(newer)]
            def cold_spawn(argv,**kwargs):
                env=kwargs['env']
                contexts=json.loads(env['SHOREBIRD_VARIANT_CONTEXTS'])
                self.assertEqual([item['selection']['epoch'] for item in contexts],['4'])
                self.assertNotIn('SHOREBIRD_VARIANT_CHECK',env)
                nonce=env['SHOREBIRD_E2E_RECEIPT']
                lifecycle.receipts[nonce]=report('pro',latest,latest,previous=lifecycle.previous_data,nonce=nonce,pid=124)
                lifecycle.provider_receipt.write_text(json.dumps(dict(latest,nonce=nonce,pid=124,key_id='A'*16))+'\n')
                return mock.Mock(pid=124,poll=mock.Mock(return_value=None))
            lifecycle.job.spawn.side_effect=cold_spawn
            cold=lifecycle.launch_variant('cold-pro','pro',cold=True)
            self.assertEqual(cold['selection']['current'],latest)
            quarantined=[False]
            def fallback_spawn(argv,**kwargs):
                env=kwargs['env']; nonce=env['SHOREBIRD_E2E_RECEIPT']
                self.assertEqual(env['SHOREBIRD_E2E_KEY_MODE'],'missing')
                current=dict(latest,variant_id='free',license_type='free',patch_number=None)
                value=report('free',current,latest,previous=lifecycle.previous_data,nonce=nonce,pid=126)
                if quarantined[0]:
                    value['selection']['next_boot']={'variant_id':'free','license_type':'free','patch_number':None,'reason':'quarantined'}
                    value['selection']['restart_required']=False
                lifecycle.receipts[nonce]=value
                return mock.Mock(pid=126,poll=mock.Mock(return_value=None))
            lifecycle.job.spawn.side_effect=fallback_spawn
            fallback=lifecycle.launch_variant('missing-free','free',mode='missing',cold=True,fallback=True)
            self.assertTrue(fallback['selection']['restart_required'])
            quarantined[0]=True
            with self.assertRaisesRegex(native.FixtureError,'Retryable key refusal'):
                lifecycle.launch_variant('bad-quarantine-free','free',mode='missing',cold=True,fallback=True)
            for log in lifecycle.logs: log.close()

    def test_metadata_is_cleared_before_first_generated_flutter_invocation(self):
        native=windows_variant_e2e.native
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp).resolve()
            lifecycle=windows_variant_e2e.Lifecycle.__new__(windows_variant_e2e.Lifecycle)
            lifecycle.work=work; lifecycle.manifest={'sources':{'flutter':{'head':'ref'}}}
            roots=(work/'flutter',work/'cli/bin/cache/flutter/ref')
            for flutter in roots:
                (flutter/'bin/cache').mkdir(parents=True); (flutter/'bin/internal').mkdir()
                (flutter/'bin/cache/engine.stamp').write_text('e'*40)
                (flutter/'bin/internal/engine.version').write_text('e'*40)
                (flutter/'bin/cache/engine_stamp.json').write_text('stale')
                (flutter/'bin/cache/flutter.version.json').write_text('stale')
            commands=[]
            def output(command,cwd=None):
                commands.append(command)
                return b'1700000000\n'
            lifecycle.output=output
            def generate(*args,**kwargs):
                for flutter in roots:
                    # Inherited copies are gone; the authored stamp dates the pinned engine
                    # so flutter_tools never falls back to `git log <engine id>`.
                    stamp=json.loads((flutter/'bin/cache/engine_stamp.json').read_text())
                    self.assertEqual(stamp['git_revision'],'e'*40)
                    self.assertEqual(stamp['git_revision_date'],'2023-11-14T22:13:20Z')
                    self.assertFalse((flutter/'bin/cache/flutter.version.json').exists())
            with mock.patch.object(native.Lifecycle,'run_command',side_effect=generate):
                lifecycle.run_command(['private-flutter-create'],name='generate-windows')
            self.assertTrue(lifecycle._private_metadata_cleared)
            self.assertEqual(commands,[['git','-C',work/'flutter','log','-1','--format=%ct','HEAD']])
            for name in ('tooling','vended'):
                self.assertEqual(set(lifecycle.manifest['private_flutter_metadata'][name]['removed_inherited_metadata']),
                                 {'engine_stamp.json','flutter.version.json'})
                self.assertTrue(lifecycle.manifest['private_flutter_metadata'][name]['authored_engine_stamp'])

    def test_driver_failure_prints_sanitized_reason_and_build_excerpt(self):
        module,kind=windows_variant_e2e.native,windows_variant_e2e.Lifecycle
        for error,reason in ((module.FixtureError('variant-build-base failed (private log removed during cleanup)'),
                              'variant-build-base failed (private log removed during cleanup)'),
                             (ValueError('C:\\Users\\private token=SECRET'),'unavailable')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                args=SimpleNamespace(phase='run',execute=True,arch='arm64',work=root/'work',results=root/'results',
                    engine_src=root/'engine',tooling_dart=root/'dart',patch_tool=root/'patch',pub_cache=root/'pub',provider_probe=None)
                instance=mock.Mock(manifest={'schema':v2.SCHEMA,'result':'failed','runtime_acceptance':False,
                    'stages':[{'name':'isolated-toolchain','evidence':True}],
                    'command_failure':{'operation':'variant-build-base','exit_code':70,'timed_out':False,'classification':'unknown',
                        'exception_types':[],'build_codes':['C2220','C4996'],'known_messages':['windows_build_failed'],
                        'excerpt':['<path> error C2220: the following warning is treated as an error'],'tail':['Build process failed.']}})
                instance.execute.side_effect=error
                factory=mock.Mock(return_value=instance,target_os=kind.target_os)
                printed=[]
                with mock.patch.object(module,'parser',return_value=mock.Mock(parse_args=mock.Mock(return_value=args))), \
                     mock.patch.object(module.architecture,'require_native_host'), \
                     mock.patch.object(module,'preflight',return_value={'engine':'aarch64'}), \
                     mock.patch('builtins.print',side_effect=lambda *a,**k: printed.append(' '.join(map(str,a)))):
                    self.assertEqual(run_driver(module,factory,[]),1)
                text='\n'.join(printed)
                self.assertIn('Native V2 windows lifecycle failure: type={} after_stage=isolated-toolchain reason={} '
                              'command=variant-build-base exit_code=70'.format(type(error).__name__,reason),text)
                self.assertIn('build_codes=C2220,C4996',text)
                self.assertIn('failure excerpt: <path> error C2220',text)
                self.assertIn('failure tail: Build process failed.',text)
                self.assertNotIn('SECRET',text)
                value=json.loads((root/'results/results.json').read_text())
                self.assertNotIn('SECRET',json.dumps(value))

    def test_windows_online_checked_launch_rejects_same_variant_current_epoch_rewrite(self):
        native=windows_variant_e2e.native
        for rewritten in (False,True):
            with self.subTest(rewritten=rewritten),tempfile.TemporaryDirectory() as temp:
                root=Path(temp); bundle=root/'released'; bundle.mkdir(); (bundle/'app.exe').write_bytes(b'app')
                lifecycle=windows_variant_e2e.Lifecycle.__new__(windows_variant_e2e.Lifecycle)
                lifecycle.work=root; lifecycle.bundle=bundle; lifecycle.bundle_hashes=native.tree_hashes(bundle)
                lifecycle.runner=bundle/'app.exe'; lifecycle.key_cache=root/'dpapi'; lifecycle.data_directory=root/'data'
                lifecycle.provider_receipt=root/'provider.jsonl'; lifecycle.session_id='session'; lifecycle.sentinel='sentinel'
                prior=identity(grant(epoch='2',platform='windows'))
                latest=identity(grant(epoch='4',platform='windows'))
                lifecycle.last_current_identity=prior
                lifecycle.previous_data=report('pro',prior,prior)['data']
                lifecycle.protocol=v2.NativeVariantSession(APP,17,'1.0.0+1','windows','x86_64','session',mock.Mock())
                lifecycle.protocol.installation_id=INSTALLATION; lifecycle.protocol.observed=latest
                lifecycle.protocol.contexts=[v2.context_for(grant(epoch=epoch,platform='windows')) for epoch in ('2','4')]
                lifecycle.observer=SimpleNamespace(url='http://127.0.0.1:12345/frame')
                lifecycle.provision=SimpleNamespace(url='http://127.0.0.1:23456/key')
                lifecycle.secrets={'provision':'private-token'}; lifecycle.api=object()
                lifecycle.logs=[]; lifecycle.receipts={}; lifecycle.receipt_lock=threading.Lock()
                lifecycle.args=SimpleNamespace(runtime_timeout=1); lifecycle.stage=mock.Mock()
                def spawn(argv,**kwargs):
                    env=kwargs['env']; nonce=env['SHOREBIRD_E2E_RECEIPT']
                    self.assertEqual([c['selection']['epoch'] for c in json.loads(env['SHOREBIRD_VARIANT_CONTEXTS'])],['2','4'])
                    current=identity(grant(epoch='3',platform='windows')) if rewritten else prior
                    value=report('pro',current,latest,previous=lifecycle.previous_data,nonce=nonce,pid=125)
                    value['stage']='checked'; lifecycle.receipts[nonce]=value
                    return mock.Mock(pid=125,poll=mock.Mock(return_value=None))
                lifecycle.job=mock.Mock(spawn=mock.Mock(side_effect=spawn))
                try:
                    if rewritten:
                        with self.assertRaisesRegex(native.FixtureError,'process-pinned'):
                            lifecycle.launch_variant('online-check','pro',check=True,online=True)
                    else:
                        value=lifecycle.launch_variant('online-check','pro',check=True,online=True)
                        self.assertEqual(value['selection']['current'],prior)
                finally:
                    for log in lifecycle.logs: log.close()

    def test_main_handoff_keeps_machine_evidence_and_always_cleans_on_failure(self):
        for module,kind in ((linux_variant_e2e.native,linux_variant_e2e.Lifecycle),(windows_variant_e2e.native,windows_variant_e2e.Lifecycle)):
            for fail in (False,True):
                with self.subTest(platform=kind.target_os,fail=fail),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp)
                    args=SimpleNamespace(phase='run',execute=True,arch='arm64',work=root/'work',results=root/'results',
                        engine_src=root/'engine',tooling_dart=root/'dart',patch_tool=root/'patch',pub_cache=root/'pub',provider_probe=None)
                    instance=mock.Mock(manifest={'schema':v2.SCHEMA,'result':'failed','runtime_acceptance':False,'stages':[]})
                    def execute():
                        if fail: raise ValueError('authored fixture failure')
                        instance.manifest.update(result='passed',runtime_acceptance=True)
                    instance.execute.side_effect=execute
                    factory=mock.Mock(return_value=instance,target_os=kind.target_os)
                    with mock.patch.object(module,'parser',return_value=mock.Mock(parse_args=mock.Mock(return_value=args))), \
                         mock.patch.object(module.architecture,'require_native_host'), \
                         mock.patch.object(module,'preflight',return_value={'engine':'aarch64'}):
                        code=run_driver(module,factory,[])
                    value=json.loads((root/'results/results.json').read_text())
                    self.assertEqual(code,1 if fail else 0)
                    self.assertEqual(value['binary_architectures'],{'engine':'aarch64'})
                    self.assertEqual(value['runtime_arch'],'aarch64')
                    instance.cleanup.assert_called_once()
                    self.assertNotIn('authored fixture failure',json.dumps(value))


class AdversarialTest(unittest.TestCase):
    def make_cache(self,root):
        root=Path(root).resolve()
        root.mkdir()
        patch=root/'patches/2'; patch.mkdir(parents=True)
        (patch/'transport.aesgcm').write_bytes(b'ciphertext-that-must-be-downloaded')
        (patch/'state.json').write_text(json.dumps({'kind':'InstalledEncryptedTransportV1',
            'signature':'AbCd','ciphertext_size':33,'other':'preserve'}))
        journal=root/'selections-v2'; journal.mkdir()
        (journal/('a'*64+'.json')).write_bytes(b'independent-signed-journal-do-not-rewrite')
        return root

    def test_contained_cache_controls_change_one_file_and_never_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=self.make_cache(Path(temp)/'cache')
            before=adversarial.cache_snapshot(root)
            changed=adversarial.mutate_cache(root,2,'ciphertext')
            old=b'ciphertext-that-must-be-downloaded'
            new=(root/changed['path']).read_bytes()
            self.assertEqual(sum(a!=b for a,b in zip(old,new)),1)
            self.assertEqual(len(old),len(new))
            self.assertNotEqual(changed['before']['sha256'],changed['after']['sha256'])
            changed=adversarial.mutate_cache(root,2,'signature')
            state=json.loads((root/changed['path']).read_text())
            self.assertEqual(state['other'],'preserve')
            self.assertEqual(state['signature'],'BbCd')
            adversarial.assert_journal_preserved(before,adversarial.cache_snapshot(root))
            for number,kind in (('../selections-v2','signature'),('02','ciphertext'),(2,'journal')):
                with self.subTest(number=number,kind=kind),self.assertRaises(ValueError):
                    adversarial.mutate_cache(root,number,kind)
            damaged=copy.deepcopy(before); damaged['journals']['new']={'sha256':'bad'}
            with self.assertRaisesRegex(ValueError,'journal bytes'):
                adversarial.assert_journal_preserved(before,damaged)

    def test_cache_controls_reject_symlink_and_hardlink_inputs(self):
        for link in ('symbolic','hard','directory'):
            with self.subTest(link=link),tempfile.TemporaryDirectory() as temp:
                parent=Path(temp); root=self.make_cache(parent/'cache')
                outside=parent/'outside'; outside.write_bytes(b'untouched')
                path=root/'patches/2/transport.aesgcm'
                if link=='directory':
                    moved=parent/'patch'; (root/'patches/2').rename(moved)
                    (root/'patches/2').symlink_to(moved,target_is_directory=True)
                else:
                    path.unlink()
                    if link=='symbolic': path.symlink_to(outside)
                    else: os.link(outside,path)
                with self.assertRaises(ValueError): adversarial.mutate_cache(root,2,'ciphertext')
                self.assertEqual(outside.read_bytes(),b'untouched')

    def test_effective_fallback_and_signed_free_oracles_reject_adjacent_states(self):
        paid=identity(grant(epoch='4'))
        current=dict(paid,variant_id='free',license_type='free',patch_number=None)
        for reason in ('expired','content_unavailable'):
            value=report('free',current,paid)
            value['selection']['next_boot']=dict(current,reason=reason)
            for key in ('epoch','digest'): value['selection']['next_boot'].pop(key)
            value['selection']['restart_required']=False
            adversarial.validate_fallback(value,paid,reason)
            for fault in ('accepted','current','reason','restart'):
                bad=copy.deepcopy(value)
                if fault=='accepted': bad['selection']['accepted']['epoch']='3'
                elif fault=='current': bad['selection']['current']['digest']='f'*64
                elif fault=='reason': bad['selection']['next_boot']['reason']='quarantined'
                else: bad['selection']['restart_required']=True
                with self.subTest(reason=reason,fault=fault),self.assertRaises(ValueError):
                    adversarial.validate_fallback(bad,paid,reason)
        free=identity(grant('free','5'))
        value=report('free',current,free)
        self.assertEqual(adversarial.validate_automatic_free(value,paid),free)
        for epoch in ('3','4','6'):
            bad=copy.deepcopy(value); bad['selection']['accepted']=identity(grant('free',epoch))
            with self.subTest(epoch=epoch),self.assertRaises(ValueError):
                adversarial.validate_automatic_free(bad,paid)
        value.update(stage='checked',check_status='upToDate',check_error=None)
        adversarial.validate_rejected_check(value,free)
        for field,item in (('check_error','Exception'),('check_status','outdated'),('stage','boot')):
            bad=copy.deepcopy(value); bad[field]=item
            with self.subTest(field=field),self.assertRaises(ValueError):
                adversarial.validate_rejected_check(bad,free)

    def test_capture_preserves_server_response_and_exports_only_hashes_with_limitations(self):
        session=v2.NativeVariantSession(APP,17,'1.0.0+1','linux','x86_64','session',mock.Mock())
        session.installation_id=INSTALLATION; session.observed=identity(grant(epoch='7'))
        def request(method,path,body):
            self.assertEqual((method,path),('POST','/api/v1/patches/check'))
            self.assertIs(body['accept_encrypted_patch'],True)
            result=grant(epoch='7')
            result['delivery']={'download_url':'https://private.invalid/artifact?token=bearer-secret'}
            payload={'request':{'nonce':body['nonce']},'selection_digest':result['selection_digest'],
                     'selection':result['selection'],'delivery':result['delivery']}
            import base64
            result['authentication']={'payload':base64.b64encode(json.dumps(payload).encode()).decode(),
                                      'signature':'AuthenticSignature'}
            return result
        body,accepted,evidence=adversarial.capture_response(request,session,{'base_artifact_hash':'a'*64,
            'host_abi_hash':None,'runtime_profile':'native-aot'})
        self.assertEqual(accepted,session.latest)
        self.assertEqual(json.loads(body),evidence['response'])
        self.assertIn('not isolated epoch',evidence['limitation'])
        self.assertIn('not locally RSA-verified',evidence['limitation'])
        summary=adversarial.capture_summary(body,accepted,evidence)
        requests=[dict(evidence['request'])]
        exported=json.dumps([summary,adversarial.replay_requests_summary(requests)])
        for private in (INSTALLATION,'bearer-secret','private.invalid',evidence['request']['nonce'],
                        evidence['response']['authentication']['payload'],'AuthenticSignature'):
            self.assertNotIn(private,exported)
        self.assertEqual(summary['response_sha256'],hashlib.sha256(body).hexdigest())
        corrupt=json.loads(adversarial.corrupt_response_signature(body))
        original=json.loads(body)
        self.assertNotEqual(corrupt['authentication']['signature'],original['authentication']['signature'])
        corrupt['authentication']['signature']=original['authentication']['signature']
        self.assertEqual(corrupt,original)

    def test_replay_listener_is_bounded_to_check_events_and_closes(self):
        import socket
        import urllib.error
        import urllib.request
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        url=f'http://127.0.0.1:{port}'; requests=[]
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(path,value):
            return opener.open(urllib.request.Request(url+path,json.dumps(value).encode(),method='POST'),timeout=2)
        with adversarial.replay_endpoint(url,b'{"signed":"original"}',requests):
            with post('/api/v1/patches/check',{'selection_protocol':2,'nonce':'fresh'}) as response:
                self.assertEqual(response.read(),b'{"signed":"original"}')
            with post('/api/v1/patches/events',{}) as response: self.assertEqual(response.read(),b'{}')
            with self.assertRaises(urllib.error.HTTPError) as error: post('/api/v1/apps/app/selections',{})
            error.exception.close()
        self.assertEqual(requests,[{'selection_protocol':2,'nonce':'fresh'}])
        with socket.socket() as probe: self.assertNotEqual(probe.connect_ex(('127.0.0.1',port)),0)
        for unsafe in ('https://127.0.0.1:12','http://example.com:12','http://127.0.0.1:12/path'):
            with self.subTest(url=unsafe),self.assertRaises(ValueError):
                with adversarial.replay_endpoint(unsafe,b'{}',[]): pass

    def test_replay_cleanup_closes_stalled_headers_without_unbounded_handler_join(self):
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        ready=threading.Event(); release=threading.Event(); done=threading.Event(); errors=[]; requests=[]
        def listen():
            try:
                with adversarial.replay_endpoint(f'http://127.0.0.1:{port}',b'{}',requests):
                    ready.set(); release.wait(3)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        worker=threading.Thread(target=listen,daemon=True); worker.start()
        connection=None
        try:
            self.assertTrue(ready.wait(2))
            connection=socket.create_connection(('127.0.0.1',port),timeout=1)
            connection.sendall(b'POST /api/v1/patches/check HTTP/1.1\r\nHost: localhost\r\n')
            connection.settimeout(.1)
            with self.assertRaises(TimeoutError): connection.recv(1)
            release.set()
            # Keep the stalled peer open: cleanup must actively close it, rather
            # than wait indefinitely for BaseHTTPRequestHandler header parsing.
            self.assertTrue(done.wait(2),'Replay cleanup waited for an incomplete HTTP header')
            self.assertEqual(errors,[]); self.assertEqual(requests,[])
        finally:
            release.set()
            if connection: connection.close()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_replay_failed_body_send_cannot_satisfy_response_delivery_evidence(self):
        import http.client
        import socket
        import socketserver
        import urllib.request
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        url=f'http://127.0.0.1:{port}'; requests=[]; attempted=threading.Event()
        body=b'{"signed":"response-not-delivered"}'
        original=socketserver._SocketWriter.write
        def write(stream,data):
            if data==body:
                attempted.set()
                raise BrokenPipeError('Injected replay response body failure')
            return original(stream,data)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with mock.patch.object(socketserver._SocketWriter,'write',write):
            with adversarial.replay_endpoint(url,body,requests):
                request=urllib.request.Request(url+'/api/v1/patches/check',
                    b'{"selection_protocol":2}',method='POST')
                with opener.open(request,timeout=2) as response:
                    with self.assertRaises(http.client.IncompleteRead): response.read()
                self.assertTrue(attempted.wait(1))
        self.assertEqual(requests,[],'Failed response send was accepted as successful replay evidence')

    def test_deadline_uses_platform_clock_until_actually_past_deadline(self):
        readings=iter((100,99,100,101,101.02)); sleeps=[]
        self.assertEqual(adversarial.wait_past_deadline(100,now=lambda:next(readings),sleep=sleeps.append),101.02)
        self.assertEqual(len(sleeps),4)
        for value in (float('nan'),float('inf'),'100',True):
            with self.subTest(value=value),self.assertRaises(ValueError):
                adversarial.wait_past_deadline(value)
            with self.subTest(clock=value),self.assertRaises(ValueError):
                adversarial.wait_past_deadline(100,now=lambda:value)

    def test_actual_windows_launch_replay_fallback_zero_keys_and_automatic_free(self):
        native=windows_variant_e2e.native
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); bundle=root/'released'; bundle.mkdir(); (bundle/'app.exe').write_bytes(b'app')
            lifecycle=windows_variant_e2e.Lifecycle.__new__(windows_variant_e2e.Lifecycle)
            lifecycle.work=root; lifecycle.bundle=bundle; lifecycle.bundle_hashes=native.tree_hashes(bundle)
            lifecycle.runner=bundle/'app.exe'; lifecycle.key_cache=root/'dpapi'; lifecycle.data_directory=root/'data'
            lifecycle.provider_receipt=root/'provider.jsonl'; lifecycle.session_id='session'; lifecycle.sentinel='sentinel'
            paid=identity(grant(epoch='4',platform='windows'))
            free_current=dict(paid,variant_id='free',license_type='free',patch_number=None)
            lifecycle.last_current_identity=free_current; lifecycle.previous_data=None
            lifecycle.protocol=v2.NativeVariantSession(APP,17,'1.0.0+1','windows','x86_64','session',mock.Mock())
            lifecycle.protocol.installation_id=INSTALLATION; lifecycle.protocol.observed=paid
            lifecycle.protocol.contexts=[v2.context_for(grant(epoch='4',platform='windows'))]
            lifecycle.observer=SimpleNamespace(url='http://127.0.0.1:12345/frame')
            lifecycle.api=lifecycle.provision=None; lifecycle.secrets={'provision':'private-token'}
            lifecycle.logs=[]; lifecycle.receipts={}; lifecycle.receipt_lock=threading.Lock()
            lifecycle.args=SimpleNamespace(runtime_timeout=1); lifecycle.stage=mock.Mock()
            release_key=[False]; automatic=[False]
            def spawn(argv,**kwargs):
                env=kwargs['env']; nonce=env['SHOREBIRD_E2E_RECEIPT']
                self.assertEqual(env['SHOREBIRD_VARIANT_CHECK'],'1')
                if not automatic[0]: self.assertNotIn('SHOREBIRD_E2E_PROVISION_URL',env)
                accepted=identity(grant('free','5',platform='windows')) if automatic[0] else paid
                value=report('free',free_current,accepted,previous=lifecycle.previous_data,nonce=nonce,pid=130)
                value.update(stage='checked',check_status='upToDate',check_error=None)
                if not automatic[0]:
                    value['selection']['next_boot']={'variant_id':'free','license_type':'free','patch_number':None,'reason':'expired'}
                    value['selection']['restart_required']=False
                lifecycle.receipts[nonce]=value
                if release_key[0]: lifecycle.provider_receipt.write_text(json.dumps(dict(paid,nonce=nonce,pid=130))+'\n')
                return mock.Mock(pid=130,poll=mock.Mock(return_value=None))
            lifecycle.job=mock.Mock(spawn=mock.Mock(side_effect=spawn))
            try:
                lifecycle.launch_variant('rejected','free',check=True,replay=True,cold=True,fallback=True,fallback_reason='expired')
                self.assertEqual(lifecycle.protocol.latest,paid)
                release_key[0]=True
                with self.assertRaisesRegex(native.FixtureError,'paid key release'):
                    lifecycle.launch_variant('bad-key-release','free',check=True,replay=True,cold=True,fallback=True,fallback_reason='expired')
                lifecycle.provider_receipt.unlink(); release_key[0]=False
                lifecycle.api=object(); lifecycle.provision=SimpleNamespace(url='http://127.0.0.1:23456/key')
                with self.assertRaisesRegex(native.FixtureError,'stopped real API'):
                    lifecycle.launch_variant('bad-overlap','free',check=True,replay=True)
                automatic[0]=True
                lifecycle.launch_variant('expired-server-free','free',check=True,online=True,automatic_free=True)
                self.assertEqual(lifecycle.protocol.latest,identity(grant('free','5',platform='windows')))
                self.assertEqual(lifecycle.last_current_identity,free_current)
            finally:
                for log in lifecycle.logs: log.close()

    def test_actual_adversarial_wrapper_flow_uses_real_controls_without_journal_restore(self):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            lifecycle=windows_variant_e2e.Lifecycle.__new__(windows_variant_e2e.Lifecycle)
            lifecycle.cache=self.make_cache(root/'cache'); lifecycle.artifacts={'pro':{'number':2}}
            lifecycle.api_url='http://127.0.0.1:12345'; lifecycle.api=lifecycle.provision=None
            lifecycle.scope={'base_artifact_hash':'a'*64,'host_abi_hash':None,'runtime_profile':'native-aot'}
            lifecycle.protocol=v2.NativeVariantSession(APP,17,'1.0.0+1','windows','x86_64','session',mock.Mock())
            lifecycle.protocol.installation_id=INSTALLATION
            lifecycle.args=SimpleNamespace(lease_seconds=90); lifecycle.stage=mock.Mock()
            clock=[1000]; calls=[]; saved_cipher=(lifecycle.cache/'patches/2/transport.aesgcm').read_bytes()
            journal=adversarial.cache_snapshot(lifecycle.cache)['journals']
            def assign(variant):
                lifecycle.protocol.observed=identity(grant(variant,str(int(lifecycle.protocol.latest['epoch'])+1)))
            def switch(label,target,current,**lease):
                calls.append(('switch',label,target,current,lease)); assign(target)
                # This models a real server update only at the explicit repair.
                # No runtime/cache primitive in the fixture restores these bytes.
                if label=='ciphertext-same-pro-redownload':
                    (lifecycle.cache/'patches/2/transport.aesgcm').write_bytes(saved_cipher)
                lifecycle.api=lifecycle.provision=None
                return {'check_status':'outdated'}
            lifecycle.switch_variant=mock.Mock(side_effect=switch)
            def online(): lifecycle.api=lifecycle.provision=object()
            def offline(): lifecycle.api=lifecycle.provision=None
            lifecycle.online=mock.Mock(side_effect=online); lifecycle.go_offline=mock.Mock(side_effect=offline)
            lifecycle.stop_app=mock.Mock()
            def launch(label,variant,**kwargs):
                calls.append(('launch',label,variant,kwargs))
                if kwargs.get('automatic_free'): assign('free')
                if kwargs.get('replay'):
                    self.assertIsNone(lifecycle.api); self.assertIsNone(lifecycle.provision)
                if label=='platform-clock-expired-free': self.assertGreater(clock[0],1090)
                return {}
            lifecycle.launch_variant=mock.Mock(side_effect=launch)
            def request(method,path,body=None):
                if path.endswith('/patches'): return {'patches':[{'patch_number':3,'patch_id':42}]}
                if method=='POST' and path.endswith(('/rollback','/rollforward')):
                    calls.append(('patch-control',path)); return {'changed':True}
                result=grant(epoch=lifecycle.protocol.latest['epoch'])
                result['delivery']={'download_url':'https://private.invalid/blob?token=bearer-secret'}
                import base64
                result['authentication']={'signature':'ActualServerTestDouble',
                    'payload':base64.b64encode(json.dumps({'request':{'nonce':body['nonce']},
                        'selection_digest':result['selection_digest'],'selection':result['selection'],
                        'delivery':result['delivery']}).encode()).decode()}
                return result
            lifecycle.request=mock.Mock(side_effect=request)
            @contextmanager
            def replay(url,body,requests):
                self.assertIsNone(lifecycle.api); self.assertIsNone(lifecycle.provision)
                requests.append({'nonce':'fresh-request','accepted_selection_epoch':lifecycle.protocol.latest['epoch'],
                    'accepted_selection_digest':lifecycle.protocol.latest['digest']})
                yield
            def wait(deadline): clock[0]=deadline+2; return clock[0]
            with mock.patch.object(desktop,'time',SimpleNamespace(time=lambda:clock[0])), \
                 mock.patch.object(adversarial,'wait_past_deadline',side_effect=wait), \
                 mock.patch.object(adversarial,'replay_endpoint',side_effect=replay):
                lifecycle.adversarial_cases()
            self.assertEqual(adversarial.cache_snapshot(lifecycle.cache)['journals'],journal)
            labels=[row[1] for row in calls]
            for required in ('ciphertext-offline-free','ciphertext-signed-free','ciphertext-same-pro-redownload',
                'cached-signature-still-valid-pro','platform-clock-expired-free','old-signed-response-rejected',
                'corrupt-stale-response-combined-rejected','expired-server-signed-free','rollback-server-signed-free',
                'rollforward-alone-still-free','explicit-enterprise-restored','adversarial-final-offline-free'):
                self.assertIn(required,labels)
            self.assertLess(labels.index('rollforward-alone-still-free'),labels.index('explicit-enterprise-restored'))
            self.assertEqual(calls[-1][1],'adversarial-final-offline-free')
            lease=next(row[4] for row in calls if row[1]=='actual-leased-pro')
            self.assertEqual(lease,{'offline_valid_until':'1090'})
            self.assertEqual(sum(row[0]=='patch-control' for row in calls),2)
            exported=repr(lifecycle.stage.call_args_list)
            for private in (INSTALLATION,'bearer-secret','private.invalid','ActualServerTestDouble','fresh-request'):
                self.assertNotIn(private,exported)


class ProviderAdapterTest(unittest.TestCase):
    def test_cpp_registration_and_context_before_cached_key_release(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        self.assertIsNotNone(compiler,'C++ compiler required for pure stub-cache adapter checks')
        tool=Path(__file__).parent
        for target in ('linux','windows'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                shutil.copy2(tool/(target+'_variant_provider.cpp'),root/'adapter.cpp')
                shutil.copy2(tool/'native_variant_context.h',root/'native_variant_context.h')
                canonical=tool.parents[2]/'flutter/engine/src/flutter/shell/common/shorebird/shorebird_patch_encryption.h'
                shutil.copy2(canonical,root/'native_variant_api.h')
                public=root/('flutter_linux/flutter_linux.h' if target=='linux' else 'flutter_windows.h')
                public.parent.mkdir(exist_ok=True)
                public.write_text('#include <cstring>\ninline unsigned long GetCurrentProcessId(){return 10;}\ninline void SecureZeroMemory(void* p,size_t n){std::memset(p,0,n);}\n')
                (root/'legacy_provider.cpp').write_text('''
#include <cstring>
static int cached_lookups=0;
ERASE_DEFINITION
static bool LookupKey(const char*,const char*,uint8_t* out,size_t cap,size_t* written){
 ++cached_lookups;if(cap<32)return false;std::memset(out,7,32);*written=32;return true;}
bool RegisterTARGETNativeCacheTestProvider(){return shorebird_set_patch_decryption_key_provider(LookupKey);}
'''.replace('TARGET',target.title()).replace('ERASE_DEFINITION',
    'static void Erase(void* p,size_t n){std::memset(p,0,n);}' if target=='linux' else ''))
                (root/'main.cpp').write_text('''
#include "adapter.cpp"
static FlutterShorebirdPatchDecryptionKeyProviderV2 registered=nullptr;
extern "C" bool shorebird_set_patch_decryption_key_provider_v2(FlutterShorebirdPatchDecryptionKeyProviderV2 p){registered=p;return true;}
int main(){
 if(!RegisterTARGETNativeCacheTestProvider()||!registered)return 1;
 uint8_t key[32];size_t written=123;
 if(registered(std::getenv("KEY_ID"),std::getenv("AAD"),"{}",key,32,&written)||written||cached_lookups)return 2;
 if(!registered(std::getenv("KEY_ID"),std::getenv("AAD"),std::getenv("CONTEXT"),key,32,&written)||written!=32||cached_lookups!=1)return 3;
 for(const char* name:{"EXPIRED_CONTEXT","FUTURE_CONTEXT","DUPLICATE_CONTEXT"}){
  written=123;
  if(registered(std::getenv("KEY_ID"),std::getenv("AAD"),std::getenv(name),key,32,&written)||written||cached_lookups!=1)return 4;
 }
 if(registered(std::getenv("KEY_ID"),std::getenv("FOREIGN_AAD"),std::getenv("CONTEXT"),key,32,&written)||written||cached_lookups!=1)return 5;
 return 0;
}
'''.replace('TARGET',target.title()))
                built=subprocess.run([compiler,'-std=c++14' if target=='linux' else '-std=c++17','-Wall','-Wextra','-Werror','-I',str(root),str(root/'main.cpp'),'-o',str(root/'unit')],capture_output=True,text=True)
                self.assertEqual(built.returncode,0,built.stdout+built.stderr)
                context=v2.context_for(grant(platform=target))
                expired=copy.deepcopy(context); expired['selection']['offline_valid_until']=str(int(time.time())-1)
                future=copy.deepcopy(context); future['selection']['not_before']=str(int(time.time())+60)
                aad=v2.canonical_aad(aad_context(platform=target))
                key_id,_=v2.delivery_material(b'fixture',aad)
                env=dict(os.environ,KEY_ID=key_id,AAD=aad,CONTEXT=json.dumps(context),
                    SHOREBIRD_VARIANT_CONTEXTS=json.dumps([context,expired,future]),SHOREBIRD_E2E_RECEIPT='abc',
                    EXPIRED_CONTEXT=json.dumps(expired),FUTURE_CONTEXT=json.dumps(future),
                    DUPLICATE_CONTEXT=json.dumps(context).replace('"version": 2','"version":2,"version":2',1),
                    FOREIGN_AAD=aad.replace(CLIENT_HASH,'d'*64),
                    SHOREBIRD_VARIANT_PROVIDER_RECEIPT=str(root/'provider.jsonl'))
                result=subprocess.run([str(root/'unit')],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                receipt=json.loads((root/'provider.jsonl').read_text())
                self.assertEqual(receipt['variant_id'],'pro')
                self.assertEqual(receipt['digest'],context['selection_digest'])
                self.assertNotIn('key_out',receipt)


if __name__=='__main__': unittest.main()

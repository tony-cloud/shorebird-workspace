#!/usr/bin/env python3
"""Synthetic consumer controls; no network, app/server launch, signing or build."""
import base64
import copy
from contextlib import closing, contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0,str(Path(__file__).parent))
import macos_app_lifecycle_consumer as consumer
import test_macos_app_prepared_transport as fixtures

transport=consumer.transport
WORKSPACE=Path(__file__).resolve().parents[1]
IMAGE={'schema':'macos-app-native-image-v1','image_os':'macos15','image_version':'20261102.1','runner_image_commit':'1'*40}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*_): self.close()


class Opener:
    def __init__(self,*responses): self.responses=list(responses); self.requests=[]
    def open(self,request,timeout):
        self.requests.append(request)
        value=self.responses.pop(0)
        if isinstance(value,BaseException): raise value
        return Response(value)


class FakeOwner:
    def __init__(self,callback=None,**kwargs): self.callback=callback; self.owned={}; self.closed=False; self.spawned=[]
    def spawn(self,command,**kwargs):
        pid=2000+len(self.spawned)
        process=SimpleNamespace(pid=pid,wait=mock.Mock(return_value=0),poll=mock.Mock(return_value=0))
        self.owned[pid]=process; self.spawned.append((command,kwargs,process))
        if self.callback: self.callback(process,kwargs)
        return process
    def stop(self,process): self.owned.pop(process.pid)
    def close(self): self.owned.clear(); self.closed=True


class ConsumerTest(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.TransportTest('test_round_trip_preserves_links_modes_and_producer_roots')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.seed['variant_keys']={v:transport.sha(('private-fixture-'+v).encode()) for v in ('pro','enterprise')}
        # Match the actual Go producer's app/release/patch/wire storage namespace.
        with closing(sqlite3.connect(self.fixture.payload/'state.sqlite')) as db,db:
            db.execute('ALTER TABLE patch_artifacts ADD COLUMN hash_signature TEXT')
            db.execute('UPDATE patch_artifacts SET hash_signature=?',(base64.b64encode(b's'*256).decode(),))
            for number,key in db.execute('SELECT p.number,a.storage_key FROM patches p JOIN patch_artifacts a ON a.patch_id=p.id').fetchall():
                replacement=f'apps/{self.fixture.state["app_id"]}/releases/1/patches/{number}/macos/x86_64/dlc.vmcode'
                old=self.fixture.payload/'objects/releases'/key; new=self.fixture.payload/'objects/releases'/replacement
                new.parent.mkdir(parents=True,mode=0o700); old.rename(new)
                self.fixture.objects['releases/'+replacement]=self.fixture.objects.pop('releases/'+key)
                db.execute('UPDATE patch_artifacts SET storage_key=? WHERE storage_key=?',(replacement,key))
        self.fixture.put_json('private-bootstrap.json',self.fixture.seed); self.fixture.seal()
        self.directory=self.fixture.directory; self.state=self.fixture.state
        self.home=self.directory/'home'; self.home.mkdir()
        self.home_patch=mock.patch.object(Path,'home',return_value=self.home)
        self.home_patch.start(); self.addCleanup(self.home_patch.stop)
        self.args=SimpleNamespace(execute=True,workspace=WORKSPACE,source_commit=fixtures.COMMIT,
            components=self.directory/'components.json',components_sha256='1'*64,certificate_sha1=fixtures.CERTIFICATE,
            archive=self.directory/'prepared.zip',sha256='2'*64,python=Path(sys.executable).resolve(),python_sha256='3'*64,
            image=self.directory/'image.json',image_sha256='4'*64,evidence=self.directory/'consumer')
        self.args.components.write_bytes(transport.encoded(self.fixture.pins)); self.args.components_sha256=transport.inputs.digest(self.args.components)
        self.args.image.write_bytes(transport.encoded(IMAGE)); self.args.image_sha256=transport.inputs.digest(self.args.image)

    def release(self,payload=b'prepared-private-bytes'):
        digest=transport.sha(payload)
        return {'id':1,'draft':True,'published_at':None,'assets':[{'id':2,'name':consumer.ASSET,'state':'uploaded',
            'content_type':'application/zip','size':len(payload),'digest':'sha256:'+digest,'created_at':'fixed','updated_at':'fixed'}]}

    def test_draft_download_pins_membership_before_after_and_private_bytes(self):
        payload=b'prepared-private-bytes'; data=self.release(payload)
        opener=Opener(transport.encoded(data),payload,transport.encoded(data))
        output=self.directory/'private.zip'; evidence=self.directory/'download'
        with mock.patch.dict(os.environ,{'GITHUB_TOKEN':'DO-NOT-EXPORT','HTTPS_PROXY':'https://secret-proxy'}), mock.patch.object(
            consumer.urllib.request,'build_opener',return_value=opener) as builder:
            report=consumer.download('owner/repo',1,2,transport.sha(payload),output,evidence)
        self.assertEqual(output.read_bytes(),payload); self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(len(opener.requests),3)
        self.assertTrue(all(r.full_url.startswith('https://api.github.com/repos/owner/repo/releases/') for r in opener.requests))
        self.assertEqual(builder.call_args.args[0].proxies,{})
        self.assertNotIn('DO-NOT-EXPORT',(evidence/'download.json').read_text())
        self.assertEqual(report['status'],'download_verified_runtime_pending')

    def test_download_rejects_published_changed_duplicate_incomplete_and_wrong_hash(self):
        payload=b'private-bytes'; valid=self.release(payload)
        for role in ('published','duplicate','changed','incomplete','wrong_hash'):
            before=copy.deepcopy(valid); after=copy.deepcopy(valid); content=payload
            if role=='published': before['draft']=False
            if role=='duplicate': before['assets'].append(dict(before['assets'][0]))
            if role=='changed': after['assets'][0]['updated_at']='changed'
            if role=='incomplete': content=payload[:-1]
            if role=='wrong_hash': content=b'x'*len(payload)
            opener=Opener(transport.encoded(before),content,transport.encoded(after))
            output=self.directory/(role+'.zip'); evidence=self.directory/role
            with self.subTest(role=role), mock.patch.dict(os.environ,{'GITHUB_TOKEN':'private-token'}), mock.patch.object(
                consumer.urllib.request,'build_opener',return_value=opener), self.assertRaises(ValueError):
                consumer.download('owner/repo',1,2,transport.sha(payload),output,evidence)
            self.assertFalse(output.exists()); self.assertNotIn('private-token',(evidence/'download.json').read_text())

    def test_asset_redirect_https_exact_hosts_and_auth_removal_is_irreversible(self):
        redirect=consumer.SafeRedirect(); request=urllib.request.Request('https://api.github.com/releases/assets/2',headers={'Authorization':'Bearer private'})
        storage=redirect.redirect_request(request,None,302,'',{},'https://release-assets.githubusercontent.com/path?sig=secret')
        self.assertIsNone(storage.get_header('Authorization'))
        returned=redirect.redirect_request(storage,None,302,'',{},'https://api.github.com/path')
        self.assertIsNone(returned.get_header('Authorization'))
        for url in ('http://release-assets.githubusercontent.com/path','https://evil.githubusercontent.com/path',
                    'https://api.github.com.evil/path','https://user:secret@api.github.com/path','https://api.github.com:444/path'):
            with self.subTest(url=url),self.assertRaises(ValueError): redirect.redirect_request(request,None,302,'',{},url)

    def test_all_child_environments_are_small_and_preserve_real_home(self):
        poison={'GITHUB_TOKEN':'github','GH_TOKEN':'gh','AWS_SECRET_ACCESS_KEY':'aws','HTTP_PROXY':'proxy',
            'DYLD_INSERT_LIBRARIES':'inject','SHOREBIRD_TOKEN':'api','OPEN_SHOREBIRD_TEST_KEY_HEX':'key','HOME':'/fake/home'}
        with mock.patch.dict(os.environ,poison):
            clean=consumer.clean_environment(IMAGE)
            with consumer.clean_process_environment(IMAGE): self.assertEqual(dict(os.environ),clean)
            self.assertEqual(os.environ['GITHUB_TOKEN'],'github')
        for name in poison:
            if name!='HOME': self.assertNotIn(name,clean)
        self.assertEqual(clean['HOME'],consumer.pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(clean['PATH'],'/usr/bin:/bin:/usr/sbin:/sbin')

    def test_fixed_worker_command_has_no_shell_cases_or_manifest_commands(self):
        command=consumer.fixed_worker_command(self.args,self.args.python)
        self.assertEqual(command[:4],[str(self.args.python),'-I',str(WORKSPACE/consumer.SELF),'_worker'])
        self.assertIn('--execute',command)
        for forbidden in ('--sign','--cases','--compiler','--shell','--command'): self.assertNotIn(forbidden,command)
        self.assertNotIn('GITHUB_TOKEN',' '.join(command))
        self.assertEqual(command[command.index('--archive')+1],str(self.args.archive))

    def test_native_context_checks_image_before_git_and_scrubs_git_environment(self):
        with mock.patch.dict(os.environ,{'ImageOS':IMAGE['image_os'],'ImageVersion':'wrong'}), mock.patch.object(consumer,'source_identity') as identity:
            with self.assertRaises(ValueError): consumer.native_context(WORKSPACE,fixtures.COMMIT,self.fixture.pins,
                self.args.python,self.args.python_sha256,self.args.image,self.args.image_sha256)
            identity.assert_not_called()
        def identity(*_):
            self.assertNotIn('GITHUB_TOKEN',os.environ)
            return self.fixture.source
        native=SimpleNamespace(require_native_host=mock.Mock())
        with mock.patch.dict(os.environ,{'ImageOS':IMAGE['image_os'],'ImageVersion':IMAGE['image_version'],'GITHUB_TOKEN':'do-not-pass'}), \
            mock.patch.object(consumer,'source_identity',side_effect=identity),mock.patch.object(transport,'fixture_modules') as modules, \
            mock.patch.object(transport.inputs,'digest',return_value=self.args.python_sha256),mock.patch.object(transport.inputs,'macho'):
            modules.return_value.__enter__.return_value=(None,native)
            consumer.native_context(WORKSPACE,fixtures.COMMIT,self.fixture.pins,self.args.python,self.args.python_sha256,self.args.image,self.args.image_sha256)
            native.require_native_host.assert_called_once_with({'arch':'x64'})
        self.assertNotIn('ImageOS',consumer.clean_environment())

    def copy_work(self):
        shutil.copytree(self.fixture.payload,self.fixture.root,symlinks=True)
        self.addCleanup(lambda: shutil.rmtree(self.fixture.root) if self.fixture.root.exists() else None)
        (self.fixture.root/'secrets.json').write_bytes(transport.encoded({**self.fixture.seed,'jwt_secret':'fresh'}))
        return self.fixture.root

    def modules_with_owner(self,owner):
        original=transport.fixture_modules
        @contextmanager
        def modules(workspace):
            with original(workspace) as (fixture,lifecycle):
                with mock.patch.object(lifecycle,'OwnedHostProcesses',return_value=owner): yield fixture,lifecycle
        return modules

    def receipt_callback(self,process,kwargs,result='absent'):
        env=kwargs['env']; account=env['OPEN_SHOREBIRD_TEST_KEY_ID']
        receipt={'schema':'macos-variant-key-control-v2','session':self.state['session'],'bundle_id':self.state['bundle_id'],
            'service':self.state['key_service'],'account':account,'action':'delete','result':result,'pid':process.pid,'time':time.time()}
        path=self.home/'Library/Containers'/self.state['bundle_id']/'Data/Library/Application Support/shorebird-macos-fixture'/('key-control-'+account+'.json')
        transport.write(path,transport.encoded(receipt))

    def test_native_control_smoke_validates_both_absent_receipts_and_marker(self):
        work=self.copy_work(); evidence=self.directory/'smoke'; evidence.mkdir()
        owner=FakeOwner(self.receipt_callback)
        with mock.patch.object(transport,'fixture_modules',self.modules_with_owner(owner)),consumer.clean_process_environment(IMAGE):
            report=consumer.smoke(WORKSPACE,work,self.state,evidence,IMAGE)
        self.assertEqual(report['status'],'capability_smoke_passed'); self.assertFalse(report['runtime_acceptance'])
        self.assertEqual([r['variant'] for r in report['receipts']],['pro','enterprise']); self.assertTrue(owner.closed)
        self.assertEqual(len(owner.spawned),2)
        for command,kwargs,_ in owner.spawned:
            self.assertEqual(command,[str(work/'released/Fixture.app/Contents/MacOS/Fixture')])
            self.assertNotIn('OPEN_SHOREBIRD_TEST_KEY_HEX',kwargs['env']); self.assertNotIn('GITHUB_TOKEN',kwargs['env'])
        self.assertFalse(list((self.home/'Library/Containers').rglob('capability-*')))

    def test_smoke_preexisting_key_or_stale_wrong_pid_receipt_stops_before_lifecycle(self):
        for failure in ('deleted','pid','stale'):
            work=self.copy_work() if not self.fixture.root.exists() else self.fixture.root
            shutil.rmtree(self.home/'Library',ignore_errors=True)
            evidence=self.directory/('smoke-'+failure); evidence.mkdir()
            def callback(process,kwargs):
                self.receipt_callback(process,kwargs,result='deleted' if failure=='deleted' else 'absent')
                if failure!='deleted':
                    path=next((self.home/'Library/Containers').rglob('key-control-*.json'))
                    value=json.loads(path.read_bytes()); value['pid']=1 if failure=='pid' else process.pid
                    if failure=='stale': value['time']=1
                    path.write_bytes(transport.encoded(value))
            owner=FakeOwner(callback)
            with self.subTest(failure=failure),mock.patch.object(transport,'fixture_modules',self.modules_with_owner(owner)),consumer.clean_process_environment(IMAGE),self.assertRaises(ValueError):
                consumer.smoke(WORKSPACE,work,self.state,evidence,IMAGE)
            self.assertTrue(owner.closed); self.assertEqual(len(owner.spawned),1)

    def test_smoke_marker_permission_failure_is_not_success(self):
        work=self.copy_work(); evidence=self.directory/'denied'; evidence.mkdir(); owner=FakeOwner(self.receipt_callback)
        original=transport.write
        def denied(path,data,*args):
            if path.name.startswith('capability-'): raise PermissionError(13,'secret-path')
            return original(path,data,*args)
        with mock.patch.object(transport,'fixture_modules',self.modules_with_owner(owner)),mock.patch.object(transport,'write',side_effect=denied),consumer.clean_process_environment(IMAGE),self.assertRaises(PermissionError):
            consumer.smoke(WORKSPACE,work,self.state,evidence,IMAGE)
        self.assertTrue(owner.closed)

    def test_receipt_allowlist_rejects_secret_extra_and_bad_identity(self):
        receipt={'schema':'macos-variant-key-control-v2','session':self.state['session'],'bundle_id':self.state['bundle_id'],
            'service':self.state['key_service'],'account':self.state['key_ids']['pro'],'action':'delete','result':'absent','pid':10,'time':100}
        good=consumer.validate_receipt(self.state,receipt,variant='pro',pid=10,started=99,ended=101,absent_only=True)
        self.assertTrue(good['identity_verified'])
        for change in ({'token':'private'},{'pid':11},{'time':90},{'result':'deleted'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                consumer.validate_receipt(self.state,{**receipt,**change},variant='pro',pid=10,started=99,ended=101,absent_only=True)

    def test_case_contract_exact_62_including_final_native_deletions(self):
        spec=consumer.case_spec(); self.assertEqual(len(spec),62)
        self.assertEqual([r[0] for r in spec[-3:]],['final-offline-free','final-delete-pro','final-delete-enterprise'])
        self.assertEqual(sum(v is None for _,_,v in spec),8)
        self.assertEqual(len({r[0] for r in spec}),62)

    def ffi(self,fixture,variant):
        base=variant=='free'
        return {'schema':'full-replacement-ffi-v1','phase':'base' if base else 'update','license':'free' if base else 'pro',
            'pro_feature_enabled':not base,'structural_verified':True,'ffi_verified':True,
            'rendered_text':'license:free full-replacement:verified value:10 full-ffi:v1-base' if base else 'license:pro full-replacement:verified value:28 full-ffi:v2-update',
            'checks':list(fixture.v1.shared.FFI_BASE_CHECKS if base else fixture.v1.shared.FFI_UPDATE_CHECKS),'workers_started':0 if base else 2,
            'workers_joined':True,'callbacks_closed':True,'static_callbacks_retained':0 if base else 1}

    def entry(self,index,fixture,previous=None):
        role,pattern,variant=consumer.case_spec()[index]; started=time.time()-.1; pid=4000+index
        entry={'label':role,'started':started,'pid':pid,'validated':True,'signed_bundle_unchanged':True,'expected_variant':variant}
        if 'transition-' in role:
            target=role.split('-')[2]; suffix=role.rsplit('-',1)[1]
            entry['label']='switch-'+target+'-e'+str(10+int(role.split('-')[1]))+'-'+suffix
        if role=='initial-pro-key': entry['label']='key-provision-pro-3'
        if role=='initial-enterprise-key': entry['label']='key-provision-enterprise-4'
        if variant is None:
            key,action=consumer.KEY_ROLES[role]
            entry['key_control']={'schema':'macos-variant-key-control-v2','session':self.state['session'],'bundle_id':self.state['bundle_id'],
                'service':self.state['key_service'],'account':self.state['key_ids'][key],'action':action,
                'result':'absent' if action=='delete' else 'stored','pid':pid,'time':started+.01}
            return entry
        def identity(value): return {'epoch':'7','digest':'d'*64,'variant_id':value,'license_type':value,'patch_number':fixture.EXPECTED_NUMBERS[value]}
        current=identity(variant); accepted=identity('pro') if role in ('missing-key-free','wrong-key-free') else current
        if role.startswith('transition-'):
            accepted=identity(role.split('-')[2]); accepted['epoch']=str(10+int(role.split('-')[1]))
            current={**current,'epoch':str(int(accepted['epoch'])-1) if role.endswith('-check') else accepted['epoch']}
        history=(previous['history'] if previous else [])+[variant]
        report={'schema':fixture.SCHEMA,'session':self.state['session'],'variant':variant,'pid':pid,'started':started+.01,
            'installation_id':'raw-installation-do-not-export','new_code_result':{'free':'free:base','pro':'pro:42','enterprise':'enterprise:24'}[variant],
            'stage':'boot','update_status':None,'error':None,'selection':{'version':2,'current':current,'accepted':accepted,'restart_required':False,
                'next_boot':{'variant_id':variant,'license_type':variant,'patch_number':fixture.EXPECTED_NUMBERS[variant],'reason':'ready'}},
            'data':{'session':self.state['session'],'sentinel':self.state['sentinel'],'schema':3,'launches':len(history),'history':history,
                    'private_extra':'do-not-export'}}
        scope={'app_id':self.state['app_id'],'release_version':self.state['release_version'],'platform':'macos','arch':'x86_64',
            'runtime_profile':'interpreter-full','client_id_hash':transport.sha(report['installation_id'].encode())}
        events=[]; expected=accepted if role in ('missing-key-free','wrong-key-free') else current
        if variant!='free' or role in ('missing-key-free','wrong-key-free'):
            missing=role=='missing-key-free'
            events=[{'sequence':1,'outcome':'refused' if missing else 'released','reason':'key_unavailable' if missing else 'key_available',
                'time':started+.02,'scope':scope,'epoch':expected['epoch'],'selection_digest':expected['digest'],'variant_id':expected['variant_id'],
                'license_type':expected['license_type'],'patch_number':expected['patch_number'],'key_id':self.state['key_ids'][expected['variant_id']]}]
        provider={'schema':'macos-variant-provider-ledger-v2','session':self.state['session'],'bundle_id':self.state['bundle_id'],'pid':pid,
            'nonce':'nonce','started':started+.01,'time':started+.03,'events':events,'overflow':False}
        entry.update(report=report,ffi={'verification':self.ffi(fixture,variant)},provider=provider,provider_nonce='nonce',
            provider_counts={'attempts':len(events),'successful_releases':sum(e['outcome']=='released' for e in events),'refused':sum(e['outcome']=='refused' for e in events)},
            container={'schema':'macos-interpreter-container-v1','bundle_id':self.state['bundle_id'],'pid':pid,'time':started+.01,
                'home':str(fixture.v1.container_root(self.state)/'Data'),'status_path':str(fixture.v1.container_file(self.state,'ffi-status.json'))},
            cache={'private_journal':'do-not-export'},raw_reports={'private_bearer':'do-not-export'})
        return entry

    def test_public_projection_validates_original_ffi_provider_container_and_filters_raw_secrets(self):
        with transport.fixture_modules(WORKSPACE) as (fixture,lifecycle):
            entry=self.entry(0,fixture)
            projected,previous=consumer.public_launch(entry,self.state,0,fixture,lifecycle,None)
            raw=transport.encoded(projected).decode()
            for secret in ('raw-installation-do-not-export','private_extra','private_journal','private_bearer',self.state['sentinel'],self.state['session']): self.assertNotIn(secret,raw)
            self.assertEqual(projected['ffi']['check_count'],3); self.assertTrue(projected['provider']['identity_verified'])
            self.assertEqual(previous['history'],['free'])
            for mutate in (lambda e:e['ffi']['verification']['checks'].pop(),lambda e:e['report'].update(new_code_result='wrong'),
                           lambda e:e['provider'].update(pid=1),lambda e:e['container'].update(home='/foreign'),
                           lambda e:e.update(label='extra-case'),lambda e:e.update(validated=False),
                           lambda e:e['report']['selection']['next_boot'].update(reason='private secret text')):
                invalid=copy.deepcopy(entry); mutate(invalid)
                with self.subTest(mutate=mutate),self.assertRaises(ValueError): consumer.public_launch(invalid,self.state,0,fixture,lifecycle,None)

    def test_public_data_continuity_and_dynamic_epoch_are_bound(self):
        with transport.fixture_modules(WORKSPACE) as (fixture,lifecycle):
            first=self.entry(0,fixture); _,previous=consumer.public_launch(first,self.state,0,fixture,lifecycle,None)
            second=self.entry(1,fixture,previous); consumer.public_launch(second,self.state,1,fixture,lifecycle,previous)
            second['report']['data']['history']=['pro','free']
            with self.assertRaises(ValueError): consumer.public_launch(second,self.state,1,fixture,lifecycle,previous)
            transition=self.entry(5,fixture); transition['label']='switch-pro-e8-check'
            with self.assertRaises(ValueError): consumer.public_launch(transition,self.state,5,fixture,lifecycle,None)

    def test_failure_public_error_never_exports_exception_message(self):
        error=PermissionError(13,'Bearer secret installation URL /Users/private')
        self.assertEqual(consumer.public_error(error,'protected_container_access_unproven'),{
            'phase':'protected_container_access_unproven','error_type':'PermissionError','errno':13})
        self.assertNotIn('Bearer',transport.encoded(consumer.public_error(error,'smoke')).decode())

    def test_cleanup_metadata_remainder_is_incomplete(self):
        result=consumer.cleanup_public({'all_variant_keys_deleted':True,'bootstrap_secrets_removed':True,'owned_app_removed':True,
            'container_payload_removed':True,'container_removed':False,'cleanup_complete':False,
            'os_metadata_shell_remaining':'/Users/private/container','metadata_removal_errno':1})
        self.assertFalse(result['cleanup_complete']); self.assertTrue(result['os_metadata_remainder'])
        self.assertNotIn('/Users',transport.encoded(result).decode())

    def test_export_inventory_contains_only_authored_public_json(self):
        public=self.directory/'public'; public.mkdir()
        report={'schema':consumer.SCHEMA,'status':'runtime_failed','runtime_acceptance':False,'error':consumer.public_error(ValueError('private'),'admission')}
        consumer.write_public(public,report)
        inventory=transport.json_file(public,'inventory.json')
        self.assertEqual(set(p.name for p in public.iterdir()),{'public.json','inventory.json'})
        self.assertEqual(set(inventory['files']),{'public.json'})
        self.assertEqual(inventory['files']['public.json']['sha256'],transport.inputs.digest(public/'public.json'))

    def worker_mocks(self,mode='passed'):
        work=self.copy_work(); test=self; instances=[]; original_modules=transport.fixture_modules
        class Runner:
            def __init__(self,root,state,identity,*,verify_presigned):
                test.assertTrue(verify_presigned); test.assertEqual(identity,fixtures.CERTIFICATE)
                self.state=state; self.launches=[]; self.cleaned=False; self.root=root; self.work=root
                self.result={'runtime_acceptance':False,'signing_mode':'verify_presigned','bundle_verified':True}
                instances.append(self); (root/'lifecycle').mkdir(); (root/'server.log').write_bytes(b'')
                (root/'lifecycle/server-controller.log').write_bytes(b'synthetic controller output')
            def write(self,name,value): transport.write(self.root/'lifecycle'/name,transport.encoded(value))
            def inspection(self,index):
                directory=self.root/'signing-evidence'/str(1700000000000000000+index)
                for leaf in ('inspection.json','bundle-verify.log','runner-exports.log'):
                    transport.write(directory/leaf,b'{}' if leaf.endswith('.json') else b'synthetic tool output')
                for number in range(3):
                    for leaf in ('verify.log','details.log','entitlements.log','certificate.log','certificate-0','arch.log','dependencies.log'):
                        transport.write(directory/(str(number)+'-'+leaf),b'synthetic tool output')
                return {'evidence_directory':str(directory),'signatures':[{}, {}, {}]}
            def launch(self,label,*,entry):
                delta=time.time()-entry['started']
                def freshen(value):
                    if isinstance(value,dict):
                        for key,item in value.items():
                            if key in ('started','time') and type(item) in (int,float): value[key]=item+delta
                            else: freshen(item)
                    elif isinstance(value,list):
                        for item in value: freshen(item)
                freshen(entry)
                self.launches.append(entry)
                index=60 if label=='final-delete-pro' else 61 if label=='final-delete-enterprise' else len(self.launches)-1
                role=consumer.case_spec()[index][0]
                with original_modules(WORKSPACE) as (_,lifecycle):
                    catalog=consumer.transfer_catalog(self.root,self.state,test.fixture.manifest)
                    probe=consumer.cache_probe(lifecycle,self.state,catalog,role,self.paid_cache_baselines)
                    target=consumer.paid_check_target(role)
                    patch=target if consumer.expected_transfer(role,probe) else None
                    cache=lifecycle.cache_root(self.state)
                if role in ('ciphertext-tamper-fallback','signature-tamper-fallback'):
                    shutil.rmtree(cache/'patches/2',ignore_errors=True)
                repair=role=='ciphertext-reauthorized-pro-check'
                if patch and not (mode=='no_repair_transfer' and repair):
                    item=consumer.transfer_catalog(self.root,self.state,test.fixture.manifest)[patch]
                    path=item['path'].replace(test.state['app_id'],'foreign-app') if mode=='wrong_transfer_scope' and repair else item['path']
                    status=403 if mode=='failed_repair_transfer' and repair else 200
                    with (self.root/'server.log').open('ab') as log:
                        log.write(f'2026/11/02 12:00:00 "GET {self.state["api_url"]}{path} HTTP/1.1" from 127.0.0.1:54321 - {status} {item["size"]}B in 1.2ms\n'.encode())
                if target:
                    data=b'reconstructed-target-artifact-'+target.encode(); directory=cache/'patches'/target
                    directory.mkdir(parents=True,exist_ok=True)
                    (directory/'dlc.vmcode').write_bytes(data)
                    (directory/'state.json').write_bytes(transport.encoded({'kind':'Installed','size':len(data),'signature':catalog[target]['hash_signature']}))
                entry['cache']={'private_journal':'do-not-export','files':{path.relative_to(cache).as_posix():{
                    'sha256':transport.inputs.digest(path),'size':path.stat().st_size} for path in cache.rglob('*') if path.is_file()}}
                return entry
            def run_cases(self):
                if mode=='run_failed': raise RuntimeError('private token in raw failure')
                previous=None
                with original_modules(WORKSPACE) as (fixture,_):
                    for index in range(60):
                        entry=test.entry(index,fixture,previous); self.launch(entry['label'],entry=entry)
                        if 'report' in entry: previous={'history':entry['report']['data']['history']}
                    transport.write(self.root/'lifecycle/signed-bundle.json',transport.encoded({**self.inspection(0),'exports':list(fixture.v1.shared.FFI_EXPORTS)}))
                transport.write(self.root/'lifecycle/signed-bundle-tree.json',b'{}')
                transport.write(self.root/'lifecycle/replay-capture.json',transport.encoded({'private_url':'raw-download-url'}))
                for leaf in ('ciphertext-tamper.json','signature-tamper.json','rollback-51.json','rollforward-53.json'):
                    transport.write(self.root/'lifecycle'/leaf,b'{}')
                transport.write(self.root/'lifecycle/lease-expiry.json',transport.encoded({'deadline':1000,'wait_completed':1001,'launch_started':1002}))
                transport.write(self.root/'lifecycle/replay-requests.json',transport.encoded([{
                    'request_body_sha256':'1'*64,'response_body_sha256':'2'*64,'received_at':1000,'completed_at':1001,
                    'request':{'client_id':'raw private identity'},'request_body_base64':'private payload'}]))
                self.result['runtime_acceptance']=True
            def cleanup(self):
                self.cleaned=True; self.state['cleanup_fence']=time.time()-1
                with original_modules(WORKSPACE) as (fixture,_):
                    for index in (60,61):
                        entry=test.entry(index,fixture); self.launch(entry['label'],entry=entry)
                self.result['cleanup']={key:True for key in ('all_variant_keys_deleted','bootstrap_secrets_removed','owned_app_removed',
                    'container_payload_removed','container_removed','cleanup_complete')}
                if mode=='cleanup_failed': self.result['cleanup']['container_removed']=False
                transport.write(self.root/'lifecycle/result.json',transport.encoded({**self.result,'launches':self.launches}))
                for index,entry in enumerate(self.launches,1):
                    directory=self.root/'lifecycle'/entry['label']
                    for leaf in ('launch.json','raw_reports.json','cache.json','app.log'):
                        transport.write(directory/leaf,transport.encoded(entry) if leaf=='launch.json' else
                            transport.encoded(entry['cache']) if leaf=='cache.json' else b'{}')
                    transport.write(directory/'signature.json',transport.encoded(self.inspection(index)))
                    for leaf in (('key-control.json',) if entry['expected_variant'] is None else
                                 ('variant-status.json','ffi-status.json','container.json','user-data.json','provider.json')):
                        transport.write(directory/leaf,transport.encoded(entry.get('report',entry.get('key_control'))))
                for variant in ('pro','enterprise'):
                    transport.write(self.root/'cleanup-evidence'/(variant+'-native-key-deletion.json'),b'{}')
                if mode=='missing_private_evidence': (self.root/'lifecycle/signed-bundle-tree.json').unlink()
                shutil.rmtree(test.home/'Library/Containers'/test.state['bundle_id'],ignore_errors=True)
                return []
        @contextmanager
        def modules(workspace):
            with original_modules(workspace) as (fixture,lifecycle):
                with mock.patch.object(lifecycle,'Lifecycle',Runner): yield fixture,lifecycle
        def extract(*args):
            evidence=args[-1]; evidence.mkdir(mode=0o700); info=work.stat()
            transport.write(evidence/'owned-root.json',transport.encoded({'schema':'macos-app-owned-root-v1','root':str(work),'device':info.st_dev,'inode':info.st_ino}))
        return work,instances,modules,extract

    def invoke_worker(self,mode='passed',smoke_error=None):
        work,instances,modules,extract=self.worker_mocks(mode)
        capability={'status':'capability_smoke_passed','runtime_acceptance':False,'receipts':[
            {'variant':v,'result':'absent','identity_verified':True} for v in ('pro','enterprise')],
            'owned_marker_read_write_removed':True,'same_native_python_reader':True}
        with mock.patch.object(consumer,'native_context',return_value=(self.fixture.source,self.args.python,IMAGE)),\
            mock.patch.object(consumer,'host_metadata',return_value={'macos_version':'15.7.9','kernel_release':'24.6.0','python_version':[3,13,0],'python_path_sha256':'a'*64}),\
            mock.patch.object(consumer,'process_chain',return_value=[]),\
            mock.patch.object(transport,'validate_archive',return_value=(work,self.fixture.manifest)),\
            mock.patch.object(transport,'extract',side_effect=extract),mock.patch.object(transport,'fixture_modules',modules),\
            mock.patch.object(consumer,'smoke',side_effect=smoke_error,return_value=capability):
            result=consumer.worker(self.args)
        return result,instances

    def test_worker_runs_fixed_presigned_canonical_methods_exports_62_and_removes_owned_root(self):
        report,instances=self.invoke_worker()
        self.assertEqual(len(instances),1); self.assertTrue(instances[0].cleaned)
        self.assertTrue(report['runtime_acceptance']); self.assertEqual(len(report['cases']),62)
        self.assertTrue(report['outer_root_removed']); self.assertFalse(self.fixture.root.exists())
        consumer.validate_public(report,fixtures.COMMIT,self.args.python_sha256)
        raw=(self.args.evidence/'public/public.json').read_text()
        for private in ('raw-installation-do-not-export','raw private identity','private payload','private_extra','private_journal',
            self.fixture.seed['signing_master_key'],self.fixture.seed['variant_keys']['pro']): self.assertTrue(private not in raw,'Private payload escaped public projection')
        self.assertEqual(set(p.name for p in (self.args.evidence/'public').iterdir()),{'public.json','inventory.json'})
        for change in (lambda r:r['cases'].pop(),lambda r:r['cases'].append(r['cases'][0]),
            lambda r:r['cases'][0].update(role='extra'),lambda r:r['cases'][0].update(status='failed'),
            lambda r:r.update(signing_mode='sign_locally'),lambda r:r['capability_smoke'].update(status='unproven'),
            lambda r:r['cleanup'].update(container_removed=False)):
            invalid=copy.deepcopy(report); change(invalid)
            with self.subTest(change=change),self.assertRaises(ValueError): consumer.validate_public(invalid,fixtures.COMMIT,self.args.python_sha256)

    def test_worker_runtime_failure_still_calls_canonical_cleanup_and_exports_no_raw_error(self):
        report,instances=self.invoke_worker('run_failed')
        self.assertTrue(instances[0].cleaned); self.assertFalse(report['runtime_acceptance'])
        self.assertTrue(report['outer_root_removed']); self.assertFalse(self.fixture.root.exists())
        self.assertNotIn('private token',(self.args.evidence/'public/public.json').read_text())

    def test_worker_incomplete_canonical_cleanup_is_not_runtime_acceptance(self):
        report,instances=self.invoke_worker('cleanup_failed')
        self.assertTrue(instances[0].cleaned); self.assertFalse(report['runtime_acceptance'])
        self.assertTrue(report['outer_root_removed'])

    def test_worker_smoke_denial_skips_all_lifecycle_and_still_removes_private_root(self):
        report,instances=self.invoke_worker(smoke_error=PermissionError(13,'private container path'))
        self.assertEqual(instances,[]); self.assertEqual(report['status'],'protected_container_access_unproven')
        self.assertFalse(report['runtime_acceptance']); self.assertTrue(report['outer_root_removed'])
        self.assertFalse(report['container_cleanup_verified']); self.assertEqual(report['error']['errno'],13)

    def test_worker_archive_rejection_creates_no_runtime_root_and_only_authored_failure(self):
        with mock.patch.object(consumer,'native_context',return_value=(self.fixture.source,self.args.python,IMAGE)),\
            mock.patch.object(consumer,'process_chain',return_value=[]),\
            mock.patch.object(transport,'validate_archive',side_effect=ValueError('private untrusted exception')),\
            mock.patch.object(transport,'extract') as extract:
            report=consumer.worker(self.args)
        extract.assert_not_called(); self.assertFalse(self.fixture.root.exists())
        self.assertFalse(report['runtime_acceptance']); self.assertEqual(report['error']['phase'],'archive_admission')
        self.assertNotIn('private untrusted',(self.args.evidence/'public/public.json').read_text())

    def test_public_export_failure_still_cleans_owned_private_runtime_root(self):
        original=consumer.write_public; calls=[]
        def broken(directory,report):
            calls.append(True)
            if len(calls)==2: raise OSError(28,'private disk-full path')
            return original(directory,report)
        with mock.patch.object(consumer,'write_public',side_effect=broken):
            report,_=self.invoke_worker(smoke_error=PermissionError(13,'private container'))
        self.assertTrue(report['outer_root_removed']); self.assertFalse(report['runtime_acceptance'])
        self.assertEqual(report['export_error']['errno'],28); self.assertFalse(self.fixture.root.exists())

    def test_controller_uses_fixed_clean_native_worker_and_validates_public_completion(self):
        report,_=self.invoke_worker(); self.args.evidence=self.directory/'controller-success'
        def callback(process,kwargs):
            self.assertNotIn('GITHUB_TOKEN',kwargs['env']); self.assertNotIn('GITHUB_TOKEN',os.environ)
            self.args.evidence.mkdir(); (self.args.evidence/'public').mkdir()
            consumer.write_public(self.args.evidence/'public',report)
        owner=FakeOwner(callback)
        with mock.patch.dict(os.environ,{'GITHUB_TOKEN':'keep-out-of-native-children'}),\
            mock.patch.object(consumer,'native_context',return_value=(self.fixture.source,self.args.python,IMAGE)),\
            mock.patch.object(transport,'fixture_modules',self.modules_with_owner(owner)):
            result=consumer.run(self.args)
        self.assertTrue(result['runtime_acceptance']); self.assertTrue(owner.closed)
        self.assertEqual(owner.spawned[0][0],consumer.fixed_worker_command(self.args,self.args.python))
        self.assertEqual((self.args.evidence.with_name(self.args.evidence.name+'-controller.log')).stat().st_mode&0o777,0o600)

    def test_controller_timeout_closes_owned_group_and_exports_incomplete_cleanup(self):
        self.fixture.root.mkdir(mode=0o700); self.addCleanup(lambda: shutil.rmtree(self.fixture.root) if self.fixture.root.exists() else None)
        (self.fixture.root/'private-bootstrap.json').write_text('private-only')
        def callback(process,kwargs):
            process.poll.return_value=None
            self.args.evidence.mkdir(); (self.args.evidence/'public').mkdir(); (self.args.evidence/'transport').mkdir()
            info=self.fixture.root.stat()
            transport.write(self.args.evidence/'transport/owned-root.json',transport.encoded({'schema':'macos-app-owned-root-v1',
                'root':str(self.fixture.root),'device':info.st_dev,'inode':info.st_ino}))
            consumer.write_public(self.args.evidence/'public',{'schema':consumer.SCHEMA,'status':'runtime_failed',
                'runtime_acceptance':False,'outer_root_removed':False,'cases':[]})
        owner=FakeOwner(callback)
        with mock.patch.object(consumer,'native_context',return_value=(self.fixture.source,self.args.python,IMAGE)),\
            mock.patch.object(transport,'fixture_modules',self.modules_with_owner(owner)),\
            mock.patch.object(consumer.time,'monotonic',side_effect=[0,7201]),self.assertRaises(ValueError): consumer.run(self.args)
        report=transport.json_file(self.args.evidence,'public/public.json')
        self.assertTrue(owner.closed); self.assertFalse(report['runtime_acceptance']); self.assertTrue(report['outer_root_removed'])
        self.assertFalse(report['native_cleanup_verified']); self.assertFalse(self.fixture.root.exists())

    def transfer_line(self,item,*,path=None,status=200,size=None,method='GET',host=None):
        return (f'2026/11/02 12:00:00 "{method} {host or self.state["api_url"]}{path or item["path"]} HTTP/1.1" '
            f'from 127.0.0.1:54321 - {status} {item["size"] if size is None else size}B in 1.2ms\n').encode()

    def test_exact_transfer_catalog_binds_real_app_release_wire_and_both_hash_roles(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)
        self.assertEqual(set(catalog),{'1','2','3'})
        self.assertEqual(catalog['2']['path'],f'/storage/dl/releases/apps/{self.state["app_id"]}/releases/1/patches/2/macos/x86_64/dlc.vmcode')
        self.assertNotEqual(catalog['2']['artifact_hash'],catalog['2']['stored_sha256'])
        with closing(sqlite3.connect(self.fixture.payload/'state.sqlite')) as db,db:
            db.execute("UPDATE patch_artifacts SET storage_key='apps/foreign/releases/1/patches/2/macos/x86_64/dlc.vmcode' WHERE patch_id=2")
        with self.assertRaises(ValueError): consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)

    def test_both_repairs_require_one_complete_successful_real_signed_patch_transfer(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)
        before={'size':10,'time':100,'sha256':'0'*64}; after={'size':100,'time':101,'sha256':'1'*64}; item=catalog['2']; good=self.transfer_line(item)
        for role in ('ciphertext-reauthorized-pro-check','signature-reauthorized-pro-check'):
            proof=consumer.transfer_proof(good,self.state,catalog,role,999,before,after)
            self.assertEqual(proof['successful_transfers'],1); self.assertEqual(proof['transferred_bytes'],item['size'])
            self.assertTrue(proof['signed_download_grant_validated']); self.assertNotIn('http://',transport.encoded(proof).decode())
            invalid=(b'',good+good,self.transfer_line(item,status=403),self.transfer_line(item,status=206),
                self.transfer_line(item,size=0),self.transfer_line(item,size=item['size']-1),
                self.transfer_line(item,path=item['path'].replace(self.state['app_id'],'foreign-app')),
                self.transfer_line(catalog['3']),self.transfer_line(item,host='http://127.0.0.1:9'),
                self.transfer_line(item,method='POST'),self.transfer_line(item,path=item['path']+'?token=private'))
            for raw in invalid:
                with self.subTest(role=role,raw_sha256=transport.sha(raw)),self.assertRaises(ValueError):
                    consumer.transfer_proof(raw,self.state,catalog,role,999,before,after)

    def test_cached_offline_and_control_cases_require_zero_transfer_requests(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)
        before={'size':0,'time':100,'sha256':'0'*64}; after={'size':0,'time':101,'sha256':'0'*64}
        for role in ('same-content-higher-epoch-check','offline-paid','ciphertext-repaired-offline-pro','final-delete-pro'):
            probe={'role':role,'target_patch_number':consumer.paid_check_target(role),'verified_cached':True if consumer.paid_check_target(role) else None,
                'baseline_available':True,'pair_present':True,'pair_pristine':True,'survivors_pristine':True}
            proof=consumer.transfer_proof(b'',self.state,catalog,role,999,before,after,probe)
            self.assertEqual(proof['request_count'],0)
            with self.subTest(role=role),self.assertRaises(ValueError):
                consumer.transfer_proof(self.transfer_line(catalog['2']),self.state,catalog,role,999,before,after,probe)

    def test_transfer_windows_reject_log_rewrite_rotation_partial_and_missing_log(self):
        work=self.fixture.payload; path=work/'server.log'; path.write_bytes(b'baseline\n')
        before=consumer.log_checkpoint(work); catalog=consumer.transfer_catalog(work,self.state,self.fixture.manifest)
        line=self.transfer_line(catalog['2'])
        with path.open('ab') as stream: stream.write(line)
        raw,after=consumer.log_window(work,before); self.assertEqual(raw,line); self.assertGreater(after['size'],before['size'])
        for change in (lambda:path.write_bytes(b'rewritten\n'+line),lambda:path.write_bytes(b'baseline\npartial')):
            change()
            with self.assertRaises(ValueError): consumer.log_window(work,before)
        path.write_bytes(b'baseline\n'); current=consumer.log_checkpoint(work)
        path.rename(work/'rotated.log'); path.write_bytes(b'baseline\n')
        with self.assertRaises(ValueError): consumer.log_window(work,current)
        path.unlink()
        with self.assertRaises(ValueError): consumer.log_checkpoint(work)
        self.assertIsNone(consumer.log_checkpoint(work,allow_absent=True))

    def test_cached_repair_wrong_scope_and_failed_download_cannot_pass_joined_worker(self):
        for mode in ('no_repair_transfer','wrong_transfer_scope','failed_repair_transfer'):
            self.args.evidence=self.directory/('consumer-'+mode)
            report,instances=self.invoke_worker(mode)
            self.assertTrue(instances[0].cleaned); self.assertFalse(report['runtime_acceptance'])
            self.assertTrue(report['outer_root_removed']); self.assertTrue(report['private_evidence']['retained'])
            failed=[p for p in report['http_transfers'] if p.get('status')=='failed']
            self.assertTrue(failed); self.assertEqual(failed[0]['role'],'ciphertext-reauthorized-pro-check')

    def test_private_retention_preserves_auditable_raw_evidence_after_seed_root_removal(self):
        report,_=self.invoke_worker(); directory=self.args.evidence/'private-canonical'
        inventory=transport.json_file(directory,'inventory.json')
        self.assertTrue(inventory['complete']); self.assertFalse(self.fixture.root.exists())
        self.assertEqual(report['private_evidence']['inventory_sha256'],transport.inputs.digest(directory/'inventory.json'))
        self.assertIn('lifecycle/replay-capture.json',inventory['files']); self.assertIn('server.log',inventory['files'])
        self.assertTrue(any(name.startswith('signing-evidence/') for name in inventory['files']))
        self.assertTrue(any(name.endswith('/http-window.log') for name in inventory['files']))
        self.assertTrue(any(name.endswith('/cache.json') for name in inventory['files']))
        for name,info in inventory['files'].items():
            self.assertTrue(consumer.private_name(name)); self.assertEqual(transport.inputs.digest(directory/name),info['sha256'])
            self.assertEqual((directory/name).stat().st_mode&0o777,0o600)
        for forbidden in ('private-bootstrap.json','secrets.json','state.sqlite'):
            self.assertNotIn(forbidden,inventory['files']); self.assertFalse((directory/forbidden).exists())
        self.assertFalse((directory/'objects').exists()); self.assertFalse((directory/'released').exists())
        public=transport.json_file(self.args.evidence/'public','inventory.json')
        self.assertEqual(set(public['files']),{'public.json'})
        self.assertNotIn('raw-download-url',(self.args.evidence/'public/public.json').read_text())
        self.assertIn('raw-download-url',(directory/'lifecycle/replay-capture.json').read_text())

    def test_private_retention_rejects_payload_aliases_unknown_files_and_bounds(self):
        work=self.fixture.payload
        for role in ('unknown','symlink','hardlink','size'):
            branch=work/'lifecycle'; shutil.rmtree(branch,ignore_errors=True); branch.mkdir()
            if role=='unknown': (branch/'private-bootstrap.json').write_bytes(b'do-not-copy')
            elif role=='symlink': (branch/'result.json').symlink_to(work/'private-bootstrap.json')
            elif role=='hardlink': os.link(work/'private-bootstrap.json',branch/'result.json')
            else: (branch/'result.json').write_bytes(b'too-large')
            directory=self.directory/('retained-'+role)
            with mock.patch.object(consumer,'MAX_PRIVATE_FILE',4 if role=='size' else 64*1024**2):
                summary,error=consumer.retain_private(work,directory)
            self.assertFalse(summary['complete']); self.assertIsNotNone(error)
            self.assertFalse((directory/'lifecycle/private-bootstrap.json').exists())
        for name in ('private-bootstrap.json','secrets.json','state.sqlite','objects/releases/private','released/Fixture.app/Contents/MacOS/Fixture'):
            self.assertFalse(consumer.private_name(name))

    def test_optional_paid_downloads_follow_verified_cache_and_same_content_requires_cached_zero(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)
        with transport.fixture_modules(WORKSPACE) as (_,lifecycle):
            role='transition-3-pro-check'
            data=b'reconstructed-target-artifact-2'; metadata={'kind':'Installed','size':len(data),'signature':catalog['2']['hash_signature']}
            raw=transport.encoded(metadata)
            baseline={'2':{'ciphertext':{'size':len(data),'sha256':transport.sha(data)},
                'metadata':{'size':len(raw),'sha256':transport.sha(raw)},'anchor_role':'transition-1-pro-cold','pid':99}}
            missing=consumer.cache_probe(lifecycle,self.state,catalog,role,baseline)
            self.assertFalse(missing['verified_cached']); self.assertTrue(consumer.expected_transfer(role,missing))
            root=lifecycle.cache_root(self.state); directory=root/'patches/2'; directory.mkdir(parents=True)
            (directory/'dlc.vmcode').write_bytes(data); (directory/'state.json').write_bytes(transport.encoded(metadata))
            cached=consumer.cache_probe(lifecycle,self.state,catalog,role,baseline)
            self.assertTrue(cached['verified_cached']); self.assertFalse(consumer.expected_transfer(role,cached))
            self.assertEqual(consumer.transfer_counts(role,cached),(0,1))
            for count in (0,1):
                before={'size':0,'time':100,'sha256':'0'*64}; after={'size':0,'time':101,'sha256':'0'*64}
                consumer.transfer_proof(self.transfer_line(catalog['2']) if count else b'',self.state,catalog,role,999,before,after,cached)
            for target_role in ('same-content-higher-epoch-check','leased-pro-check'):
                probe=consumer.cache_probe(lifecycle,self.state,catalog,target_role,baseline)
                self.assertTrue(probe['verified_cached']); self.assertFalse(consumer.expected_transfer(target_role,probe))
            (directory/'dlc.vmcode').write_bytes(data[:-1]+b'x')
            corrupted=consumer.cache_probe(lifecycle,self.state,catalog,role,baseline)
            self.assertFalse(corrupted['verified_cached'])
            with self.assertRaises(ValueError): consumer.expected_transfer(role,corrupted)
            for target_role in ('same-content-higher-epoch-check','leased-pro-check'):
                with self.assertRaises(ValueError): consumer.expected_transfer(target_role,consumer.cache_probe(lifecycle,self.state,catalog,target_role,baseline))
            (directory/'dlc.vmcode').write_bytes(data)
            (directory/'state.json').write_bytes(transport.encoded({**metadata,'signature':base64.b64encode(b'w'*256).decode()}))
            self.assertFalse(consumer.cache_probe(lifecycle,self.state,catalog,role,baseline)['verified_cached'])
            with mock.patch.object(transport,'read',side_effect=PermissionError(13,'private cache')),self.assertRaises(PermissionError):
                consumer.cache_probe(lifecycle,self.state,catalog,role)

    def test_final_native_cleanup_is_attempted_even_if_transfer_log_cannot_be_read(self):
        called=[]
        class Base:
            def __init__(self): self.work=self.fixture_work; self.state={}; self.launches=[]
            def launch(self,label): called.append(label); self.launches.append({'label':label,'pid':1}); return self.launches[-1]
        Base.fixture_work=self.directory
        with transport.fixture_modules(WORKSPACE) as (fixture,lifecycle):
            measured=consumer.measured_lifecycle(Base,fixture,lifecycle,{})()
            with mock.patch.object(consumer,'log_checkpoint',side_effect=PermissionError(13,'private log')),self.assertRaises(PermissionError):
                measured.launch('final-delete-pro')
        self.assertEqual(called,['final-delete-pro'])

    def test_future_target_absence_requires_transfer_and_surviving_pair_must_match_cold_baseline(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest)
        data=b'reconstructed-target-artifact-2'; metadata=transport.encoded({'kind':'Installed','size':len(data),'signature':catalog['2']['hash_signature']})
        baseline={'2':{'ciphertext':{'size':len(data),'sha256':transport.sha(data)},'metadata':{
            'size':len(metadata),'sha256':transport.sha(metadata)},'anchor_role':'transition-1-pro-cold','pid':99}}
        with transport.fixture_modules(WORKSPACE) as (_,lifecycle):
            directory=lifecycle.cache_root(self.state)/'patches/2'; directory.mkdir(parents=True)
            for surviving in ('ciphertext','metadata'):
                for leaf in ('dlc.vmcode','state.json'): (directory/leaf).unlink(missing_ok=True)
                path=directory/('dlc.vmcode' if surviving=='ciphertext' else 'state.json')
                raw=data if surviving=='ciphertext' else metadata; path.write_bytes(raw)
                probe=consumer.cache_probe(lifecycle,self.state,catalog,'transition-3-pro-check',baseline)
                self.assertEqual(consumer.transfer_counts('transition-3-pro-check',probe),(1,))
                path.write_bytes(raw+b' ')
                with self.assertRaises(ValueError): consumer.transfer_counts('transition-3-pro-check',
                    consumer.cache_probe(lifecycle,self.state,catalog,'transition-3-pro-check',baseline))

    def test_running_paid_pair_is_anchored_at_first_cold_and_unrelated_content_need_not_survive(self):
        catalog=consumer.transfer_catalog(self.fixture.payload,self.state,self.fixture.manifest); baselines={}
        pair={'patches/2/dlc.vmcode':{'sha256':catalog['2']['artifact_hash'],'size':50},
            'patches/2/state.json':{'sha256':transport.sha(b'Installed metadata'),'size':500}}
        with transport.fixture_modules(WORKSPACE) as (fixture,_):
            first=self.entry(6,fixture); first['cache']={'files':copy.deepcopy(pair)}
            proof=consumer.running_cache(first,'transition-1-pro-cold',catalog,baselines)
            self.assertTrue(proof['baseline_created']); self.assertEqual(set(baselines),{'2'})
            later=self.entry(7,fixture); later['cache']={'files':copy.deepcopy(pair)}
            self.assertTrue(consumer.running_cache(later,'transition-2-enterprise-check',catalog,baselines)['unchanged'])
            for leaf in pair:
                corrupted=copy.deepcopy(later); corrupted['cache']['files'][leaf]['sha256']='0'*64
                with self.subTest(leaf=leaf),self.assertRaises(ValueError):
                    consumer.running_cache(corrupted,'transition-2-enterprise-check',catalog,baselines)

    def test_missing_required_private_raw_evidence_blocks_acceptance_but_still_wipes_seed_root(self):
        report,instances=self.invoke_worker('missing_private_evidence')
        self.assertTrue(instances[0].cleaned); self.assertFalse(report['runtime_acceptance'])
        self.assertEqual(report['status'],'private_evidence_incomplete'); self.assertTrue(report['outer_root_removed'])
        self.assertTrue(report['private_evidence']['retained']); self.assertFalse(report['private_evidence']['complete'])
        self.assertFalse(self.fixture.root.exists())


if __name__=='__main__': unittest.main()

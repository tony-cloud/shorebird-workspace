"""Pure target/host controls. No compiler, signing tool, app or device runs."""
import base64
import copy
import errno
import hashlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import macos_interpreter_e2e as base
import macos_variant_e2e as variant
import macos_variant_lifecycle as lifecycle


class TargetArchitectureTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='macos-target-unit-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        dependency=self.root/'private-updater/shorebird_code_push'
        (dependency/'lib/src').mkdir(parents=True)
        (dependency/'lib/src/selection_status.dart').write_text('// Preparation-contract mock only.\n')
        self.args=SimpleNamespace(cli=self.root/'cli',flutter_root=self.root/'flutter',engine_src=self.root/'engine',
            engine_name=None,host_engine_name=None,server=self.root/'server',release_version='1.0.0+1',
            updater_package=dependency,target_arch='arm64')

    def prepare(self,arch,**overrides):
        args=copy.copy(self.args); args.target_arch=arch
        for name,value in overrides.items(): setattr(args,name,value)
        work=self.root/('session-'+arch)
        with mock.patch.object(base,'unique_ports',return_value=(43101,43102)):
            state=variant.prepare(args,work)
        state['app_id']='11111111-1111-4111-8111-111111111111'
        variant.configure_native(work,state); base.save(work,state)
        return work,state

    def test_canonical_target_mapping_defaults_and_aliases_fail_closed(self):
        self.assertEqual(base.target_architecture({})['arch'],'arm64')
        for arch,machine,wire in (('arm64','arm64','aarch64'),('x64','x86_64','x86_64')):
            self.assertEqual(base.target_architecture({'arch':arch}),dict(arch=arch,machine=machine,wire=wire,
                flutter_target='darwin-'+arch,envelope=arch))
        for invalid in ('aarch64','x86_64','amd64','universal','',None,True,[]):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError): base.target_architecture({'arch':invalid})
        for name in ('arm64','x64'):
            args=variant.parser().parse_args(['prepare','--work-dir','/unused','--target-arch',name])
            self.assertEqual(args.target_arch,name)
        with mock.patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):
            variant.parser().parse_args(['prepare','--work-dir','/unused','--target-arch','x86_64'])
        args=copy.copy(self.args); args.target_arch='x86_64'; work=self.root/'must-remain-absent'
        with self.assertRaises(ValueError): variant.prepare(args,work)
        self.assertFalse(work.exists())

    def test_actual_preparation_persists_target_and_keeps_host_output_independent(self):
        for arch in ('arm64','x64'):
            host='macos_release_'+('x64' if arch=='arm64' else 'arm64')
            work,state=self.prepare(arch,host_engine_name=host)
            self.assertEqual(base.load(work)['arch'],arch)
            self.assertEqual(state['engine_name'],'macos_release_'+arch)
            self.assertEqual(state['host_engine_name'],host)
            for phase in variant.BUILD_ORDER:
                command=variant.cli_command(work,state,phase)
                self.assertEqual(command[-2:],['--','--target-platform=darwin-'+arch])
                self.assertIn('--local-engine-host='+host,command)
                self.assertIn('--local-engine=macos_release_'+arch,command)
                self.assertIn('--no-codesign',command)
            wire=base.target_architecture(state)['wire']
            swift=(work/'app/macos/Runner/PatchTestKeys.swift').read_text()
            self.assertIn('scope["arch"] as? String == "'+wire+'"',swift)
            self.assertIn('"arch": "'+wire+'"',swift)
            self.assertNotIn('__WIRE_ARCH__',swift)
            self.assertEqual(variant.plan(arch)['target']['wire'],wire)
            state['arch']='x86_64'; base.save(work,state)
            with self.assertRaises(ValueError): variant.load(work)

    def test_compiled_scope_filename_content_baseline_and_other_arch_assets(self):
        for arch in ('arm64','x64'):
            work,state=self.prepare(arch)
            wire=base.target_architecture(state)['wire']; other='aarch64' if wire=='x86_64' else 'x86_64'
            assets=work/'compiled/Test.app/Contents/Resources/flutter_assets'; assets.mkdir(parents=True)
            baseline=assets/'shorebird_replacement_base.sbr'; baseline.write_bytes(b'non-executable baseline unit input')
            scope=dict(app_id=state['app_id'],release_version=state['release_version'],platform='macos',arch=wire,
                runtime_profile='interpreter-full',base_artifact_hash=base.digest(baseline),host_abi_hash='a'*64)
            path=assets/('shorebird_release_scope_macos_'+wire+'.json'); path.write_text(json.dumps(scope))
            (assets/'shorebird.yaml').write_text(variant.yaml_source(state)+'patch_public_key: public-unit-placeholder\n'+
                'aot_patch_base_snapshot_hash: '+scope['base_artifact_hash']+'\naot_patch_host_abi: '+'a'*64+'\n')
            self.assertEqual(variant.validate_compiled_scope(state,assets.parent.parent.parent),scope)
            foreign=path.with_name('shorebird_release_scope_macos_'+other+'.json')
            path.rename(foreign)
            with self.assertRaises(ValueError): variant.validate_compiled_scope(state,assets.parent.parent.parent)
            foreign.rename(path); foreign.write_text(json.dumps(dict(scope,arch=other)))
            with self.assertRaises(ValueError): variant.validate_compiled_scope(state,assets.parent.parent.parent)
            foreign.unlink(); path.write_text(json.dumps(dict(scope,arch=other)))
            with self.assertRaises(ValueError): variant.validate_compiled_scope(state,assets.parent.parent.parent)
            path.write_text(json.dumps(scope)); baseline.write_bytes(b'other baseline')
            with self.assertRaises(ValueError): variant.validate_compiled_scope(state,assets.parent.parent.parent)

    def test_real_api_selection_routing_rejects_response_from_other_target(self):
        for arch in ('arm64','x64'):
            work,state=self.prepare(arch); wire=base.target_architecture(state)['wire']
            state['installation_id']='observed-installation'; base.save(work,state)
            calls=[]; wrong=[False]
            def api(work,state,method,path,body=None):
                if method=='GET': return {'releases':[{'version':state['release_version'],'id':9,'selection_protocol':2}]}
                calls.append((method,path,copy.deepcopy(body)))
                self.assertEqual(body['arch'],wire)
                if method=='PUT': return {}
                scope=dict(app_id=state['app_id'],release_version=state['release_version'],platform='macos',arch=wire,
                    client_id_hash=hashlib.sha256(state['installation_id'].encode()).hexdigest())
                if wrong[0]: scope['arch']='aarch64' if wire=='x86_64' else 'x86_64'
                return {'selection':dict(epoch=str(int(body['expected_epoch'])+1),variant_id='pro',license_type='pro',
                    scope=scope,target={'patch_number':'2'}),'selection_digest':'a'*64}
            with mock.patch.object(variant,'api',side_effect=api):
                variant.map_free(work,state); variant.assign(work,state,'pro')
                wrong[0]=True
                with self.assertRaisesRegex(ValueError,'installation/epoch/content'): variant.assign(work,state,'pro')
            self.assertEqual(calls[0][0],'PUT')
            self.assertEqual(len(state['assignments']),1)
            self.assertIn('pending_assignment',base.load(work))

    def test_encrypted_envelopes_bind_both_canonical_app_targets(self):
        for arch in ('arm64','x64'):
            _,state=self.prepare(arch); state['release_scope']={'base_artifact_hash':'b'*64}
            metadata=dict(app_id=state['app_id'],app_build_id=state['release_version'],target_os='macos',target_arch=arch,
                runtime_mode='dart-bytecode-interpreter',flavor_id='pro',license_type='pro',base_snapshot_hash='b'*64)
            envelope={'format':'open-aot-vmcode-encrypted-v1','payload_kind':'full-snapshot','metadata':metadata,
                'encryption':{'algorithm':'AES-256-GCM','key_id':state['key_ids']['pro']}}
            variant.validate_envelope_metadata(state,envelope,'pro')
            for field,value in (('target_arch','arm64' if arch=='x64' else 'x64'),('target_os','ios'),
                                ('base_snapshot_hash','c'*64),('app_build_id','foreign')):
                bad=copy.deepcopy(envelope); bad['metadata'][field]=value
                with self.subTest(arch=arch,field=field),self.assertRaises(ValueError):
                    variant.validate_envelope_metadata(state,bad,'pro')

    def test_capture_replay_and_provider_oracles_use_the_prepared_target(self):
        for arch in ('arm64','x64'):
            work,state=self.prepare(arch); wire=base.target_architecture(state)['wire']
            state.update(installation_id='observed-installation',release_scope=dict(base_artifact_hash='a'*64,
                host_abi_hash='b'*64,runtime_profile='interpreter-full'))
            old=dict(epoch='1',digest='c'*64,variant_id='pro',license_type='pro',patch_number='2')
            new=dict(old,epoch='2',digest='d'*64)
            state['observed_selection']=old
            instance=lifecycle.Lifecycle.__new__(lifecycle.Lifecycle)
            instance.work=work; instance.state=state; instance.reload=mock.Mock(); instance.write=mock.Mock()
            captured=[]
            def request(state,method,path,body):
                self.assertEqual(body['arch'],wire); captured.append(body)
                payload=base64.b64encode(json.dumps({'request':{'nonce':body['nonce']},'selection_digest':old['digest']}).encode()).decode()
                return {'selection':{'epoch':old['epoch']},'selection_digest':old['digest'],
                    'authentication':{'signature':'public-unit-placeholder','payload':payload}}
            with mock.patch.object(base.shared,'request',side_effect=request): body,selection=instance.capture_response()
            request=dict(captured[0],nonce=str(uuid.uuid4()),accepted_selection_epoch=new['epoch'],accepted_selection_digest=new['digest'])
            def record():
                raw=json.dumps(request).encode()
                return {'request':copy.deepcopy(request),'request_body_base64':base64.b64encode(raw).decode(),
                    'request_body_sha256':hashlib.sha256(raw).hexdigest(),'response_body_sha256':hashlib.sha256(body).hexdigest(),
                    'received_at':10,'completed_at':11}
            lifecycle.validate_replay_delivery(state,[record()],body,selection,new,after=9,before=12)
            request['arch']='aarch64' if wire=='x86_64' else 'x86_64'
            with self.assertRaises(ValueError): lifecycle.validate_replay_delivery(state,[record()],body,selection,new,after=9,before=12)
            scope=dict(app_id=state['app_id'],release_version=state['release_version'],platform='macos',arch=wire,
                runtime_profile='interpreter-full',client_id_hash=hashlib.sha256(state['installation_id'].encode()).hexdigest())
            event=dict(sequence=1,outcome='released',reason='key_available',time=10.5,scope=scope,
                epoch=new['epoch'],selection_digest=new['digest'],variant_id='pro',license_type='pro',patch_number='2',key_id=state['key_ids']['pro'])
            ledger=dict(schema='macos-variant-provider-ledger-v2',session=state['session'],bundle_id=state['bundle_id'],
                pid=123,nonce='launch-nonce',started=10,time=11,events=[event],overflow=False)
            report={'installation_id':state['installation_id'],'selection':{'current':new,'accepted':new}}
            self.assertEqual(lifecycle.validate_provider_ledger(state,report,ledger,pid=123,nonce='launch-nonce',after=9)['successful_releases'],1)
            event['scope']['arch']='aarch64' if wire=='x86_64' else 'x86_64'
            with self.assertRaises(ValueError): lifecycle.validate_provider_ledger(state,report,ledger,pid=123,nonce='launch-nonce',after=9)

    def test_actual_signature_inspection_keeps_security_checks_and_rejects_wrong_nested_cpu(self):
        for arch in ('arm64','x64'):
            work,state=self.prepare(arch); machine=base.target_architecture(state)['machine']
            app=work/'released/Test.app'; runner=app/'Contents/MacOS/Runner'; runner.parent.mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':state['bundle_id'],'CFBundleExecutable':'Runner'}))
            nested=app/'Contents/Frameworks/Unit.framework/Versions/A/Unit'; nested.parent.mkdir(parents=True)
            # Inert inventory fixtures; every external command is mocked.
            for path in (runner,nested): path.write_bytes(b'\xcf\xfa\xed\xfe'+bytes(16))
            certificate=b'public-unit-certificate'; identity=hashlib.sha1(certificate).hexdigest().upper()
            details='CodeDirectory flags=0x10000(runtime)\nAuthority=Developer ID Application: Unit\nTeamIdentifier=TEAM123456\n'
            fault=[None]; commands=[]
            def checked(command,log):
                command=list(map(str,command)); commands.append(command)
                if command[0]=='lipo': return ('arm64 x86_64' if fault[0]=='fat' else ('wrong' if fault[0]=='nested' else machine)) if command[-1]==str(nested) else machine
                for arg in command:
                    if arg.startswith('--extract-certificates='):
                        Path(arg.split('=',1)[1]+'0').write_bytes(b'other' if fault[0]=='certificate' else certificate)
                if '--verbose=4' in command and '--display' in command: return details
                if '--entitlements' in command:
                    entitlements=base.APP_ENTITLEMENTS if command[-1] in (str(app),str(runner)) else {}
                    if fault[0]=='entitlements': entitlements=dict(entitlements,**{'com.apple.security.cs.allow-jit':True})
                    return plistlib.dumps(entitlements).decode()
                if command[0]=='xcrun': return '\n'.join('_'+name for name in base.shared.FFI_EXPORTS)
                return ''
            with mock.patch.object(base,'checked',side_effect=checked):
                inspected=base.inspect_app(work,state,app,identity)
                self.assertEqual(len(inspected['signatures']),3)
                self.assertTrue(any('--deep' in command and '--strict' in command for command in commands))
                for bad in ('nested','fat','certificate','entitlements'):
                    fault[0]=bad
                    with self.subTest(arch=arch,fault=bad),self.assertRaises(ValueError): base.inspect_app(work,state,app,identity)


class NativeHostTest(unittest.TestCase):
    def probe(self,arch,*,result=0,value=0,error=0,size=None):
        def invoke(name,out,length,new,new_length):
            self.assertEqual(name,b'sysctl.proc_translated')
            out._obj.value=value
            if size is not None: length._obj.value=size
            return result
        library=SimpleNamespace(sysctlbyname=mock.Mock(side_effect=invoke))
        with mock.patch.object(lifecycle.sys,'platform','darwin'), \
             mock.patch.object(lifecycle.platform,'machine',return_value=base.target_architecture({'arch':arch})['machine']), \
             mock.patch.object(lifecycle.ctypes,'CDLL',return_value=library), \
             mock.patch.object(lifecycle.ctypes,'get_errno',return_value=error):
            lifecycle.require_native_host({'arch':arch})

    def test_native_both_targets_and_only_documented_absent_probe_are_accepted(self):
        for arch in ('arm64','x64'):
            self.probe(arch)
            self.probe(arch,result=-1,error=errno.ENOENT)
            for options in ({'value':1},{'value':2},{'size':0},{'result':1},{'result':-1,'error':0},{'result':-1,'error':errno.EACCES}):
                with self.subTest(arch=arch,options=options),self.assertRaises(ValueError): self.probe(arch,**options)

    def test_wrong_os_cpu_and_unavailable_native_probe_fail_closed(self):
        for system,machine in (('linux','x86_64'),('darwin','arm64')):
            with mock.patch.object(lifecycle.sys,'platform',system),mock.patch.object(lifecycle.platform,'machine',return_value=machine), \
                 mock.patch.object(lifecycle.ctypes,'CDLL') as library,self.assertRaises(ValueError):
                lifecycle.require_native_host({'arch':'x64'})
            library.assert_not_called()
        with mock.patch.object(lifecycle.sys,'platform','darwin'),mock.patch.object(lifecycle.platform,'machine',return_value='x86_64'), \
             mock.patch.object(lifecycle.ctypes,'CDLL',side_effect=OSError('unavailable')),self.assertRaises(ValueError):
            lifecycle.require_native_host({'arch':'x64'})

    def test_main_refuses_target_or_host_before_lifecycle_mutations_and_plan_does_not_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)/'must-remain-absent'
            with mock.patch.object(lifecycle,'require_native_host',side_effect=ValueError('not native')),self.assertRaises(ValueError):
                lifecycle.Lifecycle(work,{'arch':'x64'},'A'*40)
            self.assertFalse(work.exists())
        args=['--execute','--work-dir','/unused','--signing-identity','A'*40]
        with mock.patch.object(lifecycle.fixture,'load',return_value={'arch':'x64'}), \
             mock.patch.object(lifecycle,'require_native_host') as host,mock.patch.object(lifecycle,'require_fresh_fixture') as fresh, \
             mock.patch.object(lifecycle,'Lifecycle') as create:
            with self.assertRaisesRegex(ValueError,'differs from prepared'): lifecycle.main(args+['--target-arch','arm64'])
            host.assert_not_called(); fresh.assert_not_called(); create.assert_not_called()
            host.side_effect=ValueError('not native')
            with self.assertRaisesRegex(ValueError,'not native'): lifecycle.main(args+['--target-arch','x64'])
            fresh.assert_not_called(); create.assert_not_called()
        with mock.patch.object(lifecycle.fixture,'load') as load,mock.patch.object(lifecycle,'require_native_host') as host, \
             mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            lifecycle.main(['--work-dir','/unused','--target-arch','x64'])
            self.assertEqual(json.loads(output.getvalue())['target']['machine'],'x86_64')
            host.assert_not_called(); load.assert_not_called()


if __name__=='__main__': unittest.main()

"""Pure pre-signed mode controls; all signature tools/processes are mocked."""
import copy
import hashlib
import io
import json
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

import macos_variant_lifecycle as runner


class PresignedLifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='macos-presigned-unit-')
        self.addCleanup(temporary.cleanup)
        self.work=Path(temporary.name).resolve()
        self.app=self.work/'released/Test.app'
        self.binary=self.app/'Contents/MacOS/Runner'; self.binary.parent.mkdir(parents=True)
        self.nested=self.app/'Contents/Frameworks/Unit.framework/Versions/A/Unit'
        self.nested.parent.mkdir(parents=True)
        for path in (self.binary,self.nested):
            path.write_bytes(b'\xcf\xfa\xed\xfe'+bytes(16))  # Inert inventory input, never executed.
        self.state=dict(schema=runner.v1.SCHEMA,variant_schema=runner.fixture.SCHEMA,
            work_dir=str(self.work),created=1,arch='x64',session='aabbccddeeff',
            bundle_id='org.tonycloud.shorebird-macos-ffi.aabbccddeeff',released_app=str(self.app),
            app_id='11111111-1111-4111-8111-111111111111',release_version='1.0.0+1',
            key_ids={'pro':'pro-unit','enterprise':'enterprise-unit'},built_phases=list(runner.fixture.BUILD_ORDER),
            assignments=[],api_url='http://127.0.0.1:43101',report_url='http://127.0.0.1:43102/report')
        self.info=self.app/'Contents/Info.plist'
        self.info.write_bytes(plistlib.dumps({'CFBundleIdentifier':self.state['bundle_id'],'CFBundleExecutable':'Runner'}))
        assets=self.app/'Contents/Resources/flutter_assets'; assets.mkdir(parents=True)
        baseline=assets/'shorebird_replacement_base.sbr'; baseline.write_bytes(b'unit baseline, not executable')
        self.scope=dict(app_id=self.state['app_id'],release_version='1.0.0+1',platform='macos',arch='x86_64',
            runtime_profile='interpreter-full',base_artifact_hash=runner.v1.digest(baseline),host_abi_hash='a'*64)
        (assets/'shorebird_release_scope_macos_x86_64.json').write_text(json.dumps(self.scope))
        (assets/'shorebird.yaml').write_text(runner.fixture.yaml_source(self.state)+
            'patch_public_key: public-unit-placeholder\naot_patch_base_snapshot_hash: '+self.scope['base_artifact_hash']+
            '\naot_patch_host_abi: '+'a'*64+'\n')
        self.state['release_scope']=copy.deepcopy(self.scope)
        (self.work/'secrets.json').write_text(json.dumps({'variant_keys':{'pro':'a1'*32,'enterprise':'b2'*32}}))
        entitlement=self.work/'app/macos/Runner/Release.entitlements'; entitlement.parent.mkdir(parents=True)
        entitlement.write_bytes(plistlib.dumps(runner.v1.APP_ENTITLEMENTS))
        (self.work/'empty-library-entitlements.plist').write_bytes(plistlib.dumps({}))
        runner.v1.save(self.work,self.state)
        self.certificate=b'public-unit-certificate'
        self.identity=hashlib.sha1(self.certificate).hexdigest().upper()
        self.commands=[]; self.fault=None

    def controller(self,verify_presigned=True):
        with mock.patch.object(runner,'require_native_host'):
            return runner.Lifecycle(self.work,copy.deepcopy(self.state),self.identity,verify_presigned=verify_presigned)

    def checked(self,command,log):
        command=list(map(str,command)); self.commands.append(command)
        target=command[-1]
        if '--sign' in command:
            (self.app/'local-signing-marker').write_text('mock signing only')
            return ''
        if command[0]=='codesign' and '--verify' in command:
            if self.fault=='unsigned': raise ValueError('Unit signature verification rejected unsigned code')
            return ''
        if command[0]=='codesign' and '--verbose=4' in command and '--display' in command:
            team='TEAMOTHER' if self.fault=='team' and target==str(self.nested) else 'TEAM123456'
            flags='0x2(adhoc)' if self.fault=='runtime' else '0x10000(runtime)'
            return 'CodeDirectory flags='+flags+'\nAuthority=Developer ID Application: Unit\nTeamIdentifier='+team+'\n'
        if '--entitlements' in command:
            value=runner.v1.APP_ENTITLEMENTS if target in (str(self.app),str(self.binary)) else {}
            if self.fault=='entitlements': value=dict(value,**{'com.apple.security.cs.disable-library-validation':True})
            return plistlib.dumps(value).decode()
        for argument in command:
            if argument.startswith('--extract-certificates='):
                value=b'wrong certificate' if self.fault=='certificate' else self.certificate
                Path(argument.split('=',1)[1]+'0').write_bytes(value)
                return ''
        if command[0]=='lipo':
            machine=runner.v1.target_architecture(self.state)['machine']
            if self.fault=='cpu' and target==str(self.nested): return 'arm64' if machine=='x86_64' else 'x86_64'
            return 'x86_64 arm64' if self.fault=='fat' else machine
        if command[0]=='otool': return '/System/Library/Frameworks/Foundation.framework/Foundation\n'
        if command[0]=='xcrun':
            if self.fault=='mutation': (self.app/'unexpected-verification-write').write_bytes(b'changed')
            return '\n'.join('_'+symbol for symbol in runner.v1.shared.FFI_EXPORTS)
        raise AssertionError('Unexpected verification tool command: '+repr(command))

    def test_run_cases_presigned_verifies_without_signing_lookup_or_bundle_mutation(self):
        controller=self.controller(); before=runner.bundle_tree(self.app)
        with mock.patch.object(runner.v1,'checked',side_effect=self.checked), \
             mock.patch.object(runner.v1,'signing_plan',side_effect=AssertionError('No signing plan in pre-signed mode')) as signing, \
             mock.patch.object(runner.v1.subprocess,'run',side_effect=AssertionError('No real tools or private identity lookup')) as process, \
             mock.patch.object(controller,'start_server',side_effect=RuntimeError('verified-entry-boundary')) as server:
            with self.assertRaisesRegex(RuntimeError,'verified-entry-boundary'): controller.run_cases()
        self.assertEqual(before,runner.bundle_tree(self.app)); self.assertEqual(controller.tree,before)
        self.assertEqual(controller.result['signing_mode'],'verify_presigned')
        self.assertTrue(controller.result['bundle_verified'])
        self.assertFalse(controller.result['runtime_acceptance'])
        self.assertTrue(any('--deep' in command and '--strict' in command for command in self.commands))
        self.assertTrue(all('--sign' not in command and command[0]!='security' for command in self.commands))
        signing.assert_not_called(); process.assert_not_called(); server.assert_called_once()
        saved=json.loads((controller.evidence/'result.json').read_text())
        self.assertEqual(saved['signing_mode'],'verify_presigned')
        self.assertTrue((controller.evidence/'signed-bundle-tree.json').is_file())

    def test_default_mode_still_signs_then_inspects_the_signed_bundle(self):
        controller=self.controller(verify_presigned=False); before=runner.bundle_tree(self.app)
        with mock.patch.object(runner.v1,'checked',side_effect=self.checked), \
             mock.patch.object(runner.v1,'signing_plan',wraps=runner.v1.signing_plan) as signing:
            controller.sign()
        self.assertNotEqual(before,runner.bundle_tree(self.app))
        self.assertEqual(controller.tree,runner.bundle_tree(self.app))
        self.assertEqual(controller.result['signing_mode'],'sign_locally')
        self.assertTrue(controller.result['bundle_verified']); signing.assert_called_once()
        self.assertIn('--sign',self.commands[0])
        self.assertTrue(all(command[-1]!=str(self.app) for command in self.commands[:1]))
        self.assertFalse(controller.verify_presigned)

    def test_presigned_arm64_reuses_the_same_read_only_security_path(self):
        self.state['arch']='arm64'; self.scope['arch']='aarch64'
        self.state['release_scope']=copy.deepcopy(self.scope)
        assets=self.app/'Contents/Resources/flutter_assets'
        (assets/'shorebird_release_scope_macos_x86_64.json').unlink()
        (assets/'shorebird_release_scope_macos_aarch64.json').write_text(json.dumps(self.scope))
        runner.v1.save(self.work,self.state)
        controller=self.controller(); before=runner.bundle_tree(self.app)
        with mock.patch.object(runner.v1,'checked',side_effect=self.checked),mock.patch.object(runner.v1,'signing_plan') as signing:
            controller.sign()
            signing.assert_not_called()
        self.assertTrue(controller.result['bundle_verified'])
        self.assertEqual(controller.tree,before)
        self.assertFalse(any('--sign' in command for command in self.commands))

    def test_presigned_immutable_tree_is_checked_again_before_each_launch(self):
        controller=self.controller()
        with mock.patch.object(runner.v1,'checked',side_effect=self.checked): controller.sign()
        (self.app/'changed-after-verification').write_bytes(b'changed')
        with mock.patch.object(controller.owner,'spawn') as spawn,mock.patch.object(runner.v1,'inspect_app') as inspect, \
             mock.patch.object(runner.v1,'container_file',side_effect=lambda state,name:self.work/'absent-container'/name), \
             mock.patch.object(runner,'cache_snapshot',return_value={'files':{},'journals':{}}):
            with self.assertRaisesRegex(ValueError,'Signed bundle changed before launch'):
                controller.launch('must-not-launch','free')
            spawn.assert_not_called(); inspect.assert_not_called()

    def test_all_existing_signature_target_and_identity_failures_stop_before_server_or_launch(self):
        controller=self.controller()
        with mock.patch.object(runner.v1,'checked',side_effect=self.checked), \
             mock.patch.object(runner.v1,'signing_plan') as signing,mock.patch.object(controller,'start_server') as server, \
             mock.patch.object(controller.owner,'spawn') as spawn:
            for fault in ('unsigned','certificate','team','runtime','cpu','fat','entitlements','mutation'):
                self.fault=fault
                with self.subTest(fault=fault),self.assertRaises(ValueError): controller.run_cases()
                self.assertIsNone(controller.tree); self.assertFalse(controller.result['bundle_verified'])
                server.assert_not_called(); spawn.assert_not_called()
            signing.assert_not_called()

    def test_presigned_pins_original_scope_and_refuses_missing_or_foreign_app(self):
        controller=self.controller()
        with mock.patch.object(runner.v1,'inspect_app') as inspect,mock.patch.object(controller,'start_server') as server:
            controller.state['release_scope']=dict(self.scope,host_abi_hash='b'*64)
            with self.assertRaisesRegex(ValueError,'original pinned release scope'): controller.run_cases()
            self.assertEqual(controller.state['release_scope']['host_abi_hash'],'b'*64)
            controller.state.pop('release_scope')
            with self.assertRaisesRegex(ValueError,'original pinned release scope'): controller.run_cases()
            controller.state['release_scope']=self.scope
            self.info.write_bytes(plistlib.dumps({'CFBundleIdentifier':'foreign','CFBundleExecutable':'Runner'}))
            with self.assertRaisesRegex(ValueError,'foreign application'): controller.run_cases()
            shutil.rmtree(self.app)
            with self.assertRaisesRegex(ValueError,'Missing private pre-signed'): controller.run_cases()
            inspect.assert_not_called(); server.assert_not_called()

    def test_cli_plan_and_public_pin_requirement_remain_resource_free(self):
        with mock.patch.object(runner.fixture,'load') as load,mock.patch.object(runner,'Lifecycle') as create, \
             mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            runner.main(['--work-dir','/unused','--verify-presigned','--target-arch','x64'])
            plan=json.loads(output.getvalue())
            self.assertEqual(plan['signing_mode'],'verify_presigned'); self.assertFalse(plan['runtime_acceptance'])
            with self.assertRaisesRegex(ValueError,'pinned Developer ID'):
                runner.main(['--work-dir','/unused','--verify-presigned','--execute'])
            load.assert_not_called(); create.assert_not_called()
        self.assertFalse(runner.parser().parse_args(['--work-dir','/unused']).verify_presigned)

    def test_main_forwards_mode_and_scope_pin_without_weakening_host_admission(self):
        for presigned in (False,True):
            controlled=mock.Mock(result={'runtime_acceptance':False},evidence=self.work/'unit-evidence')
            controlled.cleanup.return_value=[]
            arguments=['--execute','--work-dir',str(self.work),'--signing-identity',self.identity]
            if presigned: arguments.append('--verify-presigned')
            with mock.patch.object(runner.fixture,'load',return_value=copy.deepcopy(self.state)), \
                 mock.patch.object(runner,'require_native_host') as host,mock.patch.object(runner,'require_fresh_fixture') as fresh, \
                 mock.patch.object(runner,'Lifecycle',return_value=controlled) as create, \
                 mock.patch('sys.stdout',new_callable=io.StringIO):
                runner.main(arguments)
                host.assert_called_once()
                self.assertEqual(fresh.call_args.kwargs,{'preserve_scope':presigned})
                self.assertIs(create.call_args.kwargs['verify_presigned'],presigned)
                controlled.run_cases.assert_called_once(); controlled.cleanup.assert_called_once()

    def test_fresh_gate_preserves_original_scope_in_presigned_mode(self):
        original=copy.deepcopy(self.state['release_scope'])
        with mock.patch.object(runner,'cache_root',return_value=self.work/'absent-cache'), \
             mock.patch.object(runner.v1,'container_file',side_effect=lambda state,name:self.work/'absent-container'/name):
            runner.require_fresh_fixture(self.work,self.state,preserve_scope=True)
            self.assertEqual(self.state['release_scope'],original)
            self.state['release_scope']=dict(original,base_artifact_hash='b'*64)
            with self.assertRaisesRegex(ValueError,'original pinned release scope'):
                runner.require_fresh_fixture(self.work,self.state,preserve_scope=True)
            self.assertEqual(self.state['release_scope']['base_artifact_hash'],'b'*64)


if __name__=='__main__': unittest.main()

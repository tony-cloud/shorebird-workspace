#!/usr/bin/env python3
"""Synthetic transport admission controls; no signing, build, server or app run.

The minimal DB fixture preserves the public schema/relationships read from the
retained macos-ci-unicode-path-20261102 producer: managed app+macos_supplement
containers, mandatory supplement scope ID/hash, three stable mappings and three
wire-CPU patches. IDs, hashes, public RSA parameters and wrapped-key placeholder
are synthetic; no retained private keys, credentials or installation IDs copied.
"""
import base64
import copy
from contextlib import closing, contextmanager
import hashlib
import json
import os
import shutil
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
import macos_app_prepared_transport as transport

WORKSPACE = Path(__file__).resolve().parents[1]
CERTIFICATE = 'A' * 40
COMMIT = '1' * 40


def thin(cpu=transport.inputs.X64, kind=2):
    return struct.pack('<8I', 0xfeedfacf, cpu, 0, kind, 0, 0, 0, 0) + b'synthetic-only'


def server_binary(revision='9' * 40, modified='false'):
    def counted(value):
        count = len(value); prefix = b''
        while count >= 128:
            prefix += bytes([(count & 127) | 128]); count >>= 7
        return prefix + bytes([count]) + value
    module = (b'0' * 16 + ('path\tgithub.com/shorebird-server/cmd/server\n'
        'build\tGOOS=darwin\nbuild\tGOARCH=amd64\nbuild\tCGO_ENABLED=0\n'
        'build\tvcs=git\nbuild\tvcs.revision=' + revision + '\nbuild\tvcs.modified=' + modified + '\n').encode() + b'0' * 16)
    info = b'\xff Go buildinf:\x08\x02' + b'\0' * 16 + counted(b'go1.26.10') + counted(module)
    header = struct.pack('<8I', 0xfeedfacf, transport.inputs.X64, 0, 2, 1, 152, 0, 0)
    segment = struct.pack('<II16sQQQQiiII', 0x19, 152, b'__DATA', 0, len(info), 184, len(info), 3, 3, 1, 0)
    section = struct.pack('<16s16sQQIIIIIIII', b'__go_buildinfo', b'__DATA', 0, len(info), 184, 0, 0, 0, 0, 0, 0, 0)
    return header + segment + section + info


def public_rsa_der(*, spki=False, exponent=65537):
    def der(tag,value):
        size=len(value)
        length=bytes([size]) if size<128 else bytes([128+(size.bit_length()+7)//8])+size.to_bytes((size.bit_length()+7)//8,'big')
        return bytes([tag])+length+value
    # Public-only 2048-bit odd modulus, canonical PKCS1 and Go PKIX encoding.
    modulus=b'\0\x80'+b'\x01'*255
    pkcs1=der(0x30,der(0x02,modulus)+der(0x02,exponent.to_bytes((exponent.bit_length()+7)//8,'big')))
    return der(0x30,bytes.fromhex('300d06092a864886f70d0101010500')+der(0x03,b'\0'+pkcs1)) if spki else pkcs1


class TransportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='shorebird-app-transport-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = Path('/private/tmp/shorebird-macos-x64-app-' + uuid.uuid4().hex)
        self.stage = self.directory / 'stage'
        self.payload = self.stage / 'payload'
        self.payload.mkdir(parents=True, mode=0o700)
        self.pins = {name: chr(97 + index) * 40 for index, name in
            enumerate(('dart_sdk', 'flutter_source', 'flutter', 'updater', 'shorebird'))}
        self.pins['engine_identity'] = hashlib.sha1(('open-shorebird-toolchain-v1\n' + ''.join(
            self.pins[key] + '\n' for key in ('dart_sdk', 'flutter_source', 'updater'))).encode()).hexdigest()
        self.pins['server'] = '9' * 40
        self.source = {'workspace_commit': COMMIT, 'components': self.pins,
                       'files': {name: '0' * 64 for name in transport.SOURCES}}
        self.signature = {'certificate_sha1': CERTIFICATE, 'team_identifier': 'TESTTEAM01',
                          'hardened_runtime': True, 'app_entitlements': {'com.apple.security.app-sandbox': True, 'com.apple.security.network.client': True}}
        self.state = {'schema': 'macos-interpreter-e2e-v1', 'variant_schema': 'macos-variant-e2e-v2',
            'work_dir': str(self.root), 'session': '012345abcdef', 'created': 123.0, 'platform': 'macos',
            'arch': 'x64', 'bundle_id': 'org.tonycloud.shorebird-macos-ffi.012345abcdef',
            'key_service': 'org.tonycloud.shorebird-macos-key.012345abcdef',
            'key_ids': {v: '012345abcdef-' + v + '-artifact' for v in ('pro', 'enterprise')},
            'app_id': '01234567-0123-4567-8901-012345678901', 'release_version': '1.2.3+4',
            'sentinel': 'f' * 48, 'full_replacement': True, 'full_replacement_ffi': True,
            'base_built': True, 'built_phases': ['base', 'pro_seed', 'pro', 'enterprise'],
            'native_source_hashes': {}, 'released_app': str(self.root / 'released/Fixture.app'),
            'server': str(self.root / 'runtime/shorebird-server'), 'api_url': 'http://127.0.0.1:32121',
            'report_url': 'http://127.0.0.1:32122/report/012345abcdef', 'assignments': []}
        self.state['release_scope'] = {k: self.state[k] for k in ('app_id', 'release_version')}
        self.state['release_scope'].update(platform='macos', arch='x86_64', runtime_profile='interpreter-full',
            base_artifact_hash=transport.sha(b'original full baseline'), host_abi_hash='e' * 64)
        self.seed = {'bootstrap_password': 'a' * 32, 'admin_password': 'b' * 32,
            'admin_password_active': True, 'signing_master_key': base64.b64encode(b'k' * 32).decode(),
            'variant_keys': {'pro': '1' * 64, 'enterprise': '2' * 64}}
        for name in transport.NATIVE:
            value = b'native-source:' + name.encode()
            self.put(name, value)
            self.state['native_source_hashes'][name[4:]] = transport.sha(value)
        self.app = 'released/Fixture.app'
        self.put(self.app + '/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': self.state['bundle_id'], 'CFBundleExecutable': 'Fixture'}), 0o644)
        self.put(self.app + '/Contents/MacOS/Fixture', thin(), 0o755)
        self.put(self.app + '/Contents/Frameworks/FlutterMacOS.framework/Versions/A/FlutterMacOS', thin(kind=6), 0o755)
        self.put(self.app + '/Contents/Resources/shorebird_replacement_base.sbr', b'original full baseline', 0o644)
        self.put(self.app + '/Contents/Resources/shorebird_release_scope_macos_x86_64.json',
            transport.encoded(self.state['release_scope']), 0o644)
        scope = self.state['release_scope']
        self.public = base64.b64encode(public_rsa_der()).decode()
        self.public_spki = base64.b64encode(public_rsa_der(spki=True)).decode()
        self.put(self.app + '/Contents/Resources/shorebird.yaml', (
            'selection_protocol: 2\npatch_verification: strict\npatch_response_verification: required\n'
            + 'patch_public_key: ' + self.public + '\naot_patch_app_build_id: ' + self.state['release_version']
            + '\naot_patch_base_snapshot_hash: ' + scope['base_artifact_hash']
            + '\naot_patch_host_abi: ' + scope['host_abi_hash'] + '\n').encode(), 0o644)
        framework = self.payload / self.app / 'Contents/Frameworks/FlutterMacOS.framework'
        (framework / 'Versions/Current').symlink_to('A')
        (framework / 'FlutterMacOS').symlink_to('Versions/Current/FlutterMacOS')
        self.put('runtime/shorebird-server', server_binary(), 0o700)
        self.build, self.server = self.provenance()
        self.put_json('provenance/build-inputs.json', self.build)
        self.put_json('provenance/server-input.json', self.server)
        self.put_json('state.json', self.state)
        self.put_json('private-bootstrap.json', self.seed)
        self.make_database()
        self.seal()
        self.patch_source = mock.patch.object(transport, 'source_identity', return_value=self.source)
        self.patch_source.start(); self.addCleanup(self.patch_source.stop)
        self.patch_signature = mock.patch.object(transport, 'inspect_bundle', return_value=self.signature)
        self.signature_mock = self.patch_signature.start(); self.addCleanup(self.patch_signature.stop)

    def use_owned_filesystem_root(self):
        """Only materializing tests relocate; archive-policy tests stay real.

        Production accepts a Mac /private/tmp root and an identically prefixed
        ZIP. Linux must never create /private. Admit exactly this test's owned
        root through ROOT's normal fullmatch, without replacing canonical().
        """
        self.root = self.directory / ('shorebird-macos-x64-app-' + uuid.uuid4().hex)
        self.state.update(work_dir=str(self.root), released_app=str(self.root / self.app),
                          server=str(self.root / 'runtime/shorebird-server'))
        self.put_json('state.json', self.state)
        self.seal()
        patch = mock.patch.object(transport, 'ROOT', re.compile(re.escape(str(self.root))))
        patch.start(); self.addCleanup(patch.stop)

    def put(self, name, value, mode=0o600):
        path = self.payload / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(value); path.chmod(mode)

    def put_json(self, name, value):
        self.put(name, transport.encoded(value))

    def provenance(self):
        pins = {k: v for k, v in self.pins.items() if k != 'server'}
        producer = {'repository': 'example/workspace', 'workspace_commit': '2' * 40,
            'run_id': '123', 'artifact_id': '456', 'artifact_name': 'macos-engine-x64',
            'metadata_origin': 'authenticated-api-export'}
        host_producer = {'kind': 'local-frozen-host', 'workspace_commit': '3' * 40,
                        'build_name': transport.inputs.HOST_NAME, 'source_manifest_sha256': '4' * 64}
        files = {name: {'sha256': '5' * 64, 'size': 1, 'executable': False}
                 for name in transport.inputs.HOST_REQUIRED}
        host = {'schema': 'macos-app-host-closure-v1', 'components': pins,
                'producer': host_producer, 'files': files}
        target = {'schema': 'macos-app-target-archive-v1', 'components': pins,
                  'producer': producer, 'archive_sha256': '6' * 64}
        build = {'schema': 'macos-app-materialized-build-inputs-v1', 'status': 'materialized_unexecuted',
            'runtime_acceptance': False, 'components': pins,
            'metadata': {'external_sha256': {'host_manifest': '7' * 64, 'target_pin': '8' * 64},
                'host_closure_canonical_sha256': transport.sha(transport.encoded(host)),
                'target_pin_canonical_sha256': transport.sha(transport.encoded(target))},
            'host': {'platform': 'macos', 'arch': 'arm64', 'machine': 'arm64',
                'out': 'out/' + transport.inputs.HOST_NAME, 'producer': host_producer,
                'original_args_sha256': '5' * 64, 'closure_files': files},
            'target': {'platform': 'macos', 'arch': 'x64', 'machine': 'x86_64',
                'out': 'out/' + transport.inputs.TARGET_NAME, 'producer': producer,
                'original_args_sha256': '5' * 64, 'archive_sha256': '6' * 64, 'archive_manifest': {}},
            'required_private_flutter_cache_bindings': [], 'pending': ['runtime'],
            'files': {'out/target': {'sha256': '5' * 64, 'size': 1, 'executable': False}}}
        server_producer = {**producer, 'workspace_commit': '4' * 40,
                           'artifact_name': 'shorebird-server-darwin-amd64'}
        server = {'schema': 'macos-app-server-input-v1', 'server_commit': self.pins['server'],
                  'binary_sha256': transport.sha(server_binary()), 'archive_sha256': '7' * 64, 'producer': server_producer}
        return build, server

    def make_database(self):
        self.objects = {}
        with closing(sqlite3.connect(self.payload / 'state.sqlite')) as db, db:
            for statement in (
                'CREATE TABLE apps(id TEXT)',
                'CREATE TABLE users(email TEXT,is_admin INTEGER,must_change_password INTEGER,auth_provider TEXT)',
                'CREATE TABLE releases(id INTEGER, app_id TEXT, version TEXT, selection_protocol INTEGER)',
                'CREATE TABLE release_selection_states(release_id INTEGER,state TEXT)',
                'CREATE TABLE account_tokens(id INTEGER)', 'CREATE TABLE patch_events(id INTEGER)',
                'CREATE TABLE rolled_back_patches(id INTEGER)', 'CREATE TABLE patch_target_devices(id INTEGER)',
                'CREATE TABLE release_platform_statuses(release_id INTEGER,platform TEXT,status TEXT)',
                'CREATE TABLE managed_signing_keys(id INTEGER,app_id TEXT,public_key_pem TEXT,wrapped_private_key TEXT)',
                'CREATE TABLE release_artifacts(id INTEGER,release_id INTEGER,platform TEXT,arch TEXT,storage_key TEXT,hash TEXT,size INTEGER,signing_mode TEXT,signing_key_id INTEGER)',
                'CREATE TABLE patches(id INTEGER,release_id INTEGER,number INTEGER)',
                'CREATE TABLE patch_artifacts(patch_id INTEGER,platform TEXT,arch TEXT,storage_key TEXT,hash TEXT,size INTEGER)'):
                db.execute(statement)
            db.execute("INSERT INTO users VALUES('macos-fixture@example.invalid',1,0,'password')")
            db.execute('INSERT INTO apps VALUES(?)', (self.state['app_id'],))
            db.execute('INSERT INTO releases VALUES(1,?,?,2)', (self.state['app_id'], self.state['release_version']))
            db.execute('INSERT INTO managed_signing_keys VALUES(1,?,?,?)', (self.state['app_id'],
                '-----BEGIN PUBLIC KEY-----\n' + self.public_spki + '\n-----END PUBLIC KEY-----\n',
                base64.b64encode(b'w' * 48).decode()))
            db.execute("INSERT INTO release_platform_statuses VALUES(1,'macos','active')")
            for number in range(5):
                name = 'fixture/object-' + str(number)
                is_release=number in (0,4)
                value = b'original-cli-object-' + str(number).encode() if is_release else b'\x28\xb5\x2f\xfdsynthetic-stored-delta-' + str(number).encode()
                target_hash = transport.sha(value if is_release else b'reconstructed-target-artifact-' + str(number).encode())
                self.put('objects/releases/' + name, value)
                self.objects['releases/'+name] = {'sha256': transport.sha(value), 'size': len(value), 'artifact_hash': target_hash}
                if number == 0:
                    db.execute("INSERT INTO release_artifacts VALUES(10,1,'macos','app',?,?,?,'managed',1)",
                               (name, target_hash, len(value)))
                elif number==4:
                    db.execute("INSERT INTO release_artifacts VALUES(11,1,'macos','macos_supplement',?,?,?,'managed',1)",
                               (name, target_hash, len(value)))
                else:
                    db.execute('INSERT INTO patches VALUES(?,1,?)', (number, number))
                    db.execute("INSERT INTO patch_artifacts VALUES(?,'macos','x86_64',?,?,?)",
                               (number, name, target_hash, len(value)))
            selection = {'scopes': {'macos/x86_64': {'record': {'scope': self.state['release_scope'],
                'release_artifact_id': 10, 'supplement_artifact_id': 11, 'published': True},
                'release_hash': self.objects['releases/fixture/object-0']['artifact_hash'],
                'supplement_hash': self.objects['releases/fixture/object-4']['artifact_hash']}},
                'published': {'macos': True}, 'mappings': {'macos/x86_64/stable/free':{'platform':'macos','arch':'x86_64',
                    'channel':'stable','license_type':'free','target':{'kind':'base','patch_number':None,
                    'artifact_hash':None,'encryption':'none','artifact_key_id':None}}}, 'selections': {}, 'idempotency': {}}
            for variant, number in (('pro', 2), ('enterprise', 3)):
                selection['mappings']['macos/x86_64/stable/' + variant] = {'platform': 'macos', 'arch': 'x86_64',
                    'channel': 'stable', 'license_type': variant, 'target': {'kind': 'patch', 'patch_number': str(number),
                    'artifact_hash': self.objects['releases/fixture/object-' + str(number)]['artifact_hash'],
                    'encryption': 'artifact-aes-256-gcm', 'artifact_key_id': self.state['key_ids'][variant]}}
            db.execute('INSERT INTO release_selection_states VALUES(1,?)', (transport.encoded(selection).decode(),))
        (self.payload / 'state.sqlite').chmod(0o600)

    def seal(self):
        (self.payload / transport.MANIFEST).unlink(missing_ok=True)
        self.manifest = {'schema': transport.SCHEMA, 'status': 'prepared_runtime_pending',
            'canonical_root': str(self.root), 'source': self.source, 'certificate_sha1': CERTIFICATE,
            'signature': self.signature, 'app': self.app, 'objects': self.objects,
            'files': transport.scan(self.payload), 'input_pins': {name: transport.sha(transport.read(
                self.payload, 'provenance/' + name + '.json')) for name in ('build-inputs', 'server-input')}}
        self.put_json(transport.MANIFEST, self.manifest)

    def package(self):
        archive = self.directory / ('prepared-' + uuid.uuid4().hex + '.zip')
        digest = transport.package(self.stage, archive, WORKSPACE, COMMIT, self.pins, CERTIFICATE)
        return archive, digest

    def rewrite_zip(self, archive, mutate):
        with zipfile.ZipFile(archive) as source:
            members = [(entry, source.read(entry)) for entry in source.infolist()]
        members = mutate(members)
        with zipfile.ZipFile(archive, 'w') as target:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                for entry, data in members:
                    target.writestr(entry, data)
        return transport.inputs.digest(archive)

    def validate(self, archive, digest):
        return transport.validate_archive(archive, digest, WORKSPACE, COMMIT, self.pins, CERTIFICATE)

    def mutate_selection(self, change):
        with closing(sqlite3.connect(self.payload / 'state.sqlite')) as db, db:
            state = json.loads(db.execute('SELECT state FROM release_selection_states').fetchone()[0])
            change(state)
            db.execute('UPDATE release_selection_states SET state=?', (json.dumps(state),))

    def test_round_trip_preserves_links_modes_and_producer_roots(self):
        archive, digest = self.package()
        root, manifest = self.validate(archive, digest)
        self.assertEqual(root, self.root)
        self.assertEqual(manifest, self.manifest)
        self.assertNotEqual(COMMIT, self.build['target']['producer']['workspace_commit'])
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
            path = Path(temporary)
            transport.materialize_archive(archive, root, manifest, path)
            found = transport.scan(path); found.pop(transport.MANIFEST)
            self.assertEqual(found, manifest['files'])
        self.assertFalse(root.exists())

    def test_original_mac_canonical_policy_accepts_only_the_exact_private_tmp_namespace(self):
        original = '/private/tmp/shorebird-macos-x64-app-01234567'
        self.assertEqual(transport.canonical(original), Path(original))
        self.assertEqual(transport.canonical('/private/tmp/shorebird-macos-x64-app-' + 'a' * 80),
                         Path('/private/tmp/shorebird-macos-x64-app-' + 'a' * 80))
        for path in (str(self.directory / 'shorebird-macos-x64-app-01234567'),
                     '/tmp/shorebird-macos-x64-app-01234567', '/private/tmp/foreign-01234567',
                     '/private/tmp/shorebird-macos-x64-app-short', original + '/child',
                     original + '/../outside', '/private/tmp/shorebird-macos-x64-app-' + 'a' * 81):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.canonical(path)

    def test_materializing_fixture_admits_only_its_owned_root(self):
        self.use_owned_filesystem_root()
        self.assertTrue(self.root.is_relative_to(self.directory))
        self.assertEqual(transport.canonical(str(self.root)), self.root)
        for path in (str(self.root / 'child'), str(self.root.parent / 'foreign'),
                     '/private/tmp/shorebird-macos-x64-app-01234567'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.canonical(path)

    def test_prepare_projects_private_seed_without_mutating_fixture(self):
        self.use_owned_filesystem_root()
        shutil.copytree(self.payload, self.root, symlinks=True)
        self.addCleanup(lambda: shutil.rmtree(self.root) if self.root.exists() else None)
        original = {**self.state, 'cli': '/local/cli', 'flutter_root': '/local/flutter', 'serve_started': 10}
        (self.root / 'state.json').write_bytes(transport.encoded(original))
        (self.root / 'secrets.json').write_bytes(transport.encoded({**self.seed, 'access_token': 'old-private-token', 'jwt_secret': 'old-private-jwt'}))
        with closing(sqlite3.connect(self.root/'state.sqlite')) as db, db:
            db.execute('INSERT INTO account_tokens VALUES(1)')
        before = transport.scan(self.root)
        destination = self.directory / 'fresh-preparation'
        build_path = self.payload / 'provenance/build-inputs.json'
        server_path = self.payload / 'provenance/server-input.json'
        with mock.patch.object(Path, 'home', return_value=self.directory / 'home'):
            manifest = transport.prepare(self.root, destination, WORKSPACE, COMMIT, self.pins,
                build_path, transport.inputs.digest(build_path), self.root / 'runtime/shorebird-server',
                server_path, transport.inputs.digest(server_path), CERTIFICATE)
        self.assertEqual(transport.scan(self.root), before)
        self.assertEqual(transport.json_file(destination / 'payload', 'private-bootstrap.json'), self.seed)
        self.assertEqual(transport.json_file(destination / 'payload', 'state.json'), self.state)
        self.assertFalse((destination / 'payload/secrets.json').exists())
        files = transport.scan(destination / 'payload'); files.pop(transport.MANIFEST)
        self.assertEqual(manifest['files'], files)
        self.assertEqual(manifest['status'], 'prepared_runtime_pending')
        with closing(sqlite3.connect(destination/'payload/state.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM account_tokens').fetchone(),(0,))
        # Token pruning must never erase prior assignment authority in order to
        # admit a used fixture. Failure removes only this new private stage.
        with closing(sqlite3.connect(self.root/'state.sqlite')) as db, db:
            selection=json.loads(db.execute('SELECT state FROM release_selection_states').fetchone()[0])
            selection['selections']['prior-installation']={}
            db.execute('UPDATE release_selection_states SET state=?',(json.dumps(selection),))
        used_before=transport.scan(self.root); rejected=self.directory/'rejected-used-fixture'
        with mock.patch.object(Path,'home',return_value=self.directory/'home'), self.assertRaises(ValueError):
            transport.prepare(self.root,rejected,WORKSPACE,COMMIT,self.pins,build_path,transport.inputs.digest(build_path),
                self.root/'runtime/shorebird-server',server_path,transport.inputs.digest(server_path),CERTIFICATE)
        self.assertFalse(rejected.exists()); self.assertEqual(transport.scan(self.root),used_before)

    def test_go_server_metadata_requires_clean_pinned_native_producer(self):
        transport.validate_server(server_binary(), self.server)
        for data in (thin(), server_binary(revision='0' * 40), server_binary(modified='true')):
            pin = {**self.server, 'binary_sha256': transport.sha(data)}
            with self.subTest(data=transport.sha(data)), self.assertRaises(ValueError):
                transport.validate_server(data, pin)

    def test_port_collision_closes_probes_without_stopping_listener(self):
        first, second = mock.Mock(), mock.Mock()
        second.bind.side_effect = OSError(48, 'occupied')
        with mock.patch.object(transport.socket, 'socket', side_effect=[first, second]), self.assertRaises(OSError):
            transport.available_ports(self.state)
        first.close.assert_called_once(); second.close.assert_called_once()

    def test_runtime_projection_omits_build_inputs_and_token(self):
        original = {**self.state, 'server': '/local/build/server', 'cli': '/local/cli',
                    'engine_src': '/local/engine', 'serve_started': 10}
        self.assertEqual(transport.project_state(original, self.root), self.state)
        for field in ('installation_id', 'observed_selection', 'pending_assignment', 'cleanup', 'server_pid', 'launches'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                transport.project_state({**original, field: None}, self.root)

    def test_full_ffi_and_exact_state_fields_required(self):
        for field in ('full_replacement', 'full_replacement_ffi'):
            for value in (False, None, 1):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    transport.validate_state({**self.state, field: value}, self.root)
        with self.assertRaises(ValueError):
            transport.validate_state({**self.state, 'cli': '/bin/sh'}, self.root)

    def test_private_seed_allowlist_and_distinct_nonzero_keys(self):
        for change in ({'access_token': 'forbidden'}, {'jwt_secret': 'forbidden'},
                       {'variant_keys': {'pro': '0' * 64, 'enterprise': '2' * 64}},
                       {'variant_keys': {'pro': '2' * 64, 'enterprise': '2' * 64}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                transport.validate_seed({**self.seed, **change})

    def test_absence_permission_error_is_not_freshness(self):
        with mock.patch.object(Path, 'lstat', side_effect=PermissionError(13, 'private')), self.assertRaises(PermissionError):
            transport.absent(self.root)

    def test_db_prior_authority_and_mapping_drift_rejected(self):
        for change in (lambda s: s['selections'].update({'old': {}}),
                       lambda s: s['idempotency'].update({'old': {}}),
                       lambda s: s['published'].update({'macos': False}),
                       lambda s: s['mappings'].clear(),
                       lambda s: s['scopes']['macos/x86_64'].update(release_hash='f' * 64)):
            with self.subTest(change=change):
                old = (self.payload / 'state.sqlite').read_bytes()
                self.mutate_selection(change)
                with self.assertRaises(ValueError):
                    transport.database_objects(self.payload, self.state, self.app)
                self.put('state.sqlite', old)

    def test_database_wrong_public_key_objects_tokens_and_wal(self):
        original_object=(self.payload/'objects/releases/fixture/object-2').read_bytes()
        self.put('objects/releases/fixture/object-2', b'corrupt')
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)
        self.put('objects/releases/fixture/object-2', original_object)
        with closing(sqlite3.connect(self.payload / 'state.sqlite')) as db, db: db.execute('INSERT INTO account_tokens VALUES(1)')
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)
        with closing(sqlite3.connect(self.payload / 'state.sqlite')) as db, db: db.execute('DELETE FROM account_tokens')
        policy = self.payload / self.app / 'Contents/Resources/shorebird.yaml'
        original = policy.read_bytes(); policy.write_bytes(original.replace(self.public.encode(), base64.b64encode(b'wrong public key')))
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)
        policy.write_bytes(original)
        self.put('state.sqlite-wal', b'not a closed seed')
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)

    def test_storage_namespace_and_distinct_patch_target_hashes(self):
        objects=transport.database_objects(self.payload,self.state,self.app)
        self.assertEqual(objects,self.objects)
        for number in (1,2,3):
            item=objects['releases/fixture/object-'+str(number)]
            self.assertNotEqual(item['sha256'],item['artifact_hash'])
        for number in (0,1):
            path=self.payload/('objects/releases/fixture/object-'+str(number))
            path.rename(self.payload/('objects/fixture-object-'+str(number)))
            with self.subTest(number=number), self.assertRaises(OSError):
                transport.database_objects(self.payload,self.state,self.app)
            (self.payload/('objects/fixture-object-'+str(number))).rename(path)
        self.assertNotEqual(base64.b64decode(self.public),base64.b64decode(self.public_spki))

    def test_mac_producer_app_supplement_scope_and_all_three_mappings(self):
        objects=transport.database_objects(self.payload,self.state,self.app)
        self.assertEqual(len(objects),5)
        self.assertIn('releases/fixture/object-4',objects)
        original=(self.payload/'state.sqlite').read_bytes()
        for sql in ("DELETE FROM release_artifacts WHERE arch='macos_supplement'",
                    "UPDATE release_artifacts SET arch='x86_64' WHERE arch='app'",
                    "UPDATE release_artifacts SET signing_key_id=2 WHERE arch='macos_supplement'",
                    "INSERT INTO release_artifacts VALUES(12,1,'macos','unexpected','unexpected','"+("0"*64)+"',1,'managed',1)"):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.payload/'state.sqlite')) as db,db: db.execute(sql)
                with self.assertRaises(ValueError): transport.database_objects(self.payload,self.state,self.app)
                self.put('state.sqlite',original)
        for change in (lambda state: state['scopes']['macos/x86_64']['record'].update(supplement_artifact_id=None),
                       lambda state: state['scopes']['macos/x86_64'].update(supplement_hash='0'*64),
                       lambda state: state['mappings'].pop('macos/x86_64/stable/free'),
                       lambda state: state['mappings']['macos/x86_64/stable/free']['target'].update(encryption='artifact-aes-256-gcm')):
            with self.subTest(change=change):
                self.mutate_selection(change)
                with self.assertRaises(ValueError): transport.database_objects(self.payload,self.state,self.app)
                self.put('state.sqlite',original)
        path=self.payload/'objects/releases/fixture/object-4'; value=path.read_bytes()
        path.write_bytes(value[:-1]+bytes([value[-1]^1]))
        with self.assertRaises(ValueError): transport.database_objects(self.payload,self.state,self.app)

    def test_stored_patch_corruption_and_reconstructed_hash_drift_rejected(self):
        path=self.payload/'objects/releases/fixture/object-2'
        data=path.read_bytes()
        path.write_bytes(data[:-1]+bytes([data[-1]^1]))
        with self.assertRaises(ValueError):
            transport.validate_payload(self.payload,self.manifest,WORKSPACE,self.source,CERTIFICATE,inspect=False)
        path.write_bytes(data)
        # Mapping identity remains the signed reconstructed target, never the
        # independently inventoried compressed upload/download byte hash.
        self.mutate_selection(lambda state: state['mappings']['macos/x86_64/stable/pro']['target'].update(
            artifact_hash=self.objects['releases/fixture/object-2']['sha256']))
        with self.assertRaises(ValueError): transport.database_objects(self.payload,self.state,self.app)

    def test_rsa_spki_and_pkcs1_normalize_strict_public_parameters(self):
        identity=transport.rsa_public_identity(public_rsa_der(),spki=False)
        self.assertEqual(identity,transport.rsa_public_identity(public_rsa_der(spki=True),spki=True))
        self.assertNotEqual(identity,transport.rsa_public_identity(public_rsa_der(exponent=3),spki=False))
        for der,spki in ((public_rsa_der()+b'\0',False),
                         (public_rsa_der(),True),
                         (public_rsa_der(spki=True).replace(bytes.fromhex('2a864886f70d010101'),bytes.fromhex('2a864886f70d010102')),True),
                         (b'\x30\x80',False)):
            with self.subTest(spki=spki), self.assertRaises(ValueError): transport.rsa_public_identity(der,spki=spki)
        policy=self.payload/self.app/'Contents/Resources/shorebird.yaml'
        policy.write_bytes(policy.read_bytes().replace(self.public.encode(),base64.b64encode(public_rsa_der(exponent=3))))
        with self.assertRaises(ValueError): transport.database_objects(self.payload,self.state,self.app)

    def test_database_seed_password_flag_and_foreign_accounts_rejected(self):
        self.put_json('private-bootstrap.json', {**self.seed, 'admin_password_active': False})
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)
        self.put_json('private-bootstrap.json', self.seed)
        with closing(sqlite3.connect(self.payload / 'state.sqlite')) as db, db:
            db.execute("INSERT INTO users VALUES('foreign@example.invalid',0,0,'password')")
        with self.assertRaises(ValueError): transport.database_objects(self.payload, self.state, self.app)

    def test_runtime_file_injection_and_private_modes_rejected(self):
        for path in ('unexpected.sh', 'app/pubspec.yaml', 'runtime/compiler', 'objects/unreferenced'):
            with self.subTest(path=path):
                self.put(path, b'unexpected')
                with self.assertRaises(ValueError):
                    transport.validate_inventory(transport.scan(self.payload), self.app, self.objects)
                (self.payload / path).unlink()
        entries = copy.deepcopy(self.manifest['files']); entries['private-bootstrap.json']['mode'] = 0o644
        with self.assertRaises(ValueError): transport.validate_inventory(entries, self.app, self.objects)

    def test_links_reject_escape_dangling_cycles_and_nonapp(self):
        link = self.app + '/Contents/Frameworks/FlutterMacOS.framework/Versions/Current'
        for target in ('/private/tmp/foreign', '../A', 'Missing', 'Current'):
            entries = copy.deepcopy(self.manifest['files']); entries[link]['target'] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                transport.validate_inventory(entries, self.app, self.objects)
        entries = copy.deepcopy(self.manifest['files']); entries['runtime/link'] = {'type': 'link', 'target': 'shorebird-server'}
        with self.assertRaises(ValueError): transport.validate_inventory(entries, self.app, self.objects)
        entries = copy.deepcopy(self.manifest['files']); entries[self.app+'/Contents/MacOS/Alias'] = {'type': 'link', 'target': 'Fixture'}
        with self.assertRaises(ValueError): transport.validate_inventory(entries, self.app, self.objects)

    def test_hardlink_and_case_collision_rejected(self):
        os.link(self.payload / 'state.json', self.payload / 'hardlink')
        with self.assertRaises(ValueError): transport.scan(self.payload)
        (self.payload / 'hardlink').unlink()
        entries = copy.deepcopy(self.manifest['files']); entries['STATE.json'] = entries['state.json']
        with self.assertRaises(ValueError): transport.validate_inventory(entries, self.app, self.objects)

    def test_archive_rejects_external_hash_duplicate_extra_traversal_and_modes(self):
        archive, digest = self.package()
        with self.assertRaises(ValueError): self.validate(archive, '0' * 64)
        original = archive.read_bytes()
        for change in (lambda m: m + [m[0]],
                       lambda m: m + [(zipfile.ZipInfo('../escape'), b'bad')],
                       lambda m: m + [(zipfile.ZipInfo(str(self.root).lstrip('/') + '/extra'), b'bad')]):
            archive.write_bytes(original)
            digest = self.rewrite_zip(archive, change)
            with self.subTest(change=change), self.assertRaises(ValueError): self.validate(archive, digest)
        archive.write_bytes(original)
        def mode_change(members):
            members[0][0].external_attr = (stat.S_IFREG | 0o777) << 16
            return members
        digest = self.rewrite_zip(archive, mode_change)
        with self.assertRaises(ValueError): self.validate(archive, digest)

    def test_semantic_archive_rejected_before_root_creation(self):
        self.put_json('state.json', {**self.state, 'full_replacement_ffi': False})
        self.seal()
        # Write directly: package correctly refuses semantically bad input.
        archive = self.directory / 'invalid.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            prefix = str(self.root).lstrip('/') + '/'
            for name, item in {**self.manifest['files'], transport.MANIFEST: {'type': 'file', 'mode': 0o600}}.items():
                entry = zipfile.ZipInfo(prefix + name + ('/' if item['type'] == 'directory' else ''))
                entry.create_system = 3
                entry.external_attr = ({'file': stat.S_IFREG, 'directory': stat.S_IFDIR, 'link': stat.S_IFLNK}[item['type']] | item.get('mode', 0o777)) << 16
                zipped.writestr(entry, item['target'].encode() if item['type'] == 'link' else b'' if item['type'] == 'directory' else transport.read(self.payload, name))
        with self.assertRaises(ValueError): self.validate(archive, transport.inputs.digest(archive))
        self.assertFalse(self.root.exists())

    def test_compiled_scope_native_source_server_and_provenance_drift(self):
        for name, data in ((transport.NATIVE[0], b'changed source'),
                           ('runtime/shorebird-server', thin(transport.inputs.ARM64)),
                           (self.app + '/Contents/Resources/shorebird_replacement_base.sbr', b'changed original')):
            with self.subTest(name=name):
                original = (self.payload / name).read_bytes(); mode = stat.S_IMODE((self.payload / name).stat().st_mode)
                self.put(name, data, mode); self.seal()
                with self.assertRaises(ValueError):
                    transport.validate_payload(self.payload, self.manifest, WORKSPACE, self.source, CERTIFICATE, inspect=False)
                self.put(name, original, mode)
        changed = copy.deepcopy(self.build); changed['host']['producer']['workspace_commit'] = COMMIT
        with self.assertRaises(ValueError): transport.provenance(changed, self.server, self.pins)
        changed = copy.deepcopy(self.build); changed['command'] = '/bin/sh'
        with self.assertRaises(ValueError): transport.provenance(changed, self.server, self.pins)

    def test_extract_native_admission_and_owned_cleanup(self):
        self.use_owned_filesystem_root()
        archive, digest = self.package()
        # Actual Mac-prefix ZIP admission is exercised by the round-trip and
        # hostile-archive tests above. Here the admitted root is relocated so
        # real materialize/validate/ownership/cleanup operations stay in temp.
        transport.validate_payload(self.payload, self.manifest, WORKSPACE, self.source, CERTIFICATE, inspect=False)
        with mock.patch.object(transport, 'fixture_modules') as modules:
            modules.return_value.__enter__.return_value = (None, SimpleNamespace(require_native_host=mock.Mock(side_effect=ValueError('not native'))))
            # Mock only the native probe after semantic validation has succeeded.
            with mock.patch.object(transport, 'validate_archive', return_value=(self.root, self.manifest)):
                with self.assertRaises(ValueError):
                    transport.extract(archive, digest, WORKSPACE, COMMIT, self.pins, CERTIFICATE, self.directory / 'evidence')
        self.assertFalse(self.root.exists())
        original_modules = transport.fixture_modules
        @contextmanager
        def native_modules(workspace):
            with original_modules(workspace) as (variant, lifecycle):
                with mock.patch.object(lifecycle, 'require_native_host'):
                    yield variant, lifecycle
        with mock.patch.object(transport, 'fixture_modules', native_modules), \
                mock.patch.object(transport, 'validate_archive', return_value=(self.root, self.manifest)), \
                mock.patch.object(Path, 'home', return_value=self.directory / 'home'):
            result = transport.extract(archive, digest, WORKSPACE, COMMIT, self.pins, CERTIFICATE, self.directory / 'evidence')
        self.addCleanup(lambda: transport.cleanup(result['ownership_receipt']) if self.root.exists() else None)
        self.assertFalse(result['runtime_acceptance'])
        credentials = transport.json_file(self.root, 'secrets.json')
        self.assertEqual(set(credentials), transport.SEED_KEYS | {'jwt_secret'})
        self.assertNotIn('access_token', credentials)
        self.assertEqual(stat.S_IMODE((self.root / 'secrets.json').stat().st_mode), 0o600)
        receipt = Path(result['ownership_receipt']); saved = receipt.read_bytes()
        changed = json.loads(saved); changed['inode'] += 1; receipt.write_bytes(transport.encoded(changed))
        with self.assertRaises(ValueError): transport.cleanup(receipt)
        self.assertTrue(self.root.is_dir())
        receipt.write_bytes(saved); transport.cleanup(receipt); transport.cleanup(receipt)
        self.assertFalse(self.root.exists())

    def test_source_identity_checks_gitlinks_clean_tree_and_helper_bytes(self):
        self.patch_source.stop()
        def verified_git(workspace, *arguments):
            if arguments == ('rev-parse', '--show-toplevel'): return str(WORKSPACE)
            if arguments == ('rev-parse', 'HEAD'): return COMMIT
            if arguments[0] == 'diff': return ''
            if arguments[0] == 'ls-files': return arguments[-1]
            if arguments[0] == 'ls-tree':
                name = arguments[-1]
                return '160000 commit ' + self.pins[transport.LINKS[name]] + '\t' + name
            self.fail('Unexpected git arguments')
        with mock.patch.object(transport, 'git', side_effect=verified_git):
            result = transport.source_identity(WORKSPACE, COMMIT, self.pins)
        self.assertEqual(result['components'], self.pins)
        self.assertEqual(set(result['files']), set(transport.SOURCES))
        for kind in ('dirty', 'gitlink'):
            def invalid_git(workspace, *arguments):
                if kind == 'dirty' and arguments[0] == 'diff': return 'tracked.py'
                if kind == 'gitlink' and arguments[0] == 'ls-tree': return '160000 commit ' + '0' * 40 + '\t' + arguments[-1]
                return verified_git(workspace, *arguments)
            with self.subTest(kind=kind), mock.patch.object(transport, 'git', side_effect=invalid_git), self.assertRaises(ValueError):
                transport.source_identity(WORKSPACE, COMMIT, self.pins)
        self.patch_source.start()

    def test_real_source_identity_rejects_wrong_external_commit(self):
        self.patch_source.stop()
        with self.assertRaises(ValueError): transport.source_identity(WORKSPACE, '0' * 40, self.pins)
        self.patch_source.start()


if __name__ == '__main__':
    unittest.main()

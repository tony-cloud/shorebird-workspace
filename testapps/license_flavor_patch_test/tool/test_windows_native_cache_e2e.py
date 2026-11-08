"""Pure protocol/oracle tests; no services, builds, engine, or device required."""
import contextlib
import copy
from email.message import Message
import importlib.util
import io
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'windows_native_cache_e2e', Path(__file__).with_name('windows_native_cache_e2e.py'))
e2e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e2e)


def context():
    return {'app_id': '7cd314d0-8ace-46a2-b21b-81a92bcd0738', 'release_id': 17,
            'patch_number': 3, 'patch_hash': 'a' * 64, 'transport_hash': 'b' * 64,
            'platform': 'windows', 'arch': 'x86_64', 'device_id_hash': 'c' * 64}


class DeliveryProtocolTest(unittest.TestCase):
    def setUp(self):
        self.context = context()
        self.aad = e2e.canonical_aad(self.context)
        self.authority = e2e.KeyAuthority(b'fixture-secret', 'private-token')
        self.authority.allow(self.context)
        self.key_id, self.key = e2e.delivery_material(b'fixture-secret', self.aad)
        self.request = json.dumps({'key_id': self.key_id, 'aad': self.aad}).encode()

    def test_canonical_order_matches_go_contract(self):
        expected = ('open-aot-patch-delivery-v2\n'
                    'app_id=7cd314d0-8ace-46a2-b21b-81a92bcd0738\n'
                    'release_id=17\npatch_number=3\npatch_hash=' + 'a' * 64 +
                    '\ntransport_hash=' + 'b' * 64 + '\nplatform=windows\narch=x86_64\ndevice_id_hash=' + 'c' * 64)
        self.assertEqual(self.aad, expected)
        self.assertEqual(e2e.parse_aad(expected), {k: str(v) for k, v in self.context.items()})
        # Detect a source protocol migration before the Windows fixture starts.
        source = (e2e.ROOT / 'shorebird-server/internal/api/handlers/device_delivery.go').read_text()
        self.assertIn('"key\\x00"+aad', source)
        self.assertIn('"key-id\\x00"+aad', source)
        self.assertIn('hmacDigest(h.PatchDelivery.AESSecret, []byte("key-id\\x00"+aad))[:12]', source)
        self.assertIn('open-aot-patch-delivery-v2\\napp_id=%s\\nrelease_id=%d\\npatch_number=%d\\npatch_hash=%s\\ntransport_hash=%s\\nplatform=%s\\narch=%s\\ndevice_id_hash=%s', source)

    def test_hmac_known_vector_and_exact_hex_response(self):
        self.assertEqual(self.key_id, 'sQlTbnI3MlPc8POe')
        self.assertEqual(self.key.hex(), '6b5e20ae5b107798a714a0e87c3b3a20c474a8dd7a11df2f3693d6a58b68f53b')
        response = self.authority.provision('Bearer private-token', self.request)
        self.assertEqual(response, self.key.hex().encode())
        self.assertEqual(len(response), 64)
        self.assertEqual(self.authority.accepted, 1)

    def test_malformed_duplicate_unknown_and_noncanonical_aad_rejected(self):
        variants = [self.aad + '\n', self.aad + '\nunknown=value',
                    self.aad.replace('release_id=17', 'release_id=017'),
                    self.aad.replace('release_id=17', 'release_id=-1'),
                    self.aad.replace('release_id=17', 'patch_number=17'),
                    self.aad.replace('arch=x86_64', 'arch=amd64'),
                    self.aad.replace('platform=windows', 'platform=linux'),
                    self.aad.replace('app_id=7cd', 'app_id=7CD'),
                    self.aad.replace('patch_hash=' + 'a' * 64, 'patch_hash=' + 'A' * 64),
                    self.aad.replace('transport_hash=' + 'b' * 64, 'transport_hash=' + 'b' * 63),
                    self.aad.replace('arch=x86_64', 'arch=x86_64\r'),
                    None, [], '\ud800', 'x' * (e2e.MAX_BODY + 1)]
        for variant in variants:
            with self.subTest(variant=repr(variant)[:50]), self.assertRaises((ValueError, UnicodeError)):
                e2e.parse_aad(variant)

    def test_rejects_bad_token_key_id_unknown_context_and_duplicate_json(self):
        requests = [(None, self.request), ('Bearer wrong', self.request),
                    ('Bearer private-token', b'{"key_id":"x","key_id":"y","aad":"x"}'),
                    ('Bearer private-token', b'{"key_id":"x","aad":"x","extra":true}'),
                    ('Bearer private-token', b'[]'), ('Bearer private-token', b'\xff'),
                    ('Bearer private-token', b'x' * (e2e.MAX_BODY + 1))]
        for field, value in (('key_id', 'invalid'), ('aad', self.aad.replace('release_id=17', 'release_id=18')),
                             ('key_id', None), ('aad', [])):
            body = {'key_id': self.key_id, 'aad': self.aad}
            body[field] = value
            requests.append(('Bearer private-token', json.dumps(body).encode()))
        for authorization, request in requests:
            with self.subTest(request=request[:30]), self.assertRaises(ValueError):
                self.authority.provision(authorization, request)
        self.assertEqual(self.authority.accepted, 0)
        self.assertEqual(self.authority.rejected, len(requests))

    def test_empty_allowlist_fails_closed_even_with_valid_derivation(self):
        self.authority.allowed_aad = None
        with self.assertRaises(ValueError):
            self.authority.provision('Bearer private-token', self.request)

    def test_duplicate_json_rejected_at_every_nesting_level(self):
        for value in ('{"a":1,"a":2}', '{"a":{"b":1,"b":2}}'):
            with self.assertRaises(ValueError):
                e2e.strict_json(value)


class BodyFramingTest(unittest.TestCase):
    @staticmethod
    def handler(headers, body=b'{}'):
        value = mock.Mock()
        value.headers = Message()
        for name, content in headers:
            value.headers[name] = content
        value.rfile = io.BytesIO(body)
        return value

    def test_bounded_exact_json_and_standard_dart_charset(self):
        for content_type in ('application/json', 'application/json; charset=utf-8'):
            handler = self.handler([('Content-Length', '2'), ('Content-Type', content_type)])
            self.assertEqual(e2e.read_body(handler), b'{}')
            handler.connection.settimeout.assert_called_once_with(5)

    def test_rejects_ambiguous_or_oversized_framing(self):
        variants = [[], [('Content-Length', '2'), ('Content-Length', '2')],
                    [('Content-Length', '-1')], [('Content-Length', str(e2e.MAX_BODY + 1))],
                    [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')],
                    [('Content-Length', '3')], [('Content-Length', '0')],
                    [('Content-Length', '2'), ('Content-Type', 'text/plain')]]
        for headers in variants:
            headers = headers + [('Content-Type', 'application/json')]
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                e2e.read_body(self.handler(headers))


class RenderedSelectionTest(unittest.TestCase):
    def report(self, phase='U'):
        return {'schema': e2e.SCHEMA, 'nonce': 'new-process', 'pid': 42, 'post_frame': True,
                'rendered': ['license:pro', 'pro-feature:enabled'] if phase == 'U' else
                            ['license:free', 'pro-feature:off'], 'current_patch': 3 if phase == 'U' else 0}

    def test_rendered_u_and_base_exactly_bind_current_process_and_selection(self):
        for phase, patch in (('U', 3), ('BASE', 0)):
            report = self.report(phase)
            self.assertEqual(e2e.validate_report(report, 'new-process', 42, patch, phase), report)

    def test_stale_preframe_and_mismatching_selection_cannot_satisfy_receipt(self):
        for field, value in (('schema', 'license-v1'), ('nonce', 'old-process'), ('pid', 41),
                             ('pid', True), ('post_frame', False), ('current_patch', 0),
                             ('current_patch', True), ('rendered', ['license:pro']),
                             ('rendered', ['license:free', 'pro-feature:off'])):
            report = self.report()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                e2e.validate_report(report, 'new-process', 42, 3, 'U')
        report = self.report()
        report['pre_run_app_status'] = True
        with self.assertRaises(ValueError):
            e2e.validate_report(report, 'new-process', 42, 3, 'U')

    def test_embedded_fixture_reads_render_object_after_frame_and_real_dll_symbol(self):
        source = e2e.DART_FIXTURE
        self.assertIn('addPostFrameCallback', source)
        self.assertIn('object is! RenderParagraph', source)
        self.assertIn('object.text.toPlainText()', source)
        self.assertIn("DynamicLibrary.open('flutter_windows.dll')", source)
        self.assertIn('shorebird_current_boot_patch_number', source)
        self.assertNotIn('writeLaunchStatus', source)


class CacheOracleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'updater'
        self.keys = self.root / 'dpapi'
        self.directory = self.cache / 'patches/3'
        self.directory.mkdir(parents=True)
        self.keys.mkdir()
        self.transport = b'authenticated ciphertext fixture'
        self.state = {'kind': 'InstalledEncryptedTransportV1', 'signature': 'public-signature',
                      'size': 123456, 'ciphertext_size': len(self.transport)}
        self.write_state()
        (self.directory / 'transport.aesgcm').write_bytes(self.transport)
        (self.cache / 'pointers.json').write_text('{"next_boot_patch":3}')

    def write_state(self):
        (self.directory / 'state.json').write_text(json.dumps(self.state))

    def test_installed_oracle_requires_real_ciphertext_kind_signature_size_and_selection(self):
        evidence = e2e.installed_evidence(self.cache, 3)
        self.assertEqual(evidence['transport_sha256'], e2e.sha256(self.transport))
        for field, value in (('kind', 'Installed'), ('signature', None), ('ciphertext_size', 1)):
            original = copy.deepcopy(self.state)
            self.state[field] = value
            self.write_state()
            with self.subTest(field=field), self.assertRaises(ValueError):
                e2e.installed_evidence(self.cache, 3)
            self.state = original
        self.write_state()
        (self.cache / 'pointers.json').write_text('{"next_boot_patch":null}')
        with self.assertRaises(ValueError):
            e2e.installed_evidence(self.cache, 3)

    def test_final_audit_explicitly_does_not_claim_transient_tracing(self):
        (self.keys / ('d' * 64 + '.dpapi')).write_bytes(b'opaque DPAPI blob')
        result = e2e.scan_cache(self.cache, self.keys, patch_hash='e' * 64)
        self.assertEqual(result['scope'], 'final-files-only')
        self.assertIs(result['transient_write_trace'], False)
        self.assertEqual(len(result['files']), 4)

    def test_raw_native_payload_and_executable_paths_rejected(self):
        for name, content in (('unexpected.bin', b'\x7fELFplain'), ('unexpected.bin', b'MZplain'),
                              ('app.so', b'anything'), ('dlc.vmcode', b'anything'),
                              ('unexpected.bin', b'real native image bytes')):
            path = self.cache / name
            path.write_bytes(content)
            digest = e2e.sha256(content) if content == b'real native image bytes' else 'e' * 64
            with self.subTest(name=name, content=content), self.assertRaises(RuntimeError):
                e2e.scan_cache(self.cache, self.keys, patch_hash=digest)
            path.unlink()

    def test_dpapi_cache_namespace_size_and_aliases_rejected(self):
        for name, content in (('key.txt', b'plaintext'), ('d' * 64 + '.dpapi', b''),
                              ('d' * 64 + '.dpapi', b'x' * 16385)):
            path = self.keys / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                e2e.scan_cache(self.cache, self.keys, patch_hash='e' * 64)
            path.unlink()
        target = self.root / 'target'
        target.write_bytes(b'opaque')
        link = self.keys / ('d' * 64 + '.dpapi')
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('Host lacks unprivileged symlink creation')
        with self.assertRaises(RuntimeError):
            e2e.scan_cache(self.cache, self.keys, patch_hash='e' * 64)

    def test_plaintext_key_disguised_as_dpapi_blob_is_rejected(self):
        key = b'private derived key material 1234'
        (self.keys / ('d' * 64 + '.dpapi')).write_bytes(b'prefix' + key + b'suffix')
        with self.assertRaisesRegex(e2e.FixtureError, 'Plaintext secret'):
            e2e.scan_cache(self.cache, self.keys, patch_hash='e' * 64, forbidden_secrets=(key,))


class ReleasePolicyTest(unittest.TestCase):
    def test_actual_bundled_policy_requires_native_budget_key_and_strict_response(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            path = bundle / 'data/flutter_assets/shorebird.yaml'
            path.parent.mkdir(parents=True)
            source = ('app_id: disposable-app\npatch_verification: strict\n'
                      'patch_response_verification: required\nnative_patch_max_bytes: 268435456\n'
                      'patch_public_key: cHVibGljLXZlcmlmaWNhdGlvbi1rZXk=\n')
            path.write_text(source)
            self.assertEqual(e2e.verify_release_policy(bundle, 'disposable-app')['patch_response_verification'], 'required')
            variants = [source.replace('strict', 'legacy'), source.replace('required', 'optional'),
                        source.replace('268435456', '0'), source.replace('disposable-app', 'another-app'),
                        source + 'aot_patch_runtime_mode: dart-bytecode-interpreter\n',
                        source.replace('cHVibGljLXZlcmlmaWNhdGlvbi1rZXk=', 'invalid'),
                        source + 'patch_verification: strict\n']
            for value in variants:
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(e2e.FixtureError):
                    e2e.verify_release_policy(bundle, 'disposable-app')


class ToolchainPreparationTest(unittest.TestCase):
    def test_invalid_pinned_engine_identity_does_not_rewrite_cache_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            flutter = Path(temp)
            (flutter / 'bin/internal').mkdir(parents=True)
            (flutter / 'bin/cache').mkdir()
            stamp, realm = (flutter / 'bin/cache' / name for name in ('engine.stamp', 'engine.realm'))
            stamp.write_text('copied stamp')
            realm.write_text('copied realm')
            for revision in ('', 'engine-id', 'a' * 39, 'a' * 41, 'a' * 40 + '\n' + 'b' * 40):
                (flutter / 'bin/internal/engine.version').write_text(revision)
                with self.subTest(revision=revision), self.assertRaisesRegex(e2e.FixtureError, 'full revision'):
                    e2e.stage_local_engine_metadata(flutter, 1700000000)
                self.assertEqual(stamp.read_text(), 'copied stamp')
                self.assertEqual(realm.read_text(), 'copied realm')
                self.assertFalse((flutter / 'bin/cache/engine_stamp.json').exists())

    def test_engine_stamp_dates_pinned_engine_without_git_lookup(self):
        # Without engine_stamp.json flutter_tools runs `git log <engine id>`; its
        # "Standard error: fatal: bad object" line fails MSBuild (MSB8066, code -1).
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flutter = root / 'flutter'
            (flutter / 'bin/internal').mkdir(parents=True)
            (flutter / 'bin/cache').mkdir()
            revision = 'c' * 40
            (flutter / 'bin/internal/engine.version').write_text(revision + '\n')
            inherited = root / 'inherited_stamp.json'
            inherited.write_text('{"git_revision": "stale"}')
            json_stamp = flutter / 'bin/cache/engine_stamp.json'
            try:
                json_stamp.symlink_to(inherited)
            except OSError:
                json_stamp.write_text('stale')
            for bad_time in (0, -1, True, '1700000000', 1.5):
                with self.subTest(commit_time=bad_time), self.assertRaisesRegex(e2e.FixtureError, 'commit time'):
                    e2e.stage_local_engine_metadata(flutter, bad_time)
            self.assertEqual(e2e.stage_local_engine_metadata(flutter, 1700000000), revision)
            self.assertFalse(json_stamp.is_symlink())
            self.assertEqual(inherited.read_text(), '{"git_revision": "stale"}')
            stamp = e2e.strict_json(json_stamp.read_text())
            self.assertEqual(stamp, {'build_time_ms': 1700000000000, 'git_revision': revision,
                                     'git_revision_date': '2023-11-14T22:13:20Z', 'content_hash': ''})
            # Field types match flutter_tools FlutterEngineStampFromFile.tryParseFromFile.
            self.assertIs(type(stamp['build_time_ms']), int)
            self.assertTrue(all(isinstance(stamp[key], str) for key in ('git_revision', 'git_revision_date', 'content_hash')))
            self.assertEqual((flutter / 'bin/cache/engine.stamp').read_text(), revision + '\n')
            self.assertEqual((flutter / 'bin/cache/engine.realm').read_text(), '\n')

    def test_generated_package_replaces_stale_cache_without_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host, flutter = root / 'host', root / 'flutter'
            generated = host / 'gen/dart-pkg/sky_engine'
            generated.mkdir(parents=True)
            (generated / 'pubspec.yaml').write_text('name: sky_engine\n')
            (generated / 'ui.dart').write_text('generated API')
            source_hashes = e2e.tree_hashes(host)
            target = flutter / 'bin/cache/pkg/sky_engine'
            target.mkdir(parents=True)
            (target / 'stale.dart').write_text('obsolete')
            e2e.stage_local_engine_packages(host, flutter)
            self.assertEqual(e2e.tree_hashes(target), e2e.tree_hashes(generated))
            self.assertFalse((target / 'stale.dart').exists())
            self.assertFalse(any(path.is_symlink() for path in target.rglob('*')))
            self.assertFalse(os.path.samefile(target / 'ui.dart', generated / 'ui.dart'))
            (target / 'ui.dart').write_text('private mutation')
            self.assertEqual(e2e.tree_hashes(host), source_hashes)
            # Missing source must fail before discarding even a stale cache.
            (generated / 'pubspec.yaml').unlink()
            with self.assertRaisesRegex(e2e.FixtureError, 'sky_engine package missing'):
                e2e.stage_local_engine_packages(host, flutter)
            self.assertEqual((target / 'ui.dart').read_text(), 'private mutation')

    @unittest.skipIf(os.name == 'nt', 'Symlink privileges are not assumed on Windows')
    def test_stale_alias_is_unlinked_without_mutating_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host, flutter, original = root / 'host', root / 'flutter', root / 'original'
            source = host / 'gen/dart-pkg/sky_engine'
            source.mkdir(parents=True)
            (source / 'pubspec.yaml').write_text('name: sky_engine\n')
            original.mkdir()
            (original / 'preserve').write_text('original')
            target = flutter / 'bin/cache/pkg/sky_engine'
            target.parent.mkdir(parents=True)
            target.symlink_to(original, target_is_directory=True)
            e2e.stage_local_engine_packages(host, flutter)
            self.assertFalse(target.is_symlink())
            self.assertEqual((original / 'preserve').read_text(), 'original')
            shutil.rmtree(target.parent)
            target.parent.symlink_to(original, target_is_directory=True)
            with self.assertRaisesRegex(e2e.FixtureError, 'escapes owned SDK'):
                e2e.stage_local_engine_packages(host, flutter)
            self.assertEqual((original / 'preserve').read_text(), 'original')

    def test_precache_stamp_name_matches_cli_guard(self):
        # The vended copy is stamped so the CLI skips an unrelated `flutter precache`.
        source = (Path(__file__).resolve().parents[3] / 'shorebird/packages/shorebird_cli/lib/src/shorebird_flutter.dart').read_text()
        self.assertIn("precacheStampName = '.shorebird_precache'", source)

    def test_prepare_stages_generated_package_before_vended_copy_and_commands(self):
        for stale_metadata in (False, True):
            with self.subTest(stale_metadata=stale_metadata):
                self.assert_prepared_toolchain(stale_metadata)

    def assert_prepared_toolchain(self, stale_metadata):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout, work, host = root / 'checkout', root / 'work', root / 'engine/out/host'
            for name in ('shorebird/bin/internal', 'flutter/bin/internal',
                         'flutter/bin/cache/dart-sdk', 'flutter/bin/cache/pkg/sky_engine'):
                (checkout / name).mkdir(parents=True)
            engine_id = 'e15e' + 'a' * 36
            (checkout / 'flutter/bin/internal/engine.version').write_text(engine_id + '\n')
            if stale_metadata:
                (checkout / 'flutter/bin/cache/engine.stamp').write_text('0' * 40 + '\n')
                (checkout / 'flutter/bin/cache/engine.realm').write_text('stale-presubmit-realm\n')
                (checkout / 'flutter/bin/cache/engine_stamp.json').write_text('{"git_revision": "stale"}\n')
            (checkout / 'flutter/bin/cache/pkg/sky_engine/stale').write_text('old')
            generated = host / 'gen/dart-pkg/sky_engine'
            generated.mkdir(parents=True)
            (generated / 'pubspec.yaml').write_text('name: sky_engine\n')
            (host / 'dart-sdk').mkdir()
            pub = root / 'pub'
            pub.mkdir()
            fixture = root / 'fixture'
            (fixture / 'lib').mkdir(parents=True)
            (fixture / 'pubspec.yaml').write_text('name: fixture\nversion: 0.0.0\n')
            patch = root / 'patch.exe'
            patch.write_bytes(b'prebuilt patch tool')
            args = mock.Mock(arch='x64', work=work, engine_src=root / 'engine', host_engine='host',
                             target_engine='windows_release', pub_cache=pub, patch_tool=patch)
            with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=1234):
                lifecycle = e2e.Lifecycle(args)
            def snapshot(source, target):
                shutil.copytree(source, target)
                lifecycle.manifest.setdefault('sources', {})[source.name] = {'head': 'flutter-ref'}
            lifecycle.snapshot = snapshot
            def output(command, cwd=None):
                if command[0] in ('whoami', 'icacls'):
                    return b'S-1-5-21-123'
                self.assertEqual(command, ['git', '-C', work / 'flutter', 'log', '-1', '--format=%ct', 'HEAD'])
                return b'1700000000\n'
            lifecycle.output = output
            lifecycle.remap_config = mock.Mock()
            foreign_cache = root / 'foreign-go-modules'
            foreign_cache.mkdir()
            (foreign_cache / 'preserve').write_text('foreign module cache')
            operations = []
            def stop_before_tools(command, **kwargs):
                operations.append(kwargs['name'])
                if kwargs['name'] == 'compile-server':
                    self.assertEqual(command[:3], ['go', 'build', '-modcacherw'])
                    self.assertEqual(kwargs['env']['GOPATH'], str(work / 'go-path'))
                    self.assertEqual(kwargs['env']['GOMODCACHE'], str(work / 'go-path/pkg/mod'))
                    self.assertEqual(kwargs['env']['GOCACHE'], str(work / 'go-cache'))
                    self.assertEqual(kwargs['env']['GOFLAGS'], '-trimpath')
                    return
                self.assertIn(kwargs['name'], ('compile-cli', 'generate-windows'))
                for sdk in (work / 'flutter', work / 'cli/bin/cache/flutter/flutter-ref'):
                    self.assertEqual((sdk / 'bin/cache/engine.stamp').read_text(), engine_id + '\n')
                    self.assertEqual((sdk / 'bin/cache/engine.realm').read_text(), '\n')
                    self.assertEqual((sdk / 'bin/internal/engine.version').read_text().strip(), engine_id)
                    self.assertEqual(e2e.strict_json((sdk / 'bin/cache/engine_stamp.json').read_text()), {
                        'build_time_ms': 1700000000000, 'git_revision': engine_id,
                        'git_revision_date': '2023-11-14T22:13:20Z', 'content_hash': ''})
                    package = sdk / 'bin/cache/pkg/sky_engine'
                    self.assertEqual(e2e.tree_hashes(package), e2e.tree_hashes(generated))
                    self.assertFalse((package / 'stale').exists())
                    self.assertFalse(os.path.samefile(package / 'pubspec.yaml', generated / 'pubspec.yaml'))
                self.assertTrue((work / 'cli/bin/cache/flutter/flutter-ref/.shorebird_precache').is_file())
                for key in ('FLUTTER_STORAGE_BASE_URL', 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL'):
                    self.assertEqual(kwargs['env'][key], 'https://storage.googleapis.com')
                if kwargs['name'] == 'generate-windows':
                    self.assertEqual(command, e2e.create_windows_command(args, work))
                    raise RuntimeError('stop before external tools')
            lifecycle.run_command = stop_before_tools
            with mock.patch.object(e2e, 'ROOT', checkout), \
                    mock.patch.object(e2e, 'FIXTURE', fixture), \
                    mock.patch.dict(os.environ, {'FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid',
                        'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid',
                        'GOMODCACHE': str(foreign_cache), 'GOFLAGS': '-trimpath'}), \
                    self.assertRaisesRegex(RuntimeError, 'stop before external tools'):
                lifecycle.prepare()
            self.assertEqual(operations, ['compile-cli', 'compile-server', 'generate-windows'])
            self.assertEqual(e2e.tree_hashes(foreign_cache), {
                'preserve': e2e.sha256(b'foreign module cache')})
            self.assertEqual((work / 'cli/bin/internal/engine.version').read_text(), engine_id + '\n')
            if stale_metadata:
                self.assertEqual((checkout / 'flutter/bin/cache/engine.stamp').read_text(), '0' * 40 + '\n')
                self.assertEqual((checkout / 'flutter/bin/cache/engine.realm').read_text(), 'stale-presubmit-realm\n')
                self.assertEqual((checkout / 'flutter/bin/cache/engine_stamp.json').read_text(), '{"git_revision": "stale"}\n')
            else:
                self.assertFalse((checkout / 'flutter/bin/cache/engine.stamp').exists())
                self.assertFalse((checkout / 'flutter/bin/cache/engine.realm').exists())
            self.assertEqual((checkout / 'flutter/bin/cache/pkg/sky_engine/stale').read_text(), 'old')
            self.assertFalse((host / 'gen/dart-pkg/flutter_gpu').exists())


class CommandDiagnosticsTest(unittest.TestCase):
    def test_only_fixed_classifications_and_full_log_digest_survive(self):
        variants = {
            b'Could not determine engine revision.\n': 'missing_engine_revision',
            b'Could not find package sky_engine in the Flutter SDK. Version solving failed.': 'missing_sky_engine',
            b'Material Fonts: http://localhost:8080/download.flutter.io/material_fonts.zip': 'material_fonts_local_artifact_server',
            b'the Flutter SDK is not available': 'flutter_sdk_unavailable',
            b'Version solving failed.': 'pub_solver',
            b'arbitrary private-token text': 'unknown',
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'private.log'
            for content, classification in variants.items():
                path.write_bytes(content)
                result = e2e.command_failure('generate-windows', 1, False, path)
                self.assertEqual(result['classification'], classification)
                self.assertEqual(result['log_sha256'], e2e.sha256(content))
                self.assertEqual(result['log_bytes'], len(content))
                self.assertFalse(result['classification_scan_truncated'])
                self.assertNotIn(content.decode(), json.dumps(result))
            content = (b'A' * e2e.FAILURE_SCAN_BYTES +
                       b'could not find package sky_engine in the Flutter SDK' +
                       b'B' * e2e.FAILURE_SCAN_BYTES + b'Version solving failed')
            path.write_bytes(content)
            result = e2e.command_failure('generate-windows', 1, False, path)
            self.assertEqual(result['classification'], 'pub_solver')
            self.assertTrue(result['classification_scan_truncated'])
            self.assertEqual(result['log_sha256'], e2e.sha256(content))

    def test_probe_classification_emits_only_fixed_keys(self):
        secret = b'C:\\Users\\private token=SECRET'
        cases = (
            ('status', 128, b'', b'fatal: detected dubious ownership in repository at ' + secret, 'git_dubious_ownership'),
            ('head', 128, b'', b'fatal: not a git repository ' + secret, 'git_not_repository'),
            ('log', 128, b'', b'fatal: bad revision HEAD', 'git_no_commit'),
            ('latest_tag', 0, b'', b'', 'git_no_tags'),
            ('latest_tag', 0, b'3.41.0\n', b'', 'git_ok'),
            ('tags_at_head', 0, b'', b'', 'git_ok'),
            ('origin_url', 2, b'', b'error: No such remote ' + secret, 'git_missing_remote'),
            ('upstream', 128, b'', b'fatal: no upstream configured', 'git_no_upstream'),
            ('status', 1, b'', b'Filename too long', 'git_path_too_long'),
            ('status', 0, b' M x', b'', 'git_ok'),
            ('status', None, b'', b'', 'probe_timed_out'),
            ('flutter_version', 0, b'{"x":1}', b'', 'flutter_version_ok'),
            ('flutter_version', 0, b'{"x":1}', b'Failed to find the latest git commit date: VersionCheckError: '
             b'Command exited with code 128\nStandard error: fatal: bad object ' + secret, 'flutter_version_commit_date_fallback'),
            ('flutter_version', 1, b'', b'VersionCheckError: Command exited ' + secret, 'flutter_version_check_error'),
            ('flutter_version', 1, b'', b'boom ' + secret, 'flutter_version_failed'),
            ('flutter_version', 1, b'', b'Failed to download https://x/engine_stamp.json. Exception: 404', 'flutter_engine_stamp_download_failed'),
        )
        allowed = set()
        for name, code, out, err, expected in cases:
            key = e2e.classify_probe(name, code, out, err)
            self.assertEqual(key, expected, name)
            self.assertNotIn(b'SECRET', key.encode())
            allowed.add(key)
        self.assertTrue(all(re.fullmatch(r'[a-z_]+', key) for key in allowed))

    def test_flutter_and_git_failure_texts_map_to_known_message_keys(self):
        text = (b'VersionCheckError: Command exited with code 128: git log\nfatal: detected dubious ownership in repository\n'
                b'The tool could not determine the remote upstream\nfatal: not a git repository\n'
                b'The Flutter SDK is tracking a non-standard remote "x"\nUnable to install Flutter abc.\nFailed to build windows app.\n')
        result = e2e.error_signature(text, b'', 'unknown')
        self.assertEqual(result['exception_types'], ['VersionCheckError'])
        self.assertEqual(result['known_messages'], [
            'flutter_install_failed', 'windows_build_failed', 'flutter_git_command_failed', 'flutter_remote_upstream_unknown',
            'flutter_nonstandard_remote', 'git_dubious_ownership', 'git_not_repository'])

    def test_benign_commit_date_block_and_build_codes(self):
        benign = (b'Failed to find the latest git commit date: VersionCheckError: Command exited with code 128: git log abc\n'
                  b'Standard out: \nStandard error: fatal: bad object abc\n\nReturning 1970-01-01 08:00:00.000 instead.\n')
        build = (b'C:\\private\\x.vcxproj : error MSB8066: custom build exited with code 1\n'
                 b'LINK : fatal error LNK1181: cannot open input file C:\\private\\flutter_windows.dll.lib\n'
                 b'CMake Error at secret/path\nFailed to build windows app.\n')
        result = e2e.error_signature(benign + build, b'', 'unknown')
        self.assertEqual(result['exception_types'], [])
        self.assertEqual(result['build_codes'], ['MSB8066', 'LNK1181'])
        self.assertIn('windows_build_failed', result['known_messages'])
        self.assertNotIn('flutter_git_command_failed', result['known_messages'])
        self.assertTrue({'cmake_error', 'msbuild_error', 'link_input_missing'} <= set(result['known_messages']))
        self.assertNotIn('private', json.dumps(result))

    def test_release_excerpt_keeps_build_errors_and_redacts_private_text(self):
        raw = (b'Building Windows application...\n'
               b'Target kernel_snapshot_program failed: Exception: C:\\runner\\work\\app\\lib\\main.dart missing\n'
               b'Error: token=abc123SECRET at https://example.invalid/x?sig=1\n'
               b'MSB8066: Custom build for C:\\w\\flutter_assemble.rule exited with code 1.\n'
               b'plain progress line\n')
        lines = e2e.sanitized_excerpt(raw)
        self.assertEqual(lines[0], 'Target kernel_snapshot_program failed: Exception: <path> missing')
        self.assertEqual(len(lines), 3)
        encoded = json.dumps(lines)
        for leak in ('SECRET', 'example.invalid', 'runner', 'progress'):
            self.assertNotIn(leak, encoded)
        many = b''.join(b'error %d\n' % i for i in range(100))
        self.assertEqual(len(e2e.sanitized_excerpt(many)), 40)

    def test_error_signature_extracts_capped_identifiers_without_leaking_text(self):
        secret = 'Bearer sk-live-SECRET https://private.invalid/?sig=abc C:\\Users\\private\\file.txt'
        lines = [secret, 'Unhandled PathNotFoundException: ' + secret,
                 'No release directory found at C:\\Users\\private\\build', 'Failed to build release artifacts: x']
        lines += ['Error%dError thing' % i for i in range(3)]
        lines += ['Type%cException' % c for c in 'ABCDEFGH']
        lines += ['Because pkg%c depends on dep%c ^1.0.0 and fake-name_X depends on Bad.Name' % (c, c) for c in 'abcdefghijklmn']
        lines += ['Because every version of flutter_lints depends on lints ^6.0.0, version solving failed.']
        content = '\n'.join(lines).encode()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'private.log'
            path.write_bytes(content)
            result = e2e.command_failure('release', 70, False, path)
            self.assertEqual(result['classification'], 'pub_solver')
            self.assertEqual(len(result['exception_types']), 5)
            self.assertEqual(result['exception_types'][0], 'PathNotFoundException')
            self.assertEqual(len(set(result['exception_types'])), 5)
            self.assertEqual(len(result['pub_conflict_packages']), 10)
            self.assertTrue(all(re.fullmatch(r'[a-z][a-z0-9_]{0,63}', n) for n in result['pub_conflict_packages']))
            self.assertEqual(result['known_messages'], ['release_directory_missing', 'release_build_failed'])
            encoded = json.dumps(result)
            for leak in ('SECRET', 'private', 'sk-live', 'https', 'sig=', 'Bearer', 'file.txt', '1.0.0', 'Bad'):
                self.assertNotIn(leak, encoded)
            path.write_bytes(b'Because every version of flutter_lints depends on lints ^6.0.0, version solving failed.')
            result = e2e.command_failure('generate-windows', 1, False, path)
            self.assertEqual(result['pub_conflict_packages'], ['flutter_lints', 'lints'])
            path.write_bytes(b'plain output')
            result = e2e.command_failure('release', 70, False, path)
            self.assertEqual((result['exception_types'], result['known_messages'], result['build_codes']), ([], [], []))
            self.assertNotIn('pub_conflict_packages', result)

    def test_failed_and_timed_out_commands_retain_no_secrets_after_cleanup(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temp:
                work = Path(temp) / 'private'
                work.mkdir()
                args = mock.Mock(arch='x64', work=work, build_timeout=1)
                with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=1234):
                    lifecycle = e2e.Lifecycle(args)
                lifecycle.work_owned = True
                payload = b'Bearer private-token https://private.invalid/?sig=secret password=private-password\n'
                process = mock.Mock()
                process.wait.side_effect = subprocess.TimeoutExpired('private-command', 1) if timeout else None
                process.wait.return_value = 17
                def spawn(*_args, **kwargs):
                    kwargs['stdout'].write(payload)
                    return process
                lifecycle.job.spawn.side_effect = spawn
                with mock.patch.object(e2e, 'stop_process') as stop, self.assertRaises(e2e.FixtureError):
                    lifecycle.run_command(['private-command', 'private-argument'], name='generate-windows')
                stop.assert_called_once_with(process)
                evidence = lifecycle.manifest['command_failure']
                self.assertEqual(evidence['exit_code'], None if timeout else 17)
                self.assertEqual(evidence['timed_out'], timeout)
                self.assertEqual(evidence['log_sha256'], e2e.sha256(payload))
                lifecycle.cleanup()
                self.assertFalse(work.exists())
                encoded = json.dumps(lifecycle.manifest)
                for secret in ('private-token', 'private.invalid', 'private-password', 'private-command', 'private-argument'):
                    self.assertNotIn(secret, encoded)
                self.assertEqual(set(evidence), {'operation', 'exit_code', 'timed_out', 'log_bytes',
                    'log_sha256', 'classification', 'classification_scan_truncated',
                    'exception_types', 'build_codes', 'known_messages'})


class ExecutionBoundaryTest(unittest.TestCase):
    def test_create_selects_same_local_artifacts_as_release_before_command(self):
        args = mock.Mock(arch='x64', engine_src=Path('/isolated/engine/src'), target_engine='windows_release',
                         host_engine='host_release')
        work = Path('/private/session')
        command = e2e.create_windows_command(args, work)
        flags = e2e.local_engine_flags(args)
        self.assertEqual(flags, ['--local-engine-src-path=' + str(args.engine_src),
                                 '--local-engine=windows_release', '--local-engine-host=host_release'])
        self.assertEqual(command[0], work / 'flutter/bin/cache/dart-sdk/bin/dart.exe')
        self.assertEqual(command[2], work / 'flutter/packages/flutter_tools/bin/flutter_tools.dart')
        self.assertEqual(command[3:6], flags)
        for flag in flags:
            self.assertLess(command.index(flag), command.index('create'))
        self.assertEqual(command[command.index('create') + 1:], [
            '--platforms=windows', '--project-name=license_flavor_patch_test', '.'])

    @staticmethod
    def windows_os():
        # Replace only this module's OS reference, leaving pathlib on the real host.
        class WindowsOS:
            name = 'nt'
            def __getattr__(self, name):
                return getattr(os, name)
        return WindowsOS()

    def test_successful_execute_keeps_preflight_machine_evidence_in_final_report(self):
        # Exercise real main/constructor/cleanup/report handoff; no native runtime
        # starts. The mocked execute supplies a successful lifecycle's receipts.
        stages = (
            'isolated-toolchain', 'released-native-runtime-policy', 'actual-cli-release-free',
            'rebuilt-native-bytes-identical', 'actual-cli-patch-pro', 'ciphertext-installed',
            'api-and-provisioning-stopped', 'offline-cold-U', 'offline-missing-key-BASE',
            'offline-missing-recovery-U', 'offline-wrong-key-BASE', 'offline-wrong-recovery-U',
            'final-cache-before-rollback', 'online-U-receive-signed-rollback',
            'offline-cold-rollback-BASE', 'final-cache-after-rollback', 'released-bundle-unchanged')
        for arch, runtime in [('x64', 'x86_64'), ('arm64', 'aarch64')]:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                work, results = Path(temp) / 'work', Path(temp) / 'results'
                binaries = {str(n): {'architecture': arch, 'native_target': True,
                                      'format': 'pe'} for n in range(6)}

                def execute(lifecycle):
                    self.assertEqual(lifecycle.manifest.get('target_arch'), arch)
                    self.assertEqual(lifecycle.manifest.get('runtime_arch'), runtime)
                    self.assertEqual(lifecycle.manifest.get('binary_architectures'), binaries)
                    lifecycle.manifest['stages'] = [{'name': name} for name in stages]
                    lifecycle.manifest['result'] = 'passed'

                with mock.patch.object(e2e.architecture, 'require_native_host') as host, \
                        mock.patch.object(e2e, 'preflight', return_value=binaries) as preflight, \
                        mock.patch.object(e2e, 'OwnedWindowsJob') as owner, \
                        mock.patch.object(e2e, 'free_port', return_value=12345), \
                        mock.patch.object(e2e.Lifecycle, 'execute', autospec=True,
                                          side_effect=execute) as executed, \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = e2e.main(['run', '--execute', '--arch', arch,
                                    '--work', str(work), '--results', str(results)])
                self.assertEqual(code, 0)
                host.assert_called_once_with('windows', arch)
                preflight.assert_called_once()
                executed.assert_called_once()
                owner.return_value.close.assert_called_once_with()
                report = json.loads((results / 'results.json').read_text())
                self.assertEqual(report['target_arch'], arch)
                self.assertEqual(report['runtime_arch'], runtime)
                self.assertEqual(report['binary_architectures'], binaries)
                self.assertEqual([stage['name'] for stage in report['stages']], list(stages))
                e2e.architecture.validate_lifecycle_report(report, 'windows', arch)
                self.assertFalse(work.exists())

    def test_exclusive_work_creation_conflict_preserves_other_session_through_finally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, results = root / 'work', root / 'results'
            def competing_session(_args):
                work.mkdir()
                (work / 'other-session.txt').write_text('preserve me')
            with mock.patch.object(e2e, 'os', self.windows_os()), \
                    mock.patch.object(e2e.architecture, 'os', self.windows_os()), \
                    mock.patch.object(e2e.platform, 'machine', return_value='AMD64'), \
                    mock.patch.object(e2e.architecture, 'require_binary', return_value={}), \
                    mock.patch.object(e2e, 'preflight', side_effect=competing_session), \
                    mock.patch.object(e2e, 'free_port', return_value=12345), \
                    mock.patch.object(e2e, 'OwnedWindowsJob') as job, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = e2e.main(['run', '--execute', '--work', str(work), '--results', str(results)])
            self.assertEqual(code, 1)
            self.assertEqual((work / 'other-session.txt').read_text(), 'preserve me')
            job.return_value.close.assert_called_once_with()
            report = json.loads((results / 'results.json').read_text())
            self.assertEqual(report['failure']['type'], 'FileExistsError')
            self.assertEqual(report['result'], 'failed')

    def test_preflight_failure_writes_sanitized_manifest_without_work_or_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, results = root / 'work', root / 'results'
            with mock.patch.object(e2e, 'os', self.windows_os()), \
                    mock.patch.object(e2e.architecture, 'os', self.windows_os()), \
                    mock.patch.object(e2e.platform, 'machine', return_value='AMD64'), \
                    mock.patch.object(e2e.architecture, 'require_binary', return_value={}), \
                    mock.patch.object(e2e, 'preflight', side_effect=e2e.FixtureError('Prebuilt SDK missing')), \
                    mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('runtime started')), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = e2e.main(['run', '--execute', '--work', str(work), '--results', str(results)])
            self.assertEqual(code, 1)
            self.assertFalse(work.exists())
            report = json.loads((results / 'results.json').read_text())
            self.assertEqual(report['failure'], {'type': 'FixtureError', 'after_stage': 'preflight',
                                                  'reason': 'Prebuilt SDK missing'})
            self.assertIn('work untouched', report['cleanup'])

    def test_cleanup_preserves_nonowned_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, cache = root / 'work', root / 'other-cache'
            cache.mkdir()
            (cache / 'state.json').write_text('other app')
            args = mock.Mock(arch='x64', work=work)
            with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=12345):
                lifecycle = e2e.Lifecycle(args)
            lifecycle.cache = cache
            lifecycle.cleanup()
            self.assertEqual((cache / 'state.json').read_text(), 'other app')

    def test_owned_tree_removal_retries_only_transient_windows_locks(self):
        def locked(code, filename):
            error = PermissionError(13, 'locked C:\\Users\\private token=SECRET', filename)
            error.winerror = code
            return error
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'owned'
            (root / 'flutter/bin/cache/secret-name dir').mkdir(parents=True)
            target = root / 'flutter/bin/cache/secret-name dir/a.dll'
            target.write_text('x')
            real = shutil.rmtree
            calls, sleeps = [], []
            def flaky(path, onerror=None):
                calls.append(path)
                if len(calls) < 3:
                    raise locked(32, str(target))
                real(path)
            with mock.patch.object(e2e.shutil, 'rmtree', side_effect=flaky):
                e2e.remove_owned_tree(root, sleep=sleeps.append)
            self.assertFalse(root.exists())
            self.assertEqual(sleeps, [0.25, 0.5])
            root.mkdir()
            for code, clock in ((2, None), (32, iter([0, 0, 61]))):
                ticks = (lambda values: (lambda: next(values)))(clock) if clock else (lambda: 0)
                with self.subTest(winerror=code), \
                        mock.patch.object(e2e.shutil, 'rmtree', side_effect=locked(code, str(target))), \
                        self.assertRaises(e2e.OwnedTreeRemovalError) as raised:
                    e2e.remove_owned_tree(root, sleep=lambda _: None, clock=ticks)
                item = e2e.cleanup_failure('work-tree', raised.exception)
                self.assertEqual(item, {'action': 'work-tree', 'type': 'PermissionError', 'winerror': code, 'errno': 13,
                                        'path': 'flutter/bin/cache'})
                self.assertNotIn('SECRET', e2e.describe_cleanup_failure(item))
            self.assertEqual(e2e.sanitized_relative(root, '/elsewhere/private'), '<outside-owned-tree>')

    def test_server_rows_close_the_sqlite_handle(self):
        # An unclosed read-only connection kept server.sqlite open and failed cleanup (WinError 32).
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            with contextlib.closing(e2e.sqlite3.connect(work / 'server.sqlite')) as database:
                database.execute('CREATE TABLE releases (id INTEGER, version TEXT)')
                database.execute("INSERT INTO releases VALUES (1, '1.0.0+1')")
                database.commit()
            lifecycle = object.__new__(e2e.Lifecycle)
            lifecycle.work = work
            opened = []
            real = e2e.sqlite3.connect
            def connect(*args, **kwargs):
                connection = mock.Mock(wraps=real(*args, **kwargs))
                opened.append(connection)
                return connection
            with mock.patch.object(e2e.sqlite3, 'connect', side_effect=connect):
                self.assertEqual(lifecycle.rows('SELECT id, version FROM releases'), [(1, '1.0.0+1')])
            self.assertEqual(len(opened), 1)
            opened[0].close.assert_called_once_with()

    def test_cleanup_failure_names_action_code_and_owned_path(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / 'work'
            (work / 'app/build').mkdir(parents=True)
            args = mock.Mock(arch='arm64', work=work)
            with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=12345):
                lifecycle = e2e.Lifecycle(args)
            lifecycle.work_owned = True
            error = PermissionError(13, 'C:\\Users\\private', str(work / 'app/build/x.pdb'))
            error.winerror = 32
            removal = e2e.OwnedTreeRemovalError(error, work, str(work / 'app/build/x.pdb'))
            lifecycle.job.close.side_effect = e2e.FixtureError('Owned Windows processes still running after job termination: 2')
            with mock.patch.object(e2e, 'remove_owned_tree', side_effect=removal), \
                    self.assertRaises(e2e.FixtureError) as raised:
                lifecycle.cleanup()
            message = str(raised.exception)
            self.assertIn('action=process-job type=FixtureError reason=Owned Windows processes still running after job termination: 2', message)
            self.assertIn('action=work-tree type=PermissionError winerror=32 errno=13 path=app/build/x.pdb', message)
            self.assertNotIn('private', message)
            self.assertEqual([item['action'] for item in lifecycle.manifest['cleanup_failures']], ['process-job', 'work-tree'])

    def test_job_close_terminates_and_waits_for_owned_processes(self):
        for counts, expected in (([2, 1, 0], None), ([1] * 1000, 'still running after job termination: 1')):
            with self.subTest(counts=counts[:3]):
                job = object.__new__(e2e.OwnedWindowsJob)
                job.handle = 7
                job.kernel = mock.Mock()
                values = iter(counts)
                job.active_processes = lambda: next(values)
                clock = iter(range(0, 10000, 5))
                with mock.patch.object(e2e.time, 'monotonic', side_effect=lambda: next(clock)), \
                        mock.patch.object(e2e.time, 'sleep'), mock.patch.dict('sys.modules', {'ctypes.wintypes': mock.Mock()}):
                    if expected:
                        with self.assertRaisesRegex(e2e.FixtureError, expected):
                            job.close(timeout=30)
                    else:
                        job.close(timeout=30)
                job.kernel.TerminateJobObject.assert_called_once_with(7, 1)
                job.kernel.CloseHandle.assert_called_once_with(7)
                self.assertIsNone(job.handle)

    def test_owned_child_is_suspended_and_assigned_before_resume(self):
        job = object.__new__(e2e.OwnedWindowsJob)
        calls = []
        process = mock.Mock(pid=123)
        job.attach = lambda child: calls.append(('attach', child.pid))
        job.resume_primary_thread = lambda pid: calls.append(('resume', pid))
        with mock.patch.object(e2e.subprocess, 'Popen', return_value=process) as create:
            self.assertIs(job.spawn(['owned.exe'], cwd='private'), process)
        create.assert_called_once_with(['owned.exe'], creationflags=0x00000004, cwd='private')
        self.assertEqual(calls, [('attach', 123), ('resume', 123)])

    def test_failed_job_assignment_never_resumes_and_terminates_only_owned_child(self):
        job = object.__new__(e2e.OwnedWindowsJob)
        process = mock.Mock(pid=123)
        job.attach = mock.Mock(side_effect=e2e.FixtureError('assignment failure'))
        job.resume_primary_thread = mock.Mock()
        with mock.patch.object(e2e.subprocess, 'Popen', return_value=process), \
                mock.patch.object(e2e, 'stop_process') as stop, self.assertRaises(e2e.FixtureError):
            job.spawn(['owned.exe'])
        job.resume_primary_thread.assert_not_called()
        stop.assert_called_once_with(process)

    def test_plan_has_no_mutation_service_or_build(self):
        with mock.patch.object(e2e, 'preflight', side_effect=AssertionError('preflight in plan')), \
                mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('lifecycle in plan')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(e2e.main(['plan']), 0)
        value = json.loads(output.getvalue())
        self.assertIs(value['read_only'], True)
        self.assertIn('transient write audit unimplemented', value['audit'])

    def test_mutating_run_needs_explicit_execute(self):
        with self.assertRaisesRegex(SystemExit, 'requires --execute'):
            e2e.main(['run'])

    @unittest.skipIf(os.name == 'nt', 'Non-Windows boundary check')
    def test_mac_fullrun_rejected_before_any_service_or_filesystem_change(self):
        with mock.patch.object(e2e, 'preflight', side_effect=AssertionError('unexpected preflight')), \
                mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('unexpected lifecycle')), \
                self.assertRaisesRegex(SystemExit, 'requires Windows x64'):
            e2e.main(['run', '--execute'])

    def test_provider_injected_once_before_engine_with_fixed_release_options(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            runner = app / 'windows/runner'
            runner.mkdir(parents=True)
            (runner / 'main.cpp').write_text('#include "flutter_window.h"\n  flutter::DartProject project(L"data");\n')
            (runner / 'CMakeLists.txt').write_text('add_executable(${BINARY_NAME}\n  "main.cpp"\n)\n')
            e2e.inject_provider(app)
            source = (runner / 'main.cpp').read_text()
            self.assertLess(source.index('RegisterWindowsNativeCacheTestProvider()'), source.index('flutter::DartProject'))
            cmake = (runner / 'CMakeLists.txt').read_text()
            for value in ('runner_patch_key_provider.cpp', 'winhttp crypt32 bcrypt', '/Brepro', '/INCREMENTAL:NO', '/PDBALTPATH:%_PDB%'):
                self.assertIn(value, cmake)
            with self.assertRaises(RuntimeError):
                e2e.inject_provider(app)


if __name__ == '__main__':
    unittest.main()


class ArchitectureRoutingTest(unittest.TestCase):
    def test_native_architecture_paths_and_cache_are_explicit(self):
        for arch, runtime in [('x64', 'x86_64'), ('arm64', 'aarch64')]:
            args = e2e.parser().parse_args(['plan', '--arch', arch])
            self.assertEqual(e2e.runtime_arch(args), runtime)
            path = e2e.bundle_directory(args, Path('/owned'), 'windows')
            self.assertIn(('windows', arch), list(zip(path.parts, path.parts[1:])))
            cached = e2e.patch_cache_directory(args, Path('/owned'), 'windows')
            self.assertEqual(cached.name, 'windows-arm64' if arch == 'arm64' else 'patch')

    def test_arm64_aad_is_canonical_and_cross_arch_key_context_is_rejected(self):
        arm = dict(context(), arch='aarch64')
        authority = e2e.KeyAuthority(b'fixture-secret', 'private-token')
        authority.allow(arm)
        aad = e2e.canonical_aad(arm)
        self.assertIn('arch=aarch64\n', aad)
        key_id, key = e2e.delivery_material(b'fixture-secret', aad)
        self.assertEqual(authority.provision('Bearer private-token',
            json.dumps({'key_id': key_id, 'aad': aad}).encode()), key.hex().encode())
        x64 = e2e.canonical_aad(context())
        x64_key_id, _ = e2e.delivery_material(b'fixture-secret', x64)
        with self.assertRaises(ValueError):
            authority.provision('Bearer private-token',
                json.dumps({'key_id': x64_key_id, 'aad': x64}).encode())
        self.assertNotEqual(key_id, x64_key_id)

    def test_arm64_plan_is_read_only_and_records_selected_architecture(self):
        output = io.StringIO()
        with mock.patch.object(e2e, 'preflight', side_effect=AssertionError('plan mutation')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(e2e.main(['plan', '--arch', 'arm64']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['target_arch'], 'arm64')
        self.assertEqual(report['runtime_arch'], 'aarch64')

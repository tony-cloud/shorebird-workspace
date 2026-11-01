"""Pure protocol/oracle tests; no services, builds, engine, or device required."""
import contextlib
import copy
from email.message import Message
import importlib.util
import io
import json
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

    def test_prepare_stages_generated_package_before_vended_copy_and_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout, work, host = root / 'checkout', root / 'work', root / 'engine/out/host'
            for name in ('shorebird/bin/internal', 'flutter/bin/internal',
                         'flutter/bin/cache/dart-sdk', 'flutter/bin/cache/pkg/sky_engine'):
                (checkout / name).mkdir(parents=True)
            (checkout / 'flutter/bin/internal/engine.version').write_text('engine-id')
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
            args = mock.Mock(work=work, engine_src=root / 'engine', host_engine='host',
                             target_engine='windows_release', pub_cache=pub, patch_tool=patch)
            with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=1234):
                lifecycle = e2e.Lifecycle(args)
            def snapshot(source, target):
                shutil.copytree(source, target)
                lifecycle.manifest.setdefault('sources', {})[source.name] = {'head': 'flutter-ref'}
            lifecycle.snapshot = snapshot
            lifecycle.output = mock.Mock(return_value=b'S-1-5-21-123')
            lifecycle.remap_config = mock.Mock()
            operations = []
            def stop_before_tools(command, **kwargs):
                operations.append(kwargs['name'])
                if kwargs['name'] == 'compile-server':
                    return
                self.assertIn(kwargs['name'], ('compile-cli', 'generate-windows'))
                for sdk in (work / 'flutter', work / 'cli/bin/cache/flutter/flutter-ref'):
                    package = sdk / 'bin/cache/pkg/sky_engine'
                    self.assertEqual(e2e.tree_hashes(package), e2e.tree_hashes(generated))
                    self.assertFalse((package / 'stale').exists())
                    self.assertFalse(os.path.samefile(package / 'pubspec.yaml', generated / 'pubspec.yaml'))
                for key in ('FLUTTER_STORAGE_BASE_URL', 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL'):
                    self.assertEqual(kwargs['env'][key], 'https://storage.googleapis.com')
                if kwargs['name'] == 'generate-windows':
                    self.assertEqual(command, e2e.create_windows_command(args, work))
                    raise RuntimeError('stop before external tools')
            lifecycle.run_command = stop_before_tools
            with mock.patch.object(e2e, 'ROOT', checkout), \
                    mock.patch.object(e2e, 'FIXTURE', fixture), \
                    mock.patch.dict(os.environ, {'FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid',
                        'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid'}), \
                    self.assertRaisesRegex(RuntimeError, 'stop before external tools'):
                lifecycle.prepare()
            self.assertEqual(operations, ['compile-cli', 'compile-server', 'generate-windows'])
            self.assertEqual((checkout / 'flutter/bin/cache/pkg/sky_engine/stale').read_text(), 'old')
            self.assertFalse((host / 'gen/dart-pkg/flutter_gpu').exists())


class CommandDiagnosticsTest(unittest.TestCase):
    def test_only_fixed_classifications_and_full_log_digest_survive(self):
        variants = {
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

    def test_failed_and_timed_out_commands_retain_no_secrets_after_cleanup(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temp:
                work = Path(temp) / 'private'
                work.mkdir()
                args = mock.Mock(work=work, build_timeout=1)
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
                    'log_sha256', 'classification', 'classification_scan_truncated'})


class ExecutionBoundaryTest(unittest.TestCase):
    def test_create_selects_same_local_artifacts_as_release_before_command(self):
        args = mock.Mock(engine_src=Path('/isolated/engine/src'), target_engine='windows_release',
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

    def test_exclusive_work_creation_conflict_preserves_other_session_through_finally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, results = root / 'work', root / 'results'
            def competing_session(_args):
                work.mkdir()
                (work / 'other-session.txt').write_text('preserve me')
            with mock.patch.object(e2e, 'os', self.windows_os()), \
                    mock.patch.object(e2e.platform, 'machine', return_value='AMD64'), \
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
                    mock.patch.object(e2e.platform, 'machine', return_value='AMD64'), \
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
            args = mock.Mock(work=work)
            with mock.patch.object(e2e, 'OwnedWindowsJob'), mock.patch.object(e2e, 'free_port', return_value=12345):
                lifecycle = e2e.Lifecycle(args)
            lifecycle.cache = cache
            lifecycle.cleanup()
            self.assertEqual((cache / 'state.json').read_text(), 'other app')

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

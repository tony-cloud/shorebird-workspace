#!/usr/bin/env python3
"""Synthetic transport controls; never compilation, signing or native proof."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request
import uuid
import warnings
import zipfile
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location('macos_x64_proof_transport',
    Path(__file__).with_name('macos_x64_proof_transport.py'))
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)
WORKSPACE = 'a' * 40
SDK = 'b' * 40
CERT = 'C' * 40
MACHO = b'\xcf\xfa\xed\xfe' + struct.pack('<II', 0x1000007, 3) + b'synthetic'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


class PreparedTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = Path('/private/tmp/shorebird-macos-x64-proof-' + uuid.uuid4().hex)
        self.prefix = str(self.root).lstrip('/') + '/'
        self.entries = {}
        build_bytes = {name: MACHO + name.encode() for name in transport.BUILD_FILES}
        build_bytes.update({name: MACHO for name in transport.SDK_REQUIRED})
        self.build = {'format_version': transport.BUILD_FORMAT_VERSION, 'kind': 'unsigned-native-intel-build-inputs',
                      'status': 'build_inputs_only', 'workspace_commit': WORKSPACE,
                      'sdk_source_commit': SDK, 'target': {'os': 'macos', 'arch': 'x64'},
                      'files': {name: sha(value) for name, value in build_bytes.items()}}
        self.entries['build-inputs-manifest.json'] = encoded(self.build)
        for suite in transport.SUITES:
            roles = {'dart': 'dart', 'platform': 'vm_platform_product.dill',
                     'gen_snapshot': 'gen_snapshot_product', 'harness': transport.HARNESSES[suite]}
            inputs = {'original-inputs/' + role: build_bytes[name] for role, name in roles.items()}
            runtime = ['strict-signing/' + transport.HARNESSES[suite], *transport.RUNTIME[suite]]
            inputs.update({name: MACHO + name.encode() for name in runtime})
            manifest = {
                'suite': suite, 'status': 'prepared_runtime_pending',
                'target': {'os': 'macos', 'arch': 'x64', 'machine': 'x86_64'},
                'canonical_root': str(self.root / suite), 'proof_root': str(self.root),
                'sdk_source_commit': SDK, 'inputs': {name: sha(value) for name, value in inputs.items()},
                'original_artifacts': {role: {'path': 'original-inputs/' + role,
                    'sha256': self.build['files'][name], 'usage': 'provenance-only-never-executed'}
                    for role, name in roles.items()},
                'signed_runtime_inputs': [{'path': name, 'sha256': sha(inputs[name]),
                    'certificate_sha1': CERT, 'machine': 'x86_64', 'hardened_runtime': True,
                    'entitlements': {}} for name in runtime],
                'test_cases': [{'id': 'case-' + str(index),
                    'argv': [str(self.root / suite / runtime[0]), 'fixture']}
                    for index in range(transport.CASE_COUNTS[suite])],
            }
            for name, value in inputs.items():
                self.entries[suite + '/' + name] = value
            self.entries[suite + '/prepared-manifest.json'] = encoded(manifest)

    def archive(self, extra=(), refresh=True):
        manifest = {'format_version': 1, 'kind': 'native-intel-prepared-proof',
            'status': 'prepared_runtime_pending', 'proof_root': str(self.root),
            'workspace_commit': WORKSPACE, 'sdk_source_commit': SDK, 'certificate_sha1': CERT,
            'build_manifest_sha256': sha(self.entries['build-inputs-manifest.json']),
            'suites': {suite: sha(self.entries[suite + '/prepared-manifest.json']) for suite in transport.SUITES},
            'files': {name: sha(value) for name, value in self.entries.items()}}
        path = self.directory / (uuid.uuid4().hex + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in self.entries.items():
                entry = zipfile.ZipInfo(self.prefix + name)
                entry.external_attr = (stat.S_IFREG | 0o700) << 16
                archive.writestr(entry, value)
            archive.writestr(self.prefix + 'transport-manifest.json', encoded(manifest))
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                for entry, data in extra:
                    archive.writestr(entry, data)
        return path

    def verify(self, path, **pins):
        values = dict(expected_sha=transport.digest(path), workspace_commit=WORKSPACE,
                      sdk_commit=SDK, certificate=CERT)
        values.update(pins)
        return transport.validate_prepared_zip(path, **values)

    def edit_suite(self, suite, change):
        path = suite + '/prepared-manifest.json'
        data = json.loads(self.entries[path])
        change(data)
        self.entries[path] = encoded(data)

    def test_complete_valid_archive_keeps_unsigned_compiler_roles_separate(self):
        path = self.archive()
        root, manifest = self.verify(path)
        self.assertEqual(root, self.root)
        self.assertEqual(set(manifest['suites']), set(transport.SUITES))
        self.assertFalse(root.exists())
        self.assertEqual(sum(transport.CASE_COUNTS.values()), 93)

    def test_external_hash_source_root_and_certificate_pins(self):
        path = self.archive()
        for pins in ({'expected_sha': '0' * 64}, {'workspace_commit': 'd' * 40},
                     {'sdk_commit': 'e' * 40}, {'certificate': 'D' * 40}):
            with self.subTest(pins=pins), self.assertRaises(ValueError):
                self.verify(path, **pins)

    def test_unsafe_archive_names_duplicates_aliases_links_and_extra_fields(self):
        bad = ['../escape', '/private/tmp/absolute', self.prefix + 'callbacks/../../escape',
               self.prefix + 'callouts\\escape', 'outside/entry', self.prefix + 'callouts//escape',
               self.prefix + 'callouts/original-inputs/dart', self.prefix + 'CALLOUTS/original-inputs/dart']
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.verify(self.archive([(name, b'bad')]))
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
            entry = zipfile.ZipInfo(self.prefix + 'link')
            entry.external_attr = mode << 16
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.verify(self.archive([(entry, b'../../escape')]))
        entry = zipfile.ZipInfo(self.prefix + 'hardlink-extra')
        entry.extra = b'\x0d\x00\x00\x00'
        with self.assertRaises(ValueError):
            self.verify(self.archive([(entry, b'file')]))
        self.assertFalse(self.root.exists())

    def test_wrong_input_hash_and_wrong_provenance_are_rejected(self):
        name = 'callouts/original-inputs/dart'
        original = self.entries[name]
        self.entries[name] = b'tampered'
        with self.assertRaises(ValueError):
            self.verify(self.archive())
        self.entries[name] = original
        self.edit_suite('callouts', lambda data: data['original_artifacts']['dart'].update(usage='execute'))
        with self.assertRaises(ValueError):
            self.verify(self.archive())

    def test_signed_inputs_and_commands_cannot_select_original_compilers(self):
        self.edit_suite('callbacks', lambda data: data['signed_runtime_inputs'][0].update(path='original-inputs/dart'))
        with self.assertRaises(ValueError):
            self.verify(self.archive())
        self.setUp()
        self.edit_suite('full', lambda data: data['test_cases'][0]['argv'].__setitem__(0, '/bin/sh'))
        with self.assertRaises(ValueError):
            self.verify(self.archive())

    def test_missing_suite_or_case_cannot_satisfy_native_proof(self):
        self.edit_suite('full', lambda data: data['test_cases'].pop())
        with self.assertRaises(ValueError):
            self.verify(self.archive())

    def test_safe_extract_refuses_existing_root_and_cleanup_is_inode_bound(self):
        path = self.archive()
        evidence = self.directory / 'evidence'
        self.addCleanup(lambda: transport.cleanup(evidence) if self.root.exists() else None)
        root, _ = transport.extract_prepared(path, transport.digest(path), WORKSPACE, SDK, CERT, evidence)
        self.assertEqual(root, self.root)
        self.assertEqual(transport.digest(root / 'callouts/original-inputs/dart'),
                         self.build['files']['dart'])
        with self.assertRaises(FileExistsError):
            transport.extract_prepared(path, transport.digest(path), WORKSPACE, SDK, CERT, evidence)
        state = json.loads((evidence / 'owned-root.json').read_text())
        (evidence / 'owned-root.json').write_text(json.dumps(dict(state, inode=-1)))
        with self.assertRaises(ValueError):
            transport.cleanup(evidence)
        (evidence / 'owned-root.json').write_text(json.dumps(state))
        transport.cleanup(evidence)
        self.assertFalse(root.exists())

    def test_wrapper_runs_only_fixed_source_drivers_with_empty_credentials_and_collects_all_suites(self):
        for failed in (None, 'callbacks'):
            with self.subTest(failed=failed):
                self.setUp()
                path = self.archive()
                evidence = self.directory / 'run-evidence'
                self.addCleanup(lambda evidence=evidence, root=self.root: transport.cleanup(evidence) if root.exists() else None)
                calls = []
                workspace = (self.directory / 'source').resolve()
                def launch(command, **kwargs):
                    suite = next(name for name, driver in transport.SUITES.items() if command[1].endswith('/' + driver))
                    calls.append(suite)
                    self.assertEqual(command, [transport.sys.executable,
                        str(workspace / 'dart-sdk/tools' / transport.SUITES[suite]),
                        '--run-prepared=' + str(self.root / suite / 'prepared-manifest.json')])
                    self.assertEqual(kwargs['env'], transport.credential_free_environment())
                    self.assertTrue(kwargs['start_new_session'])
                    result_dir = self.root / (suite + '-results')
                    result_dir.mkdir()
                    prepared = json.loads(self.entries[suite + '/prepared-manifest.json'])
                    (result_dir / 'native-results.json').write_text(json.dumps({
                        'suite': suite, 'status': 'native_execution_passed', 'immutable_inputs_unchanged': True,
                        'cases': [{'id': case['id'], 'status': 'passed'} for case in prepared['test_cases']]}))
                    return mock.Mock(wait=mock.Mock(return_value=1 if suite == failed else 0))
                args = SimpleNamespace(evidence=evidence, workspace=workspace, sdk_commit=SDK,
                    archive=path, sha256=transport.digest(path), certificate_sha1=CERT)
                with mock.patch.object(transport, 'require_native_intel'), \
                        mock.patch.object(transport, 'source_identity', return_value=WORKSPACE), \
                        mock.patch.object(transport.subprocess, 'Popen', side_effect=launch), \
                        mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'private-token'}):
                    if failed:
                        with self.assertRaisesRegex(ValueError, 'mandatory'):
                            transport.run_proof(args)
                    else:
                        transport.run_proof(args)
                self.assertEqual(calls, list(transport.SUITES))
                report = json.loads((evidence / 'transport-results.json').read_text())
                self.assertEqual(report['status'], 'native_execution_failed' if failed else 'native_execution_passed')
                for suite in transport.SUITES:
                    self.assertTrue((evidence / suite / 'native-results.json').is_file())
                transport.cleanup(evidence)


class ReleaseMetadataTest(unittest.TestCase):
    def metadata(self):
        return {'id': 17, 'draft': True, 'published_at': None, 'assets': [
            {'id': 31, 'name': transport.ASSET_NAME, 'state': 'uploaded',
             'content_type': 'application/zip', 'size': 10, 'digest': 'sha256:' + 'a' * 64}]}

    def test_exact_draft_asset_is_accepted(self):
        self.assertEqual(transport.validate_release(self.metadata(), 17, 31, 'a' * 64)['id'], 31)

    def test_published_wrong_draft_release_asset_hash_and_metadata_reject(self):
        for field, value in [('draft', False), ('draft', 1), ('published_at', 'now'), ('id', 18)]:
            data = dict(self.metadata(), **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                transport.validate_release(data, 17, 31, 'a' * 64)
        for field, value in [('id', 32), ('name', 'other.zip'), ('digest', 'sha256:' + 'b' * 64),
                             ('size', 0), ('state', 'new'), ('content_type', 'application/octet-stream')]:
            data = self.metadata()
            data['assets'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                transport.validate_release(data, 17, 31, 'a' * 64)
        data = self.metadata()
        data['assets'] *= 2
        with self.assertRaises(ValueError):
            transport.validate_release(data, 17, 31, 'a' * 64)

    def test_redirect_strips_token_and_rejects_untrusted_destinations(self):
        request = urllib.request.Request('https://api.github.com/repos/o/r/releases/assets/31',
                                         headers={'Authorization': 'Bearer private-token'})
        redirect = transport.SafeRedirect()
        result = redirect.redirect_request(request, None, 302, '', {},
            'https://release-assets.githubusercontent.com/allowed?signature=opaque')
        self.assertIsNone(result.get_header('Authorization'))
        for target in ('http://release-assets.githubusercontent.com/file', 'https://evil.invalid/file',
                       'https://user:password@api.github.com/file'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, '', {}, target)

    def test_authenticated_download_uses_fixed_ids_rechecks_draft_and_retains_failure_evidence(self):
        for published_after in (False, True):
            with self.subTest(published_after=published_after), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                content = b'synthetic proof archive bytes'
                metadata = self.metadata()
                metadata['assets'][0].update(size=len(content), digest='sha256:' + sha(content))
                after = copy.deepcopy(metadata)
                if published_after:
                    after.update(draft=False, published_at='now')
                opener = mock.Mock()
                opener.open.side_effect = [io.BytesIO(encoded(metadata)), io.BytesIO(content), io.BytesIO(encoded(after))]
                with mock.patch.object(transport.urllib.request, 'build_opener', return_value=opener), \
                        mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'private-token'}):
                    if published_after:
                        with self.assertRaises(ValueError):
                            transport.download('owner/repo', 17, 31, sha(content), directory / 'proof.zip', directory / 'evidence')
                    else:
                        transport.download('owner/repo', 17, 31, sha(content), directory / 'proof.zip', directory / 'evidence')
                requests = [call.args[0] for call in opener.open.call_args_list]
                self.assertEqual([item.full_url for item in requests], [
                    'https://api.github.com/repos/owner/repo/releases/17',
                    'https://api.github.com/repos/owner/repo/releases/assets/31',
                    'https://api.github.com/repos/owner/repo/releases/17'])
                self.assertTrue(all(item.get_header('Authorization') == 'Bearer private-token' for item in requests))
                evidence = (directory / 'evidence/download-status.json').read_text()
                self.assertNotIn('private-token', evidence)
                self.assertEqual(json.loads(evidence)['status'],
                    'download_failed' if published_after else 'download_verified_runtime_pending')

    def test_native_gate_rejects_rosetta_and_children_get_no_credentials(self):
        with mock.patch.object(transport.sys, 'platform', 'darwin'), \
                mock.patch.object(transport.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(transport.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='1')):
            with self.assertRaisesRegex(ValueError, 'Rosetta'):
                transport.require_native_intel()
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'secret', 'AWS_SECRET_ACCESS_KEY': 'secret', 'DYLD_INSERT_LIBRARIES': 'evil'}):
            self.assertEqual(set(transport.credential_free_environment()), {'PATH', 'LANG', 'LC_ALL', 'TZ'})

    def test_json_duplicate_fields_and_noncanonical_paths_reject(self):
        with self.assertRaises(ValueError):
            transport.strict_json('{"draft":true,"draft":false}')
        for path in ('a/../b', 'a/./b', 'a//b', 'a\\b', '/absolute', 'a\nsecret', 'a:b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.relative(path)


class SourceAndWorkflowTest(unittest.TestCase):
    def synthetic_build(self, directory):
        build, source = directory / 'build', directory / 'source'
        for name in transport.BUILD_FILES | transport.SDK_REQUIRED:
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(MACHO + name.encode())
            path.chmod(0o755 if name in transport.BUILD_EXECUTABLE else 0o644)
        (build / 'args.gn').write_text('target_cpu = "x64"\ndart_dynamic_modules = false\n'
            'dart_enable_aot_patching = true\ndart_enable_shorebird_interpreter = true\n')
        return build, source

    def test_unsigned_producer_and_fresh_hash_verified_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            build, source = self.synthetic_build(directory)
            # create_sdk uses hardlinks in the original build tree. Packaging
            # must read them unchanged and extraction must break that alias.
            (build / 'dart-sdk/bin/dart').unlink()
            os.link(build / 'dart', build / 'dart-sdk/bin/dart')
            archive = directory / 'unsigned.zip'
            with mock.patch.object(transport, 'require_native_intel'), \
                    mock.patch.object(transport, 'source_identity', return_value=WORKSPACE), \
                    mock.patch.object(transport, 'git', return_value=SDK):
                transport.package_build(source, build, archive)
                destination = directory / 'private-build'
                manifest = transport.extract_build(archive, transport.digest(archive), source, SDK, destination)
                self.assertEqual(manifest['status'], 'build_inputs_only')
                self.assertEqual(manifest['format_version'], 2)
                self.assertEqual(manifest['files']['dartvm'], sha((build / 'dartvm').read_bytes()))
                self.assertEqual((destination / 'dartvm').read_bytes(), (build / 'dartvm').read_bytes())
                self.assertTrue((destination / 'dartvm').stat().st_mode & 0o111)
                self.assertEqual((destination / 'dartvm').stat().st_nlink, 1)
                self.assertNotEqual(manifest['files']['dartvm'], manifest['files']['dart-sdk/bin/dartvm'])
                self.assertEqual((destination / 'gen_snapshot_product').read_bytes(),
                                 (build / 'gen_snapshot_product').read_bytes())
                self.assertEqual((destination / 'dart-sdk/bin/dart').stat().st_nlink, 1)
                self.assertEqual((build / 'dart-sdk/bin/dart').stat().st_nlink, 2)
                with self.assertRaisesRegex(ValueError, 'fresh private'):
                    transport.extract_build(archive, transport.digest(archive), source, SDK, destination)
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    transport.extract_build(archive, '0' * 64, source, SDK, directory / 'wrong-hash')
                self.assertFalse((directory / 'wrong-hash').exists())

    def test_version_only_launcher_success_cannot_replace_missing_adjacent_vm(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            build, source = self.synthetic_build(directory)
            (build / 'dartvm').unlink()
            self.assertTrue((build / 'dart-sdk/bin/dartvm').is_file())
            # Model the observed split: --version succeeds, script launch
            # cannot find a sibling VM. No supplied executable is run here.
            launcher = mock.Mock(side_effect=lambda args: SimpleNamespace(
                returncode=0 if args == ['--version'] else 255,
                stdout='Dart SDK version: synthetic', stderr='Unable to locate the Dart VM executable'))
            self.assertEqual(launcher(['--version']).returncode, 0)
            self.assertEqual(launcher(['script.dart']).returncode, 255)
            with mock.patch.object(transport, 'require_native_intel'), \
                    mock.patch.object(transport, 'source_identity', return_value=WORKSPACE), \
                    mock.patch.object(transport, 'git', return_value=SDK), \
                    mock.patch.object(transport.subprocess, 'run', side_effect=AssertionError('No compiler probe')):
                with self.assertRaisesRegex(ValueError, 'dartvm'):
                    transport.package_build(source, build, directory / 'incomplete.zip')
            self.assertFalse((directory / 'incomplete.zip').exists())
            self.assertFalse((directory / 'incomplete.manifest.json').exists())

    def test_producer_requires_regular_executable_thin_x64_vm_companions(self):
        for name in ('dartvm', 'dart-sdk/bin/dartvm'):
            for mutation in ('missing', 'arm64', 'not-executable', 'symlink'):
                with self.subTest(name=name, mutation=mutation), tempfile.TemporaryDirectory() as temp:
                    directory = Path(temp).resolve()
                    build, source = self.synthetic_build(directory)
                    companion = build / name
                    if mutation in ('missing', 'symlink'):
                        companion.unlink()
                        if mutation == 'symlink':
                            companion.symlink_to(build / 'dart')
                    elif mutation == 'arm64':
                        companion.write_bytes(MACHO[:4] + struct.pack('<II', 0x100000c, 0) + b'synthetic')
                    else:
                        companion.chmod(0o644)
                    with mock.patch.object(transport, 'require_native_intel'), \
                            mock.patch.object(transport, 'source_identity', return_value=WORKSPACE), \
                            mock.patch.object(transport, 'git', return_value=SDK):
                        with self.assertRaises(ValueError):
                            transport.package_build(source, build, directory / 'rejected.zip')
                    self.assertFalse((directory / 'rejected.zip').exists())

    def test_current_extractor_rejects_legacy_missing_tampered_or_wrong_arch_vm(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            build, source = self.synthetic_build(directory)
            archive = directory / 'build.zip'
            with mock.patch.object(transport, 'require_native_intel'), \
                    mock.patch.object(transport, 'source_identity', return_value=WORKSPACE), \
                    mock.patch.object(transport, 'git', return_value=SDK):
                transport.package_build(source, build, archive)
                for mutation in ('legacy', 'missing-root', 'missing-sdk', 'tampered', 'arm64', 'not-executable'):
                    with zipfile.ZipFile(archive) as original:
                        entries = {info.filename: [info, original.read(info)] for info in original.infolist()}
                    manifest_name = 'build-inputs/build-inputs-manifest.json'
                    manifest = json.loads(entries[manifest_name][1])
                    root_vm = 'build-inputs/dartvm'
                    if mutation == 'legacy':
                        manifest['format_version'] = 1
                        manifest['files'].pop('dartvm')
                        del entries[root_vm]
                    elif mutation.startswith('missing-'):
                        name = 'dartvm' if mutation == 'missing-root' else 'dart-sdk/bin/dartvm'
                        del entries['build-inputs/' + name]
                        del manifest['files'][name]
                    elif mutation in ('tampered', 'arm64'):
                        data = (b'changed' if mutation == 'tampered' else
                                MACHO[:4] + struct.pack('<II', 0x100000c, 0) + b'synthetic')
                        entries[root_vm][1] = data
                        if mutation == 'arm64':
                            manifest['files']['dartvm'] = sha(data)
                    else:
                        entries[root_vm][0].external_attr = (stat.S_IFREG | 0o644) << 16
                    entries[manifest_name][1] = encoded(manifest)
                    bad = directory / (mutation + '.zip')
                    with zipfile.ZipFile(bad, 'w') as output:
                        for info, data in entries.values():
                            output.writestr(info, data)
                    destination = directory / ('extracted-' + mutation)
                    with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                        transport.extract_build(bad, transport.digest(bad), source, SDK, destination)
                    self.assertFalse(destination.exists())

    def test_old_gitlink_and_dirty_sdk_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / 'dart-sdk'
            sdk.mkdir()
            def run(repo, *args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
            for repo in (root, sdk):
                run(repo, 'init')
                run(repo, 'config', 'user.name', 'Fixture')
                run(repo, 'config', 'user.email', 'fixture@example.invalid')
            (sdk / 'tools').mkdir()
            names = (*transport.SUITES.values(), 'native_ffi_test_support.py', 'native_ffi_prepared_cases.py',
                     'native_ffi_prepared_manifest.schema.json')
            for name in names:
                (sdk / 'tools' / name).write_text('synthetic source')
            run(sdk, 'add', 'tools')
            run(sdk, 'commit', '-m', 'source')
            revision = run(sdk, 'rev-parse', 'HEAD')
            run(root, 'update-index', '--add', '--cacheinfo', '160000,' + revision + ',dart-sdk')
            run(root, 'commit', '-m', 'pin')
            self.assertEqual(transport.source_identity(root, revision), run(root, 'rev-parse', 'HEAD'))
            with self.assertRaisesRegex(ValueError, 'gitlink'):
                transport.source_identity(root, 'e' * 40)
            (sdk / 'tools' / names[0]).write_text('changed source')
            with self.assertRaises(subprocess.CalledProcessError):
                transport.source_identity(root, revision)

    def test_workflow_uses_native_runner_readonly_token_fixed_source_and_always_cleanup(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / '.github/workflows/macos-x64-prepared-proof.yml').read_text()
        for required in ('workflow_dispatch:', 'runs-on: macos-15-intel', 'contents: read',
                         'ref: ${{ github.sha }}', 'persist-credentials: false',
                         'GITHUB_TOKEN: ${{ github.token }}', '--release-id "$RELEASE_ID"',
                         '--asset-id "$ASSET_ID"', '--sha256 "$ASSET_SHA256"',
                         '--sdk-commit "$SDK_COMMIT"', '--certificate-sha1 "$CERTIFICATE_SHA1"',
                         'if: ${{ always() }}', 'macos_x64_proof_transport.py cleanup'):
            self.assertIn(required, text)
        self.assertNotIn('contents: write', text)
        self.assertEqual(text.count('GITHUB_TOKEN:'), 1)
        self.assertNotIn('codesign --sign', text)
        producer = (root / '.github/workflows/open-shorebird-ci.yml').read_text()
        producer = producer.split('  custom-dart-sdk-macos-x64:\n', 1)[1].split('\n  custom-dart-sdk-windows:', 1)[0]
        for required in ('product_ffi_test product_ffi_callback_test product_full_ffi_test',
                         'macos_x64_proof_transport.py verify-source',
                         'macos_x64_proof_transport.py package-build',
                         'macos-x64-ffi-proof-build-inputs.zip.sha256',
                         'macos-x64-ffi-proof-build-inputs.manifest.json'):
            self.assertIn(required, producer)


if __name__ == '__main__':
    unittest.main()

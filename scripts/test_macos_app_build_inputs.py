#!/usr/bin/env python3
"""Synthetic input/materialization checks; no compiler or native runtime proof."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import warnings
import zipfile

SPEC = importlib.util.spec_from_file_location('macos_app_build_inputs',
                                            Path(__file__).with_name('macos_app_build_inputs.py'))
inputs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inputs)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def thin(cpu, kind=2):
    return struct.pack('<8I', 0xfeedfacf, cpu, 0, kind, 0, 0, 0, 0) + b'synthetic-only'


def fat(cpus=(inputs.ARM64, inputs.X64)):
    entries, slices = [], b''
    offset = 8 + 20 * len(cpus)
    for cpu in cpus:
        data = thin(cpu)
        entries.append(struct.pack('>5I', cpu, 0, offset, len(data), 0))
        slices += data
        offset += len(data)
    return b'\xca\xfe\xba\xbe' + struct.pack('>I', len(cpus)) + b''.join(entries) + slices


def zip_bytes(entries, extra=()):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, (data, mode) in [*entries.items(), *extra]:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                archive.writestr(info, data)
    return out.getvalue()


def tar_header_types(path):
    """Observe extension headers before Python transparently consumes them."""
    kinds = []
    class InspectTarInfo(tarfile.TarInfo):
        def _proc_member(self, archive):
            kinds.append(self.type)
            return super()._proc_member(archive)
    with tarfile.open(path, 'r:*', tarinfo=InspectTarInfo) as archive:
        list(archive)
    return kinds


class MaterializerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.host = self.root / 'host'
        self.host.mkdir()
        self.pins = {name: chr(97 + index) * 40 for index, name in
                     enumerate(('dart_sdk', 'flutter_source', 'flutter', 'updater', 'shorebird'))}
        identity = 'open-shorebird-toolchain-v1\n' + ''.join(
            self.pins[k] + '\n' for k in ('dart_sdk', 'flutter_source', 'updater'))
        self.pins['engine_identity'] = hashlib.sha1(identity.encode()).hexdigest()
        self.host_provenance = {'schema': 'macos-app-host-provenance-v1', 'components': self.pins,
            'producer': {'kind': 'local-frozen-host', 'workspace_commit': '7' * 40,
                         'build_name': inputs.HOST_NAME, 'source_manifest_sha256': '8' * 64}}
        for name in inputs.HOST_REQUIRED | {'shader_lib/' + n for n in inputs.SHADER_FILES}:
            self.put_host(name, b'synthetic-' + name.encode())
        self.put_host('dart-sdk/revision', (self.pins['dart_sdk'] + '\n').encode())
        self.put_host('args.gn', self.args('arm64'))
        for name, kind in inputs.HOST_NATIVE.items():
            self.put_host(name, thin(inputs.ARM64, kind), kind == 2)
        # Extra files prove complete selected trees, including unused helpers,
        # are copied/inventoried rather than just the critical-path files.
        self.put_host('dart-sdk/lib/core/core.dart', b'// complete source input')
        self.put_host('dart-sdk/bin/utils/gen_snapshot', thin(inputs.ARM64), True)
        self.put_host('gen/dart-pkg/sky_engine/lib/core/core.dart', b'// source package')
        self.host_manifest = inputs.inventory_host(self.host, self.host_provenance, self.root / 'host.json')
        self.framework_entries = {name: (b'framework-input', stat.S_IFREG | 0o644)
                                  for name in inputs.FRAMEWORK_REQUIRED | {inputs.FRAMEWORK_LICENSE}}
        self.framework_entries[f'{inputs.FRAMEWORK}/Versions/A/FlutterMacOS'] = (
            thin(inputs.X64, 6), stat.S_IFREG | 0o755)
        self.framework_entries[f'{inputs.FRAMEWORK}/Versions/A/Resources/Info.plist'] = (
            plistlib.dumps({'CFBundleExecutable': 'FlutterMacOS', 'CFBundlePackageType': 'FMWK'}),
            stat.S_IFREG | 0o644)
        self.framework_entries.update({n: (v.encode(), stat.S_IFLNK | 0o777)
                                       for n, v in inputs.FRAMEWORK_LINKS.items()})
        self.platform_entries = {name: (b'PRODUCT-' + name.encode(), stat.S_IFREG | 0o644)
                                 for name in ('flutter_patched_sdk_product/platform_strong.dill',
                                              'flutter_patched_sdk_product/vm_outline_strong.dill',
                                              inputs.PLATFORM_LICENSE)}
        self.manifest = {'name': 'macos-engine-x64', 'git_sha': '1' * 40, 'runner_os': 'macOS',
            'engine_revision': self.pins['engine_identity'],
            'macos_build_dir': 'flutter/engine/src/out/macos_release_x64', 'runtime_mode': 'release',
            'target_os': 'macos', 'target_arch': 'x64', 'dart_dynamic_modules': False,
            'dart_enable_aot_patching': True, 'dart_enable_shorebird_interpreter': True,
            'shorebird_enable_aot_patching': True, 'shorebird_interpreter': False,
            'included': sorted(inputs.target_members(self.pins['engine_identity']))}
        self.pin = {'schema': 'macos-app-target-archive-v1', 'components': self.pins,
            'producer': {'repository': 'example/workspace', 'workspace_commit': '1' * 40,
                         'run_id': '123', 'artifact_id': '456', 'artifact_name': 'macos-engine-x64',
                         'metadata_origin': 'authenticated-api-export'}, 'archive_sha256': '0' * 64}
        self.target = self.root / 'target.tar.gz'
        self.tar_entries = {}
        self.repack()

    def put_host(self, name, data, executable=False):
        path = self.host / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if executable else 0o644)

    def args(self, arch):
        return ('\n'.join(f'{k} = {v}' for k, v in {
            'is_debug': 'false', 'target_os': '"mac"', 'target_cpu': f'"{arch}"',
            'dart_target_arch': f'"{arch}"', 'flutter_runtime_mode': '"release"',
            'dart_runtime_mode': '"release"', 'dart_dynamic_modules': 'false',
            'dart_enable_aot_patching': 'true', 'dart_enable_shorebird_interpreter': 'true',
            'shorebird_enable_aot_patching': 'true', 'shorebird_use_interpreter': 'false',
            'flutter_prebuilt_dart_sdk': 'false', 'dart_version': json.dumps(self.pins['dart_sdk']),
            'engine_version': json.dumps(self.pins['flutter'])}.items()) + '\n').encode()

    def repack(self, overrides=None, extra=()):
        framework = zip_bytes(self.framework_entries)
        font = b'unused font ZIP is hash-bound but never extracted/executed'
        values = {n: (b'input-' + n.encode(), stat.S_IFREG | 0o644)
                  for n in inputs.target_members(self.pins['engine_identity'])}
        values['FlutterMacOS.framework.zip'] = (framework, stat.S_IFREG | 0o644)
        values['flutter_patched_sdk_product.zip'] = (zip_bytes(self.platform_entries), stat.S_IFREG | 0o644)
        values['macos_release_x64.args.gn'] = (self.args('x64'), stat.S_IFREG | 0o644)
        values['gen_snapshot_x64'] = (fat(), stat.S_IFREG | 0o755)
        values['analyze_snapshot_x64'] = (fat(), stat.S_IFREG | 0o755)
        values['font-subset.zip'] = (font, stat.S_IFREG | 0o644)
        for n in values:
            if n.startswith('mirror/'):
                data = framework if n.endswith('FlutterMacOS.framework.zip') else (
                    font if n.endswith('font-subset.zip') else fat())
                values[n] = (data, stat.S_IFREG | 0o644)
        values['manifest.json'] = (encoded(self.manifest), stat.S_IFREG | 0o644)
        values = {'macos-engine/' + n: v for n, v in values.items()}
        for source, value in (overrides or {}).items():
            if source == 'macos-engine/FlutterMacOS.framework.zip':
                for n in values:
                    if n.startswith('macos-engine/mirror/') and n.endswith('FlutterMacOS.framework.zip'):
                        values[n] = value
        values.update(overrides or {})
        self.tar_entries = values
        self.write_tar([*values.items(), *extra])

    def write_tar(self, entries, format=tarfile.USTAR_FORMAT):
        with tarfile.open(self.target, 'w:gz', format=format) as archive:
            for name, (data, mode) in entries:
                info = tarfile.TarInfo(name)
                info.mode = mode & 0o777
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode()
                    archive.addfile(info)
                elif stat.S_IFMT(mode) == stat.S_IFCHR:
                    info.type = tarfile.CHRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        self.pin['archive_sha256'] = sha(self.target.read_bytes())

    def materialize(self, **kwargs):
        return inputs.materialize(self.host, kwargs.pop('host_manifest', self.host_manifest), self.target,
                                  kwargs.pop('pin', self.pin), kwargs.pop('destination', self.root / 'result'),
                                  **kwargs)

    def assert_rejected_cleanly(self):
        with self.assertRaises((ValueError, OSError, tarfile.TarError, zipfile.BadZipFile)):
            self.materialize()
        self.assertFalse((self.root / 'result').exists())

    def test_complete_arm_host_x64_target_keeps_original_bytes_and_provenance(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('No execution')):
            result = self.materialize()
        root = self.root / 'result'
        self.assertFalse(result['runtime_acceptance'])
        self.assertEqual(result['status'], 'materialized_unexecuted')
        self.assertEqual(result['host']['arch'], 'arm64')
        self.assertEqual(result['target']['arch'], 'x64')
        self.assertEqual(result['target']['producer'], self.pin['producer'])
        self.assertNotEqual(result['target']['producer']['workspace_commit'],
                            result['host']['producer']['workspace_commit'])
        for name, entry in self.host_manifest['files'].items():
            out = root / 'out' / inputs.HOST_NAME / name
            self.assertEqual(sha(out.read_bytes()), entry['sha256'])
            self.assertEqual(out.stat().st_nlink, 1)
        target = root / 'out' / inputs.TARGET_NAME
        self.assertEqual((target / 'args.gn').read_bytes(), self.args('x64'))
        self.assertEqual((target / 'gen_snapshot_x64').read_bytes(), fat())
        self.assertEqual((target / 'flutter_patched_sdk/platform_strong.dill').read_bytes(),
                         (target / 'flutter_patched_sdk_product/platform_strong.dill').read_bytes())
        framework = target / 'FlutterMacOS.xcframework/macos-x86_64' / inputs.FRAMEWORK
        self.assertTrue((framework / 'FlutterMacOS').is_symlink())
        self.assertEqual((framework / 'FlutterMacOS').read_bytes(), thin(inputs.X64, 6))
        plist = plistlib.loads((target / 'FlutterMacOS.xcframework/Info.plist').read_bytes())
        self.assertEqual(plist['AvailableLibraries'][0]['SupportedArchitectures'], ['x86_64'])
        actual = inputs.output_inventory(root)
        del actual['build-inputs.json']
        self.assertEqual(result['files'], actual)

    def test_output_inventory_is_deterministic_except_receipt_itself(self):
        first = self.materialize(destination=self.root / 'first')
        second = self.materialize(destination=self.root / 'second')
        self.assertEqual(first, second)
        self.assertNotIn('build-inputs.json', first['files'])

    def test_public_cli_requires_external_json_pins_and_never_executes(self):
        provenance = self.root / 'provenance.json'
        provenance.write_bytes(encoded(self.host_provenance))
        host_output = self.root / 'cli-host.json'
        inputs.main(['inventory-host', '--host', str(self.host), '--provenance', str(provenance),
                     '--provenance-sha256', sha(provenance.read_bytes()), '--output', str(host_output)])
        pin_path = self.root / 'target-pin.json'
        pin_path.write_bytes(encoded(self.pin))
        argv = ['materialize', '--host', str(self.host), '--host-manifest', str(host_output),
                '--host-manifest-sha256', sha(host_output.read_bytes()), '--target-archive', str(self.target),
                '--target-pin', str(pin_path), '--target-pin-sha256', sha(pin_path.read_bytes()),
                '--destination', str(self.root / 'result')]
        with mock.patch('subprocess.run', side_effect=AssertionError('No execution')):
            self.assertEqual(inputs.main(argv), 0)
        receipt = json.loads((self.root / 'result/build-inputs.json').read_text())
        self.assertEqual(receipt['metadata']['external_sha256'], {
            'host_manifest': sha(host_output.read_bytes()), 'target_pin': sha(pin_path.read_bytes())})
        self.assertEqual(len(receipt['required_private_flutter_cache_bindings']), 2)
        with self.assertRaises(ValueError):
            inputs.pinned_json(pin_path, '0' * 64)

    def test_wrong_or_unknown_source_producer_metadata_is_rejected(self):
        for change in (
            lambda p: p['components'].__setitem__('engine_identity', '0' * 40),
            lambda p: p['producer'].__setitem__('run_id', '0'),
            lambda p: p['producer'].__setitem__('artifact_id', 123),
            lambda p: p['producer'].__setitem__('metadata_origin', 'assumed-ci-output'),
            lambda p: p.__setitem__('extra', True),
        ):
            pin = copy.deepcopy(self.pin)
            change(pin)
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.materialize(pin=pin)
        self.assertFalse((self.root / 'result').exists())

    def test_host_target_components_must_match_without_relabeling_producers(self):
        pin = copy.deepcopy(self.pin)
        pin['components']['shorebird'] = '9' * 40
        with self.assertRaisesRegex(ValueError, 'component identity'):
            self.materialize(pin=pin)

    def test_archive_hash_manifest_root_engine_arch_and_flags_are_bound(self):
        pin = copy.deepcopy(self.pin)
        pin['archive_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.materialize(pin=pin)
        for key, value in [('git_sha', '2' * 40), ('engine_revision', '0' * 40),
                           ('target_arch', 'arm64'), ('dart_dynamic_modules', True),
                           ('dart_enable_shorebird_interpreter', False), ('shorebird_interpreter', True),
                           ('runtime_mode', 'debug'), ('runner_os', 'Linux'), ('new_field', True)]:
            original = copy.deepcopy(self.manifest)
            self.manifest[key] = value
            self.repack()
            with self.subTest(field=key):
                self.assert_rejected_cleanly()
            self.manifest = original

    def test_all_critical_original_gn_flags_and_source_pins_are_required(self):
        good = self.args('x64')
        for line in good.splitlines():
            bad = good.replace(line + b'\n', b'')
            self.repack({'macos-engine/macos_release_x64.args.gn': (bad, stat.S_IFREG | 0o644)})
            with self.subTest(line=line):
                self.assert_rejected_cleanly()
        self.repack({'macos-engine/macos_release_x64.args.gn': (
            good + b'target_cpu = "arm64"\n', stat.S_IFREG | 0o644)})
        self.assert_rejected_cleanly()

    def test_no_cached_fallback_for_missing_or_modified_host_tools(self):
        for name in ('impellerc', 'font-subset', 'gen/const_finder.dart.snapshot',
                     'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot',
                     'gen/dart-pkg/sky_engine/lib/ui/ui.dart'):
            path = self.host / name
            saved = path.read_bytes()
            path.unlink()
            with self.subTest(missing=name):
                self.assert_rejected_cleanly()
            path.write_bytes(saved)
            path.chmod(0o755 if self.host_manifest['files'][name]['executable'] else 0o644)
        self.put_host('dart-sdk/uninventoried', b'changed')
        self.assert_rejected_cleanly()

    def test_source_mode_hash_empty_inputs_and_wrong_sdk_revision_are_rejected(self):
        for name, data in [('font-subset', b'tampered'), ('dart-sdk/revision', b'0' * 40),
                           ('shader_lib/flutter/runtime_effect.glsl', b'')]:
            path = self.host / name
            before = path.read_bytes()
            path.write_bytes(data)
            with self.subTest(name=name):
                self.assert_rejected_cleanly()
            path.write_bytes(before)
        path = self.host / 'font-subset'
        path.chmod(0o644)
        self.assert_rejected_cleanly()

    def test_synthetic_host_provenance_cannot_hide_intel_or_fat_host_tools(self):
        for data in (thin(inputs.X64), fat()):
            self.put_host('impellerc', data, True)
            manifest = {**self.host_manifest, 'files': inputs.host_inventory(self.host)}
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.materialize(host_manifest=manifest)

    def test_host_links_hardlinks_and_case_collisions_are_rejected(self):
        path = self.host / 'dart-sdk/lib/alias'
        for kind in ('symlink', 'hardlink'):
            if kind == 'symlink':
                path.symlink_to('core/core.dart')
            elif kind == 'hardlink':
                os.link(self.host / 'dart-sdk/lib/core/core.dart', path)
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                inputs.host_inventory(self.host)
            path.unlink()
        # macOS commonly refuses creating two case-colliding physical names;
        # exercise the inventory validator directly without a platform skip.
        with self.assertRaises(ValueError):
            inputs.check_names(['dart-sdk/lib/core/core.dart', 'dart-sdk/lib/CORE/other'])

    def test_tar_traversal_aliases_duplicates_links_specials_and_unknown_files(self):
        for name, mode in [('../escape', stat.S_IFREG), ('/absolute', stat.S_IFREG),
                           ('macos-engine//extra', stat.S_IFREG), ('macos-engine/./extra', stat.S_IFREG),
                           ('macos-engine\\extra', stat.S_IFREG), ('macos-engine/extra', stat.S_IFREG),
                           ('macos-engine/gen_snapshot_x64', stat.S_IFREG),
                           ('macos-engine/alias', stat.S_IFLNK), ('macos-engine/device', stat.S_IFCHR)]:
            self.repack(extra=[(name, (b'input', mode | 0o644))])
            with self.subTest(name=name, mode=mode):
                self.assert_rejected_cleanly()

    def test_tar_extensions_and_oversized_headers_are_refused_before_payload_read(self):
        for kind, size in ((tarfile.XHDTYPE, 4096), (tarfile.GNUTYPE_LONGNAME, 4096),
                           (tarfile.LNKTYPE, 0), (tarfile.REGTYPE, inputs.MAX_FILE + 1)):
            header = tarfile.TarInfo('macos-engine/manifest.json')
            header.type, header.size = kind, size
            # Deliberately omit the declared payload. The bounded header
            # guard must reject it before tarfile attempts that read.
            self.target.write_bytes(header.tobuf(format=tarfile.USTAR_FORMAT) + b'\0' * 1024)
            self.pin['archive_sha256'] = sha(self.target.read_bytes())
            with self.subTest(kind=kind, size=size), self.assertRaisesRegex(ValueError, 'tar member|PAX'):
                self.materialize()
            self.assertFalse((self.root / 'result').exists())

    def system_tar_source(self):
        source = self.root / 'pack-source'
        for name, (data, mode) in self.tar_entries.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode & 0o777)
        return source

    def test_current_system_tar_recipe_accepts_bsd_but_rejects_gnu_longname(self):
        source = self.system_tar_source()
        # Real host packaging of synthetic inputs, never a compiler/app. On
        # macOS this exercises BSD tar's actual per-file PAX/AppleDouble output.
        # GNU's default instead emits unsupported long-name extensions for
        # our canonical mirror paths; it is not the BSD producer's recipe.
        version = subprocess.run(['/usr/bin/tar', '--version'], check=True,
                                 capture_output=True, text=True).stdout
        subprocess.run(['/usr/bin/tar', '-C', str(source), '-czf', str(self.target), 'macos-engine'], check=True)
        self.pin['archive_sha256'] = sha(self.target.read_bytes())
        if 'GNU tar' in version:
            self.assertIn(tarfile.GNUTYPE_LONGNAME, tar_header_types(self.target))
            with self.assertRaisesRegex(ValueError, 'Extended/special/oversize tar member'):
                self.materialize()
            self.assertFalse((self.root / 'result').exists())
            return
        self.assertIn('bsdtar', version, 'System archive producer is neither BSD nor GNU tar')
        result = self.materialize()
        self.assertFalse(any('/._' in n or n.startswith('._') for n in result['files']))

    def test_system_tar_explicit_ustar_recipe_is_portable(self):
        source = self.system_tar_source()
        subprocess.run(['/usr/bin/tar', '--format=ustar', '-C', str(source),
                        '-czf', str(self.target), 'macos-engine'], check=True)
        self.pin['archive_sha256'] = sha(self.target.read_bytes())
        self.assertNotIn(tarfile.GNUTYPE_LONGNAME, tar_header_types(self.target))
        result = self.materialize()
        self.assertEqual(result['target']['archive_sha256'], self.pin['archive_sha256'])

    def test_complete_gnu_longname_archive_is_rejected_without_restoring_it(self):
        # A complete otherwise valid archive reproduces GNU's default long
        # mirror-path encoding even on macOS hosts without a GNU tar binary.
        self.assertTrue(any(len(name.encode()) > 100 for name in self.tar_entries))
        self.write_tar(list(self.tar_entries.items()), format=tarfile.GNU_FORMAT)
        self.assertIn(tarfile.GNUTYPE_LONGNAME, tar_header_types(self.target))
        with self.assertRaisesRegex(ValueError, 'Extended/special/oversize tar member'):
            self.materialize()
        self.assertFalse((self.root / 'result').exists())

    def test_pax_semantic_overrides_and_malformed_appledouble_are_rejected(self):
        for key, value in [('path', '../escape'), ('size', '999999999'), ('linkpath', '/outside'),
                           ('GNU.sparse.size', '1'), ('vendor.unknown', 'unbound')]:
            with tarfile.open(self.target, 'w:gz', format=tarfile.PAX_FORMAT,
                              pax_headers={key: value}) as archive:
                info = tarfile.TarInfo('macos-engine/manifest.json')
                archive.addfile(info, io.BytesIO())
            self.pin['archive_sha256'] = sha(self.target.read_bytes())
            self.assert_rejected_cleanly()
        self.repack(extra=[('macos-engine/._manifest.json', (b'not-AppleDouble', stat.S_IFREG | 0o644))])
        self.assert_rejected_cleanly()
        header = struct.pack('>2I', 0x00051607, 0x00020000) + b'\0' * 16 + struct.pack('>H', 1)
        valid = header + struct.pack('>3I', 9, 38, 1) + b'x'
        self.repack(extra=[('macos-engine/._not-declared', (valid, stat.S_IFREG | 0o644))])
        self.assert_rejected_cleanly()

    def test_bounded_pax_and_appledouble_validation_is_platform_independent(self):
        def record(key, value):
            body = b' ' + key + b'=' + value + b'\n'
            size = len(body) + 1
            while len(str(size).encode()) + len(body) != size:
                size = len(str(size).encode()) + len(body)
            return str(size).encode() + body
        inputs.validate_pax(record(b'mtime', b'123.456') + record(b'SCHILY.xattr.com.apple.provenance', b'opaque'))
        for payload in (record(b'path', b'../escape'), record(b'size', b'999'),
                        record(b'linkpath', b'/outside'), record(b'GNU.sparse.size', b'1'),
                        record(b'mtime', b'NaN'), record(b'unknown', b'x'),
                        record(b'mtime', b'1') * 2, b'999999999999999 x=y\n'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                inputs.validate_pax(payload)
        header = struct.pack('>2I', 0x00051607, 0x00020000) + b'\0' * 16 + struct.pack('>H', 1)
        inputs.validate_appledouble(header + struct.pack('>3I', 9, 38, 1) + b'x')
        for entry in (struct.pack('>3I', 9, 0, 1), struct.pack('>3I', 9, 38, 99),
                      struct.pack('>3I', 999, 38, 1)):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                inputs.validate_appledouble(header + entry + b'x')

    def test_parser_consumes_owned_hash_verified_snapshot_not_reopened_source(self):
        original = tarfile.open
        def replace_source(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.target)
            self.target.write_bytes(b'replaced after independent snapshot copy')
            return original(path, *args, **kwargs)
        with mock.patch.object(tarfile, 'open', side_effect=replace_source):
            result = self.materialize()
        self.assertEqual(result['target']['archive_sha256'], self.pin['archive_sha256'])

    def test_fresh_host_inventory_still_rejects_wrong_revision_and_hidden_intel_helper(self):
        for name, data, executable in (
            ('dart-sdk/revision', b'0' * 40, False),
            ('dart-sdk/bin/hidden-helper', thin(inputs.X64), True),
            ('shader_lib/unknown.glsl', b'unknown', False),
        ):
            path = self.host / name
            before = path.read_bytes() if path.exists() else None
            self.put_host(name, data, executable)
            fresh = {**self.host_manifest, 'files': inputs.host_inventory(self.host)}
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.materialize(host_manifest=fresh)
            if before is None:
                path.unlink()
            else:
                self.put_host(name, before)

    def test_framework_escaping_cycles_dangling_unknown_and_parent_aliases(self):
        name = f'{inputs.FRAMEWORK}/Versions/Current'
        for value in ('../../outside', '/absolute', 'Current', '../Current', 'B'):
            saved = self.framework_entries[name]
            self.framework_entries[name] = (value.encode(), stat.S_IFLNK | 0o777)
            self.repack()
            with self.subTest(value=value):
                self.assert_rejected_cleanly()
            self.framework_entries[name] = saved
        for name in ('extra', f'{inputs.FRAMEWORK}/Versions/A/alias',
                     f'{inputs.FRAMEWORK}/Versions/Current/file'):
            self.framework_entries[name] = (b'A', stat.S_IFLNK | 0o777)
            self.repack()
            with self.subTest(extra=name):
                self.assert_rejected_cleanly()
            del self.framework_entries[name]

    def test_framework_unknown_files_outside_root_and_duplicate_zip_members(self):
        for name in ('../escape', '/absolute', 'extra', f'{inputs.FRAMEWORK}//extra',
                     f'{inputs.FRAMEWORK}/Versions/a/other'):
            entries = {**self.framework_entries, name: (b'bad', stat.S_IFREG | 0o644)}
            bad = zip_bytes(entries)
            self.repack({'macos-engine/FlutterMacOS.framework.zip': (bad, stat.S_IFREG | 0o644)})
            with self.subTest(name=name):
                self.assert_rejected_cleanly()
        name = next(iter(self.framework_entries))
        bad = zip_bytes(self.framework_entries, extra=[(name, self.framework_entries[name])])
        # Refresh mirror as well to exercise ZIP inspection rather than mirror gate.
        overrides = {n: (bad, stat.S_IFREG | 0o644) for n in self.tar_entries
                     if n.endswith('FlutterMacOS.framework.zip')}
        self.repack(overrides)
        self.assert_rejected_cleanly()

    def test_framework_and_target_native_architectures_are_not_inferred_from_host(self):
        image = f'{inputs.FRAMEWORK}/Versions/A/FlutterMacOS'
        for data in (thin(inputs.ARM64, 6), fat(), b'not-Mach-O'):
            self.framework_entries[image] = (data, stat.S_IFREG | 0o755)
            self.repack()
            with self.subTest(image=data):
                self.assert_rejected_cleanly()
        self.framework_entries[image] = (thin(inputs.X64, 6), stat.S_IFREG | 0o755)
        for data in (thin(inputs.X64), fat((inputs.ARM64, inputs.ARM64)), fat((inputs.X64,)), b'bad'):
            self.repack({'macos-engine/gen_snapshot_x64': (data, stat.S_IFREG | 0o755)})
            with self.subTest(tool=data):
                self.assert_rejected_cleanly()

    def test_product_zip_refuses_unexpected_missing_or_aliased_platform_files(self):
        for entries in (
            {k: v for k, v in self.platform_entries.items() if not k.endswith('vm_outline_strong.dill')},
            {**self.platform_entries, 'vm_platform_product.dill': (b'wrong', stat.S_IFREG | 0o644)},
            {**self.platform_entries, 'flutter_patched_sdk_product/../escape': (b'bad', stat.S_IFREG | 0o644)},
        ):
            self.repack({'macos-engine/flutter_patched_sdk_product.zip': (zip_bytes(entries), stat.S_IFREG | 0o644)})
            self.assert_rejected_cleanly()

    def test_mirror_content_is_bound_instead_of_silently_choosing_first(self):
        name = next(n for n in self.tar_entries if n.startswith('macos-engine/mirror/')
                    and n.endswith('FlutterMacOS.framework.zip'))
        self.repack({name: (b'wrong mirror', stat.S_IFREG | 0o644)})
        self.assert_rejected_cleanly()

    def test_existing_destination_and_alias_parent_are_never_overwritten(self):
        dest = self.root / 'result'
        dest.mkdir()
        marker = dest / 'keep'
        marker.write_bytes(b'untouched')
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertEqual(marker.read_bytes(), b'untouched')
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.materialize(destination=alias / 'new')

    def test_copy_race_hash_mismatch_removes_only_owned_partial_destination(self):
        original = inputs.copy_host
        def changed(root, manifest, destination):
            self.put_host('dart-sdk/version', b'changed after validation')
            return original(root, manifest, destination)
        with mock.patch.object(inputs, 'copy_host', side_effect=changed):
            self.assert_rejected_cleanly()
        self.assertTrue(self.host.is_dir())

    def test_inventory_stops_growing_or_truncated_reader_with_bounded_read_count(self):
        for chunks, reason in (([b'abc', b'x'], 'grew'), ([b'ab', b''], 'truncated')):
            reads = []
            class Reader:
                def read(self, size):
                    reads.append(size)
                    if len(reads) > 2:
                        raise AssertionError('Unbounded host read continued')
                    return chunks[len(reads) - 1]
            @contextlib.contextmanager
            def changed(*args, **kwargs):
                yield Reader(), SimpleNamespace(st_size=3, st_mode=stat.S_IFREG | 0o644)
            with self.subTest(reason=reason), mock.patch.object(inputs, 'host_file', changed):
                with self.assertRaisesRegex(ValueError, reason):
                    inputs.host_inventory(self.host)
            self.assertEqual(reads, [4, 1] if reason == 'grew' else [4, 2])

    def test_copy_checks_growth_before_writing_and_rejects_truncation(self):
        for index, (chunks, reason) in enumerate((([b'abc', b'x'], 'grew'), ([b'ab', b''], 'truncated'))):
            reads = []
            class Reader:
                def read(self, size):
                    reads.append(size)
                    if len(reads) > 2:
                        raise AssertionError('Unbounded host copy continued')
                    return chunks[len(reads) - 1]
            @contextlib.contextmanager
            def changed(*args, **kwargs):
                yield Reader(), SimpleNamespace(st_size=3, st_mode=stat.S_IFREG | 0o644)
            manifest = {'files': {'synthetic': {'size': 3, 'sha256': sha(b'abc'), 'executable': False}}}
            destination = self.root / f'copy-{index}'
            with self.subTest(reason=reason), mock.patch.object(inputs, 'host_file', changed):
                with self.assertRaisesRegex(ValueError, reason):
                    inputs.copy_host(self.host, manifest, destination)
            self.assertEqual((destination / 'synthetic').read_bytes(), b'abc' if reason == 'grew' else b'ab')
            self.assertEqual(len(reads), 2)

    def test_declared_file_and_aggregate_bounds_fail_before_host_stream_read(self):
        @contextlib.contextmanager
        def declared(*args, **kwargs):
            yield SimpleNamespace(read=mock.Mock(side_effect=AssertionError('Must not read'))), \
                SimpleNamespace(st_size=3, st_mode=stat.S_IFREG | 0o644)
        for bound in ('MAX_FILE', 'MAX_TOTAL'):
            with self.subTest(bound=bound), mock.patch.object(inputs, bound, 2), \
                    mock.patch.object(inputs, 'host_file', declared):
                with self.assertRaisesRegex(ValueError, 'bound'):
                    inputs.host_inventory(self.host)
                manifest = {'files': {'synthetic': {'size': 3, 'sha256': sha(b'abc'), 'executable': False}}}
                with self.assertRaisesRegex(ValueError, 'bound'):
                    inputs.copy_host(self.host, manifest, self.root / bound)
        # Reserve already completed bytes as well as the current declaration.
        reader = mock.Mock()
        with mock.patch.object(inputs, 'MAX_TOTAL', 4), self.assertRaisesRegex(ValueError, 'bound'):
            list(inputs.bounded_blocks(reader, 3, completed=2))
        reader.read.assert_not_called()

    def test_post_inventory_host_header_read_is_bounded_too(self):
        reads = []
        class Reader:
            def read(self, size):
                reads.append(size)
                return b'abc' if len(reads) == 1 else b'x'
        @contextlib.contextmanager
        def changed(*args, **kwargs):
            yield Reader(), SimpleNamespace(st_size=3, st_mode=stat.S_IFREG | 0o644)
        with mock.patch.object(inputs, 'host_file', changed), self.assertRaisesRegex(ValueError, 'grew'):
            inputs.bounded_bytes(self.host / 'args.gn')
        self.assertEqual(reads, [4, 1])

    def test_json_duplicates_bounds_and_extra_metadata_fail_closed(self):
        for data in (b'{"schema":1,"schema":2}',):
            with self.assertRaises(ValueError):
                inputs.strict_json(data)
        with mock.patch.object(inputs, 'MAX_JSON', 3), self.assertRaises(ValueError):
            inputs.strict_json(b'{"a":1}')
        large = self.root / 'large.json'
        large.write_bytes(b'large')
        with mock.patch.object(inputs, 'MAX_JSON', 3), mock.patch.object(
                Path, 'read_bytes', side_effect=AssertionError('Must bound before read')):
            with self.assertRaises(ValueError):
                inputs.pinned_json(large, '0' * 64)
        manifest = copy.deepcopy(self.host_manifest)
        manifest['surprise'] = 'unbound'
        with self.assertRaises(ValueError):
            self.materialize(host_manifest=manifest)
        with mock.patch.object(inputs, 'MAX_ARCHIVE', 1):
            self.assert_rejected_cleanly()

    def test_macho_rejects_overlaps_wrong_slice_type_and_malformed_load_commands(self):
        good = bytearray(fat())
        struct.pack_into('>I', good, 8 + 20 + 8, 8 + 20 * 2)
        for data in (bytes(good), thin(inputs.X64, 6),
                     struct.pack('<8I', 0xfeedfacf, inputs.X64, 0, 2, 1, 8, 0, 0) + b'\0' * 8):
            with self.subTest(data=data), self.assertRaises(ValueError):
                inputs.macho(data, {inputs.ARM64, inputs.X64}, 2, universal=True)


if __name__ == '__main__':
    unittest.main()

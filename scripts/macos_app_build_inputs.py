#!/usr/bin/env python3
"""Materialize pinned ARM64-host/x64-target Mac app build inputs; never execute them.

Two offline commands:
  inventory-host --host H --provenance P --provenance-sha256 SHA --output HOST.json
  materialize --host H --host-manifest HOST.json --host-manifest-sha256 SHA
    --target-archive macos-engine-x64.tar.gz --target-pin TARGET.json
    --target-pin-sha256 SHA --destination FRESH

The caller authenticates and externally pins the JSON inputs, including their
actual producer/component identities. This program does not contact CI or attest
that arbitrary binaries were compiled from claimed source. Target-pin schema is
macos-app-target-archive-v1, with components, producer, archive_sha256. Producer
records repository, workspace_commit, run_id, artifact_id, artifact_name, and
metadata_origin (authenticated-api-export or producer-attestation). A sidecar
assembled from authenticated CI API data must say authenticated-api-export.
Host provenance schema is macos-app-host-provenance-v1, with components and
producer {kind:local-frozen-host, workspace_commit, build_name,
source_manifest_sha256}. inventory-host adds the exact closure file inventory.
Components: dart_sdk, flutter_source, flutter, updater, shorebird, engine_identity.
The engine identity is checked against the canonical derived toolchain identity.

Only complete selected host trees/tools and the current Mac packaging recipe are
accepted. No SDK proof extractor is reused, no GN regeneration, Xcode invocation,
compiler probe, signing, package resolution or app execution occurs. Version and
compiler capability checks remain mandatory preparation work after this step.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import struct
import tarfile
import zipfile

COMPONENTS = {'dart_sdk', 'flutter_source', 'flutter', 'updater', 'shorebird', 'engine_identity'}
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
MAX_FILES = 20000
MAX_FILE = 512 * 1024**2
MAX_TOTAL = 4 * 1024**3
MAX_ARCHIVE = 2 * 1024**3
MAX_JSON = 8 * 1024**2
MAX_PAX = 256 * 1024
MAX_METADATA = 8 * 1024**2
ARM64, X64 = 0x0100000c, 0x01000007
HOST_NAME, TARGET_NAME = 'macos_release_arm64', 'macos_release_x64'
HOST_TREES = ('dart-sdk', 'gen/dart-pkg/sky_engine', 'shader_lib')
HOST_FILES = ('args.gn', 'dartaotruntime', 'font-subset', 'gen/const_finder.dart.snapshot')
HOST_NATIVE = {'dart-sdk/bin/dart': 2, 'dart-sdk/bin/dartaotruntime': 2,
               'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot': 6,
               'dart-sdk/bin/snapshots/dart2bytecode.dart.snapshot': 6,
               'dartaotruntime': 2, 'font-subset': 2, 'impellerc': 2}
HOST_REQUIRED = {*HOST_FILES, *HOST_NATIVE, 'dart-sdk/revision', 'dart-sdk/version',
                 'dart-sdk/lib/_internal/vm_platform_strong.dill',
                 'gen/dart-pkg/sky_engine/pubspec.yaml',
                 'gen/dart-pkg/sky_engine/lib/ui/ui.dart',
                 'gen/dart-pkg/sky_engine/lib/_embedder.yaml',
                 'gen/dart-pkg/sky_engine/lib/ui_web/ui_web.dart',
                 'shader_lib/flutter/runtime_effect.glsl'}
SHADER_FILES = {'flutter/runtime_effect.glsl', *(f'impeller/{name}.glsl' for name in (
    'blending', 'branching', 'color', 'conical_gradient_uniform_fill', 'constants',
    'dithering', 'external_texture_oes', 'gaussian', 'gradient', 'math', 'path',
    'texture', 'tile_mode', 'transform', 'types'))}
FRAMEWORK = 'FlutterMacOS.framework'
FRAMEWORK_LICENSE = 'LICENSE.zip_macos_flutter_framework.md'
PLATFORM_LICENSE = 'LICENSE.flutter_patched_sdk.md'
FRAMEWORK_LINKS = {f'{FRAMEWORK}/Versions/Current': 'A', **{
    f'{FRAMEWORK}/{name}': f'Versions/Current/{name}'
    for name in ('FlutterMacOS', 'Headers', 'Modules', 'Resources')}}
FRAMEWORK_REQUIRED = {f'{FRAMEWORK}/Versions/A/{name}' for name in (
    'FlutterMacOS', 'Resources/Info.plist', 'Resources/icudtl.dat',
    'Headers/FlutterMacOS.h', 'Modules/module.modulemap', 'Resources/PrivacyInfo.xcprivacy')}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relative(name):
    require(isinstance(name, str) and name and name.isascii() and '\\' not in name
            and ':' not in name and all(32 <= ord(c) < 127 for c in name)
            and not name.startswith('/') and len(name) <= 1024
            and all(p not in ('', '.', '..') for p in name.split('/'))
            and len(name.split('/')) <= 32, 'Unsafe or aliased relative path')
    return name


def check_names(names):
    folded = {}
    for name in names:
        relative(name)
        parts = name.split('/')
        for i in range(1, len(parts) + 1):
            part = '/'.join(parts[:i])
            previous = folded.setdefault(part.casefold(), part)
            require(previous == part, 'Case-colliding path')


def regular_path(path):
    path = Path(path).absolute()
    require(path == path.resolve(), 'Input path traverses a symlink')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'Expected an unlinked regular input file')
    return path


def digest(path):
    result = hashlib.sha256()
    with regular_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    require(len(data) <= MAX_JSON, 'JSON exceeds bounded size')
    return json.loads(data, object_pairs_hook=pairs)


def pinned_json(path, expected):
    require(isinstance(expected, str) and SHA.fullmatch(expected), 'Invalid external JSON SHA256')
    path = regular_path(path)
    require(path.stat().st_size <= MAX_JSON, 'JSON exceeds bounded size')
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == expected, 'External JSON SHA256 mismatch')
    return strict_json(data)


def components(value):
    require(isinstance(value, dict) and set(value) == COMPONENTS
            and all(isinstance(v, str) and COMMIT.fullmatch(v) for v in value.values()),
            'Invalid exact component pins')
    text = 'open-shorebird-toolchain-v1\n' + ''.join(
        value[key] + '\n' for key in ('dart_sdk', 'flutter_source', 'updater'))
    require(hashlib.sha1(text.encode()).hexdigest() == value['engine_identity'],
            'Derived engine identity does not match component pins')
    return value


def host_provenance(data, schema='macos-app-host-provenance-v1'):
    expected = {'schema', 'components', 'producer'} | ({'files'} if schema.endswith('closure-v1') else set())
    require(isinstance(data, dict) and set(data) == expected and data['schema'] == schema,
            'Invalid exact host manifest schema')
    components(data['components'])
    p = data['producer']
    require(isinstance(p, dict) and set(p) == {'kind', 'workspace_commit', 'build_name', 'source_manifest_sha256'}
            and p['kind'] == 'local-frozen-host' and p['build_name'] == HOST_NAME
            and isinstance(p['workspace_commit'], str) and COMMIT.fullmatch(p['workspace_commit'])
            and isinstance(p['source_manifest_sha256'], str) and SHA.fullmatch(p['source_manifest_sha256']),
            'Invalid original host producer provenance')
    return data


def target_pin(data):
    require(isinstance(data, dict) and set(data) == {'schema', 'components', 'producer', 'archive_sha256'}
            and data['schema'] == 'macos-app-target-archive-v1', 'Invalid exact target pin schema')
    components(data['components'])
    p = data['producer']
    require(isinstance(p, dict) and set(p) == {'repository', 'workspace_commit', 'run_id', 'artifact_id',
                                             'artifact_name', 'metadata_origin'}
            and isinstance(p['repository'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', p['repository'])
            and isinstance(p['workspace_commit'], str) and COMMIT.fullmatch(p['workspace_commit'])
            and all(isinstance(p[k], str) and re.fullmatch(r'[1-9][0-9]{0,24}', p[k]) for k in ('run_id', 'artifact_id'))
            and p['artifact_name'] == 'macos-engine-x64'
            and p['metadata_origin'] in ('authenticated-api-export', 'producer-attestation'),
            'Invalid original target producer provenance')
    require(isinstance(data['archive_sha256'], str) and SHA.fullmatch(data['archive_sha256']),
            'Invalid external target archive SHA256')
    return data


def validate_args(data, pins, arch):
    expected = {'is_debug': 'false', 'target_os': '"mac"', 'target_cpu': f'"{arch}"',
                'dart_target_arch': f'"{arch}"', 'flutter_runtime_mode': '"release"',
                'dart_runtime_mode': '"release"', 'dart_dynamic_modules': 'false',
                'dart_enable_aot_patching': 'true', 'dart_enable_shorebird_interpreter': 'true',
                'shorebird_enable_aot_patching': 'true', 'shorebird_use_interpreter': 'false',
                'flutter_prebuilt_dart_sdk': 'false', 'dart_version': json.dumps(pins['dart_sdk']),
                'engine_version': json.dumps(pins['flutter'])}
    values = {}
    for line in data.decode('utf-8').splitlines():
        match = re.fullmatch(r'\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*(.*?)\s*(?:#.*)?', line)
        if match and match[1] in expected:
            require(match[1] not in values, 'Duplicate capability GN assignment')
            values[match[1]] = match[2].strip()
    require(values == expected, 'Original GN args do not establish pinned release capability/architecture')


def macho(data, cpus, filetype, universal=False):
    """Check actual bounded Mach-O headers/slices; never run a supplied binary."""
    require(len(data) >= 32, 'Truncated Mach-O')
    if data[:4] == b'\xcf\xfa\xed\xfe':
        _, cpu, _, kind, ncmds, size, _, _ = struct.unpack_from('<8I', data)
        require(not universal and {cpu} == cpus and kind == filetype and ncmds <= 10000
                and size <= len(data) - 32 and size >= ncmds * 8, 'Wrong Mach-O CPU/type/commands')
        pos = 32
        for _ in range(ncmds):
            require(pos + 8 <= 32 + size, 'Truncated Mach-O load command')
            _, count = struct.unpack_from('<2I', data, pos)
            require(count >= 8 and count % 4 == 0 and pos + count <= 32 + size,
                    'Invalid Mach-O load command size')
            pos += count
        require(pos == 32 + size, 'Mach-O command inventory mismatch')
        return
    require(universal and data[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'),
            'Expected Mach-O or universal host tool')
    count = struct.unpack_from('>I', data, 4)[0]
    is64 = data[:4] == b'\xca\xfe\xba\xbf'
    width = 32 if is64 else 20
    require(count == len(cpus) and 8 + width * count <= len(data), 'Wrong universal slice count')
    seen, intervals = set(), []
    for index in range(count):
        if is64:
            cpu, _, offset, size, align, reserved = struct.unpack_from('>IIQQII', data, 8 + width * index)
            require(reserved == 0, 'Invalid universal reserved field')
        else:
            cpu, _, offset, size, align = struct.unpack_from('>5I', data, 8 + width * index)
        require(cpu in cpus and cpu not in seen and align <= 30 and offset % (1 << align) == 0
                and offset >= 8 + width * count and size >= 32 and offset + size <= len(data),
                'Invalid universal slice bounds/CPU')
        require(all(offset + size <= a or offset >= b for a, b in intervals), 'Overlapping universal slices')
        macho(data[offset:offset + size], {cpu}, filetype)
        seen.add(cpu)
        intervals.append((offset, offset + size))


def bounded_bytes(path):
    path = regular_path(path)
    with host_file(path.parent, path.name, MAX_FILE) as (source, info):
        return b''.join(bounded_blocks(source, info.st_size))


@contextlib.contextmanager
def host_file(root, name, max_size=MAX_FILE):
    """Open each source ancestor without following links, including during copy."""
    relative(name)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            info = os.fstat(child)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= max_size,
                    'Host input must be a bounded unlinked regular file')
            with os.fdopen(child, 'rb', closefd=False) as stream:
                yield stream, info
        finally:
            os.close(child)
    finally:
        os.close(fd)


def bounded_blocks(stream, expected_size, completed=0):
    """Reject growth before yielding/writing; read at most one sentinel byte."""
    require(type(expected_size) is int and 0 <= expected_size <= MAX_FILE
            and 0 <= completed <= MAX_TOTAL and completed + expected_size <= MAX_TOTAL,
            'Host stream declared size exceeds file/closure bound')
    count = 0
    while True:
        block = stream.read(min(1024**2, expected_size - count + 1))
        if not block:
            require(count == expected_size, 'Host input truncated during bounded read')
            return
        count += len(block)
        require(count <= expected_size and count <= MAX_FILE and completed + count <= MAX_TOTAL,
                'Host input grew beyond bounded read size')
        yield block


def host_inventory(root):
    root = Path(root).absolute()
    require(root == root.resolve() and root.is_dir(), 'Host root must be a real directory')
    names = [*HOST_FILES, 'impellerc']
    for name in HOST_TREES:
        tree = root / name
        require(tree.is_dir() and not tree.is_symlink(), 'Missing complete host tree')
        for directory, dirs, files in os.walk(tree, followlinks=False):
            for child in dirs:
                require(not (Path(directory) / child).is_symlink(), 'Linked host directory')
            names.extend((Path(directory) / child).relative_to(root).as_posix() for child in files)
            require(len(names) <= MAX_FILES, 'Host inventory exceeds file bound')
    check_names(names)
    result, total = {}, 0
    for name in sorted(names):
        with host_file(root, name) as (stream, info):
            h = hashlib.sha256()
            for block in bounded_blocks(stream, info.st_size, total):
                h.update(block)
            result[name] = {'sha256': h.hexdigest(), 'size': info.st_size,
                            'executable': bool(info.st_mode & 0o111)}
            total += info.st_size
            require(total <= MAX_TOTAL, 'Host closure exceeds size bound')
    return result


def validate_host(root, manifest):
    host_provenance(manifest, 'macos-app-host-closure-v1')
    files = manifest['files']
    require(isinstance(files, dict) and HOST_REQUIRED.issubset(files)
            and 0 < len(files) <= MAX_FILES, 'Incomplete host closure')
    for name, entry in files.items():
        relative(name)
        require(name in {*HOST_FILES, 'impellerc'} or any(name.startswith(tree + '/') for tree in HOST_TREES),
                'Undeclared host input tree')
        require(isinstance(entry, dict) and set(entry) == {'sha256', 'size', 'executable'}
                and isinstance(entry['sha256'], str) and SHA.fullmatch(entry['sha256'])
                and type(entry['size']) is int and 0 <= entry['size'] <= MAX_FILE
                and type(entry['executable']) is bool, 'Invalid host file inventory')
    require({name.removeprefix('shader_lib/') for name in files if name.startswith('shader_lib/')} == SHADER_FILES,
            'Incomplete or unknown shader include tree')
    require(host_inventory(root) == files, 'Host closure bytes/mode/inventory differ from external pin')
    validate_args(bounded_bytes(Path(root) / 'args.gn'), manifest['components'], 'arm64')
    require(bounded_bytes(Path(root) / 'dart-sdk/revision').decode().strip() == manifest['components']['dart_sdk'],
            'SDK revision file differs from source pin')
    for name, kind in HOST_NATIVE.items():
        require(files[name]['size'] > 0 and (kind != 2 or files[name]['executable']), 'Missing host native executable')
        macho(bounded_bytes(Path(root) / name), {ARM64}, kind)
    for name in HOST_REQUIRED:
        require(files[name]['size'] > 0, 'Empty required host input')
    # Also reject an accidentally mixed Intel native helper anywhere in the SDK.
    for name in files:
        if name.startswith('dart-sdk/') and name not in HOST_NATIVE:
            with host_file(root, name) as (stream, _):
                header = stream.read(4)
            if header in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
                data = bounded_bytes(Path(root) / name)
                kind = struct.unpack_from('<I', data, 12)[0] if header == b'\xcf\xfa\xed\xfe' else 0
                require(kind in (2, 6, 8), 'Unexpected SDK Mach-O type')
                macho(data, {ARM64}, kind)


def inventory_host(root, provenance, output):
    host_provenance(provenance)
    manifest = {**provenance, 'schema': 'macos-app-host-closure-v1', 'files': host_inventory(root)}
    validate_host(root, manifest)
    output = Path(output).absolute()
    require(output.parent == output.parent.resolve(), 'Output parent must be canonical')
    with output.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return manifest


def target_members(engine):
    mirror = f'mirror/shorebird/flutter_infra_release/flutter/{engine}'
    return {'font-subset.zip', 'FlutterMacOS.framework.zip', 'flutter_patched_sdk_product.zip',
            'macos_release_x64.args.gn', 'gen_snapshot_x64', 'analyze_snapshot_x64',
            f'mirror/shorebird/{engine}/analyze-snapshot-macos-x64',
            f'{mirror}/darwin-x64/font-subset.zip',
            f'{mirror}/darwin-x64-release/FlutterMacOS.framework.zip'}


def validate_pax(payload):
    """Only inert BSD metadata; path/link/size/global/sparse overrides fail."""
    pos, seen = 0, set()
    while pos < len(payload):
        space = payload.find(b' ', pos, min(pos + 24, len(payload)))
        require(space > pos and payload[pos:space].isdigit(), 'Malformed PAX record length')
        count = int(payload[pos:space])
        require(count > space - pos + 3 and pos + count <= len(payload), 'Malformed PAX record bounds')
        record = payload[space + 1:pos + count]
        require(record.endswith(b'\n') and b'=' in record, 'Malformed PAX record')
        key, value = record[:-1].split(b'=', 1)
        require(key not in seen and len(key) <= 256, 'Duplicate or oversized PAX key')
        inert = key in (b'mtime', b'atime', b'ctime', b'SCHILY.fflags')
        inert = inert or key.startswith((b'LIBARCHIVE.xattr.', b'SCHILY.xattr.'))
        require(inert and all(32 < c < 127 for c in key), 'PAX path/size or unknown semantic override')
        if key in (b'mtime', b'atime', b'ctime'):
            require(re.fullmatch(rb'-?[0-9]{1,20}(?:\.[0-9]{1,20})?', value), 'Invalid PAX timestamp')
        seen.add(key)
        pos += count


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # BSD tar emits fractional timestamps/xattrs even for fresh compiled
        # files. Inspect bounded per-file metadata BEFORE tarfile interprets it.
        if self.type == tarfile.XHDTYPE:
            require(not getattr(archive, '_app_pax_pending', False) and 0 < self.size <= MAX_PAX,
                    'Nested or oversized PAX header')
            total = getattr(archive, '_app_pax_bytes', 0) + self.size
            require(total <= MAX_METADATA, 'PAX metadata total bound exceeded')
            archive._app_pax_bytes = total
            offset = archive.fileobj.tell()
            payload = archive.fileobj.read(self.size)
            require(len(payload) == self.size, 'Truncated PAX metadata')
            validate_pax(payload)
            archive.fileobj.seek(offset)
            archive._app_pax_pending = True
            try:
                return super()._proc_member(archive)
            finally:
                archive._app_pax_pending = False
        require(self.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                and 0 <= self.size <= MAX_FILE, 'Extended/special/oversize tar member')
        return super()._proc_member(archive)


def appledouble_pair(name):
    parent, _, basename = name.rpartition('/')
    if basename.startswith('._') and len(basename) > 2:
        return (parent + '/' if parent else '') + basename[2:]
    return None


def validate_appledouble(data):
    require(26 <= len(data) <= 1024**2, 'AppleDouble metadata size bound')
    magic, version = struct.unpack_from('>2I', data)
    count = struct.unpack_from('>H', data, 24)[0]
    require(magic == 0x00051607 and version == 0x00020000 and 0 < count <= 64
            and 26 + 12 * count <= len(data), 'Invalid AppleDouble header')
    ids, regions = set(), []
    for index in range(count):
        kind, offset, size = struct.unpack_from('>3I', data, 26 + index * 12)
        require(kind in (2, 9) and kind not in ids and offset >= 26 + 12 * count
                and offset + size <= len(data), 'Invalid AppleDouble metadata entry')
        require(all(offset + size <= a or offset >= b for a, b in regions), 'Overlapping AppleDouble entries')
        ids.add(kind)
        regions.append((offset, offset + size))


def extract_target_tar(path, pin, destination):
    path = regular_path(path)
    require(path.stat().st_size <= MAX_ARCHIVE, 'Target archive exceeds size bound')
    # Hash the independent owned copy that will actually be parsed; do not
    # verify a mutable source path and reopen it as a different archive.
    snapshot, h, count = destination / '.pinned-target.tar', hashlib.sha256(), 0
    with host_file(path.parent, path.name, MAX_ARCHIVE) as (source, _), snapshot.open('xb') as out:
        for block in iter(lambda: source.read(1024**2), b''):
            count += len(block)
            require(count <= MAX_ARCHIVE, 'Target archive exceeds size bound')
            out.write(block)
            h.update(block)
    require(h.hexdigest() == pin['archive_sha256'], 'External target archive SHA256 mismatch')
    expected = {'macos-engine/' + n for n in target_members(pin['components']['engine_identity']) | {'manifest.json'}}
    allowed_dirs = {str(p) for name in expected for p in PurePosixPath(name).parents if str(p) != '.'}
    seen, files, total, metadata_total = set(), set(), 0, 0
    with tarfile.open(snapshot, 'r:*', tarinfo=BoundedTarInfo) as archive:
        for count, entry in enumerate(archive):
            require(count < MAX_FILES, 'Tar entry bound exceeded')
            name = entry.name.rstrip('/') if entry.isdir() else entry.name
            relative(name)
            require(name not in seen, 'Duplicate tar entry')
            seen.add(name)
            require(not entry.sparse, 'Sparse tar input is not allowed')
            require(entry.isdir() or entry.isfile(), 'Tar links and special files are not allowed')
            pair = appledouble_pair(name)
            if pair is not None:
                require(entry.isfile() and pair in expected | allowed_dirs and entry.size <= 1024**2,
                        'Unpaired or oversized AppleDouble sidecar')
                metadata_total += entry.size
                require(metadata_total <= MAX_METADATA, 'AppleDouble metadata total bound exceeded')
                with archive.extractfile(entry) as source:
                    validate_appledouble(source.read())
                # This is archive metadata, never a restored input or output.
                continue
            require(name in (allowed_dirs if entry.isdir() else expected), 'Unknown target tar entry')
            if entry.isdir():
                continue
            require(entry.size <= MAX_FILE, 'Tar file exceeds bound')
            total += entry.size
            require(total <= MAX_TOTAL, 'Tar total exceeds bound')
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as source, target.open('xb') as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o755 if entry.mode & 0o111 else 0o644)
            files.add(name)
    check_names(seen)
    require(files == expected, 'Target archive is incomplete')
    data = strict_json((destination / 'macos-engine/manifest.json').read_bytes())
    wanted = {'name', 'git_sha', 'runner_os', 'engine_revision', 'macos_build_dir', 'runtime_mode',
              'target_os', 'target_arch', 'dart_dynamic_modules', 'dart_enable_aot_patching',
              'dart_enable_shorebird_interpreter', 'shorebird_enable_aot_patching', 'shorebird_interpreter', 'included'}
    require(isinstance(data, dict) and set(data) == wanted and data['name'] == 'macos-engine-x64'
            and data['git_sha'] == pin['producer']['workspace_commit'] and data['runner_os'] == 'macOS'
            and data['engine_revision'] == pin['components']['engine_identity']
            and data['macos_build_dir'] == 'flutter/engine/src/out/macos_release_x64'
            and data['runtime_mode'] == 'release' and data['target_os'] == 'macos' and data['target_arch'] == 'x64'
            and data['dart_dynamic_modules'] is False and data['dart_enable_aot_patching'] is True
            and data['dart_enable_shorebird_interpreter'] is True and data['shorebird_enable_aot_patching'] is True
            and data['shorebird_interpreter'] is False
            and isinstance(data['included'], list) and all(isinstance(n, str) for n in data['included'])
            and len(data['included']) == len(set(data['included']))
            and set(data['included']) == target_members(pin['components']['engine_identity']),
            'Target archive manifest disagrees with authenticated producer/component/profile pins')
    base = destination / 'macos-engine'
    for source in ('font-subset.zip', 'FlutterMacOS.framework.zip', 'analyze_snapshot_x64'):
        mirror_name = next(n for n in target_members(pin['components']['engine_identity'])
                           if n.startswith('mirror/') and n.endswith(
                               'analyze-snapshot-macos-x64' if source == 'analyze_snapshot_x64' else source))
        require(digest(base / source) == digest(base / mirror_name), 'Mirror bytes differ from original target input')
    return data


def extract_zip(path, destination, *, framework=False):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) <= MAX_FILES, 'ZIP entry bound exceeded')
        entries, links, total = {}, {}, 0
        for info in infos:
            name = info.filename[:-1] if info.is_dir() else info.filename
            relative(name)
            require(name not in entries and name not in links, 'Duplicate ZIP entry')
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(not info.flag_bits & 1 and kind in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK),
                    'Encrypted or special ZIP member')
            require(info.file_size <= MAX_FILE, 'ZIP file bound exceeded')
            total += info.file_size
            require(total <= MAX_TOTAL, 'ZIP total bound exceeded')
            if framework:
                require(name == FRAMEWORK_LICENSE or name == FRAMEWORK or name.startswith(FRAMEWORK + '/'),
                        'ZIP entry outside framework')
            else:
                require(name in ('flutter_patched_sdk_product', 'flutter_patched_sdk_product/platform_strong.dill',
                                 'flutter_patched_sdk_product/vm_outline_strong.dill', PLATFORM_LICENSE),
                        'Unknown PRODUCT ZIP entry')
            if kind == stat.S_IFLNK:
                require(framework and name in FRAMEWORK_LINKS and info.file_size <= 256,
                        'Undeclared framework link')
                target = archive.read(info).decode('ascii')
                require(target == FRAMEWORK_LINKS[name], 'Unsafe framework link target')
                links[name] = target
            else:
                require((info.is_dir() and kind in (0, stat.S_IFDIR)) or
                        (not info.is_dir() and kind in (0, stat.S_IFREG)), 'ZIP type/directory disagreement')
                entries[name] = info
        check_names([*entries, *links])
        leaves = set(links) | {name for name, info in entries.items() if not info.is_dir()}
        require(not any(str(p) in leaves for n in leaves for p in PurePosixPath(n).parents),
                'ZIP file/link aliases a parent')
        if framework:
            require(set(links) == set(FRAMEWORK_LINKS) and (FRAMEWORK_REQUIRED | {FRAMEWORK_LICENSE}).issubset(leaves),
                    'Incomplete framework inventory/aliases')
        else:
            require(leaves == {'flutter_patched_sdk_product/platform_strong.dill',
                               'flutter_patched_sdk_product/vm_outline_strong.dill', PLATFORM_LICENSE},
                    'Incomplete PRODUCT platform')
        for name, info in sorted(entries.items()):
            out = destination / name
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, out.open('xb') as target:
                shutil.copyfileobj(source, target)
            out.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
        for name, target in sorted(links.items()):
            out = destination / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.symlink_to(target)
        for name in links:
            require((destination / name).resolve(strict=True).is_relative_to(destination.resolve()),
                    'Dangling or escaping framework link')
    return leaves


def copy_host(root, manifest, destination):
    total = 0
    for name, expected in sorted(manifest['files'].items()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        h, count = hashlib.sha256(), 0
        with host_file(root, name) as (source, info):
            require(info.st_size == expected['size'], 'Host input size changed before verified copy')
            # Reserve the declared aggregate before opening a destination file.
            require(total + expected['size'] <= MAX_TOTAL, 'Host copy exceeds closure size bound')
            with target.open('xb') as out:
                for block in bounded_blocks(source, expected['size'], total):
                    out.write(block)
                    h.update(block)
                    count += len(block)
        require(h.hexdigest() == expected['sha256'] and count == expected['size'],
                'Host input changed during verified copy')
        target.chmod(0o755 if expected['executable'] else 0o644)
        total += count


def output_inventory(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            result[p.relative_to(root).as_posix()] = {'symlink': os.readlink(p)}
        elif p.is_file():
            result[p.relative_to(root).as_posix()] = {'sha256': digest(p), 'size': p.stat().st_size,
                                                    'executable': bool(p.stat().st_mode & 0o111)}
    return result


def materialize(host, host_manifest, target_archive, pin, destination, *, metadata_pins=None):
    host = Path(host).absolute()
    target_archive = regular_path(target_archive)
    target_pin(pin)
    validate_host(host, host_manifest)
    require(host_manifest['components'] == pin['components'], 'Host/target component identity differs')
    destination = Path(destination).absolute()
    require(destination == destination.resolve() and destination.parent.is_dir()
            and not destination.exists() and not destination.is_symlink()
            and not host.is_relative_to(destination) and not destination.is_relative_to(host),
            'Materialization requires a fresh canonical destination outside host inputs')
    destination.mkdir(mode=0o700)
    identity = destination.stat()
    try:
        stage = destination / '.verified-target'
        stage.mkdir()
        archive_manifest = extract_target_tar(target_archive, pin, stage)
        original = stage / 'macos-engine'
        args = bounded_bytes(original / 'macos_release_x64.args.gn')
        validate_args(args, pin['components'], 'x64')
        for name in ('gen_snapshot_x64', 'analyze_snapshot_x64'):
            require((original / name).stat().st_mode & 0o111, 'Target tool is not executable')
            macho(bounded_bytes(original / name), {ARM64, X64}, 2, universal=True)
        target = destination / 'out' / TARGET_NAME
        target.mkdir(parents=True)
        wrapper = target / 'FlutterMacOS.xcframework'
        extract_zip(original / 'FlutterMacOS.framework.zip', wrapper / 'macos-x86_64', framework=True)
        image = wrapper / 'macos-x86_64' / FRAMEWORK / 'Versions/A/FlutterMacOS'
        macho(bounded_bytes(image), {X64}, 6)
        framework_info = plistlib.loads((image.parent / 'Resources/Info.plist').read_bytes())
        require(isinstance(framework_info, dict) and framework_info.get('CFBundleExecutable') == 'FlutterMacOS'
                and framework_info.get('CFBundlePackageType') == 'FMWK', 'Wrong framework Info.plist')
        for name in FRAMEWORK_REQUIRED:
            require((wrapper / 'macos-x86_64' / name).stat().st_size > 0, 'Empty required framework input')
        plist = {'CFBundlePackageType': 'XFWK', 'XCFrameworkFormatVersion': '1.0', 'AvailableLibraries': [{
            'LibraryIdentifier': 'macos-x86_64', 'LibraryPath': FRAMEWORK,
            'SupportedArchitectures': ['x86_64'], 'SupportedPlatform': 'macos'}]}
        (wrapper / 'Info.plist').write_bytes(plistlib.dumps(plist, sort_keys=True))
        extract_zip(original / 'flutter_patched_sdk_product.zip', target)
        shutil.copytree(target / 'flutter_patched_sdk_product', target / 'flutter_patched_sdk')
        for p in (target / 'flutter_patched_sdk').iterdir():
            require(p.stat().st_size > 0, 'Empty PRODUCT platform input')
        for name in ('gen_snapshot_x64', 'analyze_snapshot_x64'):
            shutil.copyfile(original / name, target / name)
            (target / name).chmod(0o755)
        (target / 'args.gn').write_bytes(args)
        copy_host(host, host_manifest, destination / 'out' / HOST_NAME)
        shutil.rmtree(stage)
        receipt = {'schema': 'macos-app-materialized-build-inputs-v1',
                   'status': 'materialized_unexecuted', 'runtime_acceptance': False,
                   'components': pin['components'],
                   'metadata': {'external_sha256': metadata_pins or {},
                                'host_closure_canonical_sha256': hashlib.sha256(json.dumps(
                                    host_manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                                'target_pin_canonical_sha256': hashlib.sha256(json.dumps(
                                    pin, sort_keys=True, separators=(',', ':')).encode()).hexdigest()},
                   'target': {'platform': 'macos', 'arch': 'x64', 'machine': 'x86_64',
                              'out': 'out/' + TARGET_NAME, 'producer': pin['producer'],
                              'archive_sha256': pin['archive_sha256'], 'archive_manifest': archive_manifest,
                              'original_args_sha256': hashlib.sha256(args).hexdigest()},
                   'host': {'platform': 'macos', 'arch': 'arm64', 'machine': 'arm64',
                            'out': 'out/' + HOST_NAME, 'producer': host_manifest['producer'],
                            'original_args_sha256': host_manifest['files']['args.gn']['sha256'],
                            'closure_files': host_manifest['files']},
                   'required_private_flutter_cache_bindings': [
                       {'source': f'out/{HOST_NAME}/dart-sdk', 'destination': 'bin/cache/dart-sdk',
                        'rule': 'complete exact inventoried tree; every private Flutter copy'},
                       {'source': f'out/{HOST_NAME}/gen/dart-pkg/sky_engine',
                        'destination': 'bin/cache/pkg/sky_engine',
                        'rule': 'complete exact inventoried tree; every private Flutter copy'}],
                   'pending': ['actual compiler versions and frontend/bytecode capability probes',
                               'private Flutter bootstrap SDK and sky_engine cache bindings',
                               'independently pinned material fonts, pub packages, CLI/server and Xcode inputs',
                               'real app build, signing and native Intel lifecycle'],
                   'files': output_inventory(destination)}
        (destination / 'build-inputs.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        return receipt
    except BaseException:
        current = destination.lstat()
        if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            shutil.rmtree(destination)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    inventory = commands.add_parser('inventory-host')
    inventory.add_argument('--host', type=Path, required=True)
    inventory.add_argument('--provenance', type=Path, required=True)
    inventory.add_argument('--provenance-sha256', required=True)
    inventory.add_argument('--output', type=Path, required=True)
    build = commands.add_parser('materialize')
    for name in ('host', 'host-manifest', 'target-archive', 'target-pin', 'destination'):
        build.add_argument('--' + name, type=Path, required=True)
    build.add_argument('--host-manifest-sha256', required=True)
    build.add_argument('--target-pin-sha256', required=True)
    args = parser.parse_args(argv)
    if args.command == 'inventory-host':
        inventory_host(args.host, pinned_json(args.provenance, args.provenance_sha256), args.output)
    else:
        materialize(args.host, pinned_json(args.host_manifest, args.host_manifest_sha256),
                    args.target_archive, pinned_json(args.target_pin, args.target_pin_sha256), args.destination,
                    metadata_pins={'host_manifest': args.host_manifest_sha256, 'target_pin': args.target_pin_sha256})
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(f'Build input materialization failed: {error}') from error

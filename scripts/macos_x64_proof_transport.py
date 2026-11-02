#!/usr/bin/env python3
"""Pinned draft-asset transport for prepared native Intel SDK FFI proof."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

SUITES = {'callouts': 'test_aot_product_ffi.py',
          'callbacks': 'test_aot_product_ffi_callbacks.py',
          'full': 'test_aot_full_replacement_ffi.py'}
HARNESSES = {'callouts': 'product_ffi_test', 'callbacks': 'product_ffi_callback_test',
             'full': 'product_full_ffi_test'}
RUNTIME = {
    'callouts': ['libfixture.dylib', 'body.snapshot', 'structural.snapshot',
                 'ordinary-assets.snapshot', 'full.snapshot', 'full-from-dill.snapshot'],
    'callbacks': ['libcallback_fixture.dylib', 'baseline.snapshot', 'compiled_controls.snapshot',
                  'async_group.snapshot', 'shared-baseline.snapshot', 'async_shared.snapshot'],
    'full': ['libreleased_full_ffi.dylib', 'native-source.snapshot', 'native-from-dill.snapshot'],
}
CASE_COUNTS = {'callouts': 8, 'callbacks': 19, 'full': 66}
# v1 omitted the build-root launcher's adjacent dartvm. It must remain bound to
# its original source-pinned tooling; never silently upgrade its provenance.
BUILD_FORMAT_VERSION = 2
BUILD_FILES = {'args.gn', 'dart', 'dartvm', 'gen_snapshot_product', 'vm_platform_product.dill', *HARNESSES.values()}
SDK_REQUIRED = {'dart-sdk/bin/dart', 'dart-sdk/bin/dartvm', 'dart-sdk/bin/dartaotruntime',
                'dart-sdk/bin/snapshots/gen_kernel_aot.dart.snapshot',
                'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot',
                'dart-sdk/lib/_internal/vm_platform_strong.dill'}
BUILD_NATIVE = {'dart', 'dartvm', 'gen_snapshot_product', *HARNESSES.values(),
                *(SDK_REQUIRED - {'dart-sdk/lib/_internal/vm_platform_strong.dill'})}
BUILD_EXECUTABLE = BUILD_NATIVE - {name for name in BUILD_NATIVE if name.endswith('.snapshot')}
ROOT_PATTERN = re.compile(r'/private/tmp/shorebird-macos-x64-proof-[A-Za-z0-9_-]{8,80}')
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
CERT = re.compile(r'[0-9A-F]{40}')
ASSET_NAME = 'macos-x64-prepared-proof.zip'
MAX_ARCHIVE = 2 * 1024**3
MAX_TOTAL = 6 * 1024**3
MAX_FILES = 30000


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def relative(value):
    if (not isinstance(value, str) or not value or not value.isascii() or '\\' in value or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or
            str(PurePosixPath(value)) != value or PurePosixPath(value).is_absolute() or
            any(part in ('.', '..') or ':' in part for part in value.split('/'))):
        raise ValueError('Unsafe relative path')
    return value


def canonical_root(value):
    if not isinstance(value, str) or not ROOT_PATTERN.fullmatch(value):
        raise ValueError('Invalid canonical proof root')
    return Path(value)


def x64_macho(header):
    if len(header) < 12 or header[:4] != b'\xcf\xfa\xed\xfe' or struct.unpack('<I', header[4:8])[0] != 0x1000007:
        raise ValueError('Expected a thin x86_64 Mach-O')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def source_identity(workspace, sdk_commit):
    if not COMMIT.fullmatch(sdk_commit):
        raise ValueError('Invalid SDK commit pin')
    workspace = Path(workspace).resolve()
    head = git(workspace, 'rev-parse', 'HEAD')
    tree = git(workspace, 'ls-tree', 'HEAD', 'dart-sdk').split()
    if len(tree) != 4 or tree[:3] != ['160000', 'commit', sdk_commit]:
        raise ValueError('Workspace SDK gitlink does not match expected source commit')
    if git(workspace / 'dart-sdk', 'rev-parse', 'HEAD') != sdk_commit:
        raise ValueError('Checked out SDK does not match expected source commit')
    subprocess.run(['git', '-C', str(workspace / 'dart-sdk'), 'diff', '--quiet', 'HEAD'], check=True)
    for driver in (*SUITES.values(), 'native_ffi_test_support.py', 'native_ffi_prepared_cases.py',
                   'native_ffi_prepared_manifest.schema.json'):
        if not (workspace / 'dart-sdk/tools' / driver).is_file():
            raise ValueError('Pinned SDK does not support prepared native Intel proof')
        subprocess.run(['git', '-C', str(workspace / 'dart-sdk'), 'ls-files', '--error-unmatch', 'tools/' + driver],
                       check=True, stdout=subprocess.DEVNULL)
    return head


def files_under(directory, *, allow_hardlinks=False):
    found = {}
    for path in sorted(Path(directory).rglob('*')):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not allow_hardlinks):
            raise ValueError('Only unlinked regular proof files are permitted')
        name = relative(path.relative_to(directory).as_posix())
        found[name] = digest(path)
    return found


def write_zip(output, files):
    output = Path(output)
    with output.open('xb') as target, zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            relative(name)
            archive.write(path, name)
    Path(str(output) + '.sha256').write_text(digest(output) + '  ' + output.name + '\n')


def package_build(workspace, build, output):
    require_native_intel()
    workspace, build = Path(workspace).resolve(), Path(build).resolve()
    sdk_commit = git(workspace / 'dart-sdk', 'rev-parse', 'HEAD')
    workspace_commit = source_identity(workspace, sdk_commit)
    if not (build / 'dart-sdk/bin/dart').is_file():
        raise ValueError('Complete matching SDK/frontend tools are required')
    selected = {name: build / name for name in BUILD_FILES}
    # GN's create_sdk intentionally hardlinks some build outputs. Read/hash them
    # without mutation; ZIP extraction materializes independent private copies.
    for name in files_under(build / 'dart-sdk', allow_hardlinks=True):
        selected['dart-sdk/' + name] = build / 'dart-sdk' / name
    if not SDK_REQUIRED.issubset(selected):
        raise ValueError('Complete matching SDK/frontend tools are required')
    for name in sorted(BUILD_NATIVE):
        if not selected[name].is_file() or not stat.S_ISREG(selected[name].lstat().st_mode):
            raise ValueError('Missing regular native compiler input: ' + name)
        if name in BUILD_EXECUTABLE and not selected[name].stat().st_mode & 0o111:
            raise ValueError('Native compiler input is not executable: ' + name)
        with selected[name].open('rb') as source:
            x64_macho(source.read(12))
    args = (build / 'args.gn').read_text()
    for key, expected in {'target_cpu': '"x64"', 'dart_dynamic_modules': 'false',
                          'dart_enable_aot_patching': 'true', 'dart_enable_shorebird_interpreter': 'true'}.items():
        if not re.search(r'^\s*' + key + r'\s*=\s*' + re.escape(expected) + r'\s*$', args, re.M):
            raise ValueError('Missing required proof GN argument: ' + key)
    manifest = {'format_version': BUILD_FORMAT_VERSION, 'kind': 'unsigned-native-intel-build-inputs',
                'status': 'build_inputs_only', 'workspace_commit': workspace_commit,
                'sdk_source_commit': sdk_commit, 'target': {'os': 'macos', 'arch': 'x64'},
                'files': {name: digest(path) for name, path in sorted(selected.items())}}
    output = Path(output)
    manifest_path = output.with_suffix('.manifest.json')
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    write_zip(output, {'build-inputs/' + name: path for name, path in selected.items()} |
              {'build-inputs/build-inputs-manifest.json': manifest_path})


def validate_build_manifest(data, workspace_commit, sdk_commit):
    if isinstance(data, dict) and data.get('format_version') == 1:
        raise ValueError('Legacy unsigned build format 1 lacks the required root dartvm closure; '
                         'use its exact original source-pinned tooling, not an upgraded manifest')
    if (not isinstance(data, dict) or set(data) != {'format_version', 'kind', 'status', 'workspace_commit',
            'sdk_source_commit', 'target', 'files'} or type(data['format_version']) is not int or
            (data['format_version'], data['kind'], data['status'], data['workspace_commit'],
             data['sdk_source_commit'], data['target']) !=
            (BUILD_FORMAT_VERSION, 'unsigned-native-intel-build-inputs', 'build_inputs_only', workspace_commit,
             sdk_commit, {'os': 'macos', 'arch': 'x64'})):
        raise ValueError('Unsigned build source/target identity mismatch')
    hashes = data['files']
    if not isinstance(hashes, dict) or not (BUILD_FILES | SDK_REQUIRED).issubset(hashes):
        raise ValueError('Incomplete unsigned build inventory')
    for name, value in hashes.items():
        relative(name)
        if name not in BUILD_FILES and not name.startswith('dart-sdk/'):
            raise ValueError('Unallowlisted unsigned compiler input')
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError('Invalid unsigned compiler hash')
    return hashes


def inspect_zip(archive):
    entries, folded, total = {}, set(), 0
    for info in archive.infolist():
        name = relative(info.filename)
        mode = info.external_attr >> 16
        if (name in entries or info.is_dir() or info.extra or info.flag_bits & 1 or
                (stat.S_IFMT(mode) not in (0, stat.S_IFREG))):
            raise ValueError('Archive contains duplicate, encrypted or nonregular entries')
        # Folded paths cannot alias on the standard case-insensitive macOS volume.
        if name.casefold() in folded:
            raise ValueError('Archive contains aliased paths')
        folded.add(name.casefold())
        total += info.file_size
        if info.file_size > 1024**3 or total > MAX_TOTAL or len(entries) >= MAX_FILES:
            raise ValueError('Proof archive exceeds bounded inventory size')
        entries[name] = info
    for name in entries:
        if any(str(parent).casefold() in folded for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('Archive file aliases a parent directory')
    return entries


def zip_json(archive, name):
    if archive.getinfo(name).file_size > 4 * 1024**2:
        raise ValueError('Manifest exceeds bounded size')
    return strict_json(archive.read(name))


def zip_hash(archive, name):
    result = hashlib.sha256()
    with archive.open(name) as source:
        for block in iter(lambda: source.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def verify_archive_hash(path, expected):
    if not SHA.fullmatch(expected) or Path(path).stat().st_size > MAX_ARCHIVE or digest(path) != expected:
        raise ValueError('Externally pinned archive SHA-256 mismatch')


def extract_build(path, expected_sha, workspace, sdk_commit, destination):
    workspace_commit = source_identity(workspace, sdk_commit)
    verify_archive_hash(path, expected_sha)
    destination = Path(destination).absolute()
    if destination != destination.resolve() or destination.exists():
        raise ValueError('Unsigned build extraction requires a fresh private directory')
    with zipfile.ZipFile(path) as archive:
        entries = inspect_zip(archive)
        data = zip_json(archive, 'build-inputs/build-inputs-manifest.json')
        hashes = validate_build_manifest(data, workspace_commit, sdk_commit)
        if set(entries) != {'build-inputs/' + name for name in hashes} | {'build-inputs/build-inputs-manifest.json'}:
            raise ValueError('Undeclared unsigned build archive input')
        for name, value in hashes.items():
            if zip_hash(archive, 'build-inputs/' + name) != value:
                raise ValueError('Unsigned compiler input hash mismatch')
        for name in sorted(BUILD_NATIVE):
            info = entries['build-inputs/' + name]
            if name in BUILD_EXECUTABLE and not (info.external_attr >> 16) & 0o111:
                raise ValueError('Native compiler input is not executable: ' + name)
            with archive.open(info) as source:
                x64_macho(source.read(12))
        destination.mkdir(mode=0o700)
        for entry in archive.infolist():
            output = destination / entry.filename[len('build-inputs/'):]
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(entry) as source, output.open('xb') as target:
                shutil.copyfileobj(source, target)
            output.chmod(0o700 if (entry.external_attr >> 16) & 0o111 else 0o600)
    return data


def validate_prepared_zip(path, expected_sha, workspace_commit, sdk_commit, certificate):
    verify_archive_hash(path, expected_sha)
    if not COMMIT.fullmatch(workspace_commit) or not COMMIT.fullmatch(sdk_commit) or not CERT.fullmatch(certificate):
        raise ValueError('Invalid external source/certificate pin')
    with zipfile.ZipFile(path) as archive:
        entries = inspect_zip(archive)
        roots = {name.split('/')[2] for name in entries if len(name.split('/')) >= 4 and name.startswith('private/tmp/')}
        if len(roots) != 1:
            raise ValueError('Archive requires exactly one canonical proof prefix')
        root = canonical_root('/private/tmp/' + roots.pop())
        prefix = str(root).lstrip('/') + '/'
        if not entries or any(not name.startswith(prefix) for name in entries):
            raise ValueError('Archive file outside canonical proof prefix')
        manifest = zip_json(archive, prefix + 'transport-manifest.json')
        keys = {'format_version', 'kind', 'status', 'proof_root', 'workspace_commit', 'sdk_source_commit',
                'certificate_sha1', 'build_manifest_sha256', 'suites', 'files'}
        if (not isinstance(manifest, dict) or set(manifest) != keys or type(manifest['format_version']) is not int or
                (manifest['format_version'], manifest['kind'], manifest['status'], manifest['proof_root'],
                 manifest['workspace_commit'], manifest['sdk_source_commit'], manifest['certificate_sha1']) !=
                (1, 'native-intel-prepared-proof', 'prepared_runtime_pending', str(root), workspace_commit,
                 sdk_commit, certificate)):
            raise ValueError('Prepared transport source/certificate/target identity mismatch')
        build = zip_json(archive, prefix + 'build-inputs-manifest.json')
        build_hashes = validate_build_manifest(build, workspace_commit, sdk_commit)
        if zip_hash(archive, prefix + 'build-inputs-manifest.json') != manifest['build_manifest_sha256']:
            raise ValueError('Unsigned build manifest hash mismatch')
        if not isinstance(manifest['suites'], dict) or set(manifest['suites']) != set(SUITES):
            raise ValueError('All three prepared suites are required')
        expected_files = {'build-inputs-manifest.json'}
        for suite in SUITES:
            manifest_name = suite + '/prepared-manifest.json'
            if zip_hash(archive, prefix + manifest_name) != manifest['suites'][suite]:
                raise ValueError('Prepared suite manifest hash mismatch')
            data = zip_json(archive, prefix + manifest_name)
            if (data.get('suite'), data.get('status'), data.get('target'), data.get('canonical_root'),
                    data.get('proof_root'), data.get('sdk_source_commit')) != (
                    suite, 'prepared_runtime_pending', {'os': 'macos', 'arch': 'x64', 'machine': 'x86_64'},
                    str(root / suite), str(root), sdk_commit):
                raise ValueError('Prepared suite source/target/root mismatch')
            inputs = data.get('inputs')
            if not isinstance(inputs, dict) or not inputs:
                raise ValueError('Missing prepared immutable inventory')
            for name, value in inputs.items():
                relative(name)
                if not isinstance(value, str) or not SHA.fullmatch(value) or zip_hash(archive, prefix + suite + '/' + name) != value:
                    raise ValueError('Prepared immutable input hash mismatch')
                expected_files.add(suite + '/' + name)
            expected_files.add(manifest_name)
            originals = data.get('original_artifacts')
            if not isinstance(originals, dict) or set(originals) != {'dart', 'platform', 'gen_snapshot', 'harness'}:
                raise ValueError('Missing unsigned provenance roles')
            roles = {'dart': 'dart', 'platform': 'vm_platform_product.dill',
                     'gen_snapshot': 'gen_snapshot_product', 'harness': HARNESSES[suite]}
            for role, build_name in roles.items():
                name = 'original-inputs/' + role
                if originals[role] != {'path': name, 'sha256': build_hashes[build_name],
                                       'usage': 'provenance-only-never-executed'} or inputs.get(name) != build_hashes[build_name]:
                    raise ValueError('Original compiler provenance differs from CI build')
            signed = data.get('signed_runtime_inputs')
            signed_paths = {'strict-signing/' + HARNESSES[suite], *RUNTIME[suite]}
            if not isinstance(signed, list) or len(signed) != len(signed_paths) or {item.get('path') for item in signed} != signed_paths:
                raise ValueError('Signed execution inputs must be separate from compiler provenance')
            for item in signed:
                if (item.get('certificate_sha1') != certificate or item.get('sha256') != inputs.get(item['path']) or
                        item.get('machine') != 'x86_64' or item.get('hardened_runtime') is not True or item.get('entitlements') != {}):
                    raise ValueError('Signed runtime certificate/machine/entitlement mismatch')
                with archive.open(prefix + suite + '/' + item['path']) as binary:
                    x64_macho(binary.read(12))
            cases = data.get('test_cases')
            if (not isinstance(cases, list) or len(cases) != CASE_COUNTS[suite] or
                    len({case.get('id') for case in cases}) != len(cases) or
                    any(not isinstance(case.get('argv'), list) or not case['argv'] or
                        case['argv'][0] != str(root / suite / 'strict-signing' / HARNESSES[suite]) for case in cases)):
                raise ValueError('Incomplete cases or non-harness execution target')
        files = manifest['files']
        if not isinstance(files, dict) or set(files) != expected_files or set(entries) != {prefix + name for name in files} | {prefix + 'transport-manifest.json'}:
            raise ValueError('Undeclared or missing proof archive files')
        for name, value in files.items():
            relative(name)
            if not isinstance(value, str) or not SHA.fullmatch(value) or zip_hash(archive, prefix + name) != value:
                raise ValueError('Transport immutable file hash mismatch')
    return root, manifest


def package_prepared(root, build_manifest, workspace, sdk_commit, certificate, output):
    root = canonical_root(str(Path(root).absolute()))
    if root.is_symlink() or root.resolve() != root or set(path.name for path in root.iterdir()) != set(SUITES):
        raise ValueError('Only the three fresh prepared suite trees may be packaged')
    workspace_commit = source_identity(workspace, sdk_commit)
    build = strict_json(Path(build_manifest).read_text())
    validate_build_manifest(build, workspace_commit, sdk_commit)
    destination = root / 'build-inputs-manifest.json'
    destination.write_text(json.dumps(build, indent=2) + '\n')
    manifest = {'format_version': 1, 'kind': 'native-intel-prepared-proof', 'status': 'prepared_runtime_pending',
                'proof_root': str(root), 'workspace_commit': workspace_commit, 'sdk_source_commit': sdk_commit,
                'certificate_sha1': certificate, 'build_manifest_sha256': digest(destination),
                'suites': {suite: digest(root / suite / 'prepared-manifest.json') for suite in SUITES},
                'files': files_under(root)}
    metadata = root / 'transport-manifest.json'
    metadata.write_text(json.dumps(manifest, indent=2) + '\n')
    write_zip(output, {str(root).lstrip('/') + '/' + name: root / name for name in files_under(root)})
    validate_prepared_zip(output, digest(output), workspace_commit, sdk_commit, certificate)


def validate_release(data, release_id, asset_id, expected_sha):
    if (type(data.get('id')) is not int or data['id'] != release_id or data.get('draft') is not True or
            'published_at' not in data or data['published_at'] is not None):
        raise ValueError('Only the exact authenticated unpublished draft release is permitted')
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise ValueError('Missing release asset inventory')
    matches = [asset for asset in assets if type(asset.get('id')) is int and asset['id'] == asset_id]
    if len(matches) != 1:
        raise ValueError('Expected asset ID is not uniquely attached to the draft release')
    asset = matches[0]
    if (asset.get('name') != ASSET_NAME or asset.get('state') != 'uploaded' or
            asset.get('content_type') != 'application/zip' or type(asset.get('size')) is not int or
            not 0 < asset['size'] <= MAX_ARCHIVE or
            asset.get('digest') not in (None, 'sha256:' + expected_sha)):
        raise ValueError('Draft asset metadata/hash does not match the external pin')
    return asset


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https' or target.username or target.password or not (
                target.hostname == 'api.github.com' or (target.hostname or '').endswith('.githubusercontent.com')):
            raise ValueError('Untrusted asset download redirect')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if target.hostname != 'api.github.com':
            result.remove_header('Authorization')
        return result


def _download(repo, release_id, asset_id, expected_sha, output, evidence):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not SHA.fullmatch(expected_sha):
        raise ValueError('Invalid repository/hash pin')
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('Authenticated draft download requires GITHUB_TOKEN')
    opener = urllib.request.build_opener(SafeRedirect())
    base = 'https://api.github.com/repos/' + repo
    headers = {'Authorization': 'Bearer ' + token, 'X-GitHub-Api-Version': '2022-11-28',
               'Accept': 'application/vnd.github+json', 'User-Agent': 'shorebird-native-intel-proof'}
    with opener.open(urllib.request.Request(base + '/releases/' + str(release_id), headers=headers), timeout=60) as response:
        data = strict_json(response.read(8 * 1024**2))
    asset = validate_release(data, release_id, asset_id, expected_sha)
    headers['Accept'] = 'application/octet-stream'
    size = 0
    with opener.open(urllib.request.Request(base + '/releases/assets/' + str(asset_id), headers=headers), timeout=60) as response, Path(output).open('xb') as target:
        while block := response.read(1024**2):
            size += len(block)
            if size > asset['size']:
                raise ValueError('Downloaded asset exceeds its pinned metadata size')
            target.write(block)
    if size != asset['size']:
        raise ValueError('Downloaded asset is incomplete')
    verify_archive_hash(output, expected_sha)
    # Recheck mutable release membership/publication state after the transfer.
    headers['Accept'] = 'application/vnd.github+json'
    with opener.open(urllib.request.Request(base + '/releases/' + str(release_id), headers=headers), timeout=60) as response:
        validate_release(strict_json(response.read(8 * 1024**2)), release_id, asset_id, expected_sha)
    Path(evidence).mkdir(parents=True, exist_ok=True)
    (Path(evidence) / 'download.json').write_text(json.dumps(
        {'release_id': release_id, 'asset_id': asset_id, 'draft': True, 'published_at': None,
         'archive_sha256': expected_sha, 'size': size}, indent=2) + '\n')


def download(repo, release_id, asset_id, expected_sha, output, evidence):
    Path(evidence).mkdir(parents=True, exist_ok=True)
    report = {'status': 'download_failed', 'release_id': release_id, 'asset_id': asset_id}
    try:
        if release_id <= 0 or asset_id <= 0:
            raise ValueError('Release and asset IDs must be positive')
        _download(repo, release_id, asset_id, expected_sha, output, evidence)
        report['status'] = 'download_verified_runtime_pending'
    except BaseException as error:
        report['error_type'] = type(error).__name__
        raise
    finally:
        (Path(evidence) / 'download-status.json').write_text(json.dumps(report, indent=2) + '\n')


def _sysctl_flag(name):
    # -i hides an absent OID as silent success. Only the exact documented
    # unknown-OID result is admissible when Intel macOS omits these flags.
    probe = subprocess.run(['/usr/sbin/sysctl', '-n', name], capture_output=True,
                           text=True, env=credential_free_environment(), timeout=60)
    if probe.returncode == 0 and not probe.stderr.strip() and probe.stdout.strip() in ('0', '1'):
        return probe.stdout.strip() == '1'
    if (probe.returncode == 1 and not probe.stdout.strip() and
            re.fullmatch(r"sysctl: unknown oid '" + re.escape(name) + "'",
                         probe.stderr.strip(), flags=re.IGNORECASE)):
        return None
    raise ValueError('Cannot establish native Intel host: sysctl probe failed for ' + name)


def require_native_intel():
    if sys.platform != 'darwin' or platform.machine() != 'x86_64':
        raise ValueError('Native proof requires a native Intel Mac')
    translated = _sysctl_flag('sysctl.proc_translated')
    hardware_arm64 = _sysctl_flag('hw.optional.arm64')
    if translated or hardware_arm64:
        raise ValueError('Rosetta cannot establish native Intel runtime proof')


def extract_prepared(path, expected_sha, workspace_commit, sdk_commit, certificate, evidence):
    root, manifest = validate_prepared_zip(path, expected_sha, workspace_commit, sdk_commit, certificate)
    # Exclusive creation owns exactly this canonical root. Never merge/reuse inputs.
    root.mkdir(mode=0o700)
    info = root.lstat()
    ownership = {'root': str(root), 'device': info.st_dev, 'inode': info.st_ino}
    Path(evidence).mkdir(parents=True, exist_ok=True)
    (Path(evidence) / 'owned-root.json').write_text(json.dumps(ownership) + '\n')
    prefix = str(root).lstrip('/') + '/'
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            destination = root / entry.filename[len(prefix):]
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open('xb') as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o700 if (entry.external_attr >> 16) & 0o111 else 0o600)
    return root, manifest


def credential_free_environment():
    return {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC'}


def run_proof(args):
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'status': 'native_execution_failed', 'suites': {}}
    try:
        require_native_intel()
        workspace_commit = source_identity(args.workspace, args.sdk_commit)
        root, manifest = extract_prepared(args.archive, args.sha256, workspace_commit,
                                           args.sdk_commit, args.certificate_sha1, evidence)
        transport_hash = digest(root / 'transport-manifest.json')
        for suite, driver in SUITES.items():
            command = [sys.executable, str(Path(args.workspace).resolve() / 'dart-sdk/tools' / driver),
                       '--run-prepared=' + str(root / suite / 'prepared-manifest.json')]
            with (evidence / (suite + '-runner.log')).open('w') as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                           env=credential_free_environment(), start_new_session=True)
                try:
                    code = process.wait(timeout=3600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    code = -1
            results = root / (suite + '-results')
            if results.is_dir():
                files_under(results)  # Reject result links before evidence copying.
                shutil.copytree(results, evidence / suite)
            result = strict_json((results / 'native-results.json').read_text()) if (results / 'native-results.json').is_file() else {}
            prepared = strict_json((root / suite / 'prepared-manifest.json').read_text())
            cases = result.get('cases', [])
            valid = (code == 0 and result.get('status') == 'native_execution_passed' and
                     result.get('suite') == suite and result.get('immutable_inputs_unchanged') is True and
                     len(cases) == CASE_COUNTS[suite] and all(case.get('status') == 'passed' for case in cases) and
                     [case.get('id') for case in cases] == [case['id'] for case in prepared['test_cases']])
            report['suites'][suite] = {'status': 'passed' if valid else 'failed', 'returncode': code}
        if any(item['status'] != 'passed' for item in report['suites'].values()):
            raise ValueError('At least one mandatory native Intel suite failed')
        observed = {name: value for name, value in files_under(root).items()
                    if name.split('/')[0] not in {suite + '-results' for suite in SUITES}}
        if observed != dict(manifest['files'], **{'transport-manifest.json': transport_hash}):
            raise ValueError('Prepared transport inputs changed during native execution')
        source_identity(args.workspace, args.sdk_commit)
        report.update(status='native_execution_passed', archive_sha256=args.sha256,
                      workspace_commit=workspace_commit, sdk_source_commit=args.sdk_commit,
                      certificate_sha1=args.certificate_sha1, proof_root=str(root))
    except BaseException as error:
        report['error_type'] = type(error).__name__
        raise
    finally:
        (evidence / 'transport-results.json').write_text(json.dumps(report, indent=2) + '\n')


def cleanup(evidence):
    state = Path(evidence) / 'owned-root.json'
    if not state.is_file():
        return
    data = strict_json(state.read_text())
    root = canonical_root(data['root'])
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (data['device'], data['inode']):
        raise ValueError('Owned canonical root identity changed; refusing cleanup')
    shutil.rmtree(root)
    (Path(evidence) / 'cleanup.json').write_text(json.dumps({'root': str(root), 'removed': True}) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    verify = sub.add_parser('verify-source')
    verify.add_argument('--workspace', type=Path, required=True)
    verify.add_argument('--sdk-commit', required=True)
    verify.add_argument('--evidence', type=Path)
    build = sub.add_parser('package-build')
    build.add_argument('--workspace', type=Path, required=True)
    build.add_argument('--build-dir', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    extract = sub.add_parser('extract-build')
    extract.add_argument('--archive', type=Path, required=True)
    extract.add_argument('--sha256', required=True)
    extract.add_argument('--workspace', type=Path, required=True)
    extract.add_argument('--sdk-commit', required=True)
    extract.add_argument('--destination', type=Path, required=True)
    package = sub.add_parser('package-prepared')
    package.add_argument('--proof-root', type=Path, required=True)
    package.add_argument('--build-manifest', type=Path, required=True)
    package.add_argument('--output', type=Path, required=True)
    for command in (package, sub.add_parser('run')):
        command.add_argument('--workspace', type=Path, required=True)
        command.add_argument('--sdk-commit', required=True)
        command.add_argument('--certificate-sha1', required=True)
    fetch = sub.add_parser('download')
    fetch.add_argument('--repo', required=True)
    fetch.add_argument('--release-id', type=int, required=True)
    fetch.add_argument('--asset-id', type=int, required=True)
    fetch.add_argument('--output', type=Path, required=True)
    for command in (fetch, sub.choices['run']):
        command.add_argument('--sha256', required=True)
        command.add_argument('--evidence', type=Path, required=True)
    sub.choices['run'].add_argument('--archive', type=Path, required=True)
    sub.add_parser('cleanup').add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'verify-source':
        report = {'status': 'source_verification_failed'}
        try:
            require_native_intel()
            report['workspace_commit'] = source_identity(args.workspace, args.sdk_commit)
            report.update(status='source_verified_runtime_pending', sdk_source_commit=args.sdk_commit)
        except BaseException as error:
            report['error_type'] = type(error).__name__
            raise
        finally:
            if args.evidence:
                args.evidence.mkdir(parents=True, exist_ok=True)
                (args.evidence / 'source-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    elif args.command == 'package-build':
        package_build(args.workspace, args.build_dir, args.output)
    elif args.command == 'extract-build':
        extract_build(args.archive, args.sha256, args.workspace, args.sdk_commit, args.destination)
    elif args.command == 'package-prepared':
        package_prepared(args.proof_root, args.build_manifest, args.workspace, args.sdk_commit,
                         args.certificate_sha1, args.output)
    elif args.command == 'download':
        download(args.repo, args.release_id, args.asset_id, args.sha256, args.output, args.evidence)
    elif args.command == 'run':
        run_proof(args)
    else:
        cleanup(args.evidence)


if __name__ == '__main__':
    main()

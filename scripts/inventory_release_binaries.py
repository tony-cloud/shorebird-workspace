#!/usr/bin/env python3
"""Inventory native binaries in CI release artifact directories.

Walks each input directory (and, recursively, the .tar/.tar.gz/.tgz/.zip/.aar
archives inside it) without extracting anything to disk except a bounded spool
file for very large members. For every ELF, Mach-O (thin or fat), and PE file
it records:

  * platform and architecture (from the binary headers; the container path
    disambiguates Android from Linux ELF when no Android note is present);
  * size and sha256;
  * exported dynamic symbols: the total count, counts per project category
    (shorebird updater API, Dart embedding API, Flutter embedder API, JNI,
    BoringSSL/OpenSSL-like), the names in every non-"other" category, and a
    digest of the complete sorted export list;
  * role (runtime library shipped in apps, host tool, or debug symbols);
  * the plan section 7.1 origin classification of the artifact route that
    carries it (see ``classify_route``).

Output is canonical JSON (sorted keys, sorted records, no timestamps), so the
same inputs always produce identical bytes. This is P0 evidence for
docs/HARDENING_AND_UPSTREAM_DEPENDENCY_PLAN.md; see docs/HARDENING_CONTRACTS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import mmap
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tarfile
import tempfile
from typing import Callable, Iterable, Iterator
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from write_artifact_manifest import DEFAULT_ARTIFACT_OVERRIDES  # noqa: E402

FORMAT_VERSION = 1
SPOOL_LIMIT = 64 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 5
ARCHIVE_SUFFIXES = ('.tar.gz', '.tgz', '.tar', '.zip', '.aar', '.jar')

# Symbol categories, matched against the export name with one Mach-O leading
# underscore removed. Order matters: the first matching category wins.
SYMBOL_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('shorebird', re.compile(r'^shorebird_')),
    ('dart_api', re.compile(r'^(Dart_|kDart)')),
    ('flutter_embedder', re.compile(
        r'^(Flutter[A-Z_]|FlutterEngine|FlutterDesktop|fl_|InternalFlutter|'
        r'OBJC_[A-Z_]+_\$_Flutter)')),
    ('jni', re.compile(r'^(JNI_On(Load|Unload)|Java_)')),
    ('boringssl', re.compile(
        r'^(SSL_|EVP_|CRYPTO_|BIO_|X509|RSA_|EC_|ECDSA_|BN_|OPENSSL_|ERR_|'
        r'PEM_|HMAC|SHA(1|224|256|384|512)_|AES_|RAND_|d2i_|i2d_|ASN1_|bssl)')),
    # Rust v0 (_R) and legacy (_ZN...17h<hash>E) mangling: updater internals.
    ('rust_internal', re.compile(r'^(_R[A-Z]|_ZN.*17h[0-9a-f]{16}E$|rust_)')),
    ('cxx_mangled', re.compile(r'^_Z')),
    ('objc', re.compile(r'^(OBJC_|\.objc_)')),
    ('unwind', re.compile(r'^(_?_?[Uu]nw|_Unwind_|__(de)?register_frame)')),
)

# Categories whose names are listed in full; the rest are counted only (the
# Rust/C++ internals run to thousands of names per engine).
LISTED_CATEGORIES = {'shorebird', 'dart_api', 'jni', 'boringssl', 'objc'}

# Basenames of engine libraries that ship inside applications.
RUNTIME_BASENAMES = {
    'libflutter.so',
    'libflutter_linux_gtk.so',
    'flutter_windows.dll',
    'Flutter',
    'FlutterMacOS',
    'libflutter_engine.so',
    'libflutter_engine.dylib',
    'flutter_engine.dll',
}

ELF_MACHINES = {3: 'x86', 40: 'arm', 62: 'x64', 183: 'arm64', 243: 'riscv64'}
MACHO_CPUS = {7: 'x86', 0x01000007: 'x64', 12: 'arm', 0x0100000C: 'arm64',
              0x0200000C: 'arm64_32'}
MACHO_PLATFORMS = {1: 'macos', 2: 'ios', 3: 'tvos', 4: 'watchos', 6: 'maccatalyst',
                   7: 'ios-simulator', 8: 'tvos-simulator', 9: 'watchos-simulator'}
PE_MACHINES = {0x14C: 'x86', 0x8664: 'x64', 0xAA64: 'arm64', 0x1C4: 'arm'}

# Route rule mirrored from shorebird/packages/artifact_proxy/lib/src/
# artifact_proxy.dart (_requiresCustomSdkArtifact). test_inventory_release_
# binaries.py asserts the Dart source still carries these exact fragments.
REQUIRES_CUSTOM_ROUTE = (
    re.compile(r'/dart-sdk-[^/]+\.zip$'),
    re.compile(r'/font-subset\.zip$'),
    re.compile(r'/flutter_patched_sdk_product\.zip$'),
    re.compile(r'/(?:android-[^/]+|ios|darwin(?:-[^/]+)?|linux-[^/]+)-release/'),
    re.compile(r'/windows-[^/]+/windows-[^/]+-flutter\.zip$'),
    re.compile(r'/io/flutter/[^/]+_release/'),
)
ENGINE_ROUTE = re.compile(
    r'(?:flutter_infra_release/flutter|download\.flutter\.io/io/flutter/[^/]+)/'
    r'(?:1\.0\.0-)?([0-9a-f]{40})(/|-)')


class FormatError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Binary parsing


def _cstr(data, offset: int, limit: int | None = None) -> str:
    end = data.find(b'\0', offset, limit if limit is not None else len(data))
    if end < 0:
        raise FormatError('unterminated string')
    return bytes(data[offset:end]).decode('utf-8', 'replace')


def detect_format(data) -> str | None:
    head = bytes(data[:8])
    if head[:4] == b'\x7fELF':
        return 'elf'
    if head[:4] in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):
        return 'macho'
    if head[:4] in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf') and _plausible_fat(data):
        return 'macho-fat'
    if head[:2] == b'MZ' and len(data) >= 0x40:
        pe_offset = struct.unpack_from('<I', data, 0x3C)[0]
        if pe_offset + 4 <= len(data) and bytes(data[pe_offset:pe_offset + 4]) == b'PE\0\0':
            return 'pe'
    return None


def _plausible_fat(data) -> bool:
    # 0xCAFEBABE is shared with Java class files; require valid slice records.
    if len(data) < 8:
        return False
    is64 = bytes(data[:4]) == b'\xca\xfe\xba\xbf'
    count = struct.unpack_from('>I', data, 4)[0]
    entry = 32 if is64 else 20
    if not 0 < count <= 16 or 8 + count * entry > len(data):
        return False
    for index in range(count):
        cputype = struct.unpack_from('>i', data, 8 + index * entry)[0] & 0xFFFFFFFF
        if is64:
            offset, size = struct.unpack_from('>QQ', data, 8 + index * entry + 8)
        else:
            offset, size = struct.unpack_from('>II', data, 8 + index * entry + 8)
        if cputype not in MACHO_CPUS or offset + size > len(data):
            return False
    return True


def parse_elf(data) -> dict:
    if data[5] != 1:
        raise FormatError('only little-endian ELF is supported')
    elf64 = data[4] == 2
    machine = struct.unpack_from('<H', data, 18)[0]
    if elf64:
        shoff = struct.unpack_from('<Q', data, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x3A)
        section_format, symbol_size = '<IIQQQQIIQQ', 24
    else:
        shoff = struct.unpack_from('<I', data, 0x20)[0]
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x2E)
        section_format, symbol_size = '<IIIIIIIIII', 16
    if shoff == 0 or shnum == 0 or shoff + shnum * shentsize > len(data):
        raise FormatError('ELF section headers are missing')
    sections = []
    for index in range(shnum):
        name, kind, _flags, _addr, offset, size, link, _info, _align, entsize = struct.unpack_from(
            section_format, data, shoff + index * shentsize)
        sections.append((name, kind, offset, size, link, entsize))
    shstr = sections[shstrndx]
    names = [_cstr(data, shstr[2] + section[0], shstr[2] + shstr[3]) for section in sections]
    android = '.note.android.ident' in names
    exports: list[str] = []
    for section in sections:
        if section[1] != 11:  # SHT_DYNSYM
            continue
        strtab = sections[section[4]]
        entsize = section[5] or symbol_size
        for offset in range(section[2] + entsize, section[2] + section[3], entsize):
            if elf64:
                st_name, st_info, st_other, st_shndx = struct.unpack_from('<IBBH', data, offset)
            else:
                st_name, _value, _size, st_info, st_other, st_shndx = struct.unpack_from(
                    '<IIIBBH', data, offset)
            binding = st_info >> 4
            visibility = st_other & 3
            if st_shndx == 0 or binding not in (1, 2, 10) or visibility not in (0, 3):
                continue
            exports.append(_cstr(data, strtab[2] + st_name, strtab[2] + strtab[3]))
    return {
        'format': 'elf',
        'arch': ELF_MACHINES.get(machine, f'elf-machine-{machine}'),
        'os': 'android' if android else None,
        'elf_debug_info': '.debug_info' in names,
        'exports': exports,
    }


def _read_uleb(data, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, offset
        if shift > 63:
            raise FormatError('ULEB128 overflow')


def _walk_export_trie(data, start: int, size: int) -> list[str]:
    names: list[str] = []
    end = start + size
    pending = [(0, '')]
    visited = set()
    while pending:
        node, prefix = pending.pop()
        if node in visited or start + node >= end:
            raise FormatError('malformed export trie')
        visited.add(node)
        cursor = start + node
        terminal_size, cursor = _read_uleb(data, cursor)
        if terminal_size:
            names.append(prefix)
        cursor += terminal_size
        children = data[cursor]
        cursor += 1
        for _ in range(children):
            label = _cstr(data, cursor, end)
            cursor += len(label.encode('utf-8')) + 1
            child, cursor = _read_uleb(data, cursor)
            pending.append((child, prefix + label))
    return names


def parse_macho(data) -> dict:
    magic = bytes(data[:4])
    if magic != b'\xcf\xfa\xed\xfe':
        raise FormatError('only 64-bit little-endian Mach-O is supported')
    cputype, _subtype, filetype, ncmds, _sizeofcmds = struct.unpack_from('<iiIII', data, 4)
    cursor = 32
    platform = None
    trie = None
    symtab = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from('<II', data, cursor)
        if cmd in (0x22, 0x80000022):  # LC_DYLD_INFO(_ONLY)
            export_off, export_size = struct.unpack_from('<II', data, cursor + 40)
            if export_size:
                trie = (export_off, export_size)
        elif cmd == 0x80000033:  # LC_DYLD_EXPORTS_TRIE
            trie = struct.unpack_from('<II', data, cursor + 8)
        elif cmd == 0x2:  # LC_SYMTAB
            symtab = struct.unpack_from('<IIII', data, cursor + 8)
        elif cmd == 0x32:  # LC_BUILD_VERSION
            platform = struct.unpack_from('<I', data, cursor + 8)[0]
        elif cmd in (0x24, 0x25) and platform is None:  # LC_VERSION_MIN_MACOSX/IPHONEOS
            platform = 1 if cmd == 0x24 else 2
        if cmdsize < 8:
            raise FormatError('malformed load command')
        cursor += cmdsize
    if trie is not None:
        exports = _walk_export_trie(data, *trie)
    elif symtab is not None:
        symoff, nsyms, stroff, strsize = symtab
        exports = []
        for index in range(nsyms):
            n_strx, n_type, _sect, _desc, _value = struct.unpack_from('<IBBhQ', data, symoff + index * 16)
            if n_type & 0xE0 or not n_type & 0x01 or n_type & 0x0E == 0:
                continue  # stab, non-external, or undefined
            exports.append(_cstr(data, stroff + n_strx, stroff + strsize))
    else:
        exports = []
    return {
        'format': 'macho',
        'arch': MACHO_CPUS.get(cputype & 0xFFFFFFFF, f'macho-cpu-{cputype}'),
        'os': MACHO_PLATFORMS.get(platform) if platform is not None else None,
        'macho_filetype': filetype,
        'exports': exports,
    }


def macho_slices(data) -> list[tuple[int, int]]:
    is64 = bytes(data[:4]) == b'\xca\xfe\xba\xbf'
    count = struct.unpack_from('>I', data, 4)[0]
    entry = 32 if is64 else 20
    slices = []
    for index in range(count):
        if is64:
            offset, size = struct.unpack_from('>QQ', data, 8 + index * entry + 8)
        else:
            offset, size = struct.unpack_from('>II', data, 8 + index * entry + 8)
        slices.append((offset, size))
    return slices


def parse_pe(data) -> dict:
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    machine, nsections, _ts, _ptr, _nsyms, opt_size, characteristics = struct.unpack_from(
        '<HHIIIHH', data, pe + 4)
    opt = pe + 24
    magic = struct.unpack_from('<H', data, opt)[0]
    datadir = opt + (112 if magic == 0x20B else 96)
    export_rva, export_size = struct.unpack_from('<II', data, datadir)
    section_table = opt + opt_size
    sections = []
    for index in range(nsections):
        base = section_table + index * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from('<IIII', data, base + 8)
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def rva_to_offset(rva: int) -> int:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return rva - vaddr + rawptr
        raise FormatError(f'RVA {rva:#x} is outside all sections')

    exports: list[str] = []
    ordinal_only = 0
    if export_rva and export_size:
        directory = rva_to_offset(export_rva)
        functions, names, _functions_rva, names_rva = struct.unpack_from('<IIII', data, directory + 20)
        names_offset = rva_to_offset(names_rva) if names else 0
        for index in range(names):
            name_rva = struct.unpack_from('<I', data, names_offset + index * 4)[0]
            exports.append(_cstr(data, rva_to_offset(name_rva)))
        ordinal_only = max(functions - names, 0)
    return {
        'format': 'pe',
        'arch': PE_MACHINES.get(machine, f'pe-machine-{machine:#x}'),
        'os': 'windows',
        'pe_is_dll': bool(characteristics & 0x2000),
        'pe_ordinal_only_exports': ordinal_only,
        'exports': exports,
    }


def summarize_exports(exports: Iterable[str], strip_underscore: bool) -> dict:
    names = sorted(set(exports))
    categories: dict[str, list[str]] = {name: [] for name, _ in SYMBOL_CATEGORIES}
    other = 0
    for name in names:
        key = name[1:] if strip_underscore and name.startswith('_') else name
        for category, pattern in SYMBOL_CATEGORIES:
            if pattern.search(key):
                categories[category].append(name)
                break
        else:
            other += 1
    digest = hashlib.sha256('\n'.join(names).encode('utf-8')).hexdigest()
    return {
        'export_count': len(names),
        'export_list_sha256': digest,
        'export_counts': {**{name: len(values) for name, values in categories.items()}, 'other': other},
        'exports_by_category': {name: values for name, values in categories.items()
                                if values and name in LISTED_CATEGORIES},
    }


# ---------------------------------------------------------------------------
# Classification


def route_path(container: list[str]) -> str | None:
    """Return the mirror route ($engine-normalized) for a container chain."""
    for part in reversed(container):
        match = ENGINE_ROUTE.search(part)
        if match:
            start = part.index('flutter_infra_release') if 'flutter_infra_release' in part else part.index('download.flutter.io')
            return part[start:].replace(match.group(1), '$engine')
    return None


def classify_route(route: str | None) -> dict:
    """Plan section 7.1 classification for the route that carries a binary.

    Every binary inventoried from this workspace's CI artifacts is built by the
    fork toolchain, so its origin is ``custom-toolchain``. The route rule then
    says whether an official Flutter artifact may ever stand in for it:
    ``forbidden`` when the artifact proxy rejects stock substitution, and
    ``allowed-if-unchanged`` when the proxy would fall back to the official
    Flutter origin for a non-overridden route.
    """
    if route is None:
        return {'origin': 'custom-toolchain', 'route': None, 'published_override': False,
                'official_substitution': 'not-routed'}
    published = route in DEFAULT_ARTIFACT_OVERRIDES
    slash_route = '/' + route
    requires_custom = any(pattern.search(slash_route) for pattern in REQUIRES_CUSTOM_ROUTE)
    return {
        'origin': 'custom-toolchain',
        'route': route,
        'published_override': published,
        'official_substitution': 'forbidden' if requires_custom else 'allowed-if-unchanged',
    }


def classify_role(container: list[str], basename: str) -> str:
    joined = '/'.join(container).lower()
    if 'symbols' in joined or '.dsym' in joined or basename.endswith(('.debug', '.sym')):
        return 'debug-symbols'
    if basename in RUNTIME_BASENAMES:
        return 'runtime'
    return 'host-tool'


ROLE_PRIORITY = ('runtime', 'host-tool', 'debug-symbols')


def infer_os(container: list[str], parsed_os: str | None, fmt: str) -> str:
    """Headers decide: Mach-O build version, PE, or the NDK's Android ELF note.

    An ELF without .note.android.ident is a Linux binary, including host
    gen_snapshot builds shipped inside android-*-release archives.
    """
    del container
    if parsed_os:
        return parsed_os
    return 'linux' if fmt == 'elf' else 'unknown'


# ---------------------------------------------------------------------------
# Walking


class Blob:
    """A member payload held in memory or spooled to a scratch file."""

    def __init__(self, reader: Callable[[], io.BufferedIOBase], size: int, scratch: Path):
        self.size = size
        self._spool = None
        self._map = None
        stream = reader()
        if size > SPOOL_LIMIT:
            self._spool = tempfile.TemporaryFile(dir=scratch)
            while chunk := stream.read(1 << 20):
                self._spool.write(chunk)
            self._spool.flush()
            self._map = mmap.mmap(self._spool.fileno(), 0, access=mmap.ACCESS_READ)
            self.data = self._map
        else:
            self.data = stream.read()

    @classmethod
    def from_path(cls, path: Path) -> 'Blob':
        blob = cls.__new__(cls)
        blob.size = path.stat().st_size
        blob._spool = path.open('rb')
        blob._map = mmap.mmap(blob._spool.fileno(), 0, access=mmap.ACCESS_READ) if blob.size else None
        blob.data = blob._map if blob._map is not None else b''
        return blob

    def fileobj(self):
        if self._spool is not None:
            self._spool.seek(0)
            return self._spool
        return io.BytesIO(self.data)

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
        if self._spool is not None:
            self._spool.close()


def _is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def iter_tar(blob: Blob, scratch: Path) -> Iterator[tuple[str, Blob]]:
    with tarfile.open(fileobj=blob.fileobj(), mode='r:*') as archive:
        for member in archive:
            if not member.isreg():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            yield member.name, Blob(lambda: extracted, member.size, scratch)


def iter_zip(blob: Blob, scratch: Path) -> Iterator[tuple[str, Blob]]:
    with zipfile.ZipFile(blob.fileobj()) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            mode = info.external_attr >> 16
            if info.is_dir() or (mode and not stat.S_ISREG(mode)):
                continue  # directories and symlinks (framework Versions/Current)
            yield info.filename, Blob(lambda: archive.open(info), info.file_size, scratch)


def inspect_blob(container: list[str], blob: Blob, scratch: Path, depth: int,
                 records: list[dict], errors: list[dict]) -> None:
    name = container[-1]
    if _is_archive(name):
        if depth >= MAX_ARCHIVE_DEPTH:
            errors.append({'path': container, 'error': 'archive nesting too deep'})
            return
        lowered = name.lower()
        iterator = iter_zip if lowered.endswith(('.zip', '.aar', '.jar')) else iter_tar
        try:
            for member_name, member in iterator(blob, scratch):
                try:
                    inspect_blob(container + [member_name], member, scratch, depth + 1, records, errors)
                finally:
                    member.close()
        except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as error:
            errors.append({'path': container, 'error': f'unreadable archive: {error}'})
        return
    fmt = detect_format(blob.data)
    if fmt is None:
        return
    sha256 = hashlib.sha256()
    for start in range(0, blob.size, 1 << 22):
        sha256.update(blob.data[start:start + (1 << 22)])
    basename = PurePosixPath(name).name
    base = {
        'container': container,
        'basename': basename,
        'size': blob.size,
        'sha256': sha256.hexdigest(),
        **classify_route(route_path(container)),
    }
    try:
        if fmt == 'macho-fat':
            parsed_slices = []
            for offset, size in macho_slices(blob.data):
                parsed_slices.append((parse_macho(blob.data[offset:offset + size]), size))
        elif fmt == 'macho':
            parsed_slices = [(parse_macho(blob.data), blob.size)]
        elif fmt == 'elf':
            parsed_slices = [(parse_elf(blob.data), blob.size)]
        else:
            parsed_slices = [(parse_pe(blob.data), blob.size)]
    except (FormatError, struct.error, IndexError) as error:
        errors.append({'path': container, 'error': f'{fmt}: {error}'})
        return
    for parsed, slice_size in parsed_slices:
        exports = parsed.pop('exports')
        record = {
            **base,
            **parsed,
            'role': classify_role(container, basename),
            'fat': fmt == 'macho-fat',
            'slice_size': slice_size,
            'os': infer_os(container, parsed.get('os'), parsed['format']),
            **summarize_exports(exports, strip_underscore=parsed['format'] == 'macho'),
        }
        records.append(record)


def inventory(inputs: list[Path], scratch: Path) -> dict:
    records: list[dict] = []
    errors: list[dict] = []
    for root in inputs:
        files = sorted(path for path in root.rglob('*') if path.is_file() and not path.is_symlink())
        for path in files:
            relative = f'{root.name}/{path.relative_to(root).as_posix()}'
            blob = Blob.from_path(path)
            try:
                inspect_blob([relative], blob, scratch, 0, records, errors)
            finally:
                blob.close()
    records.sort(key=lambda record: (record['container'], record['arch']))
    unique: dict[tuple[str, str], dict] = {}
    for record in records:
        key = (record['sha256'], record['arch'])
        entry = unique.setdefault(key, {
            'sha256': record['sha256'],
            'arch': record['arch'],
            'os': record['os'],
            'format': record['format'],
            'basename': record['basename'],
            'roles': set(),
            'debug_info': record.get('elf_debug_info', False),
            'size': record['size'],
            'slice_size': record['slice_size'],
            'export_count': record['export_count'],
            'export_counts': record['export_counts'],
            'origin': record['origin'],
            'official_substitution': set(),
            'routes': set(),
            'occurrences': 0,
        })
        entry['occurrences'] += 1
        entry['roles'].add(record['role'])
        entry['official_substitution'].add(record['official_substitution'])
        if record['route']:
            entry['routes'].add(record['route'])
    binaries = []
    for entry in unique.values():
        # One digest can be staged as a shipped library and as a symbols copy;
        # the most consequential role names the binary.
        entry['role'] = next(role for role in ROLE_PRIORITY if role in entry['roles'])
        entry['roles'] = sorted(entry['roles'])
        if entry['routes']:
            entry['official_substitution'].discard('not-routed')
        entry['official_substitution'] = sorted(entry['official_substitution'])
        entry['routes'] = sorted(entry['routes'])
        binaries.append(entry)
    binaries.sort(key=lambda entry: (entry['role'], entry['os'], entry['arch'], entry['basename'], entry['sha256']))
    return {
        'format_version': FORMAT_VERSION,
        'tool': 'scripts/inventory_release_binaries.py',
        'inputs': sorted(path.name for path in inputs),
        'record_count': len(records),
        'unique_binary_count': len(binaries),
        'unique_binaries': binaries,
        'records': records,
        'errors': sorted(errors, key=lambda error: error['path']),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('inputs', nargs='+', type=Path, help='artifact directories')
    parser.add_argument('--output', type=Path, help='write JSON here instead of stdout')
    parser.add_argument('--scratch', type=Path, help='directory for spooling large members '
                        '(default: the output directory, else the current directory)')
    parser.add_argument('--source', action='append', default=[], metavar='KEY=VALUE',
                        help='provenance recorded verbatim in the output, e.g. ci_run=42464951579')
    args = parser.parse_args(argv)
    for path in args.inputs:
        if not path.is_dir():
            parser.error(f'not a directory: {path}')
    scratch = args.scratch or (args.output.parent if args.output else Path.cwd())
    scratch.mkdir(parents=True, exist_ok=True)
    result = inventory([path.resolve() for path in args.inputs], scratch)
    source = {}
    for item in args.source:
        key, separator, value = item.partition('=')
        if not separator or not key:
            parser.error(f'--source expects KEY=VALUE: {item}')
        source[key] = value
    result['source'] = dict(sorted(source.items()))
    text = json.dumps(result, indent=1, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Tests for inventory_release_binaries.py using synthetic ELF/Mach-O/PE files."""

from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory_release_binaries as inv  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = '44a553c3b3c7797c7f0f20961d46a8b17e06444c'


def strtab(names: list[str]) -> tuple[bytes, list[int]]:
    data = b'\0'
    offsets = []
    for name in names:
        offsets.append(len(data))
        data += name.encode() + b'\0'
    return data, offsets


def build_elf(exports: list[str], *, hidden: list[str] = (), undefined: list[str] = (),
              elf64: bool = True, machine: int = 183, android: bool = False, debug: bool = False) -> bytes:
    """Minimal little-endian ELF with .dynsym/.dynstr/.shstrtab sections."""
    names = list(exports) + list(hidden) + list(undefined)
    dynstr, offsets = strtab(names)
    sym_size = 24 if elf64 else 16
    symbols = [b'\0' * sym_size]
    for index, offset in enumerate(offsets):
        name = names[index]
        shndx = 0 if name in undefined else 7
        other = 2 if name in hidden else 0  # STV_HIDDEN
        info = (1 << 4) | 2  # GLOBAL FUNC
        if elf64:
            symbols.append(struct.pack('<IBBHQQ', offset, info, other, shndx, 0x1000, 4))
        else:
            symbols.append(struct.pack('<IIIBBH', offset, 0x1000, 4, info, other, shndx))
    dynsym = b''.join(symbols)
    extra = (['.note.android.ident'] if android else []) + (['.debug_info'] if debug else [])
    section_names = ['', '.dynsym', '.dynstr', '.shstrtab'] + extra
    shstrtab, name_offsets = strtab(section_names[1:])
    name_offsets = [0] + name_offsets
    header_size = 64 if elf64 else 52
    payloads = [b'', dynsym, dynstr, shstrtab] + [b'\0' * 8 for _ in extra]
    offsets = []
    cursor = header_size
    body = b''
    for payload in payloads:
        offsets.append(cursor if payload else 0)
        body += payload
        cursor += len(payload)
    shoff = cursor
    kinds = [0, 11, 3, 3] + [7 if name.startswith('.note') else 1 for name in extra]
    links = [0, 2, 0, 0] + [0 for _ in extra]
    entsizes = [0, sym_size, 0, 0] + [0 for _ in extra]
    sections = b''
    for index, payload in enumerate(payloads):
        fields = (name_offsets[index], kinds[index], 0, 0, offsets[index], len(payload),
                  links[index], 0, 0, entsizes[index])
        sections += struct.pack('<IIQQQQIIQQ' if elf64 else '<IIIIIIIIII', *fields)
    ident = b'\x7fELF' + bytes([2 if elf64 else 1, 1, 1]) + b'\0' * 9
    if elf64:
        header = ident + struct.pack('<HHIQQQIHHHHHH', 3, machine, 1, 0, 0, shoff, 0,
                                     64, 56, 0, 64, len(payloads), 3)
    else:
        header = ident + struct.pack('<HHIIIIIHHHHHH', 3, machine, 1, 0, 0, shoff, 0,
                                     52, 32, 0, 40, len(payloads), 3)
    return header + body + sections


def uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def build_trie(names: list[str]) -> bytes:
    # Root has one edge per name; each child is terminal with no children.
    # Child offsets are fixed-width (2-byte ULEB) so the layout is computable.
    root_edges = []
    children = b''
    child_nodes = []
    for _ in names:
        child_nodes.append(b'\x02\x00\x00\x00')  # terminal size 2 (flags, addr), 0 children
    root_size = 1 + 1 + sum(len(name.encode()) + 1 + 2 for name in names)
    cursor = root_size
    for name, node in zip(names, child_nodes):
        root_edges.append(name.encode() + b'\0' + bytes([0x80 | (cursor & 0x7F), cursor >> 7]))
        children += node
        cursor += len(node)
    return b'\x00' + bytes([len(names)]) + b''.join(root_edges) + children


def build_macho(exports: list[str], *, cpu: int = 0x0100000C, platform: int = 1,
                use_trie: bool = True, undefined: list[str] = ()) -> bytes:
    commands = []
    # LC_BUILD_VERSION: cmd, cmdsize, platform, minos, sdk, ntools
    commands.append(struct.pack('<IIIIII', 0x32, 24, platform, 0, 0, 0))
    linkedit_start = 4096
    if use_trie:
        trie = build_trie(exports)
        commands.append(struct.pack('<IIII', 0x80000033, 16, linkedit_start, len(trie)))
        linkedit = trie
    else:
        names = list(exports) + list(undefined)
        table, offsets = strtab(names)
        symbols = b''
        for name, offset in zip(names, offsets):
            n_type = 0x01 if name in undefined else 0x0F  # N_EXT | (N_UNDF or N_SECT)
            symbols += struct.pack('<IBBhQ', offset, n_type, 0 if name in undefined else 1, 0, 0x1000)
        commands.append(struct.pack('<IIIIII', 0x2, 24, linkedit_start, len(names),
                                    linkedit_start + len(symbols), len(table)))
        linkedit = symbols + table
    load = b''.join(commands)
    header = struct.pack('<IiiIIIII', 0xFEEDFACF, cpu, 0, 6, len(commands), len(load), 0, 0)
    body = header + load
    return body + b'\0' * (linkedit_start - len(body)) + linkedit


def build_fat(slices: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack('>II', 0xCAFEBABE, len(slices))
    cursor = 4096
    entries = b''
    payload = b''
    for cpu, data in slices:
        entries += struct.pack('>iiIII', cpu, 0, cursor, len(data), 12)
        payload += data + b'\0' * ((-len(data)) % 4096)
        cursor += len(data) + ((-len(data)) % 4096)
    head = header + entries
    return head + b'\0' * (4096 - len(head)) + payload


def build_pe(exports: list[str], *, machine: int = 0x8664, ordinal_only: int = 0) -> bytes:
    section_rva, section_raw = 0x1000, 0x200
    names_blob = b''
    name_rvas = []
    directory_size = 40
    functions = len(exports) + ordinal_only
    eat = directory_size
    npt = eat + 4 * functions
    ordinals = npt + 4 * len(exports)
    strings = ordinals + 2 * len(exports)
    for name in exports:
        name_rvas.append(section_rva + strings + len(names_blob))
        names_blob += name.encode() + b'\0'
    directory = struct.pack('<IIHHIIIIIII', 0, 0, 0, 0, 0, 1, functions, len(exports),
                            section_rva + eat, section_rva + npt, section_rva + ordinals)
    section = (directory + struct.pack(f'<{functions}I', *([0x2000] * functions)) +
               struct.pack(f'<{len(exports)}I', *name_rvas) +
               struct.pack(f'<{len(exports)}H', *range(len(exports))) + names_blob)
    optional = bytearray(240)
    struct.pack_into('<H', optional, 0, 0x20B)
    struct.pack_into('<II', optional, 112, section_rva, len(section))
    coff = struct.pack('<HHIIIHH', machine, 1, 0, 0, 0, len(optional), 0x2022)
    section_header = b'.edata\0\0' + struct.pack('<IIIIIIHHI', len(section), section_rva,
                                                  len(section), section_raw, 0, 0, 0, 0, 0x40000040)
    dos = bytearray(0x40)
    dos[:2] = b'MZ'
    struct.pack_into('<I', dos, 0x3C, 0x40)
    head = bytes(dos) + b'PE\0\0' + coff + bytes(optional) + section_header
    return head + b'\0' * (section_raw - len(head)) + section


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def tar_gz_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class ParserTest(unittest.TestCase):
    def test_elf64_exports_skip_hidden_and_undefined(self):
        data = build_elf(['shorebird_init', 'Dart_Initialize', 'JNI_OnLoad', '_RNvCs1_7updater4main'],
                         hidden=['internal_hidden'], undefined=['malloc'], android=True)
        parsed = inv.parse_elf(data)
        self.assertEqual(parsed['arch'], 'arm64')
        self.assertEqual(parsed['os'], 'android')
        self.assertEqual(sorted(parsed['exports']),
                         ['Dart_Initialize', 'JNI_OnLoad', '_RNvCs1_7updater4main', 'shorebird_init'])

    def test_elf32_arm(self):
        parsed = inv.parse_elf(build_elf(['JNI_OnLoad', 'SSL_new'], elf64=False, machine=40))
        self.assertEqual((parsed['arch'], parsed['os']), ('arm', None))
        self.assertEqual(sorted(parsed['exports']), ['JNI_OnLoad', 'SSL_new'])

    def test_macho_trie_and_symtab(self):
        for use_trie in (True, False):
            with self.subTest(use_trie=use_trie):
                data = build_macho(['_shorebird_init', '_FlutterEngineRun'], platform=2, use_trie=use_trie,
                                   undefined=['_free'])
                self.assertEqual(inv.detect_format(data), 'macho')
                parsed = inv.parse_macho(data)
                self.assertEqual((parsed['arch'], parsed['os']), ('arm64', 'ios'))
                self.assertEqual(sorted(parsed['exports']), ['_FlutterEngineRun', '_shorebird_init'])

    def test_fat_macho_and_java_class_are_distinguished(self):
        fat = build_fat([(0x0100000C, build_macho(['_a'])), (0x01000007, build_macho(['_b'], cpu=0x01000007))])
        self.assertEqual(inv.detect_format(fat), 'macho-fat')
        self.assertEqual(len(inv.macho_slices(fat)), 2)
        java_class = b'\xca\xfe\xba\xbe\x00\x00\x00\x34' + b'\0' * 64
        self.assertIsNone(inv.detect_format(java_class))

    def test_pe_exports(self):
        data = build_pe(['FlutterDesktopEngineCreate', 'shorebird_init'], machine=0xAA64, ordinal_only=2)
        self.assertEqual(inv.detect_format(data), 'pe')
        parsed = inv.parse_pe(data)
        self.assertEqual((parsed['arch'], parsed['os'], parsed['pe_is_dll']), ('arm64', 'windows', True))
        self.assertEqual(parsed['exports'], ['FlutterDesktopEngineCreate', 'shorebird_init'])
        self.assertEqual(parsed['pe_ordinal_only_exports'], 2)

    def test_non_binaries_are_ignored(self):
        for data in (b'', b'MZ', b'#!/bin/sh\n', b'\x7fELX' + b'\0' * 60):
            self.assertIsNone(inv.detect_format(data))


class SummaryTest(unittest.TestCase):
    def test_categories_and_digest(self):
        summary = inv.summarize_exports(
            ['_shorebird_init', '_Dart_Initialize', '_SSL_new', '__RNvCs1_7updater4main',
             '__ZdlPv', '_custom', '_shorebird_init'], strip_underscore=True)
        counts = summary['export_counts']
        self.assertEqual(summary['export_count'], 6)
        self.assertEqual((counts['shorebird'], counts['dart_api'], counts['boringssl'],
                          counts['rust_internal'], counts['cxx_mangled'], counts['other']),
                         (1, 1, 1, 1, 1, 1))
        self.assertEqual(summary['exports_by_category']['shorebird'], ['_shorebird_init'])
        self.assertNotIn('rust_internal', summary['exports_by_category'])
        again = inv.summarize_exports(reversed(['_shorebird_init', '_Dart_Initialize', '_SSL_new',
                                                '__RNvCs1_7updater4main', '__ZdlPv', '_custom']),
                                      strip_underscore=True)
        self.assertEqual(again['export_list_sha256'], summary['export_list_sha256'])


class ClassificationTest(unittest.TestCase):
    def test_routes(self):
        engine_route = f'mirror/shorebird/flutter_infra_release/flutter/{ENGINE}/android-arm64-release/artifacts.zip'
        self.assertEqual(inv.route_path(['a.tar.gz', engine_route, 'libflutter.so']),
                         'flutter_infra_release/flutter/$engine/android-arm64-release/artifacts.zip')
        release = inv.classify_route('flutter_infra_release/flutter/$engine/android-arm64-release/artifacts.zip')
        self.assertEqual((release['origin'], release['official_substitution'], release['published_override']),
                         ('custom-toolchain', 'forbidden', True))
        debug = inv.classify_route('flutter_infra_release/flutter/$engine/android-arm64/artifacts.zip')
        self.assertEqual(debug['official_substitution'], 'allowed-if-unchanged')
        self.assertFalse(debug['published_override'])
        maven = inv.route_path([f'm/download.flutter.io/io/flutter/arm64_v8a_release/1.0.0-{ENGINE}/'
                                f'arm64_v8a_release-1.0.0-{ENGINE}.jar'])
        self.assertEqual(inv.classify_route(maven)['official_substitution'], 'forbidden')
        self.assertEqual(inv.classify_route(None)['official_substitution'], 'not-routed')

    def test_roles_and_os(self):
        self.assertEqual(inv.classify_role(['x/symbols.zip', 'libflutter.so'], 'libflutter.so'), 'debug-symbols')
        self.assertEqual(inv.classify_role(['x/artifacts.zip', 'libflutter.so'], 'libflutter.so'), 'runtime')
        self.assertEqual(inv.classify_role(['x/artifacts.zip', 'gen_snapshot'], 'gen_snapshot'), 'host-tool')
        self.assertEqual(inv.infer_os(['x/a.jar', 'lib/arm64-v8a/libflutter.so'], 'android', 'elf'), 'android')
        # A host gen_snapshot inside an android-*-release archive has no NDK note.
        self.assertEqual(inv.infer_os(['android-engine/linux-x64.zip', 'gen_snapshot'], None, 'elf'), 'linux')
        self.assertEqual(inv.infer_os(['a'], None, 'macho'), 'unknown')

    def test_requires_custom_rule_mirrors_artifact_proxy(self):
        source = ROOT / 'shorebird/packages/artifact_proxy/lib/src/artifact_proxy.dart'
        if not source.exists():
            self.skipTest('shorebird submodule not checked out')
        text = source.read_text(encoding='utf-8')
        for fragment in ("r'/dart-sdk-[^/]+\\.zip$'", "'/font-subset.zip'", "'/flutter_patched_sdk_product.zip'",
                         "'/(?:android-[^/]+|ios|darwin(?:-[^/]+)?|linux-[^/]+)-release/'",
                         "r'/windows-[^/]+/windows-[^/]+-flutter\\.zip$'", "'/io/flutter/[^/]+_release/'"):
            self.assertIn(fragment, text, 'artifact_proxy custom-route rule changed; update REQUIRES_CUSTOM_ROUTE')


class EndToEndTest(unittest.TestCase):
    def build_tree(self, root: Path) -> None:
        engine = root / 'android-engine-arm64'
        engine.mkdir()
        lib = build_elf(['JNI_OnLoad', 'shorebird_init', '_RNvCs1_7updater4main'], android=True, debug=True)
        artifacts = zip_bytes({'libflutter.so': lib, 'README': b'text'})
        # The same unstripped bytes staged as the symbols copy; plus a class file.
        symbols = zip_bytes({'libflutter.so': lib})
        jar = zip_bytes({'lib/arm64-v8a/libflutter.so': lib,
                         'io/flutter/Foo.class': b'\xca\xfe\xba\xbe\x00\x00\x00\x34' + b'\0' * 32})
        mirror = f'android-engine/mirror/shorebird/flutter_infra_release/flutter/{ENGINE}/android-arm64-release'
        (engine / 'android-engine-arm64.tar.gz').write_bytes(tar_gz_bytes({
            f'{mirror}/artifacts.zip': artifacts,
            f'{mirror}/symbols.zip': symbols,
            'android-engine/artifacts.zip': artifacts,
            'android-engine/arm64_v8a_release.jar': jar,
            'android-engine/gen_snapshot_arm64': build_elf([], machine=62),
        }))
        windows = root / 'windows-engine-x64'
        windows.mkdir()
        (windows / 'flutter_windows.dll').write_bytes(build_pe(['FlutterDesktopEngineCreate', 'shorebird_init']))

    def test_inventory_is_deterministic_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_tree(root)
            inputs = [root / 'windows-engine-x64', root / 'android-engine-arm64']
            first = inv.inventory(inputs, root)
            second = inv.inventory(list(reversed(inputs)), root)
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
            self.assertEqual(first['errors'], [])
            self.assertEqual(first['record_count'], 6)
            by_role = {(entry['role'], entry['os']): entry for entry in first['unique_binaries']}
            runtime = by_role[('runtime', 'android')]
            self.assertEqual(runtime['occurrences'], 4)
            self.assertEqual(runtime['roles'], ['debug-symbols', 'runtime'])
            self.assertTrue(runtime['debug_info'])
            self.assertEqual(runtime['official_substitution'], ['forbidden'])
            self.assertEqual(runtime['export_counts']['shorebird'], 1)
            self.assertEqual(runtime['export_counts']['rust_internal'], 1)
            host = by_role[('host-tool', 'linux')]
            self.assertEqual((host['arch'], host['official_substitution']), ('x64', ['not-routed']))
            windows = by_role[('runtime', 'windows')]
            self.assertEqual((windows['arch'], windows['official_substitution']), ('x64', ['not-routed']))

    def test_cli_writes_canonical_json_with_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_tree(root)
            output = root / 'out' / 'inventory.json'
            output.parent.mkdir()
            status = inv.main([str(root / 'android-engine-arm64'), '--output', str(output),
                               '--source', 'ci_run=1', '--source', 'commit=abc'])
            self.assertEqual(status, 0)
            data = json.loads(output.read_text())
            self.assertEqual(data['source'], {'ci_run': '1', 'commit': 'abc'})
            self.assertEqual(output.read_text(), json.dumps(data, indent=1, sort_keys=True) + '\n')

    def test_corrupt_archive_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / 'bad'
            bad.mkdir()
            (bad / 'broken.zip').write_bytes(b'PK\x03\x04not a zip')
            result = inv.inventory([bad], root)
            self.assertEqual(len(result['errors']), 1)
            self.assertEqual(inv.main([str(bad), '--output', str(root / 'o.json')]), 1)


if __name__ == '__main__':
    unittest.main()

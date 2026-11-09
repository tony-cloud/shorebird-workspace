#!/usr/bin/env python3
"""Tests for scripts/hardened_lifecycle.py (P6 lifecycle-negatives runbook).

Portable: synthetic ELF/PE/Mach-O images and packages, and a fake launcher that
behaves like the integrity gate (config digest and engine measurement), so the
orchestration is exercised without a device or a hardened engine.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hardened_lifecycle as hl  # noqa: E402

CONFIG = (b'app_id: p6\nbase_url: https://updates.example.invalid\nsecurity_profile: hardened\n'
          b'toolchain_id: sha256:' + b'1' * 64 + b'\n'
          b'shorebird_build_manifest: v2.eyJjb250cmFjdF92ZXJzaW9uIjoyLCJtb2R1bGVzIjpbXX0.c2ln.dHJ1c3Q\n')


def elf64(rodata: bytes = b'R' * 64, writable: bool = False) -> bytes:
    """Minimal ELF64 with a section table: null, .rodata, .shstrtab."""
    names = b'\0.rodata\0.shstrtab\0'
    header_size = 64
    rodata_offset = header_size
    names_offset = rodata_offset + len(rodata)
    shoff = names_offset + len(names)
    ident = b'\x7fELF' + bytes([2, 1, 1]) + bytes(9)
    header = ident + struct.pack('<HHIQQQIHHHHHH', 3, 0xB7, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 3, 2)
    sections = bytes(64)
    sections += struct.pack('<IIQQQQIIQQ', 1, 1, 0x2 | (0x1 if writable else 0), 0, rodata_offset,
                            len(rodata), 0, 0, 1, 0)
    sections += struct.pack('<IIQQQQIIQQ', 9, 3, 0, 0, names_offset, len(names), 0, 0, 1, 0)
    return header + rodata + names + sections


def pe(rdata: bytes = b'D' * 64) -> bytes:
    pe_offset = 0x40
    optional = 0xF0
    table = pe_offset + 24 + optional
    raw_offset = 0x200
    data = bytearray(raw_offset + len(rdata))
    data[0:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, pe_offset)
    data[pe_offset:pe_offset + 4] = b'PE\0\0'
    struct.pack_into('<HH', data, pe_offset + 4, 0xAA64, 1)
    struct.pack_into('<H', data, pe_offset + 20, optional)
    data[table:table + 8] = b'.rdata\0\0'
    struct.pack_into('<II', data, table + 16, len(rdata), raw_offset)
    struct.pack_into('<I', data, table + 36, 0x40000040)
    data[raw_offset:] = rdata
    return bytes(data)


def macho64(cstring: bytes = b'C' * 64) -> bytes:
    """Thin 64-bit Mach-O: one LC_SEGMENT_64 __TEXT with a __cstring section."""
    cmdsize = 72 + 80
    offset = 32 + cmdsize
    header = struct.pack('<IiiIIIII', 0xFEEDFACF, 0x0100000C, 0, 6, 1, cmdsize, 0, 0)
    segment = struct.pack('<II16sQQQQiiII', 0x19, cmdsize, b'__TEXT', 0, 0, 0, 0, 5, 5, 1, 0)
    section = struct.pack('<16s16sQQIIIIIIII', b'__cstring', b'__TEXT', 0, len(cstring), offset, 0, 0, 0, 0, 0, 0, 0)
    return header + segment + section + cstring


def fat(*slices: bytes) -> bytes:
    header = struct.pack('>II', 0xCAFEBABE, len(slices))
    offset = 0x1000
    entries = b''
    body = b''
    for item in slices:
        entries += struct.pack('>iiIII', 0x0100000C, 0, offset + len(body), len(item), 12)
        body += item + bytes((-len(item)) % 0x1000)
    return header + entries + bytes(offset - 8 - len(entries)) + body


class ParseTest(unittest.TestCase):
    def test_passed_line_records_the_startup_cost(self):
        text = ('2026-11-09 21:34:47.369 Df hardened_app[6785:5ee1356] [dev.shorebird:updater::integrity] '
                '[shorebird] Integrity check passed (18011 us)\n')
        self.assertEqual(hl.parse_integrity(text), {'outcome': 'passed', 'code': None, 'micros': 18011})

    def test_refusals_from_the_updater_and_the_engine(self):
        updater = ('[shorebird] integrity check failed: module_mismatch (17592 us). Patches and protected keys '
                   'are disabled; the bundled release runs.')
        engine = ('[ERROR:flutter/shell/common/shorebird/updater.cc(181)] Shorebird integrity check failed '
                  '(config_digest_mismatch); running the bundled release without patches.')
        detailed = 'integrity check failed: manifest_invalid (signature) (12 us)'
        self.assertEqual(hl.parse_integrity(updater)['code'], 'module_mismatch')
        self.assertEqual(hl.parse_integrity(engine)['code'], 'config_digest_mismatch')
        self.assertEqual(hl.parse_integrity(detailed)['code'], 'manifest_invalid')
        # A refusal wins over an earlier pass; two different codes are ambiguous.
        self.assertEqual(hl.parse_integrity('Integrity check passed (5 us)\n' + engine)['outcome'], 'refused')
        self.assertEqual(hl.parse_integrity(updater + '\n' + engine)['outcome'], 'ambiguous')
        self.assertEqual(hl.parse_integrity('nothing here')['outcome'], 'unknown')


class MutationTest(unittest.TestCase):
    def test_config_tamper_redirects_base_url_above_the_manifest(self):
        tampered = hl.tamper_config(CONFIG)
        self.assertIn(b'base_url: https://attacker.invalid\n', tampered)
        self.assertEqual(tampered.splitlines()[-1], CONFIG.splitlines()[-1])
        without = CONFIG.replace(b'base_url: https://updates.example.invalid\n', b'')
        self.assertIn(b'auto_update: true\n', hl.tamper_config(without))

    def test_config_tamper_refuses_a_legacy_config(self):
        with self.assertRaisesRegex(hl.LifecycleError, 'hardened release'):
            hl.tamper_config(b'app_id: p6\n')

    def test_engine_flip_lands_in_a_measured_section(self):
        for image, region in ((elf64(), (64, 64)), (pe(), (0x200, 64)), (macho64(), (32 + 152, 64))):
            mutated = hl.flip_measured_byte(image)
            changed = [i for i, (a, b) in enumerate(zip(image, mutated)) if a != b]
            self.assertEqual(len(changed), 1)
            self.assertTrue(region[0] <= changed[0] < region[0] + region[1])

    def test_every_fat_slice_is_modified(self):
        image = fat(macho64(), macho64(b'X' * 64))
        mutated = hl.flip_measured_byte(image)
        self.assertEqual(sum(a != b for a, b in zip(image, mutated)), 2)

    def test_no_read_only_section_is_an_error(self):
        with self.assertRaisesRegex(hl.LifecycleError, 'no measured read-only section'):
            hl.flip_measured_byte(elf64(writable=True))

    def test_apk_mutation_rewrites_one_entry_and_drops_the_old_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'app.apk'
            with zipfile.ZipFile(source, 'w') as apk:
                apk.writestr('assets/flutter_assets/shorebird.yaml', CONFIG)
                engine = zipfile.ZipInfo('lib/arm64-v8a/libflutter.so')
                engine.compress_type = zipfile.ZIP_STORED
                apk.writestr(engine, elf64())
                apk.writestr('META-INF/CERT.RSA', b'sig')
                apk.writestr('META-INF/services/x', b'keep')
            for variant, name in (('tampered-config', 'assets/flutter_assets/shorebird.yaml'),
                                  ('replaced-engine', 'lib/arm64-v8a/libflutter.so')):
                target = Path(directory) / f'{variant}.apk'
                hl.mutate_apk(source, target, variant, 'arm64-v8a')
                with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
                    self.assertNotIn('META-INF/CERT.RSA', after.namelist())
                    self.assertIn('META-INF/services/x', after.namelist())
                    self.assertEqual(after.getinfo('lib/arm64-v8a/libflutter.so').compress_type, zipfile.ZIP_STORED)
                    changed = [n for n in after.namelist() if after.read(n) != before.read(n)]
                    self.assertEqual(changed, [name])
            with self.assertRaisesRegex(hl.LifecycleError, 'has no lib/x86_64/libflutter.so'):
                hl.mutate_apk(source, Path(directory) / 'x.apk', 'replaced-engine', 'x86_64')


def linux_bundle(root: Path) -> Path:
    bundle = root / 'bundle'
    (bundle / 'data' / 'flutter_assets').mkdir(parents=True)
    (bundle / 'lib').mkdir()
    (bundle / 'data' / 'flutter_assets' / 'shorebird.yaml').write_bytes(CONFIG)
    (bundle / 'lib' / 'libflutter_linux_gtk.so').write_bytes(elf64())
    exe = bundle / 'app'
    exe.write_text('#!/bin/sh\n')
    exe.chmod(0o755)
    return bundle


class LifecycleTest(unittest.TestCase):
    def test_gate_behaviour_is_accepted_and_costed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = linux_bundle(root)
            original_config = hashlib.sha256(CONFIG).hexdigest()
            original_engine = hashlib.sha256(elf64()).hexdigest()
            copies = {}

            def launch(variant, run):
                # A stand-in for the engine's gate over the variant's files.
                if variant not in copies:
                    copies[variant] = hl.prepare_variant('linux', bundle, root / 'work', variant)
                layout = hl.desktop_layout('linux', copies[variant])
                if hashlib.sha256(layout.config.read_bytes()).hexdigest() != original_config:
                    return 'integrity check failed: config_digest_mismatch (3 us)'
                if hashlib.sha256(layout.engine.read_bytes()).hexdigest() != original_engine:
                    return '[ERROR:updater.cc(181)] Shorebird integrity check failed (module_mismatch); ...'
                return f'Integrity check passed ({100 + run} us)'

            evidence = hl.Evidence('linux', str(bundle))
            evidence.release = hl.release_facts(CONFIG)
            result = hl.run_lifecycle(evidence, 3, launch)
            self.assertTrue(result['accepted'], result['failures'])
            self.assertEqual(result['startup_cost']['samples_us'], [101, 102, 103, 101, 102, 103])
            self.assertEqual(result['startup_cost']['p50_us'], 102)
            self.assertEqual(result['release']['toolchain_id'], 'sha256:' + '1' * 64)
            self.assertEqual(result['release']['manifest_contract_version'], 2)
            json.dumps(result)

    def test_a_gate_that_accepts_tampering_is_rejected(self):
        evidence = hl.Evidence('linux', 'x')
        result = hl.run_lifecycle(evidence, 1, lambda variant, run: 'Integrity check passed (9 us)')
        self.assertFalse(result['accepted'])
        self.assertEqual(len(result['failures']), 2)
        self.assertIn('tampered-config run 1: passed', result['failures'][0])

    def test_wrong_refusal_code_is_rejected(self):
        evidence = hl.Evidence('linux', 'x')
        result = hl.run_lifecycle(evidence, 1, lambda variant, run: (
            'integrity check failed: module_unbound' if variant != 'baseline' and variant != 'reinstalled'
            else 'Integrity check passed (9 us)'))
        self.assertFalse(result['accepted'])
        self.assertTrue(any('expected refused config_digest_mismatch' in f for f in result['failures']))

    def test_layouts_require_the_config_and_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = linux_bundle(Path(directory))
            self.assertEqual(hl.desktop_layout('linux', bundle).executable.name, 'app')
            (bundle / 'lib' / 'libflutter_linux_gtk.so').unlink()
            with self.assertRaisesRegex(hl.LifecycleError, 'missing'):
                hl.desktop_layout('linux', bundle)

    def test_cli_requires_a_hardened_release(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = linux_bundle(Path(directory))
            (bundle / 'data' / 'flutter_assets' / 'shorebird.yaml').write_bytes(b'app_id: legacy\n')
            code = hl.main(['--output', str(Path(directory) / 'e.json'), '--runs', '1', '--seconds', '0.1',
                            'desktop', '--platform', 'linux', '--app', str(bundle)])
            self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Pure machine/oracle tests; no native binary or build is executed."""
import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import verify_native_desktop_arch as arch


def elf(arm64=True):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, 183 if arm64 else 62)
    return bytes(header)


def pe(arm64=True):
    header = bytearray(136)
    header[:2] = b'MZ'
    struct.pack_into('<I', header, 0x3c, 128)
    header[128:132] = b'PE\0\0'
    struct.pack_into('<H', header, 132, 0xaa64 if arm64 else 0x8664)
    return bytes(header)


class MachineTest(unittest.TestCase):
    def test_elf_and_pe_machine_types_and_wrong_architecture(self):
        for make in (elf, pe):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'binary'
                for arm64 in (False, True):
                    path.write_bytes(make(arm64))
                    expected = 'arm64' if arm64 else 'x64'
                    self.assertEqual(arch.binary_arch(path), expected)
                    self.assertTrue(arch.require_binary(path, expected)['native_target'])
                    with self.assertRaises(ValueError):
                        arch.require_binary(path, 'x64' if arm64 else 'arm64')
                for data in (b'', b'MZ', b'\x7fELF', b'not-a-machine', pe()[:130]):
                    path.write_bytes(data)
                    with self.assertRaises(ValueError):
                        arch.require_binary(path, 'arm64')

    def test_matching_cpu_in_wrong_os_container_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'patch.exe'
            path.write_bytes(elf())
            with self.assertRaisesRegex(ValueError, 'Expected pe'):
                arch.require_binary(path, 'arm64', expected_format='pe')

    def test_native_host_gate_rejects_emulated_python(self):
        with mock.patch.object(arch.platform, 'system', return_value='Linux'), \
                mock.patch.object(arch.platform, 'machine', return_value='aarch64'), \
                mock.patch.object(arch, 'require_binary', side_effect=ValueError('x64 Python')):
            with self.assertRaisesRegex(ValueError, 'x64 Python'):
                arch.require_native_host('linux', 'arm64')
        with mock.patch.object(arch.platform, 'system', return_value='Linux'), \
                mock.patch.object(arch.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(ValueError, 'foreign or emulated'):
                arch.require_native_host('linux', 'arm64')

    def test_lifecycle_receipt_requires_all_stages_native_machines_and_cleanup(self):
        common = (
            'isolated-toolchain', 'released-native-runtime-policy', 'actual-cli-release-free',
            'rebuilt-native-bytes-identical', 'actual-cli-patch-pro', 'ciphertext-installed',
            'api-and-provisioning-stopped', 'offline-cold-U', 'offline-missing-key-BASE',
            'offline-missing-recovery-U', 'offline-wrong-key-BASE', 'offline-wrong-recovery-U',
            'final-cache-before-rollback', 'online-U-receive-signed-rollback',
            'offline-cold-rollback-BASE', 'final-cache-after-rollback', 'released-bundle-unchanged')
        for target_os in ('linux', 'windows'):
            stages = common + (('native-probe-self-test', 'native-probe-online', 'native-probe-offline')
                               if target_os == 'linux' else ())
            report = {'schema': target_os + '-native-cache-e2e-v1', 'result': 'passed',
                'target_arch': 'arm64', 'runtime_arch': 'aarch64',
                'binary_architectures': {str(n): {'architecture': 'arm64', 'native_target': True,
                                                'format': 'elf' if target_os == 'linux' else 'pe'}
                                        for n in range(7)},
                'cleanup': 'owned processes, ports, disposable app cache, private session removed' +
                           ('; owned session keyring revoked' if target_os == 'linux' else ''),
                'stages': [{'name': name} for name in stages]}
            arch.validate_lifecycle_report(report, target_os, 'arm64')
            for field, bad in [('target_arch', 'x64'), ('runtime_arch', 'arm64'),
                               ('stages', report['stages'][:-1]), ('cleanup', 'not cleaned'),
                               ('binary_architectures', {})]:
                with self.subTest(platform=target_os, field=field), self.assertRaises(ValueError):
                    arch.validate_lifecycle_report(dict(report, **{field: bad}), target_os, 'arm64')
            wrong = copy.deepcopy(report)
            wrong['binary_architectures']['0']['architecture'] = 'x64'
            with self.assertRaises(ValueError):
                arch.validate_lifecycle_report(wrong, target_os, 'arm64')

    def test_build_tool_emulation_is_reported_without_runtime_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            native, tool = Path(directory) / 'native', Path(directory) / 'linker'
            native.write_bytes(pe())
            tool.write_bytes(pe(False))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(arch.main(['--platform', 'windows', '--arch', 'arm64',
                    '--binary', str(native), '--build-tool', str(tool)]), 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report['build_tools']['linker']['emulated'])
            self.assertTrue(report['runtime_binaries']['native']['native_target'])
            self.assertNotIn('host', report)


if __name__ == '__main__':
    unittest.main()

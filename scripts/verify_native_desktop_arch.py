#!/usr/bin/env python3
"""Check desktop machine types without executing foreign binaries.

Runtime binaries must match the selected native target. Build tools may run
under OS emulation, but their machine types are reported separately.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import struct
import sys

RUNTIME_ARCH = {'x64': 'x86_64', 'arm64': 'aarch64'}


def machine_format(data: bytes) -> str:
    if data[:6] == b'\x7fELF\x02\x01':
        return 'elf'
    if data[:2] == b'MZ':
        return 'pe'
    return 'unsupported'


def machine_arch(data: bytes) -> str:
    if len(data) >= 20 and data[:6] == b'\x7fELF\x02\x01':
        machine = struct.unpack_from('<H', data, 18)[0]
        return {62: 'x64', 183: 'arm64'}.get(machine, 'unsupported')
    if len(data) >= 64 and data[:2] == b'MZ':
        offset = struct.unpack_from('<I', data, 0x3c)[0]
        if offset <= len(data) - 6 and data[offset:offset + 4] == b'PE\0\0':
            machine = struct.unpack_from('<H', data, offset + 4)[0]
            return {0x8664: 'x64', 0xaa64: 'arm64'}.get(machine, 'unsupported')
    return 'unsupported'


def binary_arch(path: Path) -> str:
    with Path(path).open('rb') as source:
        header = source.read(64)
        if len(header) >= 64 and header[:2] == b'MZ':
            offset = struct.unpack_from('<I', header, 0x3c)[0]
            if offset > 1024 * 1024:
                return 'unsupported'
            source.seek(offset)
            pe = source.read(6)
            # machine_arch expects the PE header at its declared file offset.
            return machine_arch(header + b'\0' * max(0, offset - 64) + pe)
        return machine_arch(header)


def require_binary(path: Path, arch: str, *, expected_format=None) -> dict:
    if arch not in RUNTIME_ARCH:
        raise ValueError('Unsupported desktop target architecture')
    with Path(path).open('rb') as source:
        actual_format = machine_format(source.read(64))
    if expected_format is not None and actual_format != expected_format:
        raise ValueError(f'Expected {expected_format} binary at {path}; found {actual_format}')
    observed = binary_arch(path)
    if observed != arch:
        raise ValueError(f'Expected native {arch} binary at {path}; found {observed}')
    return {'architecture': observed, 'format': actual_format, 'native_target': True}


def require_native_host(target_os: str, arch: str) -> dict:
    aliases = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}
    actual = aliases.get(platform.machine().lower())
    native_os = os.name == 'nt' if target_os == 'windows' else platform.system() == 'Linux'
    if not native_os or actual != arch:
        raise ValueError(f'Execution requires {target_os.title()} {arch} native process; foreign or emulated process rejected')
    require_binary(Path(sys.executable), arch, expected_format='pe' if target_os == 'windows' else 'elf')
    return {'os': target_os, 'architecture': actual, 'python_native': True}


def validate_lifecycle_report(report: dict, target_os: str, arch: str) -> None:
    """Validate fresh driver receipt inventory; this does not execute a runtime."""
    if not isinstance(report, dict) or arch not in RUNTIME_ARCH:
        raise ValueError('Invalid native lifecycle report or architecture')
    cleanup = 'owned processes, ports, disposable app cache, private session removed'
    if target_os == 'linux':
        cleanup += '; owned session keyring revoked'
    if (report.get('schema') != target_os + '-native-cache-e2e-v1' or
            report.get('result') != 'passed' or report.get('cleanup') != cleanup or
            report.get('target_arch') != arch or report.get('runtime_arch') != RUNTIME_ARCH[arch]):
        raise ValueError('Native lifecycle report identity, result, architecture or cleanup mismatch')
    binaries = report.get('binary_architectures')
    if (not isinstance(binaries, dict) or len(binaries) < (7 if target_os == 'linux' else 6) or
            any(not isinstance(item, dict) or item.get('architecture') != arch or
                item.get('native_target') is not True or
                item.get('format') != ('elf' if target_os == 'linux' else 'pe')
                for item in binaries.values())):
        raise ValueError('Native lifecycle binary machine evidence is incomplete or mismatched')
    required = {
        'isolated-toolchain', 'released-native-runtime-policy', 'actual-cli-release-free',
        'rebuilt-native-bytes-identical', 'actual-cli-patch-pro', 'ciphertext-installed',
        'api-and-provisioning-stopped', 'offline-cold-U', 'offline-missing-key-BASE',
        'offline-missing-recovery-U', 'offline-wrong-key-BASE', 'offline-wrong-recovery-U',
        'final-cache-before-rollback', 'online-U-receive-signed-rollback',
        'offline-cold-rollback-BASE', 'final-cache-after-rollback', 'released-bundle-unchanged',
    }
    if target_os == 'linux':
        required.update(('native-probe-self-test', 'native-probe-online', 'native-probe-offline'))
    stages = report.get('stages')
    if not isinstance(stages, list) or any(not isinstance(stage, dict) or not isinstance(stage.get('name'), str) for stage in stages):
        raise ValueError('Native lifecycle stage receipts are malformed')
    observed = {stage.get('name') for stage in stages}
    if not required.issubset(observed):
        raise ValueError('Native lifecycle stage receipts are incomplete')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('linux', 'windows'), required=True)
    parser.add_argument('--arch', choices=tuple(RUNTIME_ARCH), required=True)
    parser.add_argument('--binary', type=Path, action='append', default=[])
    parser.add_argument('--build-tool', type=Path, action='append', default=[])
    parser.add_argument('--require-native-host', action='store_true')
    parser.add_argument('--lifecycle-report', type=Path)
    args = parser.parse_args(argv)
    report = {'target_os': args.platform, 'target_arch': args.arch,
              'runtime_binaries': {}, 'build_tools': {}}
    if args.lifecycle_report:
        validate_lifecycle_report(json.loads(args.lifecycle_report.read_text()), args.platform, args.arch)
        report['lifecycle_report_validated'] = True
    if args.require_native_host:
        report['host'] = require_native_host(args.platform, args.arch)
    for path in args.binary:
        report['runtime_binaries'][path.name] = require_binary(path, args.arch, expected_format='pe' if args.platform == 'windows' else 'elf')
    for path in args.build_tool:
        observed = binary_arch(path)
        if observed == 'unsupported':
            raise ValueError(f'Cannot identify build tool machine: {path}')
        report['build_tools'][path.name] = {'architecture': observed, 'emulated': observed != args.arch}
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None

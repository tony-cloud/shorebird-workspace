#!/usr/bin/env python3
"""Prove native ARM64 producer gates reject representative CI regressions."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/open-shorebird-ci.yml'


def edit_job(source, name, before, after):
    start = source.index('  ' + name + ':\n')
    end_match = re.search(r'^  [a-z][\w-]*:\n', source[start + 3:], re.M)
    end = start + 3 + end_match.start() if end_match else len(source)
    block = source[start:end]
    if before not in block:
        raise AssertionError('negative control no longer matches ' + name)
    return source[:start] + block.replace(before, after) + source[end:]


class Arm64WorkflowContractTest(unittest.TestCase):
    def test_native_contract_and_negative_controls(self):
        source = WORKFLOW.read_text()
        controls = [
            ('cli-runner', edit_job(source, 'cli-artifacts', 'os: ubuntu-24.04-arm', 'os: ubuntu-latest'), 'missing CLI target linux/arm64'),
            ('linux-gn', edit_job(source, 'linux-engine-arm64', '--linux-cpu=arm64', '--linux-cpu=x64'), 'explicit ARM64 engine and Rust targets'),
            ('windows-rust', edit_job(source, 'windows-engine-arm64', 'rustup target add aarch64-pc-windows-msvc', 'rustup target add x86_64-pc-windows-msvc'), 'explicit ARM64 engine and Rust targets'),
            ('runtime-plan', edit_job(source, 'linux-engine-arm64', 'run --execute --arch arm64', 'plan --arch arm64'), 'execute the real ARM64 app lifecycle'),
            ('sdk-gn', edit_job(source, 'custom-dart-sdk-linux-arm64', "'target_cpu=\"arm64\"'", "'target_cpu=\"x64\"'"), 'explicit ARM64 GN and patching flags'),
            ('mirror-inventory', edit_job(source, 'artifact-mirror', "--require 'windows-engine-arm64/*windows-engine-arm64.tar.gz'", "--require 'removed/windows-engine-arm64.tar.gz'"), 'release manifest must require windows-engine-arm64'),
            ('native-process', edit_job(source, 'windows-engine-arm64', '--require-native-host', ''), 'native host and binary machine types'),
            ('manifest-json', edit_job(source, 'linux-engine-arm64', '"name": "linux-engine-arm64",', '"name": "linux-engine-arm64",,'), 'invalid producer manifest JSON template'),
            ('runtime-receipt', edit_job(source, 'windows-engine-arm64', '--lifecycle-report', '--receipt-validation-removed'), 'validate complete native machine and lifecycle stage receipts'),
            ('mac-runtime-filter', edit_job(source, 'macos-engine', ':DartIsolateTest.CommittedInterpreterGroupRetainsApplicationShutdownCallback', ''), 'exact committed/uncommitted interpreter shutdown controls'),
            ('mac-inventory-arch', edit_job(source, 'macos-engine', '--arch=${{ matrix.arch }}', '--arch=arm64'), 'macOS inventory validator must receive the actual matrix architecture'),
            ('linux-version-tag', edit_job(source, 'linux-engine', 'python3 scripts/prepare_flutter_version_tag.py flutter', 'echo tag-restore-removed'), 'verified canonical Flutter version tag before lifecycle preparation'),
            ('windows-arm64-version-tag', edit_job(source, 'windows-engine-arm64', 'python3 scripts/prepare_flutter_version_tag.py flutter', 'echo tag-restore-removed'), 'verified canonical Flutter version tag before lifecycle preparation'),
            ('windows-vs-year', edit_job(source, 'custom-dart-sdk-windows-arm64', 'GYP_MSVS_VERSION: "2026"', 'GYP_MSVS_VERSION: "2022"'), 'align the Windows ARM64 runner with VS2026 toolchain environment'),
            ('windows-vs-range', edit_job(source, 'windows-engine-arm64', "-version '[18.0,19.0)'", "-version '[17.0,18.0)'"), 'discover VS18 ARM64 and export its matching installation'),
            ('linux-arm64-android-deps', edit_job(source, 'linux-engine-arm64', 'INCLUDE_ANDROID_DEPS: "0"', 'INCLUDE_ANDROID_DEPS: "1"'), 'exclude unavailable Android host packages'),
            ('windows-arm64-rust-driver', edit_job(source, 'windows-engine-arm64', 'VC/Tools/Llvm/ARM64/bin/clang.exe', 'VC/Tools/Llvm/ARM64/bin/clang-cl.exe'), 'pin native GNU-style clang drivers'),
        ]
        for label, text, error in [('valid', source, None), *controls]:
            with self.subTest(control=label):
                with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', prefix='.arm64-contract-', dir=WORKFLOW.parent) as fixture:
                    fixture.write(text)
                    fixture.flush()
                    result = subprocess.run(['bash', (ROOT / 'scripts/verify_ci_workflow.sh').as_posix(), Path(fixture.name).as_posix()],
                        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if error is None:
                    self.assertEqual(result.returncode, 0, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0, label)
                    self.assertIn(error, result.stdout)


if __name__ == '__main__':
    unittest.main()

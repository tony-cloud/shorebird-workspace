#!/usr/bin/env python3
"""Exercise real gitlink pin generation and verification through a tiny archive."""
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from write_cli_toolchain_pins import source_pins, verify_pins, write_pins


HELPER = Path(__file__).with_name('write_cli_toolchain_pins.py').resolve()
ENGINE = '1' * 40
STALE = '2' * 40


class ToolchainPinsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shorebird-cli-pins-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.flutter = self.root / 'flutter'
        self.flutter.mkdir()
        self.engine_file = self.flutter / 'bin/internal/engine.version'
        self.engine_file.parent.mkdir(parents=True)
        self.engine_file.write_text(ENGINE + '\n')
        self.git(self.flutter, 'init', '-q')
        self.git(self.flutter, 'add', '.')
        self.commit(self.flutter)
        self.revision = self.git(self.flutter, 'rev-parse', 'HEAD')
        self.git(self.root, 'init', '-q')
        self.git(self.root, 'update-index', '--add', '--cacheinfo',
                 f'160000,{self.revision},flutter')
        self.commit(self.root)
        # A stale source-CLI pin must have no influence on the packaged pair.
        stale = self.root / 'shorebird/bin/internal/flutter.version'
        stale.parent.mkdir(parents=True)
        stale.write_text(STALE + '\n')
        self.package = self.root / 'package'

    @staticmethod
    def git(directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def commit(self, directory):
        self.git(directory, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def write_manifest(self):
        (self.package / 'manifest.json').write_text(json.dumps({
            'flutter_revision': (self.package / 'bin/internal/flutter.version').read_text().strip(),
            'engine_revision': (self.package / 'bin/internal/engine.version').read_text().strip(),
        }))

    def test_real_generation_archive_extraction_and_both_verification_gates(self):
        subprocess.run([sys.executable, str(HELPER), 'write', '--workspace', str(self.root),
                        '--package', str(self.package)], check=True,
                       capture_output=True, text=True, timeout=10)
        self.write_manifest()
        expected = {'flutter_revision': self.revision, 'engine_revision': ENGINE}
        self.assertEqual(verify_pins(self.root, self.package), expected)
        archive = self.root / 'cli.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            output.add(self.package, arcname='cli')
        extracted = self.root / 'extracted'
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter='data')
        subprocess.run([sys.executable, str(HELPER), 'verify', '--workspace', str(self.root),
                        '--package', str(extracted / 'cli')], check=True,
                       capture_output=True, text=True, timeout=10)
        self.assertEqual(verify_pins(self.root, extracted / 'cli'), expected)

    def test_stale_cli_flutter_copy_is_rejected_even_with_matching_stale_manifest(self):
        write_pins(self.root, self.package)
        (self.package / 'bin/internal/flutter.version').write_text(STALE + '\n')
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Packaged flutter.version'):
            verify_pins(self.root, self.package)

    def test_wrong_engine_file_and_stale_manifest_are_rejected(self):
        write_pins(self.root, self.package)
        self.write_manifest()
        (self.package / 'bin/internal/engine.version').write_text(STALE + '\n')
        with self.assertRaisesRegex(ValueError, 'Packaged engine.version'):
            verify_pins(self.root, self.package)
        write_pins(self.root, self.package)
        manifest = self.package / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['engine_revision'] = STALE
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'manifest'):
            verify_pins(self.root, self.package)

    def test_unpinned_flutter_checkout_is_rejected_before_any_package_write(self):
        (self.flutter / 'new-file').write_text('next revision')
        self.git(self.flutter, 'add', '.')
        self.commit(self.flutter)
        with self.assertRaisesRegex(ValueError, 'differs from the root Flutter gitlink'):
            write_pins(self.root, self.package)
        self.assertFalse(self.package.exists())

    def test_dirty_engine_pin_is_rejected_before_any_package_write(self):
        self.engine_file.write_text(STALE + '\n')
        with self.assertRaisesRegex(ValueError, 'engine.version differs'):
            write_pins(self.root, self.package)
        self.assertFalse(self.package.exists())

    def test_root_requires_a_flutter_gitlink(self):
        self.git(self.root, 'update-index', '--force-remove', 'flutter')
        self.git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                 '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'remove gitlink')
        with self.assertRaisesRegex(ValueError, 'pin Flutter as a gitlink'):
            source_pins(self.root)


if __name__ == '__main__':
    unittest.main()

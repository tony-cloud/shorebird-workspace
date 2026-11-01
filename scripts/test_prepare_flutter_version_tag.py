#!/usr/bin/env python3
"""Exercise real Git refs and ancestry without network or SDK mutation."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('prepare_flutter_version_tag',
    Path(__file__).with_name('prepare_flutter_version_tag.py'))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class FlutterVersionTagTest(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

    def repo(self, path):
        path.mkdir()
        self.git(path, 'init')
        self.git(path, 'config', 'user.name', 'Fixture')
        self.git(path, 'config', 'user.email', 'fixture@example.invalid')
        self.git(path, 'commit', '--allow-empty', '-m', 'base')
        return self.git(path, 'rev-parse', 'HEAD')

    def test_fetches_exact_ancestor_and_is_idempotent_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            upstream, checkout = Path(temp) / 'upstream', Path(temp) / 'checkout'
            revision = self.repo(upstream)
            self.git(upstream, 'tag', helper.TAG)
            self.git(upstream, 'clone', '--no-tags', str(upstream), str(checkout))
            args = dict(upstream=str(upstream), revision=revision)
            self.assertEqual(helper.restore_version_tag(checkout, **args), revision)
            self.assertEqual(self.git(checkout, 'rev-parse', helper.TAG), revision)
            self.assertEqual(helper.restore_version_tag(checkout, upstream='nonexistent', revision=revision), revision)

    def test_rejects_mismatched_existing_tag_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'checkout'
            base = self.repo(repo)
            self.git(repo, 'commit', '--allow-empty', '-m', 'new')
            wrong = self.git(repo, 'rev-parse', 'HEAD')
            self.git(repo, 'tag', helper.TAG)
            with self.assertRaisesRegex(ValueError, 'unexpected revision'):
                helper.restore_version_tag(repo, revision=base)
            self.assertEqual(self.git(repo, 'rev-parse', helper.TAG), wrong)

    def test_rejects_wrong_upstream_revision_without_creating_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            upstream, checkout = Path(temp) / 'upstream', Path(temp) / 'checkout'
            revision = self.repo(upstream)
            self.git(upstream, 'tag', helper.TAG)
            self.git(upstream, 'clone', '--no-tags', str(upstream), str(checkout))
            with self.assertRaisesRegex(ValueError, 'unexpected revision'):
                helper.restore_version_tag(checkout, upstream=str(upstream), revision='0' * 40)
            self.assertEqual(self.git(checkout, 'tag', '--list'), '')
            self.assertEqual(self.git(checkout, 'rev-parse', 'HEAD'), revision)

    def test_rejects_nonancestor_without_creating_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            upstream, checkout = Path(temp) / 'upstream', Path(temp) / 'checkout'
            self.repo(upstream)
            self.git(upstream, 'clone', '--no-tags', str(upstream), str(checkout))
            self.git(upstream, 'commit', '--allow-empty', '-m', 'not in checkout')
            revision = self.git(upstream, 'rev-parse', 'HEAD')
            self.git(upstream, 'tag', helper.TAG)
            with self.assertRaisesRegex(ValueError, 'not an ancestor'):
                helper.restore_version_tag(checkout, upstream=str(upstream), revision=revision)
            self.assertEqual(self.git(checkout, 'tag', '--list'), '')


if __name__ == '__main__':
    unittest.main()

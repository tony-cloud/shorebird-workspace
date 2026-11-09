#!/usr/bin/env python3
"""Tests for scripts/provision_release_keys.py.

GitHub is a fake `gh` on PATH that records every call; no test contacts
GitHub. Private directories and workspaces are temporary directories outside
every git work tree. Production-mode cases use throwaway keys that are
deleted with their temporary directory.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_catalog as ac  # noqa: E402
import provision_release_keys as prk  # noqa: E402
import release_trust as rt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'shorebird-server'
FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Fake gh: records argv and stdin digests; keeps variables and secrets in a JSON file."""
    import hashlib, json, os, sys
    state_path, log_path = os.environ['FAKE_GH_STATE'], os.environ['FAKE_GH_LOG']
    args = sys.argv[1:]
    if '--repo' not in args:
        sys.exit('fake gh: --repo is required')
    repo = args[args.index('--repo') + 1]
    rest = [a for i, a in enumerate(args) if a != '--repo' and (i == 0 or args[i - 1] != '--repo')]
    stdin = b'' if sys.stdin.isatty() else sys.stdin.buffer.read() if rest[1:2] == ['set'] else b''
    with open(log_path, 'a') as log:
        log.write(json.dumps({'argv': args, 'stdin_sha256': hashlib.sha256(stdin).hexdigest() if stdin else None}) + '\\n')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    repo_state = state.setdefault(repo, {'variables': {}, 'secrets': {}})
    if os.environ.get('FAKE_GH_FAIL') and ' '.join(rest[:2]) == os.environ['FAKE_GH_FAIL']:
        sys.exit('fake gh: simulated failure')
    if rest[:2] == ['variable', 'list']:
        print(json.dumps([{'name': k, 'value': v} for k, v in repo_state['variables'].items()]))
    elif rest[:2] == ['secret', 'list']:
        print(json.dumps([{'name': k} for k in repo_state['secrets']]))
    elif rest[:2] == ['variable', 'set'] and len(rest) == 3:
        repo_state['variables'][rest[2]] = stdin.decode()
    elif rest[:2] == ['secret', 'set'] and len(rest) == 3:
        repo_state['secrets'][rest[2]] = stdin.decode()
    else:
        sys.exit(f'fake gh: unsupported {rest}')
    json.dump(state, open(state_path, 'w'))
''')
SET_ORDER = [('variable', prk.VAR_TRUST), ('variable', prk.VAR_ROOT), ('secret', prk.SECRET_CATALOG),
             ('variable', prk.VAR_SERVER_KEYS), ('secret', prk.SECRET_SERVER)]


class ProvisionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='provision-test-')).resolve()
        self.addCleanup(shutil.rmtree, self.temp)
        self.assertIsNone(prk.git_work_tree_containing(self.temp), 'TMPDIR must be outside every git work tree')
        self.private = self.temp / 'private'
        self.workspace = self.temp / 'workspace'
        trust = self.workspace / 'flutter' / 'bin' / 'internal' / 'shorebird_artifact_trust.json'
        trust.parent.mkdir(parents=True)
        shutil.copy(ac.TRUST_FILE, trust)
        self.trust_store = trust
        # The server installer pieces --write-repo-pins rebuilds.
        for relative in ('deploy/linux/build_installer.py', 'deploy/linux/install_body.sh',
                         'deploy/linux/install_lib.py', 'deploy/linux/server.env.example',
                         'deploy/linux/server-update.json', 'deploy/linux/install.sh',
                         'deploy/systemd/shorebird-server.service', 'signing/server-update.trusted-keys'):
            target = self.workspace / 'shorebird-server' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SERVER / relative, target)
        bin_dir = self.temp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.gh_state = self.temp / 'gh-state.json'
        self.gh_log = self.temp / 'gh-log.jsonl'
        environment = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', 'FAKE_GH_STATE': str(self.gh_state),
                       'FAKE_GH_LOG': str(self.gh_log)}
        for key, value in environment.items():
            patcher = unittest.mock.patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop('OPEN_SHOREBIRD_PROVISION_REPO', None)

    def run_tool(self, *args, expect=0, private=None, repo='test/fork'):
        command = [args[0], '--private-dir', str(private or self.private), '--workspace', str(self.workspace),
                   '--trust-store', str(self.trust_store), *(['--repo', repo] if repo else []), *map(str, args[1:])]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prk.main(command)
        output = out.getvalue() + err.getvalue()
        self.assertEqual(code, expect, output)
        self.assert_no_secret_in(output)
        return output

    def gh_calls(self) -> list[dict]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def gh_sets(self) -> list[tuple[str, str]]:
        return [(c['argv'][0], c['argv'][2]) for c in self.gh_calls() if c['argv'][1] == 'set']

    def github(self, repo='test/fork') -> dict:
        return json.loads(self.gh_state.read_text())[repo] if self.gh_state.exists() else {'variables': {}, 'secrets': {}}

    def private_material(self) -> list[str]:
        """Every private key PEM body and raw seed encoding in the private directory."""
        values = []
        for path in self.private.rglob('*.pem'):
            text = path.read_text()
            if 'PRIVATE KEY' in text:
                body = ''.join(line for line in text.splitlines() if '-----' not in line)
                seed = ac.load_private_seed(path)
                values += [body, base64.b64encode(seed).decode(), seed.hex()]
        return values

    def assert_no_secret_in(self, text: str):
        if self.private.exists():
            for value in self.private_material():
                self.assertNotIn(value, text)
        self.assertNotIn('PRIVATE KEY', text)

    def key_files(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.private)): p.read_bytes() for p in sorted(self.private.rglob('*'))
                if p.is_file() and p.name != prk.STATE_FILE}

    def state(self) -> dict:
        return json.loads((self.private / prk.STATE_FILE).read_text())


class DevelopmentInitTest(ProvisionTestCase):
    def test_init_creates_every_key_once(self):
        output = self.run_tool('init', '--development')
        for role in prk.ALL_ROLES:
            self.assertIn(f'created {role}-1', output)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        for path in (self.private / 'keys').iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
            self.assertIn('DEVELOPMENT-ONLY', path.name)
        # Development directories start from the committed public test keys.
        catalog = next(k for k in self.state()['keys'] if k['role'] == 'artifact-catalog')
        self.assertEqual(catalog['public_key'], base64.b64encode(ac.load_public_key(ac.DEVELOPMENT_KEY)).decode())
        server = next(k for k in self.state()['keys'] if k['role'] == prk.SERVER_ROLE)
        self.assertEqual(server['public_key'], prk.SERVER_DEVELOPMENT_PUBLIC_KEY)
        bundle = json.loads((self.private / 'trust' / 'release-trust.json').read_bytes())
        trust = rt.verify_bundle(bundle, base64.b64decode(prk.compute_pins(prk.Store.open(
            self.private, create=False, mode=None))['gn_args']['shorebird_release_root']), at=prk.time_text(prk.now()))
        self.assertEqual(trust.delegations['version'], 1)
        with self.assertRaises(rt.TrustError):  # the development root never passes production checks
            rt.verify_bundle(bundle, trust.root_payload, production=True)

        # Rerun: nothing created, nothing re-signed, no key file touched.
        files = self.key_files()
        output = self.run_tool('init')
        self.assertNotIn('created', output)
        self.assertNotIn('signed', output)
        self.assertEqual(self.key_files(), files)

    def test_dry_run_never_writes_github_or_the_repository(self):
        output = self.run_tool('init', '--development')
        self.assertEqual(self.gh_sets(), [])
        self.assertEqual({c['argv'][1] for c in self.gh_calls()}, {'list'})
        for _, name in SET_ORDER:
            self.assertIn(name, output)
        self.assertIn('would create', output)
        self.assertIn('pass --apply-github', output)
        self.assertIn('would write', output)
        self.assertFalse((self.workspace / 'release-pins').exists())

    def test_apply_github_order_stdin_and_idempotency(self):
        self.run_tool('init', '--development', '--apply-github')
        self.assertEqual(self.gh_sets(), SET_ORDER)
        for call in self.gh_calls():
            argv = ' '.join(call['argv'])
            for value in self.private_material():
                self.assertNotIn(value, argv)
            if call['argv'][1] == 'set':
                self.assertIsNotNone(call['stdin_sha256'], 'values go through stdin')
                self.assertEqual(call['argv'][3:], ['--repo', 'test/fork'])
        github = self.github()
        state = self.state()
        catalog = next(k for k in state['keys'] if k['role'] == 'artifact-catalog')
        self.assertEqual(github['secrets'][prk.SECRET_CATALOG], (self.private / catalog['file']).read_text())
        self.assertEqual(json.loads(github['variables'][prk.VAR_TRUST]),
                         json.loads((self.private / 'trust' / 'release-trust.json').read_bytes()))
        self.assertEqual(github['variables'][prk.VAR_SERVER_KEYS], prk.SERVER_DEVELOPMENT_PUBLIC_KEY)

        # A second apply changes nothing on GitHub.
        before = len(self.gh_sets())
        output = self.run_tool('sync', '--apply-github')
        self.assertEqual(len(self.gh_sets()), before)
        self.assertNotIn('would create', output)
        self.assertNotIn('would update', output)
        self.assertEqual(output.count('unchanged'), 5)
        self.run_tool('verify', '--skip-repo')

    def test_development_material_never_reaches_the_default_repository(self):
        output = self.run_tool('init', '--development', '--apply-github', repo=None, expect=1)
        self.assertIn('never synced', output)
        self.assertEqual(self.gh_sets(), [])

    def test_missing_signed_key_is_never_recreated(self):
        self.run_tool('init', '--development', '--skip-github')
        manifest = next(k for k in self.state()['keys'] if k['role'] == 'build-manifest')
        (self.private / manifest['file']).unlink()
        output = self.run_tool('init', '--skip-github', expect=1)
        self.assertIn('restore it from your backup', output)
        self.assertFalse((self.private / manifest['file']).exists())

    def test_tampered_key_file_is_refused(self):
        self.run_tool('init', '--development', '--skip-github')
        root = next(k for k in self.state()['keys'] if k['role'] == 'root')
        path = self.private / root['file']
        path.chmod(0o644)
        self.assertIn('chmod 600', self.run_tool('status', '--skip-github', '--skip-repo', expect=0))
        self.run_tool('verify', '--skip-github', '--skip-repo', expect=1)
        path.chmod(0o600)
        path.write_text(prk.private_pem(bytes(32)))
        self.assertIn('does not hold', self.run_tool('init', '--skip-github', expect=1))

    def test_env_prints_paths_only(self):
        self.run_tool('init', '--development', '--skip-github')
        output = self.run_tool('env', '--skip-github')
        self.assertIn('SHOREBIRD_RELEASE_TRUST=', output)
        self.assertIn('SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY=', output)


class PrivateDirectoryTest(ProvisionTestCase):
    def test_refusals(self):
        repo = self.temp / 'checkout'
        (repo / '.git').mkdir(parents=True)
        self.assertIn('git work tree', self.run_tool('init', '--development', private=repo / 'keys', expect=1))
        self.assertIn('git work tree', self.run_tool('init', '--development', private=ROOT / 'build' / 'keys',
                                                     expect=1))
        loose = self.temp / 'loose'
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        self.assertIn('chmod 700', self.run_tool('init', '--development', private=loose, expect=1))
        link = self.temp / 'link'
        link.symlink_to(self.temp)
        self.assertIn('symlink', self.run_tool('init', '--development', private=link, expect=1))
        busy = self.temp / 'busy'
        busy.mkdir(mode=0o700)
        (busy / 'old.pem').write_text('x')
        self.assertIn('not empty', self.run_tool('init', '--development', private=busy, expect=1))
        self.assertIn('--production or --development', self.run_tool('init', expect=1))
        self.assertIn('run `init` first', self.run_tool('status', '--skip-github', expect=1))
        self.assertFalse(self.private.exists())


class ProductionTest(ProvisionTestCase):
    def test_production_and_development_never_mix(self):
        self.run_tool('init', '--development', '--skip-github')
        self.assertIn('DEVELOPMENT-ONLY material', self.run_tool('init', '--production', '--skip-github', expect=1))
        other = self.temp / 'production'
        self.run_tool('init', '--production', '--skip-github', private=other)
        self.assertIn('holds production keys', self.run_tool('init', '--development', '--skip-github',
                                                             private=other, expect=1))
        for path in (other / 'keys').iterdir():
            self.assertNotIn('DEVELOPMENT-ONLY', path.name)
        development = prk.development_public_keys()
        for record in json.loads((other / prk.STATE_FILE).read_text())['keys']:
            self.assertNotIn(record['public_key'], development)
        # A committed DEVELOPMENT-ONLY key is refused as a production external signer.
        third = self.temp / 'third'
        output = self.run_tool('init', '--production', '--skip-github', '--external',
                               f'artifact-catalog={ac.DEVELOPMENT_KEY}', private=third, expect=1)
        self.assertIn('DEVELOPMENT-ONLY', output)

    def test_production_pins_github_and_verify(self):
        output = self.run_tool('init', '--production', '--root-keys', '3', '--threshold', '2',
                               '--write-repo-pins', '--apply-github')
        self.assertIn('2-of-3', output)
        self.assertEqual(self.gh_sets(), SET_ORDER)
        pins = json.loads((self.workspace / 'release-pins' / 'release-pins.json').read_text())
        self.assertFalse(pins['development'])
        self.assertEqual(pins['toolchain_security']['release_root'], pins['gn_args']['shorebird_release_root'])
        self.assertEqual(self.github()['variables'][prk.VAR_ROOT], pins['gn_args']['shorebird_release_root'])
        gn = (self.workspace / 'release-pins' / 'release-root.gn').read_text()
        self.assertIn(f'shorebird_release_root = "{pins["gn_args"]["shorebird_release_root"]}"', gn)
        self.assertIn('shorebird_release_min_delegations_version = 1', gn)
        trust = json.loads(self.trust_store.read_text())
        self.assertEqual(ac.check_trust_store(trust), [])
        production = [k for k in trust['keys'] if k['usage'] == 'production']
        self.assertEqual([k['key_id'] for k in production], pins['artifact_catalog_key_ids'])
        server_keys = self.workspace / 'shorebird-server' / 'signing' / 'server-update.trusted-keys'
        self.assertIn(pins['server_update_public_keys'][0], server_keys.read_text())
        installer = self.workspace / 'shorebird-server' / 'deploy' / 'linux' / 'install.sh'
        self.assertIn(pins['server_update_public_keys'][0], installer.read_text())
        bundle = (self.workspace / 'release-pins' / 'release-trust.json').read_bytes()
        rt.verify_bundle(bundle, base64.b64decode(pins['gn_args']['shorebird_release_root']), production=True,
                         at=prk.time_text(prk.now()))
        self.assertIn('0 problem(s)', self.run_tool('verify'))
        # Idempotent: nothing more to write or set.
        sets = len(self.gh_sets())
        self.assertIn('repository pins: up to date', self.run_tool('init', '--write-repo-pins', '--apply-github'))
        self.assertEqual(len(self.gh_sets()), sets)

        # Disagreements are reported: a GitHub variable, a repository pin, a missing secret.
        state = json.loads(self.gh_state.read_text())
        state['test/fork']['variables'][prk.VAR_ROOT] = 'AAAA'
        del state['test/fork']['secrets'][prk.SECRET_SERVER]
        self.gh_state.write_text(json.dumps(state))
        (self.workspace / 'release-pins' / 'release-root.gn').write_text('tampered\n')
        output = self.run_tool('verify', expect=1)
        self.assertIn(f'variable {prk.VAR_ROOT} differs', output)
        self.assertIn(f'secret {prk.SECRET_SERVER} is not set', output)
        self.assertIn('release-root.gn differs', output)
        self.run_tool('status')  # status reports but exits 0

    def test_foreign_github_values_are_refused(self):
        self.gh_state.write_text(json.dumps({'test/fork': {'variables': {prk.VAR_ROOT: 'someone-elses-pin'},
                                                            'secrets': {prk.SECRET_SERVER: 'x'}}}))
        output = self.run_tool('init', '--production', '--apply-github', expect=1)
        self.assertIn(f'{prk.VAR_ROOT}: REFUSED', output)
        self.assertIn(f'{prk.SECRET_CATALOG}: skipped', output)
        self.assertIn(f'{prk.SECRET_SERVER}: REFUSED', output)
        self.assertEqual(self.gh_sets(), [('variable', prk.VAR_TRUST), ('variable', prk.VAR_SERVER_KEYS)])
        self.assertEqual(self.github()['variables'][prk.VAR_ROOT], 'someone-elses-pin')
        self.run_tool('sync', '--apply-github', '--force-github')
        self.assertEqual(self.gh_sets()[2:], [('variable', prk.VAR_ROOT), ('secret', prk.SECRET_CATALOG),
                                              ('secret', prk.SECRET_SERVER)])

    def test_failed_variable_stops_before_its_secret(self):
        with unittest.mock.patch.dict(os.environ, {'FAKE_GH_FAIL': 'variable set'}):
            output = self.run_tool('init', '--production', '--apply-github', expect=1)
        self.assertIn('gh variable set failed', output)
        self.assertEqual(self.gh_sets(), [('variable', prk.VAR_TRUST)])  # attempted, failed, nothing after it
        self.assertEqual(self.github()['variables'], {})
        self.assertNotIn(prk.SECRET_CATALOG, self.github()['secrets'])

    def test_release_token_goes_through_stdin(self):
        token = self.temp / 'token'
        token.write_text('ghp_not_a_real_token_value\n')
        token.chmod(0o600)
        output = self.run_tool('init', '--production', '--apply-github', '--server-release-token-file', token)
        self.assertNotIn('ghp_not_a_real_token_value', output)
        self.assertEqual(self.github()['secrets'][prk.SECRET_RELEASE_TOKEN], 'ghp_not_a_real_token_value')
        self.assertNotIn('ghp_', self.gh_log.read_text())
        token.chmod(0o644)
        self.assertIn('chmod 600', self.run_tool('sync', '--apply-github', '--server-release-token-file', token,
                                                 expect=1))

    def test_external_signers(self):
        kms = self.temp / 'kms'
        kms.mkdir()
        for name, seed in (('manifest', 41), ('server', 42)):
            (kms / f'{name}.pub.pem').write_text(prk.public_pem(ac.ed25519_public_key(bytes([seed]) * 32)))
        self.run_tool('init', '--production', '--external', f'build-manifest={kms / "manifest.pub.pem"}',
                      '--external', f'server-update={kms / "server.pub.pem"}', '--apply-github')
        records = {k['role']: k for k in self.state()['keys']}
        self.assertIsNone(records['build-manifest']['file'])
        self.assertIsNone(records[prk.SERVER_ROLE]['file'])
        self.assertNotIn(prk.SECRET_SERVER, self.github()['secrets'])
        self.assertIn(prk.SECRET_CATALOG, self.github()['secrets'])
        self.assertEqual(self.github()['variables'][prk.VAR_SERVER_KEYS],
                         base64.b64encode(ac.ed25519_public_key(bytes([42]) * 32)).decode())
        output = self.run_tool('env', '--skip-github')
        self.assertIn('SHOREBIRD_BUILD_MANIFEST_SIGNER_KEY_ID=' + records['build-manifest']['key_id'], output)
        private = kms / 'private.pem'
        private.write_text(prk.private_pem(bytes([43]) * 32))
        self.assertIn('only its public PEM', self.run_tool('rotate', 'build-manifest', '--external', private,
                                                           '--skip-github', expect=1))
        self.assertIn('is a production feature', self.run_tool(
            'init', '--development', '--skip-github', '--external', f'build-manifest={kms / "manifest.pub.pem"}',
            private=self.temp / 'dev', expect=1))


class RotationTest(ProvisionTestCase):
    def bundle_and_pin(self):
        store = prk.Store.open(self.private, create=False, mode=None)
        return store.bundle(), store.pin_bytes(), store.state

    def test_delegate_revocation_raises_the_floor(self):
        self.run_tool('init', '--development', '--apply-github', '--write-repo-pins')
        old = next(k for k in self.state()['keys'] if k['role'] == 'artifact-catalog')
        self.run_tool('rotate', 'artifact-catalog', '--revoke', '--raise-floor', '--apply-github', '--write-repo-pins')
        bundle, pin, state = self.bundle_and_pin()
        trust = rt.verify_bundle(bundle, pin, 2, at=prk.time_text(prk.now()))
        self.assertEqual(trust.delegations['version'], 2)
        self.assertEqual(trust.delegations['revoked_key_ids'], [old['key_id']])
        with self.assertRaises(rt.TrustError) as caught:
            trust.role_key('artifact-catalog', old['key_id'])
        self.assertEqual(caught.exception.code, 'signer_revoked')
        self.assertTrue((self.private / 'retired' / Path(old['file']).name).exists())
        pins = json.loads((self.workspace / 'release-pins' / 'release-pins.json').read_text())
        self.assertEqual(pins['gn_args']['shorebird_release_min_delegations_version'], 2)
        store_ids = {k['key_id'] for k in json.loads(self.trust_store.read_text())['keys']}
        self.assertNotIn(old['key_id'], store_ids)
        self.assertIn(pins['artifact_catalog_key_ids'][0], store_ids)
        # The catalog secret and the trust variable follow; the root pin does not change.
        self.assertEqual(self.gh_sets()[5:], [('variable', prk.VAR_TRUST), ('secret', prk.SECRET_CATALOG)])
        self.run_tool('verify')
        # A later delegations version keeps the revocation (release_trust.py rule).
        self.run_tool('renew', '--skip-github')
        bundle, pin, _ = self.bundle_and_pin()
        self.assertEqual(rt.verify_bundle(bundle, pin, 2).delegations['revoked_key_ids'], [old['key_id']])

    def test_planned_root_rotation_keeps_the_pin(self):
        self.run_tool('init', '--development', '--skip-github')
        first_pin = self.bundle_and_pin()[1]
        output = self.run_tool('rotate', 'root', '--root-keys', '2', '--threshold', '2', '--skip-github')
        self.assertIn('pinned version 1', output)
        bundle, pin, state = self.bundle_and_pin()
        self.assertEqual(pin, first_pin)
        trust = rt.verify_bundle(bundle, pin, at=prk.time_text(prk.now()))
        self.assertEqual((trust.root['version'], trust.root['threshold'], trust.delegations['root_version']), (2, 2, 2))
        self.assertEqual(len(bundle['root_chain']), 2)
        self.run_tool('rotate', 'root', '--repin', '--skip-github')
        bundle, pin, state = self.bundle_and_pin()
        self.assertEqual(rt.parse_payload(pin, 'release-root')['version'], 3)
        self.run_tool('verify', '--skip-github', '--skip-repo')

    def test_server_update_rotation_stages(self):
        self.run_tool('init', '--development', '--apply-github')
        first = self.github()['variables'][prk.VAR_SERVER_KEYS]
        self.assertIn('run --stage add first', self.run_tool('rotate', 'server-update', '--stage', 'activate',
                                                             '--skip-github', expect=1))
        self.run_tool('rotate', 'server-update', '--stage', 'add', '--apply-github')
        keys = self.github()['variables'][prk.VAR_SERVER_KEYS].split(',')
        self.assertEqual(keys[0], first)
        self.assertEqual(len(keys), 2)
        self.assertEqual(self.gh_sets()[-1], ('variable', prk.VAR_SERVER_KEYS))
        self.run_tool('rotate', 'server-update', '--stage', 'activate', '--apply-github')
        self.assertEqual(self.gh_sets()[-1], ('secret', prk.SECRET_SERVER))
        self.run_tool('rotate', 'server-update', '--stage', 'retire', '--apply-github')
        self.assertEqual(self.github()['variables'][prk.VAR_SERVER_KEYS], keys[1])
        self.run_tool('verify', '--skip-repo')

    def test_development_pins_never_replace_production_pins(self):
        production = self.temp / 'production'
        self.run_tool('init', '--production', '--skip-github', '--write-repo-pins', private=production)
        output = self.run_tool('init', '--development', '--skip-github', '--write-repo-pins', expect=1)
        self.assertIn('DEVELOPMENT-ONLY pins never replace them', output)


class WrapperTest(unittest.TestCase):
    def test_shell_wrapper(self):
        result = subprocess.run([str(ROOT / 'scripts' / 'provision_release_keys.sh'), '--help'],
                                capture_output=True, text=True, check=True)
        self.assertIn('init', result.stdout)
        self.assertIn('--apply-github', subprocess.run(
            [str(ROOT / 'scripts' / 'provision_release_keys.sh'), 'init', '--help'],
            capture_output=True, text=True, check=True).stdout)


if __name__ == '__main__':
    unittest.main()

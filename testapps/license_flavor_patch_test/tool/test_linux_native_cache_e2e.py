"""Pure Linux harness contracts; no Linux runtime, server, SDK or build invoked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import select
import sqlite3
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('linux_native_cache_e2e',
    Path(__file__).with_name('linux_native_cache_e2e.py'))
e2e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e2e)


def context():
    return {'app_id': '7cd314d0-8ace-46a2-b21b-81a92bcd0738', 'release_id': 17,
            'patch_number': 3, 'patch_hash': 'a' * 64, 'transport_hash': 'b' * 64,
            'platform': 'linux', 'arch': 'x86_64', 'device_id_hash': 'c' * 64}


class ProtocolTest(unittest.TestCase):
    def test_linux_exact_context_and_independent_go_kdf_vector(self):
        aad = e2e.canonical_aad(context())
        self.assertEqual(e2e.parse_aad(aad), {k: str(v) for k, v in context().items()})
        self.assertIn('\nplatform=linux\narch=x86_64\n', aad)
        key_id, key = e2e.delivery_material(b'fixture-secret', aad)
        import hashlib
        import hmac
        expected = hmac.new(b'fixture-secret', b'key\0' + aad.encode(), hashlib.sha256).digest()
        self.assertEqual(key, expected)
        self.assertEqual(len(key_id), 16)
        authority = e2e.KeyAuthority(b'fixture-secret', 'private-token')
        request = json.dumps({'key_id': key_id, 'aad': aad}).encode()
        with self.assertRaises(ValueError):
            authority.provision('Bearer private-token', request)
        authority.allow(context())
        self.assertEqual(authority.provision('Bearer private-token', request), key.hex().encode())
        for authorization, body in ((None, request), ('Bearer wrong', request),
                ('Bearer private-token', b'{"aad":"x","aad":"y","key_id":"z"}'),
                ('Bearer private-token', request.replace(b'patch_number=3', b'patch_number=4'))):
            with self.assertRaises(ValueError):
                authority.provision(authorization, body)

    def test_platform_arch_and_canonical_context_rejections(self):
        aad = e2e.canonical_aad(context())
        for value in (aad.replace('platform=linux', 'platform=windows'),
                      aad.replace('arch=x86_64', 'arch=arm64'), aad + '\n',
                      aad.replace('release_id=17', 'release_id=017'),
                      aad.replace('app_id=7cd', 'app_id=7CD'), None, []):
            with self.subTest(value=repr(value)[:60]), self.assertRaises(ValueError):
                e2e.parse_aad(value)


class ReceiptTest(unittest.TestCase):
    def test_exact_post_frame_real_process_and_patch_receipt(self):
        report = {'schema': e2e.SCHEMA, 'nonce': 'fresh', 'pid': 42,
                  'post_frame': True, 'rendered': ['license:pro', 'pro-feature:enabled'], 'current_patch': 3}
        self.assertEqual(e2e.validate_report(report, 'fresh', 42, 3, 'U'), report)
        for field, value in (('schema', e2e.portable.SCHEMA), ('nonce', 'stale'), ('pid', True),
                             ('pid', 41), ('post_frame', False), ('current_patch', 0),
                             ('current_patch', True), ('rendered', ['license:pro'])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                e2e.validate_report(dict(report, **{field: value}), 'fresh', 42, 3, 'U')
        with self.assertRaises(ValueError):
            e2e.validate_report(dict(report, extra=True), 'fresh', 42, 3, 'U')

    def test_linux_receipt_uses_real_export_and_rendered_objects(self):
        source = e2e.DART_FIXTURE
        for marker in ('addPostFrameCallback', 'object is! RenderParagraph',
                       'object.text.toPlainText()', 'DynamicLibrary.process()',
                       'shorebird_current_boot_patch_number', e2e.SCHEMA):
            self.assertIn(marker, source)
        self.assertNotIn('flutter_windows.dll', source)


class OwnershipTest(unittest.TestCase):
    def test_spawn_owns_new_group_before_return_and_forbids_overrides(self):
        owner = e2e.OwnedProcessGroups()
        child = mock.Mock(pid=123, reaped=False)
        with mock.patch.object(e2e, 'RetainedGroupProcess', return_value=child) as spawn:
            self.assertIs(owner.spawn(['owned'], cwd='/private'), child)
        spawn.assert_called_once_with(['owned'], start_new_session=True, cwd='/private')
        self.assertIs(owner.owned[123], child)
        for settings in ({'start_new_session': False}, {'preexec_fn': lambda: None}):
            with self.assertRaises(e2e.FixtureError):
                owner.spawn(['owned'], **settings)

    def test_kills_exact_pinned_group_then_reaps_and_never_signals_unowned_group(self):
        owner = e2e.OwnedProcessGroups()
        child = mock.Mock(pid=123, reaped=False)
        owner.owned[123] = child
        calls = []
        child.reap.side_effect = lambda: calls.append('reap')
        with mock.patch.object(e2e.os, 'killpg', side_effect=lambda group, sig: calls.append((group, sig))), \
                mock.patch.object(owner, 'live_members', return_value=[]):
            owner.stop(child)
        self.assertEqual(calls, [(123, signal.SIGTERM), (123, signal.SIGKILL), 'reap'])
        self.assertFalse(owner.owned)
        with mock.patch.object(e2e.os, 'killpg') as kill, self.assertRaises(e2e.FixtureError):
            owner.stop(mock.Mock(pid=456, reaped=False))
        kill.assert_not_called()

    def test_term_timeout_escalates_and_remains_bounded(self):
        owner = e2e.OwnedProcessGroups()
        child = mock.Mock(pid=123, reaped=False)
        child.wait.side_effect = [subprocess.TimeoutExpired('owned', 5), 0]
        owner.owned[123] = child
        with mock.patch.object(owner, '_signal', return_value=False) as send, \
                mock.patch.object(owner, 'live_members', return_value=[]):
            owner.stop(child)
        self.assertEqual(send.call_args_list, [mock.call(123, signal.SIGTERM),
            mock.call(123, signal.SIGKILL), mock.call(123, signal.SIGKILL)])
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=5), mock.call(timeout=5)])

    @unittest.skipUnless(e2e.platform.system() == 'Linux', 'Linux /proc retained-group runtime check')
    def test_real_benign_python_parent_and_child_group_are_cleaned_up(self):
        owner = e2e.OwnedProcessGroups()
        # The parent reaps its child on TERM, avoiding orphan zombies while
        # checking that the manager's real group signal reaches both processes.
        script = """import signal, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(*_):
    child.wait(timeout=3)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(child.pid, flush=True)
signal.pause()
"""
        process = owner.spawn([sys.executable, '-c', script], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'child readiness timeout')
            descendant = int(process.stdout.readline())
            self.assertEqual(os.getpgid(descendant), process.pid)
            owner.stop(process)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.kill(descendant, 0)
            self.assertFalse(owner.owned)
        finally:
            owner.close()
            process.stdout.close()

    def test_still_live_group_fails_closed_and_retains_ownership(self):
        owner = e2e.OwnedProcessGroups()
        child = mock.Mock(pid=123, reaped=False)
        owner.owned[123] = child
        with mock.patch.object(e2e.os, 'killpg'), \
                mock.patch.object(owner, 'live_members', return_value=[123]), \
                mock.patch.object(e2e.time, 'monotonic', side_effect=[0, 6]), \
                self.assertRaisesRegex(e2e.FixtureError, 'still exists'):
            owner.stop(child)
        self.assertIs(owner.owned[123], child)

    def test_observing_exit_does_not_reap_and_explicit_reap_is_last(self):
        process = object.__new__(e2e.RetainedGroupProcess)
        process.pid, process.args, process.returncode = 123, ['owned'], None
        process.reaped, process._child_created = False, False
        process._waitpid_lock = threading.Lock()
        status = mock.Mock(si_code=e2e.os.CLD_EXITED, si_status=0)
        with mock.patch.object(e2e.os, 'waitid', return_value=status) as observe, \
                mock.patch.object(e2e.os, 'waitpid', return_value=(123, 0)) as reap:
            self.assertEqual(process.poll(), 0)
            self.assertEqual(process.wait(timeout=1), 0)
            observe.assert_called_once_with(e2e.os.P_PID, 123,
                e2e.os.WEXITED | e2e.os.WNOHANG | e2e.os.WNOWAIT)
            reap.assert_not_called()
            self.assertFalse(process.reaped)
            process.reap()
            reap.assert_called_once_with(123, 0)
            self.assertTrue(process.reaped)

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'WNOWAIT'), 'POSIX waitid required')
    def test_real_python_communicate_retains_leader_until_owned_group_cleanup(self):
        # waitid WNOWAIT is also available on the authoring Mac. This checks
        # stdlib communicate integration without a server, SDK or Linux runtime.
        owner = e2e.OwnedProcessGroups()
        with mock.patch.object(e2e.platform, 'system', return_value='Linux'):
            process = owner.spawn([sys.executable, '-c', 'print("benign")'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _ = process.communicate(timeout=5)
            self.assertEqual(output, b'benign\n')
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertEqual(status.si_pid, process.pid)
            self.assertFalse(process.reaped)
            # This child creates no descendants. Linux /proc membership scanning
            # is covered separately and by the Linux-only group runtime test.
            # Darwin cannot signal this retained zombie group (EPERM), so only
            # signal delivery is mocked here; waitid and final reap stay real.
            with mock.patch.object(owner, 'live_members', return_value=[]), \
                    mock.patch.object(owner, '_signal', return_value=False):
                owner.stop(process)
            self.assertTrue(process.reaped)
            with self.assertRaises(ChildProcessError):
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        finally:
            with mock.patch.object(owner, 'live_members', return_value=[]), \
                    mock.patch.object(owner, '_signal', return_value=False):
                owner.close()
            process.stdout.close()
            process.stderr.close()

    def test_reaped_leader_is_never_used_to_signal_numeric_group(self):
        owner = e2e.OwnedProcessGroups()
        process = mock.Mock(pid=123, reaped=True)
        owner.owned[123] = process
        with mock.patch.object(e2e.os, 'killpg') as kill, self.assertRaises(e2e.FixtureError):
            owner.stop(process)
        kill.assert_not_called()

    def test_linux_proc_scan_ignores_retained_zombie_and_handles_comm_parentheses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for pid, name, state, group in ((123, 'leader', 'Z', 123),
                    (124, 'child (named)', 'S', 123), (125, 'unrelated', 'S', 125)):
                directory = root / str(pid)
                directory.mkdir()
                (directory / 'stat').write_text(f'{pid} ({name}) {state} 1 {group} 0 0')
            with mock.patch.object(e2e, 'Path', return_value=root) as proc:
                self.assertEqual(e2e.OwnedProcessGroups.live_members(123), [124])
            proc.assert_called_once_with('/proc')

    def test_keyring_is_fresh_anonymous_possessor_only_and_revoked(self):
        library = mock.Mock()
        library.keyctl_join_session_keyring.return_value = 91
        library.keyctl_get_keyring_ID.return_value = 91
        for name in ('keyctl_clear', 'keyctl_setperm', 'keyctl_revoke'):
            getattr(library, name).return_value = 0
        with mock.patch.object(e2e.ctypes.util, 'find_library', return_value='libkeyutils.so.1'), \
                mock.patch.object(e2e.ctypes, 'CDLL', return_value=library):
            ring = e2e.OwnedSessionKeyring()
            library.keyctl_join_session_keyring.assert_called_once_with(None)
            library.keyctl_clear.assert_called_once_with(91)
            library.keyctl_setperm.assert_called_once_with(91, 0x3f000000)
            library.keyctl_get_keyring_ID.assert_called_once_with(-3, 0)
            ring.close()
            ring.close()
            library.keyctl_revoke.assert_called_once_with(91)

    def test_keyring_failure_revokes_created_ring_and_never_falls_back(self):
        library = mock.Mock()
        library.keyctl_join_session_keyring.return_value = 91
        library.keyctl_clear.return_value = -1
        library.keyctl_revoke.return_value = 0
        with mock.patch.object(e2e.ctypes.util, 'find_library', return_value='keyutils'), \
                mock.patch.object(e2e.ctypes, 'CDLL', return_value=library), self.assertRaises(e2e.FixtureError):
            e2e.OwnedSessionKeyring()
        library.keyctl_revoke.assert_called_once_with(91)


class EnvironmentAndInjectionTest(unittest.TestCase):
    def test_cleanup_records_redacted_action_failures_and_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / 'private'
            work.mkdir()
            lifecycle = e2e.Lifecycle(mock.Mock(work=work))
            lifecycle.work_owned = True
            lifecycle.job.close = mock.Mock(side_effect=e2e.FixtureError('private process details'))
            lifecycle.ring = mock.Mock()
            lifecycle.logs = [mock.Mock()]
            # Model a private Go module directory that cannot be deleted. Keep
            # the error path secret and preserve process cleanup failures too.
            with mock.patch.object(e2e.shutil, 'rmtree', side_effect=PermissionError('private module path')), \
                    self.assertRaisesRegex(e2e.FixtureError, 'Owned resource cleanup failed'):
                lifecycle.cleanup()
            self.assertEqual(lifecycle.manifest['cleanup_failures'], [
                {'action': 'process-groups', 'type': 'FixtureError'},
                {'action': 'work-tree', 'type': 'PermissionError'}])
            lifecycle.ring.close.assert_called_once_with()
            lifecycle.logs[0].close.assert_called_once_with()
            self.assertNotIn('private', json.dumps(lifecycle.manifest))

    def lifecycle(self, root):
        args = mock.Mock(arch='x64', work=root, provider_probe=root / 'probe', build_timeout=30)
        lifecycle = e2e.Lifecycle(args)
        lifecycle.ring = mock.Mock(serial=91)
        return lifecycle

    def test_registration_reads_engine_app_namespace_for_both_architectures(self):
        # Only the filesystem/SQL observation path runs here. The simulated
        # Runner writes where fl_shorebird + ConfigureShorebird put real state;
        # no app, API, keyring, or build is started by this regression test.
        class RegistrationObserved(Exception):
            pass

        for arch, runtime in (('x64', 'x86_64'), ('arm64', 'aarch64')):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for name in ('home', 'app'):
                    (root / name).mkdir()
                lifecycle = self.lifecycle(root)
                lifecycle.args.arch = arch
                lifecycle.args.runtime_timeout = .2
                app_id = context()['app_id']
                lifecycle.request = mock.Mock(side_effect=[
                    {'token': 'unit-token'}, {'id': 1}, {'id': app_id}])
                expected = root / 'home/.shorebird_cache/shorebird_updater' / app_id
                with contextlib.closing(sqlite3.connect(root / 'server.sqlite')) as database:
                    database.execute('CREATE TABLE patch_events (app_id, client_id, event_type, arch, platform, release_version)')
                    database.execute('INSERT INTO patch_events VALUES (?, ?, ?, ?, ?, ?)',
                        (app_id, 'unit-updater-client', 'PatchCheck', runtime, 'linux', '1.0.0+1'))
                    database.commit()

                def build(patch=False):
                    if patch:
                        raise RegistrationObserved
                    lifecycle.release_version = '1.0.0+1'

                def launch(*_args, **_kwargs):
                    expected.mkdir(parents=True, exist_ok=True)
                    (expected / 'state.json').write_text(json.dumps({'client_id': 'unit-updater-client'}))

                with contextlib.ExitStack() as stack:
                    connect = sqlite3.connect
                    def owned_connection(*args, **kwargs):
                        database = connect(*args, **kwargs)
                        stack.callback(database.close)
                        return database
                    stack.enter_context(mock.patch.object(sqlite3, 'connect', side_effect=owned_connection))
                    for name in ('prepare', 'start_api', 'make_observer', 'probe_provider', 'stop_app'):
                        stack.enter_context(mock.patch.object(lifecycle, name))
                    stack.enter_context(mock.patch.object(e2e, 'LocalService'))
                    stack.enter_context(mock.patch.object(lifecycle, 'build', side_effect=build))
                    stack.enter_context(mock.patch.object(lifecycle, 'launch', side_effect=launch))
                    rows = stack.enter_context(mock.patch.object(lifecycle, 'rows', wraps=lifecycle.rows))
                    with self.assertRaises(RegistrationObserved):
                        lifecycle.execute()
                    self.assertEqual(rows.call_args.args[1], (app_id, 'unit-updater-client', 'PatchCheck'))
                    lifecycle.stop_app.assert_called_once_with()
                self.assertEqual(lifecycle.cache, expected)
                self.assertFalse((expected.parent.parent / 'state.json').exists())

    def test_v1_and_inherited_v2_bootstrap_own_only_empty_app_cache_inside_work(self):
        import linux_variant_e2e
        for kind in (e2e.Lifecycle, linux_variant_e2e.Lifecycle):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                work = root / 'owned'
                (work / 'home').mkdir(parents=True)
                (work / 'app').mkdir()
                unrelated = root / 'unrelated'
                unrelated.write_text('preserve')
                lifecycle = kind(mock.Mock(arch='x64', work=work))
                lifecycle.work_owned = True
                app_id = context()['app_id']
                lifecycle.request = mock.Mock(side_effect=[
                    {'token': 'unit-token'}, {'id': 1}, {'id': app_id}])
                lifecycle.bootstrap()
                expected = work / 'home/.shorebird_cache/shorebird_updater' / app_id
                self.assertEqual(lifecycle.cache, expected)
                self.assertTrue(lifecycle.cache_owned)
                self.assertEqual(list(expected.iterdir()), [])
                self.assertEqual(expected.stat().st_mode & 0o777, 0o700)
                self.assertIn('app_id: ' + app_id + '\n', (work / 'app/shorebird.yaml').read_text())
                lifecycle.cleanup()
                self.assertFalse(work.exists())
                self.assertEqual(unrelated.read_text(), 'preserve')

    def test_bootstrap_refuses_existing_app_cache_without_claiming_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'app').mkdir()
            app_id = context()['app_id']
            expected = root / 'home/.shorebird_cache/shorebird_updater' / app_id
            expected.mkdir(parents=True)
            sentinel = expected / 'state.json'
            sentinel.write_text('preserve')
            lifecycle = self.lifecycle(root)
            lifecycle.request = mock.Mock(side_effect=[
                {'token': 'unit-token'}, {'id': 1}, {'id': app_id}])
            with self.assertRaisesRegex(e2e.FixtureError, 'Refusing existing updater app cache'):
                lifecycle.bootstrap()
            self.assertFalse(lifecycle.cache_owned)
            self.assertIsNone(lifecycle.cache)
            self.assertEqual(sentinel.read_text(), 'preserve')

    def test_private_runtime_home_and_no_inherited_provisioning_proxy_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.dict(os.environ, {'SHOREBIRD_TOKEN': 'secret',
                    'SHOREBIRD_E2E_PROVISION_URL': 'http://evil/key', 'SHOREBIRD_E2E_PROVISION_TOKEN': 'secret',
                    'HTTP_PROXY': 'http://evil', 'PATCH_DELIVERY_AES_SECRET': 'secret'}):
                lifecycle = self.lifecycle(root)
                env = lifecycle.runtime_environment(mode='wrong')
            self.assertEqual(env['HOME'], str(root / 'home'))
            self.assertEqual(env['XDG_CACHE_HOME'], str(root / 'xdg-cache'))
            self.assertEqual(env['XDG_RUNTIME_DIR'], str(root / 'xdg-runtime'))
            self.assertEqual(env['PUB_CACHE'], str(root / 'pub-cache'))
            self.assertEqual(env['SHOREBIRD_E2E_KEYRING_ID'], '91')
            self.assertEqual(env['SHOREBIRD_E2E_KEY_MODE'], 'wrong')
            for name in ('SHOREBIRD_TOKEN', 'SHOREBIRD_E2E_PROVISION_URL',
                         'SHOREBIRD_E2E_PROVISION_TOKEN', 'HTTP_PROXY', 'PATCH_DELIVERY_AES_SECRET',
                         'SHOREBIRD_E2E_KEY_CACHE_DIR'):
                self.assertNotIn(name, env)

    def test_prepare_copies_host_generated_package_before_vended_sdk_and_tools(self):
        for stale_metadata in (False, True):
            with self.subTest(stale_metadata=stale_metadata):
                self.assert_prepared_toolchain(stale_metadata)

    def assert_prepared_toolchain(self, stale_metadata):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout, work, host = root / 'checkout', root / 'work', root / 'engine/out/host'
            for name in ('shorebird/bin/internal', 'shorebird-server', 'flutter/bin/internal',
                         'flutter/bin/cache/dart-sdk', 'flutter/bin/cache/pkg/sky_engine'):
                (checkout / name).mkdir(parents=True)
            engine_id = 'e15e' + 'a' * 36
            (checkout / 'flutter/bin/internal/engine.version').write_text(engine_id + '\n')
            if stale_metadata:
                (checkout / 'flutter/bin/cache/engine.stamp').write_text('0' * 40 + '\n')
                (checkout / 'flutter/bin/cache/engine.realm').write_text('stale-presubmit-realm\n')
            (checkout / 'flutter/bin/cache/pkg/sky_engine/stale').write_text('old')
            generated = host / 'gen/dart-pkg/sky_engine'
            generated.mkdir(parents=True)
            (generated / 'pubspec.yaml').write_text('name: sky_engine\n')
            (host / 'dart-sdk').mkdir()
            pub = root / 'pub'
            pub.mkdir()
            fixture = root / 'fixture'
            (fixture / 'lib').mkdir(parents=True)
            (fixture / 'pubspec.yaml').write_text('name: fixture\nversion: 0.0.0\n')
            patch = root / 'patch'
            patch.write_bytes(b'prebuilt patch tool')
            args = mock.Mock(arch='x64', work=work, engine_src=root / 'engine', host_engine='host',
                             target_engine='linux_release', pub_cache=pub, patch_tool=patch)
            lifecycle = e2e.Lifecycle(args)
            def snapshot(source, target):
                shutil.copytree(source, target)
                lifecycle.manifest.setdefault('sources', {})[source.name] = {'head': 'flutter-ref'}
            lifecycle.snapshot = snapshot
            lifecycle.remap_config = mock.Mock()
            foreign_cache = root / 'foreign-go-modules'
            foreign_cache.mkdir()
            (foreign_cache / 'preserve').write_text('foreign module cache')
            operations = []
            def stop_before_tools(command, **kwargs):
                operations.append(kwargs['name'])
                if kwargs['name'] == 'compile-server':
                    self.assertEqual(command[:3], ['go', 'build', '-modcacherw'])
                    self.assertEqual(kwargs['env']['GOPATH'], str(work / 'go-path'))
                    self.assertEqual(kwargs['env']['GOMODCACHE'], str(work / 'go-path/pkg/mod'))
                    self.assertEqual(kwargs['env']['GOCACHE'], str(work / 'go-cache'))
                    self.assertEqual(kwargs['env']['GOFLAGS'], '-trimpath')
                    return
                self.assertIn(kwargs['name'], ('compile-cli', 'generate-linux'))
                for sdk in (work / 'flutter', work / 'cli/bin/cache/flutter/flutter-ref'):
                    self.assertEqual((sdk / 'bin/cache/engine.stamp').read_text(), engine_id + '\n')
                    self.assertEqual((sdk / 'bin/cache/engine.realm').read_text(), '\n')
                    self.assertEqual((sdk / 'bin/internal/engine.version').read_text().strip(), engine_id)
                    package = sdk / 'bin/cache/pkg/sky_engine'
                    self.assertEqual(e2e.tree_hashes(package), e2e.tree_hashes(generated))
                    self.assertFalse((package / 'stale').exists())
                    self.assertFalse(os.path.samefile(package / 'pubspec.yaml', generated / 'pubspec.yaml'))
                for key in ('FLUTTER_STORAGE_BASE_URL', 'SHOREBIRD_FLUTTER_STORAGE_BASE_URL'):
                    self.assertEqual(kwargs['env'][key], 'https://storage.googleapis.com')
                if kwargs['name'] == 'generate-linux':
                    self.assertEqual(command, e2e.create_linux_command(args, work))
                    raise RuntimeError('stop before external tools')
            lifecycle.run_command = stop_before_tools
            with mock.patch.object(e2e, 'ROOT', checkout), \
                    mock.patch.object(e2e, 'FIXTURE', fixture), \
                    mock.patch.object(e2e, 'OwnedSessionKeyring'), \
                    mock.patch.dict(os.environ, {'FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid',
                        'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': 'http://untrusted.invalid',
                        'GOMODCACHE': str(foreign_cache), 'GOFLAGS': '-trimpath'}), \
                    self.assertRaisesRegex(RuntimeError, 'stop before external tools'):
                lifecycle.prepare()
            self.assertEqual(operations, ['compile-cli', 'compile-server', 'generate-linux'])
            self.assertEqual(e2e.tree_hashes(foreign_cache), {
                'preserve': e2e.portable.sha256(b'foreign module cache')})
            self.assertEqual((work / 'cli/bin/internal/engine.version').read_text(), engine_id + '\n')
            if stale_metadata:
                self.assertEqual((checkout / 'flutter/bin/cache/engine.stamp').read_text(), '0' * 40 + '\n')
                self.assertEqual((checkout / 'flutter/bin/cache/engine.realm').read_text(), 'stale-presubmit-realm\n')
            else:
                self.assertFalse((checkout / 'flutter/bin/cache/engine.stamp').exists())
                self.assertFalse((checkout / 'flutter/bin/cache/engine.realm').exists())
            self.assertEqual((checkout / 'flutter/bin/cache/pkg/sky_engine/stale').read_text(), 'old')
            self.assertEqual((generated / 'pubspec.yaml').read_text(), 'name: sky_engine\n')
            self.assertFalse((host / 'gen/dart-pkg/flutter_gpu').exists())

    def test_failed_and_timed_out_command_diagnostics_survive_private_cleanup(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temp:
                work = Path(temp) / 'private'
                work.mkdir()
                lifecycle = self.lifecycle(work)
                lifecycle.work_owned = True
                lifecycle.job = mock.Mock()
                payload = (b'could not find package sky_engine in the Flutter SDK\n'
                           b'Bearer private-token https://private.invalid/?sig=secret password=private-password')
                process = mock.Mock(returncode=17)
                process.communicate.side_effect = subprocess.TimeoutExpired('private-command', 1) if timeout else None
                def spawn(*_args, **kwargs):
                    kwargs['stdout'].write(payload)
                    return process
                lifecycle.job.spawn.side_effect = spawn
                with self.assertRaises(e2e.FixtureError):
                    lifecycle.run_command(['private-command', 'private-argument'], name='generate-linux')
                lifecycle.job.stop.assert_called_once_with(process)
                evidence = lifecycle.manifest['command_failure']
                self.assertEqual(evidence['exit_code'], None if timeout else 17)
                self.assertEqual(evidence['timed_out'], timeout)
                self.assertEqual(evidence['classification'], 'missing_sky_engine')
                self.assertEqual(evidence['log_sha256'], e2e.sha256(payload))
                self.assertEqual(evidence['exception_types'], [])
                self.assertEqual(evidence['known_messages'], [])
                lifecycle.cleanup()
                self.assertFalse(work.exists())
                encoded = json.dumps(lifecycle.manifest)
                for secret in ('private-token', 'private.invalid', 'private-password', 'private-command', 'private-argument'):
                    self.assertNotIn(secret, encoded)

    def test_staged_toolchain_blocks_unselected_precache_and_shell_sdk_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flutter, vended = root / 'flutter', root / 'vended'
            for directory in (flutter, vended):
                (directory / 'bin').mkdir(parents=True)
            e2e.stage_local_launchers(flutter, vended)
            for directory in (flutter, vended):
                wrapper = (directory / 'bin/flutter').read_text()
                self.assertIn('flutter_tools.dart', wrapper)
                self.assertIn('cache/dart-sdk/bin/dart', wrapper)
                self.assertNotIn('shared.sh', wrapper)
                self.assertNotIn('shared.sh', (directory / 'bin/dart').read_text())
                self.assertEqual((directory / 'bin/flutter').stat().st_mode & 0o777, 0o700)
            self.assertTrue((vended / '.shorebird_precache').is_file())
            # Pin the actual CLI guard, so a future marker migration fails the
            # contract instead of silently re-enabling unrelated precache.
            source = (e2e.ROOT / 'shorebird/packages/shorebird_cli/lib/src/shorebird_flutter.dart').read_text()
            self.assertIn("precacheStampName = '.shorebird_precache'", source)

    def test_generate_selects_local_engine_before_create(self):
        args = mock.Mock(arch='x64', engine_src=Path('/engine/src'), host_engine='host_release', target_engine='linux_release')
        command = e2e.create_linux_command(args, Path('/private'))
        for flag in e2e.local_engine_flags(args):
            self.assertLess(command.index(flag), command.index('create'))
        self.assertEqual(command[-4:], ['create', '--platforms=linux', '--project-name=license_flavor_patch_test', '.'])

    def test_provider_registration_precedes_application_construction_exactly_once(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            runner = app / 'linux/runner'
            runner.mkdir(parents=True)
            (runner / 'main.cc').write_text('#include "my_application.h"\nint main() {\n  g_autoptr(MyApplication) app = my_application_new();\n}\n')
            (runner / 'CMakeLists.txt').write_text('add_executable(${BINARY_NAME}\n  "main.cc"\n)\n')
            e2e.inject_provider(app)
            source = (runner / 'main.cc').read_text()
            self.assertLess(source.index('RegisterLinuxNativeCacheTestProvider()'), source.index('my_application_new()'))
            cmake = (runner / 'CMakeLists.txt').read_text()
            for value in ('runner_patch_key_provider.cpp', 'libcurl glib-2.0', 'PkgConfig::NATIVE_CACHE_PROVIDER keyutils'):
                self.assertIn(value, cmake)
            with self.assertRaises(e2e.FixtureError):
                e2e.inject_provider(app)

    def test_probe_context_uses_stdin_fresh_process_and_clears_before_app_download(self):
        with tempfile.TemporaryDirectory() as temp:
            lifecycle = self.lifecycle(Path(temp))
            lifecycle.authority.allow(context())
            lifecycle.provision = mock.Mock(url='http://127.0.0.1:1234/key')
            lifecycle.ring.lib.keyctl_clear.return_value = 0
            calls = []
            def command(*args, **kwargs):
                calls.append(kwargs)
                if kwargs['name'] == 'native-probe-online':
                    lifecycle.authority.accepted += 1
            lifecycle.run_command = command
            with contextlib.redirect_stdout(io.StringIO()):
                lifecycle.probe_context()
            self.assertEqual(len(calls), 2)
            key = e2e.delivery_material(lifecycle.authority.secret, lifecycle.authority.allowed_aad)[1]
            self.assertEqual([c['input'] for c in calls], [key, key])
            self.assertIn('SHOREBIRD_E2E_PROVISION_URL', calls[0]['env'])
            self.assertNotIn('SHOREBIRD_E2E_PROVISION_URL', calls[1]['env'])
            lifecycle.ring.lib.keyctl_clear.assert_called_once_with(91)
            self.assertEqual([s['name'] for s in lifecycle.manifest['stages']],
                             ['native-probe-online', 'native-probe-offline'])
            self.assertNotIn(key.hex(), json.dumps(lifecycle.manifest))
            self.assertNotIn(key, (Path(temp) / 'provider-context.txt').read_bytes())


class CacheAndExecutionBoundaryTest(unittest.TestCase):
    def test_persisted_signed_response_authentication_is_audited_without_exporting_payload(self):
        # response_auth::persist_patch_authentication writes this alongside every
        # installed response-verified patch. Use the actual server/Rust fixture.
        response = json.loads((e2e.ROOT / 'updater/library/test_fixtures/server_response_v1.json').read_text())
        authentication = json.dumps(response['response']['authentication']).encode()
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            patch = cache / 'patches/3'
            patch.mkdir(parents=True)
            (patch / 'transport.aesgcm').write_bytes(b'ciphertext')
            (patch / 'state.json').write_text('{"kind":"InstalledEncryptedTransportV1"}')
            (patch / 'authentication.json').write_bytes(authentication)
            result = e2e.scan_cache(cache, patch_hash='e' * 64)
            entry = next(f for f in result['files'] if f['path'] == 'patches/3/authentication.json')
            self.assertEqual(entry['sha256'], e2e.sha256(authentication))
            self.assertEqual(entry['size'], len(authentication))
            self.assertNotIn(response['response']['authentication']['payload'], json.dumps(result))
            self.assertNotIn(response['response']['authentication']['signature'], json.dumps(result))

            # Recognizing this metadata file must not bypass the portable
            # plaintext-image/key scan or admit neighboring/temporary paths.
            for content, reason in ((b'\x7fELFnative', 'Plaintext executable'),
                                    (b'prefix secret-key suffix', 'Plaintext secret'),
                                    (b'native-image', 'Plaintext executable')):
                (patch / 'authentication.json').write_bytes(content)
                with self.subTest(content=content), self.assertRaisesRegex(e2e.FixtureError, reason):
                    e2e.scan_cache(cache, patch_hash=e2e.sha256(b'native-image'),
                                   forbidden_secrets=(b'secret-key',))
            (patch / 'authentication.json').write_bytes(authentication)
            for relative in ('patches/3/authentication.json.tmp', 'patches/0/authentication.json',
                             'patches/3/nested/authentication.json', 'patches/3/authenticationXjson',
                             'patches/3/stateXjson', 'patches/3/transportXaesgcm'):
                unknown = cache / relative
                unknown.parent.mkdir(parents=True, exist_ok=True)
                unknown.write_bytes(authentication)
                with self.subTest(path=relative), self.assertRaisesRegex(e2e.FixtureError, 'Unexpected updater cache file'):
                    e2e.scan_cache(cache, patch_hash='e' * 64)
                unknown.unlink()

    def test_cache_final_files_only_and_plaintext_key_or_elf_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            patch = cache / 'patches/3'
            patch.mkdir(parents=True)
            (patch / 'transport.aesgcm').write_bytes(b'ciphertext')
            (patch / 'state.json').write_text('{"kind":"InstalledEncryptedTransportV1"}')
            result = e2e.scan_cache(cache, patch_hash='e' * 64)
            self.assertEqual(result['scope'], 'final-files-only')
            self.assertIs(result['transient_write_trace'], False)
            for relative, content in (('key.txt', b'plain'), ('patches/3/app.so', b'anything'),
                                      ('state.json', b'\x7fELFnative'),
                                      ('state.json', b'prefix secret-key suffix')):
                path = cache / relative
                path.write_bytes(content)
                with self.subTest(path=relative), self.assertRaises(e2e.FixtureError):
                    e2e.scan_cache(cache, patch_hash='e' * 64, forbidden_secrets=(b'secret-key',))
                path.unlink()
            (cache / 'alias').symlink_to(patch, target_is_directory=True)
            with self.assertRaises(e2e.FixtureError):
                e2e.scan_cache(cache, patch_hash='e' * 64)

    def test_successful_execute_keeps_preflight_machine_evidence_in_final_report(self):
        # Exercise real main/constructor/cleanup/report handoff; no native runtime
        # starts. The mocked execute supplies a successful lifecycle's receipts.
        stages = (
            'isolated-toolchain', 'released-native-runtime-policy', 'actual-cli-release-free',
            'rebuilt-native-bytes-identical', 'actual-cli-patch-pro', 'ciphertext-installed',
            'api-and-provisioning-stopped', 'offline-cold-U', 'offline-missing-key-BASE',
            'offline-missing-recovery-U', 'offline-wrong-key-BASE', 'offline-wrong-recovery-U',
            'final-cache-before-rollback', 'online-U-receive-signed-rollback',
            'offline-cold-rollback-BASE', 'final-cache-after-rollback', 'released-bundle-unchanged')
        stages += ('native-probe-self-test', 'native-probe-online', 'native-probe-offline')
        for arch, runtime in [('x64', 'x86_64'), ('arm64', 'aarch64')]:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                work, results = Path(temp) / 'work', Path(temp) / 'results'
                binaries = {str(n): {'architecture': arch, 'native_target': True,
                                      'format': 'elf'} for n in range(7)}

                def execute(lifecycle):
                    self.assertEqual(lifecycle.manifest.get('target_arch'), arch)
                    self.assertEqual(lifecycle.manifest.get('runtime_arch'), runtime)
                    self.assertEqual(lifecycle.manifest.get('binary_architectures'), binaries)
                    lifecycle.manifest['stages'] = [{'name': name} for name in stages]
                    lifecycle.manifest['result'] = 'passed'

                with mock.patch.object(e2e.architecture, 'require_native_host') as host, \
                        mock.patch.object(e2e, 'preflight', return_value=binaries) as preflight, \
                        mock.patch.object(e2e, 'OwnedProcessGroups') as owner, \
                        mock.patch.object(e2e, 'free_port', return_value=12345), \
                        mock.patch.object(e2e.Lifecycle, 'execute', autospec=True,
                                          side_effect=execute) as executed, \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = e2e.main(['run', '--execute', '--arch', arch,
                                    '--work', str(work), '--results', str(results)])
                self.assertEqual(code, 0)
                host.assert_called_once_with('linux', arch)
                preflight.assert_called_once()
                executed.assert_called_once()
                owner.return_value.close.assert_called_once_with()
                report = json.loads((results / 'results.json').read_text())
                self.assertEqual(report['target_arch'], arch)
                self.assertEqual(report['runtime_arch'], runtime)
                self.assertEqual(report['binary_architectures'], binaries)
                self.assertEqual([stage['name'] for stage in report['stages']], list(stages))
                e2e.architecture.validate_lifecycle_report(report, 'linux', arch)
                self.assertEqual(report['key_storage'], 'kernel-session-keyring')
                self.assertEqual(report['key_storage_scope'], 'cold-app-processes-in-one-session')
                self.assertFalse(work.exists())

    def test_plan_has_no_mutation_service_or_keyring(self):
        with mock.patch.object(e2e, 'preflight', side_effect=AssertionError('preflight')), \
                mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('runtime')), \
                mock.patch.object(e2e, 'OwnedSessionKeyring', side_effect=AssertionError('keyring')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(e2e.main(['plan']), 0)
        value = json.loads(output.getvalue())
        self.assertIs(value['read_only'], True)
        self.assertIn('transient write audit unimplemented', value['audit'])
        self.assertIn('no reboot/session-restart claim', value['key_persistence_scope'])

    def test_run_requires_execute_and_linux_before_any_mutation(self):
        with self.assertRaisesRegex(SystemExit, 'requires --execute'):
            e2e.main(['run'])
        with mock.patch.object(e2e.platform, 'system', return_value='Darwin'), \
                mock.patch.object(e2e, 'preflight', side_effect=AssertionError('preflight')), \
                mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('runtime')), \
                self.assertRaisesRegex(SystemExit, 'requires Linux x64'):
            e2e.main(['run', '--execute'])

    def test_preflight_error_redacts_raw_secret_and_preserves_work(self):
        with tempfile.TemporaryDirectory() as temp:
            work, results = Path(temp) / 'work', Path(temp) / 'results'
            console = io.StringIO()
            with mock.patch.object(e2e.platform, 'system', return_value='Linux'), \
                    mock.patch.object(e2e.platform, 'machine', return_value='x86_64'), \
                    mock.patch.object(e2e.architecture, 'require_binary', return_value={}), \
                    mock.patch.object(e2e, 'preflight', side_effect=RuntimeError('private-token')), \
                    mock.patch.object(e2e, 'Lifecycle', side_effect=AssertionError('runtime')), \
                    contextlib.redirect_stdout(console):
                self.assertEqual(e2e.main(['run', '--execute', '--work', str(work), '--results', str(results)]), 1)
            self.assertFalse(work.exists())
            output = (results / 'results.json').read_text()
            self.assertNotIn('private-token', output)
            self.assertNotIn('private-token', console.getvalue())
            self.assertIn('type=RuntimeError after_stage=preflight', console.getvalue())
            report = json.loads(output)
            self.assertEqual(report['failure'], {'type': 'RuntimeError', 'after_stage': 'preflight'})
            self.assertIn('work untouched', report['cleanup'])

    def test_competing_work_creation_preserved_during_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            work, results = Path(temp) / 'work', Path(temp) / 'results'
            def competing(_args):
                work.mkdir()
                (work / 'other').write_text('preserve')
            with mock.patch.object(e2e.platform, 'system', return_value='Linux'), \
                    mock.patch.object(e2e.platform, 'machine', return_value='x86_64'), \
                    mock.patch.object(e2e.architecture, 'require_binary', return_value={}), \
                    mock.patch.object(e2e, 'preflight', side_effect=competing), \
                    mock.patch.object(e2e, 'free_port', return_value=1234), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(e2e.main(['run', '--execute', '--work', str(work), '--results', str(results)]), 1)
            self.assertEqual((work / 'other').read_text(), 'preserve')
            report = json.loads((results / 'results.json').read_text())
            self.assertEqual(report['failure']['type'], 'FileExistsError')

    def test_lifecycle_construction_never_instantiates_windows_job(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(e2e.portable, 'OwnedWindowsJob', side_effect=AssertionError('Windows job')), \
                mock.patch.object(e2e.portable.Lifecycle, '__init__', side_effect=AssertionError('Windows lifecycle')):
            lifecycle = e2e.Lifecycle(mock.Mock(work=Path(temp)))
            self.assertIsInstance(lifecycle.job, e2e.OwnedProcessGroups)
            lifecycle.cleanup()


if __name__ == '__main__':
    unittest.main()


class ArchitectureRoutingTest(unittest.TestCase):
    def test_native_architecture_paths_and_cache_are_explicit(self):
        for arch, runtime in [('x64', 'x86_64'), ('arm64', 'aarch64')]:
            args = e2e.parser().parse_args(['plan', '--arch', arch])
            self.assertEqual(e2e.runtime_arch(args), runtime)
            path = e2e.bundle_directory(args, Path('/owned'), 'linux')
            self.assertIn(('linux', arch), list(zip(path.parts, path.parts[1:])))
            cached = e2e.patch_cache_directory(args, Path('/owned'), 'linux')
            self.assertEqual(cached.name, 'linux-arm64' if arch == 'arm64' else 'patch')

    def test_arm64_aad_is_canonical_and_cross_arch_key_context_is_rejected(self):
        arm = dict(context(), arch='aarch64')
        authority = e2e.KeyAuthority(b'fixture-secret', 'private-token')
        authority.allow(arm)
        aad = e2e.canonical_aad(arm)
        self.assertIn('arch=aarch64\n', aad)
        key_id, key = e2e.delivery_material(b'fixture-secret', aad)
        self.assertEqual(authority.provision('Bearer private-token',
            json.dumps({'key_id': key_id, 'aad': aad}).encode()), key.hex().encode())
        x64 = e2e.canonical_aad(context())
        x64_key_id, _ = e2e.delivery_material(b'fixture-secret', x64)
        with self.assertRaises(ValueError):
            authority.provision('Bearer private-token',
                json.dumps({'key_id': x64_key_id, 'aad': x64}).encode())
        self.assertNotEqual(key_id, x64_key_id)

    def test_arm64_plan_is_read_only_and_records_selected_architecture(self):
        output = io.StringIO()
        with mock.patch.object(e2e, 'preflight', side_effect=AssertionError('plan mutation')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(e2e.main(['plan', '--arch', 'arm64']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['target_arch'], 'arm64')
        self.assertEqual(report['runtime_arch'], 'aarch64')

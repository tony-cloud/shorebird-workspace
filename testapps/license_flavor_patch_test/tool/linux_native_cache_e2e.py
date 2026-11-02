#!/usr/bin/env python3
"""Real Linux GTK signed/encrypted native lifecycle, using an owned session keyring.

Only ``plan`` and pure unit tests run on other hosts. ``run --execute`` needs
prebuilt matching Linux engine/SDK and an active X11/DBus session (e.g.
dbus-run-session -- xvfb-run -a). Cold app boots stay in the same kernel session;
this fixture makes no machine-reboot or session-restart persistence claim.
Only sanitized results.json survives cleanup. Cache audit is final-files-only.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid

# The imported module has no runtime side effects. Explicitly reuse its portable
# helpers and methods; never construct its Windows Lifecycle or Windows Job.
_spec = importlib.util.spec_from_file_location('native_cache_portable',
    Path(__file__).with_name('windows_native_cache_e2e.py'))
portable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portable)
ROOT, FIXTURE = portable.ROOT, portable.FIXTURE
SCHEMA = 'linux-native-cache-e2e-v1'
FixtureError = portable.FixtureError
sha256, file_hash = portable.sha256, portable.file_hash
strict_json, private_write = portable.strict_json, portable.private_write
replace_once, tree_hashes = portable.replace_once, portable.tree_hashes
local_engine_flags = portable.local_engine_flags
stage_local_engine_packages = portable.stage_local_engine_packages
stage_local_engine_metadata = portable.stage_local_engine_metadata
wait_for, free_port = portable.wait_for, portable.free_port
LocalService, installed_evidence = portable.LocalService, portable.installed_evidence
verify_release_policy = portable.verify_release_policy

selected_arch, runtime_arch = portable.selected_arch, portable.runtime_arch
bundle_directory, patch_cache_directory = portable.bundle_directory, portable.patch_cache_directory
verify_binary, architecture = portable.verify_binary, portable.architecture


def parse_aad(aad):
    # Reuse all strict portable field validation while requiring Linux exactly.
    if not isinstance(aad, str) or '\nplatform=linux\n' not in aad:
        raise ValueError('Unsupported Linux runtime context')
    context = portable.parse_aad(aad.replace('\nplatform=linux\n', '\nplatform=windows\n'))
    context['platform'] = 'linux'
    return context


def canonical_aad(context):
    if set(context) != set(portable.AAD_FIELDS):
        raise ValueError('Unknown delivery fields')
    aad = portable.AAD_PREFIX + ''.join('\n' + name + '=' + str(context[name])
                                      for name in portable.AAD_FIELDS)
    if parse_aad(aad) != {name: str(context[name]) for name in portable.AAD_FIELDS}:
        raise ValueError('Invalid delivery context')
    return aad


def delivery_material(secret, aad):
    import hashlib
    import hmac
    parse_aad(aad)
    def digest(label):
        return hmac.new(secret, label + b'\0' + aad.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest(b'key-id')[:12]).rstrip(b'=').decode(), digest(b'key')


class KeyAuthority(portable.KeyAuthority):
    def allow(self, context):
        self.allowed_aad = canonical_aad(context)

    def provision(self, authorization, body):
        import hmac
        MAX_BODY = portable.MAX_BODY
        with self.lock:
            try:
                if not isinstance(authorization, str) or not hmac.compare_digest(
                        authorization, 'Bearer ' + self.token):
                    raise ValueError('Authorization rejected')
                if len(body) > MAX_BODY:
                    raise ValueError('Oversized request')
                request = strict_json(body.decode('utf-8'))
                if not isinstance(request, dict) or set(request) != {'key_id', 'aad'}:
                    raise ValueError('Invalid request fields')
                aad, key_id = request['aad'], request['key_id']
                parse_aad(aad)
                if not isinstance(key_id, str) or aad != self.allowed_aad:
                    raise ValueError('Unregistered context')
                expected, key = delivery_material(self.secret, aad)
                if not hmac.compare_digest(key_id, expected):
                    raise ValueError('Mismatching key identifier')
                self.accepted += 1
                return key.hex().encode('ascii')
            except (ValueError, UnicodeError, TypeError, AttributeError):
                self.rejected += 1
                raise ValueError('Provisioning rejected') from None


DART_FIXTURE = portable.DART_FIXTURE.replace(
    "DynamicLibrary.open('flutter_windows.dll')", "DynamicLibrary.process()").replace(
    'windows-native-cache-e2e-v1', SCHEMA)


def validate_report(report, nonce, process_id, expected_patch, phase):
    if not isinstance(report, dict) or report.get('schema') != SCHEMA:
        raise ValueError('Invalid Linux rendered receipt')
    transformed = dict(report, schema=portable.SCHEMA)
    portable.validate_report(transformed, nonce, process_id, expected_patch, phase)
    return report


class OwnedSessionKeyring:
    """Fresh unnamed session ring inherited by children; no user/persistent ring."""
    KEY_SPEC_SESSION_KEYRING = -3
    KEY_POS_ALL = 0x3f000000

    def __init__(self):
        self.serial = None
        library = ctypes.util.find_library('keyutils')
        if not library:
            raise FixtureError('libkeyutils is required')
        self.lib = ctypes.CDLL(library, use_errno=True)
        for name, arguments in (
                ('keyctl_join_session_keyring', [ctypes.c_char_p]),
                ('keyctl_clear', [ctypes.c_int32]),
                ('keyctl_revoke', [ctypes.c_int32]),
                ('keyctl_setperm', [ctypes.c_int32, ctypes.c_uint32]),
                ('keyctl_get_keyring_ID', [ctypes.c_int32, ctypes.c_int])):
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, ctypes.c_int32
        serial = self.lib.keyctl_join_session_keyring(None)
        if serial <= 0:
            raise FixtureError('Creating anonymous Linux session keyring is forbidden or unavailable')
        self.serial = serial
        try:
            if (self.lib.keyctl_clear(serial) != 0 or
                    self.lib.keyctl_setperm(serial, self.KEY_POS_ALL) != 0 or
                    self.lib.keyctl_get_keyring_ID(self.KEY_SPEC_SESSION_KEYRING, 0) != serial):
                raise FixtureError('Cannot secure or verify owned Linux session keyring')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.serial is not None:
            if self.lib.keyctl_revoke(self.serial) != 0:
                raise FixtureError('Owned Linux session keyring revocation failed')
            self.serial = None


class RetainedGroupProcess(subprocess.Popen):
    """Observe exit without reaping: the owned leader pins its PID and PGID.

    Popen.communicate(), wait(), and poll() all use these overridden internals.
    Only the owner calls reap(), after terminating the group's live members.
    """
    def __init__(self, *args, **kwargs):
        if platform.system() != 'Linux' or not hasattr(os, 'WNOWAIT'):
            raise FixtureError('Owned process execution requires Linux waitid WNOWAIT')
        self.reaped = False
        super().__init__(*args, **kwargs)

    def _internal_poll(self, *args, **kwargs):
        if self.returncode is None:
            with self._waitpid_lock:
                if self.returncode is None:
                    try:
                        status = os.waitid(os.P_PID, self.pid,
                                           os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    except ChildProcessError:
                        raise FixtureError('Owned group leader was reaped outside its owner') from None
                    if status is not None:
                        if status.si_code == os.CLD_EXITED:
                            self.returncode = status.si_status
                        elif status.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                            self.returncode = -status.si_status
                        else:
                            raise FixtureError('Unexpected owned process wait status')
        return self.returncode

    def _wait(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self._internal_poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01)

    def reap(self):
        if self.reaped or self._internal_poll() is None:
            raise FixtureError('Owned process cannot be reaped before group cleanup')
        with self._waitpid_lock:
            try:
                waited, status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                raise FixtureError('Owned group leader identity was lost') from None
            if waited != self.pid:
                raise FixtureError('Unexpected owned process reap result')
            self.returncode = os.waitstatus_to_exitcode(status)
            self.reaped = True


class OwnedProcessGroups:
    """Signal only groups pinned by their own retained, unreaped child leader."""
    def __init__(self):
        self.owned = {}

    def spawn(self, command, **kwargs):
        if 'start_new_session' in kwargs or 'preexec_fn' in kwargs:
            raise FixtureError('Owned process group settings cannot be overridden')
        process = RetainedGroupProcess(command, start_new_session=True, **kwargs)
        self.owned[process.pid] = process
        return process

    @staticmethod
    def _signal(group, sig):
        try:
            os.killpg(group, sig)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def live_members(group):
        # An exited retained leader is deliberately still a zombie, anchoring
        # the group identity. Zombies hold no executing descendants themselves.
        live = []
        try:
            entries = list(Path('/proc').iterdir())
        except OSError:
            raise FixtureError('Cannot inspect owned Linux process group') from None
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
                    live.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError):
                raise FixtureError('Cannot verify owned Linux process group members') from None
        return live

    def stop(self, process):
        if process is None:
            return
        if self.owned.get(process.pid) is not process or process.reaped:
            raise FixtureError('Refusing to signal a process group without its retained owned leader')
        self._signal(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._signal(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        self._signal(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while self.live_members(process.pid):
            if time.monotonic() >= deadline:
                raise FixtureError('Owned process group still exists after bounded termination')
            # The unreaped leader still pins this exact group identity.
            self._signal(process.pid, signal.SIGKILL)
            time.sleep(.05)
        process.reap()
        self.owned.pop(process.pid)

    def close(self):
        failures = []
        for process in list(self.owned.values()):
            try:
                self.stop(process)
            except Exception:
                failures.append(True)
        if failures:
            raise FixtureError('Owned process group cleanup failed')


def create_linux_command(args, work):
    return [work / 'flutter/bin/cache/dart-sdk/bin/dart',
            '--packages=' + str(work / 'flutter/packages/flutter_tools/.dart_tool/package_config.json'),
            work / 'flutter/packages/flutter_tools/bin/flutter_tools.dart',
            *local_engine_flags(args), 'create', '--platforms=linux',
            '--project-name=license_flavor_patch_test', '.']


def inject_provider(app):
    runner = app / 'linux/runner'
    if (runner / 'runner_patch_key_provider.cpp').exists():
        raise FixtureError('Native provider was already injected')
    for name in ('runner_patch_key_provider.h', 'runner_patch_key_provider.cpp'):
        shutil.copy2(FIXTURE / 'tool/linux_native' / name, runner / name)
    replace_once(runner / 'main.cc', '#include "my_application.h"',
                 '#include "runner_patch_key_provider.h"\n#include "my_application.h"')
    replace_once(runner / 'main.cc', '  g_autoptr(MyApplication) app = my_application_new();',
                 '  if (!RegisterLinuxNativeCacheTestProvider()) return EXIT_FAILURE;\n'
                 '  g_autoptr(MyApplication) app = my_application_new();')
    replace_once(runner / 'CMakeLists.txt', '  "main.cc"',
                 '  "main.cc"\n  "runner_patch_key_provider.cpp"')
    with (runner / 'CMakeLists.txt').open('a') as output:
        output.write('\npkg_check_modules(NATIVE_CACHE_PROVIDER REQUIRED IMPORTED_TARGET libcurl glib-2.0)\n'
                     'target_link_libraries(${BINARY_NAME} PRIVATE PkgConfig::NATIVE_CACHE_PROVIDER keyutils)\n')



def stage_local_launchers(flutter, vended):
    # The CLI calls installRevision even when a full local toolchain has already
    # been copied. Mark this explicit preflight-validated staging complete, so it
    # cannot run unrelated `precache --android` without local-engine flags.
    flutter_command = '#!/bin/sh\nexec "$(dirname "$0")/cache/dart-sdk/bin/dart" "--packages=$(dirname "$0")/../packages/flutter_tools/.dart_tool/package_config.json" "$(dirname "$0")/../packages/flutter_tools/bin/flutter_tools.dart" "$@"\n'
    dart_command = '#!/bin/sh\nexec "$(dirname "$0")/cache/dart-sdk/bin/dart" "$@"\n'
    for base in (flutter, vended):
        for name, command in (('flutter', flutter_command), ('dart', dart_command)):
            wrapper = base / 'bin' / name
            wrapper.write_text(command)
            wrapper.chmod(0o700)
    (vended / '.shorebird_precache').write_text('fixture: prebuilt local toolchain staged after preflight\n')


def scan_cache(cache, *, patch_hash, forbidden_secrets=()):
    # No on-disk key cache exists. Reject unknown files, not merely ELF magic.
    for path in cache.rglob('*'):
        if path.is_symlink():
            raise FixtureError('Unexpected cache symlink')
        # Required signed-response verification persists authentication.json
        # before install; cold boots authenticate that record again. It remains
        # subject to every plaintext/key check in the portable scan below.
        if path.is_file() and not (path.relative_to(cache).as_posix() in ('state.json', 'pointers.json') or
                re.fullmatch(r'patches/[1-9][0-9]*/(?:state\.json|transport\.aesgcm|authentication\.json)',
                             path.relative_to(cache).as_posix())):
            raise FixtureError('Unexpected updater cache file')
    # The portable key-directory check sees an absent directory: no disk keys.
    return portable.scan_cache(cache, cache / '.no-key-files', patch_hash=patch_hash,
                               forbidden_secrets=forbidden_secrets)


def preflight(args):
    host, target = (args.engine_src / 'out' / name for name in (args.host_engine, args.target_engine))
    required = [host / 'dart-sdk/bin/dart', host / 'dart-sdk/bin/dartaotruntime',
                host / 'dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot',
                host / 'gen/const_finder.dart.snapshot', host / 'font-subset',
                host / 'libflutter_linux_gtk.so', host / 'flutter_linux/flutter_linux.h',
                host / 'icudtl.dat', host / 'gen/dart-pkg/sky_engine/pubspec.yaml',
                target / 'flutter_patched_sdk/platform_strong.dill',
                target / 'flutter_patched_sdk_product/platform_strong.dill',
                args.tooling_dart, args.patch_tool]
    if args.provider_probe is None:
        raise FixtureError('A precompiled --provider-probe is required')
    required += [args.provider_probe,
                 ROOT / 'shorebird/.dart_tool/package_config.json',
                 ROOT / 'flutter/packages/flutter_tools/.dart_tool/package_config.json']
    if not any((target / directory / 'gen_snapshot').is_file() for directory in
               ('.', 'universal', 'clang_x64', 'clang_x86', 'clang_i386', 'clang_arm64', 'clang_riscv64')):
        raise FixtureError('Prebuilt Flutter-supported gen_snapshot path missing')
    if any(not path.is_file() for path in required) or not args.pub_cache.is_dir():
        raise FixtureError('Prebuilt matching Linux engine/full SDK, tooling, package configs, probe or pub cache missing')
    for name in ('git', 'go', 'pkg-config', 'cmake', 'ninja', 'clang++', 'xvfb-run', 'xauth', 'dbus-run-session'):
        if shutil.which(name) is None:
            raise FixtureError('Required executable missing: ' + name)
    if not os.environ.get('DISPLAY') or not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        raise FixtureError('Active X11 and DBus session required; use dbus-run-session -- xvfb-run -a')
    processes = OwnedProcessGroups()
    try:
        child = processes.spawn(['pkg-config', '--exists', 'gtk+-3.0', 'libcurl', 'glib-2.0'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if child.wait(timeout=15):
            raise FixtureError('GTK/libcurl/GLib development dependencies unavailable')
    finally:
        processes.close()
    ring = OwnedSessionKeyring()
    ring.close()
    binaries = [host / 'dart-sdk/bin/dart', host / 'dart-sdk/bin/dartaotruntime',
                host / 'font-subset', host / 'libflutter_linux_gtk.so',
                args.tooling_dart, args.patch_tool, args.provider_probe]
    binaries.append(next(target / directory / 'gen_snapshot' for directory in
                         ('.', 'universal', 'clang_x64', 'clang_x86', 'clang_i386', 'clang_arm64', 'clang_riscv64')
                         if (target / directory / 'gen_snapshot').is_file()))
    return {str(path.relative_to(args.engine_src)) if path.is_relative_to(args.engine_src)
            else path.name: verify_binary(path, args) for path in binaries}


class Lifecycle:
    # Explicitly opt into the portable surface; no Windows constructor, process
    # cleanup, cache path, or provider can be reached by inheritance.
    stage = portable.Lifecycle.stage
    snapshot = portable.Lifecycle.snapshot
    remap_config = portable.Lifecycle.remap_config
    request = portable.Lifecycle.request
    rows = portable.Lifecycle.rows
    make_observer = portable.Lifecycle.make_observer
    candidate = portable.Lifecycle.candidate
    boot_confirmed = portable.Lifecycle.boot_confirmed

    def __init__(self, args):
        self.args, self.work = args, args.work
        self.api = self.provision = self.observer = self.app = None
        self.logs, self.receipts = [], {}
        self.cache, self.ring = None, None
        self.work_owned = self.cache_owned = False
        self.receipt_lock = threading.Lock()
        self.secrets = {name: secrets.token_urlsafe(48) for name in
                        ('master', 'jwt', 'password', 'provision')}
        self.secrets['signing'] = base64.b64encode(secrets.token_bytes(32)).decode()
        self.authority = KeyAuthority(self.secrets['master'].encode(), self.secrets['provision'])
        self.manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [],
                         'validation': 'real Linux GTK CLI/server/Runner lifecycle',
                         'key_storage': 'kernel-session-keyring',
                         'key_storage_scope': 'cold-app-processes-in-one-session',
                         'key_persistence_scope': 'cold app processes within one kernel session only',
                         'transient_write_trace': False}
        self.api_url = 'http://127.0.0.1:' + str(free_port())
        self.token = None
        self.job = OwnedProcessGroups()

    def run_command(self, command, *, cwd=None, env=None, name='command', input=None):
        self.manifest['operation'] = name
        path = self.work / (name + '.private.log')
        with path.open('ab') as log:
            process = self.job.spawn([str(x) for x in command], cwd=cwd or ROOT,
                env=env or self.environment(), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=log, stderr=log)
            code, timed_out = None, False
            try:
                process.communicate(input=input, timeout=self.args.build_timeout)
                code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                self.job.stop(process)
        if code or timed_out:
            self.manifest['command_failure'] = portable.command_failure(name, code, timed_out, path)
            raise FixtureError(name + ' failed (private log removed during cleanup)')

    def output(self, command, cwd=None):
        process = self.job.spawn([str(x) for x in command], cwd=cwd or ROOT,
            env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _ = process.communicate(timeout=60)
            if process.returncode:
                raise FixtureError('Source snapshot command failed')
            return output
        finally:
            self.job.stop(process)

    def environment(self):
        env = dict(os.environ)
        for name in tuple(env):
            if name.startswith(('SHOREBIRD_', 'PATCH_DELIVERY_', 'PATCH_SIGNING_', 'FLUTTER_')):
                del env[name]
        env.update({'HOME': str(self.work / 'home'),
                    'XDG_CONFIG_HOME': str(self.work / 'xdg-config'),
                    'XDG_CACHE_HOME': str(self.work / 'xdg-cache'),
                    'XDG_DATA_HOME': str(self.work / 'xdg-data'),
                    'XDG_RUNTIME_DIR': str(self.work / 'xdg-runtime'),
                    'GOCACHE': str(self.work / 'go-cache'),
                    'GOPATH': str(self.work / 'go-path'),
                    'GOMODCACHE': str(self.work / 'go-path/pkg/mod'),
                    'SHOREBIRD_HOSTED_URL': self.api_url,
                    'AUTH_SERVICE_URL': self.api_url + '/auth',
                    'SHOREBIRD_ARTIFACT_BASE_URL': self.api_url,
                    'SHOREBIRD_FLUTTER_STORAGE_BASE_URL': portable.PUBLIC_FLUTTER_STORAGE,
                    'FLUTTER_STORAGE_BASE_URL': portable.PUBLIC_FLUTTER_STORAGE,
                    'FLUTTER_ROOT': str(self.work / 'flutter'),
                    'PUB_CACHE': str(self.work / 'pub-cache'),
                    'PATH': str(self.work / 'flutter/bin') + os.pathsep + os.environ['PATH'],
                    'CI': 'true', 'BOT': 'true', 'FLUTTER_SUPPRESS_ANALYTICS': 'true',
                    'DART_SUPPRESS_ANALYTICS': 'true', 'SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS': '0'})
        if self.token:
            env['SHOREBIRD_TOKEN'] = 'sb_api_' + self.token
        return env

    def prepare(self):
        self.work.mkdir(mode=0o700)
        self.work_owned = True
        for directory in ('home', 'xdg-config', 'xdg-cache', 'xdg-data', 'xdg-runtime'):
            (self.work / directory).mkdir(mode=0o700)
        self.ring = OwnedSessionKeyring()
        # Inherit no mutable checkout/cache aliases; copy snapshots and caches.
        self.snapshot(ROOT / 'shorebird', self.work / 'cli')
        self.snapshot(ROOT / 'flutter', self.work / 'flutter')
        self.snapshot(ROOT / 'shorebird-server', self.work / 'server-src')
        shutil.copytree(ROOT / 'flutter/bin/cache', self.work / 'flutter/bin/cache', dirs_exist_ok=True)
        pub = self.args.pub_cache
        shutil.copytree(pub, self.work / 'pub-cache')
        sdk = self.work / 'flutter/bin/cache/dart-sdk'
        shutil.rmtree(sdk)
        shutil.copytree(self.args.engine_src / 'out' / self.args.host_engine / 'dart-sdk', sdk)
        stage_local_engine_packages(self.args.engine_src / 'out' / self.args.host_engine,
                                    self.work / 'flutter')
        engine_id = stage_local_engine_metadata(self.work / 'flutter')
        mappings = [(ROOT / 'shorebird', self.work / 'cli'), (ROOT / 'flutter', self.work / 'flutter'),
                    (pub, self.work / 'pub-cache')]
        self.remap_config(ROOT / 'shorebird/.dart_tool/package_config.json',
                          self.work / 'cli/.dart_tool/package_config.json', mappings)
        self.remap_config(ROOT / 'flutter/packages/flutter_tools/.dart_tool/package_config.json',
                          self.work / 'flutter/packages/flutter_tools/.dart_tool/package_config.json', mappings)
        flutter_ref = self.manifest['sources']['flutter']['head']
        for name, value in (('flutter.version', flutter_ref), ('engine.version', engine_id)):
            (self.work / 'cli/bin/internal' / name).write_text(value + '\n')
        # Real CLI expects its vended SDK here. Copy, without mutable links to the source SDK.
        vended = self.work / 'cli/bin/cache/flutter' / flutter_ref
        shutil.copytree(self.work / 'flutter', vended)
        stage_local_launchers(self.work / 'flutter', vended)
        self.cli = self.work / 'cli/bin/shorebird-e2e'
        self.run_command([self.args.tooling_dart, 'compile', 'exe',
                          '--packages=' + str(self.work / 'cli/.dart_tool/package_config.json'),
                          self.work / 'cli/packages/shorebird_cli/bin/shorebird.dart', '-o', self.cli],
                         env=self.environment(), name='compile-cli')
        artifact = patch_cache_directory(self.args, self.work, 'linux')
        artifact.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.args.patch_tool, artifact / 'patch')
        (artifact / 'patch.stamp').touch()
        # Downloads live in this session's private GOPATH. Keep their directories
        # writable so ordinary owned-tree cleanup can remove the module cache.
        self.run_command(['go', 'build', '-modcacherw', '-o', self.work / 'server', './cmd/server'],
                         cwd=self.work / 'server-src', env=self.environment(), name='compile-server')
        shutil.copytree(FIXTURE, self.work / 'app', ignore=shutil.ignore_patterns(
            '.git', '.dart_tool', 'build', 'Pods', '.symlinks', '.gradle', 'windows', 'linux'))
        pubspec = self.work / 'app/pubspec.yaml'
        content = re.sub(r'^version:.*\n?', '', pubspec.read_text(), flags=re.M)
        pubspec.write_text(content + '\nversion: 1.0.0+1\n')
        (self.work / 'app/lib/main.dart').write_text(DART_FIXTURE)
        self.run_command(create_linux_command(self.args, self.work),
                         cwd=self.work / 'app', env=self.environment(), name='generate-linux')
        inject_provider(self.work / 'app')
        self.native_sources = tree_hashes(self.work / 'app/linux')
        self.expected_engine_hash = file_hash(self.args.engine_src / 'out' / self.args.host_engine / 'libflutter_linux_gtk.so')
        self.stage('isolated-toolchain', {'engine_gtk_sha256': self.expected_engine_hash,
                                          'patch_tool_sha256': file_hash(self.args.patch_tool)})

    def start_api(self):
        env = dict(self.environment(), DB_DRIVER='sqlite', DB_PATH=str(self.work / 'server.sqlite'),
                   STORAGE_DRIVER='local', STORAGE_LOCAL_DIR=str(self.work / 'objects'),
                   SERVER_HOST='127.0.0.1', SERVER_PORT=self.api_url.rsplit(':', 1)[1],
                   SERVER_BASE_URL=self.api_url, JWT_SECRET=self.secrets['jwt'],
                   PATCH_SIGNING_MASTER_KEY=self.secrets['signing'],
                   PATCH_DELIVERY_ENCRYPTION='per_device_aes_gcm',
                   PATCH_DELIVERY_AES_SECRET=self.secrets['master'], PATCH_DELIVERY_CACHE_ENCRYPTED='true',
                   SMTP_HOST='', DEFAULT_ADMIN_EMAIL='linux-e2e@example.invalid',
                   DEFAULT_ADMIN_PASSWORD=self.secrets['password'])
        log = (self.work / 'server.private.log').open('ab')
        self.logs.append(log)
        self.api = self.job.spawn([str(self.work / 'server')], env=env, stdout=log, stderr=log)
        def health():
            if self.api.poll() is not None:
                raise FixtureError('Disposable API exited')
            return self.request('GET', '/health', authenticated=False)
        wait_for(health, 30, 'real Go API readiness')

    def bootstrap(self):
        login = self.request('POST', '/auth/token', {
            'email': 'linux-e2e@example.invalid', 'password': self.secrets['password']}, authenticated=False)
        self.token = login['token']
        if login.get('must_change_password'):
            password = secrets.token_urlsafe(32)
            self.request('PATCH', '/api/v1/users/me/password', {
                'current_password': self.secrets['password'], 'new_password': password})
            self.secrets['password'] = password
        org = self.request('POST', '/api/v1/organizations', {'name': 'Disposable Linux native E2E'})
        app = self.request('POST', '/api/v1/apps', {'organization_id': org['id'], 'display_name': 'Linux native encrypted lifecycle'})
        self.app_id = str(uuid.UUID(app['id']))
        # fl_shorebird supplies the private HOME cache root; the shared desktop
        # ConfigureShorebird then appends shorebird_updater/<app_id>.
        candidate = self.work / 'home/.shorebird_cache/shorebird_updater' / self.app_id
        try:
            candidate.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            raise FixtureError('Refusing existing updater app cache') from None
        self.cache = candidate
        self.cache_owned = True
        (self.work / 'app/shorebird.yaml').write_text(
            f'app_id: {self.app_id}\nbase_url: {self.api_url}\nauto_update: true\n'
            'patch_verification: strict\npatch_response_verification: required\n'
            'native_patch_max_bytes: 268435456\n')

    def build(self, patch=False):
        kind = 'patch' if patch else 'release'
        if patch:
            current = tree_hashes(self.work / 'app/linux')
            # Flutter creates ephemeral output below linux/flutter. Only sources
            # recorded before release participate in this immutability check.
            if any(current.get(name) != digest for name, digest in self.native_sources.items()):
                raise FixtureError('Native Runner fixture changed between release and patch')
        command = [self.cli, '--json', *local_engine_flags(self.args),
                   kind, 'linux', '--dart-define=LICENSE_TYPE=' + ('pro' if patch else 'free')]
        if patch:
            command.extend(['--release-version=' + self.release_version, '--track=stable'])
        self.run_command(command, cwd=self.work / 'app', env=self.environment(), name=kind)
        build = bundle_directory(self.args, self.work, 'linux')
        if not patch:
            self.bundle = self.work / 'released'
            shutil.copytree(build, self.bundle)
            self.bundle_hashes = tree_hashes(self.bundle)
            self.runner = self.bundle / 'license_flavor_patch_test'
            if not self.runner.is_file():
                raise FixtureError('Expected generated released Runner missing')
            for binary in (self.runner, self.bundle / 'lib/libapp.so', self.bundle / 'lib/libflutter_linux_gtk.so'):
                verify_binary(binary, self.args)
            if file_hash(self.bundle / 'lib/libflutter_linux_gtk.so') != self.expected_engine_hash:
                raise FixtureError('Released GTK engine does not match selected prebuilt local engine')
            self.stage('released-native-runtime-policy', verify_release_policy(self.bundle, self.app_id))
            release = self.rows('SELECT id, version FROM releases WHERE app_id=?', (self.app_id,))
            if len(release) != 1:
                raise FixtureError('Expected one actual CLI release')
            self.release_id, self.release_version = release[0]
            self.stage('actual-cli-release-free', {'release_version': self.release_version, 'bundle': self.bundle_hashes})
        else:
            built_hashes = tree_hashes(build)
            native = {name: digest for name, digest in self.bundle_hashes.items()
                      if name != 'lib/libapp.so' and (name == 'license_flavor_patch_test' or name.startswith('lib/'))}
            current_native = {name: digest for name, digest in built_hashes.items()
                              if name != 'lib/libapp.so' and (name == 'license_flavor_patch_test' or name.startswith('lib/'))}
            if not native or native != current_native:
                raise FixtureError('Actual rebuilt Runner/engine/plugin bytes changed')
            self.stage('rebuilt-native-bytes-identical', native)
            rows = self.rows('SELECT p.id, p.number, a.hash, a.storage_key, a.arch, a.hash_signature '
                             'FROM patches p JOIN patch_artifacts a ON a.patch_id=p.id '
                             'WHERE p.release_id=? AND a.platform=?', (self.release_id, 'linux'))
            if len(rows) != 1:
                raise FixtureError('Expected one real Linux patch artifact')
            self.patch_id, self.number, self.patch_hash, storage, artifact_arch, signature = rows[0]
            if artifact_arch != runtime_arch(self.args):
                raise FixtureError('Published patch architecture does not match selected native target')
            verify_binary(build / 'lib/libapp.so', self.args)
            if not signature:
                raise FixtureError('Server-managed artifact signature missing')
            if file_hash(build / 'lib/libapp.so') != self.patch_hash:
                raise FixtureError('Published patch does not match actual CLI-built native AOT image')
            transport = self.work / 'objects/releases' / storage
            if not transport.is_file():
                raise FixtureError('Published original patch transport missing')
            self.transport_hash = file_hash(transport)
            self.stage('actual-cli-patch-pro', {'number': self.number, 'hash': self.patch_hash,
                                              'transport_hash': self.transport_hash, 'artifact_arch': artifact_arch,
                                              'managed_signature_present': True})

    def launch(self, label, phase, number, *, mode='correct', online=False):
        if tree_hashes(self.bundle) != self.bundle_hashes:
            raise FixtureError('Preserved released bundle changed')
        nonce = secrets.token_hex(24)
        env = self.runtime_environment(mode=mode)
        env.update({'SHOREBIRD_E2E_OBSERVER': self.observer.url, 'SHOREBIRD_E2E_RECEIPT': nonce})
        if online:
            env.update({'SHOREBIRD_E2E_PROVISION_URL': self.provision.url,
                        'SHOREBIRD_E2E_PROVISION_TOKEN': self.secrets['provision']})
        elif self.api is not None or self.provision is not None:
            raise FixtureError('Offline launch requires stopped API and provisioning services')
        log = (self.work / (label + '.private.log')).open('ab')
        self.logs.append(log)
        self.app = self.job.spawn([str(self.runner)], cwd=self.bundle, env=env, stdout=log, stderr=log)
        def receipt():
            if self.app.poll() is not None:
                raise FixtureError('Released Runner exited before post-frame receipt')
            with self.receipt_lock:
                report = self.receipts.get(nonce)
            if report is not None:
                return validate_report(report, nonce, self.app.pid, number, phase)
        report = wait_for(receipt, self.args.runtime_timeout, label + ' rendered status/current patch')
        self.stage(label, {key: value for key, value in report.items() if key not in ('nonce', 'pid')})
        return report

    def stop_app(self):
        self.job.stop(self.app)
        self.app = None

    def go_offline(self):
        self.job.stop(self.api)
        self.api = None
        if self.provision:
            self.provision.close()
            self.provision = None
        # Listener closure is evidence of offline fixture services; no proxies.
        for url in (self.api_url, getattr(self, 'provision_url', self.api_url)):
            parsed = urllib.parse.urlparse(url)
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(('127.0.0.1', parsed.port)) == 0:
                    raise FixtureError('Owned online service is still accepting connections')
        self.stage('api-and-provisioning-stopped')

    def execute(self):
        self.prepare()
        self.start_api()
        self.bootstrap()
        self.make_observer()
        # No patch exists during first registration boot. Provisioning stays closed.
        self.provision = LocalService(self.authority.provision, '/key')
        self.provision_url = self.provision.url
        self.probe_provider()
        self.build()
        self.launch('initial-online-BASE', 'BASE', 0, online=True)
        def registration():
            state = strict_json((self.cache / 'state.json').read_text())
            events = self.rows('SELECT DISTINCT arch, platform, release_version FROM patch_events WHERE app_id=? AND client_id=? AND event_type=?',
                               (self.app_id, state['client_id'], 'PatchCheck'))
            if len(events) == 1:
                return state, events[0]
        state, (observed_arch, runtime_platform, version) = wait_for(registration, self.args.runtime_timeout, 'genuine updater client and runtime architecture')
        if observed_arch != runtime_arch(self.args) or runtime_platform != 'linux' or version != self.release_version:
            raise FixtureError('Genuine updater runtime context mismatch')
        self.stop_app()
        self.build(patch=True)
        self.authority.allow({'app_id': self.app_id, 'release_id': self.release_id, 'patch_number': self.number,
                              'patch_hash': self.patch_hash, 'transport_hash': self.transport_hash,
                              'platform': runtime_platform, 'arch': observed_arch,
                              'device_id_hash': sha256(state['client_id'].encode())})
        self.probe_context()
        accepted_before_download = self.authority.accepted
        self.launch('online-BASE-download', 'BASE', 0, online=True)
        candidate = wait_for(self.candidate, self.args.runtime_timeout, 'actual ciphertext installation')
        if self.authority.accepted <= accepted_before_download:
            raise FixtureError('Real updater did not provision a delivery key')
        # Exact cached ciphertext must be the server's managed encrypted object.
        objects = [p for p in (self.work / 'objects/patches').rglob('*') if p.is_file()]
        if not any(file_hash(p) == candidate['transport_sha256'] for p in objects):
            raise FixtureError('Installed ciphertext differs from real Go delivery')
        self.stage('ciphertext-installed', candidate)
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-U', 'U', self.number)
        wait_for(self.boot_confirmed, self.args.runtime_timeout, 'U boot-success lifecycle')
        self.stop_app()
        for mode in ('missing', 'wrong'):
            self.launch('offline-' + mode + '-key-BASE', 'BASE', 0, mode=mode)
            if self.candidate() != candidate:
                raise FixtureError('Failed-key boot destroyed or changed candidate')
            self.stop_app()
            self.launch('offline-' + mode + '-recovery-U', 'U', self.number)
            wait_for(self.boot_confirmed, self.args.runtime_timeout, 'recovered U boot success')
            self.stop_app()
        _key_id, key = delivery_material(self.authority.secret, self.authority.allowed_aad)
        self.forbidden_secrets = (key, key.hex().encode(), self.secrets['master'].encode(),
                                  self.secrets['provision'].encode())
        self.stage('final-cache-before-rollback', scan_cache(
            self.cache, patch_hash=self.patch_hash, forbidden_secrets=self.forbidden_secrets))
        self.start_api()
        self.request('POST', f'/api/v1/apps/{self.app_id}/releases/{self.release_id}/patches/{self.patch_id}/rollback')
        # API returns signed rollback in the real updater check; no state edits.
        self.provision = LocalService(self.authority.provision, '/key')
        self.provision_url = self.provision.url
        self.launch('online-U-receive-signed-rollback', 'U', self.number, online=True)
        def rolled_back():
            pointers = strict_json((self.cache / 'pointers.json').read_text())
            return pointers.get('next_boot_patch') is None and pointers.get('last_booted_patch') is None
        wait_for(rolled_back, self.args.runtime_timeout, 'authenticated rollback applied by updater')
        self.stop_app()
        self.go_offline()
        self.launch('offline-cold-rollback-BASE', 'BASE', 0)
        self.stop_app()
        self.stage('final-cache-after-rollback', scan_cache(
            self.cache, patch_hash=self.patch_hash, forbidden_secrets=self.forbidden_secrets))
        if tree_hashes(self.bundle) != self.bundle_hashes:
            raise FixtureError('Original release bundle was mutated')
        self.stage('released-bundle-unchanged', self.bundle_hashes)
        self.manifest['provisioning'] = {'accepted': self.authority.accepted, 'rejected': self.authority.rejected,
                                         'context_source': 'genuine updater state + runtime PatchCheck + published signed DB/artifacts'}
        self.manifest['result'] = 'passed'

    def runtime_environment(self, *, mode='correct'):
        if mode not in ('correct', 'missing', 'wrong') or self.ring is None:
            raise FixtureError('Invalid provider mode or missing owned session keyring')
        env = self.environment()
        for name in tuple(env):
            if name.startswith(('SHOREBIRD_', 'PATCH_DELIVERY_', 'PATCH_SIGNING_')):
                del env[name]
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(name, None)
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
        env.update({'SHOREBIRD_E2E_KEYRING_ID': str(self.ring.serial),
                    'SHOREBIRD_E2E_KEY_MODE': mode})
        return env

    def probe_provider(self):
        self.run_command([self.args.provider_probe, '--self-test'],
                         env=self.runtime_environment(), name='native-probe-self-test')
        self.stage('native-probe-self-test')

    def probe_context(self):
        # Use the exact genuine updater context, then clear the ring so this
        # probe cannot satisfy the app's subsequent online provisioning oracle.
        aad = self.authority.allowed_aad
        key_id, key = delivery_material(self.authority.secret, aad)
        context_path = self.work / 'provider-context.txt'
        private_write(context_path, aad)
        command = [self.args.provider_probe, '--read-context', key_id, context_path]
        env = self.runtime_environment()
        env.update({'SHOREBIRD_E2E_PROVISION_URL': self.provision.url,
                    'SHOREBIRD_E2E_PROVISION_TOKEN': self.secrets['provision']})
        accepted = self.authority.accepted
        self.run_command(command, env=env, input=key, name='native-probe-online')
        if self.authority.accepted <= accepted:
            raise FixtureError('Native probe did not use genuine allowlisted provisioning')
        self.stage('native-probe-online', {'context_source': 'genuine updater and published signed artifact',
                                           'expected_key_source': 'stdin only'})
        accepted = self.authority.accepted
        self.run_command(command, env=self.runtime_environment(), input=key,
                         name='native-probe-offline-fresh-process')
        if self.authority.accepted != accepted:
            raise FixtureError('Offline native probe unexpectedly provisioned a key')
        if self.ring.lib.keyctl_clear(self.ring.serial) != 0:
            raise FixtureError('Cannot clear probe key before actual app provisioning')
        self.stage('native-probe-offline', {
            'context_source': 'genuine updater and published signed artifact',
            'expected_key_source': 'stdin only', 'fresh_process_offline': True,
            'probe_key_cleared_before_app_download': True})

    def cleanup(self):
        failures = []
        actions = [('app', self.stop_app), ('api', lambda: self.job.stop(self.api)),
                   ('process-groups', self.job.close)]
        actions += [(name, service.close) for name, service in
                    (('provision-service', self.provision), ('observer-service', self.observer)) if service]
        actions += [('log', log.close) for log in self.logs]
        if self.ring is not None:
            actions.append(('keyring', self.ring.close))
        if self.work_owned:
            actions.append(('work-tree', lambda: shutil.rmtree(self.work) if self.work.exists() else None))
        for name, action in actions:
            try:
                action()
            except Exception as error:
                failures.append({'action': name, 'type': type(error).__name__})
        if failures:
            self.manifest['cleanup_failures'] = failures
            raise FixtureError('Owned resource cleanup failed')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('phase', choices=('plan', 'run'), nargs='?', default='plan')
    result.add_argument('--execute', action='store_true')
    result.add_argument('--arch', choices=('x64', 'arm64'), default='x64')
    result.add_argument('--work', type=Path)
    result.add_argument('--results', type=Path, help='New sanitized results.json directory')
    result.add_argument('--engine-src', type=Path, default=ROOT / 'flutter/engine/src')
    result.add_argument('--host-engine', default='host_release')
    result.add_argument('--target-engine', default='host_release')
    result.add_argument('--tooling-dart', type=Path, default=ROOT / 'flutter/bin/cache/dart-sdk/bin/dart')
    result.add_argument('--patch-tool', type=Path, default=ROOT / 'updater/target/release/patch')
    result.add_argument('--provider-probe', type=Path)
    result.add_argument('--pub-cache', type=Path, default=Path(os.environ.get('PUB_CACHE', Path.home() / '.pub-cache')))
    result.add_argument('--runtime-timeout', type=int, default=120)
    result.add_argument('--build-timeout', type=int, default=1800)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.phase == 'plan':
        print(json.dumps({'schema': SCHEMA, 'read_only': True, 'host': platform.system(),
                          'target_arch': args.arch, 'runtime_arch': runtime_arch(args),
            'requires': ['Linux ' + args.arch + ' native X11/DBus session', 'GTK/libcurl/GLib/keyutils development libraries',
                         'anonymous kernel session keyrings permitted', 'Go, Git, CMake, Ninja, clang++, Xvfb/xauth',
                         'prebuilt matching full Dart SDK/frontend/gen_snapshot/const_finder/font-subset',
                         'Linux GTK library/public headers/ICU/sky_engine and release product platform dill',
                         'populated CLI/Flutter configs and copyable caches', 'public updater patch and provider probe'],
            'lifecycle': ['actual CLI free release + genuine updater registration', 'actual CLI pro patch',
                          'Go signed per-device encrypted delivery', 'native online/offline context probe',
                          'real app ciphertext installation', 'stop API/provision',
                          'new app process offline rendered pro/current patch',
                          'missing/wrong key baseline with retained ciphertext + recovery',
                          'genuine signed rollback', 'new app process offline baseline'],
            'key_persistence_scope': 'cold app processes within one kernel session; no reboot/session-restart claim',
            'audit': 'final app-cache files only; transient write audit unimplemented'}, indent=2))
        return 0
    if not args.execute:
        raise SystemExit('run requires --execute')
    try:
        architecture.require_native_host('linux', args.arch)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    if args.work is None or args.results is None:
        raise SystemExit('run requires new --work and --results directories')
    for name in ('work', 'results', 'engine_src', 'tooling_dart', 'patch_tool', 'provider_probe', 'pub_cache'):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    if (args.work.exists() or args.results.exists() or args.results == args.work or
            args.work in args.results.parents or args.results in args.work.parents):
        raise SystemExit('Use separate new session/results directories; existing paths are never overwritten')
    args.results.mkdir(parents=True, mode=0o700)
    lifecycle = None
    manifest = {'schema': SCHEMA, 'result': 'failed', 'stages': [],
                'validation': 'real Linux GTK CLI/server/Runner lifecycle', 'transient_write_trace': False}
    try:
        manifest['target_arch'] = args.arch
        manifest['runtime_arch'] = runtime_arch(args)
        manifest['binary_architectures'] = preflight(args)
        lifecycle = Lifecycle(args)
        for name in ('target_arch', 'runtime_arch', 'binary_architectures'):
            lifecycle.manifest[name] = manifest[name]
        manifest = lifecycle.manifest
        lifecycle.execute()
    except Exception as error:
        # Only authored errors may include text. Build/HTTP errors can carry
        # credentials or signed URLs, so their raw text never leaves private work.
        manifest['failure'] = {'type': type(error).__name__,
            'after_stage': manifest['stages'][-1]['name'] if manifest['stages'] else
                           ('preparation' if lifecycle else 'preflight')}
        if isinstance(error, FixtureError):
            manifest['failure']['reason'] = str(error)
        print('Linux lifecycle failed; sanitized stage evidence retained.', flush=True)
        # Only fixed enums, numbers and authored FixtureError text are printed.
        detail = manifest.get('command_failure') or {}
        print('Linux lifecycle failure: type={} after_stage={} reason={} command={} exit_code={} timed_out={} classification={}'.format(
            manifest['failure']['type'], manifest['failure']['after_stage'],
            manifest['failure'].get('reason', 'unavailable'), detail.get('operation', 'none'),
            detail.get('exit_code'), detail.get('timed_out'), detail.get('classification', 'none')), flush=True)
    finally:
        try:
            if lifecycle:
                lifecycle.cleanup()
                manifest['cleanup'] = ('owned processes, ports, disposable app cache, private session removed; '
                                       'owned session keyring revoked')
            else:
                manifest['cleanup'] = 'no session or runtime resources created; work untouched'
        except Exception as error:
            manifest['cleanup'] = 'failed: ' + type(error).__name__
            manifest['result'] = 'failed'
        private_write(args.results / 'results.json', json.dumps(manifest, indent=2))
    return 0 if manifest['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

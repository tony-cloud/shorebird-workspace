"""Actual empty-environment Dart file/data regression; no Flutter/app/device.

Set IOS_VARIANT_TEST_DART to the pinned host dart binary when not on PATH.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

import ios_variant_e2e as runner

DART = os.environ.get('IOS_VARIANT_TEST_DART') or shutil.which('dart')


@unittest.skipUnless(DART, 'Set IOS_VARIANT_TEST_DART for the actual empty-env Dart regression')
class DartLaunchContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ios-dart-context-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        data = (runner.TEMPLATES / 'variant_status.dart.template').read_text()
        data = data[data.index('Map<String, Object?> updateFixtureData('):data.index('Map<String, Object?> selectionIdentity(')]
        data = data.replace('__SENTINEL__', 'test-sentinel')
        cls.script = cls.work / 'context_test.dart'
        cls.script.write_text("import 'dart:convert';\nimport 'dart:io';\nimport 'dart:ffi';\n"
            "const fixtureSession = '0123456789ab';\nconst fixtureBundleId = 'org.tonycloud.shorebird-ios-variant.0123456789ab';\n"
            "const fixtureObserverUrl = 'http://192.0.2.1:12345/report/0123456789ab';\nconst fixtureVariant = 'free';\nconst fixtureSchema = 1;\n" +
            (runner.TEMPLATES / 'LaunchContext.dart.template').read_text() + data + r'''
// macOS CoreFoundation adds this bookkeeping variable after env={} exec.
// Remove it only in this disposable TEST process, before Dart caches environ.
// The production context reader has no environment or FFI dependency.
@Native<Pointer<Uint8> Function(UintPtr)>(symbol: 'malloc')
external Pointer<Uint8> testAllocate(int length);
@Native<Void Function(Pointer<Void>)>(symbol: 'free')
external void testFree(Pointer<Void> pointer);
@Native<Int32 Function(Pointer<Uint8>)>(symbol: 'unsetenv')
external int testUnset(Pointer<Uint8> name);
void main(List<String> args) {
  if (Platform.isMacOS) {
    final name = '__CF_USER_TEXT_ENCODING'.codeUnits;
    final pointer = testAllocate(name.length + 1);
    if (pointer == nullptr) throw StateError('Test allocation failed');
    try {
      for (var i = 0; i < name.length; i++) { pointer[i] = name[i]; }
      pointer[name.length] = 0;
      if (testUnset(pointer) != 0) throw StateError('Could not clear test-only CF environment');
    } finally { testFree(pointer.cast<Void>()); }
  }
  if (Platform.environment.isNotEmpty) throw StateError('The test must have an actually empty Dart environment');
  final temp = Directory(args[0]);
  final value = jsonDecode(stdin.readLineSync()!) as Map<String, dynamic>;
  if (value['pid'] == 'SELF') value['pid'] = pid;
  if (value['created_at'] == 'NOW') value['created_at'] = DateTime.now().millisecondsSinceEpoch / 1000;
  final file = File('${temp.path}/shorebird-ios-variant-$fixtureSession.json');
  if (args[1] == 'symlink') {
    final redirected = File('${temp.path}/redirected.json')..writeAsStringSync(jsonEncode(value));
    Link(file.path).createSync(redirected.path);
  } else {
    file.writeAsStringSync(args[1] == 'oversized' ? 'x' * 8193 : jsonEncode(value), flush: true);
    if (args[1] == 'old-mtime') file.setLastModifiedSync(DateTime.now().subtract(const Duration(minutes: 5)));
  }
  try {
    final context = _readFixtureLaunchContext(temp);
    final data = updateFixtureData(context);
    print(jsonEncode({'accepted': true, 'empty_environment': Platform.environment.isEmpty,
      'nonce': context.nonce, 'check': context.check, 'status_route': context.statusRoute,
      'report_route': context.reportRoute, 'observer': context.observer.toString(),
      'evidence': context.evidenceDirectory, 'launches': data['launches']}));
  } on Object catch (error) {
    print(jsonEncode({'accepted': false, 'error_type': error.runtimeType.toString()}));
  }
}
''')

    def run_case(self, mutate=None, *, mode='normal', alias=False):
        with tempfile.TemporaryDirectory(prefix='ios-context-case-') as directory:
            root = Path(directory).resolve()
            app = root / 'app'
            temporary = app / 'tmp'
            evidence = app / 'Library/Application Support/shorebird-ios-variant-fixture'
            temporary.mkdir(parents=True)
            evidence.mkdir(parents=True)
            if alias:
                alias_app = root / 'alias-app'
                alias_app.symlink_to(app, target_is_directory=True)
                temporary = alias_app / 'tmp'
                context_evidence = alias_app / 'Library/Application Support/shorebird-ios-variant-fixture'
            else:
                context_evidence = evidence
            value = dict(schema='ios-variant-launch-context-v1', session='0123456789ab',
                bundle_id='org.tonycloud.shorebird-ios-variant.0123456789ab', nonce='a' * 48,
                check=True, report_route='native', status_route='public', evidence_dir=str(context_evidence),
                observer_url='http://192.0.2.1:12345/report/0123456789ab', pid='SELF', created_at='NOW')
            if mutate: mutate(value)
            result = subprocess.run([str(Path(DART).resolve()), '--disable-dart-dev', str(self.script), str(temporary), mode],
                input=json.dumps(value).encode() + b'\n', env={}, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[:2000])
            parsed = json.loads(result.stdout)
            user = evidence / 'user-data.json'
            parsed['data_exists'] = user.exists()
            if user.exists(): parsed['data'] = json.loads(user.read_text())
            parsed['expected_evidence'] = str(evidence.resolve())
            return parsed

    def test_actual_empty_environment_uses_native_context_for_data_nonce_check_routes(self):
        value = self.run_case()
        self.assertTrue(value['accepted'])
        self.assertTrue(value['empty_environment'])
        self.assertTrue(value['check'])
        self.assertEqual(value['nonce'], 'a' * 48)
        self.assertEqual((value['status_route'], value['report_route']), ('public', 'native'))
        self.assertEqual(value['observer'], 'http://192.0.2.1:12345/report/0123456789ab')
        self.assertEqual(value['evidence'], value['expected_evidence'])
        self.assertEqual(value['data']['sentinel'], 'test-sentinel')
        self.assertEqual(value['data']['history'], ['free'])
        alternate = self.run_case(lambda x: x.update(check=False, status_route='native-ffi', report_route='dart'))
        self.assertTrue(alternate['accepted'])
        self.assertFalse(alternate['check'])
        self.assertEqual((alternate['status_route'], alternate['report_route']), ('native-ffi', 'dart'))
        file_route = self.run_case(lambda x: x.update(report_route='file'))
        self.assertTrue(file_route['accepted'])
        self.assertEqual(file_route['report_route'], 'file')

    def test_actual_context_canonicalizes_aliases_like_var_private_var(self):
        value = self.run_case(alias=True)
        self.assertTrue(value['accepted'])
        self.assertEqual(value['evidence'], value['expected_evidence'])

    def test_actual_invalid_stale_unbound_contexts_fail_before_data(self):
        mutations = {
            'schema': lambda x: x.update(schema='foreign'),
            'session': lambda x: x.update(session='b' * 12),
            'bundle': lambda x: x.update(bundle_id='org.foreign'),
            'nonce': lambda x: x.update(nonce='not-a-nonce'),
            'missing': lambda x: x.pop('nonce'),
            'extra-secret-field': lambda x: x.update(key_hex='never-accept-this-secret'),
            'pid': lambda x: x.update(pid=0),
            'stale': lambda x: x.update(created_at=time.time() - 1000),
            'future': lambda x: x.update(created_at=time.time() + 60),
            'check-type': lambda x: x.update(check='1'),
            'report': lambda x: x.update(report_route='foreign'),
            'status': lambda x: x.update(status_route='foreign'),
            'observer': lambda x: x.update(observer_url='http://192.0.2.1:11111/report/0123456789ab'),
            'path': lambda x: x.update(evidence_dir='/tmp'),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                value = self.run_case(mutation)
                self.assertFalse(value['accepted'])
                self.assertFalse(value['data_exists'])
        for mode in ('symlink', 'oversized', 'old-mtime'):
            with self.subTest(mode=mode):
                value = self.run_case(mode=mode)
                self.assertFalse(value['accepted'])
                self.assertFalse(value['data_exists'])


if __name__ == '__main__':
    unittest.main()

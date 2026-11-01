"""Opt-in Android V2 generation and real host-JVM authorization-gate checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import android_variant_app as wiring

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = 'dev.shorebird.e2e.fixture'
APP_ID = '11111111-1111-4111-8111-111111111111'
SESSION = 'c' * 32
HEADER = ROOT / 'flutter/engine/src/flutter/shell/common/shorebird/shorebird_patch_encryption.h'


def fixture_context():
    return {'version': 2, 'selection_digest': 'd' * 64, 'selection': {
        'epoch': '2', 'scope': {'app_id': APP_ID, 'release_version': '1.0.0+1',
            'platform': 'android', 'arch': 'aarch64', 'client_id_hash': 'c' * 64,
            'base_artifact_hash': 'e' * 64, 'host_abi_hash': None, 'runtime_profile': 'native-aot'},
        'channel': 'stable', 'variant_id': 'pro', 'license_type': 'pro',
        'entitlement_revision': SESSION + ':grant', 'not_before': '1', 'offline_valid_until': None,
        'target': {'kind': 'patch', 'patch_number': '2', 'artifact_hash': 'a' * 64,
                   'encryption': 'transport-aes-256-gcm', 'artifact_key_id': None},
        'fallback': {'kind': 'base', 'variant_id': 'free', 'license_type': 'free'}}}


def scaffold(root):
    app = Path(root) / 'app'
    (app / 'android/app/src/main').mkdir(parents=True)
    (app / 'android/settings.gradle.kts').write_text('// disposable')
    return app


class GeneratorTest(unittest.TestCase):
    def test_all_native_architectures_use_opt_in_v2_callback_and_owned_paths(self):
        original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in wiring.v1.TEMPLATES.iterdir() if p.is_file()}
        for arch, (abi, _) in wiring.ARCHES.items():
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as directory:
                app = scaffold(directory)
                value = wiring.inject_app(app, PACKAGE, 'a' * 64, HEADER, app_id=APP_ID,
                    release_version='1.0.0+1', session=SESSION, arch=arch)
                self.assertEqual(value['android_abi'], abi)
                main = app / 'android/app/src/main'
                java = main / 'java' / PACKAGE.replace('.', '/')
                provider = (java / 'PatchKeyProvider.java').read_text()
                bridge = (main / 'cpp/fixture_jni.cpp').read_text()
                control = (java / 'ControlActivity.java').read_text()
                self.assertIn('shorebird_set_patch_decryption_key_provider_v2', bridge)
                self.assertNotIn('dlsym(flutter, "shorebird_set_patch_decryption_key_provider")', bridge)
                self.assertIn('"([B[B[B)[B"', bridge)
                for name in ('SHOREBIRD_VARIANT_APP_DATA_DIR', 'SHOREBIRD_VARIANT_CONTEXTS',
                             'SHOREBIRD_VARIANT_PROVIDER_RECEIPT', 'SHOREBIRD_VARIANT_CHECK'):
                    self.assertIn(name, bridge)
                self.assertIn('"native-aot"', provider)
                self.assertIn('"' + arch + '"', provider)
                self.assertIn('shorebird-variant-app-data', control)
                self.assertNotIn('nativeProbe', control)
                self.assertNotIn('JSONObject inspect(', provider)
                self.assertIn('abiFilters += "' + abi + '"', (app / 'android/app/build.gradle.kts').read_text())
                self.assertIn('isDebuggable = false', (app / 'android/app/build.gradle.kts').read_text())
                self.assertIn('"' + abi + '"', (main / 'cpp/CMakeLists.txt').read_text())
                for path in (*java.glob('*.java'), main / 'cpp/fixture_jni.cpp', main / 'cpp/CMakeLists.txt'):
                    self.assertNotRegex(path.read_text(), r'__[A-Z_]+__')
                with self.assertRaises(ValueError):
                    wiring.inject_app(app, PACKAGE, 'a' * 64, HEADER, app_id=APP_ID,
                        release_version='1.0.0+1', session=SESSION, arch=arch)
        self.assertEqual(original, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in wiring.v1.TEMPLATES.iterdir() if p.is_file()})

    def test_rejects_invalid_identity_and_legacy_header_before_creating_native_sources(self):
        for values in ({'app_id': 'not-a-uuid'}, {'release_version': 'bad"version'},
                       {'session': 'untrusted\n'}, {'arch': 'arm64'}):
            with tempfile.TemporaryDirectory() as directory:
                app = scaffold(directory)
                options = dict(app_id=APP_ID, release_version='1.0.0+1', session=SESSION, arch='aarch64')
                options.update(values)
                with self.assertRaises(ValueError):
                    wiring.inject_app(app, PACKAGE, 'a' * 64, HEADER, **options)
                self.assertFalse((app / 'android/app/src/main/cpp').exists())
        with tempfile.TemporaryDirectory() as directory:
            app = scaffold(directory)
            header = Path(directory) / 'legacy.h'; header.write_text('legacy callback only')
            with self.assertRaises(ValueError):
                wiring.inject_app(app, PACKAGE, 'a' * 64, header, app_id=APP_ID,
                    release_version='1.0.0+1', session=SESSION)

    @unittest.skipUnless(os.environ.get('ANDROID_VARIANT_ANDROID_JAR') and
                         os.environ.get('ANDROID_VARIANT_JSON_JAR') and os.environ.get('JAVA_HOME'),
                         'Explicit installed host-JVM/Android/org.json inputs are required')
    def test_real_java_gate_rejects_contexts_before_even_fake_cached_key_source(self):
        java_home = Path(os.environ['JAVA_HOME'])
        android = Path(os.environ['ANDROID_VARIANT_ANDROID_JAR'])
        json_jar = Path(os.environ['ANDROID_VARIANT_JSON_JAR'])
        with tempfile.TemporaryDirectory(prefix='android-variant-jvm-') as directory:
            app = scaffold(directory)
            wiring.inject_app(app, 'dev.shorebird.e2e.compilecheck', 'a' * 64, HEADER,
                app_id=APP_ID, release_version='1.0.0+1', session=SESSION)
            work = Path(directory)
            provider = next(app.rglob('PatchKeyProvider.java'))
            check = wiring.TEMPLATES / 'VariantContractCheck.java'
            classes = work / 'classes'; classes.mkdir()
            classpath = os.pathsep.join(map(str, (json_jar, android)))
            subprocess.run([str(java_home / 'bin/javac'), '--release', '17', '-cp', classpath,
                '-d', str(classes), str(provider), str(check)], check=True, timeout=60,
                capture_output=True, text=True)
            aad = ('open-aot-patch-delivery-v2\napp_id=' + APP_ID + '\nrelease_id=17\npatch_number=2'
                '\npatch_hash=' + 'a' * 64 + '\ntransport_hash=' + 'b' * 64 +
                '\nplatform=android\narch=aarch64\ndevice_id_hash=' + 'c' * 64)
            source = work / 'context.json'
            source.write_text(json.dumps({'context': fixture_context(), 'aad': aad, 'key_id': 'AbCdEfGhIjKlMnOp'}))
            result = subprocess.run([str(java_home / 'bin/java'), '-cp', str(classes) + os.pathsep + classpath,
                'dev.shorebird.e2e.compilecheck.VariantContractCheck', str(source)], check=True,
                capture_output=True, text=True, timeout=30)
            self.assertIn('native V2 verified-context/cache-gate checks', result.stdout)
            self.assertIn('Android runtime unrun', result.stdout)


if __name__ == '__main__':
    unittest.main()

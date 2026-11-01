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
    @unittest.skipUnless(os.environ.get('ANDROID_VARIANT_ANDROID_JAR') and
                         os.environ.get('ANDROID_VARIANT_JSON_JAR') and os.environ.get('JAVA_HOME'),
                         'Explicit installed host-JVM/Android/org.json inputs are required')
    def test_real_java_cache_controls_enforce_cas_containment_and_never_touch_journal(self):
        java_home = Path(os.environ['JAVA_HOME'])
        classpath = os.pathsep.join((os.environ['ANDROID_VARIANT_JSON_JAR'], os.environ['ANDROID_VARIANT_ANDROID_JAR']))
        with tempfile.TemporaryDirectory(prefix='android-variant-cache-jvm-') as directory:
            root = Path(directory).resolve(); app = scaffold(root)
            wiring.inject_app(app, 'dev.shorebird.e2e.compilecheck', 'a' * 64, HEADER,
                app_id=APP_ID, release_version='1.0.0+1', session=SESSION)
            java = next(app.rglob('ControlActivity.java')).parent
            (root/'MainActivity.java').write_text('package dev.shorebird.e2e.compilecheck; public final class MainActivity extends android.app.Activity {}')
            (root/'CacheCheck.java').write_text(r'''
package dev.shorebird.e2e.compilecheck;
import java.io.*; import java.nio.file.*; import java.security.*; import java.util.*; import org.json.*;
public final class CacheCheck {
  interface Action { void run() throws Exception; }
  static int checked;
  static void require(boolean value) { if(!value) throw new AssertionError(); checked++; }
  static void rejects(Action action) throws Exception { try { action.run(); } catch(IOException error) { checked++; return; } throw new AssertionError("Accepted unsafe mutation"); }
  static String hash(Path path) throws Exception { return PatchKeyProvider.hex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(path))); }
  public static void main(String[] args) throws Exception {
    Path work=Paths.get(args[0]),files=work.resolve("files"),code=work.resolve("code");
    Path patch=files.resolve("shorebird_updater/patches/2"),journal=files.resolve("shorebird_updater/selections-v2/"+"a".repeat(64)+".json");
    Files.createDirectories(patch); Files.createDirectories(journal.getParent());
    Path downloads=code.resolve("shorebird_updater/downloads"); Files.createDirectories(downloads);
    Path state=patch.resolve("state.json"),cipher=patch.resolve("transport.aesgcm");
    Files.writeString(state,"{\"kind\":\"InstalledEncryptedTransportV1\",\"signature\":\"AbCdEfGh\",\"size\":17,\"ciphertext_size\":32}");
    Files.write(cipher,new byte[32]); Files.writeString(journal,"original signed authority, never rewritten");
    ControlActivity.CacheControls cache=new ControlActivity.CacheControls(files.toFile(),code.toFile(),file->{
      Path path=file.toPath(); return Files.isSymbolicLink(path)?'x':Files.isDirectory(path)?'d':Files.isRegularFile(path)&&((Number)Files.getAttribute(path,"unix:nlink")).longValue()==1?'f':'x';
    });
    String authority=hash(journal),before=hash(cipher),metadata=hash(state);
    require(cache.snapshot().getString("scope").equals("hashes-only-no-key-reads"));
    Path unfinished=downloads.resolve("2"); Files.write(unfinished,new byte[32]); rejects(()->cache.snapshot()); Files.delete(unfinished);
    rejects(()->cache.tamper("tamper-ciphertext","2","f".repeat(64))); require(hash(cipher).equals(before));
    rejects(()->cache.tamper("tamper-ciphertext","../2",before));
    rejects(()->cache.tamper("restore","2",before));
    rejects(()->cache.tamper("tamper-ciphertext","3",before));
    JSONObject changed=cache.tamper("tamper-ciphertext","2",before);
    require(changed.getString("before_sha256").equals(before)); require(!hash(cipher).equals(before));
    require(hash(state).equals(metadata)); require(hash(journal).equals(authority));
    rejects(()->cache.tamper("tamper-ciphertext","2",before));
    JSONObject original=new JSONObject(Files.readString(state));
    cache.tamper("tamper-signature-metadata","2",metadata);
    JSONObject after=new JSONObject(Files.readString(state));
    require(!original.getString("signature").equals(after.getString("signature")));
    after.put("signature",original.getString("signature")); require(after.similar(original)); require(hash(journal).equals(authority));
    Path outside=work.resolve("outside"); Files.write(outside,new byte[32]);
    Files.delete(cipher); Files.createSymbolicLink(cipher,outside);
    rejects(()->cache.snapshot()); rejects(()->cache.tamper("tamper-ciphertext","2",hash(outside)));
    require(Files.readAllBytes(outside)[16]==0); Files.delete(cipher); Files.write(cipher,new byte[32]);
    Path link=work.resolve("hardlink"); Files.createLink(link,cipher); rejects(()->cache.snapshot()); Files.delete(link);
    Path unknown=patch.resolve("unexpected"); Files.writeString(unknown,"refuse unknown cache entry"); rejects(()->cache.snapshot()); Files.delete(unknown);
    Path duplicate=code.resolve("shorebird_updater/patches/2/transport.aesgcm"); Files.createDirectories(duplicate.getParent()); Files.write(duplicate,new byte[32]);
    rejects(()->cache.tamper("tamper-ciphertext","2",hash(cipher))); require(hash(journal).equals(authority));
    System.out.println("PASS "+checked+" actual cache CAS/containment controls; Android runtime unrun");
  }
}
''')
            classes = root/'classes'; classes.mkdir()
            compiled = subprocess.run([str(java_home/'bin/javac'), '--release', '17', '-cp', classpath, '-d', str(classes),
                str(java/'PatchKeyProvider.java'), str(java/'ControlActivity.java'), str(root/'MainActivity.java'),
                str(root/'CacheCheck.java')], timeout=60, capture_output=True, text=True)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            result = subprocess.run([str(java_home/'bin/java'), '-cp', str(classes)+os.pathsep+classpath,
                'dev.shorebird.e2e.compilecheck.CacheCheck', str(root/'cache')], timeout=30,
                capture_output=True, text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('actual cache CAS/containment controls; Android runtime unrun', result.stdout)

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

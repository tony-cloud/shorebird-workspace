"""Disposable Android fixture wiring and an owned read-only artifact mirror."""
from __future__ import annotations

import functools
import http.server
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import urllib.parse
import xml.etree.ElementTree as ET

TEMPLATES = Path(__file__).with_name('android_native')


def inject_app(app, package, control_sha256, header_path):
    app, header_path = Path(app), Path(header_path)
    if not re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}', package):
        raise ValueError('Invalid disposable package')
    if not re.fullmatch(r'[0-9a-f]{64}', control_sha256):
        raise ValueError('Invalid control capability digest')
    android = app / 'android'
    main = android / 'app/src/main'
    if not (android / 'settings.gradle.kts').is_file() or not main.is_dir():
        raise ValueError('Expected isolated fixture Android scaffold')
    if (main / 'cpp').exists():
        raise ValueError('Refusing to replace an existing native app project')
    # These are generated/disposable app sources, never the repository fixture.
    for language in ('java', 'kotlin'):
        directory = main / language
        if directory.exists():
            shutil.rmtree(directory)
    java = main / 'java' / package.replace('.', '/')
    java.mkdir(parents=True)
    native = main / 'cpp'
    native.mkdir()
    substitutions = {'__JAVA_PACKAGE__': package,
                     '__CONTROL_SHA256__': control_sha256}
    for source_name, destination in (
            ('PatchKeyProvider.java.template', java / 'PatchKeyProvider.java'),
            ('ControlActivity.java.template', java / 'ControlActivity.java'),
            ('MainActivity.java.template', java / 'MainActivity.java'),
            ('fixture_jni.cpp', native / 'fixture_jni.cpp'),
            ('CMakeLists.txt', native / 'CMakeLists.txt')):
        text = (TEMPLATES / source_name).read_text()
        for marker, replacement in substitutions.items():
            text = text.replace(marker, replacement)
        if any(marker in text for marker in substitutions):
            raise ValueError('Unexpanded Android fixture template')
        destination.write_text(text)
    shutil.copy2(header_path, native / 'shorebird_patch_encryption.h')
    (android / 'app/build.gradle.kts').write_text('''plugins {
    id("com.android.application")
    id("dev.flutter.flutter-gradle-plugin")
}
android {
    namespace = "''' + package + '''"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    defaultConfig {
        applicationId = "''' + package + '''"
        minSdk = 24
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
        // Flutter installs its supported ABI defaults while applying the plugin.
        // Replace those defaults; adding ARM64 would retain ARM32 and x86_64.
        ndk {
            abiFilters.clear()
            abiFilters += "arm64-v8a"
        }
    }
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("debug")
            isDebuggable = false
            isJniDebuggable = false
            isMinifyEnabled = false
            isShrinkResources = false
        }
    }
}
flutter { source = "../.." }
''')
    (main / 'AndroidManifest.xml').write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET"/>
    <application android:name="android.app.Application" android:label="Shorebird Native Test"
        android:icon="@mipmap/ic_launcher" android:allowBackup="false"
        android:usesCleartextTraffic="true">
        <activity android:name=".ControlActivity" android:exported="true"
            android:launchMode="standard" android:theme="@style/LaunchTheme"/>
        <activity android:name=".MainActivity" android:exported="false"
            android:launchMode="singleTop" android:theme="@style/LaunchTheme"
            android:configChanges="orientation|keyboardHidden|keyboard|screenSize|smallestScreenSize|locale|layoutDirection|fontScale|screenLayout|density|uiMode"
            android:hardwareAccelerated="true" android:windowSoftInputMode="adjustResize">
            <meta-data android:name="io.flutter.embedding.android.NormalTheme" android:resource="@style/NormalTheme"/>
        </activity>
        <meta-data android:name="flutterEmbedding" android:value="2"/>
    </application>
</manifest>
''')
    properties = android / 'gradle.properties'
    existing = properties.read_text() if properties.exists() else ''
    existing = re.sub(r'^org\.gradle\.daemon=.*\n?', '', existing, flags=re.M)
    properties.write_text(existing.rstrip() + '\norg.gradle.daemon=false\n')


def validate_manifest_xml(xml, package):
    manifest = ET.fromstring(xml)
    android = '{http://schemas.android.com/apk/res/android}'
    application = manifest.find('application')
    if (manifest.tag != 'manifest' or manifest.get('package') != package or
            application is None or application.get(android + 'debuggable') not in (None, 'false') or
            application.get(android + 'allowBackup') != 'false' or
            application.get(android + 'name') != 'android.app.Application'):
        raise ValueError('Compiled fixture application manifest differs from required policy')
    if any(application.findall(name) for name in ('provider', 'receiver', 'service', 'activity-alias')):
        raise ValueError('Unexpected merged fixture component or prewarming provider')
    observed = {}
    for activity in application.findall('activity'):
        name = activity.get(android + 'name', '')
        if name.startswith('.'):
            name = package + name
        if name in observed:
            raise ValueError('Duplicate merged activity')
        observed[name] = activity.get(android + 'exported')
    if observed != {package + '.ControlActivity': 'true', package + '.MainActivity': 'false'}:
        raise ValueError('Compiled fixture activity exposure differs from required policy')
    sdk = manifest.find('uses-sdk')
    if sdk is None or int(sdk.get(android + 'minSdkVersion', '0')) < 24:
        raise ValueError('Compiled fixture minimum SDK is unsupported')
    return {'debuggable': False, 'allow_backup': False, 'prewarming_providers': 0,
            'exported_activities': ['ControlActivity'], 'main_activity_exported': False}


def verify_apk_manifest(apk, package, android_sdk, *, java_home=None):
    analyzer = Path(android_sdk) / 'cmdline-tools/latest/bin/apkanalyzer'
    if not analyzer.is_file():
        raise ValueError('Installed apkanalyzer is required for compiled manifest verification')
    env = dict(os.environ)
    if java_home:
        env['JAVA_HOME'] = str(java_home)
    result = subprocess.run([str(analyzer), 'manifest', 'print', str(apk)],
                            capture_output=True, text=True, env=env, timeout=60)
    if result.returncode:
        raise ValueError('Compiled APK manifest inspection failed')
    policy = validate_manifest_xml(result.stdout, package)
    # Release Gradle configuration supplies the flag; an omitted manifest
    # attribute also defaults to false. Check the analyzer's resolved value.
    debuggable = subprocess.run([str(analyzer), 'manifest', 'debuggable', str(apk)],
                                capture_output=True, text=True, env=env, timeout=60)
    if debuggable.returncode or debuggable.stdout.strip() != 'false':
        raise ValueError('Compiled APK must be non-debuggable')
    return policy


class _MirrorHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def list_directory(self, _path):
        self.send_error(404)
        return None

    def _safe_request(self):
        request = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(request.path, errors='strict')
        if request.query or request.fragment or '\\' in path or '\0' in path:
            return False
        components = path.split('/')[1:]
        if not path.startswith('/') or any(part in ('', '.', '..') for part in components):
            return False
        target = Path(self.directory)
        for component in components:
            target /= component
            if target.is_symlink():
                return False
        return target.is_file()

    def send_head(self):
        try:
            safe = self._safe_request()
        except (ValueError, UnicodeError, OSError):
            safe = False
        if not safe:
            self.send_error(404)
            return None
        return super().send_head()


class ArtifactMirror:
    """Serve only regular files below an explicitly selected artifact directory."""
    def __init__(self, root):
        root = Path(root).resolve()
        if not (root / 'shorebird').is_dir():
            raise ValueError('Artifact mirror has no shorebird subtree')
        handler = functools.partial(_MirrorHandler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('Owned artifact mirror did not stop')

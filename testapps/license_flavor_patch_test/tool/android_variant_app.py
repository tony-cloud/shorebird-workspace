"""Opt-in V2 Android wiring; V1 fixture sources and drivers stay untouched."""
from pathlib import Path
import re
import shutil
import uuid

import android_native_app as v1

TEMPLATES = Path(__file__).with_name('android_variant')
ARCHES = {'arm': ('armeabi-v7a', 'android-arm'),
          'aarch64': ('arm64-v8a', 'android-arm64'),
          'x86_64': ('x86_64', 'android-x64')}


def inject_app(app, package, control_sha256, header_path, *, app_id,
               release_version, session, arch='aarch64'):
    """Generate only a fresh disposable scaffold, with the current public V2 ABI."""
    app, header = Path(app), Path(header_path)
    if (str(uuid.UUID(app_id)) != app_id or
            not re.fullmatch(r'[a-zA-Z0-9_.+\-]{1,128}', release_version) or
            not re.fullmatch(r'[a-f0-9]{32,64}', session) or arch not in ARCHES):
        raise ValueError('Invalid immutable Android variant identity')
    if 'shorebird_set_patch_decryption_key_provider_v2' not in header.read_text():
        raise ValueError('Native V2 public provider header is required')
    # Reuse only the proven fresh/non-debuggable manifest and Gradle generator.
    # Its output is this private app; no baseline repository source is changed.
    v1.inject_app(app, package, control_sha256, header)
    main = app / 'android/app/src/main'
    replacements = {'__JAVA_PACKAGE__': package, '__CONTROL_SHA256__': control_sha256,
                    '__APP_ID__': app_id, '__RELEASE_VERSION__': release_version,
                    '__SESSION__': session, '__WIRE_ARCH__': arch,
                    '__ANDROID_ABI__': ARCHES[arch][0]}
    for template in TEMPLATES.iterdir():
        if template.name.endswith('.java.template'):
            output = main / 'java' / package.replace('.', '/') / template.name.removesuffix('.template')
        elif template.name in ('fixture_jni.cpp', 'CMakeLists.txt'):
            output = main / 'cpp' / template.name
        else:
            continue
        text = template.read_text()
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        if re.search(r'__[A-Z_]+__', text):
            raise ValueError('Unexpanded V2 Android template')
        output.write_text(text)
    gradle = app / 'android/app/build.gradle.kts'
    source = gradle.read_text()
    if source.count('abiFilters += "arm64-v8a"') != 1:
        raise ValueError('Review changed disposable Gradle architecture wiring')
    gradle.write_text(source.replace('abiFilters += "arm64-v8a"',
                                     'abiFilters += "' + ARCHES[arch][0] + '"'))
    shutil.copy2(header, main / 'cpp/shorebird_patch_encryption.h')
    return {'arch': arch, 'android_abi': ARCHES[arch][0], 'v2_provider': True,
            'schema': 'native-variant-e2e-v2'}

#!/usr/bin/env python3
"""Compile templates and run platform-independent Java validator checks.

Uses only explicitly supplied, already installed dependencies. This does not
build an APK or execute Android/JNI/Keystore/Flutter runtime behavior.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--android-jar', type=Path, required=True)
    p.add_argument('--flutter-embedding', type=Path, required=True)
    p.add_argument('--dependency', type=Path, action='append', default=[])
    p.add_argument('--json-jar', type=Path, help='Optional pre-staged real org.json runtime for strict JSON tests')
    p.add_argument('--ndk', type=Path, required=True)
    p.add_argument('--header', type=Path, required=True)
    p.add_argument('--java-home', type=Path, required=True)
    a = p.parse_args()
    source = Path(__file__).resolve().parent
    dependencies = [a.android_jar, a.flutter_embedding, *a.dependency]
    for dependency in [*dependencies, a.header, *([a.json_jar] if a.json_jar else [])]:
        if not dependency.is_file():
            p.error('A required pre-staged input is missing')
    clangs = list((a.ndk / 'toolchains/llvm/prebuilt').glob('*/bin/aarch64-linux-android24-clang++'))
    if len(clangs) != 1:
        p.error('Expected exactly one installed NDK host toolchain')
    with tempfile.TemporaryDirectory(prefix='android-fixture-check-') as temp:
        work = Path(temp)
        for template in source.glob('*.java.template'):
            (work / template.name.removesuffix('.template')).write_text(
                template.read_text().replace('__JAVA_PACKAGE__', 'dev.shorebird.e2e.compilecheck')
                .replace('__CONTROL_SHA256__', 'a' * 64))
        (work / 'PureContractCheck.java').write_text((source / 'PureContractCheck.java').read_text())
        cp = ':'.join(map(str, dependencies))
        subprocess.run([str(a.java_home / 'bin/javac'), '--release', '17', '-cp', cp,
                        '-d', str(work / 'classes'), *map(str, work.glob('*.java'))], check=True)
        runtime_cp = str(work / 'classes') + ':' + (str(a.json_jar) + ':' if a.json_jar else '') + cp
        subprocess.run([str(a.java_home / 'bin/java'), '-cp', runtime_cp,
                        'dev.shorebird.e2e.compilecheck.PureContractCheck',
                        *(['strict-json'] if a.json_jar else [])], check=True)
        shutil.copyfile(a.header, work / 'shorebird_patch_encryption.h')
        (work / 'fixture_jni.cpp').write_text((source / 'fixture_jni.cpp').read_text()
            .replace('__JAVA_PACKAGE__', 'dev.shorebird.e2e.compilecheck'))
        subprocess.run([str(clangs[0]), '-std=c++17', '-shared', '-fPIC', '-Wall', '-Wextra',
                        '-Werror', '-fvisibility=hidden', str(work / 'fixture_jni.cpp'), '-ldl',
                        '-o', str(work / 'libshorebird_e2e_fixture.so')], check=True)
    print('PASS: Java 17 source/actual embedding compile, pure validators/HTTP checks, NDK arm64 shared-library compile')
    print('UNRUN: Android APK integration, JNI execution, Keystore, updater and device lifecycle')


if __name__ == '__main__':
    main()

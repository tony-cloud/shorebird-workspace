#!/usr/bin/env python3
"""Network-free smoke test of a built Dart SDK's pub, JIT, and default AOT tools.

Example: python3 scripts/verify_dart_sdk_tooling.py --dart /sdk/bin/dart
On Windows pass /sdk/bin/dart.exe; generated executables use the native suffix.
No dependencies, SDK modifications, engine builds, or HOME override are used.
"""

import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = r"""
import 'dart:io';

void check(bool condition, String label) {
  if (!condition) throw StateError(label);
}

class GetterProbe {
  const GetterProbe(this.value);
  final int value;
  int get length => value;
}

@pragma('vm:never-inline')
int readLength(Object value) => (value as dynamic).length as int;

// More than the polymorphic inline-cache threshold forces an SDK-interface
// dispatch table call. Its unboxed return ABI must match app implementations.
abstract class ComparableProbe implements Comparable<ComparableProbe> {
  ComparableProbe(this.value);
  final int value;
}
class ComparableProbe0 extends ComparableProbe {
  ComparableProbe0(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
class ComparableProbe1 extends ComparableProbe {
  ComparableProbe1(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
class ComparableProbe2 extends ComparableProbe {
  ComparableProbe2(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
class ComparableProbe3 extends ComparableProbe {
  ComparableProbe3(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
class ComparableProbe4 extends ComparableProbe {
  ComparableProbe4(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
class ComparableProbe5 extends ComparableProbe {
  ComparableProbe5(super.value);
  @override
  int compareTo(ComparableProbe other) => value + other.value;
}
@pragma('vm:never-inline')
int compareThroughSdkInterface(Comparable<ComparableProbe> left,
    ComparableProbe right) => left.compareTo(right);

void checkSdkInterfaceAbi() {
  final value = DateTime.now().microsecondsSinceEpoch % 1000 + 100;
  final implementations = <ComparableProbe>[
    ComparableProbe0(value), ComparableProbe1(value), ComparableProbe2(value),
    ComparableProbe3(value), ComparableProbe4(value), ComparableProbe5(value),
  ];
  for (final implementation in implementations) {
    check(compareThroughSdkInterface(implementation, ComparableProbe0(7)) ==
        value + 7, 'SDK interface native return ABI');
  }
}

int Function(int) makeClosure(int base) =>
    (int delta) => base + delta + readLength(<int>[1, 2]);

void main(List<String> args) {
  check(args.length == 1, 'fixture directory argument');
  checkSdkInterfaceAbi();
  check(readLength(<int>[1, 2, 3]) == 3, 'growable List getter dispatch');
  check(readLength(List<int>.filled(4, 7)) == 4, 'fixed List getter dispatch');
  check(readLength(List<int>.unmodifiable(<int>[8, 9])) == 2,
      'unmodifiable List getter dispatch');
  check(readLength(const GetterProbe(11)) == 11, 'ordinary getter dispatch');
  check(makeClosure(20)(20) == 42, 'closure dispatch and capture');

  final root = Directory(args.single)..createSync(recursive: true);
  final directory = Directory('${root.path}${Platform.pathSeparator}with space')
    ..createSync();
  final uri = Uri.directory(directory.path, windows: Platform.isWindows);
  check(uri.isAbsolute, 'Uri.directory is absolute');
  check(uri.toString().contains('with%20space'), 'Uri.directory encodes spaces');
  check(Directory.fromUri(uri).existsSync(), 'Directory.fromUri');

  final original = Directory.current;
  try {
    Directory.current = directory;
    final file = File.fromUri(uri.resolve('sample.txt'));
    file.writeAsStringSync('sdk-tooling-fixture');
    check(File('sample.txt').readAsStringSync() == 'sdk-tooling-fixture',
        'working directory and File read/write');
    file.renameSync('renamed.txt');
    File('renamed.txt').copySync('copied.txt');
    check(Directory.current.listSync().length == 2, 'Directory list getter');
    File('renamed.txt').deleteSync();
    File('copied.txt').deleteSync();
    check(Directory.current.listSync().isEmpty, 'File delete and List.isEmpty');
  } finally {
    Directory.current = original;
  }
  directory.deleteSync();
  print('PASS: SDK URI/list-getter/closure/filesystem fixture');
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dart', required=True, type=Path,
                        help='Built SDK bin/dart or bin/dart.exe')
    parser.add_argument('--work-dir', type=Path,
                        help='Preserve logs and artifacts in a new/empty directory')
    parser.add_argument('--timeout', type=int, default=180,
                        help='Maximum seconds per tool stage (default: 180)')
    args = parser.parse_args()
    dart = args.dart.expanduser().resolve()
    if not dart.is_file():
        parser.error(f'Dart executable does not exist: {dart}')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.work_dir:
        work_path = args.work_dir.expanduser().resolve()
        work_path.mkdir(parents=True, exist_ok=True)
        if any(work_path.iterdir()):
            parser.error('--work-dir must be new or empty')
        workspace = contextlib.nullcontext(work_path)
    else:
        workspace = tempfile.TemporaryDirectory(prefix='dart-sdk-tooling-')

    with workspace as temporary:
        work = Path(temporary)
        package = work / 'package with spaces'
        (package / 'bin').mkdir(parents=True)
        (package / 'pubspec.yaml').write_text(
            "name: dart_sdk_tooling_fixture\n"
            "publish_to: none\n"
            "environment:\n  sdk: '>=3.9.0 <4.0.0'\n",
            encoding='utf-8')
        source = package / 'bin/main.dart'
        source.write_text(SOURCE, encoding='utf-8')
        env = dict(os.environ, PUB_CACHE=str(work / 'pub-cache'),
                   DART_SUPPRESS_ANALYTICS='true', CI='true', BOT='true')

        def stage(name, command, *, require_fixture=False):
            try:
                result = subprocess.run([str(value) for value in command],
                                        cwd=package, env=env, capture_output=True,
                                        text=True, timeout=args.timeout)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(f'{name}: {error}') from None
            output = result.stdout + result.stderr
            (work / f'{name}.log').write_text(output, encoding='utf-8')
            if result.returncode:
                detail = '\n'.join(output.splitlines()[-30:])
                raise RuntimeError(f'{name}: exit {result.returncode}\n{detail}')
            if require_fixture and 'PASS: SDK URI/list-getter/closure/filesystem fixture' not in result.stdout:
                raise RuntimeError(f'{name}: tool exited without executing the fixture')
            print(f'PASS: {name}', flush=True)

        stage('version', [dart, '--version'])
        stage('offline-pub-get', [dart, 'pub', 'get', '--offline'])
        stage('run-script', [dart, 'run', source, work / 'script-files'],
              require_fixture=True)
        executable = work / ('tooling-fixture.exe' if os.name == 'nt' else 'tooling-fixture')
        # Deliberately use normal/default TFA. Disabling it would hide SDK
        # selector/getter regressions in pub/dartdev and ordinary applications.
        stage('compile-exe-default-tfa', [dart, 'compile', 'exe', source,
                                          '-o', executable])
        if not executable.is_file():
            raise RuntimeError('compile-exe-default-tfa: executable was not produced')
        stage('run-compiled-exe', [executable, work / 'aot-files'],
              require_fixture=True)
        print('PASS: built Dart SDK tooling works offline with default AOT/TFA')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        print(f'FAIL: Dart SDK tooling regression: {error}', file=sys.stderr)
        sys.exit(1)

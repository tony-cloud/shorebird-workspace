// dart test/dd_select_packages_test.dart  (no package dependencies)
import 'dart:io';

import '../lib/src/dd_select.dart';

void main() {
  final pk = {'my_app', 'my_pkg'};
  final cases = <String, bool>{
    'fn:package:my_app/main.dart::main#0/0s': true,
    'fn:package:my_pkg/src/a.dart::A::f#0/1': true,
    'fn:package:my_app_extra/x.dart::f#0/0': false, // prefix of a package name
    'fn:package:flutter/src/widgets/basic.dart::f#0/0': false,
    'fn:dart:core::_Uri::_makePath#0/6s': false,
    'closure:package:my_app/main.dart': false,
    'fn:package:my_app': false,
  };
  var failed = 0;
  cases.forEach((key, want) {
    if (keyInPackages(key, pk) != want) {
      stderr.writeln('FAIL $key expected $want');
      failed++;
    }
  });
  if (failed != 0) exit(1);
  stdout.writeln('dd_select_packages_test: ${cases.length} cases ok');
}

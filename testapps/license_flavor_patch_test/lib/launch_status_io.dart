import 'dart:io';

void writeLaunchStatus({
  required String license,
  required bool proFeatureEnabled,
}) {
  try {
    final contents =
        'license:$license\n'
        'pro-feature:${proFeatureEnabled ? 'enabled' : 'off'}\n';
    File(
      '${Directory.systemTemp.path}/license_flavor_patch_status.txt',
    ).writeAsStringSync(contents, flush: true);

    final home = Platform.environment['HOME'];
    if (home == null || home.isEmpty) {
      return;
    }
    final directory = Directory('$home/Library/Application Support');
    directory.createSync(recursive: true);
    File(
      '${directory.path}/license_flavor_patch_status.txt',
    ).writeAsStringSync(contents, flush: true);
  } on Object {
    // Best-effort test signal; the app UI should not depend on filesystem I/O.
  }
}

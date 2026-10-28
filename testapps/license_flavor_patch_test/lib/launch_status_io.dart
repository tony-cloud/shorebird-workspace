import 'dart:async';
import 'dart:io';

void writeLaunchStatus({
  required String license,
  required bool proFeatureEnabled,
}) {
  try {
    final contents =
        'license:$license\n'
        'pro-feature:${proFeatureEnabled ? 'enabled' : 'off'}\n';
    const reportUrl = String.fromEnvironment('PATCH_TEST_REPORT_URL');
    if (reportUrl.isNotEmpty) {
      unawaited(_reportStatus(Uri.parse(reportUrl), contents));
    }
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

// Optional device-test signal for distribution-signed apps whose containers
// cannot be read by developer tools. Never sends key material or user data.
Future<void> _reportStatus(Uri url, String contents) async {
  final client = HttpClient()..connectionTimeout = const Duration(seconds: 5);
  try {
    final request = await client.postUrl(url);
    request.headers.contentType = ContentType.text;
    request.write(contents);
    final response = await request.close().timeout(const Duration(seconds: 5));
    await response.drain<void>();
  } on Object {
    // The test observer must not affect app startup or patch behavior.
  } finally {
    client.close(force: true);
  }
}

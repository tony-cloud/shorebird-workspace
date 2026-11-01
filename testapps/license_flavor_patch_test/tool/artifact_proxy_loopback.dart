// Fixture entrypoint for the unchanged production artifact routing handler.
// Compile with the matching CLI workspace package configuration.
import 'dart:io';

import 'package:artifact_proxy/artifact_proxy.dart';
import 'package:shelf/shelf_io.dart' as shelf_io;

Future<void> main() async {
  final port = int.parse(Platform.environment['PORT']!);
  final mirror = Uri.parse(
    Platform.environment['SHOREBIRD_ARTIFACT_BASE_URL']!,
  );
  if (port < 1 ||
      port > 65535 ||
      mirror.scheme != 'http' ||
      mirror.host != '127.0.0.1' ||
      mirror.userInfo.isNotEmpty ||
      mirror.hasQuery ||
      mirror.hasFragment) {
    throw ArgumentError('Invalid fixture artifact proxy configuration');
  }
  final handler = artifactProxyHandler(
    client: ArtifactManifestClient(manifestBaseUri: mirror),
    shorebirdArtifactBaseUri: mirror,
    flutterArtifactBaseUri: Uri.parse('https://storage.googleapis.com'),
  );
  final server = await shelf_io.serve(
    handler,
    InternetAddress.loopbackIPv4,
    port,
  );
  server.autoCompress = true;
}

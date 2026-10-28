// Real Dart client / Go server compatibility test. Run through
// run_server_client_smoke.sh; all credentials and storage are disposable.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:open_aot_patch_tools/open_aot_patch_tools.dart' as open_patch;
import 'package:shorebird_cli/src/code_signer.dart';
import 'package:shorebird_code_push_client/shorebird_code_push_client.dart';

Future<void> main(List<String> args) async {
  if (args.length != 1) {
    stderr.writeln('Usage: smoke_server_client.dart <Go server binary>');
    exitCode = 64;
    return;
  }
  final smoke = _Smoke(File(args.single).absolute.path);
  try {
    await smoke.run();
    stdout.writeln(
      'PASS: real Dart client / Go server lifecycle and signatures',
    );
  } catch (error, stack) {
    // Never dump HTTP bodies, process logs, tokens, keys, or environment values.
    final detail = error is _Failure ? error.message : '${error.runtimeType}';
    stderr.writeln('FAIL at ${smoke.stage}: $detail\n$stack');
    exitCode = 1;
  } finally {
    await smoke.close();
  }
}

class _Failure implements Exception {
  _Failure(this.message);
  final String message;
}

void _require(bool condition, String message) {
  if (!condition) throw _Failure(message);
}

String _secret(int length) => base64UrlEncode(
  List<int>.generate(length, (_) => Random.secure().nextInt(256)),
);

class _Smoke {
  _Smoke(this.serverBinary);
  final String serverBinary;
  final http.Client transport = http.Client();
  final CodeSigner verifier = CodeSigner();
  final Directory work = Directory.systemTemp.createTempSync(
    'shorebird-server-client-data.',
  );
  final String initialPassword = _secret(32);
  final String password = _secret(32);
  final String signingMasterKey = base64Encode(
    List<int>.generate(32, (_) => Random.secure().nextInt(256)),
  );
  final String jwtSecret = _secret(48);
  String stage = 'startup';
  String accessToken = '';
  late Uri origin;
  Process? server;
  Future<int>? serverExit;

  CodePushClient get client => CodePushClient(
    httpClient: transport,
    hostedUri: origin,
    customHeaders: {'authorization': 'Bearer $accessToken'},
  );

  Future<void> start() async {
    // A free ephemeral port avoids disturbing developer servers. If another
    // process claims it after close, startup fails rather than killing it.
    if (serverExit == null) {
      final socket = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
      origin = Uri.parse('http://127.0.0.1:${socket.port}');
      await socket.close();
    }
    server = await Process.start(
      serverBinary,
      const [],
      environment: {
        'SERVER_PORT': '${origin.port}',
        'SERVER_BASE_URL': '$origin',
        'DB_DRIVER': 'sqlite',
        'DB_PATH': '${work.path}/state.sqlite',
        'STORAGE_DRIVER': 'local',
        'STORAGE_LOCAL_DIR': '${work.path}/objects',
        'JWT_SECRET': jwtSecret,
        'PATCH_SIGNING_MASTER_KEY': signingMasterKey,
        'PATCH_DELIVERY_ENCRYPTION': 'off',
        'SMTP_HOST': '',
        'DEFAULT_ADMIN_EMAIL': 'smoke@example.invalid',
        'DEFAULT_ADMIN_PASSWORD': initialPassword,
        'DEFAULT_ADMIN_NAME': 'Disposable smoke administrator',
      },
    );
    unawaited(server!.stdout.drain<void>());
    unawaited(server!.stderr.drain<void>());
    int? exited;
    serverExit = server!.exitCode;
    unawaited(serverExit!.then((code) => exited = code));
    for (var attempt = 0; attempt < 200; attempt++) {
      if (exited != null) throw _Failure('Go server exited during startup');
      try {
        final health = await transport
            .get(origin.resolve('/health'))
            .timeout(const Duration(seconds: 1));
        if (health.statusCode == 200) return;
      } on SocketException {
        // The process has not started listening yet.
      } on http.ClientException {
        // The process has not started listening yet.
      } on TimeoutException {
        // Retry until the bounded startup deadline.
      }
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
    throw _Failure('Go server did not become ready');
  }

  Future<void> stop() async {
    final process = server;
    if (process == null) return;
    process.kill(ProcessSignal.sigterm);
    try {
      await serverExit!.timeout(const Duration(seconds: 5));
    } on TimeoutException {
      process.kill(ProcessSignal.sigkill);
      await serverExit;
    }
    server = null;
  }

  Future<void> close() async {
    await stop();
    transport.close();
    work.deleteSync(recursive: true);
  }

  Future<Map<String, dynamic>> jsonRequest(
    String method,
    String path, {
    Map<String, dynamic>? body,
    int? expectedStatus,
  }) async {
    final request = http.Request(method, origin.resolve(path));
    if (accessToken.isNotEmpty) {
      request.headers['authorization'] = 'Bearer $accessToken';
    }
    if (body != null) {
      request.headers['content-type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final response = await http.Response.fromStream(
      await transport.send(request),
    );
    _require(
      expectedStatus == null
          ? response.statusCode >= 200 && response.statusCode < 300
          : response.statusCode == expectedStatus,
      '$method $path returned HTTP ${response.statusCode}',
    );
    if (response.body.isEmpty) return {};
    return (jsonDecode(response.body) as Map).cast<String, dynamic>();
  }

  Future<void> run() async {
    await start();
    stage = 'bootstrap login and password change';
    var login = await jsonRequest(
      'POST',
      '/auth/token',
      body: {'email': 'smoke@example.invalid', 'password': initialPassword},
    );
    accessToken = login['token'] as String;
    _require(login['must_change_password'] == true, 'bootstrap flag missing');
    await jsonRequest(
      'PATCH',
      '/api/v1/users/me/password',
      body: {'current_password': initialPassword, 'new_password': password},
    );
    login = await jsonRequest(
      'POST',
      '/auth/token',
      body: {'email': 'smoke@example.invalid', 'password': password},
    );
    accessToken = login['token'] as String;
    _require(login['must_change_password'] != true, 'password flag retained');
    _require(await client.getCurrentUser() != null, 'current user DTO failed');

    stage = 'OAuth refresh and one-time refresh-token consumption';
    final refreshToken = login['refresh_token'] as String;
    final refreshed = await transport.post(
      origin.resolve('/auth/token'),
      body: {'grant_type': 'refresh_token', 'refresh_token': refreshToken},
    );
    _require(refreshed.statusCode == 200, 'OAuth refresh failed');
    final tokens = jsonDecode(refreshed.body) as Map;
    accessToken = tokens['access_token'] as String;
    _require(
      tokens['refresh_token'] is String &&
          tokens['refresh_token'] != refreshToken,
      'refresh credential was not rotated',
    );
    final reused = await transport.post(
      origin.resolve('/auth/token'),
      body: {'grant_type': 'refresh_token', 'refresh_token': refreshToken},
    );
    _require(reused.statusCode == 400, 'consumed refresh token was accepted');
    _require(await client.getCurrentUser() != null, 'refreshed token unusable');
    stdout.writeln('PASS: bootstrap password change and OAuth refresh');

    stage = 'real self-hosted doctor network diagnostics';
    final uploadDiagnostic = await client.getGCPUploadSpeedTestUrl();
    final downloadDiagnostic = await client.getGCPDownloadSpeedTestUrl();
    _require(
      uploadDiagnostic.origin == origin.origin &&
          downloadDiagnostic.origin == origin.origin,
      'diagnostics must use the self-hosted origin',
    );
    final diagnosticUpload = http.MultipartRequest('POST', uploadDiagnostic)
      ..files.add(
        http.MultipartFile.fromBytes(
          'file',
          List<int>.filled(5000000, 0),
          filename: 'speed_test_file',
        ),
      );
    final diagnosticUploaded = await transport
        .send(diagnosticUpload)
        .timeout(const Duration(seconds: 30));
    await diagnosticUploaded.stream.drain<void>();
    _require(
      diagnosticUploaded.statusCode == HttpStatus.noContent,
      'doctor multipart upload must return 204',
    );
    final diagnosticDownload = await transport
        .get(downloadDiagnostic)
        .timeout(const Duration(seconds: 30));
    _require(
      diagnosticDownload.statusCode == HttpStatus.ok &&
          diagnosticDownload.bodyBytes.length == 16000000,
      'doctor download must contain exactly 16000000 bytes',
    );
    final unsignedDiagnostic = await transport.get(
      downloadDiagnostic.replace(query: ''),
    );
    _require(
      unsignedDiagnostic.statusCode == HttpStatus.forbidden,
      'diagnostic download requires a transfer grant',
    );
    stdout.writeln('PASS: real Dart client doctor upload and download');

    stage = 'organization and app DTOs';
    final organization = await client.createOrganization(name: 'Smoke test');
    final app = await client.createApp(
      organizationId: organization.id,
      displayName: 'Disposable smoke app',
    );
    final metadata = await client.getApp(appId: app.id);
    _require(metadata?.appId == app.id, 'getApp DTO lost app id');
    _require(metadata?.displayName == app.displayName, 'getApp name mismatch');
    _require(
      await client.getApp(appId: '00000000-0000-4000-8000-000000000000') ==
          null,
      'missing app must return null',
    );

    stage = 'managed key and release artifact multipart upload';
    final key = await client.getSigningKey(appId: app.id);
    final repeatedKey = await client.getSigningKey(appId: app.id);
    _require(key == repeatedKey, 'managed key changed between requests');
    const version = '1.0.0+1';
    final release = await client.createRelease(
      appId: app.id,
      version: version,
      flutterRevision: '0123456789012345678901234567890123456789',
      flutterVersion: '3.38.0',
      displayName: 'Smoke release',
    );
    final baseline = File('${work.path}/baseline.bin')
      ..writeAsBytesSync(utf8.encode('disposable native base artifact'));
    final baselineHash = sha256.convert(baseline.readAsBytesSync()).toString();
    await client.createReleaseArtifact(
      artifactPath: baseline.path,
      appId: app.id,
      releaseId: release.id,
      arch: 'xcarchive',
      platform: ReleasePlatform.ios,
      hash: baselineHash,
      canSideload: false,
      podfileLockHash: null,
      signing: ArtifactSigningRequest(
        mode: ReleaseSigningMode.managed,
        keyId: key.id,
      ),
    );
    await client.updateReleaseStatus(
      appId: app.id,
      releaseId: release.id,
      platform: ReleasePlatform.ios,
      status: ReleaseStatus.active,
    );
    Future<void> checkPersistedRelease() async {
      final artifacts = await client.getReleaseArtifacts(
        appId: app.id,
        releaseId: release.id,
        arch: 'xcarchive',
        platform: ReleasePlatform.ios,
      );
      _require(artifacts.length == 1, 'release artifact listing mismatch');
      final artifact = artifacts.single;
      _require(artifact.hash == baselineHash, 'release hash was not persisted');
      _require(
        artifact.signing?.mode == 'managed' &&
            artifact.signing?.keyId == key.id &&
            artifact.signing?.publicKeyPem == key.publicKeyPem,
        'managed signing policy was not persisted',
      );
      final downloaded = await transport.get(Uri.parse(artifact.url));
      _require(downloaded.statusCode == 200, 'release download failed');
      _require(
        sha256.convert(downloaded.bodyBytes).toString() == baselineHash,
        'release upload/download changed bytes',
      );
    }

    await checkPersistedRelease();
    stage = 'managed key and release persistence after process restart';
    await stop();
    await start();
    _require(
      await client.getSigningKey(appId: app.id) == key,
      'managed key did not survive restart',
    );
    await checkPersistedRelease();
    stdout.writeln('PASS: app/release DTOs, uploads, signing policy, restart');

    stage = 'patch upload and promotion';
    final patch = await client.createPatch(
      appId: app.id,
      releaseId: release.id,
      metadata: const {'smoke': true},
    );
    // The server treats the uploaded transport as opaque. Make its bytes
    // different from the reconstructed AES envelope, to catch servers signing
    // the transport digest rather than the final artifact hash supplied by CLI.
    final bytecode = utf8.encode('test-only interpreter bytecode');
    final envelope = open_patch.encryptArtifact(
      artifact: open_patch.linkArtifacts(
        baseSnapshot: const [],
        patchSnapshot: bytecode,
        metadata: open_patch.PatchMetadata(
          appId: app.id,
          appBuildId: version,
          flavorId: 'pro',
          licenseType: 'pro',
          sdkHash: 'test-sdk',
          baseSnapshotHash: baselineHash,
          patchSnapshotHash: open_patch.sha256Hex(bytecode),
          targetOs: 'ios',
          targetArch: 'arm64',
          runtimeMode: open_patch.runtimeModeDartBytecodeInterpreter,
        ),
        forceFullSnapshot: true,
      ),
      keyId: 'disposable-test-key',
      key: List<int>.generate(32, (_) => Random.secure().nextInt(256)),
      nonce: List<int>.generate(12, (_) => Random.secure().nextInt(256)),
    );
    final envelopeHash = sha256
        .convert(utf8.encode(jsonEncode(envelope.toJson())))
        .toString();
    final patchFile = File('${work.path}/transport.patch')
      ..writeAsBytesSync(utf8.encode('opaque test-only transport bytes'));
    final transportHash = sha256
        .convert(patchFile.readAsBytesSync())
        .toString();
    _require(envelopeHash != transportHash, 'test hashes unexpectedly equal');
    await client.createPatchArtifact(
      artifactPath: patchFile.path,
      appId: app.id,
      patchId: patch.id,
      arch: 'aarch64',
      platform: ReleasePlatform.ios,
      hash: envelopeHash,
    );
    final channels = await client.getChannels(appId: app.id);
    final stable = channels.singleWhere((channel) => channel.name == 'stable');
    await client.promotePatch(
      appId: app.id,
      patchId: patch.id,
      channelId: stable.id,
    );
    final patches = await client.getPatches(
      appId: app.id,
      releaseId: release.id,
    );
    _require(patches.length == 1, 'patch listing mismatch');
    _require(patches.single.number == patch.number, 'patch number mismatch');
    _require(
      patches.single.artifacts.single.hash == envelopeHash,
      'patch artifact hash not persisted',
    );
    _require(
      patches.single.artifacts.single.size == patchFile.lengthSync(),
      'patch artifact transport size mismatch',
    );

    Future<PatchCheckResponse> check({
      required bool available,
      required bool rolledBack,
      int? patchNumber,
    }) async {
      final requestBody = PatchCheckRequest(
        releaseVersion: version,
        platform: ReleasePlatform.ios,
        arch: 'aarch64',
        appId: app.id,
        channel: 'stable',
        patchNumber: patchNumber,
        currentPatchNumber: patchNumber,
      ).toJson()..['nonce'] = _uuidV4();
      final request =
          http.Request('POST', origin.resolve('/api/v1/patches/check'))
            ..headers['content-type'] = 'application/json'
            ..body = jsonEncode(requestBody);
      final response = await http.Response.fromStream(
        await client.send(request),
      );
      _require(response.statusCode == 200, 'device check failed');
      final json = (jsonDecode(response.body) as Map).cast<String, dynamic>();
      final typed = PatchCheckResponse.fromJson(json);
      _require(
        typed.patchAvailable == available,
        'patch availability mismatch',
      );
      _require(
        typed.rolledBackPatchNumbers!.contains(patch.number) == rolledBack,
        'rollback state mismatch',
      );
      final authentication = json['authentication'] as Map;
      final payloadText = utf8.decode(
        base64Decode(authentication['payload'] as String),
      );
      _require(
        verifier.verify(
          message: payloadText,
          signature: authentication['signature'] as String,
          publicKeyPem: key.publicKeyPem,
        ),
        'patch control response RSA signature invalid',
      );
      final payload = jsonDecode(payloadText) as Map;
      _require(payload['version'] == 1, 'signed response version mismatch');
      for (final field in [
        'app_id',
        'release_version',
        'platform',
        'arch',
        'channel',
        'nonce',
      ]) {
        _require(
          payload[field] == requestBody[field],
          'signed context mismatch: $field',
        );
      }
      for (final field in [
        'patch_available',
        'patch',
        'rolled_back_patch_numbers',
      ]) {
        _require(
          _sameJson(payload[field], json[field]),
          'signed response does not bind $field',
        );
      }
      if (available) {
        final offer = typed.patch!;
        _require(offer.number == patch.number, 'offered patch number mismatch');
        _require(offer.hash == envelopeHash, 'offered final hash mismatch');
        _require(
          verifier.verify(
            message: envelopeHash,
            signature: offer.hashSignature!,
            publicKeyPem: key.publicKeyPem,
          ),
          'managed RSA signature does not cover final UTF-8 hash string',
        );
        _require(
          !verifier.verify(
            message: transportHash,
            signature: offer.hashSignature!,
            publicKeyPem: key.publicKeyPem,
          ),
          'managed RSA signature covers uploaded transport instead of final hash',
        );
        final downloaded = await transport.get(Uri.parse(offer.downloadUrl));
        _require(downloaded.statusCode == 200, 'patch download failed');
        _require(
          sha256.convert(downloaded.bodyBytes).toString() == transportHash,
          'patch upload/download changed transport bytes',
        );
      }
      return typed;
    }

    stage = 'patch checks and RSA artifact/control signatures';
    await check(available: true, rolledBack: false);
    await check(available: false, rolledBack: false, patchNumber: patch.number);
    stage = 'rollback, signed removal, and idempotency';
    _require(
      await client.rollbackPatch(
        appId: app.id,
        releaseId: release.id,
        patchId: patch.id,
      ),
      'first rollback did not change state',
    );
    _require(
      !await client.rollbackPatch(
        appId: app.id,
        releaseId: release.id,
        patchId: patch.id,
      ),
      'repeated rollback was not idempotent',
    );
    await check(available: false, rolledBack: true, patchNumber: patch.number);
    stage = 'rollforward and idempotency';
    _require(
      await client.rollforwardPatch(
        appId: app.id,
        releaseId: release.id,
        patchId: patch.id,
      ),
      'first rollforward did not change state',
    );
    _require(
      !await client.rollforwardPatch(
        appId: app.id,
        releaseId: release.id,
        patchId: patch.id,
      ),
      'repeated rollforward was not idempotent',
    );
    await check(available: true, rolledBack: false);
    stage = 'patch persistence after restart';
    await stop();
    await start();
    await check(available: true, rolledBack: false);
    stdout.writeln(
      'PASS: patch DTOs/uploads, RSA signatures, rollback/rollforward',
    );
  }
}

String _uuidV4() {
  final bytes = List<int>.generate(16, (_) => Random.secure().nextInt(256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  final hex = bytes
      .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
      .join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

// Go structs serialize fields in declaration order; object order is not part
// of JSON semantics. Compare all signed fields without depending on that order.
bool _sameJson(Object? left, Object? right) {
  Object? canonical(Object? value) {
    if (value is Map) {
      final keys = value.keys.cast<String>().toList()..sort();
      return {for (final key in keys) key: canonical(value[key])};
    }
    if (value is List) return value.map(canonical).toList();
    return value;
  }

  return jsonEncode(canonical(left)) == jsonEncode(canonical(right));
}

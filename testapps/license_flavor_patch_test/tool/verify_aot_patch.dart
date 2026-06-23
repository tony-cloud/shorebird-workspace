import 'dart:convert';
import 'dart:io';

Future<void> main() async {
  final appDir = File.fromUri(Platform.script).parent.parent;
  final workspaceRoot = appDir.parent.parent;
  final dartRoot = Directory('${workspaceRoot.path}/dart-sdk-new');
  final patchBuildDir = Directory('${dartRoot.path}/out/ReleaseX64AotPatch');
  final toolWorkspace = Directory('${workspaceRoot.path}/shorebird');
  final workDir = Directory('${appDir.path}/build/open_aot_patch_verify');

  final argsFile = File('${patchBuildDir.path}/args.gn');
  if (!argsFile.existsSync()) {
    throw StateError(
      'Missing ${argsFile.path}. Build the patch runtime first with '
      'dart_enable_aot_patching=true and dart_dynamic_modules=false.',
    );
  }
  final args = argsFile.readAsStringSync();
  if (!args.contains('dart_enable_aot_patching = true') ||
      !args.contains('dart_dynamic_modules = false')) {
    throw StateError(
      '${argsFile.path} must enable AOT patching and disable dynamic modules.',
    );
  }

  if (workDir.existsSync()) {
    workDir.deleteSync(recursive: true);
  }
  workDir.createSync(recursive: true);

  final packageConfig = File('${workDir.path}/package_config.json');
  packageConfig.writeAsStringSync(
    jsonEncode({
      'configVersion': 2,
      'packages': [
        {
          'name': 'license_flavor_patch_test',
          'rootUri': appDir.uri.toString(),
          'packageUri': 'lib/',
          'languageVersion': '3.9',
        },
      ],
    }),
  );

  final baseMap = File('${workDir.path}/base.obfuscation.json');
  final patchMap = File('${workDir.path}/patch.obfuscation.json');
  final baseVmcode = File('${workDir.path}/base_free.vmcode');
  final patchVmcode = File('${workDir.path}/patch_pro.vmcode');
  final reconstructedVmcode = File('${workDir.path}/reconstructed_pro.vmcode');

  await _compileAot(
    workspaceRoot: workspaceRoot,
    dartRoot: dartRoot,
    patchBuildDir: patchBuildDir,
    packageConfig: packageConfig,
    licenseType: 'free',
    outputVmcode: baseVmcode,
    saveObfuscationMap: baseMap,
  );
  await _compileAot(
    workspaceRoot: workspaceRoot,
    dartRoot: dartRoot,
    patchBuildDir: patchBuildDir,
    packageConfig: packageConfig,
    licenseType: 'pro',
    outputVmcode: patchVmcode,
    loadObfuscationMap: baseMap,
    saveObfuscationMap: patchMap,
  );

  _expectStatus(
    await _runAot(patchBuildDir, baseVmcode),
    license: 'free',
    proFeature: false,
  );
  _expectStatus(
    await _runAot(patchBuildDir, patchVmcode),
    license: 'pro',
    proFeature: true,
  );

  final artifact = File('${workDir.path}/patch.json');
  final encrypted = File('${workDir.path}/patch.encrypted.json');
  const keyHex =
      '000102030405060708090a0b0c0d0e0f'
      '101112131415161718191a1b1c1d1e1f';
  const wrongKeyHex =
      '1f1e1d1c1b1a19181716151413121110'
      '0f0e0d0c0b0a09080706050403020100';
  const nonceHex = '000102030405060708090a0b';
  const offlineExpiresAt = '2030-01-01T00:00:00Z';
  const beforeExpiry = '2029-01-01T00:00:00Z';
  const afterExpiry = '2031-01-01T00:00:00Z';

  await _runTool(toolWorkspace, [
    'link',
    '--base=${baseVmcode.path}',
    '--patch=${patchVmcode.path}',
    '--output=${artifact.path}',
    '--app-id=license-flavor-patch-test',
    '--app-build-id=host-aot-smoke',
    '--base-flavor-id=free',
    '--base-license-type=free',
    '--flavor-id=pro',
    '--license-type=pro',
    '--sdk-hash=${_sha256File(File('${patchBuildDir.path}/gen_snapshot.exe'))}',
    '--target-os=windows',
    '--target-arch=x64',
    '--obfuscation-map-hash=${_sha256File(baseMap)}',
    '--offline-expires-at=$offlineExpiresAt',
    '--full-snapshot=true',
  ]);

  final artifactJson = (jsonDecode(artifact.readAsStringSync()) as Map)
      .cast<String, Object?>();
  if (artifactJson['payload_kind'] != 'full-snapshot') {
    throw StateError('Expected a directly loadable full-snapshot payload.');
  }
  final metadata = (artifactJson['metadata'] as Map).cast<String, Object?>();
  if (metadata['offline_expires_at'] != '2030-01-01T00:00:00.000Z') {
    throw StateError('Expected normalized offline_expires_at metadata.');
  }

  await _runTool(toolWorkspace, [
    'encrypt',
    '--input=${artifact.path}',
    '--output=${encrypted.path}',
    '--key-id=test-key',
    '--key-hex=$keyHex',
    '--nonce-hex=$nonceHex',
  ]);

  await _runTool(toolWorkspace, [
    'verify',
    '--input=${encrypted.path}',
    '--key-hex=$keyHex',
    '--artifact-sha256=${_sha256File(encrypted)}',
    '--base-flavor-id=free',
    '--base-license-type=free',
    '--flavor-id=pro',
    '--license-type=pro',
    '--base=${baseVmcode.path}',
    '--now=$beforeExpiry',
  ]);
  await _runToolExpectFailure(toolWorkspace, [
    'verify',
    '--input=${encrypted.path}',
    '--key-hex=$keyHex',
    '--base-flavor-id=free',
    '--base-license-type=free',
    '--flavor-id=pro',
    '--license-type=pro',
    '--base=${baseVmcode.path}',
    '--now=$afterExpiry',
  ]);
  await _runToolExpectFailure(toolWorkspace, [
    'verify',
    '--input=${encrypted.path}',
    '--key-hex=$wrongKeyHex',
    '--base-flavor-id=free',
    '--base-license-type=free',
    '--flavor-id=pro',
    '--license-type=pro',
    '--base=${baseVmcode.path}',
  ]);
  await _runToolExpectFailure(toolWorkspace, [
    'verify',
    '--input=${encrypted.path}',
    '--key-hex=$keyHex',
    '--base-flavor-id=free',
    '--base-license-type=free',
    '--flavor-id=enterprise',
    '--license-type=pro',
    '--base=${baseVmcode.path}',
  ]);

  await _runTool(toolWorkspace, [
    'dump-blobs',
    '--input=${encrypted.path}',
    '--key-hex=$keyHex',
    '--base=${baseVmcode.path}',
    '--output=${reconstructedVmcode.path}',
    '--now=$beforeExpiry',
  ]);
  if (_sha256File(reconstructedVmcode) != _sha256File(patchVmcode)) {
    throw StateError('Reconstructed patch vmcode does not match pro snapshot.');
  }

  _expectStatus(
    await _runAot(patchBuildDir, reconstructedVmcode),
    license: 'pro',
    proFeature: true,
  );

  await _runToolExpectFailure(toolWorkspace, [
    'dump-blobs',
    '--input=${encrypted.path}',
    '--key-hex=$keyHex',
    '--base=${baseVmcode.path}',
    '--output=${reconstructedVmcode.path}',
    '--now=$afterExpiry',
  ]);

  stdout.writeln(
    'AOT patch applied to license_flavor_patch_test successfully.',
  );
  stdout.writeln('base: license:free / pro-feature:off');
  stdout.writeln('patch: license:pro / pro-feature:enabled');
  stdout.writeln('artifact: ${encrypted.path}');
  stdout.writeln('vmcode: ${reconstructedVmcode.path}');
}

Future<void> _compileAot({
  required Directory workspaceRoot,
  required Directory dartRoot,
  required Directory patchBuildDir,
  required File packageConfig,
  required String licenseType,
  required File outputVmcode,
  required File saveObfuscationMap,
  File? loadObfuscationMap,
}) async {
  final dart = '${dartRoot.path}/tools/sdks/dart-sdk/bin/dart.exe';
  final genKernel = '${dartRoot.path}/pkg/vm/bin/gen_kernel.dart';
  final source =
      '${workspaceRoot.path}/testapps/license_flavor_patch_test/bin/license_status.dart';
  final dill = File('${outputVmcode.parent.path}/${licenseType}_app.dill');
  await _run(dart, [
    genKernel,
    '--packages',
    packageConfig.path,
    '--platform',
    '${patchBuildDir.path}/vm_platform_product.dill',
    '--aot',
    '--target-os',
    'windows',
    '-Ddart.vm.product=true',
    '-DLICENSE_TYPE=$licenseType',
    '-o',
    dill.path,
    '--invocation-modes=compile',
    '--verbosity=all',
    source,
  ]);

  final genSnapshot = '${patchBuildDir.path}/gen_snapshot.exe';
  await _run(genSnapshot, [
    '--snapshot-kind=app-aot-elf',
    '--elf=${outputVmcode.path}',
    '--strip',
    '--obfuscate',
    if (loadObfuscationMap != null)
      '--load-obfuscation-map=${loadObfuscationMap.path}',
    '--save-obfuscation-map=${saveObfuscationMap.path}',
    dill.path,
  ]);
}

Future<String> _runAot(Directory patchBuildDir, File vmcode) async {
  final result = await _run('${patchBuildDir.path}/dartaotruntime.exe', [
    vmcode.path,
  ]);
  return result.stdout as String;
}

Future<ProcessResult> _runTool(
  Directory toolWorkspace,
  List<String> args,
) async {
  return _run(
    '${toolWorkspace.parent.path}/dart-sdk-new/tools/sdks/dart-sdk/bin/dart.exe',
    ['packages/open_aot_patch_tools/bin/open_aot_patch_tools.dart', ...args],
    workingDirectory: toolWorkspace,
  );
}

Future<ProcessResult> _runToolExpectFailure(
  Directory toolWorkspace,
  List<String> args,
) async {
  final result = await Process.run(
    '${toolWorkspace.parent.path}/dart-sdk-new/tools/sdks/dart-sdk/bin/dart.exe',
    ['packages/open_aot_patch_tools/bin/open_aot_patch_tools.dart', ...args],
    workingDirectory: toolWorkspace.path,
  );
  if (result.exitCode == 0) {
    throw StateError(
      'Expected open_aot_patch_tools ${args.join(' ')} to fail.',
    );
  }
  return result;
}

Future<ProcessResult> _run(
  String executable,
  List<String> args, {
  Directory? workingDirectory,
}) async {
  final result = await Process.run(
    executable,
    args,
    workingDirectory: workingDirectory?.path,
  );
  if (result.exitCode != 0) {
    throw ProcessException(
      executable,
      args,
      [
        'exit code ${result.exitCode}',
        'stdout:',
        result.stdout,
        'stderr:',
        result.stderr,
      ].join('\n'),
      result.exitCode,
    );
  }
  return result;
}

void _expectStatus(
  String output, {
  required String license,
  required bool proFeature,
}) {
  final lines = const LineSplitter()
      .convert(output.trim())
      .where((line) => line.isNotEmpty)
      .toList();
  final expectedFeature = proFeature ? 'enabled' : 'off';
  if (!lines.contains('license:$license') ||
      !lines.contains('pro-feature:$expectedFeature')) {
    throw StateError(
      'Unexpected app status. Expected license:$license and '
      'pro-feature:$expectedFeature, got:\n$output',
    );
  }
}

String _sha256File(File file) {
  final result = Process.runSync('certutil', [
    '-hashfile',
    file.path,
    'SHA256',
  ]);
  if (result.exitCode != 0) {
    throw ProcessException(
      'certutil',
      ['-hashfile', file.path, 'SHA256'],
      result.stderr.toString(),
      result.exitCode,
    );
  }
  final match = RegExp(
    r'\b[0-9a-fA-F]{64}\b',
  ).firstMatch('${result.stdout}\n${result.stderr}');
  if (match == null) {
    throw StateError('Could not parse SHA256 hash for ${file.path}.');
  }
  return match.group(0)!.toLowerCase();
}

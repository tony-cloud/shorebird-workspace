#!/usr/bin/env python3
"""Exercise Dart-produced AES envelopes in a live precompiled host isolate.

Requires a built SDK with dart_enable_aot_patching=true,
dart_enable_shorebird_interpreter=true, and dart_dynamic_modules=false. Reuses
the SDK's body-patch fixture and compiler, then bridges the actual Dart crypto
producer to Dart_InstallAotPatch -> Dart_ReloadBytecodePatch -> payload free.
This is host runtime evidence, not an iOS device or FlutterEngine test.
Use --structural for the opted-in boot-only structural baseline and delta.
Use --full-replacement for the experimental complete-domain wrapper profile.
Add --product to exercise matching PRODUCT compiler, platform and shared runtime.
"""

import argparse
import contextlib
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile


DART_PRODUCER = r"""
import 'dart:convert';
import 'dart:io';
import 'package:open_aot_patch_tools/open_aot_patch_tools.dart';

void main(List<String> args) {
  final baseline = File(args[0]).readAsBytesSync();
  final delta = File(args[1]).readAsBytesSync();
  final out = Directory(args[2])..createSync(recursive: true);
  Map<String, Object?> envelope(String expiry, int nonceSeed) {
    final metadata = PatchMetadata(
      appId: 'encrypted-aot-host-app', appBuildId: '1.0.0+1',
      baseFlavorId: 'free', baseLicenseType: 'free',
      flavorId: 'pro', licenseType: 'pro', sdkHash: 'encrypted-aot-host-sdk',
      baseSnapshotHash: sha256Hex(baseline), patchSnapshotHash: sha256Hex(delta),
      targetOs: 'ios', targetArch: 'arm64',
      runtimeMode: runtimeModeDartBytecodeInterpreter,
      offlineExpiresAt: expiry,
    );
    return encryptArtifact(
      artifact: linkArtifacts(baseSnapshot: baseline, patchSnapshot: delta,
        metadata: metadata, forceFullSnapshot: true),
      keyId: 'encrypted-aot-host-key',
      // Fixed TEST-ONLY AES key; never use this fixture key in a real app.
      key: List<int>.filled(32, 0x42),
      nonce: List<int>.generate(12, (i) => i + nonceSeed),
    ).toJson();
  }
  final valid = envelope('9999-12-31T23:59:59.999999Z', 0);
  File('${out.path}/valid.json').writeAsStringSync(jsonEncode(valid));
  final expired = envelope('2000-01-01T00:00:00.000Z', 32);
  File('${out.path}/expired.json').writeAsStringSync(jsonEncode(expired));
  final tampered = Map<String, Object?>.of(valid);
  final ciphertext = base64Decode(tampered['encrypted_payload_base64'] as String);
  ciphertext[0] ^= 1;
  tampered['encrypted_payload_base64'] = base64Encode(ciphertext);
  File('${out.path}/tampered.json').writeAsStringSync(jsonEncode(tampered));
}
"""


HARNESS = r"""
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
#include "include/dart_api.h"
#include "include/dart_engine.h"

bool wrong_key = false;
int key_requests = 0;

bool ResolveKey(const char* id, uint8_t* buffer, intptr_t capacity,
                intptr_t* length) {
  ++key_requests;
  if (!id || std::strcmp(id, "encrypted-aot-host-key") != 0 || capacity < 32) {
    return false;
  }
  std::memset(buffer, wrong_key ? 0x43 : 0x42, 32);
  *length = 32;
  return true;
}

void Check(Dart_Handle value) {
  if (Dart_IsError(value)) {
    std::cerr << Dart_GetError(value) << std::endl;
    std::exit(1);
  }
}

int64_t Probe(const char* name) {
  auto value = Dart_Invoke(Dart_RootLibrary(),
                          Dart_NewStringFromCString(name), 0, nullptr);
  Check(value);
  int64_t result = 0;
  Check(Dart_IntegerToInt64(value, &result));
  return result;
}

void VerifyBodies(int64_t value) {
  const struct { const char* name; int64_t offset; } probes[] = {
    {"probe", 0}, {"probeArg", 10}, {"probeClosure", 20},
    {"probeMethod", 30}, {"probeNamed", 13}, {"probeDynamic", 40},
    {"probeObject", 0}, {"probeTearOff", 50},
    {"probeMethodTearOff", 60}, {"probeLicense", -1},
  };
  for (const auto& probe : probes) {
    if (Probe(probe.name) != value + probe.offset) {
      std::cerr << "Unexpected native caller result for " << probe.name << std::endl;
      std::exit(2);
    }
  }
}

std::vector<uint8_t> Read(const char* path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) { std::cerr << "Cannot read test fixture" << std::endl; std::exit(3); }
  return std::vector<uint8_t>((std::istreambuf_iterator<char>(input)), {});
}

struct Payload {
  uint8_t* bytes = nullptr;
  intptr_t length = 0;
  ~Payload() { Dart_FreeAotPatchPayload(bytes); }
};

void Reject(const char* label, const char* path,
            const Dart_AotPatchInstallOptions& options,
            const char* expected_error, int64_t expected_body = 1) {
  const auto envelope = Read(path);
  Payload payload;
  const int previous_requests = key_requests;
  const auto result = Dart_InstallAotPatch(
      envelope.data(), envelope.size(), &options, &payload.bytes, &payload.length);
  if (!Dart_IsError(result) || payload.bytes != nullptr || payload.length != 0 ||
      key_requests != previous_requests + 1 ||
      std::strstr(Dart_GetError(result), expected_error) == nullptr) {
    std::cerr << "Unexpected encrypted rejection: " << label << std::endl;
    if (Dart_IsError(result)) std::cerr << Dart_GetError(result) << std::endl;
    std::exit(4);
  }
  VerifyBodies(expected_body);
  std::cout << "PASS: " << label << "; prior bodies and license preserved" << std::endl;
}

int main(int argc, char** argv) {
  if (argc != 8 || !Dart_IsPrecompiledRuntime() || !Dart_AotPatchingEnabled()) {
    std::cerr << "Requires the precompiled interpreter/AES runtime" << std::endl;
    return 5;
  }
  char* error = nullptr;
  const auto snapshot = DartEngine_AotSnapshotFromFile(argv[1], &error);
  if (error) { std::cerr << error << std::endl; return 6; }
  const auto isolate = DartEngine_CreateIsolate(snapshot, &error);
  if (error) { std::cerr << error << std::endl; return 7; }
  DartEngine_AcquireIsolate(isolate);
  Dart_EnterScope();
  Dart_SetAotPatchKeyCallback(ResolveKey);

  Dart_AotPatchInstallOptions options = {};
  options.app_id = "encrypted-aot-host-app";
  options.app_build_id = "1.0.0+1";
  options.base_flavor_id = "free";
  options.base_license_type = "free";
  options.flavor_id = "pro";
  options.license_type = "pro";
  options.sdk_hash = "encrypted-aot-host-sdk";
  options.base_snapshot_hash = argv[6];
  options.patch_snapshot_hash = argv[7];
  // Exercise the same data-only iOS envelope policy on the host AOT runtime.
  options.target_os = "ios";
  options.target_arch = "arm64";
  options.runtime_mode = "dart-bytecode-interpreter";

  VerifyBodies(1);
  std::cout << "PASS: precompiled baseline probe=1, license=free" << std::endl;
  wrong_key = true;
  Reject("wrong AES key", argv[2], options, "AES-GCM decryption failed");
  wrong_key = false;
  Reject("tampered ciphertext", argv[3], options, "AES-GCM decryption failed");
  Reject("authenticated expired envelope", argv[4], options, "offline license has expired");

  {
    const auto envelope = Read(argv[2]);
    const auto expected = Read(argv[5]);
    Payload payload;
    const int previous_requests = key_requests;
    Check(Dart_InstallAotPatch(envelope.data(), envelope.size(), &options,
                              &payload.bytes, &payload.length));
    if (key_requests != previous_requests + 1 ||
        payload.length != static_cast<intptr_t>(expected.size()) ||
        !std::equal(expected.begin(), expected.end(), payload.bytes)) {
      std::cerr << "Decrypted component differs from the SDK delta" << std::endl;
      return 8;
    }
    Check(Dart_ReloadBytecodePatch(payload.bytes, payload.length));
    // Payload is freed here, before invoking any replaced function.
  }
  VerifyBodies(2);
  std::cout << "PASS: AES install -> bytecode reload -> free; probe=2, license=pro" << std::endl;
  Reject("tampered update after valid install", argv[3], options,
         "AES-GCM decryption failed", 2);

  Dart_SetAotPatchKeyCallback(nullptr);
  Dart_ExitScope();
  DartEngine_ReleaseIsolate();
  DartEngine_Shutdown();
  std::cout << "PASS: encrypted patch executed in the same no-JIT AOT process" << std::endl;
  return 0;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', required=True, type=Path)
    parser.add_argument('--work-dir', type=Path,
                        help='Preserve fixtures and stage logs in this directory')
    parser.add_argument('--tooling-dart', type=Path,
                        help='Dart SDK used to run the real envelope producer')
    parser.add_argument('--cxx', default='c++')
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument('--structural', action='store_true',
                        help='Verify encrypted boot-only structural additions')
    profile.add_argument('--full-replacement', action='store_true',
                         help='Verify encrypted complete-domain replacement')
    parser.add_argument('--product', action='store_true',
                        help='Use PRODUCT compiler/runtime (requires a boot profile)')
    args = parser.parse_args()
    if args.product and not (args.structural or args.full_replacement):
        parser.error('--product requires --structural or --full-replacement')
    root = Path(__file__).resolve().parents[1]
    sdk = root / 'dart-sdk'
    build = args.build_dir.resolve()
    tooling_dart = (args.tooling_dart or
                    sdk / 'tools/sdks/dart-sdk/bin/dart').resolve()
    packages = root / 'shorebird/.dart_tool/package_config.json'
    snapshot_compiler = build / ('gen_snapshot_product' if args.product
                                  else 'gen_snapshot')
    for required in (build / 'dart', snapshot_compiler, tooling_dart, packages):
        if not required.is_file():
            parser.error(f'Required artifact is missing: {required}')
    config = build / 'args.gn'
    if not config.is_file():
        parser.error(f'The SDK build configuration is missing: {config}')
    flags = dict(re.findall(r'(?m)^\s*(dart_\w+)\s*=\s*(true|false)\s*$',
                            config.read_text()))
    for flag, expected in (('dart_enable_aot_patching', 'true'),
                           ('dart_enable_shorebird_interpreter', 'true'),
                           ('dart_dynamic_modules', 'false')):
        if flags.get(flag) != expected:
            parser.error(f'This test requires an SDK built with {flag}={expected}')

    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    workspace = (contextlib.nullcontext(args.work_dir.resolve())
                 if args.work_dir else
                 tempfile.TemporaryDirectory(prefix='shorebird-encrypted-aot-'))
    with workspace as temporary:
        work = Path(temporary)

        def run(label, command):
            result = subprocess.run([str(value) for value in command], cwd=root,
                                    capture_output=True, text=True, timeout=900)
            (work / f'{label}.log').write_text(result.stdout + result.stderr)
            if result.returncode:
                sys.stderr.write(result.stdout + result.stderr)
                raise RuntimeError(f'{label} failed with exit {result.returncode}')
            return result.stdout

        if args.structural or args.full_replacement:
            producer = work / 'encrypt_fixture.dart'
            producer.write_text(DART_PRODUCER)
            name = 'full-replacement' if args.full_replacement else 'structural'
            harness = ('test_aot_full_replacement.py' if args.full_replacement
                       else 'test_aot_structural_patch.py')
            output = run(f'encrypted-{name}-runtime', [
                sys.executable, sdk / 'tools' / harness,
                '--build-dir', build, '--work-dir', work / 'sdk-fixture',
                '--cxx', args.cxx,
                *(['--product'] if args.product else []),
                '--envelope-producer', producer,
                '--envelope-packages', packages,
                '--envelope-dart', tooling_dart,
            ])
            print(output, end='')
            return

        fixture = work / 'sdk-fixture'
        run('sdk-fixture', [sys.executable, sdk / 'tools/test_aot_bytecode_patch.py',
                           '--build-dir', build, '--work-dir', fixture,
                           '--cxx', args.cxx])
        print('PASS: reused SDK baseline and structural-delta fixture', flush=True)

        producer = work / 'encrypt_fixture.dart'
        producer.write_text(DART_PRODUCER)
        envelopes = work / 'envelopes'
        run('dart-encryption', [tooling_dart, f'--packages={packages}', producer,
                                fixture / 'baseline.bytecode',
                                fixture / 'valid.bytecode', envelopes])
        print('PASS: envelopes encrypted by the real Dart patch tooling', flush=True)

        source = work / 'encrypted_runtime.cc'
        source.write_text(HARNESS)
        executable = work / 'encrypted_runtime'
        run('compile-harness', [args.cxx, '-std=c++17', f'-I{sdk / "runtime"}',
                               f'-I{sdk / "runtime/engine"}', source,
                               f'-L{build}', '-ldart_engine_aot_shared',
                               f'-Wl,-rpath,{build}', '-o', executable])
        baseline_hash = hashlib.sha256((fixture / 'baseline.bytecode').read_bytes()).hexdigest()
        delta_hash = hashlib.sha256((fixture / 'valid.bytecode').read_bytes()).hexdigest()
        output = run('encrypted-runtime', [executable, fixture / 'base.snapshot',
                                          envelopes / 'valid.json',
                                          envelopes / 'tampered.json',
                                          envelopes / 'expired.json',
                                          fixture / 'valid.bytecode',
                                          baseline_hash, delta_hash])
        print(output, end='')


if __name__ == '__main__':
    main()

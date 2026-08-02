# Linux, macOS, and iOS Platform Testing

The first complete runtime proof was built on Windows. The next platform pass
should verify the same source layout on Linux and macOS, then use macOS to
prepare iOS testing without introducing JIT or writable-executable-memory
requirements.

## Prerequisites

Linux:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.23+
- Python and standard build dependencies required by Dart/Flutter

macOS:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.23+
- Xcode command line tools
- Full Xcode for iOS simulator/device follow-up

Both platforms need network access for a fresh `git submodule update` and
`gclient sync`.

## Bootstrap

Linux:

```bash
./scripts/bootstrap_linux.sh
```

macOS:

```bash
./scripts/bootstrap_macos.sh
```

Useful environment variables:

| Variable | Effect |
| --- | --- |
| `SKIP_GCLIENT_SYNC=1` | Do not run `gclient sync`. Useful when dependencies are already present. |
| `SKIP_TESTS=1` | Prepare checkout only; skip Dart/Go tests. |
| `DART_BIN=/path/to/dart` | Override the Dart executable. |
| `FLUTTER_BIN=/path/to/flutter` | Override the Flutter executable. |
| `GO_BIN=/path/to/go` | Override the Go executable. |
| `RUN_IOS_SMOKE=1` | On macOS, run a no-codesign iOS build smoke check when Flutter/Xcode are available. |
| `INCLUDE_ENGINE_DEPS=1` | On macOS, write the Flutter engine `.gclient` and include iOS, Android, and web/emsdk dependencies. |
| `AOT_PATCH_BUILD_DIR=/path/to/build` | Override the Dart SDK AOT patch build used by the license/flavor verifier. |
| `IOS_ENGINE_DIR=/path/to/ios_release` | Override the iOS engine build checked by `verify_ios_interpreter_route.sh`. |
| `HOST_ENGINE_DIR=/path/to/host_release_arm64` | Override the host engine build checked by `verify_ios_interpreter_route.sh`. |
| `IOS_APP_BUNDLE=/path/to/Runner.app` | Optionally verify that a reviewed app bundle does not contain a seeded patch payload. |
| `IOS_IPA=/path/to/App.ipa` | Optionally verify the `Payload/*.app` inside an IPA instead of a raw app bundle. |
| `IOS_PATCH_ARTIFACT=/path/to/dlc.vmcode` | Optionally verify that the patch artifact is an encrypted iOS arm64 interpreter full-snapshot artifact. |
| `APP_STORE_STRICT=1` | Fail the iOS route check when App Store-inappropriate signing state, such as `get-task-allow=true`, is present. |

## Expected Checks

The bootstrap scripts run:

1. `git submodule update --init --recursive`
2. platform-specific `.gclient` generation
3. optional `gclient sync`
4. public updater sync into the Flutter engine third-party location
5. Shorebird CLI focused tests for self-hosted `base_url` behavior
6. open AOT patch tool tests
7. self-hosted server Go tests
8. license/flavor AOT verification when an AOT patch SDK build already exists.
   The verifier checks `AOT_PATCH_BUILD_DIR` first, then common local outputs
   such as `dart-sdk-new/xcodebuild/ReleaseARM64`,
   `dart-sdk-new/out/ReleaseARM64AotPatch`, and
   `dart-sdk-new/out/ReleaseX64AotPatch`.
9. iOS interpreter route verification when local `ios_release` and
   `host_release_arm64` engine args exist. This checks that
   `DART_DYNAMIC_MODULES` is off, the no-DDM interpreter route is on for both
   the device engine and host snapshotter, and the native AOT patch path is off
   for iOS.

## iOS Preparation Notes

The macOS script writes this minimal root `.gclient` by default:

```python
target_os = ["mac", "ios"]
```

Set `INCLUDE_ENGINE_DEPS=1` to also write `flutter/.gclient` and include
Android plus web/emsdk engine dependencies:

```python
target_os = ["mac", "ios", "android"]
```

The native AOT `.vmcode` path is useful for development-device validation, but
it is not the iOS App Store candidate route because it maps downloaded native
snapshot text as executable memory. The iOS interpreter route keeps
`DART_DYNAMIC_MODULES` disabled and uses the Dart SDK's no-DDM interpreter patch
runtime mode instead of loading native AOT code directly. Real App Store or
TestFlight acceptance still has to be proven through Apple's review flow.

Use this gate before producing an App Store/TestFlight archive:

```sh
IOS_APP_BUNDLE=testapps/license_flavor_patch_test/build/ios/iphoneos/Runner.app \
  IOS_PATCH_ARTIFACT=/path/to/shorebird_updater/patches/1/dlc.vmcode \
  APP_STORE_STRICT=1 \
  ./scripts/verify_ios_interpreter_route.sh
```

For an exported IPA, use:

```sh
IOS_IPA=/path/to/App.ipa APP_STORE_STRICT=1 \
  ./scripts/verify_ios_interpreter_route.sh
```

The app-bundle/IPA check is intentionally limited to review hygiene and
executable-memory risk indicators: a submitted app should not include a
pre-seeded `shorebird_updater` directory or `dlc.vmcode` payload, should not
request JIT or unsigned-executable-memory entitlements, and should not have
`get-task-allow=true` in strict mode. When `IOS_PATCH_ARTIFACT` is set, the gate
also rejects Mach-O and ELF patch files and requires the encrypted open artifact
metadata to declare `runtime_mode=dart-bytecode-interpreter`, `target_os=ios`,
`target_arch=arm64`, and `payload_kind=full-snapshot`. Real patches should
arrive through the updater after the reviewed app is installed.

## iOS Device Results

On June 24, 2026, a physical iPad Pro 13-inch (M4), iOS 26.5, successfully ran
the license/flavor patch test in release mode with the local `ios_release`
engine and `host_release_arm64` host engine.

The working development proof used a free baseline app installed on device and a
pro AOT Mach-O patch seeded into:

```text
Library/Application Support/shorebird/shorebird_updater/patches/1/dlc.vmcode
```

The patch payload must be signed with a valid Apple development/distribution
identity. An ad-hoc signed payload reached the updater but failed during
executable mapping with `mmap failed Operation not permitted`. Re-signing the
patch Mach-O with the Apple Development identity allowed the release app to boot
from patch `1`; the updater state promoted `last_booted_patch` to `1`, and an
instrumented pro payload wrote:

```text
license:pro
pro-feature:enabled
```

This result proves updater selection, metadata compatibility, signing
requirements, and the native development path. It does not make native AOT patch
execution App Store-safe. The iOS App Store candidate test target is the
interpreter runtime mode (`runtime_mode=dart-bytecode-interpreter`) with
`DART_DYNAMIC_MODULES` disabled.

On June 24, 2026, the local `ios_release` engine also built successfully with
the no-DDM interpreter configuration:

```gn
dart_dynamic_modules = false
dart_enable_aot_patching = true
dart_enable_shorebird_interpreter = true
shorebird_use_interpreter = true
shorebird_enable_aot_patching = false
```

The matching host `gen_snapshot` build must also enable the no-DDM interpreter
path when it is used to produce iOS AOT snapshots:

```sh
flutter/tools/gn --runtime-mode=release --mac-cpu=arm64 \
  --target-dir=host_release_arm64 --shorebird-interpreter
ninja -C out/host_release_arm64 gen_snapshot
```

Without the host-side interpreter flag, the generated app snapshot can bind AOT
static calls directly to base `Code` objects and omit the static-call metadata
that the iOS interpreter runtime uses to enter patched bytecode.

The current open-source interpreter path wires the no-DDM artifact handoff and
an initial product-AOT function replacement mapper. Interpreter-mode artifacts
now force and validate `payload_kind=full-snapshot`; compact interpreter diffs
are rejected until runtime reconstruction exists. The engine can hand an
encrypted open patch artifact to `Dart_InstallAotPatch`, decrypt it with an
explicit `shorebird.yaml` AES key, verify the decrypted payload is Dart
bytecode, and install it through `Dart_ReloadBytecodePatch`.

For product iOS AOT, `Dart_ReloadBytecodePatch` no longer uses stock VM reload.
It reads patch bytecode as data, maps declarations to already-loaded
libraries/classes/functions, and switches matched existing functions to the VM's
signed `InterpretCall` stub with `Function::AttachBytecode`. This updates VM
heap metadata only; it does not load native AOT patch files, map downloaded
text executable, or enable `DART_DYNAMIC_MODULES`.

On June 25, 2026, the no-DDM interpreter route was verified on the same real
iPad. The app was built as a release iOS app with the local `ios_release` engine
and `host_release_arm64` snapshotter, then installed on device:

```sh
flutter build ios --release \
  --local-engine-src-path=/Users/tonylu/git/shorebird-workspace/flutter/engine/src \
  --local-engine=ios_release \
  --local-engine-host=host_release_arm64 \
  --dart-define=LICENSE_TYPE=free

xcrun devicectl device install app \
  --device 3289ED7F-552F-59D2-984F-AAE78463DA70 \
  testapps/license_flavor_patch_test/build/ios/iphoneos/Runner.app \
  --timeout 120
```

The patch seed was copied into the app container:

```text
Library/Application Support/shorebird/shorebird_updater/
```

with these files:

```text
patches/1/dlc.vmcode
patches/1/state.json
pointers.json
state.json
```

Launching the app with that seeded patch displayed:

```text
license:pro
```

This verifies the real-device interpreter patch flow for an existing-function
full-snapshot bytecode payload: updater selection, artifact decryption,
`Dart_ReloadBytecodePatch`, bytecode attachment, and static-call dispatch all
worked in a release iOS app on physical hardware.

The current release app bundle also passes the stricter pre-review route gate:

```sh
APP_STORE_STRICT=1 \
IOS_APP_BUNDLE=testapps/license_flavor_patch_test/build/ios/iphoneos/Runner.app \
IOS_PATCH_ARTIFACT=/private/tmp/shorebird-seed-main/shorebird_updater/patches/1/dlc.vmcode \
  ./scripts/verify_ios_interpreter_route.sh
```

This verified the generated iOS and host engine args, found no bundled
`shorebird_updater` or `dlc.vmcode` payload in the app bundle, found no JIT or
unsigned-executable-memory entitlement, confirmed `get-task-allow` is not true,
and verified that the seeded patch artifact is an encrypted interpreter
full-snapshot for iOS arm64. Passing a Mach-O binary as `IOS_PATCH_ARTIFACT`
correctly fails the route gate.

The remaining App Store/TestFlight proof is a distribution test, not a local
build test:

1. Export the reviewed app as an App Store/TestFlight IPA.
2. Run the route gate:

   ```sh
   APP_STORE_STRICT=1 \
   IOS_IPA=/path/to/App.ipa \
   IOS_PATCH_ARTIFACT=/path/to/dlc.vmcode \
     ./scripts/verify_ios_interpreter_route.sh
   ```

3. Upload the IPA to App Store Connect and install it through TestFlight or the
   App Store.
4. Publish or seed the interpreter patch after that reviewed app is installed.
5. Relaunch the installed app and verify the app displays the patched behavior
   (`license:pro` for the current smoke app).

This is still a runtime milestone, not a complete App Store proof. The mapper
currently targets existing libraries/classes/functions only. The patch compiler
must emit an interpreter payload in that compatible shape, and compact
reconstruction still needs to move into the SDK before compact bytecode patches
can be accepted.

For development-device encrypted interpreter tests, `shorebird.yaml` may include
these top-level fields:

```yaml
aot_patch_key_id: test-key
aot_patch_key_hex: 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
aot_patch_base_flavor_id: free
aot_patch_base_license_type: free
aot_patch_flavor_id: pro
aot_patch_license_type: pro
aot_patch_sdk_hash: <flutter-engine-or-dart-sdk-hash>
aot_patch_base_snapshot_hash: <sha256-of-base-bytecode-snapshot>
```

The YAML key path is only the first open bridge for local verification. A
production App Store implementation still needs an app-owned key callback or
equivalent secure key source, a verified compatible interpreter payload
compiler, compact interpreter reconstruction, and TestFlight/App Store review
validation.

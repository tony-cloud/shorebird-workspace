# Platform Testing

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
- Flutter Linux desktop build dependencies (`clang`, `cmake`, `ninja`,
  `pkg-config`, GTK development headers)
- A graphical session or `xvfb-run` for headless runtime smoke tests

macOS:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.23+
- Xcode command line tools
- Full Xcode for iOS simulator/device follow-up

Android runtime validation:

- Android SDK platform tools (`adb`)
- Java/JDK on `PATH` for APK builds
- A connected Android device or emulator
- Either `adb shell run-as` access for the test package or a rooted
  emulator/device. The seeded runtime smoke writes directly to the test app's
  private updater directory; production installs normally receive patches from
  the updater download flow.

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
| `ANDROID_SERIAL=<serial>` | Select the Android device used by `scripts/android_runtime_patch_smoke.sh` when more than one device is attached. |
| `SKIP_ANDROID_BUILDS=1` | Reuse APKs for the Android runtime smoke instead of rebuilding; set `ANDROID_FREE_APK` and `ANDROID_PRO_APK`. |
| `KEEP_ANDROID_RUNTIME_SMOKE_ARTIFACTS=1` | Keep the temporary APK/libapp extraction directory after the Android runtime smoke. |
| `LINUX_RUNTIME_SMOKE_XVFB=0` | Disable automatic `xvfb-run` wrapping in `scripts/linux_runtime_patch_smoke.sh`. |
| `KEEP_LINUX_RUNTIME_SMOKE_ARTIFACTS=1` | Keep the temporary Linux app copy and free/pro bundles after the Linux runtime smoke. |

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
   such as `dart-sdk/xcodebuild/ReleaseARM64`,
   `dart-sdk/out/ReleaseARM64AotPatch`, and
   `dart-sdk/out/ReleaseX64AotPatch`.
9. iOS interpreter route verification when local `ios_release` and
   `host_release_arm64` engine args exist. This checks that
   `DART_DYNAMIC_MODULES` is off, the no-DDM interpreter route is on for both
   the device engine and host snapshotter, and the native AOT patch path is off
   for iOS.

## Current Coverage Status

| Platform | Open patch route | Current status |
| --- | --- | --- |
| iOS | Encrypted Dart bytecode interpreter artifact, `DART_DYNAMIC_MODULES=false` | Verified on a real iPad on June 25, 2026; the patched app displayed `license:pro`. |
| Android | Public updater binary diff `patch-*.zip` artifact with native AOT patch runtime enabled | CI builds mirror-ready patch binaries and a manual `android-engine-arm64` artifact with `dart_enable_aot_patching=true`, `shorebird_enable_aot_patching=true`, and interpreter mode off; local arm64 release APK build passed. Runtime smoke script is available, but this macOS host currently has no Android device or AVD attached. |
| macOS | Public updater binary diff `patch-*.zip` artifact with native AOT patch runtime enabled | Verified locally on June 25, 2026 with a native-AOT Flutter snapshot patch; the patched app displayed `license:pro`. CI builds macOS x64/arm64 patch binaries and a manual `macos-engine-arm64` artifact with native AOT patch runtime args verified. |
| Linux | Public updater binary diff `patch-*.zip` artifact with native AOT patch runtime enabled | CI builds the Linux x64 patch binary and a manual `linux-engine-x64` artifact with native AOT patch runtime args verified. The runtime smoke defaults to `linux_release_x64`; local runtime validation requires a Linux runner. |
| Windows | Public updater binary diff `patch-*.zip` artifact | Previously tested; CI still builds the Windows patch binary. |
| Web | Not a Shorebird CodePush release platform in this CLI/protocol | Manual CI builds `flutter-web-sdk`; local release web build passed with the matching local engine, but it is not counted as a Shorebird runtime patch test. |

The open CI currently does not publish `aot-tools.dill`. That artifact belongs
to the legacy native-AOT iOS linker path. The App Store-safe iOS route proven
above does not use it; Android/Linux/macOS/Windows patch creation uses the
public updater `patch` binary instead.

## macOS Host Smoke Results

On June 25, 2026, the default source-level bootstrap path passed locally with
`SKIP_GCLIENT_SYNC=1`, the workspace Flutter/Dart SDK, and network access for
pub packages:

```sh
SKIP_GCLIENT_SYNC=1 \
DART_BIN=/Users/tonylu/git/shorebird-workspace/flutter/bin/dart \
FLUTTER_BIN=/Users/tonylu/git/shorebird-workspace/flutter/bin/flutter \
  ./scripts/bootstrap_linux.sh
```

This ran the focused Shorebird CLI tests, open patch tool tests, self-hosted
server Go tests, and license/flavor AOT verifier. The AOT verifier reported:

```text
AOT patch applied to license_flavor_patch_test successfully.
base: license:free / pro-feature:off
patch: license:pro / pro-feature:enabled
```

The same app also built successfully for macOS release with the local engine:

```sh
flutter build macos --release \
  --local-engine-src-path=/Users/tonylu/git/shorebird-workspace/flutter/engine/src \
  --local-engine=host_release_arm64 \
  --local-engine-host=host_release_arm64 \
  --dart-define=LICENSE_TYPE=free
```

Output:

```text
build/macos/Build/Products/Release/license_flavor_patch_test.app
```

On June 25, 2026, the macOS runtime patch path was also verified locally. The
test saved a free baseline app, rebuilt the same Flutter app with
`--dart-define=LICENSE_TYPE=pro`, copied the resolved pro
`App.framework/Versions/A/App` Mach-O into the sandboxed updater container as:

```text
Library/Application Support/shorebird/shorebird_updater/
  license-flavor-patch-test/patches/1/dlc.vmcode
```

and seeded `pointers.json` plus `patches/1/state.json` for patch `1`.
Launching the saved free app selected that patch path and wrote:

```text
license:pro
pro-feature:enabled
```

The seeded payload must be a Flutter app snapshot for this smoke. A standalone
Dart verifier snapshot is loadable by the updater handoff, but it is not a
valid Flutter application isolate and fails later with `dart:ui` missing.
When copying from `App.framework/App`, resolve the framework symlink first
(`Versions/A/App` or `cp -L`) so the updater validates the real Mach-O size
rather than the symlink size.

Android arm64 release also built successfully with the local Android engine:

```sh
flutter build apk --release \
  --target-platform android-arm64 \
  --local-engine-src-path=/Users/tonylu/git/shorebird-workspace/flutter/engine/src \
  --local-engine=android_release_arm64 \
  --local-engine-host=host_release_arm64 \
  --dart-define=LICENSE_TYPE=free
```

Output:

```text
build/app/outputs/flutter-apk/app-release.apk
```

Android runtime validation is scripted but was not run to completion on this
macOS host because `adb devices -l` returned no Android devices and
`emulator -list-avds` returned no AVDs. When a target is available, run:

```sh
ANDROID_SERIAL=<device-or-emulator-serial> \
  ./scripts/android_runtime_patch_smoke.sh
```

The same script can be run from GitHub Actions with `workflow_dispatch` and
`run_runtime_smokes=true` on a provisioned Android runner. Use
`android_runtime_runner` to choose the runner label and `android_serial` to
select the target device when needed.

The script builds free and pro arm64 release APKs with the local Android
engine, installs the free APK, verifies the visible `license:free` and
`pro-feature:off` UI with `uiautomator`, extracts the pro `libapp.so`, seeds it
as patch `1` under the app-private `files/shorebird_updater` directory,
relaunches the free app, and verifies `license:pro` plus
`pro-feature:enabled`.

The seed step needs access to app-private storage. Use an emulator/device where
`adb shell run-as com.example.licenseflavorpatchtest.license_flavor_patch_test`
works, or a rooted emulator/device where `adb root` works. This seeded smoke is
for local runtime proof; production Android patch installation should exercise
the updater download path through the self-hosted server.

A normal web release build succeeds when pointed at the matching local engine:

```sh
flutter build web --release \
  --local-engine-src-path=/Users/tonylu/git/shorebird-workspace/flutter/engine/src \
  --local-engine=host_release_arm64 \
  --local-engine-host=host_release_arm64 \
  --dart-define=LICENSE_TYPE=free
```

Output:

```text
build/web/main.dart.js
```

Running the same web build without `--local-engine` currently fails because the
prebuilt `const_finder.dart.snapshot` in `flutter/bin/cache` expects a different
kernel binary format than this workspace Dart SDK emits. Use the local engine
when validating this workspace's web build artifacts.

The self-hosted server bundle packaging was also smoke-tested locally for
`darwin/arm64`: the archive was extracted, the server started with SQLite and
local storage, `/health` returned `status=ok`, and `/` served the packaged
dashboard HTML from the sibling `web/` directory. The server serves the checked
OpenAPI contract at `/openapi.yaml`, and CI verifies that endpoint from the
packaged Linux x64 archive. CI server artifacts therefore package `web/` and
`openapi.yaml` next to the binary instead of uploading a bare executable.

## Linux Runtime Smoke

Linux runtime validation is scripted for a Linux host:

```sh
./scripts/linux_runtime_patch_smoke.sh
```

The script copies `testapps/license_flavor_patch_test` into a temporary
directory, generates the missing Linux platform scaffold there with
`flutter create --platforms=linux`, builds free and pro release bundles with the
local Linux engine, launches the saved free bundle, verifies
`license:free`/`pro-feature:off`, seeds the pro bundle's `lib/libapp.so` as
patch `1` under:

```text
~/.shorebird_cache/shorebird_updater/license-flavor-patch-test/
```

then relaunches the saved free bundle and verifies `license:pro` plus
`pro-feature:enabled`. If no `DISPLAY` is set and `xvfb-run` exists, the script
uses it automatically.

This macOS host cannot execute Linux desktop bundles, so this check remains a
Linux-runner task. The same check can be triggered in GitHub Actions with
`workflow_dispatch` and `run_runtime_smokes=true` on a provisioned Linux
runner. Use `linux_runtime_runner` to choose the runner label; the runner must
preserve the matching `flutter/engine/src/out/host_release` local engine output.

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
`target_arch=arm64`, and `payload_kind=full-snapshot`. It parses the artifact
as JSON and also requires the AES-256-GCM wrapper fields, base64 ciphertext,
nonce/tag, key id, payload digest, and AAD digest to be present and well
formed. In strict mode, the gate also rejects bundled `aot_patch_key_hex`
values; production apps should provide patch keys with
`FlutterDartProject.shorebirdAotPatchKeyProvider` or an equivalent app-owned
key source. Real patches should arrive through the updater after the reviewed
app is installed.

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
  --target-dir=host_release_arm64 --shorebird-interpreter \
  --gn-args='dart_dynamic_modules=false dart_enable_aot_patching=true dart_enable_shorebird_interpreter=true shorebird_use_interpreter=true'
ninja -C out/host_release_arm64 gen_snapshot
```

Without the host-side interpreter and AOT patch flags, the generated app
snapshot can bind AOT static calls directly to base `Code` objects and omit the
static-call metadata
that the iOS interpreter runtime uses to enter patched bytecode.

The current open-source interpreter path wires the no-DDM artifact handoff and
an initial product-AOT function replacement mapper. Interpreter-mode artifacts
now force and validate `payload_kind=full-snapshot`; compact interpreter diffs
are rejected until runtime reconstruction exists. The engine can hand an
encrypted open patch artifact to `Dart_InstallAotPatch`, decrypt it with an
app-owned AES key provider, verify the decrypted payload is Dart bytecode, and
install it through `Dart_ReloadBytecodePatch`. A development-only
`aot_patch_key_hex` field in `shorebird.yaml` is still supported when no app key
provider is configured, but strict App Store-route checks reject it.

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
confirmed no `aot_patch_key_hex` is bundled, and verified that the seeded patch
artifact is an encrypted interpreter full-snapshot for iOS arm64. Passing a
Mach-O binary as `IOS_PATCH_ARTIFACT` correctly fails the route gate.

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

For encrypted interpreter tests, `shorebird.yaml` may include these top-level
fields:

```yaml
aot_patch_runtime_mode: dart-bytecode-interpreter
aot_patch_key_id: test-key
aot_patch_app_build_id: 1.2.3+4
aot_patch_base_flavor_id: free
aot_patch_base_license_type: free
aot_patch_flavor_id: pro
aot_patch_license_type: pro
aot_patch_sdk_hash: <flutter-engine-or-dart-sdk-hash>
aot_patch_base_snapshot_hash: <sha256-of-base-bytecode-snapshot>
```

`aot_patch_bytecode_path` is optional and should normally be omitted. When it
is omitted, `shorebird patch ios` compiles the patch target with the Dart SDK's
`dart2bytecode` snapshot and writes `build/ios_interpreter_patch.bytecode`.
The generated bytecode compile forwards user `--dart-define` values, Flutter's
`FLUTTER_APP_FLAVOR` define, and Flutter's standard version/revision/Dart SDK
defines from `bin/cache/flutter.version.json`. It also mirrors Flutter's
runtime feature flag define (`FLUTTER_ENABLED_FEATURE_FLAGS`) for enabled
runtime-id features.
Set `aot_patch_bytecode_path` or `SHOREBIRD_IOS_INTERPRETER_PATCH_PATH` only to
override that generated payload during local experiments.

Development-only local tests may also include `aot_patch_key_hex`, but reviewed
iOS app bundles should not. The `license_flavor_patch_test` iOS app now provides
the test AES key through `FlutterDartProject.shorebirdAotPatchKeyProvider`
instead.

`shorebird patch ios` now treats the interpreter route as the default iOS patch
artifact path. It packages the generated or overridden Dart bytecode payload as
an encrypted `open-aot-vmcode-encrypted-v1` full-snapshot artifact with
`runtime_mode=dart-bytecode-interpreter`, `target_os=ios`, and
`target_arch=arm64`. For production builds, provide the encryption key to the
CLI with `SHOREBIRD_AOT_PATCH_KEY_HEX` and keep `aot_patch_key_hex` out of the
bundled app. The legacy native-AOT iOS patch path is development-only and now
requires `SHOREBIRD_IOS_NATIVE_AOT_PATCH=1`.

A production App Store implementation still needs compact interpreter
reconstruction and TestFlight/App Store review validation.

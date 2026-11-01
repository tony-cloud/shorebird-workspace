# Platform Testing

Current completion is tracked in the [acceptance matrix](ACCEPTANCE_MATRIX.md).
Records below retain their original revision and validation scope.

The first complete runtime proof was built on Windows. The next platform pass
should verify the same source layout on Linux and macOS, then use macOS to
prepare iOS testing without introducing JIT or writable-executable-memory
requirements.

## October 2026 validation boundary

The June results below describe earlier snapshots and seeded patch tests. They
are not acceptance evidence for the current upstream merge or the complete
release/download/install lifecycle.

The current public SDK passes a macOS ARM64 precompiled-runtime regression with
ordinary unannotated functions, positional/named/default arguments, closures,
polymorphic calls, SDK-interface overrides, cached tear-offs, and a license enum
changing from free to pro. `scripts/verify_encrypted_aot_runtime.py` runs the real
Dart AES envelope producer and VM install/reload/free APIs in the same process.
Wrong-key, tampered, and expired envelopes preserve the previous function bodies.
This is host runtime evidence; physical iOS validation remains separate.

The default interpreter profile replaces existing function bodies. Releases that
opt into `aot_patch_structural_updates: true` additionally support new libraries,
classes, functions, methods, static fields, and instance fields on existing
ordinary classes. The CLI binds this capability to the released baseline;
a patch cannot enable it for an older body-only release. Baselines must use the
matching interpreter-enabled compiler, stable identities, and no obfuscation.
Flutter forwards the explicit structural compiler flag along with `--no-tfa`.

Structural host acceptance passes with both development and PRODUCT runtimes,
including the real encrypted C API, native interface dispatch, generic classes,
added fields, compacting GC, weak cycles, and cross-isolate copies. Separate
PRODUCT standalone startup tests exercise full bootstrap, core APIs and
`Function.apply`, rather than relying only on an embedder that skips bootstrap.
Physical Flutter/iOS acceptance is a separate required gate.

On October 31, a distribution-signed structural release completed the real
CLI/server/updater lifecycle on a physical iPad with no debugger or JIT
entitlements. After downloading the encrypted patch and restarting, it rendered
a new generic Flutter widget and validated a new helper plus fields added to an
existing mutable class before reporting `license:pro`. The baseline retained
its original field values. Bytecode and native compilation used identical
Flutter version defines, and the manifest hash matched the captured defines.

Provisioning a deliberately wrong AES key caused the next launch to fall back to
the clean `license:free` baseline. Restoring the key did not reactivate the rejected
patch. A fresh patch then downloaded and booted successfully; another cold app
launch retained `pro` without a provisioning environment. The fixed server
accepted and persisted the device's canonical download and install events.

The tested toolchain was Dart `d5779da2cab`, Flutter `356567577b1`, CLI
`229fe706`, updater `7894c9d`, and engine artifact identity
`57ff1f88449aa4ecc134db978416c1a5d83ceff7`. Recovery/event acceptance used
server `7a64b41`. The fixture is `tool/structural` under
`testapps/license_flavor_patch_test`; the runner is `tool/ios_device_e2e.py`.
This establishes the additive structural profile, not arbitrary changes to
existing const classes, signatures, or static initializers.

The completed October 29 CI run
[40803323899](https://github.com/tony-cloud/shorebird-workspace/actions/runs/40803323899)
passed 27 jobs, including iOS/Android/Linux/macOS engines, Linux/macOS SDKs,
all CLI/server targets, Android host snapshotters, and universal macOS assembly.
Windows SDK configuration and Windows engine packaging failed. Windows engine
compilation itself passed; the packaging check detected a missing exported key
provider API. Follow-up CI [41179506485](https://github.com/tony-cloud/shorebird-workspace/actions/runs/41179506485)
found a further Windows SDK version mismatch between Dart toolchain files;
the declarations now agree on the installed SDK and a source check enforces this.
All-platform support and a publishable artifact
mirror require the final source revisions to pass the complete matrix.

The November 1 PRODUCT FFI/native-cache checkpoint `e0ef8e0` subsequently
completed all 30 configured build and validation jobs, including Windows SDK,
Windows engine, universal macOS assembly and the publishable artifact mirror.
The mirror and its 42-entry release manifest passed CI validation; the manifest
archive digest, checksum and exact source commit were also checked locally.
Four optional/default-branch-only jobs were intentionally skipped. See the
[completed CI record](validation/github-actions-full-build-20261101.json).
This is an older checkpoint: subsequent Windows memory changes, compiler
mapping provenance, CLI archive/integrity guards and the Linux GTK lifecycle
still require their newer CI results. Optional Android/Linux app runtime smokes
did not execute in this completed run.

Preparing an Android test from these packaged artifacts exposed a CLI metadata
defect: its default Flutter revision selected an older engine than the archive's
engine pin. Packaging now derives a coherent pair from the root Flutter gitlink
and verifies the pair and manifest before archiving and after extraction. A
separate fresh-cache CI failure exposed an unadvertised tracing dependency pin;
the corrected published fork pin passes fresh dependency resolution and 56
trace tests. The current hosted macOS x64 CLI artifact now has verified archive
hashes and matching packaged Flutter/engine/source pins; its help commands run
locally. Complete platform runtime acceptance remains pending. See the
[toolchain packaging record](validation/public-toolchain-packaging-20261101.json).

The new Android native lifecycle fixture uses an isolated non-debuggable app,
early Java/JNI provider registration and AndroidKeyStore-wrapped per-patch keys.
It implements real CLI/server delivery, cold offline recovery, missing/wrong-key
fallback, signed rollback and unchanged baseline APK checks. Java compilation,
arm64 NDK compilation and host protocol/ownership checks pass. A historical
release APK also built and installed, but baseline startup exposed the MTE bug
described below. The rebuilt engine and full Keystore-backed encrypted update
lifecycle remain unverified. See the
[Android fixture record](validation/android-native-lifecycle-fixture-20261101.json).

In the shipping additive profile, existing signatures, inheritance and field definitions cannot change, and
removed declarations are rejected. Added fields on existing const, deeply
immutable or native-wrapper classes are unsupported. Constructor-only nonnullable
fields require an initializer or `late` for preexisting objects. These limits
mean unrestricted structural replacement is still incomplete. See the
[SDK protocol and limitations](../dart-sdk/docs/shorebird-structural-patching.md).
Size and performance impact have not been benchmarked.

The separate experimental full-replacement SDK profile has passed local macOS
ARM64 PRODUCT/development host acceptance for const layout and constructor-default
changes, statics, removals, inheritance, GC and child isolates. The real AES
producer, decryption, replacement and payload-free lifecycle also passes, with
wrong-key, altered-ciphertext and expiry rejection. The subsequent local
Flutter/CLI integration also passed signed physical-iPad baseline/update
acceptance. The full-profile fixture validates changed const layouts/defaults,
signatures and statics, removed declarations/library and a new generic widget
before reporting its rendered frame. Wrong-key fresh-base fallback, rejection
persistence after key restoration, fresh-patch recovery and three cold recovery
launches passed. The real CLI native-archive comparison and encrypted publication
paths were used, and the server persisted download/install/failure events.
This working-tree device proof still requires final pinned-source CI, physical
plugin FFI acceptance and broader compatibility/performance coverage.
See [the full-replacement contract and limits](FULL_REPLACEMENT.md).

The real Dart client/Go server smoke covers managed signing, release-scoped
patches, signed update responses, rollback/forward, refresh rotation, and restart
persistence. Rust tests cover response verification and persisted boot identity.
These do not establish protection against complete device-storage rollback.
Managed signing is required for interpreter releases. Native customer signing
requires explicit optional response authentication until the publisher supplies
a response-signing service. Native encrypted delivery uses an app-owned native key callback and authenticated
server response metadata. A real Go-produced encrypted native patch reconstructs
the exact target binary in Rust; wrong-key rejection, retry, and strict boot
identity are tested. Applications must provision derived device keys through
their authenticated service. No secret is derived from a public device ID.
The new Linux/Android native cache mode retains encrypted transport on disk and
reconstructs a verified image into a sealed anonymous descriptor. It requires
the app-owned key provider to be registered before engine initialization,
strict patch verification, required authenticated responses, and the release
public key. Activating this mode retires legacy plaintext cache entries before
publishing usable configuration. Windows now has an experimental source
implementation that reconstructs into private RW memory, consumes the writable
owner, changes the mapping to RO, and authenticates those final bytes before
leasing them to the existing ELF loader. Windows native execution remains a
required CI gate, not a completed capability claim. macOS and legacy callers
retain their existing cache behavior; this is separate from the encrypted iOS
interpreter artifact. No macOS executable-memory entitlement is added.

The expanded native image budget defaults to 512 MiB on capable native engines.
An application can set `native_patch_max_bytes` in its source `shorebird.yaml`
before building a release. CLI and Flutter compilation require a positive
integer no greater than 9223372036854775807; quoted numbers, fractional values,
null and zero are rejected. The compiled release setting controls the budget;
an update cannot raise it. It bounds expanded image bytes, not total process
memory or compressed download size.

November 1 local validation passed 333 updater workspace tests, 15 Flutter
configuration tests, 59 CLI configuration tests, 87 Shorebird engine tests and
three AOT embedder controls. The iOS framework and snapshot build also passed.
The first Linux CI checkpoint passed 87 Shorebird tests and four AOT controls
using a regular descriptor. A subsequent checkpoint passed 88 Shorebird tests
and five AOT controls, including sealed memfd ownership and execution across
two engines, and memory-image lifetime across input release and engine reuse.
This proves those native engine paths, not the complete signed/encrypted
offline GTK app lifecycle. The new Linux lifecycle harness and mandatory CI
gate cover that separate acceptance sequence; their Linux execution remains
pending. Its test provider stores keys in an owned kernel session keyring,
so offline recovery is scoped to cold app processes within one session, not
reboot or login persistence. Android linker/device execution also remains
required. See the [sealed-descriptor CI evidence](validation/native-cache-linux-sealed-ci-20261101.json),
[Linux lifecycle fixture record](validation/linux-native-lifecycle-fixture-20261101.json),
[earlier Linux CI evidence](validation/native-cache-linux-engine-ci-20261101.json), and
[native cache validation record](validation/encrypted-native-cache-host-20261101.json).

The current Linux job built the engine and passed its prepared native-cache
engine tests, then stopped at a keyring-provider compile error: `keyctl_read`
requires a `char*` buffer. The provider now converts its byte buffer explicitly
at that boundary. A local C++ signature check rejects the original call and
accepts the fix; the Python driver suite passes 25 tests with one Linux-only
test skipped. Actual Linux provider compilation and the encrypted app lifecycle
still require the corrected hosted run. See the
[keyutils compilation record](validation/linux-provider-keyutils-compile-20261101.json).

The subsequent Windows memory-source changes pass 341 updater tests and strict
lint checks on macOS. The actual SDK memory-loader target passes a separate
macOS GN/Ninja build and runtime check, plus address/undefined-behavior sanitizer
checks. C++ updater production and test sources pass syntax checks. These
results do not execute the Windows allocator, unwind registration, engine
adapter, or ACG denial path. Mandatory native Windows CI now covers those
paths, including real AOT engine creation after the input lease expires and
after engine recreation, exact executed test inventories, and a separate
process with executable-memory protection enabled. Complete encrypted offline
Windows app acceptance remains pending. See the
[memory-source validation record](validation/verified-native-memory-host-20261101.json).

The current Windows SDK job built the SDK and passed its shared FFI ABI tests,
then failed the native-mapping provenance fixture because a Windows drive path
was passed as a URI. The fixture now preserves the platform file URI. All 14
tests pass locally using the actual current macOS ARM64 CI SDK artifact,
including a Windows path containing spaces and `#`; the corrected Windows
hosted run remains required. See the
[platform URI regression record](validation/windows-sdk-platform-uri-20261101.json).

The preceding Windows engine run built successfully and passed 87 Shorebird
tests, four AOT embedder controls, four Windows adapter controls, 343 updater
tests and the native DPAPI provider contract. Its full app lifecycle stopped
while generating the Windows project. The discarded private log cannot establish
the first failure cause. Independent source inspection found missing local
`sky_engine` staging and a storage URL override absent from direct Flutter calls;
both Windows and Linux preparation paths now supply them. Real offline pub
resolution reproduces the missing-package error and succeeds with the package
staged. Failure reports now retain fixed diagnostic classifications and log
hashes without raw log text or credentials. Corrected hosted app execution is
still required; see the [desktop preparation record](validation/desktop-native-preparation-20261101.json).

The Windows CLI now compares decompressed EXE/DLL bytes with SHA-256 and
rejects ambiguous or malformed native ZIP entries before patch creation.
Its 179 focused tests passed locally, including Dart-only changes and rejection
before artifact creation/upload. Reproducible Windows native builds still need
execution on the Windows runner.

The subsequent Windows guard validates every release ZIP record and its full
namespace before comparison and before release-version or patch-artifact
extraction. Reserved names, ambiguous aliases, file ancestors and special file
types are rejected even when they are not native payloads. The focused Windows
suites passed 121 tests; the expanded combined archive gate passed 686 tests.
These are macOS-hosted CLI checks, including mocked pre-extraction boundaries,
not Windows filesystem execution or exact NTFS normalization. See the
[namespace validation record](validation/windows-namespace-guard-20261101.json).

Linux and Android archive guards now classify native payloads from their actual
contents and canonical paths, compare decompressed SHA-256 hashes, and reject
ambiguous ZIP entries. Their focused suites passed 228 and 87 tests respectively.
The Apple guard passed 242 tests, including real `codesign` and `ditto` fixtures.
It validates Mach-O slices and framework links, uses fixed scratch paths, and
requires native-change confirmation even when Podfile.lock is unchanged or
absent. Generated App images remain Dart payloads for structural updates.
These overlapping suites establish CLI behavior, not device/runtime acceptance;
see the [archive guard record](validation/native-archive-guards-20261101.json).

CLI artifact jobs also require the focused native archive suites before
compilation and packaging. They retain test logs as diagnostic artifacts outside
the release-mirror download patterns. The macOS jobs exercise real signing and
versioned-framework archives; Windows skips the Unix permission/symlink writer
fixture. These gates do not substitute for native runtime or device tests. The combined
local gate passed 573 tests, including the native-mapping and no-publication
regressions; see the [mapping validation record](validation/native-assets-manifest-guard-20261101.json).
The subsequent compiler-derived legacy mapping descriptor passes 27 compiler
checks, 88 Flutter build checks and the expanded 607-test CLI gate. Actual
compiler output also passed through Flutter asset bundling and real CLI archive
comparison. This is tooling validation with synthetic binding fixtures, not a
native app runtime test; see the
[provenance record](validation/native-mapping-provenance-20261101.json).

Release artifact downloads now reject malformed SHA-256 metadata and mismatching
bytes before patching. Failed owned downloads are cleaned up; valid cached
reuse, retry recovery and verified cache-rename fallback remain supported.
The expanded CLI gate passed 762 tests, including production integrity checks
that stop corrupt primary artifacts and supplements before extraction or
publication. These tests use controlled transport bytes on macOS, and the
expected digest remains supplied by the release service. See the
[download integrity record](validation/release-artifact-integrity-20261101.json).

Release supplements now use a separate regular-file extraction path. It validates
the complete portable namespace before writes, then checks each decompressed
file's length and CRC against the validated ZIP metadata. Empty directories and
future safe files remain supported; extraction errors propagate to owned-output
cleanup and stop patch creation. The expanded local gate passed 816 tests.
Generic app archive extraction and framework links remain separate; see the
[supplement validation record](validation/release-supplement-guard-20261101.json).

The new Windows lifecycle fixture uses real CLI release/patch commands, the Go
server's signed encrypted delivery, a test-only DPAPI key provider, and the
released Runner. It checks post-frame rendering and the running patch number,
offline cold boot, missing/wrong-key recovery and signed rollback. Its 27 Python
helper tests pass locally; the native provider probe and full Windows lifecycle
are required CI gates and have not yet executed. Only sanitized results are
uploaded. The cache scan covers final files, not transient writes; it is not a
complete plaintext-write audit. See the
[fixture instructions](../testapps/license_flavor_patch_test/tool/windows_native/README.md)
and [source validation record](validation/windows-native-lifecycle-source-20261101.json).

The older Windows SDK checkpoint also passed 774 FFI ABI golden tests, native
ELF loader permission/cleanup/refusal checks, a real ACG denial control and four
patch API tests. Its engine built, but one platform-specific asset-path test
blocked later app lifecycle gates; the expectation is now corrected. These are
SDK-level results at the recorded source, not latest-source app acceptance. See
[Windows SDK evidence](validation/windows-native-memory-sdk-ci-20261101.json).

The Android fixture now builds release AAB/APK artifacts, verifies the compiled
non-debuggable manifest and installs the baseline on an owned ARM64 emulator.
First startup exposed a VM MTE capability bug before Dart rendering. Exact engine
symbols and a native syscall probe confirmed it; the corrected gate passes five
focused cases on macOS and Android and preserves existing pointer tagging on
unsupported hardware. All six MTE regressions also pass in the rebuilt macOS
ARM64 SDK VM executable. Rebuilt Android engine startup and encrypted update
lifecycle remain pending. See [Android startup evidence](validation/android-mte-startup-20261101.json).

Runtime-created listener and group-bound callbacks also pass the native ARM64
PRODUCT host suite, including nested transitions, static-field isolation,
shared-field access, GC and cleanup. Standalone callback coverage is not
full-app or device acceptance. See the
[callback validation record](validation/product-interpreter-callback-listeners-20261101.json).

The full-profile FFI host suite now also passes on native macOS ARM64 PRODUCT:
encrypted base/update installation against source and from-dill baselines,
mutable plugin layouts and new bindings, callbacks and cleanup, a cleared-FFI-bit
positive, and 60 precise malformed-payload rejection/recovery controls. The
released native binary stays unchanged. A separate frozen-source signed iPad
run now passes encrypted full-profile FFI activation, offline cold launch,
server rollback/rollforward, and wrong-key fallback. See the
[physical FFI record](validation/full-replacement-ffi-ios-20261101.json).
Latest-source device acceptance, broad real-plugin coverage and latest-source CI
remain pending. The host suite is documented in the
[full-profile FFI validation record](validation/full-replacement-ffi-host-20261101.json).

The selected macOS security profile requires Hardened Runtime without
executable-memory or library-validation exceptions. On 2026-11-01 the ARM64
PRODUCT full-replacement FFI **host** passed with a private Developer ID-signed
harness and same-team native images, all with empty entitlements: 12 positive
fresh-group executions, 60 rejection/recovery checks, and callback cleanup.
This is an additional strict signing result; the earlier isolated host harness
used a library-validation exception. Compiler groundwork now accepts explicit
macOS targets without selecting the iOS-only framework marker and binds native
mapping descriptors to the actual compiler OS. See the
[strict macOS host record](validation/macos-hardened-interpreter-host-20261101.json).
That host test still uses iOS envelope metadata. The reviewed Flutter build and
CLI release/patch routes now implement the explicit local ARM64 full profile
with `--target-platform=darwin-arm64` and a matching verified local engine.
Hosted, x64, universal, body-only and additive macOS interpreter configurations
fail closed. The native macOS route remains available independently.

The final isolated release-engine test inventory passed 96 Shorebird, 5
embedder and 4 Cocoa tests with no failures or skips. The
[engine record](validation/macos-interpreter-engine-20261101.json) preserves both
the initial build evidence and the final committed pins, output versions and
XML hashes. These unit controls cover signed-host routing, configuration
lifetime, trusted target options, base recovery and the unchanged native route;
their SDK/install and Cocoa seams do not establish app execution.

`testapps/license_flavor_patch_test/tool/macos_interpreter_e2e.py` provides a
separate private-copy preparation/build plan, loopback API and report observer,
strict same-certificate Developer ID inspection, fresh container report checks,
and scoped Keychain/resource cleanup. Source-check CI is configured to compile
it and run its 18 pure/preparation tests. The app's released C exports and native sources stay
fixed while the full update changes Dart structure, bindings and callbacks.
The expected reports distinguish three baseline checks from all sixteen update
checks. Signing and launching are left to the caller. Final cleanup must be
armed with `cleanup --execute --begin`, followed by native key deletion and
then `cleanup --execute`; an older copied receipt cannot override the latest
native control. Cleanup preserves its validated receipt before removing app
data. A protected, otherwise empty OS container metadata shell is reported
explicitly as incomplete cleanup rather than silently ignored.

The actual strict-signed ARM64 app passes baseline startup and an encrypted
macOS-authenticated structural/FFI update. A fresh launch with both loopback
servers stopped passed all sixteen update checks without changing the signed
bundle. The real CLI release, archive comparison and patch publication also
passed. See the [partial lifecycle record](validation/macos-full-replacement-ffi-20261101.json).

The missing-key test exposed a recovery failure: baseline fallback marks the
candidate as a boot crash and deletes it, preventing offline recovery after
restoring the key. A typed retryable-key fix is in progress; no corrected
recovery or wrong-key acceptance is claimed yet. Authenticated rollback and
rollforward, broader plugins, Intel callbacks, universal full profiles and
notarization remain pending.

## Prerequisites

Linux:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.26.10 (the current server deployment toolchain pin)
- Python and standard build dependencies required by Dart/Flutter
- Flutter Linux desktop build dependencies (`clang`, `cmake`, `ninja`,
  `pkg-config`, GTK development headers)
- A graphical session or `xvfb-run` for headless runtime smoke tests

macOS:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.26.10 (the current server deployment toolchain pin)
- Xcode command line tools
- Full Xcode for iOS simulator/device follow-up

Android packaging now reclaims completed build output after staging all products,
and releases staging before verifying the produced tarball. This addresses a
runner disk exhaustion after a successful arm64 engine build without removing
native export or archive checks. A small packaging fixture passed locally; the
full-size hosted rerun is pending. See the
[disk failure and validation record](validation/android-packaging-disk-20261101.json).

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
| macOS | Legacy native-AOT updater patches; explicit local ARM64 signed-host interpreter full profile | June 25 native snapshot smoke remains historical evidence. The local full-profile Flutter/CLI route is implemented and reviewed, and the isolated engine unit suites pass. Strict-signed macOS app execution and authenticated update/recovery remain pending; hosted, x64 and universal full profiles fail closed. |
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

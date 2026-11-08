# Hardening P0 evidence: binaries, exports, origins and baselines

Stage P0 of [the hardening plan](HARDENING_AND_UPSTREAM_DEPENDENCY_PLAN.md),
recorded 2026-11-09. The baseline is root `ae8a61a` and its fully green
integration run
[42464951579](https://github.com/tony-cloud/shorebird-workspace/actions/runs/42464951579)
(47 jobs succeeded, 4 skipped, final attempt 2). The contracts that build on
this evidence are in [HARDENING_CONTRACTS.md](HARDENING_CONTRACTS.md).

Every value carries an evidence label:

- **CI**: produced by or measured on that run's artifacts.
- **Source**: source inspection, cited as `file:line`.
- **Local**: measured on this host.
- **Device**: measured on a physical device, quoted from existing records.

No number below was estimated.

Machine-readable records:

- `docs/validation/hardening-p0-binary-inventory-20261109.json`: output of
  `scripts/inventory_release_binaries.py` over the 11 engine artifacts.
  Its 103 records cover 40 unique (digest, architecture) binaries. A second run
  produced byte-identical output. Each downloaded tarball matched both its
  `.sha256` sidecar and the run's `open-shorebird-release-manifest.json`. The
  downloads were deleted after inventory.
- `docs/validation/hardening-p0-ci-baseline-20261109.json`: job durations, both
  attempts, all 54 release-manifest artifact sizes and digests, and the Actions
  artifact sizes.

## 1. Shipped native binaries and exports (CI)

`exports` counts defined dynamic exports: ELF `.dynsym`, the Mach-O export trie,
and the PE export directory. Spot-checked locally against `llvm-nm -D`,
`llvm-objdump --exports-trie` and `llvm-readobj --coff-exports` for Linux x64,
iOS, Windows x64 and Android ARM: all counts matched.

| Runtime library | Arch | Bytes | Exports | `shorebird_*` | Rust-mangled updater internals | Embedder API | Debug info |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Android `libflutter.so` (Maven jar, `artifacts.zip`) | arm | 158,301,244 | 83 | 13 | 0 | 65 | **yes** |
| Android `libflutter.so` | arm64 | 174,476,432 | 83 | 13 | 0 | 65 | **yes** |
| Android `libflutter.so` | x64 | 175,570,544 | 83 | 13 | 0 | 65 | **yes** |
| iOS `Flutter.framework/Flutter` | arm64 | 22,291,968 | 11,335 | 30 | 10,945 | 143 | no |
| macOS `FlutterMacOS` | arm64 | 26,920,032 | 11,224 | 30 | 10,938 | 135 | no |
| macOS `FlutterMacOS` | x64 | 28,654,728 | 11,289 | 30 | 11,003 | 135 | no |
| macOS universal `FlutterMacOS` (fat) | arm64+x64 | 55,592,032 | as slices | 30 | as slices | 135 | no |
| Linux `libflutter_linux_gtk.so` | arm64 | 22,630,336 | 11,455 | 30 | 11,137 | 230 | no |
| Linux `libflutter_linux_gtk.so` | x64 | 24,806,184 | 11,494 | 30 | 11,176 | 230 | no |
| Windows `flutter_windows.dll` | arm64 | 21,703,168 | 147 | 20 | 0 | 127 | n/a |
| Windows `flutter_windows.dll` | x64 | 23,896,064 | 147 | 20 | 0 | 127 | n/a |

Findings (CI evidence; they describe engine artifacts, not packaged apps):

1. **Linux, macOS and iOS engines export the updater's Rust internals.**
   About 97% of their roughly 11,000 exports are Rust-mangled updater symbols:
   `_R…` names for `updater::config`, `updater::c_api` and crates such as
   `zstd_safe` and `dashmap`. They also export all 30 `shorebird_*` C entry
   points.
   - Android limits exports through `android_exports.lst` and Windows through
     its export list; neither leaks Rust symbols.
   - This confirms the plan's caution: export hiding is real on Android and
     Windows only. Engine/updater symbol minimization on Apple and Linux is
     P3/P5 work.
2. **No engine exports the `Dart_*` embedding API**, consistent with
   `dart_lib_export_symbols = false`. BoringSSL symbols are not exported
   either: their category is 0 on every platform.
3. **Android release Maven jars carry an unstripped `libflutter.so`.**
   - It is 158–176 MB with `.debug_info`, and the identical bytes appear in
     `symbols.zip`.
   - The CI GN args say `stripped_symbols = false`
     (`android_release_arm64.args.gn` in the artifact).
   - Whether app packaging strips it (AGP's native-library strip with an NDK
     present) was **not measured**. P6 must measure actual APK/AAB contents
     before making any size or reverse-engineering claim.
4. **`shorebird_*` surface differs by platform.**
   - Android exports 13 entry points; Windows exports 20.
   - Apple and Linux export 30, including `shorebird_acquire_boot_source_memory`
     and `shorebird_set_device_id_override`.
   - P3's export minimization should start from these lists, which are recorded
     in the inventory JSON.
5. **Origin classification.** Every runtime library and SDK/engine host tool
   is `custom-toolchain`, and the proxy rule forbids official substitution on
   every one of its mirror routes (`official_substitution: forbidden`). Two
   items are `not-routed`:
   - `analyze_snapshot*`, served from `mirror/shorebird/<engine>/analyze-snapshot-*`;
   - a few top-level staging copies.

   Neither has a Flutter-tool route.
6. Host tools are recorded separately with role `host-tool`. Examples:
   `gen_snapshot` is 4.4–14.5 MB and `font-subset` is 1.2–2.1 MB. Linux host
   `gen_snapshot_*` binaries inside `android-*-release/linux-x64.zip` have no
   NDK note and are classified as Linux.

Artifact tarball sizes (CI, release manifest):

- Android engines: 341–358 MB each.
- Windows engines: 141–150 MB.
- iOS interpreter engine: 48 MB.
- macOS engines: 37–41 MB.
- Linux engines: 35–41 MB.
- Custom Dart SDK tarballs: 233–327 MB.
- CLIs: 11.7–12.4 MB.
- Servers: 6.3–7.1 MB.
- Assembled artifact mirror: 2,144,533,493 B.

The release manifest lists each `mirror-patch-*.zip` entry twice with identical
digests. This is harmless but redundant; P2's catalog generator should dedupe.

## 2. Network origin inventory (Source)

Classes:

- **official**: Flutter, Dart or Google's official artifact or package service.
- **custom-project**: this project's repositories, releases, or operator
  services.
- **third-party-canonical**: the upstream home of an unrelated tool.
- **implicit-Shorebird-hosted**: a Shorebird-owned service.

| Origin | Fetches | Where (default / override) | Class | Notes |
| --- | --- | --- | --- | --- |
| `https://storage.googleapis.com/flutter_infra_release/...` | Unchanged Flutter engine artifacts | `artifact_proxy.dart:133-134` upstream target; `resolve_dependencies.gradle.kts:17` default | official | Reached through the local proxy by default; the Gradle resolve script defaults to it directly. |
| `https://storage.googleapis.com/download.flutter.io` | Official Maven artifacts | via `FLUTTER_STORAGE_BASE_URL` in Gradle | official | Custom `*_release` coordinates must never resolve here (proxy rule). |
| `https://pub.dev` | Hosted Dart packages | `shorebird/pubspec.lock` (100 packages); `PUB_HOSTED_URL` | official | Keep the official default. |
| `https://chrome-infra-packages.appspot.com/dl` | CIPD tools (engine DEPS, flutter_tools) | `flutter_tools/lib/src/cache.dart:576-592`; `flutter/DEPS` CIPD entries | official (Flutter/Chromium infra) | Pinned by DEPS versions. |
| `https://{chromium,dart,flutter,skia,llvm,android,swiftshader}.googlesource.com` | Engine/Dart source deps | `flutter/DEPS:11-17`, `dart-sdk/DEPS:36-48`, `dart-sdk/DEPS:285` (BoringSSL) | official / third-party-canonical | Revision-pinned. |
| `https://github.com/flutter/flutter.git` | Version tag 3.47.8 (`737ba8f`) only when absent | `scripts/prepare_flutter_version_tag.py:9` | official | Revision-checked. |
| `https://github.com/tony-cloud/{flutter,dart-sdk,shorebird-updater,shorebird,shorebird-server}.git` | Fork sources | `.gitmodules`; `flutter/DEPS:21-22`; `scripts/write_gclient.sh:49,72`; CLI `shorebird_env.dart:53`; `shared.sh:14`; `shorebird.ps1:20` | custom-project | Pinned by gitlinks/DEPS (now contract-checked) except the CLI's Flutter clone, which follows `flutter.version`. |
| `https://github.com/tony-cloud/shorebird-workspace/releases/latest/download` | Custom Dart SDK zips for Flutter bootstrap | `flutter/bin/internal/update_dart_sdk.sh:138`, `update_dart_sdk.ps1:50`; `SHOREBIRD_DART_SDK_RELEASE_BASE_URL` | custom-project | **Mutable `latest`.** Plan §7.2 replaces it in P2. CI also compares against it (`open-shorebird-ci.yml:6453-6456`). |
| `http://localhost:8080/{download.flutter.io,artifacts,auth}` and `http://localhost:8080` | Proxy/mirror, custom artifacts, auth, console, update API | CLI `shorebird_env.dart:45,49,68`, `cache.dart:353`; `flutter_tools cache.dart:43`; `FlutterPluginConstants.kt:24`; `aar_init_script.gradle:45`; updater `config.rs:74` | custom-project (operator) | Plain HTTP loopback defaults. Hardened releases need an explicit HTTPS `base_url` (contract §2). |
| `https://github.com/google/bundletool/releases/download/1.18.3/bundletool-all-1.18.3.jar` | bundletool | CLI `shorebird_env.dart:58-59`, pinned sha256 `cache.dart:785-786`; `SHOREBIRD_BUNDLETOOL_URL` | third-party-canonical | **Observed flake** (next subsection). |
| `https://repo.maven.apache.org/maven2/org/json/json/20180813/json-20180813.jar` | JSON jar for the Android variant JVM tests | `open-shorebird-ci.yml:175-178` | third-party-canonical | sha256-checked. |
| `https://chromium.googlesource.com/chromium/tools/depot_tools.git` | depot_tools when the checkout lacks it | `open-shorebird-ci.yml:746-753` | third-party-canonical | In practice skipped: recursive checkout supplies the root `depot_tools` submodule. |
| crates.io index and `dl.google.com`/`maven.google.com` (Gradle `google()`), Maven Central, Go module proxy | Rust crates (257 registry packages, no git sources, `updater/Cargo.lock`), Gradle plugins, Go modules (`shorebird-server/go.sum`) | Cargo/Gradle/Go defaults; `resolve_dependencies.gradle.kts:51-52` | third-party-canonical | Lockfile/checksum pinned. |
| GitHub Actions (`actions/*@v4/v5`, `dart-lang/setup-dart@v1`, `dtolnay/rust-toolchain@stable`, `android-actions/setup-android@v3`), apt, `rustup` | CI toolchains | `open-shorebird-ci.yml:110-161,342`, `:351`, `:1788` | third-party-canonical | **Mutable tags** (`@v4`, `@stable`, `DART_SDK_CHANNEL: stable`) rather than SHAs. Recorded for P2/CI hardening. |
| `https://api.github.com` | CI artifact/transport queries | `scripts/verify_hosted_full_sdk_build.sh:45`, `macos_app_lifecycle_consumer.py:187`, `macos_x64_proof_transport.py:413` | custom-project (GitHub-hosted) | Authenticated CI use only. |
| `https://{api,auth,console}-dev.shorebird.dev` | Shorebird development services | CLI `shorebird_env.dart:62,65,71` | **implicit-Shorebird-hosted** | Selected only by the hidden `--dev` flag. No production default names a Shorebird host. Plan §7.3: remove or require explicit opt-in (P2). |

No production default in the CLI, launchers, Flutter tool, Gradle plugin,
updater or CI names a `shorebird.dev` host. `scripts/verify_open_infrastructure_defaults.sh`
already forbids the production ones. Remaining `shorebird.dev` text is
documentation or license links. One example is cached `LICENSE.*.md` files under
the untracked `flutter/bin/cache`.

### Observed flake: bundletool 504 (CI)

Attempt 1 of run 42464951579 failed in **Build Linux engine artifacts →
Run Linux native V2 variant lifecycle** at 10:01:51Z:

`shorebird release` reported `CacheUpdateFailure: Failed to download bundletool.jar from <url> 504 Gateway Time-out`.

The URL is redacted in the log. CI does not set `SHOREBIRD_BUNDLETOOL_URL`,
so the origin is the GitHub release default above. Two causes combine:

- `Cache.updateAll` (`shorebird_cli/lib/src/cache.dart:101-125`) fetches every
  required artifact, including bundletool, even for a Linux desktop release.
- `updateAll` retries only 3 times with a 200 ms doubling backoff, so a short
  GitHub release-CDN outage fails the job.

P2 owns scoping artifact downloads to the target platform and choosing a cached
or mirrored bundletool origin (checksum already pinned).

## 3. Lifecycle and performance baselines

### CI (run 42464951579)

| Measure | Value |
| --- | --- |
| Attempt 1 wall time (first job start → last job end) | 332.1 min; failed only on the bundletool 504 |
| Final wall time including the rerun | 413.5 min |
| Sum of job minutes, final state | 2,813.4 runner-minutes |
| Longest jobs | Custom Dart SDK macOS x64 230.3 min; Windows ARM64 engine 229.8; Dart SDK Windows ARM64 199.8; Windows x64 engine 187.5; macOS x64 engine 182.0; macOS x64 engine tests 130–154 each |
| Linux engine artifacts job (includes native V1/V2 lifecycles) | x64 62.8 min (rerun); ARM64 108.3 min |
| Android engine jobs | arm64 53.7, arm 42.8, x64 34.3 min |
| iOS engine artifacts | 37.3 min |
| Source checks (includes "Verify CI scripts") | 10.7 min |

Per-job values are in the CI baseline JSON.

### Local

The full "Verify CI scripts" step took 215 s on this Apple Silicon host
(`bash -e` of the extracted step, with the P0 pin contracts but before the new
test suites).

### Device and host (quoted)

These come from existing records and were not re-measured in P0:

- **iPad Pro M4 interpreter/AOT ratios and frame build times**:
  `docs/PATCH_PERFORMANCE.md:20-29,76-84,134-146,334-342`. Examples: the
  pre-decoded interpreter runs simulated code at 2.6–21.5× native; V0 frame
  build median is 0.72 ms.
- **Host M-series mixed-mode ratios**: `docs/ios-linker/RUNTIME.md:577-585,708-716,1111-1121`.
- **Patch download bytes**: iPad signed P0/P1/P2 are 41,242 / 41,477 / 41,605 B
  (`PATCH_PERFORMANCE.md:284-286`). Flutter-app variants are 42–94 KB
  (`ios-linker/COMPILER_LINKER.md:732-739`).
- **App binary sizes**: iPad `App.framework/App` V0 is 2,387,872 B and V1 is
  44,969,968 B (`PATCH_PERFORMANCE.md:86-93`).
- **Full-replacement recovery relaunch to validated report**: 1.507 s; later
  launches 1.11–1.46 s (`docs/FULL_REPLACEMENT.md:380-383`). That document says
  these are not startup benchmarks.

### Gaps later phases must measure

There is no existing measurement for any of the following:

- cold-start p50/p95 for any target;
- RSS or peak memory;
- interpreter CPU utilization, as opposed to time ratios;
- frame time on the linked mixed-mode route (0 frames were recorded);
- patch generation time;
- verify/install time;
- per-update network latency;
- packaged app size per platform, including whether the Android release strips
  `libflutter.so`;
- CI cost per profile.

The plan's proposed diversification budgets (≤5% p95 startup/CPU, ≤10% peak
RSS) cannot be calibrated until P6 records control-profile values on each
advertised target.

One known inconsistency in the quoted sources: `docs/ios-linker/RUNTIME.md:870`
and `:933` disagree by 32 bytes on the base data size (1,078,368 vs 1,078,336).
It is left for the iOS route owner.

## 4. Open questions for P1/P2

1. **P1.** Where does the compiled policy enter the updater?
   - Option A: a build-time environment variable consumed by `option_env!`.
   - Option B: a generated Rust file from the engine GN arg.

   The contract fixes the names (`shorebird_security_profile`,
   `shorebird_toolchain_id`) and semantics, not the mechanism.
2. **P1.** Hardened `update_origin_policy: https-only` versus today's
   `http://localhost:8080` defaults. The CLI must refuse to package a hardened
   release whose `base_url` is HTTP, unless `development_exceptions` is set.
3. **P2.** Choose the operator's immutable custom artifact origin and catalog
   signer (plan §10). Until then the catalog example uses a placeholder
   `base_url`.
4. **P2.** `update_dart_sdk.{sh,ps1}` and the CI comparison step still use
   mutable `releases/latest/download`.
5. **P2.** Platform-scoped CLI cache downloads (bundletool for Linux desktop
   releases) and longer backoff.
6. **P3/P5.** Export minimization on Apple and Linux, and the Android
   `stripped_symbols = false` decision. Both change shipped engine bytes and
   need native-host validation, including the Windows VM if Windows link flags
   change.

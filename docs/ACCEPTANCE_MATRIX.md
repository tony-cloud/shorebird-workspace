# Replacement acceptance matrix

This is the canonical acceptance matrix for the full replacement goal. Historical
records linked here remain evidence for their recorded revisions, not current
release certification. **The goal is incomplete.** A build, host harness, mocked
integration, or emulated run never substitutes for native app/device acceptance.

## Required end state

For each supported app target, retain one installed app and persistent data while
proving release → publish → download → activate, partial method replacement,
method addition, and repeated free → pro → enterprise → pro → free switching.
Variant selection must be independent of immutable content/patch numbering and
must preserve authenticated selection, monotonic replay protection, and explicit
rollback authorization. Test offline restart, ciphertext/signature tampering,
missing/wrong keys, restoration, rollback and recovery at every relevant boundary.

All compiler/runtime, engine, CLI, updater, server and artifact-production inputs
must be open source and pinned. Record actual CPU architecture, signing settings,
FFI/callback/loader behavior, key-store behavior, binary/package hashes, CI run,
and test outputs. Host tools and server binaries are separate from app targets.

## App target matrix

`Historical` means a real run at an older recorded revision; it does not satisfy
current acceptance. `Build` and `unit` deliberately do not mean app lifecycle.
All targets below require the full lifecycle and variant sequence above.

| App target | Release/publication/activation | Offline and key/tamper recovery | Rollback / rollforward | Native FFI, loader and key storage | Variant round trip preserving data | Current acceptance |
| --- | --- | --- | --- | --- | --- | --- |
| Android ARM64 | CI-1 engine build; corrected app lifecycle unproven | Unproven at current revisions | Unproven | MTE correction exists; native ARM64 app rerun required | Not implemented/proven | Incomplete |
| Android ARMv7 | CI-1 engine build | Unproven | Unproven | App runtime unproven | Not implemented/proven | Incomplete |
| Android x64 | CI-1 engine build | Unproven | Unproven | App runtime unproven | Not implemented/proven | Incomplete |
| iOS ARM64, physical device, no JIT/debugger | IOS-1 historical encrypted structural/FFI lifecycle | IOS-1 offline and wrong-key fallback; current recovery unproven | IOS-1 historical both directions | IOS-1 16 update FFI checks; released native exports unchanged | Only free→pro and rollback shown; no independent enterprise selector | Incomplete |
| macOS ARM64, hardened, no executable-memory/library-validation exceptions | MAC-2 signed release→publish→download→cold activate passed | MAC-2 offline, missing/wrong-key fallback+restoration and isolated ciphertext/signature rejection passed | MAC-2 authenticated rollback and rollforward passed | MAC-2 16 update FFI checks, unchanged same-team signed bundle, native Keychain | Only free→pro and rollback; v2 selector/data round trip pending | Incomplete |
| macOS x64, same hardened policy | CI-1 native engine build; full interpreter profile gated | Unproven | Unproven | Architecture-correct interpreted FFI callbacks missing | Not implemented/proven | Incomplete |
| Windows x64 | CI-1 engine build; app preparation FAILED before lifecycle | Native cache unit controls passed; app lifecycle unproven | App lifecycle unproven | DPAPI contract passed; engine 87+4+4 tests passed | Not implemented/proven | Incomplete |
| Windows ARM64, native execution | No app-target pipeline acceptance | Unproven | Unproven | Driver currently rejects ARM64; architecture/FFI/loader/DPAPI/package work required | Not implemented/proven | Incomplete |
| Linux x64 | CI-1 engine build; app preparation FAILED before lifecycle | Native cache unit controls passed; app lifecycle unproven | App lifecycle unproven | Engine 88+5 tests passed; keyring provider compiled only | Not implemented/proven | Incomplete |
| Linux ARM64, native execution | No app-target pipeline acceptance | Unproven | Unproven | Driver currently rejects ARM64; architecture/FFI/loader/keyring/package work required | Not implemented/proven | Incomplete |

macOS universal packaging requires both constituent native runtime results; an
ARM64-only run does not establish x64 support. iOS simulator results do not
substitute for physical no-JIT ARM64 execution. ARM64 server artifacts do not
establish ARM64 Flutter app or host-tool support.

## Cross-component requirements

| Requirement | Evidence / present gap | Completion evidence required |
| --- | --- | --- |
| Fully open compiler/runtime and engine | Public full-interpreter and native routes implemented in forks; architecture and plugin gaps remain | Rebuild exact released toolchain from public source; no proprietary compiler/runtime dependency |
| Partial method replacement and method addition | SDK/host and earlier device records exist; not uniformly accepted per app target | Each target executes changed existing methods and newly added callable methods, including FFI interactions |
| Independent variant selection | Server selects latest promoted patch; updater content ordering/state inhibits reselecting already installed content | Signed selector separate from patch number, durable anti-replay state, same-base payloads, all directed variant transitions |
| Signature verification and encryption | IOS-1/MAC-1 positive encrypted paths; updater/SDK rejection suites | Negative tamper, wrong-context, replay, expired authorization and wrong-key controls on native apps, with usable recovery |
| Same installed app and preserved data | Prior app bundles remained unchanged during updates; full variant data sentinel not tested | Stable installed bundle/package identity and persistent sentinel across every transition, rollback and restart |
| Reproducible artifacts | Pinned manifests/checksum tooling and CI artifacts exist; CI-1 mirror aggregate failed with lifecycle jobs | Clean-source build manifest, exact component pins and package hashes; repeat-build equivalence under documented signing/timestamp normalization |
| Self-hosted server and CLI lifecycle | Real Go server/CLI used in IOS-1 and MAC-1 | Current pinned server/CLI prove every matrix flow without hosted proprietary services |
| Recovery and rollback | MAC-1 exposed permanent retirement of retryable key failure | Review and test typed key-failure deferral, non-destructive I/O handling, concurrent engine startup, then fresh signed app proof |
| CI and packaging | CI-1 completed with 26 successes and two lifecycle-preparation failures | Every required job on the final integration revision passes, including ARM64 app runtime gates |

## Host tools and services — separate evidence

| Artifact category | Declared/current evidence | Remaining qualification |
| --- | --- | --- |
| CLI/patch tools: Linux x64, Windows x64, macOS x64/ARM64 | CI-1 artifact jobs passed | Final-revision packaging and actual app lifecycle consumption |
| Custom SDKs: Linux x64, Windows x64, macOS x64/ARM64 | CI-1 SDK jobs passed | Final typed-status changes, exported API checks and matching engine builds |
| Android host snapshotters on supported desktop hosts | CI-1 jobs passed | Final pins and per-app architecture consumption |
| Windows/Linux ARM64 host tools | Not established by x64 builds or server ARM64 artifacts | Explicit native/cross-host support contract, package/run tests; report any emulation separately |
| Server: Linux x64/ARM64, macOS x64/ARM64, Windows x64 | CI-1 artifact jobs passed | Final protocol migration/variant-selection tests and native deployment claims only where executed |

## Evidence ledger and exact revisions

- **CI-2 (running):** [run 41314411707](https://github.com/tony-cloud/shorebird-workspace/actions/runs/41314411707) tests root `0bf60d78bb006567787bcb7098be977f8a020603` and RECOVERY-UNIT component pins below. Source checks passed; platform builds/lifecycles are pending. This checkpoint includes the Linux/Windows fixture engine-stamp and isolated Go-cache fixes. It does not include the in-progress v2 or native ARM64 routing changes.
- **CI-1:** [run 41269768600](https://github.com/tony-cloud/shorebird-workspace/actions/runs/41269768600), completed failure: 26 successful jobs; Linux job 124741175665 and Windows job 124741175675 failed before app lifecycle. Root `ecc3e605f54cc0f3228752584cd74976a8b6ccdb`; SDK `928a00195899056e8283df710a4028e2c20e291d`; Flutter `c9400c1bb031a3c29f668c7f51eb30d42c369870`; engine identity `e15d73018ba8c6c9fdbc3a20faa7fbb2b877b3d8`; CLI `2f1f5a164e2533e304e9402cdfa226bc96c00bd5`; updater `bb4224303d18baaacd1c2316e24336813517142d`; server `8cc3148fdcb489b20faa0a544d7a6690b6536da9`. [Integration record](validation/platform-fixture-integration-20261101.json).
- **IOS-1:** [frozen physical-device record](validation/full-replacement-ffi-ios-20261101.json). SDK `bdc30207c93e422dfd56d4260fa84c522cbf95b1`; Flutter `41865d0ebfc6bb3814f2e796ceafb141993d4e6e`; engine identity `0b65b0bea03ad4421928f0c0b7b8bebf97632a6b`; CLI `40ce949eb1b2dc5473e178c05362a0167db52f78`; updater `ea85ce31bd9ce57ac7518d0a8035a0e78518cd87`. The record does not independently pin every server/fixture source, so it is not a complete reproducibility proof.
- **MAC-1:** [partial signed-app record](validation/macos-full-replacement-ffi-20261101.json), stored in root checkpoint `4b6bb6f892e35e27802212a230df979a84f792f3`. SDK `4aaa43dc3eb3ea8343ddbafc34153991850c708f`; Flutter `453a9982fa84f70908856d5b3595339f4dd5fc40`; engine identity `000c71b30b60503314d4ce3d5fa8b8b09bbd2cef`; updater `bb4224303d18baaacd1c2316e24336813517142d`; baseline CLI `d846e7eb86b4fe8838a393173848cb5683b13bb8`, patch CLI `3758b0068a4c5fe07b980ee56959b03a7d9e389b`. Private isolation changes and artifact hashes are recorded; temporary build/session directories were absent when work resumed, so a fresh run is required.
- **MAC-2:** [signed recovery lifecycle](validation/macos-key-recovery-lifecycle-20261101.json) uses exactly RECOVERY-UNIT pins and fixture root `0bf60d78bb006567787bcb7098be977f8a020603`. Thirteen cold-process reports cover initial/download BASE, offline UPDATE, missing/wrong-key BASE and restored UPDATE, rollback BASE, rollforward UPDATE, and isolated ciphertext/signature rejection. Retained ciphertext/authentication matched across key refusals; released bundle stayed unchanged. Key, app payload, bootstrap credentials and servers cleaned; any OS-owned metadata shell is recorded explicitly. This supersedes MAC-1 key-recovery failure only, not missing v2/x64/data acceptance.
- **RECOVERY-UNIT:** SDK `b17e766a7f25f22f31dcaeb313c55d40945a8c8a`, Flutter `dfd36cfc65621872864de714ba3948c7c2f5596c`, engine identity `29c8889bc81cec630a83b1e05416c0d588e6b2db`, updater `92a7015bca749842de87131e354bfda626390128`, CLI `ee0480819e9d61ddf84389d3cb6400e60fc07b1e`, server `8cc3148fdcb489b20faa0a544d7a6690b6536da9`. [PRODUCT SDK crypto/admission tests](validation/sdk-typed-key-failure-product-20261101.json), [351 updater tests](validation/updater-key-recovery-20261101.json), and [105 engine + 5 embedder + 4 Cocoa native tests](validation/macos-key-recovery-engine-20261101.json) passed. Engine tests ran after the final pinned build completed. These prove typed refusal and ordered bookkeeping controls, not signed app recovery; fresh app evidence remains required.

## Native-code limits

Interpreter updates replace Dart code interpreted by already signed native code.
The iOS and hardened macOS fixtures use native functions and callback entry points
shipped in the original app; a Dart patch cannot add an unshipped native plugin,
change an unavailable native ABI, or introduce a new signed native executable.
Desktop/Android native executable patch routes need their own loader, signing,
architecture, encrypted-cache and key-store acceptance. Never generalize a Dart
FFI test with pre-shipped exports into support for downloading arbitrary new
native dependencies. Requiring a store/app release for such native changes must
be explicit in the CLI and documented support contract.

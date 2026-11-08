# Updater protection, optional VM diversification, and official Flutter sources

Date: 2026-11-08 (Asia/Shanghai). Status: proposed plan only; no implementation
has been started. Source inspection and upstream documentation research informed
this plan. No new build, security test, runtime test, or device validation was
performed for it.

## 1. Intended outcome and boundaries

Strengthen the updater against replacement and modification; provide optional
release-scoped Dart VM/bytecode diversification to disrupt generic extraction
scripts; harden the actual BoringSSL and updater network boundaries; and use
official Flutter/Dart sources wherever compatible with this project's patching
features.

Interpret “official flutter.io” as the official Flutter/Dart ecosystem: official
source repositories, Flutter release artifacts, Flutter's Android Maven
repository, and pub.dev. These are different services, not one interchangeable
mirror URL. Interpret “BoringSSL defense” as resistance to common TLS-bypass and
key/plaintext-extraction hooks, while retaining standard TLS and cryptography.

The security objective is measurable resistance and reliable rejection of
unauthorized changes. An attacker controlling the process, OS, or release-signing
infrastructure can bypass in-process checks and observe runtime plaintext.
Self-hashing, opcode diversification, and symbol changes cannot promise permanent
secrecy. This follows the defense-in-depth role described by
[OWASP MASVS resilience](https://mas.owasp.org/MASVS/11-MASVS-RESILIENCE/).

Scope includes the root workspace, Dart/Flutter forks, CLI/tooling, Rust updater,
self-hosted server, build artifacts, and supported native platforms. Preserve
native AOT, interpreter body/structural/full profiles, cold-start selection,
licensed variants, rollback/recovery, and offline behavior. Web patching is not
added by this work. Updater/engine replacement requires a new native application
release; a Dart patch cannot retrofit its own native trust anchor.

## 2. Current baseline: retain what already works

Inspected workspace HEAD: `c1162ea44094dbf1b6b1cd667c2c13f097eadcca`.

| Checkout | Inspected commit |
| --- | --- |
| Dart SDK | `c3a98bfdfd296ad0f778e0a545ce1d3a18259068` |
| Flutter/engine | `11ebb7f9029566f885e8b16351303df5ac736715` |
| CLI and patch tools | `08e1ebed6f41165503768eedb940a07935c2c5eb` |
| Self-hosted server | `3479ee0445a4db67eb223d8abbbaaf65202a909b` |
| Updater | `b886d638d785eacda6c309e94c5976e0587a2401` |

Current source, rather than older document status labels, is the planning
baseline. For example, `docs/VARIANT_SELECTION_PROTOCOL.md` still opens with a
pre-implementation status although the inspected updater/server implement v2.

| Area | Existing evidence | Planning implication |
| --- | --- | --- |
| Signed selections | `updater/library/src/config.rs:303`, `selection_v2.rs:528`, and `response_auth.rs:176` enforce v2 scope, strict boot verification, signed responses, pinned public key, and artifact/selection binding. | Extend these invariants; do not replace them with obfuscation. |
| Replay and recovery | `selection_v2.rs:640` and `:1393` persist accepted epoch/digest authority before content selection; boot rechecks it. | Preserve legitimate lower patch-number selection at a newer authorization epoch, crash recovery, and signed free-base fallback. |
| Native patch sources | `updater/library/src/native_cache.rs` seals Linux/Android memfd sources and verifies Windows read-only anonymous buffers. | These protect patch input, not the updater executable itself. Preserve them. |
| Legacy policy | `updater/library/src/cache/signing.rs:25` accepts unsigned content when no key is configured; `yaml.rs` retains optional legacy modes. | New hardened releases must explicitly exclude accidental legacy/unsigned operation. |
| Signing authority | `shorebird-server/internal/api/handlers/selection_device.go:211`, `selection.go:81`, and `signing.go:116` use the managed app signing key for online selections and artifacts. | Split roles and add versioned rotation/recovery; encrypted database keys still become available to the API process. |
| TLS and crypto | `updater/library/Cargo.toml:48` uses ureq/rustls and ring; `dart-sdk/runtime/bin/secure_socket_filter.cc` uses native BoringSSL. | BoringSSL changes do not harden Rust updater TLS. Audit both stacks. |
| Native plaintext boundary | `dart-sdk/runtime/vm/dart_api_impl.cc:8359` obtains patch key bytes; `:8395` calls BoringSSL AEAD decryption. | Opcode randomization does not protect these native key/plaintext boundaries. |
| VM format | `dart-sdk/pkg/dart2bytecode/lib/dbc.dart`, `assembler.dart`, and VM `constants_kbc.h`/`interpreter.cc` use fixed opcode identities with compact/wide assumptions. | Diversification needs a coherent compiler, format, validator, interpreter, and tool change. |
| Export reduction | `flutter/engine/src/flutter/shell/platform/android/android_exports.lst:5` already limits exports and hides everything else. | Measure actual binaries before claiming symbol hiding is a new protection. |
| Artifact routing | `shorebird/packages/artifact_proxy/lib/src/artifact_proxy.dart` routes unchanged files to Google storage and custom artifacts to the open mirror; missing custom runtime/SDK artifacts are rejected. | Keep that compatibility guard while removing unnecessary proxy dependence. |
| SDK bootstrap | Flutter `bin/internal/update_dart_sdk.sh:134` and `.ps1:42` can fetch this workspace's mutable `releases/latest/download` assets. | Bind downloads to immutable toolchain identity and authenticated digests. |
| Package sourcing | `shorebird/pubspec.lock` already uses pub.dev; workspace packages use local paths. | Preserve official Pub defaults; this is mainly an SDK/engine routing issue, not wholesale Pub migration. |

Before implementation, reconcile build inputs: `flutter/DEPS:23` pins updater
`9d79a58bf49014af9f358cfa0d30d38d881dba2b`, while the inspected root updater is
`b886d638...`. Local engine Dart/updater directories are symlinks to root
checkouts. A clean gclient checkout can therefore resolve different updater
bytes. This is a reproducibility finding, not evidence that a specific shipped
binary is vulnerable.

## 3. Threat model and release policy

| Threat | Primary control | Residual limit |
| --- | --- | --- |
| Modified or substituted download, cache, metadata, or mirror | Signed identity-bound metadata, artifact digests/signatures, AEAD, bounded parsing, verification before use | A compromised trusted signer can authorize malicious content. |
| Replay, stale grant, downgrade, or copied local state | Existing epoch/digest rules, explicit profile binding, installation-bound provisioning, protected state where available | Fully restored device state and attacker-controlled offline time need stronger platform/server anchors. |
| Repacked app or replaced updater/engine | Platform package identity, signed build manifest, immutable compiled policy, startup integrity gate | In-process checks alone are bypassable on a compromised OS/process. |
| Generic VM extraction or fixed-offset hooks | Optional opcode/metadata/native-layout diversification and minimized release exports | An adapted tool can learn the format; executable code and plaintext remain observable. |
| TLS verification hooks or intercepted sessions | Correct TLS on each stack, optional first-party pinning, independently signed update authority | Pinning can also be hooked. It does not replace signatures or authenticated key provisioning. |
| Compromised online API/mirror | Offline artifact authority separated from constrained online selection authority; authenticated immutable build manifests | An online selection signer can still abuse its allowed selection authority. |

Proposed profiles, with final flag spelling deferred to implementation:

| Profile | Policy |
| --- | --- |
| Existing/legacy | Preserve existing releases and compatibility explicitly. Do not silently upgrade their wire format or security promises. |
| Hardened | Default for newly opted-in native releases: mandatory signatures/strict boot verification, explicit HTTPS production origins, immutable policy/build identity, platform integrity controls, existing encrypted delivery where applicable. |
| Hardened + diversified VM | Opt-in at native-release build time; adds a release-specific bytecode profile and measured native boundary diversification. All later patches must match. |

Debug/test exceptions must be visibly development-only and rejected by hardened
production packaging. Remote configuration and Dart code must not lower the
compiled verification/profile floor. Optional diversification being off must
never turn off core authenticity checks.

## 4. Updater self-protection and trust lifecycle

### 4.1 Immutable trust inputs and update admission

1. Compile the minimum release policy and trust roots into the native host. Bind
   app/package identity, original release/base digest, platform/architecture,
   runtime profile, host ABI, updater/engine build identity, and diversification
   profile. Treat bundled YAML as a consistency input, not the sole authority for
   security settings.
2. Extend checked initialization in the engine and Rust updater. Require all
   engines/add-to-app instances to agree with the latched process identity.
   Reject malformed configuration and attempts to retry through a weaker legacy
   entry point before keys are requested or a patch is activated.
3. Preserve authentication of control responses, no-update/base selections, and
   reconstructed artifacts. Verify downloaded transport before exposing it to
   expensive reconstruction where the signed format supplies a transport digest;
   always authenticate the final executable/interpreter artifact independently.
4. Audit HTTP schemes, cross-origin redirects, credential forwarding, size and
   decompression bounds, timeouts, and partial downloads. Use HTTPS for production
   origins; allow loopback HTTP only in an explicit development profile.
5. Retain atomic journal transitions and immutable native sources. Close any
   remaining path/symlink and verify-then-reopen races through handle-based access,
   private directories, bounded files, and verification of the actual selected
   bytes. A passing hash of one file must not authorize a later substituted file.

### 4.2 Protect the updater binary and engine

Generate a signed build manifest for the updater/engine and policy-bearing
assets. Its trust root must be anchored in the platform-signed installation or
another separately authenticated deployment root, not in the same writable cache
as the files it measures. Avoid circular whole-binary hashes: define precisely
which immutable sections are measured, how embedded measurement records and
signature regions are excluded, and how relocations/platform transformations are
handled. Validate the final distributed artifact, not only a pre-signing build.

Add an early native integrity gate before key-provider registration/use and patch
activation. Check the actual loaded module identity as well as package files.
Evaluate selected executable-section measurements after load as an additional
signal; whole-module memory hashing will need relocation-aware handling. A
one-time startup check does not prevent later runtime hooks.

| Platform | Proposed adapter and required proof |
| --- | --- |
| Android | Validate installed package/signer identity with supported certificate rotation; audit native library load paths and extracted/APK-backed libraries. Preserve sealed patch input. Optional server-verified integrity attestation must be replaceable and must not become a required Google service dependency. |
| iOS/macOS | Use supported platform signing/identity facilities and bundle/resource sealing; test actual signed distribution builds and entitlements. Keep the existing interpreter route where native patch loading is unsupported. Do not introduce JIT or self-modifying executable memory for randomization. |
| Windows | Validate signed package/module identity where configured; harden DLL search/loading and writable directories; preserve verified-memory patch sources. An adjacent unsigned hash file is not a trust anchor. |
| Linux | Use signed deployment manifests/packages anchored outside writable patch storage, restrictive installation ownership, and explicit module paths. Preserve sealed FD loading. Record that protection depends on the deployment/OS trust model. |

Audit existing PIE/ASLR, W^X/DEP, RELRO, stack protection, control-flow protection
and available hardware mitigations, then enable missing supported protections
with measured compatibility. Keep required embedding/FFI exports; remove accidental
security internals, debug service exposure, secrets in logs, and release test
hooks. Optional debugger/hook/module anomaly detection is a risk signal, not a
single definitive verdict. Start heuristic detection in observe-only mode to
measure false positives.

Failure policy must distinguish cases. Invalid patch content falls back through
the existing authorized bundled-base/recovery path. A failed updater/engine
identity check blocks dynamic patch and protected-key use and requests native
app repair; do not call execution through a suspected engine “verified safe.”
Avoid crash loops, destructive cache wipes, and deletion of application data.
Return typed errors suitable for support without exposing keys or bearer URLs.

### 4.3 Separate signing roles and recover from compromise

Introduce a versioned delegation design with an offline release root, an
offline/CI artifact signer, and a narrowly scoped online selection signer. Use
established signature algorithms and a reviewed metadata design; evaluate
[TUF's role separation, expiration, and root rotation](https://theupdateframework.github.io/specification/latest/)
for build-artifact distribution rather than inventing new cryptography. Do not
claim TUF compliance unless the selected implementation and complete behavior
meet that specification.

The online signer may select approved artifacts for an app/release/install; it
must not mint arbitrary executable artifacts. Store private signing operations
behind a KMS/HSM interface where deployed, without making one cloud provider
mandatory. Add key IDs, explicit scopes, delegation versions, validity windows,
revocation, overlap for rotation, and recovery after online-key compromise.
Separate artifact, selection, transport-encryption, and diversification material.

Have the artifact signer authenticate a domain-separated artifact statement
binding the final digest to its app/release/base, target/profile/host ABI,
format version, key identity, and immutable content expiry. A signature over a
bare hash alone does not constrain a compromised online signer from reassigning
valid bytes to another context. The updater must verify both the artifact
statement and the online selection and require their contexts to agree.

This requires a new protocol capability/version and coordinated CLI, server,
engine, updater, and fixture changes: v2 currently uses a shared managed key and
strict schemas. Do not append unknown fields and assume older clients accept
them. Existing releases continue with their original trust rules; new native
releases opt into the new roots and verification contract.

### 4.4 Installation keys and offline guarantees

Keep entitlement enforcement server-side. Authenticate installation/account key
requests with fresh challenges and verified current grants; a copied `client_id`
is not authentication. Prefer per-install asymmetric identity and platform
protected storage where supported. Audit existing app adapters before adding a
new provider API. Never ship publisher private keys or derive a secret solely
from public device identifiers or the diversification seed.

Preserve current offline lease/perpetual-grant semantics; do not silently invent
new paid expiry. Add protected epoch/time anchors only where the platform can
support and recover them. Define reinstall, backup/restore, device migration, key
loss, and unavailable-keystore behavior. Full local rollback cannot be ruled out
by an ordinary local journal, and an offline perpetual grant cannot be instantly
revoked. Sensitive deployments can choose bounded leases explicitly.

## 5. Optional VM and opcode diversification

### 5.1 Choose the correct execution layer

Start with the project's Dart bytecode interpreter. Native AOT uses real CPU
instructions; ARM64/x64 opcodes cannot be renumbered arbitrarily. The mixed-mode
ARM64 simulation/fast-interpreter path is a separate instruction representation
and needs a separate feasibility study, not an automatic application of the Dart
bytecode map. Stock Flutter artifacts cannot supply a release-specific VM map.

Ordinary Flutter name obfuscation is useful but does not encrypt code or stop
reverse engineering, as explained in
[Flutter's obfuscation documentation](https://docs.flutter.dev/deployment/obfuscate).
The current interpreter release/patch CLI rejects `--obfuscate`; retain that
guard until symbol/name identity across baseline and patches is supported and
tested. A new diversification option must not imply ordinary obfuscation already
works for interpreter/full-replacement profiles.

### 5.2 Release-scoped profile contract

Generate a random diversification seed once per application/native-release
lineage, then deterministically derive versioned subprofiles for bytecode and
native layout. Pin tool versions and target inputs. Keep the seed/build recipe
and private symbol/decode maps in controlled release records for patch builds,
reproduction, incident response, and crash symbolication. Seeds are not encryption
keys; their confidentiality is not an authenticity boundary. The shipped runtime
necessarily contains enough information to interpret its code.

Record a nonsecret `hardening_profile_id` and ABI/format version in the signed
release metadata, compiled host, encrypted/authenticated patch header, compiler
outputs, and custom artifact manifest. Incorporate them into cache and host-ABI
identities. All baseline/patch variants for a native release must use the same
profile. Include architecture in native layout identity while documenting whether
the bytecode map is shared across its architecture builds.

Generate the profile before producing artifacts. Bind the final artifact digests
in an outer signed manifest to avoid a self-referential profile hash. Changing
seed/profile requires a new native release and a fresh matched baseline. Do not
randomize independently on each patch build or process start in the first version.
Lost seed/recipe recovery means restoring the exact records or issuing a native
release, never silently selecting a new map.

### 5.3 Opcode implementation design

Use a generated semantic-opcode-to-wire-opcode bijection and inverse metadata
from one schema. Retain stable internal semantic identities for compiler logic
and validation. Decide explicitly whether compact/wide pairs stay adjacent or
are described by generated width tables; do not blindly shuffle enum values.

| Consumer | Required coordinated change |
| --- | --- |
| `dart-sdk/pkg/dart2bytecode/lib/dbc.dart` and `assembler.dart` | Encode mapped wire values; remove or deliberately preserve current enum-index, `+1`, and parity assumptions. |
| `declarations.dart` / serialization | Version the DBC3-derived format and bind profile identity for bundled base and patch components. |
| `disassembler.dart`, dump tools, private diagnostics | Decode with the matching profile and preserve maintainers' debugging ability. |
| `dart-sdk/runtime/vm/constants_kbc.h` and `bytecode_reader.cc` | Decode before semantic validation; preserve forbidden-opcode categories, width/operand checks, bounds, control flow, and metadata/cache validation. |
| `dart-sdk/runtime/vm/interpreter.cc` | Generate dispatch indexed by mapped wire bytes; keep invalid entries trapping. Avoid materializing a persistent canonical bytecode copy solely for execution. |
| Patch/full-replacement loader and host tools | Require exact baseline/profile/host identity before VM publication or execution, including recovery and bundled-base loading. |

Authenticate the envelope and compare release/profile/format before interpreting
payload instructions. Reject absent, unknown, canonical-in-diversified, or
mismatched profiles deterministically. Never auto-detect several maps until one
appears to run. Audit every raw opcode read, including VM-internal operations,
range comparisons, debugger/disassembler helpers, serialization, and native
bridges. The interpreter remains a consumer of trusted compiler output, not a
new security sandbox for arbitrary hostile code.

### 5.4 Later diversification experiments

After opcode support passes, separately evaluate deterministic native function
layout variation, metadata/object-table ordering and identifier indirection, and
snapshot serialization changes against actual extraction tools. Keep GC layouts,
FFI/embedding ABI, stack maps, exception unwinding, plugin registration, and
patch identity intact. Prefer small generated/adaptor hooks to deep upstream
rewrites. Do not change all object layouts, rewrite crypto, introduce opaque
control-flow everywhere, or compress/encrypt executable memory as the first step.

Encryption already limits offline artifact inspection; reduce unnecessary
plaintext/key copies and lifetime, clear temporary key buffers where practical,
avoid plaintext caches/dumps, and document unavoidable runtime plaintext. Persisting
plain canonical bytecode would undermine the intended generic-tool resistance.

## 6. BoringSSL and transport hardening

Treat three surfaces independently: Dart `dart:io` TLS, BoringSSL patch decryption
inside the VM, and Rust updater TLS/crypto. Native plugins may use additional
network stacks; inventory them before claiming application-wide coverage.

1. Keep BoringSSL aligned with the pinned official Flutter/Dart dependency
   revision and its security updates. Its APIs/ABI are not stable, so update it
   together with its consumers, consistent with the
   [BoringSSL project guidance](https://boringssl.googlesource.com/boringssl/+/HEAD/README.md).
   Preserve standard verification, cipher behavior, constant-time routines,
   assembly, randomness, and TLS interoperability.
2. Audit actual shipped exports and call boundaries. Android already hides most
   internals. For the diversified profile, evaluate hidden/local symbols,
   upstream-supported prefix generation, linker optimization, and deterministic
   native wrapper/function ordering to disrupt symbol/offset/signature-based
   hooks. BoringSSL's [experimental prefix support](https://boringssl.googlesource.com/boringssl/+/HEAD/BUILDING.md)
   is designed for namespace isolation; GN integration and security benefit must
   be demonstrated, not presumed from setting a CMake option.
3. Add optional SPKI pinning for operator-controlled update/key endpoints in each
   relevant client stack, with backup pins, overlap, expiry, recovery, and signed
   policy bounded by the embedded trust root. Keep hostname/certificate validation
   mandatory. Do not globally pin Pub/Flutter/CDN hosts or replace all Dart
   `HttpClient` behavior. Test corporate proxy and explicitly supported enterprise
   trust modes without silently bypassing release policy.
4. Keep signed artifact/selection verification mandatory even if TLS is bypassed.
   Harden key callback lifetime and logging at the native decryption boundary.
   Native symbol/layout diversity and integrity checks can raise hook cost but
   cannot prevent an attacker from dumping plaintext after successful decryption.
5. Test each real transport separately: valid server, wrong hostname, untrusted
   certificate, TLS interception, redirects, current/backup/retired pins, offline
   recovery, and a controlled TLS-verification-hook scenario. Successful TLS
   bypass must not authorize a forged patch or an unauthorized paid key.

## 7. Prefer official Flutter/Dart and minimize custom infrastructure

### 7.1 Classify dependencies before changing defaults

| Dependency | Intended source and rule |
| --- | --- |
| Hosted Dart packages | pub.dev by default; honor explicit `PUB_HOSTED_URL` for trusted private/regional mirrors. Keep lockfile integrity and local workspace packages. Official Pub documents this separation in [environment variables](https://dart.dev/tools/pub/environment-variables). |
| Flutter/Dart source | Track official Flutter/Dart revisions as the base, with the smallest auditable patch layer in project-owned forks. Preserve notices; record upstream commits separately from fork/toolchain identities. |
| Unchanged Flutter assets and compatible tools | Download directly from the official Flutter artifact origin (`https://storage.googleapis.com`) at the pinned official revision, using the verified artifact classification. |
| Stock Android Maven artifacts | Use `https://storage.googleapis.com/download.flutter.io` only for coordinates confirmed compatible. Custom engine/embedding release coordinates must remain custom. |
| Patched Dart SDK, kernel readers, `gen_snapshot`, platform dill, release engines, updater, custom Maven artifacts | Build and publish as an immutable matched toolchain on project/operator-controlled storage. No fallback to stock binaries. `font-subset.zip` also contains a kernel-reading snapshot and currently belongs here. |
| Patch generation tools and release-specific diversified engines | Own CI/registry with signed manifests and profile-scoped cache keys; official Flutter cannot provide these bytes. |
| Update/auth/licensing APIs | Keep the self-hosted server or an explicitly configured operator service. Official Flutter is not a code-push/licensing backend. |
| Ordinary third-party tooling | Keep its canonical source (for example Chromium/CIPD, Android tooling, Cargo/Go registries) with pinned inputs; do not route unrelated ecosystems through a Flutter mirror. |

Use [official Flutter release metadata](https://docs.flutter.dev/release/archive)
for version/revision pairing, not “latest” at build time. Regional mirrors remain
explicit choices; Flutter documents them as separately trusted providers in its
[mirror guidance](https://docs.flutter.dev/community/china).

### 7.2 Resolve artifacts by identity instead of one global host

Create a versioned, authenticated artifact catalog that maps each artifact to its
origin, upstream revision, custom toolchain/profile identity, platform, digest,
size, and required/optional status. Bundle or pin the catalog with the toolchain
so unchanged official artifacts do not require a running local proxy merely to
discover their revision. Verify custom catalog signatures and downloads before
execution/unpacking. Same-host SHA-256 sidecars alone only detect accidental
corruption if an attacker can replace both file and sidecar.

Retain the existing proxy as an optional compatibility/offline mirror route.
Prefer a shared origin resolver plus small Flutter-specific hooks; Android Gradle
may need separate repositories/content filters and immutable custom coordinates.
An official repository must not satisfy a custom runtime coordinate accidentally.
Prove resolution on all supported build paths before changing defaults.

Define one documented precedence model for direct Flutter, CLI-vended Flutter,
shell/PowerShell bootstrap, Gradle, SDK refresh, and CI. Keep existing environment
variables as compatibility aliases during migration. Scope `PUB_HOSTED_URL` to
Pub, standard `FLUTTER_STORAGE_BASE_URL` to official-compatible resources, and
the custom artifact setting to patched resources; preserve legacy all-in-one
mirrors through an explicit compatibility mode. Diagnose conflicting overrides
instead of silently downloading incompatible SDKs.

Replace mutable `releases/latest/download` bootstrap with an immutable release
or content address and an authenticated catalog. Preserve a small bootstrap trust
path capable of verifying the SDK before running that SDK. Cache by full identity
and digest, not version name alone; verify reuse and preserve offline cache
operation. A missing custom artifact produces a precise build/publish error.

### 7.3 Reduce Shorebird service reliance without breaking the fork

Audit production, development, telemetry, authentication, console, version-check,
bootstrap, artifact, and error-recovery paths for implicit Shorebird-owned hosts.
Defaults already use this project's forks/self-hosted services in many places;
remaining development constants in `shorebird_env.dart` still name Shorebird
services. Remove implicit use while retaining an explicit user-selected hosted
endpoint when desired. Historical documentation/license links are not network
dependencies and need not be renamed.

Do not point `SHOREBIRD_FLUTTER_GIT_URL` at stock Flutter and assume patching
survives. Maintain the custom source overlay and independently built runtime
pieces. Keep a tested upstream rebase procedure and minimize edits to upstream
files, following `flutter/AGENTS.md`.

Update both behavior and tests. Current infrastructure checks explicitly require
localhost mirror defaults and mutable latest-release URLs; merely changing those
strings will fail existing expectations. Replace them with origin-policy,
compatibility, integrity, and network-independence assertions. Run clean-cache
bootstrap, build, release, patch, update, and recovery while blocking implicit
Shorebird-owned service domains and recording actual outbound origins. Also run
with the optional local artifact proxy unavailable to prove direct official
resolution works. Custom artifact storage and the update server remain expected
dependencies for custom operations.

## 8. Delivery sequence and ownership

All rows below are future work. The stages are reviewable changes with gates,
not authorization to begin implementation in this planning task.

| Stage | Main owners / files | Exit condition |
| --- | --- | --- |
| P0 — Freeze evidence and contracts | Root CI/scripts; `flutter/DEPS`; source/build records; existing acceptance matrix | Resolve root/DEPS/symlink inputs, inventory real binaries/exports/origins, capture lifecycle/performance baselines, and define security/profile/catalog versions. |
| P1 — Secure release admission | Updater `config.rs`, `yaml.rs`, `network.rs`, `selection_v2.rs`, cache/boot modules; engine `shell/common/shorebird/updater.cc`; Flutter `shorebird_yaml.dart`; CLI packaging | A new hardened release cannot ship or boot with missing roots, downgraded verification, incompatible identity, or production HTTP. Existing v2 semantics and old-release compatibility remain explicit. |
| P2 — Official-source resolution and signed toolchain supply | CLI `shorebird_env.dart`, `shorebird_process.dart`, `cache.dart`, launchers; artifact proxy/catalog; Flutter cache/SDK scripts/Gradle; root manifest and CI scripts | Clean, immutable, verified bootstrap works with compatible official artifacts direct; custom pairs stay matched; no implicit Shorebird hosted traffic or silent stock fallback. |
| P3 — Updater self-integrity | Engine/Rust startup boundary, platform signing/module adapters, packaging build manifest, app key providers | Real repacked/replaced/config-tampered installs are rejected under documented platform trust assumptions; integrity failures recover without granting protected keys or destroying user data. |
| P4 — Signing separation and installation trust | Server selection/signing handlers and DB migrations; protocol/client packages; CLI; updater verifiers; engine adapters; golden fixtures | New version supports constrained online signing, separately approved artifact signatures, key rotation/revocation/recovery, and authenticated key provisioning; legacy releases remain operable. |
| P5 — Optional VM/BoringSSL diversification | Dart compiler/interpreter/loader/tooling; BoringSSL GN integration; custom engine producer; profile-aware server/CLI/catalog | Matching multi-profile builds execute all supported patch modes; wrong profiles reject early; generic extraction/hook resistance is measured and regressions stay within budget. |
| P6 — Platform acceptance and rollout | Root validation scripts/test app, native platform runners, security review | Required lifecycle, negative, clean-install, offline, performance, and device/native-host tests pass for each advertised profile/platform. Unsupported combinations are rejected at build time. |

P1 and P2 can progress independently after P0 defines their shared manifest and
identity contracts. P3 and P4 can be developed in parallel with disjoint ownership;
P5 requires the finalized profile/identity admission contract. Start with one
interpreter target, then expand; do not wait for every diversification experiment
to ship the core updater and origin improvements. Use small cross-repository
changes and pin compatible commits only after integration validation.

## 9. Acceptance matrix and release gates

| Category | Required evidence |
| --- | --- |
| Authenticity | Wrong/missing signer, modified byte, swapped artifact, modified YAML/root/profile/host ABI, malformed/duplicate JSON fields, altered no-update/base response, and forged delivery metadata all reject before activation. |
| Replay and state | Old epoch, equal epoch with changed descriptor, replayed nonce, copied journal, cache eviction, key loss, clock movement, crash between journal/content/pointer steps, concurrent engines, and reinstall/restore behavior have defined results. Authorized lower patch number at a newer epoch remains valid. |
| Self-protection | Tampered updater/engine/package resources, changed loader target, writable/symlink-swapped files, and post-check substitution tests; verify final packaged binaries and actual loaded modules. Separate detection evidence from prevention guarantees. |
| Keys and authority | Online signer compromise cannot forge a new artifact; root/delegate rotation, revocation, backup pin, expired grant, invalid install credential, lost keystore, offline grant, and recovery are covered. No public client identifier grants a key. |
| VM correctness | Exhaustively validate opcode bijection and all opcode/width combinations; test at least 16 deterministic seeds on host CI and multiple profiles on devices. Cover malformed instructions, forbidden VM-internal opcodes, cross-profile rejection, closures, exceptions, generics, async/isolates, FFI, plugins, body/structural/full replacement, and base recovery. |
| Toolchain correctness | Match compiler/runtime/SDK/profile across host and target; reproduce identical pre-signing outputs from the same seed and pinned inputs, and demonstrate intended diversity with different seeds. Preserve symbolication, dependency tracing, snapshot analysis, packaging, and cache isolation. Reject a correct compiler for the wrong profile. |
| Extraction resistance | Freeze versions/configurations of representative generic extraction and TLS-hook scripts. Prove they work on control builds, then record failures/remaining readable metadata on diversified builds. Include an adapted/manual assessment; report increased effort, never universal prevention. |
| TLS/crypto | Test Dart BoringSSL and updater rustls separately, plus inventoried plugin paths. Run upstream crypto/TLS tests for changed builds; verify identical protocol behavior, certificate/hostname enforcement, pin lifecycle, and forged-patch rejection even with TLS bypassed. |
| Dependency independence | Clean caches and sanitized environment, implicit Shorebird service domains blocked, optional proxy down, official/custom origin tracing, unsigned catalog rejection, unavailable custom artifact, mirror substitution, and verified offline cache reuse. |
| Runtime/lifecycle | Release → update → cold restart → offline use → signed variant switch → rollback/rollforward → tamper → bundled-base recovery → valid repair; verify both existing and diversified releases where supported. |
| Performance | Compare identical workloads/control and hardened profiles: cold-start p50/p95, frame time, interpreter CPU, RSS, patch generation/size, verify/install time, network cost, engine size, and CI/cache cost. Proposed initial diversification budgets: ≤5% p95 startup/steady CPU and ≤10% peak RSS growth; calibrate against P0 data before committing to release targets. |

Extend existing Rust `selection_v2`/`response_auth`/native-cache tests, server
selection/signing integration tests, Dart bytecode/patch/full-replacement tests,
artifact-proxy tests, root manifest validators, and the lifecycle test application.
Add fuzzing at changed bytecode/envelope/manifest/parser boundaries and run
available memory/undefined-behavior tooling in dedicated builds. Tests must
exercise decisions and failure boundaries, not only repeat generated mappings.

Publish results separately as source inspection, unit/integration test,
build-only, emulator, real native-host, and physical-device evidence. Android/iOS
device and desktop native-host coverage must match the advertised targets. Keep
unrun targets disabled rather than inferring support from compilation. Recheck
distribution requirements before release; current device execution alone does
not establish store acceptance.

Roll out in this order: enforced cryptographic release admission and verified
origins; platform integrity with heuristic observations; opt-in diversification
canary; wider availability after measured results. Roll back content through
authorized selection rules. Roll back a native hardening profile via a new
native release; never remotely disable signatures or accept another opcode map
to recover a mismatched patch.

## 10. Decisions to close at the implementation kickoff

Use the following defaults unless product constraints change: native-release
scope for diversification; standard cryptography unchanged; official Pub and
compatible Flutter artifacts direct; custom artifact store retained; signatures
mandatory in hardened releases; optional first-party pinning/attestation; current
offline grant semantics preserved.

Before committing implementation scope, select the first platform/profile,
measure acceptable startup/size/CI cost, choose the operator's immutable artifact
origin and signing backend, define private seed/symbol retention and recovery,
and decide which protected-state/attestation adapters are supported. These are
deployment/design choices, not blockers to producing this plan.

Planning deliverable: this document only. Existing unrelated workspace changes,
source files, dependency pins, runtime settings, build artifacts, and services
were left untouched.

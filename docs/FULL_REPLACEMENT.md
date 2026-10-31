# Experimental complete-domain replacement

The SDK has an opt-in full replacement profile intended to cover changes beyond
body replacement and additive structural patches. Flutter/CLI integration has
passed one distribution-signed physical iPad update and recovery lifecycle.
Native macOS ARM64 PRODUCT full-profile FFI host acceptance also passes;
physical iOS FFI, broader compatibility and release validation remain incomplete.
Existing releases continue to use their existing profile. A new native release
is required to enable the full profile.

## Model and binding

The native snapshot retains the SDK (`dart:*`, including `dart:ui`) as its frozen
host. Every non-SDK Dart code library belongs to the mutable domain. The reserved
`vm:ffi:native-assets` carrier contains compiler metadata only: its map belongs
to the released native host, and the carrier is omitted from replacement
payloads and manifests. Declarations or dependencies under that reserved URI
are rejected; this exception does not exclude plugin libraries. Before application
execution, a fresh isolate group must install either its bundled base component
or one replacement component. Both use the same loader. The loader allocates new
library, class, member, constant, layout and static identities, then publishes
the active library map and root only after loading and finalization succeed.
Retired native application metadata remains allocated; it is not resized or
reused for new classes.

The `SBRPLCE\0` version 1 wrapper contains a bounded manifest and a complete DBC3
component. The manifest binds the root URI, frozen-host ABI digest, original base
component digest, current component digest, baseline library set, current library
set, and removed libraries. URI sets use unique UTF-8 byte ordering. The native
snapshot stores the original baseline descriptor, so a patch cannot change its
host or baseline binding. No missing mutable URI resolves against the retired
application. Existing DBC3-only runtimes reject the outer format.

The release builder must calculate the host ABI digest from a canonical,
length-prefixed descriptor containing the exact engine/runtime, snapshot compiler
and platform dill hashes, target OS/architecture, PRODUCT setting and profile.
The VM compares the persisted stamp; it does not hash its own executable. The
host acceptance harness implements this calculation. The CLI and Flutter tools
independently hash the selected iOS device engine, snapshot compiler and PRODUCT
platform dill before compilation. A supplied environment stamp alone is not
accepted as proof of the selected artifacts.

The envelope/signature authenticates the entire wrapper. The inner SHA-256
checks component consistency; it does not replace publisher authentication.
Only trusted, authenticated compiler output is supported. This interpreter is
not a sandbox for arbitrary hostile bytecode.

## Compiler profile

Generate the bundled base first, then bind the native baseline to that exact
wrapper. The flags are mutually exclusive with structural patching:

```text
dart2bytecode --shorebird-full-replacement --shorebird-host-abi=<64hex> ...
gen_kernel --aot --no-tfa --shorebird-full-replacement \
  --shorebird-host-abi=<64hex> --shorebird-replacement-base=<base.sbr> ...
gen_snapshot --shorebird-full-replacement ...
```

Update compilation retains the entire component and uses `--patch-base=base.sbr`.
The native and bytecode builds must have identical root, library domain, source
injections, environment defines and platform inputs. The CLI generates Flutter's
plugin registrant before bytecode compilation and mirrors its additional source
roots. Package URIs and a project-relative virtual filesystem keep these inputs
stable across checkout locations.

Enable the profile in the application's source `shorebird.yaml` with
`aot_patch_runtime_mode: dart-bytecode-interpreter` and
`aot_patch_full_replacement: true`. It is mutually exclusive with
`aot_patch_structural_updates`. The release builder bundles the exact baseline
as `shorebird_replacement_base.sbr` before signing and retains the same bytes in
the release supplement. Full-profile obfuscation is currently unsupported.

For patch comparison, the CLI first compiles the complete updated component
bound to the original release. It projects the same component into a temporary
current-source baseline for the native comparison build. Archive comparison
validates and normalizes only the generated baseline asset and its YAML hash;
native/plugin/application asset changes remain visible. Only the original-bound
update wrapper is encrypted and published.

## Runtime and fallback

Installation occurs after frozen SDK bootstrap and before `MakeRunnable` or any
application invocation. Application declarations are inactive until installation
commits. Direct retired library/type/object handles are rejected by the relevant
embedding operations. Native embedders must not retain old application objects
inside SDK/native collections or caches, and must reacquire the entrypoint after
installation. The direct-handle checks do not establish safety for arbitrary
nested object graphs retained by an embedder.

Native resolver registrations made by the embedder transfer to a replacement
library with the same URI. New URIs start unregistered, removed URIs disappear,
and resolver APIs reject retired handles and failed installation groups.
Registration on the current library remains possible after startup. This copies
only the three C callback pointers; it does not preserve Dart objects held by
the embedder or change native-asset and process lookup behavior.

A failure after staging starts poisons that group. Destroy it, create a fresh
group, and install bundled base bytecode. Running the inactive native application
is not a fallback for this profile. Never install base and update sequentially
into one group. A broken bundled base must fail startup.

Full replacement admits ordinary `dart:ffi` declarations and calls on the
released native ARM64 AOT interpreter capability, excluding Windows and
simulator execution. The current full-profile execution
proof is macOS ARM64; the capability does not establish Linux or iOS device
execution. Non-FFI full replacement keeps its existing architecture support.
Every application and plugin Dart library remains mutable. Native plugin
binaries, symbols, asset mappings and signed callback trampolines remain part
of the frozen released host.

Library flags must contain the full-profile bit and may additionally contain
only the FFI bit. Actual SDK references, declarations, pools and opcodes are
checked even when the advertised FFI bit is cleared. FFI-native declarations
require paired native/external flags, no old native name, the released SDK's
pragma and Native annotation identities, and valid native function-type
metadata. Call bodies and nested closures must have valid FFI owners and native
cache operands; caches start at zero and resolve at runtime. Mirrors, ordinary
unresolved external declarations, old VM natives/`ExternalCall`, native-wrapper
inheritance/layouts, unknown flags and unsupported bytecodes remain rejected.
This admission does not update native plugin binaries or relax the release
binding and authenticated encryption contract.

The native ARM64 PRODUCT call-out path uses the shared ABI classifier and a
statically linked trampoline. Callbacks enter interpreted closures through
released signed aliases and the common entry without generating executable
instructions. The full-plugin host fixture below exercises replacement compound
layouts, new native signatures and callback kinds. Physical-device FFI and
broader plugin/application acceptance remain separate gates.

The shared native ABI classifier and a data-only interpreter call plan now
compile and link in PRODUCT without compiler backend dependencies. The ABI
golden suite also builds with precompiled-runtime defines. A macOS ARM64 native
fixture verifies the real interpreter call frame for scalar, mixed, overflow,
packed, compound and variadic calls, with ordinary JIT execution as a control.
It also covers `@Native`, `Native.addressOf` and an empty-stack trampoline guard.
Nullable local and persistent Handle identity, native Handle results, 24
nonleaf garbage collections, and leaf typed-data/struct pointer borrowing pass
in both the interpreter and JIT control. Pointer checks include views, nonzero
struct backing offsets, boundary sentinels and native-backed structs.
This fixture runs the non-AOT interpreter. PRODUCT and full-profile execution
are covered separately below.

```sh
python3 dart-sdk/tools/test_interpreter_ffi_calls.py \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64
```

Build `dartvm` explicitly before this check. The `dart` command can delegate to
a separate VM executable, so rebuilding the launcher alone does not establish
that the test exercises the changed runtime.

The PRODUCT metadata harness verifies evaluated pragmas on fresh function,
class and field declarations, frozen SDK annotation identity, first-use
`asFunction` signature metadata, actual `IntPtr` ABI conversion, malformed
annotation rejection and full-profile base/update installation. It also checks
that ordinary AOT still drops metadata and that a patchable baseline retains the
specific SDK class and field descriptors needed for lookup. The harness does
not invoke FFI calls. Its macOS-only library-validation entitlement is confined
to the test executable so it can load locally signed snapshot fixtures.

```sh
python3 dart-sdk/tools/test_aot_product_metadata.py \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64
```

The macOS ARM64 PRODUCT call-out harness passes against a structural baseline
with no application FFI use. It loads a new ordinary bytecode library through
the existing embedding API and verifies its interpreter frame and return into
the linked static trampoline. Coverage includes scalars, pointers, Handles and
GC, compounds, HFA, Union, nested/packed/inline-array layouts, varargs,
ABI-specific integer calls and memory operations, and resolver/process/released
asset lookup. Missing ABI mappings raise `ArgumentError` before memory access or
native invocation; guard-page, sentinel and native-call counter checks pass.
The host executable requests no JIT or debugger entitlement. Mapping snapshots
showed no added executable or writable-executable regions at the checkpoints;
they do not audit transient permission changes or instruction writes.

```sh
python3 dart-sdk/tools/test_aot_product_ffi.py \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64
```

This harness also passes ordinary AOT native-assets loading and no-FFI full
base/update installation from source and prelinked kernels. The released map
retains its identity and contents, the metadata carrier stays outside the
replacement domain, and package plugin libraries/classes receive fresh
identities. A forged carrier is rejected by the native snapshot compiler.
These earlier checks establish host call-outs and asset ownership. Separate
callback and full-profile fixtures below cover those execution paths; none of
these host checks establishes physical-device FFI validation.

The separate macOS ARM64 PRODUCT callback harness now passes static
`Pointer.fromFunction` and capturing `NativeCallable.isolateLocal` callbacks
created from newly loaded bytecode. It covers scalar/pointer/primitive-varargs
signatures, GC, nested calls, self-close, callback-page growth and reuse,
registration rejection cleanup, and entry without a current Dart thread.
Descriptors retain live roots without adding compiled callback code. Signed
trampoline aliases match the released bytes and have the expected permissions
at inspection checkpoints. Compiled callback controls remain separate from the
interpreted cases. Two child-isolate termination tests verify native epilogues,
embedding unwind errors and absence of further Dart execution.

The call-out harness also verifies 300 native scope probes and 300 restoration
checkpoints with an embedding API scope, then repeats them without one. The
existing bytecode call instruction already owns the required per-call scope;
the additional coverage verifies success, throws, returned errors, propagation,
nested calls and Handle varargs. The runtime change propagates callback-induced
isolate termination after the native epilogue.

```sh
python3 dart-sdk/tools/test_aot_product_ffi_callbacks.py \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64
```

The expanded harness also passes Handle and by-value aggregate callbacks:
Struct/Union, HFA, indirect returns, packed tails, nested arrays and compound
varargs. Guard pages and sentinels check exact-size copies; aliasing cases verify
that failed indirect returns are zeroed after the target has read its input.
Retained wrappers own copies of native input, while pointer fields preserve
their native alias. Managed views, nonzero backing offsets and overlapping
native return buffers also pass.

Handle cases verify caller-scope ownership, GC, errors, propagation and recursion
with and without an embedding scope. A separate child confirms that a Handle
result without a caller scope is rejected; another verifies Handle-result
callback termination. Recoverable OOM injection was not exercised.
The [aggregate callback record](validation/product-interpreter-callback-aggregates-20261101.json)
identifies this later tested tree and the corrected Windows ABI-test source list.

The later native macOS ARM64 PRODUCT milestone passes interpreted and compiled
listener and isolate-group-bound callbacks, including foreign-thread delivery,
copied aggregate/Handle arguments, closure immutability, GC, close/shutdown and
same/cross-group transitions. It also verifies nested caller restoration and
non-shared/shared static-field access rules. The
[listener/group validation record](validation/product-interpreter-callback-listeners-20261101.json)
identifies SDK commits `1a3b24b` and `a2095cb` and its precise evidence limits.
That milestone preceded full-profile FFI admission; the separate full-plugin
acceptance below now covers installation plus real interpreted calls/callbacks.
Physical-device FFI remains outstanding. Host checks do not audit transient
memory permissions or instruction writes. The earlier
[callback validation record](validation/product-interpreter-callbacks-20261031.json)
remains historical evidence for its tested tree.

The earlier call-out SDK (`e8f281db2fca`) also passed local host
SDK/snapshot-tool builds and an iOS framework build. That build does not cover
the subsequent callback changes. The installed iOS 27 SDK required selecting Xcode's Apple
linker in the generated local link rule because the bundled LLD could not parse
its TAPI architecture tag. The [engine build record](validation/product-interpreter-engine-20261031.json)
records this workaround, source revisions, artifact hashes and the limited scope
of the engine-argument route check; it is not an iOS FFI execution result.

## Validation and remaining work

The native macOS ARM64 PRODUCT full-plugin acceptance uses the real Dart
envelope producer and the unchanged released native library. Run it with matching
SDK artifacts and a Dart SDK capable of running that producer:

```sh
python3 scripts/verify_encrypted_aot_runtime.py --full-replacement-ffi \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64 \
  --tooling-dart dart-sdk/xcodebuild/ReleaseARM64/dart-sdk/bin/dart
```

The [full-profile FFI host record](validation/full-replacement-ffi-host-20261101.json)
records passing source and from-dill native baselines. For each baseline, bundled
base, original-bound update and a valid update with its advertised FFI bit cleared
execute through encrypted installation in two fresh groups. The update changes
plugin Dart fields, constructor defaults, signatures and Struct layout, removes
and adds declarations/libraries, and introduces new FFI signatures and local,
listener and group-bound callbacks. Native exports stay unchanged. The fixture
checks fresh mutable identities, resolver transfer and registration rules,
released native-assets map identity, initially zero native caches, successful
resolution/caching, native return values and callback cleanup/lifetimes.

Thirty digest-correct malformed wrappers are rejected against each native
baseline (60 loader checks), with specific diagnostic, publication, poisoning and
fresh-group bundled-base recovery assertions. These direct-loader mutation
checks are distinct from encrypted positive installation and envelope rejection
checks. They cover library flags, external/old-native definitions, malformed SDK
metadata, FFI owner/cache operands, normal/wide/truncated opcodes, ExternalCall
pools and the exact reserved assets URI. Thirteen focused compiler/native-assets
tests also pass, including direct bytecode API and from-dill validation. This host
fixture does not exercise physical iOS FFI, CLI native archive comparison or
server signature verification. It does not establish a new CI result.

Run the host acceptance with matching SDK artifacts:

```sh
python3 dart-sdk/tools/test_aot_full_replacement.py \
  --build-dir dart-sdk/xcodebuild/ReleaseARM64 --product
```

The macOS ARM64 PRODUCT proof covers changed const fields and constructor
defaults seen by unchanged-source callers, nested generic constant identity,
static initialization, field/method/class/library removal, inheritance and mixins,
SDK callbacks, garbage collection, child isolates, direct retired handles,
profile/binding rejection, malformed data rejection and fresh-group base recovery.
The real Dart AES producer also passes through native decryption, replacement,
and plaintext release before execution; wrong keys, altered ciphertext and
expired envelopes are rejected. Non-PRODUCT replacement, ordinary PRODUCT
startup and the existing PRODUCT additive suite also pass locally.
Tear-off access is normally rejected before installation, so that path does not
claim a captured retired closure was executed by the test.

CI runs this profile's host harness on Linux x64 and macOS ARM64/x64 before SDK
packaging. The iPad-tested milestone `c738750` passed all 30 configured build and
validation jobs, including artifact-mirror assembly. The
[CI record](validation/github-actions-full-build-20261031.json) lists every passed
job and the four intentional skips. That run does not cover subsequent PRODUCT
FFI changes.

The Flutter/CLI plumbing has focused unit coverage for profile propagation,
bundling, update-or-base selection, retained base configuration on retry, aligned
plugin registrant inputs and archive comparison. These checks do not replace a
real Flutter build and device run. A real Material app compile exposed desktop
windowing FFI imports in the iOS library graph. The full Flutter profile now
requires an explicit iOS target and selects a mobile window implementation via
the compiler's `dart.library._shorebird_ios_full_replacement` condition. It
retains the existing iOS null-window-owner behavior while avoiding desktop-only
imports. Other profiles keep their normal platform selection. Source bytecode
compilation of a real Material app passes with this selection. Local host and
iOS engine builds and the actual signed CLI release/archive-comparison pipeline
also pass. Physical startup exposed two gaps: PRODUCT pruning discarded empty
and export-only libraries, and AOT entrypoint lookup rejected an interpreted
function without a pre-created closure. Both have focused PRODUCT regressions
and fixes. On October 31, the distribution-signed iPad baseline and encrypted
update both reported success after their rendered-frame and semantic assertions.
The update changes const layout/defaults, signatures and statics, removes a
library and declarations, and adds a generic Flutter widget. A wrong AES key
caused fresh-group bundled-base fallback. Restoring the key did not reactivate
the rejected patch; a new patch recovered and passed three further cold launches
without provisioning credentials. Download, install and failure events were
persisted by the local server.

Recovery took 1.507 seconds from the process-relaunch command to the validated
HTTP report. Three subsequent launches took 1.463, 1.110 and 1.114 seconds. These
include device-control and network overhead; they are not VM startup, frame-time
or RSS benchmarks. The [physical test record](validation/full-replacement-ios-20261031.json)
identifies the tested integration sources. That device result predates FFI
admission; the later host acceptance above does not extend its physical-device
proof.

Remaining acceptance includes signed physical iOS full-profile FFI/callback
execution, broader plugin/application coverage, and launch/RSS/frame benchmarks.
This work does not establish complete Shorebird replacement. Native executable
cache behavior and its platform validation remain separate; see
[platform testing](PLATFORM_TESTING.md).

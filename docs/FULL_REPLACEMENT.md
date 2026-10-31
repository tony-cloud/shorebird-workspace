# Experimental complete-domain replacement

The SDK has an opt-in full replacement profile intended to cover changes beyond
body replacement and additive structural patches. Flutter/CLI integration is
implemented locally and undergoing end-to-end validation; it is not yet a
validated release feature. Existing releases continue to use their existing
profile. A new native release is required to enable the full profile.

## Model and binding

The native snapshot retains the SDK (`dart:*`, including `dart:ui`) as its frozen
host. Every non-SDK library belongs to the mutable domain. Before application
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

A failure after staging starts poisons that group. Destroy it, create a fresh
group, and install bundled base bytecode. Running the inactive native application
is not a fallback for this profile. Never install base and update sequentially
into one group. A broken bundled base must fail startup.

Version 1 rejects FFI, mirrors, native/external declarations, native-wrapper
layouts and unsupported native bytecodes. Supporting ordinary plugin FFI requires
additional design and validation; silently excluding those libraries from the
mutable domain is not supported.

The existing interpreted FFI call and symbol-resolution runtime entries are
disabled in `DART_PRECOMPILED_RUNTIME`. Enabling imports alone would reach
unsupported runtime paths. Completing plugin FFI requires PRODUCT-safe call
marshaling and symbol resolution, replacement Struct/Union layout metadata, and
callback trampolines that enter interpreted closures without generating
executable code. Native symbols and assets must remain bound to the released
host. Scalar/pointer calls, aggregates, ABI-specific integers, native annotations
and callback GC/isolate lifetimes each need runtime coverage.

## Validation and remaining work

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
packaging. A configured check is not evidence of a successful hosted run.

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
or RSS benchmarks. The tested code was the local integration working tree;
hosted builds of the final pinned revisions remain a separate gate.

Remaining acceptance includes plugin FFI, broader plugin/application coverage,
and launch/RSS/frame benchmarks. This work does not establish complete
Shorebird replacement or change the documented plaintext native executable cache
limitation.

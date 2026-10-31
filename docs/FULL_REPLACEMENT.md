# Experimental complete-domain replacement

The SDK has an opt-in full replacement profile intended to cover changes beyond
body replacement and additive structural patches. It is not yet integrated into
the Flutter/CLI release lifecycle. Existing releases continue to use their
existing profile. A new native release is required to enable the full profile.

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
host acceptance harness implements this calculation. Flutter's release builder
still needs the equivalent artifact binding.

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
injections, environment defines and platform inputs. Flutter's generated plugin
registrant is one required source injection that the current CLI does not yet
mirror in its bytecode build.

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

Remaining acceptance includes Flutter/CLI profile propagation, bundling the base
before signing, update-or-base selection, preserving bundled-base configuration
on fresh-group retry, aligned plugin registrant inputs, real iPad execution, and
launch/RSS/frame measurements. The physical iPad evidence from 2026-10-31 covers
the separate additive structural profile. This work does not establish complete
Shorebird replacement or change the documented plaintext native executable cache
limitation.

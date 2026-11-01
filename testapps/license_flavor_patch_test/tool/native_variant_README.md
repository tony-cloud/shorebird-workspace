# Opt-in native protocol-2 variant fixtures

`linux_variant_e2e.py` and `windows_variant_e2e.py` compose the existing platform
lifecycle ownership with `native_variant_desktop.py`. The V1 encrypted-cache
runners, their providers, and their workflow gates remain unchanged.

The new drivers require matching native v2 Flutter/CLI/updater sources, prebuilt
native engine/full SDK/patch tools, staged source package configurations and a
copyable pub cache. They do not build an engine or start a VM. Existing platform
preflight rules, CPU/machine checks, process/job ownership, and cleanup still
apply. Linux additionally retains the existing `--provider-probe` preflight
input; that old probe is not presented as v2 callback evidence.

A read-only plan needs no native resources:

```sh
python3 testapps/license_flavor_patch_test/tool/linux_variant_e2e.py plan --arch arm64
python3 testapps/license_flavor_patch_test/tool/windows_variant_e2e.py plan --arch x64
```

Actual native execution requires `run --execute` and all the corresponding V1
platform driver's toolchain/session arguments, including separate absent `--work`
and `--results` paths. Run Linux under its own DBus/X11 session, as with the V1
fixture. Windows must run natively on the selected x64 or ARM64 host. Main owns
VM/device startup, shared build resources and runtime scheduling.

The complete opt-in lifecycle:

1. Copies the source/tooling into the owned private session. Immediately before
   the first `generate-linux`/`generate-windows` Flutter command, checks that the
   private `engine.stamp` and `bin/internal/engine.version` agree, then clears
   inherited informational `engine_stamp.json` and `flutter.version.json` in both
   tooling and vended SDK copies. No original SDK cache is touched.
2. Copies the current additive `shorebird_code_push` public API package into the
   private app dependency tree. Stages an opt-in v2 adapter around a private copy
   of the unchanged platform key-cache provider. The canonical v2 C header is
   staged explicitly, so legacy public embedding headers cannot hide its types
   behind the old common include guard.
3. Runs real CLI `release <platform> --stage-only`. The source policy fixes
   `selection_protocol: 2`, `aot_patch_runtime_mode: native-aot`, exact build ID
   `1.0.0+1`, strict managed verification and explicit checks (`auto_update:false`).
   It verifies the measured native AOT baseline and exact unpublished server
   scope, invokes `release-finalize --platform=... --arch=<wire-arch>`, then
   requires the same published scope and matching packaged scope asset. This
   exercises the CLI's measure/seal two-pass producer and explicit finalization.
4. Builds `pro_seed`, `pro`, `enterprise` through actual CLI patch commands, with
   `--variant` and publisher license metadata. Patch numbers are 1/2/3; pro maps
   to 2, enterprise to 3. Every signed artifact hash must match the actual built
   native AOT image, while original Runner/engine/plugin bytes and baseline scope
   stay unchanged. Pro/enterprise add distinct Dart classes, fields and functions;
   runtime-fed fields and never-inline methods avoid a label-only constant test.
   Interpreter-only structural/full-replacement flags are not used. Additional
   packaged assets/native libraries are not delivered by these native patches.
5. Launches the actual preserved base app. Its real public
   `readInstallationId()`/`readSelectionStatus()` calls return the installation
   and process-pinned selection; no updater ID is seeded or overridden. Paid
   assignment uses the real authenticated `/selections` API, with CAS epoch and
   idempotent retry. Returned scope/content/lease/policy must match the request
   and actual published images.
6. Exercises cold `free -> pro -> enterprise -> pro -> free -> enterprise -> free`
   transitions, including lower patch number 3 -> 2, an extra same-content higher
   authorization epoch/restart, and missing/wrong-key BASE fallback plus recovery.
   Each online acceptance report must keep the running code/current identity;
   each normal cold report must adopt the exact latest accepted epoch/digest and
   clear restart-required. Retryable key-refusal fallback preserves that accepted
   high-water identity while executing only free/base, retains the paid ready
   next target, and requires restart; quarantine/free-next cannot satisfy it.
7. Requires actual per-process native v2 provider receipts, fresh nonce/PID,
   native ciphertext matching real Go delivery, post-frame rendered results,
   additive application-owned data across upgrades/downgrades, and unchanged
   original release bundle hashes. Signed free cold launch must not release a
   paid key, even with old paid key/ciphertext caches retained.
8. Audits only the declared v2 journal and encrypted-cache namespace (including
   `selections-v2/<scope-hash>.json`), with bounded plaintext-key/executable scans.
   The inventory is final-files-only, not a transient-write trace. Existing
   platform cleanup stops only owned processes/services and removes only the
   disposable app cache/work/DPAPI or owned Linux session keyring.

The original OS providers still implement DPAPI (Windows) and an owned restricted
session keyring (Linux). New adapters register only
`shorebird_set_patch_decryption_key_provider_v2`. On each callback they first
require the exact authenticated descriptor/digest, native scope/transport AAD
bindings, and currently valid canonical lease. Only then may they enter the
legacy cache/provisioning path. Receipt fields are public routing identity only;
keys and provisioning credentials are not written to results. Linux key
continuation is across cold app processes in the same owned kernel session, not
across logout/reboot.

`native_variant_e2e.py` and `native_variant_status.dart.template` are also the
shared Android contract. `validate_report` permits a running process to remain on
its older selection after accepting a newer grant; cold acceptance must
additionally call `validate_cold_activation` with the exact latest identity.
Application data lives in `SHOREBIRD_VARIANT_APP_DATA_DIR`, separate from updater
and provider caches. Native Android integration owns its Java/JNI app-private path
and control channel.

Focused checks:

```sh
python3 -m unittest discover -s testapps/license_flavor_patch_test/tool -p native_variant_test.py -v
python3 -m unittest discover -s testapps/license_flavor_patch_test/tool -p 'test_*native_cache_e2e.py' -v
```

These cover source-policy/CLI boundaries, actual adapter orchestration with
mocked owned processes/API, exact epochs and additive data, request/response lease
binding, metadata cleanup before Flutter invocation, and both new C++ adapters
against the canonical ABI plus a stub cache. The C++ units prove that expired,
future, duplicate-JSON, unknown and cross-AAD contexts cannot touch even an
already populated cache. They do not execute GTK, Win32 DPAPI, a Flutter engine,
a live CLI/server lifecycle, or an Android device.

No native application runtime acceptance follows from those checks or a plan.
Actual runtime receipts remain a separate main-owned gate. Extended live lease
expiry, ciphertext/signature tamper and old-epoch replay scenarios are additional
runtime work; the current desktop lifecycle does not claim the complete macOS
adversarial matrix.

# Opt-in Android native V2 fixture

`../android_variant_e2e.py` is a separate native variant driver. The existing
Android V1 drivers, templates, and baselines remain usable unchanged.

Running `python3 android_variant_e2e.py plan` prints the contract without starting
services, touching a device, or building anything. `run --execute` requires all
explicit staged inputs shown by `--help`, a new private work directory, a new
results directory, and an explicitly owned adb serial. The runner never starts
an emulator. Physical devices additionally require `--allow-physical-device`.
Supported wire architectures are `arm`, `aarch64`, and `x86_64`; the actual
device ABI must match the selected target and Android API must be at least 24.

The staged CLI, Flutter/engine, SDK, current public V2 callback header, and
`shorebird_code_push` package must agree. The runner copies writable tooling,
pub and Gradle caches, Go build/module caches, and server source into owned work.
It removes inherited Flutter version JSON metadata only in its two private SDK
copies before their first build. It does not modify the supplied source caches.

The executable lifecycle builds the base release with actual CLI `--stage-only`,
checks final AAB/APK native bytes and packaged scope, reads the authenticated
scope API, runs `release-finalize`, and verifies published scope state. It then
builds structural Pro seed, Pro, and Enterprise native patches. It assigns the
actual Pro patch 2 and Enterprise patch 3 through the authenticated server API;
it never seeds authorization state or constructs a grant. Adversarial controls
change only the two specifically described cached-content files below.
Only the original uniquely named base APK is installed, once. Subsequent checks
preserve that APK and application data while observing the rendered new Dart
functions, installation ID, and public selection-status APIs.

The Java/JNI provider uses the public V2 engine callback. It accepts a key only
after the engine supplies verified signed-selection context and Java checks its
exact authenticated assignment, app/release/installation/ABI/native scope,
transport target, key ID, ordered delivery AAD, and lease. These checks precede
even a cached-key read. The existing authenticated two-field provisioning
endpoint supplies keys; AndroidKeyStore wraps persisted keys. No context-free
probe or cached-key inspection operation is exposed. The fixture does not claim
to verify server signatures in Java or prove hardware-backed KeyStore storage.

Per-launch receipts expose public grant identity only. Cold-launch assertions
require the exact latest accepted epoch and digest, including repeated grants
for the same content. Free cold launches release no key; paid cold launches
release only the latest grant. Online launches may retain their current process
grant while downloading the newly accepted grant. Missing/wrong-key tests must
run base code while retaining the authorized pending selection, then recover
from the unchanged wrapped-key store.

The adversarial extension uses fresh pre-engine control processes. Capability
and nonce checks protect hash-only cache snapshots and compare-and-swap tamper
operations for fixed patch 2/3 targets. Canonical paths, `lstat`, single-link
regular files, file/count/size limits, and the expected SHA-256 bind each change.
The normal code-cache `downloads` directory is allowed only when empty; pending
downloads, unknown entries, links, and ambiguous duplicated targets fail closed.
Controls never read provider keys or modify a selection journal, pointer,
installation ID, user-data file, or arbitrary caller-supplied path.

Changing cached ciphertext must produce free code with the same accepted paid
epoch/digest, `content_unavailable`, no key release, and an unchanged journal.
Recovery requires authenticated free selection, retirement of the damaged cache,
and explicit paid reassignment. The public check must report `outdated`, and the
recovered ciphertext must match an actual encrypted server delivery. The fixture
does not restore saved cache bytes. Changing only redundant cached
`state.json.signature` must continue running independently authenticated paid
code, preserving the journal and ciphertext; this field is not native V2 boot
authority, and valid native content is not required to download again.

Lease tests use `--lease-seconds` (default 90; range 30–3600). They reject excessive
device/server clock skew, observe a valid paid boot, then wait for the actual
Android wall clock to pass the lease deadline before requiring free code with
`expired`, retained high-water, and zero key release. Historical expired contexts
do not invalidate unrelated current-launch receipt checks.

Replay checks stop the real API and provisioning before a separate owned
listener serves an actual captured signed response, or its corrupted signature.
Capture provenance is the owned real Go API; Python does not independently
certify its signature.
The launch performs a real public update check without provisioning credentials;
rejection must report `upToDate` with no exception and preserve authorization
journal bytes. The capture nonce differs from the subsequent updater request:
these are complete old-response/signature-and-nonce rejection tests, not isolated
epoch or signature tests. Automatic server free selection after expiry or
rollback must advance exactly one epoch. Rollforward alone must remain free;
only explicit paid reassignment may restore Enterprise. Every launch retains
the same installation and additive application data, then the lifecycle finishes
with signed free selection and a cold offline free boot.

Cleanup writes `cleanup-ledger.json` in results before mutations. It records the
owned package, serial, reverse mappings, and removal certainty. A successful
package-manager query must confirm absence before private work is deleted.
Uncertain app, mapping, or host cleanup retains the work directory and ledger
for a bounded ownership-aware retry; it does not blindly replay an uninstall.

Focused host checks:

```sh
python3 -m unittest test_android_variant_app test_android_variant_e2e -v
```

The production Java cached-key authorization gate additionally runs on the host
JVM when `JAVA_HOME`, `ANDROID_VARIANT_ANDROID_JAR`, and
`ANDROID_VARIANT_JSON_JAR` identify existing JDK/Android API/org.json inputs.
This proves the tested gate rejects invalid contexts before its cached-key
source is called. It does not execute AndroidKeyStore, JNI, an APK, or the Dart
runtime. A second host JVM test executes the production cache-control logic with
a filesystem-type guard, covering CAS failures, symlinks/hardlinks, fixed targets,
and journal preservation. Real device results require an explicit successful `run --execute` and
its retained receipts; source and host tests alone are not native runtime proof.

# Disposable Android native fixture

These sources implement the control/provider half of [PROTOCOL.md](PROTOCOL.md).
They belong only in a uniquely generated disposable fixture package. The host
owns manifest/Gradle generation, the packaged Flutter toolchain, APK inspection,
installation, process lifecycle and the signed/encrypted updater scenario.

Generate `PatchKeyProvider.java`, `ControlActivity.java`, and `MainActivity.java`
from their `.template` sources. Replace `__JAVA_PACKAGE__` in all three **and in
`fixture_jni.cpp`**. Replace `__CONTROL_SHA256__` with the lowercase SHA-256 digest
of the host's random 32-byte capability encoded as 64 lowercase hex characters.
The capability itself must never be compiled into the app. Copy the canonical
`flutter/engine/src/flutter/shell/common/shorebird/shorebird_patch_encryption.h`
next to `fixture_jni.cpp`; the bridge uses its actual callback typedef and
resolves the existing `libflutter.so` registration export. No updater library is
linked into the bridge.

The generated app requires Android API 24+, arm64-v8a, Java 17, INTERNET and
cleartext loopback HTTP permission. Use a plain `android.app.Application`, an
exported `.ControlActivity`, an unexported `.MainActivity`, a release manifest
with `debuggable=false`, and no launcher or provider that prewarms Flutter.
Configure externalNativeBuild with this CMake file and the pinned NDK. Keep the
fixture JNI library and any required C++ runtime library in the original APK;
keep the dynamically registered native method/class names from shrinking or
renaming. MainActivity may run only after ControlActivity successfully registers
the process-lifetime callback and publishes `ready`.

The wrapping alias is `<generated-package>.shorebird-e2e-wrap-v1`. Private record
writes use random-IV AndroidKeyStore AES-256-GCM, a synced temporary file, atomic
rename and directory sync. A damaged record or missing wrapping alias fails
closed. Missing/wrong modes do not access the store or network. Resolver work
runs on one bounded daemon executor, and direct loopback sockets have connect,
read and absolute close deadlines. They do not use global HTTP proxy, cookie or
authenticator state. Callback return buffers and native stack copies are erased;
raw per-patch keys still necessarily exist transiently in process memory and in
cryptographic provider internals. No hardware-backed Keystore claim is made.

`probe` runs the exact registered callback from a new C++ thread, checks that it
attaches itself to the JVM and detaches afterward, and returns an HMAC. It never
constructs a Flutter engine. Inspect never initializes Flutter or writes updater
state. Its key scan decrypts only the requested cached record, never provisions,
and covers final files only. A standalone inspection reports its own fresh PID
and process token; an inspection of a running launch reports the existing ones.

`compile_check.py` accepts explicit paths to installed `--android-jar`,
`--flutter-embedding`, `--ndk`, `--header`, and `--java-home`, with repeatable
`--dependency` jars for the embedding's AndroidX/Kotlin dependencies. An optional
`--json-jar` enables strict JSON checks against a pre-staged real org.json JVM
implementation. It uses temporary output, Java `--release 17`, and the NDK's
`aarch64-linux-android24-clang++` with warnings as errors; it does not download
anything. The host JVM tests execute context/UTF-8/URL/key-response validators,
HTTP framing/redirect rejection and an HMAC known vector. Android platform stub
methods are never executed. The Java compile uses the actual cached Flutter
embedding, not a FlutterActivity stub.

Compile checks and pure checks do **not** demonstrate APK integration, actual
JNI execution, AndroidKeyStore persistence, updater callback execution, native
prepared-FD loading, rendered receipts or any device lifecycle result. Those
require the main lifecycle driver running the original installed release APK.

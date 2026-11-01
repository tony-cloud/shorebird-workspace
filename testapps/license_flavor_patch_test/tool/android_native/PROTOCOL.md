# Disposable Android lifecycle fixture protocol

This is a test-only control surface in a uniquely named, non-debuggable app.
It is not a production provisioning API. No operation writes updater state.
The installed baseline APK is preserved for the entire lifecycle.

The Python driver generates the Java package and a SHA-256 digest of a random
control capability using template tokens `__JAVA_PACKAGE__` and
`__CONTROL_SHA256__`. Only the digest is compiled into the app. Every explicit
Intent to `.ControlActivity` includes string extras `control`, `operation`,
`nonce` (48 lowercase hex characters), and `observer` (literal IPv4 loopback
HTTP URL ending `/native`). Commands and credentials are never logged.

Operations:

- `launch`: only in a fresh process. Extras `mode` (`correct`, `missing`,
  `wrong`), `receipt_url` (loopback `/receipt`), optional paired
  `provision_url` (loopback `/key`) and `provision_token`. Load Flutter and the
  fixture JNI library, configure and register the provider before starting the
  unexported MainActivity. Publish `ready` before starting Flutter.
- `probe`: fresh process, same provider configuration as launch, plus `key_id`,
  `aad`, `challenge` (64 lowercase hex). Invoke the actual callback from a new
  native thread. Return HMAC-SHA256(key, challenge bytes), not the key. Never
  create a Flutter engine.
- `clear-provider`: fresh process; remove only the fixture's wrapped records
  and wrapping alias. No Flutter initialization or updater writes.
- `inspect`: may run in the existing launched process. Read bounded updater
  and provider files; never reconfigure the provider or initialize Flutter.
  Optional paired `key_id`/`aad` permits a cached-only key-byte scan, with no
  network request. An inspection from a running app must report its same PID
  and process token. A standalone inspection must not initialize the engine.

Reject unknown operations, missing/extra operation-specific fields, invalid
capabilities, malformed bounded UTF-8, endpoints with userinfo/query/fragment,
and repeat configuration in an initialized process. Intent URLs are restricted
to `http://127.0.0.1:<canonical-port>/<expected-path>`; the host owns adb reverse
mappings for these ports. Never accept arbitrary filesystem paths.

Every native report is a JSON object with exactly:
`schema` = `android-native-control-v1`, `nonce`, `pid`, `process_token`
(32 lowercase hex, random and constant for one process), `operation`, `payload`.
The operation is the requested name, except launch reports `ready`. Failures
report `error` with a sanitized fixed `reason`; never return exception strings.
Reports go to the specified `/native` endpoint with finite timeouts.

Payloads:

- `ready`: `{ "provider_registered": true }`.
- `probe`: `{ "mac_hex": "<64 lowercase hex>", "native_thread_attached": true }`.
- `clear-provider`: `{ "records": 0, "alias_present": false }`.
- `inspect`: `updater_roots` array with two entries, labelled `files` and
  `code-cache`, each containing `present` and `files`. Each file entry contains
  relative `path`, `size`, `sha256`, and `prefix_hex` (up to four bytes).
  Known `state.json`/`pointers.json` files additionally contain parsed `json`.
  `provider_files` lists relative path, size and SHA-256 of wrapped records;
  `alias_present` is boolean; `key_scan` is `passed`, `unavailable`, or
  `not-requested`. A known plaintext key, its hex encoding, symlink, unexpected
  file type, oversized file, malformed known JSON or excessive inventory fails
  the operation. Inspection is bounded to 1024 files, 256 MiB per file and
  1 MiB per JSON file. The host separately rejects plaintext executable hashes
  and magic, unexpected names, candidate mutation, and malformed inventory.
  `inventory_scope` is always `final-files-only`, with no transient-write claim.

The Dart receipt has the desktop receipt's exact fields plus `process_token`,
with schema `android-native-cache-e2e-v1`. JNI sets
`SHOREBIRD_E2E_RECEIPT`, `SHOREBIRD_E2E_OBSERVER`, and
`SHOREBIRD_E2E_PROCESS_TOKEN` before Flutter startup. Dart looks up the actual
boot patch in `libflutter.so`; the driver matches PID and process token to ready
and `adb pidof`, plus actual rendered text and the expected boot patch.

Provider records use a generated non-exportable AndroidKeyStore AES-256 GCM
wrapping key, no authentication/StrongBox requirement, and a random Cipher IV.
Filename: SHA-256(key_id UTF-8 || NUL || exact AAD UTF-8) + `.wrapped` under
`getNoBackupFilesDir()/shorebird-e2e-keys`. Record bytes: ASCII `SBK1`, 12-byte IV,
48-byte ciphertext/tag wrapping the 32-byte patch key. Wrapping AAD: UTF-8
`shorebird-android-e2e-v1` || NUL || key_id UTF-8 || NUL || exact AAD UTF-8.
Use private atomic writes. Existing malformed records or missing aliases fail
closed; never replace them by provisioning. Missing/wrong modes bypass storage
and network. Clear only the fixture-owned alias/records during explicit cleanup.
Raw patch keys necessarily exist transiently in process memory for the C ABI;
hardware-backed Keystore storage is not claimed on the emulator.

Provisioning uses the existing KeyAuthority JSON request (`key_id`, `aad`),
Bearer token and exact 64-lowercase-hex response. Proxies, redirects, ambient
cookies/authentication and main-thread networking are prohibited. Context is
strict canonical `open-aot-patch-delivery-v2` with platform `android`, arch
`aarch64`, and the same fields/order as the desktop fixture. The host must
confirm the actual updater-reported architecture rather than inventing it.

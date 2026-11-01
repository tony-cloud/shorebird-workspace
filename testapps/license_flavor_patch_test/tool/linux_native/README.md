# Linux native encrypted-cache fixture provider

This test-only provider calls the engine's public
`shorebird_set_patch_decryption_key_provider` API. The disposable GTK Runner must
call `RegisterLinuxNativeCacheTestProvider()` before `my_application_new()` and
exit on registration failure. It provisions an exact updater `key_id` and AAD
through the existing fixture KeyAuthority protocol, then retains only the
32-byte per-patch key in a Linux kernel **session keyring**. It does not persist
plaintext key files or use the user/persistent keyring.

The harness owns a new anonymous keyring created with
`keyctl_join_session_keyring(NULL)`. It clears the ring, sets `KEY_POS_ALL`
permissions (all user/group/other bits zero), passes its positive serial in
`SHOREBIRD_E2E_KEYRING_ID`, and revokes it in final cleanup. Registration rejects
a serial that differs from `keyctl_get_keyring_ID(KEY_SPEC_SESSION_KEYRING, 0)`.
Do not pass an interactive desktop session's existing keyring. Keyring access
failure is a test failure, including in restricted containers; it is not a skip.

Each key has type `user`, description
`shorebird-e2e-v1:<lowercase SHA256(key_id UTF8 || NUL || exact AAD UTF8)>`,
permissions `KEY_POS_ALL`, and a 3600-second timeout. Reads reject other
permissions and payload lengths. The provider validates bounded UTF8, serial,
mode, URL, token, response and output buffers; callback failure sets `written`
to zero. Access is serialized, exceptions cannot cross the C ABI, and explicit
key, response and bearer-header scratch buffers are erased after use. The
process environment and transport library still contain process-memory data;
this is an integration fixture rather than production secret management.

Environment:

| Variable | Contract |
| --- | --- |
| `SHOREBIRD_E2E_KEYRING_ID` | Required canonical positive decimal current session keyring serial, at most `INT_MAX`. |
| `SHOREBIRD_E2E_KEY_MODE` | Absent/empty or `correct`: retrieve/provision; `missing`: return unavailable; `wrong`: return 32 zero bytes. Missing/wrong callback paths do no keyring or HTTP I/O. |
| `SHOREBIRD_E2E_PROVISION_URL` | Together with token, exact `http://127.0.0.1:<port>/key`; canonical decimal port 1–65535. Both absent/empty for offline operation. |
| `SHOREBIRD_E2E_PROVISION_TOKEN` | 1–512 ASCII letters, digits or `-_.~`; sent as a bearer token. |

The libcurl request posts JSON `{"key_id":...,"aad":...}` to that strict IPv4
loopback URL. Proxy use, redirects and ambient HTTP authentication are disabled;
cookies are not enabled. Connection and overall timeouts are 3 and 10 seconds.
Only HTTP 200 with exactly 64 lowercase hex characters is accepted; response
body buffering is capped at 65 bytes so any suffix fails. Existing invalid
keyring entries fail closed rather than being overwritten through provisioning.

The session ring is inherited by fork/exec and supports **fresh-process offline
boot inside the owned session**. This does not establish persistence across an
OS restart, a new login/session keyring, logout revocation, or the key timeout.
See the [kernel key retention documentation](https://www.kernel.org/doc/html/latest/security/keys/core.html)
and [session-keyring manual](https://man7.org/linux/man-pages/man7/session-keyring.7.html).
Possessor permissions isolate this anonymous ring from processes which do not
possess it; this is not protection against privileged process inspection.

## Standalone contract probe

Compile only `native_cache_contract_test.cpp`, which includes the provider
implementation, against the real public Linux Flutter header tree. The probe
stubs the public registration function to capture its genuine callback type;
it does not load or initialize a Flutter engine. Example Linux build:

```sh
c++ -std=c++14 -Wall -Wextra -Werror -pthread \
  -I/path/to/engine/src/flutter/shell/platform/linux/public \
  native_cache_contract_test.cpp -o native_cache_contract_test \
  $(pkg-config --cflags --libs gtk+-3.0 libcurl glib-2.0) -lkeyutils
```

The Runner compiles `runner_patch_key_provider.cpp` separately and links the
same libcurl, GLib and keyutils libraries. Ubuntu packages include
`libcurl4-openssl-dev`, `libkeyutils-dev`, and `libgtk-3-dev`. Its generated
CMake target can use imported pkg-config modules for `libcurl glib-2.0` and
link `keyutils`.

Probe commands, all requiring the inherited owned-ring environment:

- `--self-test`: validates config/UTF8/JSON/response/buffer contracts, actual
  kernel storage and permissions, malformed payload rejection, missing/wrong
  modes, revocation, and a fork/exec offline read. It creates and removes its
  fixed nonsecret fixture key. It clears provisioning variables only in its
  own process and child, and never clears/revokes the harness's whole ring.
- `--read-context KEY_ID AAD_FILE`: invokes the callback and compares its actual
  output to **exactly 32 raw expected bytes on stdin followed by EOF**. Exit 0
  requires callback success, `written == 32` and exact bytes. Use distinct
  invocations with provisioning variables present and then absent to prove
  online provisioning followed by fresh-process offline recovery. AAD files
  contain exact nonempty bounded UTF8 with no NUL, with no newline trimming.
- `--expect-unavailable KEY_ID AAD_FILE`: exit 0 requires callback failure and
  `written == 0`; no expected-key stdin is needed. For wrong mode use
  `--read-context` with 32 zero bytes as the expected stdin value.

Probe exit 1 reports a sanitized failure label; exit 2 indicates incorrect
arguments. No mode prints the returned/expected key or bearer token. Lifecycle
tests should still verify missing/wrong keys cause real updater fallback and
that restoring the correct provider recovers retained encrypted cache data.

The macOS authoring host cannot execute Linux keyring syscalls. Formatting and
source checks here do not constitute Linux compilation or runtime evidence;
the Linux CI job must compile and execute this probe and the actual GTK
lifecycle.

## Full GTK lifecycle

`../linux_native_cache_e2e.py plan` is read-only on every host. Execution is
Linux x86-64 only and requires the matching prebuilt local engine and full Dart
SDK, release product dill, frontend server, const finder, sky engine, font
subsetter, snapshotter, GTK library/public headers/ICU, public updater patch
tool, populated Flutter/CLI package configs, a copyable pub cache, and the
compiled probe above. Missing X11/DBus, dependencies, probe, or kernel-keyring
access fails the run; there is no reduced-coverage skip.

Run inside an owned X11/DBus test session, for example:

```sh
dbus-run-session -- xvfb-run -a python3 ../linux_native_cache_e2e.py run --execute \
  --engine-src /path/to/flutter/engine/src \
  --host-engine linux_release_x64 --target-engine linux_release_x64 \
  --tooling-dart /path/to/flutter/engine/src/out/linux_release_x64/dart-sdk/bin/dart \
  --patch-tool /path/to/updater/target/release/patch \
  --provider-probe /path/to/native_cache_contract_test \
  --pub-cache /path/to/populated/pub-cache \
  --work /tmp/new-private-linux-lifecycle \
  --results /tmp/new-linux-lifecycle-results
```

Work and results must be separate, new directories. The driver snapshots CLI,
Flutter and server sources and copies mutable SDK/pub caches. Direct private
Dart launchers and an explicit preflight-validated toolchain staging marker keep
the CLI from running unrelated Android precache or replacing that SDK. The
released GTK library must hash-match the selected local engine. It generates the
Linux platform only in its disposable app, inserts provider registration before
`my_application_new()`, and links the provider's libcurl/GLib/keyutils dependencies
through that generated Runner's CMake. Each command, server and Runner starts
in an owned POSIX process group. Exit observation uses `waitid(WNOWAIT)` to
retain the leader and pin its group ID through cleanup; Linux `/proc` checks
exclude zombie members before the leader is finally reaped. Cleanup signals
only those pinned groups, removes
owned private work, and revokes the anonymous ring. HOME and XDG paths point
inside the private work directory, including the real Linux updater cache at
`HOME/.shorebird_cache`.

The sequence performs actual `shorebird release linux` (free) and
`shorebird patch linux` (pro), with server-managed signatures and per-device
AES-GCM delivery. The only allowed provisioning AAD comes from the real
updater client/architecture registration and the actual signed published
artifact. Native probes provision and recover that exact context in distinct
processes using expected key bytes on stdin. Their ring entries are cleared
before the real app download, which must independently provision its key.

Every launch requires a receipt bound to a fresh nonce and Runner PID, produced
after the first rendered frame. It reads the actual `RenderParagraph` text and
calls `shorebird_current_boot_patch_number` through `DynamicLibrary.process()`.
The driver stops API and provisioning listeners before a fresh offline pro
launch, tests missing/wrong key baseline fallback while retaining ciphertext,
then tests correct-key offline recovery. A real authenticated rollback API
request supplies the updater's signed rollback; the final fresh offline launch
must render baseline and report patch zero. Runner/engine/plugin bytes must
match the release when the patch is built, and the preserved installed release
bundle must remain unchanged throughout.

Only sanitized `results.json` survives. Raw build/server/Runner logs, request
credentials and temporary context files remain private and are removed. Final
cache scans require encrypted transport state and reject unexpected files,
plaintext executable images and known secret bytes. The report explicitly says
`final-files-only` and `transient_write_trace: false`: this is not a transient
write audit. Offline evidence covers cold app processes in one kernel session,
not reboot, logout, a new session keyring or key expiration.

Pure authoring-host checks (no services, builds or Linux runtime):

```sh
python3 -m unittest discover -s .. -p 'test_*native_cache_e2e.py'
```

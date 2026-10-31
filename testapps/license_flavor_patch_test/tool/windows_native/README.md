# Windows native encrypted-cache fixture

This provider is **test-only**. It provisions a derived per-patch key from the
harness's authenticated loopback service and persists only that 32-byte key inside
a current-user DPAPI blob. The server's master secret never enters the Runner.
The full lifecycle harness is `../windows_native_cache_e2e.py`; these native files
alone do not demonstrate a release, patch, native loader, rendering, or rollback.

Copy `runner_patch_key_provider.{h,cpp}` into the isolated generated Windows
Runner **before release**, add the `.cpp` to the Runner target, and link `winhttp`,
`crypt32`, and `bcrypt`. Include `runner_patch_key_provider.h` and fail startup if
`RegisterWindowsNativeCacheTestProvider()` returns false. Call it once before any
`FlutterViewController` or engine is created. It uses the existing public
`flutter_windows.h` callback ABI. Keep these native sources byte-identical when
building the patch; do not modify the fixture's original app or engine DLL.

The harness supplies these environment variables only to its owned app process:

| Variable | Meaning |
| --- | --- |
| `SHOREBIRD_E2E_KEY_CACHE_DIR` | Required absolute local Windows directory, unique to this session, already created and owned by the harness. Use a private directory with access restricted to the test account. Unicode paths are supported. |
| `SHOREBIRD_E2E_KEY_MODE` | `correct` (default), `missing`, or `wrong`. `missing` fails without cache I/O; `wrong` returns 32 zero bytes without cache I/O. |
| `SHOREBIRD_E2E_PROVISION_URL` | Online only: exactly `http://127.0.0.1:<port>/key`, decimal port 1–65535 without leading zeros. |
| `SHOREBIRD_E2E_PROVISION_TOKEN` | Online only: the private session bearer token (1–512 ASCII letters, digits, `-_.~`). |

For each offline process, **omit both URL and token**. Configuration is captured
at registration, so each lifecycle mode requires a new process. A correct lookup
checks the cache first and provisions only on a miss. Failure to persist a newly
provisioned key fails the lookup. Neither key IDs, AAD, tokens, nor key bytes are
logged by this provider.

The request is UTF-8 `POST /key` JSON with exactly `key_id` and `aad`, preserving
the callback's exact UTF-8 strings after JSON decoding. It includes
`Authorization: Bearer <token>` and `Content-Type: application/json`. Requests
are limited to 32 KiB, response headers to 16 KiB, and response body reads to 65
bytes (the extra byte detects oversized responses). Only status 200 with exactly
64 lowercase hexadecimal bytes succeeds. WinHTTP connects to the literal
`127.0.0.1`, disables proxy use, redirects, automatic authentication, and cookies,
and sets 3-second stage timeouts plus a 10-second checked read deadline.

Cache filenames are lowercase SHA-256 of `key_id + NUL + exact AAD`, followed by
`.dpapi`. The same digest is DPAPI optional entropy. Only current-user protection
with `CRYPTPROTECT_UI_FORBIDDEN` is used; unprotected payloads must be exactly 32
bytes. Cache reads reject reparse points and blobs over 16 KiB. Writes create an
exclusive random temporary file with a non-inheritable handle, write only the
protected blob, flush it, and rename it over the destination. Failure removes the
temporary file. The session directory's privacy is the harness's responsibility;
this is not isolation from another process running as the same Windows account.
A mutex serializes provider operations. Owned decrypted DPAPI payloads, key
buffers, and HTTP response bytes are explicitly zeroed after use.

The [WinHTTP option documentation](https://learn.microsoft.com/en-us/windows/win32/winhttp/option-flags)
and [Microsoft DPAPI example](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)
describe the OS option and ownership behavior used here.

## Standalone Windows contract probe

`native_cache_contract_test.cpp` includes the provider implementation and stubs
only its public engine registration endpoint. It requires a real Windows SDK and
Windows user profile; it does not link or start Flutter. It checks endpoint/token
restrictions, exact JSON escaping and digest vector, UTF-8 rejection, strict
response decoding, Unicode cache paths, a real DPAPI round trip in a fresh child
process without provisioning credentials, context entropy mismatch, key modes,
corrupt/oversized cache rejection, replacement of a corrupt blob, and temporary
file cleanup. It creates and removes its own random temporary directory and
terminates only its own test child if that child times out. It does not exercise
HTTP delivery, Flutter, updater state, native loading, rendered status, or rollback.

Run from an x64 Visual Studio Developer PowerShell at the workspace root:

```powershell
$fixture = (Resolve-Path testapps/license_flavor_patch_test/tool/windows_native).Path
$public = (Resolve-Path flutter/engine/src/out/windows_release_x64).Path
$build = Join-Path $env:TEMP ("shorebird-native-probe-" + [guid]::NewGuid())
New-Item -ItemType Directory $build | Out-Null
Push-Location $build
try {
  cl /nologo /std:c++17 /EHsc /W4 /WX /utf-8 /I"$public" `
    "$fixture/native_cache_contract_test.cpp" /Fe:native_cache_contract_test.exe `
    /link winhttp.lib crypt32.lib bcrypt.lib
  if ($LASTEXITCODE -ne 0) { throw "Native probe compilation failed" }
  & ./native_cache_contract_test.exe
  if ($LASTEXITCODE -ne 0) { throw "Native probe failed" }
} finally {
  Pop-Location
  Remove-Item -Recurse -Force $build
}
```

The native Windows build/probe and full lifecycle are unrun on the Mac development
host. Formatting or portable helper checks must not be reported as Windows runtime
validation. A final-files scan also does not prove absence of transient plaintext
writes; a complete write audit requires process tracing such as ETW/Procmon.

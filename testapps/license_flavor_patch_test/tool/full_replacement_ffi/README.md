# Physical full-replacement FFI fixture

Select this **new** acceptance run with `--full-replacement-ffi`. It implies
full replacement and is mutually exclusive with the existing `--full-replacement`
and `--structural-updates` flags. Existing default and structural profiles keep
their templates and license-only report policy.

```sh
python3 testapps/license_flavor_patch_test/tool/ios_device_e2e.py plan \
  --work-dir /private/tmp/shorebird-device-full-ffi --full-replacement-ffi
```

Use a new session and the normal phased CLI/server runner. Prepare copies these
templates and persists both `full_replacement=true` and
`full_replacement_ffi=true`; serve configures the real full-mode CLI pipeline.
Use `release --signed` and the existing dedicated bundle, distribution profile,
strict interpreter inspection, install and Keychain-provisioned launch flow.
The earlier completed iPad structural test is not FFI evidence for this profile.
This fixture does not start engine builds or bypass archive comparison,
encryption, server response verification, release binding or signing.

`ios/Runner/FullFfiFixture.c` contains every native implementation **before N/B**,
including V2/update-only symbols and callback consumers. Swift references its
anchor before engine creation. The dedicated Runner links with `export_dynamic`,
explicit native source membership and `STRIP_STYLE=non-global`. Xcode's default
archive `strip -D` removes the FFI dynamic exports even when linking preserved
them; non-global stripping (`-x`) keeps the required globals while stripping
local/debug symbols. These source settings are not export
proof: `inspect-app`/`install` now require every `IosFullFfi*` export in the final
Runner's `dyld_info -exports`, recording the binary hash and exports privately.
Actual physical calls remain required. The C/header/Swift/project sources stay
fixed while the patch phase changes only Dart templates; the real CLI's native
archive comparison must enforce that boundary.

B executes released scalar and 16-byte V1 compound calls, without application
FFI callback signatures. U changes the same `PluginRecord` class to a 24-byte V2
layout and binds its matching already-released C export. It introduces scalar,
bool/pointer and update-only bindings, `Native.addressOf`, process lookup,
Pointer.fromFunction, static and mutable capturing isolateLocal callbacks,
aggregate callbacks, queued scalar/compound listeners and an immutable capturing
group callback from two foreign pthreads. The factory captures one separate
`final int`; it shares no mutable listener/local-callback context. No private VM
probes, host embedder resolvers, executable allocation, GC commands or NativeApiDL
are used.

Native start returns immediately. Dart asynchronously awaits the four listener
events, checks the copied compounds and group results, and polls native
completion. A worker publishes completion only after every callback returns.
Only then does Dart join the quiescent workers and close every NativeCallable.
Queued messages are drained while descriptors remain live. Partial thread starts
also go through quiescence/join before close. Listener completion and native
quiescence each have ten-second bounds. If a runtime failure prevents quiescence,
the app retains callback owners until shutdown, throws and emits no success;
closing a pointer while native code might still invoke it is forbidden.
The single noncloseable Pointer.fromFunction intentionally remains until isolate
shutdown and is reported separately from closed NativeCallables.

The copied structural tests preserve changed const layouts/defaults, a
byte-identical unchanged caller, canonical nested constants, method/static
signature changes, removed fields/methods/class/library, and a new generic
Flutter widget override. U additionally checks dynamic access to the removed
field/method fails. Applying U removes `removed_library.dart`; compiled domain
manifest inspection remains part of the actual release/patch artifact evidence.
The success signal is emitted only after FFI completion, successful cleanup,
structural assertions and an attached nonempty RenderParagraph with exact text:

- B: `license:free full-replacement:verified value:10 full-ffi:v1-base`
- U: `license:pro full-replacement:verified value:28 full-ffi:v2-update`

The JSON report schema is `full-replacement-ffi-v1`, with exact phase/check
sets, rendered text, structural/FFI verification booleans, native worker count,
join/close evidence and static-pointer lifetime count. The observer rejects the
historical two-line license statuses in this profile and rejects this JSON in
legacy sessions. It cannot count an old structural success as FFI verified.
Reports retain this schema under `verification` in `launch-status.jsonl`.

Source validation performed during implementation:

```sh
python3 -m unittest discover -s testapps/license_flavor_patch_test/tool -p test_ios_device_e2e.py -v
clang -std=c11 -Wall -Wextra -Werror -fsyntax-only testapps/license_flavor_patch_test/ios/Runner/FullFfiFixture.c
plutil -lint testapps/license_flavor_patch_test/ios/Runner.xcodeproj/project.pbxproj
```

Seven pure Python tests, C syntax/warnings and project plist validation passed.
Both B/U template sets were copied into temporary packages with local existing
package mappings, formatted and analyzed with `--fatal-infos` using the explicit
existing `dart-sdk/xcodebuild/ReleaseARM64/dart-sdk/bin/dart`; both had no issues.
No pub get, network, server, Xcode/native build, native execution, device action
or physical acceptance was performed for those checks. Temporary packages were
removed. Final distribution export visibility, signed N→B/N→U execution,
authenticated download, rendered reports and native archive equality remain
required results of the parent-run device acceptance.

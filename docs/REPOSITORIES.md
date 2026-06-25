# Repository Inventory

Last verified: 2026-06-25.

## Active Top-Level Repositories

| Path | Remote URL | Branch | Pinned commit |
| --- | --- | --- | --- |
| `dart-sdk-new` | `https://git.tonycloud.org/dart-lang/sdk.git` | `tonycloud/dev` | `57b27a7b44a9112c7cb3a1cb3c73636fc149bd16` |
| `depot_tools` | `https://chromium.googlesource.com/chromium/tools/depot_tools.git` | `main` | `226aa79e9947adc1e9e0c79f96b58562516535d9` |
| `flutter` | `https://git.tonycloud.org/flutter/flutter.git` | `shorebird/dev` | `5b96fd59be2f00061cccaaa23a02e15e720c1161` |
| `shorebird` | `https://git.tonycloud.org/flutter/shorebird.git` | `main` | `07a654e7717a20d685c3eadf4b55c2e6731e96fa` |
| `shorebird-server` | `https://git.tonycloud.org/flutter/shorebird-server.git` | `main` | `d6e5a39546905b85e2345b8058c4350ed236184c` |
| `updater` | `https://git.tonycloud.org/flutter/shorebird-updater.git` | `main` | `5d4e9c339636fc2f67cbe9892026d9e10b0888bc` |

## Removed Top-Level Repositories

| Path | Previous remote URL | Reason |
| --- | --- | --- |
| `shorebird-engine` | `https://github.com/shorebirdtech/engine.git` | Separate Flutter engine checkouts are obsolete for this workspace. The archived upstream engine repository points contributors to `flutter/flutter/engine`, and this workspace's `flutter` submodule already contains `flutter/engine/src/flutter`. |

## Generated Or Obsolete Root Folders

The following root folders are not part of the meta-workspace and can be
regenerated or replaced by tracked source:

| Path | Reason |
| --- | --- |
| `.cipd` | Local CIPD cache created by gclient/depot_tools. |
| `buildtools` | Ad hoc root tool download/output; Dart build tools live under `dart-sdk-new/buildtools`. |
| `dart-sdk` | Empty stale SDK checkout. |
| `hello_shorebird_test` | Old throwaway Flutter app. The active fixture is `testapps/license_flavor_patch_test`. |
| `patches` | Earlier one-off patch files superseded by source changes and `packages/open_aot_patch_tools`. |
| `sdk` | Generated Windows toolchain cache. |
| `testapp2` | Old throwaway Flutter app. |
| `testapp3` | Old throwaway Flutter app. |

## Submodule Policy

Only top-level project repositories are submodules. Third-party repositories and
CIPD packages fetched by `gclient sync` remain owned by their upstream DEPS files
and should not be duplicated in `.gitmodules`.

Root-owned content should be limited to:

- workspace documentation
- bootstrap and test scripts
- small test fixtures that intentionally combine multiple submodules
- meta-repository config such as `.gitignore` and `.gitmodules`

## Nested Git Checkouts Observed

These repositories were found under top-level checkouts. They are managed by the
owning repository's DEPS/gclient workflow, not by the root `.gitmodules` file.

| Path | Remote URL |
| --- | --- |
| `dart-sdk-new/buildtools/clang_format/script` | `https://chromium.googlesource.com/chromium/llvm-project/cfe/tools/clang-format.git` |
| `dart-sdk-new/tests/co19/src` | `https://dart.googlesource.com/co19` |
| `dart-sdk-new/third_party/binaryen/src` | `https://chromium.googlesource.com/external/github.com/WebAssembly/binaryen.git` |
| `dart-sdk-new/third_party/boringssl/src` | `https://boringssl.googlesource.com/boringssl.git` |
| `dart-sdk-new/third_party/cpu_features/src` | `https://chromium.googlesource.com/external/github.com/google/cpu_features.git` |
| `dart-sdk-new/third_party/crashpad/crashpad` | `https://chromium.googlesource.com/crashpad/crashpad.git` |
| `dart-sdk-new/third_party/cygwin` | `https://chromium.googlesource.com/chromium/deps/cygwin.git` |
| `dart-sdk-new/third_party/emsdk` | `https://dart.googlesource.com/external/github.com/emscripten-core/emsdk.git` |
| `dart-sdk-new/third_party/googletest` | `https://fuchsia.googlesource.com/third_party/googletest` |
| `dart-sdk-new/third_party/icu` | `https://chromium.googlesource.com/chromium/deps/icu.git` |
| `dart-sdk-new/third_party/jinja2` | `https://chromium.googlesource.com/chromium/src/third_party/jinja2.git` |
| `dart-sdk-new/third_party/libc` | `https://llvm.googlesource.com/llvm-project/libc` |
| `dart-sdk-new/third_party/libcxx` | `https://llvm.googlesource.com/llvm-project/libcxx` |
| `dart-sdk-new/third_party/libcxxabi` | `https://llvm.googlesource.com/llvm-project/libcxxabi` |
| `dart-sdk-new/third_party/markupsafe` | `https://chromium.googlesource.com/chromium/src/third_party/markupsafe.git` |
| `dart-sdk-new/third_party/mini_chromium/mini_chromium` | `https://chromium.googlesource.com/chromium/mini_chromium` |
| `dart-sdk-new/third_party/perfetto/src` | `https://chromium.googlesource.com/external/github.com/google/perfetto` |
| `dart-sdk-new/third_party/pkg/core` | `https://dart.googlesource.com/core.git` |
| `dart-sdk-new/third_party/pkg/dart_style` | `https://dart.googlesource.com/dart_style.git` |
| `dart-sdk-new/third_party/pkg/dartdoc` | `https://dart.googlesource.com/dartdoc.git` |
| `dart-sdk-new/third_party/pkg/ecosystem` | `https://dart.googlesource.com/ecosystem.git` |
| `dart-sdk-new/third_party/pkg/http` | `https://dart.googlesource.com/http.git` |
| `dart-sdk-new/third_party/pkg/i18n` | `https://dart.googlesource.com/i18n.git` |
| `dart-sdk-new/third_party/pkg/leak_tracker` | `https://dart.googlesource.com/leak_tracker.git` |
| `dart-sdk-new/third_party/pkg/native` | `https://dart.googlesource.com/native.git` |
| `dart-sdk-new/third_party/pkg/protobuf` | `https://dart.googlesource.com/protobuf.git` |
| `dart-sdk-new/third_party/pkg/pub` | `https://dart.googlesource.com/pub.git` |
| `dart-sdk-new/third_party/pkg/shelf` | `https://dart.googlesource.com/shelf.git` |
| `dart-sdk-new/third_party/pkg/sync_http` | `https://dart.googlesource.com/sync_http.git` |
| `dart-sdk-new/third_party/pkg/tar` | `https://dart.googlesource.com/external/github.com/simolus3/tar.git` |
| `dart-sdk-new/third_party/pkg/test` | `https://dart.googlesource.com/test.git` |
| `dart-sdk-new/third_party/pkg/tools` | `https://dart.googlesource.com/tools.git` |
| `dart-sdk-new/third_party/pkg/vector_math` | `https://dart.googlesource.com/external/github.com/google/vector_math.dart.git` |
| `dart-sdk-new/third_party/pkg/web` | `https://dart.googlesource.com/web.git` |
| `dart-sdk-new/third_party/pkg/webdev` | `https://dart.googlesource.com/webdev.git` |
| `dart-sdk-new/third_party/pkg/webdriver` | `https://dart.googlesource.com/external/github.com/google/webdriver.dart.git` |
| `dart-sdk-new/third_party/pkg/webkit_inspection_protocol` | `https://dart.googlesource.com/external/github.com/google/webkit_inspection_protocol.dart.git` |
| `dart-sdk-new/third_party/ply` | `https://chromium.googlesource.com/chromium/src/third_party/ply.git` |
| `dart-sdk-new/third_party/WebCore` | `https://dart.googlesource.com/webcore.git` |
| `dart-sdk-new/third_party/zlib` | `https://chromium.googlesource.com/chromium/src/third_party/zlib.git` |

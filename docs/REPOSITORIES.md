# Repository Inventory

Last verified: 2026-10-28.

The October update includes Shorebird upstream main `5f04c3ac` (version
`1.6.134`, ahead of the latest upstream `v1.6.133` tag), Shorebird Flutter
`88353151`, and official Flutter stable `737ba8f8`. CI verifies upstream
Shorebird commit ancestry directly so unreleased upstream source can be built.

## Active Top-Level Repositories

| Path | Remote URL | Branch | Pinned commit |
| --- | --- | --- | --- |
| `dart-sdk` | `https://github.com/tony-cloud/dart-sdk.git` | `tonycloud/dev` | `9b8279462bac6d831926e239baa4a65e97edd74a` |
| `depot_tools` | `https://chromium.googlesource.com/chromium/tools/depot_tools.git` | `main` | `e154c8eda5e63cbe85a765ae9d06e2b7af05139e` |
| `flutter` | `https://github.com/tony-cloud/flutter.git` | `tonycloud/dev` | `863ab478e67c7f5b174520ccf5ea5e4e484dbb64` |
| `shorebird` | `https://github.com/tony-cloud/shorebird.git` | `main` | `ce38adafeb8dbcfb1d5d504b79391d6df0bf8ad2` |
| `shorebird-server` | `https://github.com/tony-cloud/shorebird-server.git` | `main` | `d6e5a39546905b85e2345b8058c4350ed236184c` |
| `updater` | `https://github.com/tony-cloud/shorebird-updater.git` | `main` | `fe8af499ba6e84b92c3091e25a3a1ddd8068c013` |

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

These repositories currently exist under top-level checkouts. They are managed
by the owning repository's DEPS/gclient workflow, not by the root `.gitmodules`
file.

| Path | Remote URL |
| --- | --- |
| `dart-sdk/buildtools/clang_format/script` | `https://chromium.googlesource.com/chromium/llvm-project/cfe/tools/clang-format.git` |
| `dart-sdk/tests/co19/src` | `https://dart.googlesource.com/co19` |
| `dart-sdk/third_party/binaryen/src` | `https://chromium.googlesource.com/external/github.com/WebAssembly/binaryen.git` |
| `dart-sdk/third_party/boringssl/src` | `https://boringssl.googlesource.com/boringssl.git` |
| `dart-sdk/third_party/cpu_features/src` | `https://chromium.googlesource.com/external/github.com/google/cpu_features.git` |
| `dart-sdk/third_party/crashpad/crashpad` | `https://chromium.googlesource.com/crashpad/crashpad.git` |
| `dart-sdk/third_party/cygwin` | `https://chromium.googlesource.com/chromium/deps/cygwin.git` |
| `dart-sdk/third_party/emsdk` | `https://dart.googlesource.com/external/github.com/emscripten-core/emsdk.git` |
| `dart-sdk/third_party/googletest` | `https://fuchsia.googlesource.com/third_party/googletest` |
| `dart-sdk/third_party/icu` | `https://chromium.googlesource.com/chromium/deps/icu.git` |
| `dart-sdk/third_party/jinja2` | `https://chromium.googlesource.com/chromium/src/third_party/jinja2.git` |
| `dart-sdk/third_party/libc` | `https://llvm.googlesource.com/llvm-project/libc` |
| `dart-sdk/third_party/libcxx` | `https://llvm.googlesource.com/llvm-project/libcxx` |
| `dart-sdk/third_party/libcxxabi` | `https://llvm.googlesource.com/llvm-project/libcxxabi` |
| `dart-sdk/third_party/markupsafe` | `https://chromium.googlesource.com/chromium/src/third_party/markupsafe.git` |
| `dart-sdk/third_party/mini_chromium/mini_chromium` | `https://chromium.googlesource.com/chromium/mini_chromium` |
| `dart-sdk/third_party/perfetto/src` | `https://chromium.googlesource.com/external/github.com/google/perfetto` |
| `dart-sdk/third_party/pkg/core` | `https://dart.googlesource.com/core.git` |
| `dart-sdk/third_party/pkg/dart_style` | `https://dart.googlesource.com/dart_style.git` |
| `dart-sdk/third_party/pkg/dartdoc` | `https://dart.googlesource.com/dartdoc.git` |
| `dart-sdk/third_party/pkg/ecosystem` | `https://dart.googlesource.com/ecosystem.git` |
| `dart-sdk/third_party/pkg/http` | `https://dart.googlesource.com/http.git` |
| `dart-sdk/third_party/pkg/i18n` | `https://dart.googlesource.com/i18n.git` |
| `dart-sdk/third_party/pkg/leak_tracker` | `https://dart.googlesource.com/leak_tracker.git` |
| `dart-sdk/third_party/pkg/native` | `https://dart.googlesource.com/native.git` |
| `dart-sdk/third_party/pkg/protobuf` | `https://dart.googlesource.com/protobuf.git` |
| `dart-sdk/third_party/pkg/pub` | `https://dart.googlesource.com/pub.git` |
| `dart-sdk/third_party/pkg/shelf` | `https://dart.googlesource.com/shelf.git` |
| `dart-sdk/third_party/pkg/sync_http` | `https://dart.googlesource.com/sync_http.git` |
| `dart-sdk/third_party/pkg/tar` | `https://dart.googlesource.com/external/github.com/simolus3/tar.git` |
| `dart-sdk/third_party/pkg/test` | `https://dart.googlesource.com/test.git` |
| `dart-sdk/third_party/pkg/tools` | `https://dart.googlesource.com/tools.git` |
| `dart-sdk/third_party/pkg/vector_math` | `https://dart.googlesource.com/external/github.com/google/vector_math.dart.git` |
| `dart-sdk/third_party/pkg/web` | `https://dart.googlesource.com/web.git` |
| `dart-sdk/third_party/pkg/webdev` | `https://dart.googlesource.com/webdev.git` |
| `dart-sdk/third_party/pkg/webdriver` | `https://dart.googlesource.com/external/github.com/google/webdriver.dart.git` |
| `dart-sdk/third_party/pkg/webkit_inspection_protocol` | `https://dart.googlesource.com/external/github.com/google/webkit_inspection_protocol.dart.git` |
| `dart-sdk/third_party/ply` | `https://chromium.googlesource.com/chromium/src/third_party/ply.git` |
| `dart-sdk/third_party/WebCore` | `https://dart.googlesource.com/webcore.git` |
| `dart-sdk/third_party/zlib` | `https://chromium.googlesource.com/chromium/src/third_party/zlib.git` |

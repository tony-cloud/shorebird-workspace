# Open Shorebird Workspace

This repository is a meta-workspace for the open Shorebird replacement work. It
pins the source repositories used by the SDK, Flutter/engine, CLI, updater, and
self-hosted server work as git submodules, while keeping local test fixtures and
platform scripts in this root repository.

## Submodules

| Path | Role | Remote | Branch |
| --- | --- | --- | --- |
| `dart-sdk-new` | Dart SDK fork with AOT patch runtime work | `https://git.tonycloud.org/dart-lang/sdk.git` | `tonycloud/dev` |
| `flutter` | Flutter fork used by app and engine integration tests; engine sources live under `flutter/engine/src/flutter` | `https://git.tonycloud.org/flutter/flutter.git` | `shorebird/dev` |
| `shorebird` | CLI, protocol/client packages, open patch tools | `https://git.tonycloud.org/flutter/shorebird.git` | `main` |
| `shorebird-server` | Self-hosted CodePush/auth/management server | `https://git.tonycloud.org/flutter/shorebird-server.git` | `main` |
| `updater` | Runtime updater and patch package tooling | `https://git.tonycloud.org/flutter/shorebird-updater.git` | `main` |
| `depot_tools` | Chromium/Dart checkout tooling | `https://chromium.googlesource.com/chromium/tools/depot_tools.git` | `main` |

The Dart SDK checkout has additional gclient-managed dependencies under
`dart-sdk-new/third_party`. They are intentionally not top-level submodules.

## First Checkout

```bash
git submodule update --init --recursive
```

For Linux:

```bash
./scripts/bootstrap_linux.sh
```

For macOS and iOS-preparation checks:

```bash
./scripts/bootstrap_macos.sh
```

By default the bootstrap scripts run `gclient sync`, which can download a large
toolchain/dependency set. Set `SKIP_GCLIENT_SYNC=1` when the checkout is already
synced or when you only want source-level tests.

## Local Test Fixture

`testapps/license_flavor_patch_test` is kept in the root repository because it
tests the open AOT patch and license/flavor behavior across the nested repos.
Generated build output under that app is ignored by this meta-repository.

## Documents

- `docs/REPOSITORIES.md` records the remote URL inventory and cleanup policy.
- `docs/PLATFORM_TESTING.md` documents the Linux, macOS, and later iOS test
  flow.

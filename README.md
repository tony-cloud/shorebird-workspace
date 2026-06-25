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

## GitHub CI

The root workflow `.github/workflows/open-shorebird-ci.yml` is the upload-ready
CI entry point for the combined workspace. Default push and pull request runs
execute source-level checks and build distributable CLI/server artifacts:

- compiled `shorebird`, `open_aot_patch_tools`, and `artifact_proxy` archives
  for Linux x64, macOS x64/arm64, and Windows x64
- public updater `patch-*.zip` mirror artifacts for the CLI cache
- `mirror-metadata` with `artifacts_manifest.yaml` for the artifact proxy
- self-hosted `shorebird-server` archives for Linux, macOS, and Windows

Default push and pull request runs also build the large SDK and engine outputs:
patched Dart SDK archives for Linux x64 and macOS arm64, Linux x64 desktop
engine artifacts, Android arm64 engine artifacts, Flutter web SDK artifacts,
and Apple iOS/macOS engine artifacts. A successful full SDK run also uploads
`open-shorebird-artifact-mirror`, a publish-ready mirror archive assembled from
the produced patch-tool, metadata, engine, and web artifacts, plus
`open-shorebird-release-manifest`, a checksum-verified provenance index for the
CLI, server, SDK, engine, and mirror archives. Manual `workflow_dispatch` runs
keep `full_sdk_build=true` by default; set it to `false` only when you want a
source/CLI/server-only run.
Use `scripts/validate_release_manifest.py` to audit a downloaded manifest
against the downloaded workflow artifacts before publishing or mirroring them.
The wrapper `scripts/verify_downloaded_release_artifacts.sh` runs that manifest
sidecar check, verifies the mirror archive sidecar, and validates the extracted
artifact mirror from a downloaded workflow run.
Before pushing the workspace and submodule forks, run
`scripts/verify_upload_readiness.sh`; it fails if any required CI support file
is untracked or if a required root/submodule checkout still has uncommitted
changes.
After upload, `scripts/verify_hosted_full_sdk_build.sh --repo owner/repo --ref main`
dispatches the hosted full SDK workflow, waits for it, downloads artifacts, and
runs the downloaded-release verifier. It uses `gh` when available, or the
GitHub REST API with `GITHUB_TOKEN`/`GH_TOKEN`, `curl`, `jq`, and `unzip`.
The heavy SDK/engine jobs default to managed GitHub-hosted runners
`ubuntu-latest` and `macos-latest`, then run an early disk-capacity preflight.
Override `linux_heavy_runner` / `macos_heavy_runner` only when you want larger
or self-hosted runners. The dispatch inputs `sdk_min_free_disk_gb` and
`engine_min_free_disk_gb` control the preflight thresholds.
The CI contract is validated by
`scripts/verify_ci_workflow.sh`; it rejects `dart_dynamic_modules=true`,
legacy `aot-tools.dill` publishing, missing checksum sidecars, and missing
required artifacts.
Manual `workflow_dispatch` runs with `run_runtime_smokes=true` run the Android
and Linux seeded runtime patch smokes on provisioned runners. These jobs are not
part of default push/PR CI because they require local engine build directories;
the Android smoke also requires an attached device or emulator.
The open CLI defaults vended Flutter installs to this workspace's open Flutter
fork and can be pointed at a future GitHub mirror with
`SHOREBIRD_FLUTTER_GIT_URL`. When no `base_url` or hosted URL override is
configured, API/auth traffic defaults to the local self-hosted server at
`http://localhost:8080` instead of Shorebird's hosted service.
The bundled updater library uses the same default when `shorebird.yaml` does not
include `base_url`.
CLI-managed patch-tool artifact downloads default to the local open mirror root
`http://localhost:8080/artifacts`; set `SHOREBIRD_ARTIFACT_BASE_URL` for a
public mirror populated with the CI-produced `patch-*.zip` files and
`artifacts_manifest.yaml`. The patch artifacts and manual engine artifacts
include publish-ready `shorebird/` mirror subtrees that can be copied to that
same mirror root. Full SDK workflow runs upload the already assembled
`open-shorebird-artifact-mirror` archive; after downloading workflow artifacts
manually, use
`scripts/assemble_artifact_mirror.sh <downloaded-artifacts-dir> <mirror-root>`
to merge those subtrees, reject conflicting artifact bytes, validate
`artifacts_manifest.yaml` overrides, verify all platform `patch-*.zip` files,
and write missing checksum sidecars. If your open engine revision differs from
the upstream Flutter engine revision used for unchanged artifacts, set the
workflow `base_flutter_engine_revision` input when generating mirror metadata.
Vended Flutter commands receive `FLUTTER_STORAGE_BASE_URL` from
`SHOREBIRD_FLUTTER_STORAGE_BASE_URL`, falling back to
`http://localhost:8080/download.flutter.io` for local self-hosted artifacts.
The Flutter fork itself uses that same open mirror as its default engine and
Android Maven artifact host when `FLUTTER_STORAGE_BASE_URL` is unset, including
its Dart SDK refresh scripts.

The iOS App Store candidate route is the encrypted Dart bytecode interpreter
path, not native AOT patch loading. Real-device testing on June 25, 2026 showed
the patched iPad app displaying `license:pro` without enabling
`DART_DYNAMIC_MODULES`. The macOS native-AOT desktop route was also verified
locally the same day by launching a saved free app with a pro Flutter snapshot
seeded as patch `1`; the patched app reported `license:pro`.
CI builds Linux, Android, and macOS engine artifacts with the native AOT patch
runtime enabled and the interpreter route disabled; iOS is the only App
Store-safe interpreter patch artifact path.
Android release APK builds pass locally with the custom Android engine; use
`scripts/android_runtime_patch_smoke.sh` on a machine with an attached Android
device or emulator to run the seeded runtime patch proof. Use
`scripts/linux_runtime_patch_smoke.sh` on a Linux desktop runner to exercise the
same saved-free-app/pro-patch flow for the Linux embedder.

## Local Test Fixture

`testapps/license_flavor_patch_test` is kept in the root repository because it
tests the open AOT patch and license/flavor behavior across the nested repos.
Generated build output under that app is ignored by this meta-repository.

## Documents

- `docs/CI.md` describes the GitHub Actions jobs for source checks, CLI/server
  artifacts, manual custom SDK/engine builds, and opt-in runtime smokes.
- `docs/REPOSITORIES.md` records the remote URL inventory and cleanup policy.
- `docs/PLATFORM_TESTING.md` documents the Linux, macOS, and later iOS test
  flow.

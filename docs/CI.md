# GitHub CI

The root workflow at `.github/workflows/open-shorebird-ci.yml` validates the
combined open Shorebird workspace rather than any single submodule in isolation.

## Default CI

Push and pull request runs perform source-level checks and build distributable
binaries:

1. `source-checks` runs the bootstrap test path with `SKIP_GCLIENT_SYNC=1`,
   including CLI, code push client, artifact proxy, updater library, open patch
   tooling, and server tests.
   This checks the Shorebird CLI focused tests, code push client open-server
   default tests, open patch tools, server tests, and the root shell scripts
   without downloading the full engine dependency graph. It also runs
   `scripts/verify_ci_workflow.sh`, which parses the
   workflow YAML, checks every workflow `run:` block with `bash -n`, and
   verifies the expected open SDK/CLI/server artifact contract, including
   archive creation and checksum sidecar generation. It also checks
   `.gitmodules`, `docs/REPOSITORIES.md`, and generated `.gclient` files so a
   fresh GitHub checkout uses open HTTPS remotes instead of local paths, SSH
   remotes, or official closed Shorebird repositories. The verifier also scans
   runtime/build-sensitive CLI, updater, artifact-proxy, Flutter tool, Gradle,
   engine metadata, and web UI artifact-copy sources to reject hosted Shorebird
   endpoints and official `shorebirdtech` GitHub dependencies. It also reads
   the forked engine BUILD files and checks that the GN targets and archive
   names referenced by the manual engine jobs still exist.
   Before uploading the workspace, run `scripts/verify_upload_readiness.sh`.
   It invokes the same workflow contract verifier in upload-ready mode, which
   requires every listed CI support file to be tracked in its owning checkout
   and requires the root plus required submodule checkouts to be clean.
   `scripts/verify_sync_open_sources.sh` also exercises the source-link helper
   in an isolated temporary workspace so clean generated engine checkouts are
   replaced by the workspace Dart/updater submodules and dirty targets are not
   overwritten.
   `scripts/verify_open_infrastructure_defaults.sh` independently scans
   build-sensitive Flutter, Shorebird CLI, artifact-proxy, updater, `.gclient`,
   and submodule metadata for closed Shorebird endpoints, official
   `shorebirdtech` dependency remotes, and private prebuilt buckets, while
   requiring the open local server and artifact mirror defaults.
   `scripts/verify_powershell_open_defaults.sh` checks the Windows launcher and
   Flutter Dart SDK updater PowerShell scripts for open Flutter/artifact
   defaults and parses them with `pwsh` when it is available.
   `scripts/verify_write_sha256.sh` smoke-tests the portable checksum helper
   used by every artifact job.
   `scripts/verify_ios_interpreter_route_validator.sh` smoke-tests the iOS
   App Store route gate with synthetic GN args and encrypted patch artifacts,
   covering a valid interpreter artifact and rejecting dynamic-module,
   non-iOS, malformed JSON, and Mach-O native patch inputs.
2. All artifact and runtime-smoke jobs declare `needs: source-checks`, so no
   uploaded CLI, server, SDK, or engine artifact is produced before the
   source-level open-replacement contract passes.
3. `cli-artifacts` compiles:
   - `shorebird` for Linux x64, macOS x64, macOS arm64, and Windows x64
   - `open_aot_patch_tools` for Linux x64, macOS x64, macOS arm64, and
     Windows x64
   - `artifact_proxy` for Linux x64, macOS x64, macOS arm64, and Windows x64
   - public updater `patch` binaries packaged as CLI-cache mirror artifacts:
     `patch-linux-x64.zip`, `patch-darwin-x64.zip`,
     `patch-darwin-arm64.zip`, and `patch-windows-x64.zip`
   - `mirror-metadata`, containing
     `shorebird/<engine-revision>/artifacts_manifest.yaml`
   The job provisions stable Rust before building the updater `patch` binary,
   so those mirror artifacts do not depend on preinstalled runner state.
   CI opens each generated patch mirror ZIP before upload and verifies it has
   exactly the cache-facing entry name (`patch` on Unix, `patch.exe` on
   Windows) with a non-empty payload.
   Each CLI upload is an `open-shorebird-cli-<os>-<arch>.tar.gz` archive with
   `bin/shorebird`, `bin/open_aot_patch_tools`, `bin/artifact_proxy`,
   `bin/internal` version metadata, `manifest.json`, and a `.sha256` sidecar.
   The manifest records the Flutter revision and engine revision used by the
   bundled CLI cache metadata. The macOS CLI split is intentional: macOS x64
   runs on `macos-15-intel`, and macOS arm64 runs on `macos-14`. Before upload,
   CI extracts the archive and runs `shorebird --version`,
   `open_aot_patch_tools --help`, and `artifact_proxy --health-check` from the
   extracted layout. The `shorebird --version` smoke must report the open
   `https://git.tonycloud.org/flutter/shorebird.git` fork and rejects the old
   official Shorebird SSH remote.
4. `server-artifacts` runs `go test ./...` and cross-compiles
   `shorebird-server` for Linux x64/arm64, macOS x64/arm64, and Windows x64
   targets. Each uploaded archive contains the server binary, `web/` dashboard
   assets, `.env.example`, `README.md`, `openapi.yaml`, `manifest.json`, and a
   `.sha256` sidecar. The manifest records both the root workflow commit and
   the server submodule commit. Keep `web/` beside the executable when
   unpacking; the server looks for dashboard assets relative to its working
   directory and executable path.
   The Linux x64 archive is extracted and smoke-tested in CI by starting the
   packaged binary with SQLite/local storage, checking `/health`, fetching the
   dashboard HTML from `/`, and verifying `/openapi.yaml`.

The CLI/server artifacts are uploaded from each workflow run. CLI archives are
intended for installation or release attachments; patch mirror ZIPs are separate
because the Shorebird CLI cache expects the `patch-*.zip` names and a `patch`
or `patch.exe` entry inside each ZIP. The patch mirror ZIPs include `.sha256`
sidecars and can be copied under
`/shorebird/<engine-revision>/` on the host referenced by
`SHOREBIRD_ARTIFACT_BASE_URL`. Each `mirror-patch-*.zip` GitHub artifact also
includes a publish-ready `shorebird/<engine-revision>/patch-*.zip` copy and
matching `.sha256` sidecar, so mirror publication can copy the `shorebird/`
subtree directly. The `mirror-metadata` artifact provides the matching
`artifacts_manifest.yaml` for the artifact proxy.
Manual engine artifacts also include a `mirror/` subtree whose contents are
already laid out under `shorebird/flutter_infra_release/...`; copy that subtree
to the same artifact mirror root to satisfy the generated
`artifacts_manifest.yaml` overrides.
On `full_sdk_build=true` runs, `artifact-mirror` downloads the mirror metadata,
all platform patch ZIPs, and the Linux/Android/web/iOS/macOS engine artifacts,
then uploads `open-shorebird-artifact-mirror`. It also downloads the CLI,
server, and custom Dart SDK artifacts and uploads
`open-shorebird-release-manifest`, a JSON manifest that verifies every archived
release artifact has a matching checksum sidecar and records its SHA-256 digest,
size, relative path, downloaded artifact group, file name, and workflow commit.
Zero-byte release artifacts are rejected even when their sidecars match.
The manifest job also requires every
expected CLI, server, custom Dart SDK, engine, patch-tool, metadata, and mirror
artifact family to be present. The mirror archive is the checked publish-ready
mirror root for `SHOREBIRD_ARTIFACT_BASE_URL`; the release manifest is the
provenance index for the full SDK/CLI/server/engine output set.
The artifact-mirror job validates the generated manifest with
`scripts/validate_release_manifest.py` before upload. After downloading workflow
artifacts, the same validator can audit the manifest against the downloaded
artifact directory and will reject missing files, duplicate or unsafe paths,
checksum/size mismatches, sidecar mismatches, orphan sidecars, mismatched
artifact group or file-name provenance fields, and artifacts that are present on
disk but missing from the manifest.
Use the wrapper below after downloading a completed `full_sdk_build=true` run:

```sh
gh run download <run-id> --dir downloaded-artifacts
./scripts/verify_downloaded_release_artifacts.sh \
  --github-sha <run-head-sha> \
  downloaded-artifacts
```

It verifies the release manifest sidecar, validates the manifest against every
downloaded artifact, optionally requires the manifest commit to match the
expected run SHA, verifies the mirror archive sidecar, safely extracts
`open-shorebird-artifact-mirror.tar.gz` after rejecting unsafe archive members,
and validates the extracted mirror root before anything is published.

After the workspace is uploaded to GitHub, the end-to-end proof can be driven
from a local checkout with:

```sh
./scripts/verify_hosted_full_sdk_build.sh \
  --repo owner/repo \
  --ref main
```

That helper dispatches `open-shorebird-ci.yml` with `full_sdk_build=true`,
waits for the hosted workflow run to succeed, downloads all artifacts, and runs
`scripts/verify_downloaded_release_artifacts.sh` against the downloaded output
with the workflow run's `headSha`.
It uses GitHub CLI when `gh` is installed; otherwise it uses the GitHub REST API
with `GITHUB_TOKEN` or `GH_TOKEN` plus `curl`, `jq`, and `unzip`.
Use `--linux-heavy-runner`, `--macos-heavy-runner`, and the disk-threshold flags
when the repository uses custom larger/self-hosted runner labels.
For local release assembly or reassembly after downloading workflow artifacts,
run:

```sh
./scripts/assemble_artifact_mirror.sh downloaded-artifacts public-mirror
```

The assembler scans direct artifact contents plus `*.tar.gz` engine archives,
validates archive member paths, rejects links/devices before extraction, copies
every `shorebird/` mirror subtree into `public-mirror`, rejects conflicting
files with different bytes, validates that each
`artifacts_manifest.yaml` override resolves to a copied non-empty file, verifies
every platform `patch-*.zip` contains exactly the cache-facing `patch` or
`patch.exe` entry, and writes missing `.sha256` sidecars.
`scripts/validate_artifact_mirror.py public-mirror` can be run independently
after safely extracting `open-shorebird-artifact-mirror.tar.gz`; the
`artifact-mirror` job safe-extracts and validates the assembled archive before
upload.
`scripts/verify_assemble_artifact_mirror.sh` keeps the low-level assembler
covered in default source checks. `scripts/verify_artifact_mirror_workflow_assembly.sh`
also dry-runs the full `artifact-mirror` aggregation flow with fake downloaded
artifacts, including the mirror-only input subset, extracted mirror validation,
and release manifest requirements. This catches path-layout regressions before
the hosted `full_sdk_build=true` run.
CI writes checksum sidecars through `scripts/write_sha256.sh` so the artifact
jobs do not depend on platform-specific checksum tools.
The workflow defaults all `run` steps to Bash, including Windows matrix jobs,
because the workspace scripts and packaging commands use Bash syntax.

On June 25, 2026, the Darwin arm64 CLI artifact path was smoke-tested locally:
the workflow-equivalent commands compiled `shorebird`, `open_aot_patch_tools`,
and `artifact_proxy`; built the Rust updater `patch` binary; produced
`open-shorebird-cli-macos-arm64.tar.gz` and `patch-darwin-arm64.zip`; wrote both
checksum sidecars; extracted the CLI archive; and verified the compiled tools
could run from the extracted layout.
The patch mirror ZIP contained a single `patch` entry, matching the CLI cache
contract.

The Darwin arm64 server artifact path was smoke-tested locally the same day:
the workflow-equivalent packaging command produced
`shorebird-server-darwin-arm64.tar.gz`, wrote a `.sha256` sidecar, extracted
the archive, started the packaged server with SQLite and local storage, and
verified `/health` returned `status=ok` while `/` served the packaged dashboard
HTML from the sibling `web/` directory. The packaged server also serves the
checked OpenAPI contract at `/openapi.yaml`.

## Open Artifact Hosts

The CLI can be used with a self-hosted server and open artifact mirror without
changing source code. When no project `base_url`, user config, or environment
override is present, the open CLI defaults API/auth traffic to the local
self-hosted server at `http://localhost:8080` instead of Shorebird's hosted
service:

```sh
export SHOREBIRD_HOSTED_URL=https://updates.example.com
export SHOREBIRD_ARTIFACT_BASE_URL=https://artifacts.example.com/open
export SHOREBIRD_FLUTTER_STORAGE_BASE_URL=https://artifacts.example.com/flutter
export SHOREBIRD_FLUTTER_GIT_URL=https://github.com/example/open-flutter.git
```

`SHOREBIRD_HOSTED_URL` points API calls at the self-hosted server and is also
written into new `shorebird.yaml` files as `base_url`. The bundled updater
library also defaults to `http://localhost:8080` when a legacy app omits
`base_url`. The self-hosted server publishes its checked OpenAPI contract at
`http://localhost:8080/openapi.yaml`. The artifact mirror keeps the same path
layout as Shorebird's default storage bucket:

```text
$SHOREBIRD_ARTIFACT_BASE_URL/shorebird/<engine-revision>/patch-linux-x64.zip
$SHOREBIRD_ARTIFACT_BASE_URL/shorebird/<engine-revision>/patch-darwin-x64.zip
$SHOREBIRD_ARTIFACT_BASE_URL/shorebird/<engine-revision>/patch-darwin-arm64.zip
$SHOREBIRD_ARTIFACT_BASE_URL/shorebird/<engine-revision>/patch-windows-x64.zip
$SHOREBIRD_ARTIFACT_BASE_URL/shorebird/<engine-revision>/artifacts_manifest.yaml
```

When `SHOREBIRD_ARTIFACT_BASE_URL` is not set, the open CLI defaults CLI-managed
artifact downloads to `http://localhost:8080/artifacts`. Use that local mirror
root for development, or set `SHOREBIRD_ARTIFACT_BASE_URL` to the public mirror
where the CI-produced `patch-*.zip` artifacts are hosted.

If the mirror is a bucket-style host, `SHOREBIRD_STORAGE_BASE_URL` and
`SHOREBIRD_STORAGE_BUCKET` can be used instead of
`SHOREBIRD_ARTIFACT_BASE_URL`. `SHOREBIRD_FLUTTER_STORAGE_BASE_URL` controls the
`FLUTTER_STORAGE_BASE_URL` passed to vended Flutter commands; when unset it
defaults to `http://localhost:8080/download.flutter.io`.
`SHOREBIRD_FLUTTER_GIT_URL` controls where the CLI clones vended Flutter
revisions during cache installation. When unset, the open CLI defaults to this
workspace's open Flutter fork instead of `github.com/shorebirdtech/flutter.git`.
The Flutter fork also defaults its own engine downloads, Dart SDK refresh
scripts, doctor network check, Android Gradle Maven host, Android host-app
integration fixtures, docs artifact scripts, and Shorebird integration tests to
`http://localhost:8080/download.flutter.io` when `FLUTTER_STORAGE_BASE_URL` is
unset, so direct Flutter use does not fall back to Shorebird's closed artifact
host.

The open CI currently does not publish `aot-tools.dill`. That file is a
different Shorebird linker artifact used by the legacy native-AOT iOS linker
path. The open App Store-safe iOS path uses encrypted Dart bytecode interpreter
artifacts instead, and Android/Linux/macOS/Windows patch creation uses the
public updater `patch` binaries above.
The CLI cache does not download `aot-tools.dill` by default. Set
`SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS=1` only when deliberately validating the
development-only legacy native-AOT linker path.

Runtime smoke scripts that need external targets are kept out of default CI:
`scripts/android_runtime_patch_smoke.sh` requires an attached Android
device/emulator with app-private storage access, and
`scripts/linux_runtime_patch_smoke.sh` requires a Linux desktop runtime or
`xvfb-run`. Use them on suitable self-hosted runners when extending runtime
coverage beyond the artifact build matrix.

Use `workflow_dispatch` with `run_runtime_smokes=true` to run those external
runtime smokes from CI. The runtime jobs are intentionally separate from
`full_sdk_build=true`: they are for provisioned runners that already have the
matching local engine build outputs in the checkout workspace. Runtime checkout
uses `clean: false` so self-hosted runners can preserve those `out/` directories
between preparation and smoke runs.

| Input | Default | Used by |
| --- | --- | --- |
| `run_runtime_smokes` | `false` | Enables `linux-runtime-smoke` and `android-runtime-smoke` |
| `linux_runtime_runner` | `self-hosted` | Runner label for `scripts/linux_runtime_patch_smoke.sh` |
| `android_runtime_runner` | `self-hosted` | Runner label for `scripts/android_runtime_patch_smoke.sh` |
| `android_serial` | empty | Optional `adb` serial passed as `ANDROID_SERIAL` |

The Linux runtime runner must provide
`flutter/engine/src/out/linux_release_x64`. The Android runtime runner must provide
`flutter/engine/src/out/android_release_arm64` and
`flutter/engine/src/out/host_release_arm64`, plus an Android target where either
`adb shell run-as` works for the test package or `adb root` works. It must also
provide Java on `PATH` because the smoke builds APKs before seeding the patch.

## Heavy SDK Builds

Default push and pull request runs build the custom SDK and engine artifacts.
Manual `workflow_dispatch` runs also build them by default because
`full_sdk_build=true` is the default input value; set `full_sdk_build=false`
only for source/CLI/server-only manual runs.

The heavy jobs install Chromium's `depot_tools` into the workflow workspace
before running `gclient`, so they can bootstrap from a clean runner. The
workflow defaults heavy jobs to managed GitHub-hosted runners:

- `ubuntu-latest` for Linux SDK, Linux engine, Android engine, and web SDK
  builds
- `macos-latest` for macOS Dart SDK, iOS engine, and macOS engine builds

Override `linux_heavy_runner` / `macos_heavy_runner` when a repository wants
larger or self-hosted runners. Every SDK/engine job also runs
`scripts/check_ci_capacity.sh` before `gclient sync` or `ninja`; SDK-only jobs
and engine jobs require at least 8 GiB free by default. Raise the dispatch
thresholds for runner images where a larger preflight budget should be enforced.
The hosted Android engine job also provisions Temurin Java 17 before building
Android JAR/APK-related engine artifacts. Every SDK/engine job verifies
`python3`, `gclient`, and `ninja` before generating build files, so
PATH/toolchain problems fail before a long build starts. CI sets
`DEPOT_TOOLS_UPDATE=0`, and the local bootstrap scripts default the same way,
so depot_tools uses the pinned submodule revision unless explicitly overridden.

Manual dispatch accepts these workflow inputs:

| Input | Default | Used by |
| --- | --- | --- |
| `linux_heavy_runner` | `ubuntu-latest` | `custom-dart-sdk`, `linux-engine`, `android-engine`, `web-sdk` |
| `macos_heavy_runner` | `macos-latest` | `custom-dart-sdk-macos`, `ios-engine` / Apple engine artifacts |
| `sdk_min_free_disk_gb` | `8` | Minimum free disk GiB for `custom-dart-sdk` and `custom-dart-sdk-macos` |
| `engine_min_free_disk_gb` | `8` | Minimum free disk GiB for Linux, Android, web, iOS, and macOS engine builds |
| `base_flutter_engine_revision` | empty | Optional upstream Flutter engine revision recorded in `artifacts_manifest.yaml` for non-overridden artifact proxy fallbacks |

Use these inputs to move the SDK/engine jobs onto different larger or
self-hosted runners without editing the workflow file. The Linux heavy jobs also run
`scripts/free_ci_disk_linux.sh`, which removes unrelated preinstalled toolchain
caches only when `GITHUB_ACTIONS=true` and `RUNNER_ENVIRONMENT=github-hosted`.
Self-hosted runners skip that cleanup by default. Set `CI_FREE_DISK_SPACE=0` to
skip cleanup anywhere, or `CI_FREE_DISK_SPACE_FORCE=1` to opt in on a
self-hosted runner.
When `base_flutter_engine_revision` is empty, `mirror-metadata` records the
same revision as `flutter/bin/internal/engine.version`. Set it when the open
Shorebird engine revision is a custom fork revision and unchanged artifacts
should be proxied back to a different upstream Flutter engine revision.

The engine jobs perform two syncs when `run_gclient_sync=true`: the root
workspace sync for the Dart SDK checkout, then a second sync from `flutter/` for
Flutter engine dependencies written by `scripts/write_gclient.sh`. Linux engine
jobs use `INCLUDE_ENGINE_DEPS=1` to include Flutter engine dependencies,
Android dependencies, and emsdk; the Apple engine job uses the same flag to
include iOS, Android, and emsdk
dependencies on macOS.
Before building, `scripts/sync_open_sources.sh` links
`flutter/engine/src/flutter/third_party/dart` to the `dart-sdk` submodule
and `flutter/engine/src/flutter/third_party/updater` to the `updater`
submodule. It does not clone the official `shorebirdtech/updater` repository by
default; set `UPDATER_URL` explicitly only when testing a different updater
fork. The helper rejects known unpatched upstream Dart SDK remotes and official
Shorebird updater remotes so CI does not accidentally build from the wrong
component checkout.
The Flutter fork's `DEPS` file also points its Dart SDK and updater dependency
URLs at the open mirrors and does not reference Shorebird's private
`shorebird-dart-sdk-prebuilt` bucket; custom SDK jobs build and archive the
open Dart SDK fork directly.

`custom-dart-sdk` and `custom-dart-sdk-macos` build the Dart SDK fork with:

```gn
dart_dynamic_modules = false
dart_enable_aot_patching = true
dart_enable_shorebird_interpreter = true
```

They run `scripts/verify_dart_sdk_args.sh`, then the focused VM patch API
tests. The Linux job uploads `custom-dart-sdk-linux-x64`; the macOS job uploads
`custom-dart-sdk-macos-arm64`. Each archive contains the Dart SDK,
`gen_snapshot`, `dartaotruntime`, `args.gn`, and a `manifest.json` that records
the root workflow commit, Dart SDK source commit, and patch-related build
flags. The workflow also uploads `.sha256` sidecars for the SDK archives.
Before writing the checksum, each SDK job extracts the archive, verifies
`manifest.json`, `args.gn`, `gen_snapshot`, `dartaotruntime`, and
`dart-sdk/bin/dart`, then runs the extracted `dart --version`.

`ios-engine` builds:

- `host_release_arm64` with `--shorebird-interpreter`,
  `dart_dynamic_modules=false`, `dart_enable_aot_patching=true`, and
  `dart_enable_shorebird_interpreter=true`
- `ios_release` with `--shorebird-interpreter`
- `macos_release_arm64` with `dart_dynamic_modules=false`,
  `dart_enable_aot_patching=true`, `shorebird_enable_aot_patching=true`, and
  `shorebird_use_interpreter=false`

It runs `scripts/verify_ios_interpreter_route.sh` before uploading the
`ios-interpreter-engine` artifact, so the archived build proves
`DART_DYNAMIC_MODULES` is off and the iOS route uses the bytecode interpreter
instead of native AOT patch loading. The archive includes `Flutter.framework`,
`Flutter.xcframework`, a mirror-ready `ios-release/artifacts.zip` containing
the xcframework plus `gen_snapshot_arm64`, `analyze_snapshot_arm64`, and the
code-sign configuration files Flutter expects, the
`host_release_arm64/gen_snapshot` binary built with `--shorebird-interpreter`,
the iOS and host `args.gn` files, and a `manifest.json` that records
`dart_dynamic_modules=false`,
`dart_enable_aot_patching=true`, `dart_enable_shorebird_interpreter=true`,
`shorebird_enable_aot_patching=false`, `shorebird_use_interpreter=true`, and
the Flutter engine revision; the workflow also uploads a `.sha256` sidecar for
the engine archive.
Before checksum upload, CI extracts the iOS engine archive and verifies
`Flutter.framework`, `Flutter.xcframework`, `ios-release/artifacts.zip`, the
mirror copy under `mirror/shorebird/flutter_infra_release/flutter/<engine>`,
both args files, `manifest.json`, and the executable host `gen_snapshot`.
When an app or IPA is supplied to the gate, strict mode also rejects bundled
patch payloads, executable-memory entitlements, and raw `aot_patch_key_hex`
material in `shorebird.yaml`.

The same Apple job uploads a separate `macos-engine-arm64` artifact containing
`FlutterMacOS.framework.zip`, `flutter_patched_sdk_product.zip`, the macOS
`args.gn`, a mirror-ready copy of the macOS framework override, a manifest with
the Flutter engine revision, and a `.sha256` sidecar. The shared
`flutter_patched_sdk_product.zip` mirror override is published only by the Linux
engine job to avoid duplicate producers for the same mirror path. The macOS
build is checked with
`scripts/verify_engine_args.sh` so CI fails if `dart_dynamic_modules=true`
appears or if the native AOT patch runtime flags are missing from the generated
args. CI extracts the macOS engine archive before upload and verifies the
framework zip, patched SDK zip, args file, manifest, and framework mirror
subtree.

`linux-engine` builds `linux_release_x64` on Ubuntu with
`dart_dynamic_modules=false`, `dart_enable_aot_patching=true`,
`shorebird_enable_aot_patching=true`, and `shorebird_use_interpreter=false`. It
uploads `linux-x64-flutter-gtk.zip`, `flutter_patched_sdk_product.zip`,
`artifacts.zip`, mirror-ready copies of those engine override files, `args.gn`,
a manifest with the Flutter engine revision, and a `.sha256` sidecar. CI
extracts the archive before upload and verifies the GTK zip, patched SDK zip,
artifacts zip, args file, manifest, and mirror subtree.

`android-engine` builds `android_release_arm64` on Ubuntu with
`dart_dynamic_modules=false`, `dart_enable_aot_patching=true`,
`shorebird_enable_aot_patching=true`, and `shorebird_use_interpreter=false`. It
uploads `artifacts.zip`, `symbols.zip`, `flutter.jar`, `libflutter.so`, host
`gen_snapshot_arm64`, `analyze_snapshot_arm64`, mirror-ready copies of the
Android engine override files, `args.gn`, a manifest with the Flutter engine
revision, and a `.sha256` sidecar. CI extracts the archive before upload and
verifies the Android artifacts/symbols zips, `flutter.jar`, `libflutter.so`,
host snapshot/analyzer tools, args file, manifest, and mirror subtree.

`web-sdk` builds the Flutter web SDK archive from `wasm_release` with
`dart_dynamic_modules=false` and uploads `flutter-web-sdk.zip`, a mirror-ready
copy of that SDK archive, `args.gn`, a manifest with the Flutter engine
revision, and a `.sha256` sidecar. Web is still not a Shorebird CodePush release
platform in this CLI/protocol; this job exists to keep the open Flutter SDK/web
artifacts buildable from the workspace. CI extracts the web SDK archive before
upload and verifies the SDK zip, args file, manifest, and mirror subtree.

Set `run_gclient_sync=false` only for debugging a runner image that already has
all gclient-managed dependencies restored.

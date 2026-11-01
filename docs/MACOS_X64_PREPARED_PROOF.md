# Native Intel SDK FFI proof transport

This transport separates native Intel execution from local Developer ID signing.
The certificate private key stays on the signing Mac. The CI consumer has only
`contents: read`, downloads an authenticated temporary **draft** release asset,
and never builds, signs, publishes, or deletes a release.

The source SDK must first integrate the prepared-runner implementation and be
pinned by the root `dart-sdk` gitlink. The old root SDK revision does not support
this protocol. The helper refuses a missing prepared protocol, a different SDK
checkout, a dirty tracked SDK, or a source commit different from the gitlink.
The workspace commit in both transport manifests must equal the native consumer's
checked-out workflow commit. Rebuild proof inputs after changing that source pin.

## Unsigned producer

`custom-dart-sdk-macos-x64` builds `product_ffi_test`,
`product_ffi_callback_test`, and `product_full_ffi_test` alongside matching `dart`,
`gen_snapshot_product`, `vm_platform_product.dill`, and the complete SDK/frontend
tools. Format 2 also requires the top-level `dartvm` companion beside `dart`,
plus the independently hashed SDK `bin/dartvm`. A launcher-only `--version`
probe cannot establish that source compilation works. Its `macos-x64-ffi-proof-build-inputs` artifact contains:

- `macos-x64-ffi-proof-build-inputs.zip` and SHA-256 sidecar;
- `macos-x64-ffi-proof-build-inputs.manifest.json`, with root/SDK commits, target,
  and all source build-file hashes, including `args.gn`.

Format 1 archives remain bound to their original source-pinned tooling; the
format 2 validator rejects them instead of silently upgrading their provenance.

This is build evidence only. These original compiler/harness bytes are preserved.
The local signing/preparation process uses private extracted inputs and signs
private runtime copies, never the CI build outputs.

After downloading the artifact from the exact producer run, independently pin its
ZIP SHA-256. At the same integrated root/SDK source commit, extract it into a fresh
private destination:

```sh
python3 scripts/macos_x64_proof_transport.py extract-build \
  --archive /PATH/macos-x64-ffi-proof-build-inputs.zip \
  --sha256 "$BUILD_ZIP_SHA256" --workspace "$PWD" \
  --sdk-commit "$SDK_COMMIT" --destination "$PRIVATE_BUILD"
```

The helper checks the external package hash, full inventory and every file hash
before extraction. It rejects links, duplicate/case-aliased paths, traversal,
special files, undeclared entries, and reused destinations. The matching SDK
source's package dependencies must already be staged locally for its source
frontend and bytecode drivers; an absolute CI `package_config.json` is not a
portable compiler dependency setup.

## Local preparation, without runtime acceptance

Choose a fresh root `/private/tmp/shorebird-macos-x64-proof-<nonce>`, with an ASCII
nonce of 8–80 letters, digits, underscores or hyphens. Each suite uses exactly its
`callouts`, `callbacks`, or `full` child. The root and suite paths must be retained
unchanged on the native Intel runner because compiled URIs and native-assets maps
contain absolute paths.

Use the SDK author's complete preparation commands documented in
`dart-sdk/tools/native_ffi_prepared.md`. The callout example is:

```sh
python3 dart-sdk/tools/test_aot_product_ffi.py --prepare-only \
  --target-os=macos --target-arch=x64 --macos-deployment-target=14.0 \
  --build-dir="$PRIVATE_BUILD" --work-dir="$PROOF_ROOT/callouts" \
  --macos-signing-identity="$LOCAL_DEVELOPER_ID"
```

Repeat with `test_aot_product_ffi_callbacks.py` and the `callbacks` directory.
Use `test_aot_full_replacement_ffi.py` and `full`, also supplying the real
`--envelope-producer`, `--envelope-dart`, and `--envelope-packages`. The pinned
root encryption producer supports explicit macOS/x64 targeting. Signing happens
before the signed harness hash is used to compute the unchanged host ABI.

On an ARM Mac the preparation may launch x64 compiler tools through Rosetta;
this is recorded as preparation provenance and cannot establish runtime proof.
`--prepare-only` must finish with `prepared_runtime_pending`; it does not execute
any PRODUCT harness. No certificate private key or keychain is an input.

Package only the three completed immutable suite trees:

```sh
python3 scripts/macos_x64_proof_transport.py package-prepared \
  --proof-root "$PROOF_ROOT" \
  --build-manifest "$PRIVATE_BUILD/build-inputs-manifest.json" \
  --workspace "$PWD" --sdk-commit "$SDK_COMMIT" \
  --certificate-sha1 "$CERTIFICATE_SHA1" \
  --output /PRIVATE/OUTPUT/macos-x64-prepared-proof.zip
```

The public certificate fingerprint is an uppercase 40-character SHA-1. This
command creates an external SHA-256 sidecar and verifies its own result. Each
suite keeps four exact original build copies under `original-inputs/`, explicitly
`provenance-only-never-executed`, and binds them to the unsigned CI build
manifest. The separate signed runtime inventory contains 7 callout, 7 callback,
and 4 full-suite Mach-Os. Only each suite's private signed harness can execute a
case. The archive includes the exact SDK-prepared input manifests and their
complete immutable inventories; no arbitrary additional root files are accepted.

## Reviewed temporary draft and native consumer

After review, the main operator creates one temporary authenticated draft release
in the same repository and uploads the single allowlisted asset named
`macos-x64-prepared-proof.zip`. Record the release ID, asset ID, externally
computed package SHA-256, source SDK commit, certificate fingerprint, and root
workflow commit. Do not publish that release. Release creation/deletion is an
operator action and is deliberately absent from these scripts and workflow.

Dispatch `.github/workflows/macos-x64-prepared-proof.yml` at that exact integrated
root commit with the five external pins. The consumer:

1. Checks out the exact workflow SHA and its SDK gitlink, then rejects unsupported
   or mismatching source before accessing the proof asset.
2. GETs the exact release ID using `GITHUB_TOKEN`, requires `draft=true` and
   `published_at=null`, and checks that the exact asset ID is uniquely attached,
   fully uploaded, correctly named, sized, and hash-compatible.
3. Downloads via the authenticated asset API, strips Authorization on GitHub's
   signed storage redirect, verifies the external SHA-256, and rechecks draft
   publication state and membership after download.
4. Validates the full ZIP inventory before exclusively creating the declared
   canonical root; an existing root, link, alias, undeclared file, changed hash,
   wrong source/certificate or incomplete suite cannot pass.
5. Invokes only three fixed checked-out SDK drivers with `--run-prepared=...`.
   There are no shell commands or dispatch choices taken from a manifest.
   Child environments contain only system PATH, locale and timezone, with no
   GitHub/cloud/signing/DYLD credentials. The SDK re-derives the trusted cases,
   verifies actual strict signatures, empty entitlements, x64 machine type,
   certificate/team, native Intel/non-Rosetta execution, released entry ranges,
   ABI, hashes, and all exact runtime oracles before accepting each suite.
6. Requires all 8 callout, 19 callback, and 66 full cases, passing immutable-input
   reports, and a final global input/source identity check. Copies reports/logs
   to the artifact directory, then always removes only its inode-bound owned
   canonical root and uploads evidence on success or failure.

The main operator deletes the temporary draft release after the native job,
including failure. Keep the release/asset IDs and deletion receipt with the run
record. No deletion or publication is performed by the native consumer.

`native_execution_passed` is produced only by actual successful native execution.
Synthetic transport tests, unsigned build archives, local signing/preparation,
Rosetta execution, and YAML validation are separate evidence. This protocol does
not establish Flutter application, physical iOS, Windows/Linux, or server proof.

GitHub currently lists `macos-15-intel` as a standard Intel runner in its
[runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The [release asset API](https://docs.github.com/en/rest/releases/assets#get-a-release-asset)
supports authenticated download with Contents read and either a direct body or a
redirect. Authenticated draft visibility and actual native execution must still
be verified by the real reviewed run.

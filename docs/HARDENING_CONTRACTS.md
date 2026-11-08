# Hardening contracts, versions 1 and 2

Status: **v1, frozen by stage P0** of
[the hardening plan](HARDENING_AND_UPSTREAM_DEPENDENCY_PLAN.md) on 2026-11-09.
P1 (secure release admission) and P2 (official-source resolution) implement
against this document. **P1 is implemented** (2026-11-09) in the updater,
engine, flutter_tools and CLI; §7 records its decisions and its
reject/accept matrix. P2 is implemented (§8). **P3 (updater self-integrity) is
implemented** (2026-11-09); §9 records its decisions, platform adapters and
reject/accept matrix. **Contract v2 (P4, release root of trust) is
implemented client-side** (2026-11-09) in the updater, engine build,
flutter_tools, CLI and scripts; §10 records it. Contract v2 changes only the
documents listed as v2 below; every other document stays v1. The server parts
of P1–P4 are not implemented yet. Evidence that informed it is in [HARDENING_P0_EVIDENCE.md](HARDENING_P0_EVIDENCE.md).

Machine-checkable parts:

| Contract | Schema | Valid examples |
| --- | --- | --- |
| Security policy | `docs/schemas/security-policy.v1.schema.json` | `docs/schemas/examples/security-policy.legacy-default.json`, `security-policy.hardened.json` |
| Build-identity manifest | `docs/schemas/build-identity.v1.schema.json` | `docs/schemas/examples/build-identity.baseline-ae8a61a.json` |
| Artifact catalog | `docs/schemas/artifact-catalog.v1.schema.json` | `docs/schemas/examples/artifact-catalog.baseline-ae8a61a.json` |
| Build manifest (P3) | `docs/schemas/build-manifest.v1.schema.json` | `docs/schemas/examples/build-manifest.linux-x64.json` |
| Security policy v2 (P4) | `docs/schemas/security-policy.v2.schema.json` | `docs/schemas/examples/security-policy.v2.legacy-default.json`, `security-policy.v2.hardened.json` |
| Build manifest v2 (P4) | `docs/schemas/build-manifest.v2.schema.json` | `docs/schemas/examples/build-manifest.v2.linux-x64.json` |
| Release root (P4) | `docs/schemas/release-root.v2.schema.json` | `scripts/testdata/release-trust/root.v1.DEVELOPMENT-ONLY.json` (a signed envelope) |
| Release delegations (P4) | `docs/schemas/release-delegations.v2.schema.json` | `scripts/testdata/release-trust/delegations.v1.DEVELOPMENT-ONLY.json` |
| Release-trust bundle (P4) | `docs/schemas/release-trust.v2.schema.json` | `scripts/testdata/release-trust/release-trust.DEVELOPMENT-ONLY.json` |
| Release record (P4) | `docs/schemas/release-record.v2.schema.json` | `docs/schemas/examples/release-record.v2.json` |

`scripts/validate_hardening_contracts.py` checks a document against its schema
and the cross-field rules below. It has no third-party dependency: it implements
exactly the JSON Schema keywords the schemas use and refuses any other keyword.
`scripts/test_hardening_contracts.py` runs in the CI "Verify CI scripts" step.
A versioned kind (security policy, build manifest) is checked against the
schema its own `contract_version` names. Signatures of release-trust documents
are verified against a pinned root by `scripts/release_trust.py verify`, not by
the validator.

The build-identity and catalog examples carry real values from baseline run
[42464951579](https://github.com/tony-cloud/shorebird-workspace/actions/runs/42464951579)
(root `ae8a61a`). Each artifact digest comes from that run's release manifest, from
the mirror zips, or from the official Flutter origin for the two official
entries. The catalog's custom `base_url` (`artifacts.example.invalid`) and the
hardened policy's trust-root key ID are placeholders, not deployments.

## 1. Common rules

- **Versioning.** Every document carries `schema` (a fixed string) and
  `contract_version: 1`. Consumers reject unknown versions and unknown fields.
  A field may be added only in a new contract version. This matches plan §4.3:
  never append fields and assume older clients accept them.
- **Canonical JSON.** Digests and IDs hash the UTF-8 bytes of
  `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  Floats are forbidden; sizes and versions are integers. Duplicate object keys
  are rejected on input.
- **Revisions and digests.** Revisions are full 40-hex git SHAs. Digests are
  lowercase SHA-256 (`sha256:<hex>` when the value is an ID).
- **Reserved profile.** `hardened-diversified` is a valid enum value, so
  documents can name it. Contract v1 nevertheless rejects it everywhere, and
  `diversification_profile_id` must be `null`. P5 defines it in a later
  contract version.

## 2. Security profiles

| Field | `legacy` | `hardened` | `hardened-diversified` |
| --- | --- | --- | --- |
| `signatures` | `optional` or `required` (as the release shipped) | `required` | reserved |
| `patch_verification` | `strict` or `install_only` | `strict` | reserved |
| `patch_response_verification` | `optional` or `required` | `required` | reserved |
| `trust_roots` | empty unless signatures are required | ≥ 1 | reserved |
| `update_origin_policy` | `any` (HTTP allowed, as today) | `https-only` | reserved |
| `development_exceptions` | `false` (concept does not exist) | `false` in production; `true` only in visibly development builds | reserved |
| `diversification_profile_id` | `null` | `null` | non-null in a later version |
| `toolchain_id` | `null` or the build identity's ID | required: the build identity's `toolchain_id` | reserved |

Cross-field rules:

- `signatures == "required"` exactly when `trust_roots` is non-empty.
- Trust-root `key_id` values are unique.
- Production packaging (`--production` in the validator, the CLI's release admission in P1) rejects
  `development_exceptions: true`.

Field meanings:

- `signatures`, `patch_verification` and `patch_response_verification` map
  one-to-one onto today's `patch_public_key`, `patch_verification` and
  `patch_response_verification` (updater `yaml.rs`, `config.rs`).
- `trust_roots[].key_id` is `sha256:` over the DER public-key bytes, which are
  the base64-decoded `patch_public_key`. `algorithm` is `rsa-pkcs1-sha256`, the
  only algorithm the updater verifies today (`cache/signing.rs`, `ring`
  `RSA_PKCS1_2048_8192_SHA256`). `role` is `patch-signing`. P4's split roles
  require a new contract version.
- `update_origin_policy: https-only` rejects a non-`https` `base_url` and any
  HTTPS-to-HTTP redirect. A development build may allow loopback HTTP only
  with `development_exceptions: true`.
- `toolchain_id` binds the compiled policy to the source identity (§3). It
  excludes output digests, so embedding it is not self-referential (plan §5.2).

How each component identifies the profile. P1 implements the wiring; the names
here are the contract.

| Component | Identification |
| --- | --- |
| CLI | `shorebird release --security-profile=<legacy\|hardened>`, default `legacy`. The release record stores `security_profile` and `toolchain_id`. `shorebird patch` never takes the flag: it reads the release's profile and fails if the toolchain's `toolchain_id` differs. The CLI writes `security_profile` and `security_contract_version: 1` into the bundled `shorebird.yaml`. |
| Engine | GN arg `shorebird_security_profile` (`"legacy"` default) and `shorebird_toolchain_id`. Both are compiled into the engine and passed to the updater's checked initialization. Nothing at runtime (Dart, YAML, network) can select a different value. |
| Updater | A compiled policy record (this schema) supplied at build time. YAML keys `security_profile` / `security_contract_version` are a consistency input only. |
| Server | Stores the release's `security_profile` and `toolchain_id` and rejects a patch upload whose declared values differ. |

Startup admission matrix (the updater compares the compiled profile with the
`shorebird.yaml` declaration):

| Compiled | YAML declares | Result |
| --- | --- | --- |
| `legacy` | nothing or `legacy` | Legacy behavior, unchanged. |
| `legacy` | `hardened` | Initialization fails (toolchain mismatch); no patch is activated. |
| `hardened` | `hardened` with matching `security_contract_version` | Hardened checks. Any YAML knob weaker than the compiled policy fails initialization. |
| `hardened` | nothing, `legacy`, or another version | Initialization fails before keys are requested or a patch is activated. |

YAML may never lower the compiled floor. Remote configuration and Dart code
cannot change the profile.

## 3. Build-identity manifest

This manifest separates official upstream revisions from fork/toolchain
identities:

- `upstream` holds official identities: `flutter` (repository, revision,
  release tag), `flutter_engine` (the official engine revision whose
  unchanged artifacts may be served), `dart`, and optionally `shorebird` and
  `updater`.
- `fork` holds the project's sources: `workspace`, `flutter`, `dart_sdk`,
  `updater` and `shorebird_cli`, plus optional `shorebird_server`.
- `toolchain.engine_revision` is the `bin/internal/engine.version` value the
  Flutter tool uses for artifact routes.
- `artifacts` lists the output digests (name, sha256, size).

`toolchain.toolchain_id = sha256:` over the canonical JSON of
`{contract_version, security_profile, diversification_profile_id, engine_revision, fork}`.
The validator recomputes it. Changing any fork revision, the engine revision
or the profile changes the ID; output digests and upstream metadata do not.
Use the ID as the cache key for toolchain-scoped artifacts.

In the baseline, the upstream values come from source inspection:

- Flutter release 3.47.8 is `737ba8f`, from `scripts/prepare_flutter_version_tag.py`.
- Its official `engine.version` is `cf636b1`, which matches the CI default
  `BASE_FLUTTER_ENGINE_REVISION`.
- Its official `DEPS` `dart_revision` is `416e33b`.
- The Shorebird upstream is `5f04c3a`, from CI `SHOREBIRD_UPSTREAM_REVISION`.

## 4. Artifact catalog

The catalog maps every Flutter-tool artifact route of one toolchain to:

- `origin`, a key into `origins`, each with a `class` and an HTTPS `base_url`;
- `class`, either `official-unchanged` or `custom-toolchain`;
- `revision`;
- `platform`;
- `sha256` and `size`;
- `required`.

Paths use the artifact-proxy convention with `$engine`. A client downloads
`<origins[origin].base_url>/<path with $engine replaced by revision>`.

Origin classes allowed in a catalog are `official` and `custom-project`. The
evidence inventory also uses `third-party-canonical` and
`implicit-shorebird-hosted`, but those may not appear in a catalog. Rules:

- `official-unchanged` requires an `official` origin, and `revision` must equal
  `upstream_engine_revision`.
- `custom-toolchain` requires a `custom-project` origin, and `revision` must equal
  `engine_revision`.
- **An official artifact never satisfies a custom route.** A path matching the
  artifact proxy's `_requiresCustomSdkArtifact` rule cannot be
  `official-unchanged`. That rule covers Dart SDK zips, `font-subset.zip`,
  `flutter_patched_sdk_product.zip`, every `*-release/` engine route, Windows
  engine zips and `io/flutter/*_release` Maven coordinates.
  `inventory_release_binaries.REQUIRES_CUSTOM_ROUTE` mirrors the rule, and a
  unit test pins it to the Dart source.
- Routes are unique.
- When a build identity is supplied, the catalog's `toolchain_id`,
  `engine_revision`, `upstream_engine_revision`, `security_profile` and
  `diversification_profile_id` must equal it.
- `catalog_sha256`, the digest of the canonical JSON, is what a signature
  covers. The signature envelope and signer are P2/P4 decisions (plan §10:
  signing backend). A hardened toolchain must not consume an unsigned catalog
  once P2 lands. Until then the catalog is not an authority.

Relationship to today's `artifacts_manifest.yaml`: a catalog published for a
toolchain must be complete. Its `custom-toolchain` routes must equal the proxy
manifest's `artifact_overrides` for the same engine revision, and its
`upstream_engine_revision` must equal that manifest's `flutter_engine_revision`.
P2 adds this completeness check because it needs the generated proxy manifest.
The P0 baseline catalog example is an excerpt that demonstrates the rules. The
proxy remains the compatibility path until P2 proves direct resolution on every
build path.

## 5. Compatibility rules for existing releases

1. A release with no recorded `security_profile` is `legacy` forever. Its
   patches are built, signed, served and verified under the rules it shipped
   with. No wire format or response schema changes for it.
2. The profile and `toolchain_id` are fixed per native release. Every patch
   inherits them. A mismatch is rejected by the CLI before upload and by the
   server on upload.
3. Moving from `legacy` to `hardened` requires a new native release with a new
   release version or build number. A patch cannot raise or lower the profile.
4. Hardened adds requirements on today's v2 fields (`patch_public_key`,
   `patch_verification`, `patch_response_verification`, HTTPS). Contract v1
   adds no new wire fields to patch-check or selection messages. New
   messages, such as split signing roles, need a protocol capability version
   (plan §4.3).
5. Existing v2 semantics are preserved: authorized lower patch numbers at a
   newer epoch, crash recovery, and signed free-base fallback.
6. Releases built before catalogs exist keep resolving through the artifact
   proxy and `artifacts_manifest.yaml`. A catalog never retroactively
   reclassifies an existing release's artifacts.
7. A consumer that meets an unknown `contract_version`, an unknown field, or the
   reserved profile fails closed with a typed error. For an updater,
   "closed" means continuing on the bundled base without activating a patch,
   never a crash loop.

## 6. Pinned input reconciliation

Local and CI engine builds symlink `flutter/engine/src/flutter/third_party/{dart,updater}`
to the root checkouts (`scripts/sync_open_sources.sh`). The generated
`flutter/.gclient` disables those DEPS entries (`scripts/write_gclient.sh`).
Packaged CLIs derive their Flutter/engine pair from the gitlink
(`scripts/write_cli_toolchain_pins.py`). A clean gclient checkout or a
source-run CLI instead reads the duplicated pins below.
`scripts/verify_ci_workflow.rb` fails when any of them drifts.

| Duplicated pin | Must equal | Status at P0 |
| --- | --- | --- |
| `flutter/DEPS` `dart_sdk_revision` | `dart-sdk` checkout HEAD | Equal (pre-existing contract) |
| `flutter/DEPS` `updater_rev` | `updater` checkout HEAD | **Drifted**: `9d79a58` vs `b886d63`. Fixed in flutter `b25ddd7`. |
| `flutter/DEPS` `dart_sdk_git`, `updater_git` | root `.gitmodules` URLs | Equal |
| `shorebird/bin/internal/flutter.version` | `flutter` checkout HEAD | **Drifted**: `0815c97` vs `11ebb7f`. Fixed in shorebird `0a72c5a` (→ `b25ddd7`). |
| `shorebird/bin/internal/engine.version` | `flutter/bin/internal/engine.version` | Equal (`44a553c`) |
| CI `BASE_FLUTTER_ENGINE_REVISION` default | `engine.version` at `prepare_flutter_version_tag.py` `REVISION` (official 3.47.8) | Equal (`cf636b1`) |
| Dart DEPS `sdk_tag` / Flutter DEPS tool SDK `version` | each other | Equal (pre-existing contract) |

These pins are recorded but intentionally have no equality contract:

- `flutter/DEPS` `engine/src/flutter/third_party/depot_tools` (`580b4ff`) and
  the root `depot_tools` submodule (`e154c8e`) are different consumers. The
  root copy is the host `gclient` for CI and local builds. The engine's copy is
  an upstream Flutter engine dependency at its own pin. Forcing them equal
  would diverge from upstream Flutter.
- `flutter/engine/src/flutter/prebuilts/{Platforms,SDKs}` are local symlinks to
  the installed Xcode. They are host toolchain inputs, not source pins, and are
  recorded in build logs.
- `shorebird-server` has no duplicate pin.

## 7. P1 implementation decisions (secure release admission)

P1 answers the two questions P0 left open and fixes where each rule lives.
Nothing here changes a v1 schema, a wire format or a legacy default.

### 7.1 Compiled policy: build-time constants, not runtime input

The engine GN args `shorebird_security_profile` (`"legacy"` default),
`shorebird_toolchain_id`, `shorebird_security_development_exceptions` and
`shorebird_trust_root_key_ids` (`flutter/engine/src/flutter/common/config.gni`)
are the single source:

- GN asserts reject `hardened-diversified`, any unknown profile, hardened
  without a toolchain ID, and development exceptions or pinned roots outside
  hardened. Hardened adds the define `SHOREBIRD_SECURITY_HARDENED=1`.
- `build_rust_updater.py` removes any inherited `SHOREBIRD_SECURITY_*`
  variable and sets `SHOREBIRD_SECURITY_PROFILE`, `SHOREBIRD_TOOLCHAIN_ID`,
  `SHOREBIRD_SECURITY_DEVELOPMENT_EXCEPTIONS` and
  `SHOREBIRD_TRUST_ROOT_KEY_IDS` from the GN args only. The action's command
  line carries the profile, so a profile change reruns it.
- The updater's `build.rs` validates the same inputs with
  `library/src/security_inputs.rs` (shared with the library's unit tests) and
  generates `OUT_DIR/compiled_security_policy.rs`: four `const`s, the same
  bytes for the same inputs. An invalid or reserved input fails the build.
  There is no runtime setter. The only override is a `#[cfg(test)]`
  thread-local that is never compiled into a shipped library, including the
  `test-hooks` cdylib.

A generated Rust file was chosen over reading an environment variable at run
time because it is reproducible, reviewable and impossible to change after
the build. YAML, Dart (FFI) and remote configuration have no path to these
constants.

The engine cross-checks the linked library: `Updater::Init` compares
`shorebird_compiled_security_profile()` (new engine C API) with its own
compiled profile and refuses initialization on a mismatch, so a stale or
substituted updater library cannot run with weaker checks than the engine
was built for. `updater.cc` also `static_assert`s that the compiled profile
is one of the two v1 profiles.

### 7.2 Compiled floor versus release policy; trust roots

One engine build serves many apps, and each app has its own signing key. The
compiled floor is therefore {profile, toolchain ID, development exceptions,
optional pinned trust-root key IDs}. The app's trust root is the bundled
`patch_public_key`:

- In hardened, the CLI embeds the Shorebird-managed key fetched at release
  time (`SHOREBIRD_PUBLIC_KEY`).
- Customer keys (`--public-key-path`/`--public-key-cmd`) are refused in
  hardened. The server cannot sign patch-check responses for a customer key,
  and hardened requires signed responses.

An operator who builds an engine for a specific app can pin
`shorebird_trust_root_key_ids`. The updater then also requires
`sha256(DER(patch_public_key))` to be one of them.

Bundled YAML is a platform-signed package resource, but in-process code cannot
detect its replacement on a repacked app. That detection is P3.

The full `security-policy.v1` document is the per-release record: the compiled
floor plus the app's trust root. Storing it with the release is P4 (see §7.6).

### 7.3 Bundled declaration and startup admission

flutter_tools (`compileShorebirdYaml`) writes these keys only for hardened
builds:

- `security_profile: hardened`
- `security_contract_version: 1`
- `toolchain_id: sha256:…`
- `security_development_exceptions: true`, only in development builds.

It also forces `patch_verification: strict` and
`patch_response_verification: required`. A checked-in shorebird.yaml that
contains any of these four keys is rejected in every profile.

The legacy output is unchanged byte for byte. The updater parses the four
keys (`yaml.rs`) and records any duplicated key.

`security::admit` runs at the top of `set_config_with_binding`, the common
path of `shorebird_init`, `shorebird_init_with_options` and
`shorebird_init_selection_v2`. No entry point is a weaker retry path. A
refusal publishes no configuration: no key is requested, no patch is
selected, and the engine boots the bundled base. `updater::init` reports a
typed `InitError::SecurityPolicy`. Error messages never contain URLs, keys or
signatures.

Defense in depth:

- `cache::check_patch_signature`, the legacy "no key configured ⇒ accept"
  site, refuses unsigned content whenever the compiled profile is hardened.
- `network::download_to_path` checks every server-supplied download URL
  against the origin policy before any request.
- The default ureq requests use `https_only(true)` in hardened, which covers
  every redirect hop, so an HTTPS-to-HTTP redirect fails. The development
  exception does not follow redirects at all.

### 7.4 HTTP origins

Defaults stay as they are: the legacy and development default `base_url`
remains `http://localhost:8080`, and legacy accepts any scheme.

Hardened requires an explicit `base_url` with these properties:

- `https` scheme, case-insensitive;
- a non-empty host and a numeric port, if any;
- no userinfo;
- no whitespace or control characters.

The updater's default URL never applies in hardened.

The only exception is development builds. A hardened engine built with
`shorebird_security_development_exceptions = true` may use `http` to
`localhost`, `127.0.0.0/8` or `[::1]`:

- The build prints a cargo warning, and the updater logs a warning at every
  initialization.
- The exception works only when the YAML also declares
  `security_development_exceptions: true`, and both directions must match.
  A development engine therefore refuses a production declaration, and a
  production engine refuses a development one.
- `shorebird release` never writes the key. It refuses a toolchain whose
  `toolchain_security.json` declares `development_exceptions: true`, and it
  allows no HTTP, not even loopback.

### 7.5 CLI release admission

- `shorebird release --security-profile=<legacy|hardened>` defaults to
  `legacy`, and `hardened-diversified` is refused as reserved.
- The installed toolchain's floor is read from
  `<shorebird root>/bin/internal/toolchain_security.json`. Its keys are exactly
  `schema` (`open-shorebird.toolchain-security`), `contract_version` (1),
  `security_profile`, `toolchain_id` and `development_exceptions`. Unknown keys
  or versions fail closed, and an absent file means a legacy toolchain.
  Toolchain packaging writes the file; that is P6's job.
- Admission runs in `ReleaseCommand.assertArgsAreValid` before any build, for
  every platform. A release is refused when:
  - the requested profile differs from the toolchain's, in either direction
    (hardened is never auto-selected);
  - the toolchain is a development toolchain;
  - the shorebird.yaml contains a reserved key.
- In addition, a hardened release is refused when:
  - `--no-patch-signing` or a customer key is passed;
  - `base_url` is missing, uses a non-`https` scheme (including loopback), or
    carries userinfo;
  - `patch_verification` is not `strict` or `patch_response_verification` is
    not `required`;
  - `--flutter-version` names anything other than the pinned revision.
- After the signing key is resolved, the build stops unless the key is
  non-null and managed. Hardened builds receive `SHOREBIRD_SECURITY_PROFILE`
  and `SHOREBIRD_TOOLCHAIN_ID`; legacy build environments are byte-identical.
- `shorebird patch` on a hardened toolchain passes the same variables. It
  refuses customer-signing arguments, a release whose signing is not managed,
  and a YAML below the floor.

### 7.6 Reject/accept matrix and deferred work

| Case | `legacy` compiled | `hardened` compiled |
| --- | --- | --- |
| No security keys in YAML (every existing release) | accept, unchanged | reject (profile mismatch) |
| YAML `security_profile: hardened`, matching ID and version | reject (mismatch) | accept |
| Missing or empty `patch_public_key` | accept (unsigned legacy) | reject (missing trust root) |
| Key not in pinned `shorebird_trust_root_key_ids` | n/a (build refuses pins) | reject |
| `patch_verification: install_only` | accept | reject (weaker than floor) |
| `patch_response_verification: optional` | accept | reject |
| Verification keys omitted | defaults: strict, optional | floor: strict, required |
| `http://` origin, or no `base_url` (default `http://localhost:8080`) | accept | reject; loopback `http` only with matching development exception |
| Userinfo in origin, or HTTPS-to-HTTP redirect | accept | reject |
| `toolchain_id` differs from the compiled ID | reject when both present | reject; missing ID also rejected |
| `security_contract_version` other than 1 | reject | reject; missing version also rejected |
| `security_profile: hardened-diversified` | reject | reject (build also refuses it) |
| Duplicate YAML key | accept (last wins, as before) | reject |
| Unsigned patch reaching signature verification | accept when no key (legacy) | reject |
| Engine and updater library compiled with different profiles | engine refuses `Init` | engine refuses `Init` |

Tests: updater `security::tests`, `security_inputs::tests`,
`yaml::security_declaration_tests`, `config::tests::hardened_admission`,
`cache::signing::tests::hardened_policy_never_accepts_unsigned_content` and
`network::tests::hardened_transport`; engine `UpdaterSecurityProfile.*` in
`updater_unittests.cc`; flutter_tools `shorebird_yaml_test.dart` group
`security profiles`; CLI `security_profile_test.dart`,
`release_command_test.dart`, `releaser_test.dart`, `patch_command_test.dart`,
`shorebird_env_test.dart` and `artifact_builder_test.dart`.

Deferred:

- **P4 (server and protocol).** Store `security_profile` and `toolchain_id`
  in the release record, reject mismatched patch uploads on the server, and
  make `shorebird patch` follow the release's recorded profile instead of the
  installed toolchain's. Until then, `shorebird patch` enforces the toolchain
  floor and the device enforces the release's compiled floor. Split signing
  roles are also P4; hardened uses the managed key until then.
- **P3.** Detect a replaced bundled shorebird.yaml or `patch_public_key` on a
  repacked or tampered install. P1 makes a tampered YAML fail closed when it
  is weaker or inconsistent, not when it is internally consistent.
- **P6.** Packaging of hardened toolchains: CI GN args, writing
  `toolchain_security.json`, and toolchain-scoped artifact routes (P2), since
  hardened and legacy engines share `engine.version` today. Also P6:
  end-to-end lifecycle cases for a hardened release, including the negative
  boot cases on a device or host.


## 8. P2 decisions (official-source resolution and signed toolchain supply)

P2 implements the plan §7.1–7.3 defaults: official Pub and compatible Flutter
artifacts are fetched directly, the custom artifact store stays, and the
all-in-one artifact proxy remains available as an explicit compatibility mode.

### 8.1 Artifact catalog signer and trust

- **Envelope.** A detached signature document
  `artifact-catalog.v1.json.sig`
  (`docs/schemas/artifact-catalog-signature.v1.schema.json`): `algorithm`
  `ed25519`, `key_id` = `sha256:` over the raw 32-byte public key,
  `catalog_sha256`, and a base64 RFC 8032 signature over
  `open-shorebird.artifact-catalog-signature.v1:<catalog_sha256 hex>`. The
  catalog file must be byte-equal to its canonical JSON (§1), so duplicate keys
  and reformatting are rejected before the digest is compared.
- **Signer.** An Ed25519 key held as the CI secret
  `OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY` (PKCS#8 PEM; generate with
  `openssl genpkey -algorithm ed25519`). `scripts/publish_artifact_catalog.sh`
  signs with OpenSSL in the `artifact-mirror` job, then verifies against the
  trust store before the mirror is packaged. A production-signed catalog is
  generated for the `hardened` profile.
- **Trust store.** `flutter/bin/internal/shorebird_artifact_trust.json`
  (`docs/schemas/artifact-catalog-trust.v1.schema.json`) ships in the Flutter
  checkout, so git authenticates it. Each key has `usage` `production` or
  `development`. To enable production signing, add the public key with
  `python3 scripts/artifact_catalog.py trust-entry <pem> --usage production
  --description ...` and set the secret.
- **Development key.** `scripts/testdata/artifact-catalog-signing-key.DEVELOPMENT-ONLY.pem`
  is public and non-production (key ID
  `sha256:86d46e09d92b413100bb65c1f098235ed23ddb84e081b574c20be11d15fd6584`).
  It is the only key shipped today. `artifact_catalog.py check-trust`, run by
  the source-checks verifiers, fails if it is ever given `production` usage.
  Without the secret, CI signs with this key, emits a
  `::warning::` annotation, and generates a `legacy` catalog. CI stays green.
- **Client policy** (`verifyShorebirdArtifactCatalog`, mirrored by
  `artifact_catalog.py verify`). The profile comes from
  `SHOREBIRD_SECURITY_PROFILE`.

  | Catalog | `legacy` toolchain | `hardened` toolchain |
  | --- | --- | --- |
  | Production-signed, hardened catalog | accept | accept |
  | Production-signed, legacy catalog | accept | reject (needs a hardened catalog) |
  | Development-signed | accept with a loud warning | reject |
  | Unsigned | reject; accept with a warning only when `SHOREBIRD_ALLOW_UNSIGNED_ARTIFACT_CATALOG=1` | reject |
  | Untrusted key, digest or signature mismatch, non-canonical, wrong engine | reject | reject |

  Verification happens before an archive is unpacked or executed.

### 8.2 One origin precedence model

There is one model, implemented in four places:

- `flutter/bin/internal/shorebird_dart_sdk.{sh,ps1}`, the Dart SDK bootstrap
  for direct Flutter, CLI-vended Flutter, and SDK refresh;
- `flutter_tools` `shorebird_artifact_origins.dart`, for engine artifacts and
  Gradle;
- the CLI's `ShorebirdEnv.artifactOrigins`, which hands Flutter
  `SHOREBIRD_ORIGIN_MODE`;
- the launchers, which no longer rewrite any origin.

| Setting | Scope | Notes |
| --- | --- | --- |
| `PUB_HOSTED_URL` | Pub only | Never read by the artifact resolvers. |
| `FLUTTER_STORAGE_BASE_URL` | Official-compatible artifacts only (default `https://storage.googleapis.com`) | Never serves a custom route or the custom Dart SDK. When it equals the open proxy `http://localhost:8080/download.flutter.io`, it is the legacy alias below. |
| `SHOREBIRD_ARTIFACT_BASE_URL` | Custom (patched) artifacts: `<store>/shorebird/<route>`, catalog `<store>/shorebird/<engine>/artifact-catalog.v1.json` (default `http://localhost:8080/artifacts`) | Every custom route needs a catalog entry and digest. A missing entry, or a 404, is the precise error "Missing custom artifact ...". |
| `SHOREBIRD_ARTIFACT_CATALOG` | Local catalog file (offline or air-gapped) | Its `.sig` must sit next to it. Otherwise the last verified catalog cached in `bin/cache/shorebird/artifact-catalog/<engine>/` is reused, but only after a network error. |
| `SHOREBIRD_DART_SDK_RELEASE_BASE_URL` | Custom Dart SDK, flat release layout | Takes precedence over the store and the pins `base_url`. A mutable `/releases/latest` URL is refused. |
| `SHOREBIRD_DART_SDK_SHA256` | Digest for an SDK not in the pins file | Explicit pin. |
| `SHOREBIRD_ORIGIN_MODE` | `split` (default) or `legacy-mirror` | `legacy-mirror` sends every artifact through one all-in-one proxy, as before P2. |
| `SHOREBIRD_DOWNLOAD_URL`, `SHOREBIRD_FLUTTER_STORAGE_BASE_URL` | Legacy aliases | Select `legacy-mirror` with that URL. In the CLI, `SHOREBIRD_MAVEN_BASE_URL` takes precedence over `SHOREBIRD_DOWNLOAD_URL` for Maven. |
| `SHOREBIRD_MAVEN_BASE_URL` | Gradle/Maven | Gradle keeps the proxy compatibility route (below). |

These combinations are configuration errors. The bootstrap exits with code 2;
flutter_tools and the CLI raise a tool exit or usage error.

- Two different legacy aliases.
- `SHOREBIRD_ORIGIN_MODE=split` together with an alias.
- `legacy-mirror` together with a different `FLUTTER_STORAGE_BASE_URL`.
- An unknown mode.

A hardened `shorebird release` or `shorebird patch` refuses `legacy-mirror`
(`hardenedLegacyMirrorRefusal`). An official origin never satisfies a custom
coordinate, and there is no silent fallback to stock binaries. The proxy is
optional: split mode needs only the custom store.

**Gradle** keeps the compatibility route. In split mode, flutter_tools gives
Gradle `FLUTTER_STORAGE_BASE_URL` = `SHOREBIRD_MAVEN_BASE_URL`, or else the
open proxy (`shorebirdGradleEnvironment`). The reason is that the Flutter
Gradle plugin resolves engine Maven coordinates from a single repository.

### 8.3 Dart SDK bootstrap

`flutter/bin/internal/shorebird_dart_sdk.pins` is in the Flutter checkout, so
git authenticates it. It records:

- `engine_revision` and `dart_sdk_revision`, which must equal `engine.version`
  and DEPS (`scripts/dart_sdk_lock.py check`, also run by
  `verify_open_infrastructure_defaults.sh` and `verify_ci_workflow.rb`);
- optionally an immutable https `base_url` and one
  `asset <zip> <sha256> <size>` line per host archive.

**Publishing.** The `publish-sdk-release` job creates the immutable tag
`dart-sdk-<engine12>-r<run_id>-<attempt>`. It never clobbers assets and never
marks the release latest. It verifies every download at that URL against the
tested digest and uploads the `shorebird_dart_sdk.pins` it generated with
`dart_sdk_lock.py write-published`. Committing those pins is the publication
step.

Until then the pins are **unpublished** (identity only). A split-mode
bootstrap then needs one of the following, and otherwise fails with a precise
error, never a fetch:

- a digest from `SHOREBIRD_DART_SDK_SHA256`;
- a pre-seeded verified archive;
- legacy-mirror mode, which installs an SDK labelled **UNVERIFIED** with a
  warning, exactly the pre-P2 behavior.

**Caching.**

- Archives are content-addressed: `bin/cache/<sha256>-<zip>`.
- The installed identity is recorded in `bin/cache/engine-dart-sdk.identity`
  as `<engine> <dart> <zip> <sha256|unverified>`.
- A current, verified install is reused offline without a request.
- A pin change replaces an unverified install.
- A locally provisioned SDK (a stamp with no identity file) is kept.
- `shared.bat` now re-runs the PowerShell identity check whenever it rebuilds
  the tool, as `shared.sh` does.

### 8.4 CLI downloads and hosts

- **`--dev` hosts.** No Shorebird host is compiled in. `--dev` requires
  `SHOREBIRD_DEV_HOSTED_URL`; `SHOREBIRD_DEV_AUTH_SERVICE_URL` and
  `SHOREBIRD_DEV_CONSOLE_URL` are optional and default to `<hosted>/auth` and
  `<hosted>`.
- **bundletool** is downloaded on demand (`Cache.ensure`) when an Android
  command needs it, not by `updateAll`. A Linux desktop release never fetches
  it.
- **Retries.** Every cache download makes up to 5 attempts with a 1 s doubling
  delay, about 15 s in total. Only retryable failures are retried: network
  errors, 5xx, 408 and 429. A definitive 4xx fails at once. This addresses the
  bundletool 504 in `HARDENING_P0_EVIDENCE.md`.

### 8.5 Negative-test matrix

| Case | Tests |
| --- | --- |
| Unsigned catalog under hardened; development-signed under hardened | `test_artifact_catalog.py` `VerifyTest`; flutter_tools `shorebird_artifact_catalog_test.dart` group `verifyShorebirdArtifactCatalog` |
| Catalog digest/signature mismatch, tampering, non-canonical, duplicate key, untrusted key, wrong engine | Same two suites |
| Archive digest or size mismatch before unzip | `test_dart_sdk_bootstrap.sh` cases 4, 5, 7 and the PowerShell matrix; flutter_tools `ArtifactUpdater with the resolver` |
| Custom coordinate classified or requested as official | `test_custom_route_classified_official_is_rejected`, `test_official_entry_never_satisfies_a_custom_route`; flutter_tools resolver tests; bootstrap case 8 (`FLUTTER_STORAGE_BASE_URL` never consulted) |
| Conflicting overrides | Bootstrap case 13 (sh and ps1); flutter_tools `ShorebirdArtifactOrigins`; CLI `shorebird_env_test.dart` `artifactOrigins`, runner `artifact origins` |
| Missing custom artifact | `test_missing_custom_artifact_is_a_precise_error`; `PublishScriptTest`; flutter_tools resolver (absent from the catalog, and 404) |
| Mutable `latest` URL | Bootstrap case 12 (sh and ps1); `dart_sdk_lock.py` rejections; `verify_ci_workflow.rb`, `verify_open_infrastructure_defaults.sh`, `verify_powershell_open_defaults.sh` |
| Offline cache reuse | Bootstrap cases 2 and 6 (sh) and the PowerShell matrix; flutter_tools cached catalog after a network error |
| Hardened release or patch through the legacy mirror | `release_command_test.dart` and `patch_command_test.dart` "refuses the legacy all-in-one artifact mirror" |
| Implicit `--dev` host; bundletool on non-Android; retry bounds | CLI runner `--dev` tests; `cache_test.dart` |
| Development key promoted to production | `test_check_trust_refuses_production_trust_for_the_development_key` |

### 8.6 Deferred

- **CI secret (operator).** Generate the production key, set
  `OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY`, and pin its public key in the
  trust store. Optionally set the repository variable
  `OPEN_SHOREBIRD_ARTIFACT_STORE_URL`, the catalog's declared custom
  `base_url`. Clients resolve against their configured store and authenticate
  by digest, so that value is descriptive. If the secret is set before its key
  is pinned, the `artifact-mirror` job fails by design.
- **Pins publication.** After the first default-branch run of
  `publish-sdk-release`, commit its `shorebird_dart_sdk.pins` to the Flutter
  fork.
- **P6.** Catalog v1 paths cannot express the CLI patch tools
  (`shorebird/<engine>/patch-*.zip`). They keep their pinned-checksum path in
  the CLI cache, and catalog v2 should cover them. Other P6 items:
  - toolchain-scoped catalog routes for hardened versus legacy builds, which
    share `engine.version`;
  - re-verifying cached artifacts when the profile changes, because artifacts
    precached under `legacy` are reused by a later hardened build;
  - the remaining resolver hooks: `doesRemoteExist`/`checkForArtifacts`, the
    doctor `http_host_validator` host, and the podspec URLs in
    `build_ios_framework`/`build_macos_framework`, all of which still name the
    configured storage base;
  - end-to-end clean-install and offline lifecycle runs.

## 9. P3 decisions (updater self-integrity)

P3 adds a startup integrity gate to hardened builds. It detects a repacked
app, a replaced engine or updater, and a tampered `shorebird.yaml` or
`patch_public_key`, and it then refuses to request keys or activate patches.
Legacy builds never run it, and their output and behavior are unchanged byte
for byte.

### 9.1 Signed build manifest

- **Location.** A hardened build appends one final line to the bundled
  `shorebird.yaml`:
  `shorebird_build_manifest: v1.<base64url(manifest JSON)>.<base64url(Ed25519 signature)>`.
  It lives in the YAML instead of a new asset, so every engine embedder
  (Java, Objective-C, Linux, Windows) delivers it through the same single read
  it already does. No embedder changed.
- **Content** (`docs/schemas/build-manifest.v1.schema.json`): `schema`,
  `contract_version: 1`, `security_profile: hardened`, `toolchain_id`,
  `development_exceptions`, `signer_key_id`, `config_sha256`, `modules[]`
  (`platform`, `arch`, `measurement`) and `package_identity`
  (`android_signing_cert_sha256[]`, `apple_team_ids[]`). Every field is
  required. Unknown and duplicate fields are rejected.
- **Config binding.** `config_sha256` is the SHA-256 of every YAML byte before
  the manifest line. The manifest line must be the last line and must appear
  exactly once at a line start. The text must not contain `\r`. A trailing
  newline is optional, because Android's line reader adds one. The updater
  computes the digest in `YamlConfig::from_yaml` from the same string it
  parses, and the engine reads its own keys from that same string. A check
  can therefore never pass on one set of bytes while another set is used.
  Adding a key, editing a value, or swapping the key or origin each change
  the digest.
- **Signature.** Ed25519 over
  `open-shorebird.build-manifest-signature.v1:<hex SHA-256 of the exact manifest bytes>`,
  the same construction as the P2 catalog. The verifier checks the exact
  bytes, so the JSON field order carries no meaning. Before verification, only
  `signer_key_id` is read, to select the key.
- **Trust anchor.** The public keys come from the new engine GN arg
  `shorebird_build_manifest_keys`: base64 raw 32-byte Ed25519 keys, passed to
  `build.rs` as `SHOREBIRD_BUILD_MANIFEST_KEYS`.
  - A hardened build requires at least one key. GN, `build_rust_updater.py`
    and `build.rs` all enforce this, and legacy builds refuse the argument.
  - A key found in the package is never trusted. A repacker who rewrites the
    YAML and the manifest consistently also needs the signing key compiled
    into the engine.
- **Signer (interim, before P4).** An operator-held Ed25519 key, the
  "build-manifest signer":
  - The release machine or CI holds the private key and passes its path as
    `SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY` (PKCS#8 PEM, from
    `openssl genpkey -algorithm ed25519`).
  - flutter_tools signs while it writes the bundled YAML
    (`shorebird_build_manifest.dart`). Signing is deterministic, so a patch
    build given the same inputs reproduces the release's `shorebird.yaml`
    byte for byte, and the patch asset diff stays clean.
  - Signing runs in-process and is not constant time, which is acceptable on
    a signer-controlled build machine.
  - **Superseded by contract v2 (§10).** The signer is now a
    `build-manifest` key delegated by the release root, the compiled key list
    became the release-root pin, and the token carries the delegation chain.
    `shorebird_build_manifest_keys` is retired: setting it fails the build.

### 9.2 Module measurement

`updater/library/src/module_measurement.rs` and flutter_tools
`shorebird_build_manifest.dart` implement one definition. The engine library
contains the statically linked updater, so measuring the module that contains
the updater measures both. A measurement is `sha256:` over the domain
`open-shorebird.module-measurement.v1\0`, a format tag, and then, for each
region, a 32-byte label, the address and size as u64 LE, and the region
bytes.

| Format | Measured regions | Excluded on purpose |
| --- | --- | --- |
| ELF (Android, Linux) | file bytes of each `PT_LOAD` without `PF_W` | writable segments (relocated data, RELRO), section headers |
| Mach-O (iOS, macOS) | each non-zero-fill section of a segment whose `initprot` lacks write, except `__LINKEDIT` | load commands and `__LINKEDIT` (code signing rewrites them), writable/`__DATA_CONST` segments (dyld fixups) |
| PE (Windows) | raw data of each section without `IMAGE_SCN_MEM_WRITE` | header checksum, certificate directory and appended Authenticode data |

These rules avoid circular whole-file hashes. They hold across code signing,
App Store encryption (undone in memory) and Authenticode. Both
implementations pin the same synthetic-image golden values, and a Rust test
proves on a real loader that the in-memory measurement equals the file
measurement (`running_module_measurement_equals_its_file_measurement`). Thin
and universal Mach-O files are measured per slice.

Run-time source of the measurement: the module actually loaded, never a path
from configuration.

- **Linux and Android:** `dl_iterate_phdr` finds the object whose `PT_LOAD`
  contains the updater's own code. Its program headers come from the loader,
  and its segments are hashed in memory.
- **iOS and macOS:** `dladdr` returns the mapped `mach_header`. Its load
  commands and sections are read in memory and hashed at the slid addresses.
- **Windows:** base relocations change `.rdata` in memory, so the module is
  measured from its file. `GetModuleHandleExW(FROM_ADDRESS)` locates the
  module and `GetModuleFileNameW` gives its path. The file is read once and
  those bytes are measured. The section table mapped in memory must equal the
  file's, so a different file at the reported path is refused.

Build-time source of the expected values: flutter_tools measures the engine
artifact that the build packages.

| Platform | Engine artifact |
| --- | --- |
| iOS | `Flutter.xcframework/ios-arm64/Flutter.framework/Flutter` |
| macOS | each slice of `FlutterMacOS.xcframework/macos-arm64_x86_64/.../FlutterMacOS` |
| Windows | `flutter_windows.dll` |
| Linux | `libflutter_linux_gtk.so` |

Android has no expected value: `libflutter.so` reaches the build through
Gradle's Maven resolution, which flutter_tools does not see. Wiring that is
P6. Android is therefore the only platform where a missing module entry is
accepted. When an entry is present there, it is enforced.

The updater measures once per process. Its startup cost has not been measured on devices yet (P6).

### 9.3 Platform package identity adapters

| Platform | Adapter | Status |
| --- | --- | --- |
| Android | SHA-256 of each signer certificate in the installed `base.apk` APK Signature Scheme v3.1/v3/v2 block (v1-only APKs have no identity). Every installed signer must be listed. Several allowed digests cover rotation and Play App Signing. | implemented; parser unit-tested, device untested |
| macOS | `SecCodeCheckValidity` of the running process, then its `kSecCodeInfoTeamIdentifier`. Ad-hoc or unsigned code has no team and fails. | implemented; tested on this host (ad-hoc path) |
| iOS | No public API exposes the running team identifier. A manifest that declares one fails closed (`package_identity_unsupported`), and the CLI and flutter_tools refuse to write one. | unsupported |
| Windows | Authenticode signer of the loaded module | unsupported (P6) |
| Linux | none: deployment trust (package manager, file ownership) | unsupported |

Identities are optional. They are declared with
`SHOREBIRD_ANDROID_SIGNING_CERT_SHA256` (hex, or keytool form with colons)
and `SHOREBIRD_APPLE_TEAM_IDS`. A declaration applies only to the platform
that can verify it, and both flutter_tools and the CLI refuse it for any
other platform. The Android adapter reads the signer that the OS verified at
install time. It does not re-verify the APK.

### 9.4 Gate order and failure policy

`set_config_with_binding` is the common path of `shorebird_init`,
`shorebird_init_with_options` and `shorebird_init_selection_v2`. It runs P1
admission first, then `integrity::check`, and only then publishes a
configuration.

Admission runs first, so a manifest signed by the compiled key still cannot
admit a configuration below the hardened floor. The gate then checks, in
order:

1. the manifest line;
2. the signer and the signature;
3. the policy fields against the compiled policy;
4. `config_sha256`;
5. the module entry for the current platform and architecture;
6. the package identity.

On failure:

- **Nothing is published.** No key provider is called (the protected
  installation keys of §4.4), no patch is selected or activated, and no launch
  is reported. The engine boots the bundled base, as with a P1 refusal.
- **Nothing is deleted.** The gate never touches the updater state, downloads
  or app data, so reinstalling the genuine app recovers with its data intact.
- **A typed reason is recorded.**
  - `updater::init` returns `InitError::Integrity(code)`.
  - `shorebird_integrity_status()`, a new engine-surface C function that is
    not a Dart FFI export, returns the code.
  - A hardened engine's `Updater::Init` stores and logs it, asking the user to
    reinstall from the original source.
  - Codes: `manifest_missing`, `manifest_malformed`,
    `manifest_untrusted_signer`, `manifest_bad_signature`,
    `manifest_policy_mismatch`, `config_digest_mismatch`, `module_unbound`,
    `module_unmeasurable`, `module_mismatch`, `package_identity_unavailable`,
    `package_identity_mismatch` and `package_identity_unsupported`. Messages
    carry no keys, URLs or digests.

### 9.5 Detection versus prevention

| Guarantee | Holds when | Kind |
| --- | --- | --- |
| A YAML or key replaced on an otherwise genuine install (desktop file write, or a repack that keeps the engine) is refused | the engine code is unmodified | prevention of update and key use, by cryptographic binding to a compiled key |
| A repack re-signed with another Android signer or Apple team is refused | an identity is declared, and the OS verified the package at install | prevention, under the platform trust assumptions |
| An engine or updater replaced by a different build (an older hardened engine, another toolchain, a hooked library that keeps the gate) is refused | the replacement still runs this gate | detection |
| Code hooked before initialization is noticed | the hook patched measured bytes | detection signal |
| Hooks installed after the one-time check, a binary patched to skip the gate, or a compromised OS or process | never | not covered: in-process checks are bypassable there |
| Windows: a file renamed and replaced after load at the module's path | the mapped section table differs | detection; contents are compared only from the file |

Platform trust assumptions:

- Android and iOS verify package signatures at install time and keep
  installed packages read-only to apps.
- macOS enforces code signatures, and library validation under the hardened
  runtime.
- On Windows and Linux, the install location is writable only by its owner
  or an administrator, and an attacker with that access is out of scope
  except for detection.

The P0 note that hardened and legacy engines share `engine.version` (and so
the Dart snapshot hash) still applies. A repack that swaps in a legacy engine
runs legacy code that has no gate. Binding the profile into the snapshot or
the toolchain artifacts belongs to P5 and P6.

### 9.6 Reject/accept matrix

| Case | Hardened result | Tests |
| --- | --- | --- |
| Build output (signed manifest, matching module) | accept | `integrity::tests::accepts_the_build_output`, `real_module_binding_accepts_this_process_and_rejects_other_bytes`, `flutter_tools_output_verifies`; `config::tests::hardened_admission::hardened_updater_boots_the_hardened_build_output` |
| Manifest stripped, malformed, repeated, not last, CRLF | reject `manifest_missing` / `manifest_malformed` | `rejects_a_missing_or_malformed_manifest`; `tampered_installs_publish_nothing_on_any_entry_point` |
| Consistent repack: YAML, key and manifest replaced and re-signed by another key | reject `manifest_untrusted_signer` / `manifest_bad_signature` | `rejects_a_manifest_not_signed_by_a_compiled_key`; flutter_tools `matches the token the updater verifies` |
| `shorebird.yaml`/key/origin/app_id replaced, consistent with each other, under the original manifest | reject `config_digest_mismatch` | `rejects_a_config_changed_after_signing`; `tampered_installs_...`; `init_reports_a_typed_integrity_refusal_and_keeps_user_data` |
| Manifest for another profile, toolchain, development setting or version; unknown or duplicate fields | reject `manifest_policy_mismatch` / `manifest_malformed` | `rejects_a_manifest_for_another_policy`; `scripts/test_hardening_contracts.py` `BuildManifestTest` |
| Signed manifest over a configuration weaker than the floor | reject (P1 admission) | `a_signed_manifest_cannot_lower_the_compiled_floor` |
| Engine or updater bytes differ (replaced or tampered module, changed loader target) | reject `module_mismatch` | `rejects_a_replaced_or_unmeasurable_engine_module`, `real_module_binding_...`, `running_module_measurement_equals_its_file_measurement`; `tampered_installs_...` |
| No entry for this platform or architecture (non-Android) | reject `module_unbound` | `rejects_a_replaced_or_unmeasurable_engine_module` |
| Measured bytes vs excluded signing regions | flip one measured byte: changes; signature/checksum regions: unchanged | `only_immutable_regions_are_measured`, `code_signature_and_authenticode_regions_are_excluded`; flutter_tools `module measurement` group |
| Re-signed APK, extra signer, or unreadable APK | reject `package_identity_mismatch` / `_unavailable` | `package_identity_rejects_a_re_signed_package`, `android_adapter_reads_the_installed_apk`, `package_identity::tests` |
| macOS ad-hoc, other team, or invalid signature | reject | same, and `macos_adapter_reads_the_running_signature` |
| iOS with a declared team | reject `package_identity_unsupported` | same |
| Any refusal | publishes nothing, keeps state and downloads | `tampered_installs_...`, `init_reports_a_typed_integrity_refusal_and_keeps_user_data` |
| Legacy build, any YAML (even a garbage manifest line) | accept, gate never runs | `legacy_ignores_the_manifest_line_entirely`, `legacy_never_runs_the_gate`, engine `UpdaterIntegrity.*` (legacy) |
| Hardened engine without manifest keys; keys on legacy | build fails | `security_inputs::tests::hardened_requires_a_valid_build_manifest_signer`; `scripts/test_build_rust_updater.py` `UpdaterSecurityEnvironmentTest`; GN asserts |
| Hardened `Updater::Init` failure | typed reason recorded, not latched | engine `UpdaterIntegrity.HardenedInitFailureRecordsTheTypedReason` |
| Hardened release or patch without a usable signing key, or an identity for the wrong platform | refused before building | CLI `security_profile_test.dart` `hardenedBuildManifestProblems`, `release_command_test.dart`, `patch_command_test.dart`; flutter_tools `updateShorebirdYaml under hardened` |

Evidence class: Rust unit and integration tests (macOS host, including real
Mach-O loader and code-signing APIs), Android cross-compilation (`aarch64`
and `armv7`) for the ELF path, engine unit tests built in both profiles,
flutter_tools and CLI unit tests, and CI wiring (`cli-artifacts` runs the
updater gate tests on Linux and macOS hosts, and the Windows engine jobs run
the whole updater suite). There is no device, store-distribution or
end-to-end lifecycle evidence yet.

The rules were also checked on this host against the real engine artifacts of
the current toolchain. For iOS `Flutter`, the Dart and Rust measurements are
equal. For `FlutterMacOS` arm64, three measurements are equal: Dart on the
universal file, Rust on the thinned file, and Rust in memory after `dlopen`
of the framework. Re-signing with `codesign -f -s -` changes the file bytes
but not the measurement.

### 9.7 Deferred

- **P4.** Done client-side in contract v2 (§10); the server parts are §10.9.
- **P6.**
  - Android module measurement source (Gradle Maven `libflutter.so`).
  - Windows Authenticode adapter.
  - A hardened toolchain in CI that carries `shorebird_release_root`.
  - Re-verification of the final distributed artifact by the CLI (IPA, AAB,
    app bundle).
  - Lifecycle negative cases: a tampered YAML and a replaced engine library
    on a real install, and recovery by reinstall.
  - Device evidence for the Android and iOS paths.
  - Measuring cold-start cost on devices.

## 10. Contract v2: release root of trust (P4)

P4 replaces P3's single operator key with a release root of trust. An offline
root delegates each signing role to separate keys, which can be rotated and
revoked, and an external KMS or HSM can hold them. Contract v2 changes the
build manifest, the security policy and the hardened `shorebird.yaml`
declaration, and adds the release-root, release-delegations, release-trust and
release-record documents. Legacy builds are unchanged byte for byte: they
never read a v2 document, and their YAML, metadata JSON and updater behavior
are identical to before.

This section is the client-side part of P4 ("P4a"): updater, engine build,
flutter_tools, CLI, scripts and CI. The server and protocol part ("P4b") is
listed in §10.9.

### 10.1 Keys and roles

| Key | Signs | Held | Trusted by |
| --- | --- | --- | --- |
| Release root (k-of-n Ed25519) | `release-root` and `release-delegations` documents only | offline, by operators | the root pin compiled into a hardened engine |
| `build-manifest` delegate | the build manifest line of a hardened `shorebird.yaml` | release machine or CI, as a PKCS#8 file or behind a KMS/HSM signer | the current delegations |
| `artifact-catalog` delegate | the hardened artifact catalog (P2 format, unchanged) | CI secret `OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY` | the P2 trust store **and** the current delegations |
| Patch signing (RSA, P1) | patches and patch-check responses | Shorebird-managed per app | the bundled `patch_public_key` (P1), unchanged |

Separation rules, enforced when delegations are signed (`release_trust.py`),
when they are verified (updater, flutter_tools, Python) and by the
security-policy validator:

- A key belongs to exactly one role.
- A root key is never delegated a role. Root keys sign metadata only.
- A revoked key is never delegated again, and a revocation is never
  withdrawn: `release_trust.py delegate --previous` carries every earlier
  revocation forward and refuses to drop one.
- A release-root key ID is never also a patch-signing trust root
  (`security-policy.v2` cross-field rule).

No single key therefore signs both a build manifest and a catalog, and no
online key can change who is trusted.

### 10.2 Documents and signatures

All payloads are canonical JSON (§1). A verifier rejects a payload whose
bytes are not canonical, so a signature covers exactly one encoding.

- **Envelope.** `{"payload": base64(payload bytes), "signatures": [{"key_id", "signature"}]}`
  (`release-trust.v2.schema.json` `$defs/envelope`). Base64 must be
  canonical, signatures are 64 bytes, and a key may appear only once.
  Signatures by unknown keys are ignored, so adding a signer never breaks
  older verifiers.
- **Domains.** A signature covers `<domain><lowercase hex SHA-256 of the payload bytes>`:

  | Document | Domain |
  | --- | --- |
  | release root | `open-shorebird.release-root-signature.v2:` |
  | release delegations | `open-shorebird.release-delegations-signature.v2:` |
  | build manifest | `open-shorebird.build-manifest-signature.v2:` |
  | artifact catalog | `open-shorebird.artifact-catalog-signature.v1:` (P2, unchanged) |

  A signature made for one document type never verifies as another.
- **`release-root`** (`release-root.v2.schema.json`): `version`, `threshold`,
  `keys[]` (`key_id` = `sha256:` of the raw 32-byte public key,
  `public_key`), `expires_at`. Version 1 is signed by a threshold of its own
  keys. Version N+1 must be signed by a threshold of version N's keys **and**
  a threshold of its own keys, and versions cannot be skipped.
- **`release-delegations`** (`release-delegations.v2.schema.json`):
  `version`, `root_version`, `issued_at`, `expires_at`,
  `roles.{build-manifest,artifact-catalog}[]` (`key_id`, `public_key`,
  `not_before`, `not_after`) and `revoked_key_ids[]`. They are signed by a
  threshold of the root version they name. At least one `build-manifest` key
  is required.
- **`release-trust` bundle** (`release-trust.v2.schema.json`):
  `root_chain[]` (root envelopes newer than the pin; older entries are
  skipped) and `delegations` (one envelope). It is at most 32 KiB, with at
  most 16 chain entries and 32 keys per document.

### 10.3 Verification: device versus tooling

`updater/library/src/release_trust.rs`, flutter_tools
`shorebird_release_trust.dart` and `scripts/release_trust.py` implement one
algorithm, with shared fixtures:

1. Parse the compiled root pin, which is the canonical payload of one root
   version.
2. Follow `root_chain` from the pin, one version at a time, requiring both
   thresholds at each step.
3. Require the delegations to name the final root version and to be signed by
   its threshold.
4. Require `delegations.version` to be at least the compiled
   `min_delegations_version` (rollback floor).
5. Look up the signer: a revoked key gives `signer_revoked`, a key of another
   role or a root key gives `signer_role`, and an unknown key gives
   `untrusted_signer`.

**The device never reads a clock.** Expiry and validity windows are checked
only by tooling at signing time (`trust_expired`, `signer_outside_window`). A
wrong device clock must not brick startup, and an attacker who controls the
clock must not gain anything. Consequences:

- **Revocation and rotation** reach a device through what it accepts:
  1. A new release ships a bundle whose delegations drop or revoke a key.
     Tooling refuses to sign with a revoked or rotated-out key.
  2. A new native release raises the compiled `min_delegations_version`. From
     then on its engine refuses every older delegations document, including
     one that still trusts a compromised key.
- **Residual risk.** An engine already installed with a lower floor keeps
  accepting a manifest signed by a compromised `build-manifest` key under the
  old delegations, until the user installs a release built with a raised
  floor. This is the cost of a clock-free device and is the reason to keep
  delegate keys in a KMS or HSM.
- **Planned root rotation** works for installed engines: they follow the
  chain.
- **Root compromise** needs a new native release with a new pin, because a
  holder of a threshold of version N's keys can sign any version N+1.

### 10.4 Compiled pin and build inputs

| Input | Where | Rule |
| --- | --- | --- |
| `shorebird_release_root` | GN (`common/config.gni`) | base64 canonical `release-root` payload. Required for hardened, refused for legacy. |
| `shorebird_release_min_delegations_version` | GN | integer ≥ 1, default 1; values other than 1 are hardened-only |
| `SHOREBIRD_RELEASE_ROOT`, `SHOREBIRD_RELEASE_MIN_DELEGATIONS_VERSION` | `build_rust_updater.py` → `build.rs` | Set only from the GN args. Inherited values are scrubbed. `security_inputs.rs` validates them (canonical, ≤ 8 KiB, contract v2 root) and generates `COMPILED_RELEASE_ROOT` and `COMPILED_MIN_DELEGATIONS_VERSION`. |
| `shorebird_build_manifest_keys` / `SHOREBIRD_BUILD_MANIFEST_KEYS` | retired | Setting either fails the build with a pointer to the release root. |

`scripts/release_trust.py pin ROOT_ENVELOPE` prints the GN values and the
`toolchain_security.json` fields. `toolchain_security.json` gains contract
version 2 with `release_root` and `min_delegations_version`:

- A hardened toolchain file must be v2 and carry a root.
- A legacy file may stay v1 (read unchanged) or be v2 with `release_root:
  null` and floor 1.
- The CLI passes both values to `flutter build` as `SHOREBIRD_RELEASE_ROOT`
  and `SHOREBIRD_RELEASE_MIN_DELEGATIONS_VERSION`, so flutter_tools verifies
  the bundle against exactly what the engine compiled.

**Development root.** `scripts/testdata/release-trust/root-1.DEVELOPMENT-ONLY.pem`
(public key `iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w=`) is a deliberately
public test key. `build.rs`, `build_rust_updater.py` and flutter_tools refuse
a root containing it unless development exceptions are on.
`release_trust.py verify --production` and `validate_hardening_contracts.py
security-policy --production` refuse it outright.

### 10.5 Build manifest v2 and signing

- **Token.** The final YAML line becomes
  `shorebird_build_manifest: v2.<b64url manifest>.<b64url signature>.<b64url canonical release-trust JSON>`.
  The bundle travels with the manifest, so every embedder still delivers it in
  the single YAML read of §9.1.
- **Manifest.** `build-manifest.v2.schema.json` is v1 plus
  `delegations_sha256`, the SHA-256 of the delegations payload the signer was
  delegated by. A valid manifest cannot be replayed under other delegations
  (`manifest_policy_mismatch`).
- **Declaration.** A hardened YAML declares `security_contract_version: 2`.
  - A hardened updater requires exactly 2.
  - A legacy updater accepts a declared 1 or 2 and rejects anything else, as
    before.
  - A v1 hardened engine is not paired with a v2 flutter_tools: the engine and
    flutter_tools ship as one toolchain.
- **Gate.** §9.4 is unchanged, except that step 2 is "verify the release-trust
  bundle against the compiled root, resolve the signer as a `build-manifest`
  delegate, verify the signature". New failure codes: `trust_root_missing`
  (hardened engine without a compiled root: a build bug, kept as a guard),
  `trust_malformed`, `trust_unverified`, `trust_rollback`,
  `manifest_signer_revoked` and `manifest_signer_role`. As in P3, a refusal
  publishes nothing and deletes nothing.
- **Signer interface (KMS/HSM).** flutter_tools signs with one of two inputs,
  and setting both is an error:
  - `SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY`: a PKCS#8 file, signed in process
    as in P3.
  - `SHOREBIRD_BUILD_MANIFEST_SIGNER` plus
    `SHOREBIRD_BUILD_MANIFEST_SIGNER_KEY_ID`: an executable invoked as
    `<signer> sign --key-id <id> --message <ascii message>`, which prints the
    base64 64-byte Ed25519 signature on stdout. The message is the domain
    plus the hex digest, so the adapter never sees YAML. On Windows a
    `.bat`/`.cmd` signer runs through the shell.

  Either way, flutter_tools verifies the signature against the delegated
  public key before writing the line, so a misconfigured signer fails the
  build instead of the app. `release_trust.py sign --key PEM --role ROLE
  --key-id ID --message TEXT` is the reference adapter. It refuses a message
  outside its role's domain, which shows the role pinning a real adapter
  should apply.
- **Build-time checks.** Before signing, flutter_tools verifies the bundle
  (`SHOREBIRD_RELEASE_TRUST`, a file path) against the pin and checks:
  - the root and delegations are not expired;
  - the signer is a current `build-manifest` delegate, inside its window;
  - the root is not the development root unless development exceptions are
    on.
- **Deterministic.** Signing is still deterministic: a patch build with the
  same inputs (same bundle) reproduces the release's YAML byte for byte.

### 10.6 Artifact catalog under the release root

The P2 catalog format and domain are unchanged. Under hardened, the catalog
signer must also be the current `artifact-catalog` delegate of the release
root:

- flutter_tools `verifyShorebirdArtifactCatalog(releaseTrust:)` checks it.
  `ShorebirdArtifactOrigins` loads the trust from `SHOREBIRD_RELEASE_TRUST`
  and `SHOREBIRD_RELEASE_ROOT`.
- `artifact_catalog.py verify --release-trust --root-pin
  [--min-delegations-version]` checks it in Python.
- `publish_artifact_catalog.sh` requires `OPEN_SHOREBIRD_RELEASE_TRUST`
  (a path or inline JSON) and `OPEN_SHOREBIRD_RELEASE_ROOT` for a hardened
  catalog.

Legacy catalogs and the P2 trust store are unchanged. The P2 store still
pins the key, and the release root now additionally authorizes it for this
role.

### 10.7 Release record and security policy v2

- **`release-record.v2`** (`release-record.v2.schema.json`). Contents: the
  profile, toolchain ID, development flag, root SHA-256 and version,
  delegations version and SHA-256, and, per build manifest, its SHA-256,
  signer, `config_sha256` and modules. It contains no keys and no paths.
  - flutter_tools writes one file per manifest to
    `SHOREBIRD_BUILD_MANIFEST_RECORD_DIR`.
  - `shorebird release --security-profile=hardened` points that at
    `<project>/build/shorebird/release-record` (cleared first) and merges the
    files right after the build. It refuses, before anything is uploaded, if
    the record is missing, malformed or inconsistent (different roots,
    delegations or toolchains).
  - It stores the record as `security_record` in `UpdateReleaseMetadata`.
    The key is omitted when null, so legacy release metadata JSON is
    byte-identical.
- **`security-policy.v2`**. v1 plus `release_root`: `root_sha256`,
  `root_version`, `threshold`, `key_ids`, `min_delegations_version`.
  - It is required (an object) for hardened and `null` for legacy.
  - Cross-field rules: the threshold is at most the number of keys, root keys
    are not patch-signing roots, and the development root needs
    `development_exceptions` and fails `--production`.

### 10.8 Reject/accept matrix

| Case | Result | Tests |
| --- | --- | --- |
| Build output under the compiled root (Rust verifies the Dart-produced v2 token; Python bundle verified in Rust and Dart) | accept | `integrity::tests::flutter_tools_output_verifies`, `accepts_the_build_output`; `release_trust::tests::python_reference_bundle_verifies`; flutter_tools `the Python reference bundle verifies under its pinned root`; `test_release_trust.py` `FixtureConsistencyTest` |
| Wrong key ID (unknown signer) | reject `manifest_untrusted_signer` | `release_trust::tests::signer_role_confusion_and_unknown_keys_are_refused`; `integrity::tests::manifest_signer_must_be_a_delegated_build_manifest_key`; flutter_tools `role confusion, unknown, revoked and rotated-out keys are refused`, `refuses a signer the delegations do not authorize now`; `test_release_trust.py` `VerifyTest` |
| Revoked key | reject `manifest_signer_revoked` | `revoked_and_rotated_out_keys_are_refused`; `revoked_rotated_out_and_rolled_back_signers_are_refused`; same Dart and Python tests |
| Rotated-out key (dropped from newer delegations) | reject `manifest_untrusted_signer` (refused at signing as well) | same |
| Signer role confusion: catalog key or root key signs a manifest; manifest key signs a catalog | reject `manifest_signer_role` / catalog refused | `signer_role_confusion_...`; `manifest_signer_must_be_...`; flutter_tools `hardened: the signer must be the release root artifact-catalog delegate`; `test_artifact_catalog.py` `test_hardened_signer_must_be_the_release_root_catalog_delegate` |
| One key in two roles, root key delegated, revoked key delegated | reject `trust_malformed` | `role_separation_is_enforced_on_device`; flutter_tools `malformed bundles, payloads and pins are refused`; `test_release_trust.py` role separation and signing rules |
| Delegations below the compiled floor (rollback) | reject `trust_rollback` | `revoked_rotated_out_and_rolled_back_signers_are_refused`; Dart and Python rollback cases |
| Delegations not signed by a root threshold; root-domain signature over delegations; duplicate signer | reject `trust_unverified` / `trust_malformed` | `delegations_must_be_signed_by_the_root`, `threshold_counts_distinct_valid_root_keys`; flutter_tools `delegations must be signed by a threshold of the root` |
| Root rotation: both thresholds present / one missing / version gap | accept / reject `trust_unverified` | `root_rotation_requires_both_thresholds`, `integrity::tests::a_planned_root_rotation_is_followed`; flutter_tools `a root rotation needs both thresholds and no gap` |
| Manifest replayed under other delegations | reject `manifest_policy_mismatch` | `manifest_binds_the_delegations_it_was_signed_under`; `test_hardening_contracts.py` `test_v2_manifest_binds_delegations` |
| Missing root pin under hardened | build fails; runtime guard `trust_root_missing` | `security_inputs::tests::hardened_requires_a_valid_release_root_pin`; `test_build_rust_updater.py` `UpdaterSecurityEnvironmentTest`; GN asserts; CLI `rejects a contract v1 hardened toolchain (no release root)` |
| Retired `shorebird_build_manifest_keys` set | build fails | `retired_build_manifest_keys_fail_the_build`; `test_build_rust_updater.py` |
| DEVELOPMENT-ONLY root without development exceptions; in production | build/sign fails; validator and `--production` refuse | `development_root_needs_a_development_build`; flutter_tools `the DEVELOPMENT-ONLY root needs a development build`; `test_release_trust.py` production test; `test_hardening_contracts.py` `test_v2_release_root_rules` |
| Expired root or delegations; signer outside its window (tooling only) | signing refused | flutter_tools `build-time checks: expiry and key validity windows`; `test_release_trust.py` tooling time checks |
| External signer returns a wrong signature; both signer inputs set | build fails before writing | flutter_tools `an external signer is checked before its output is used`; CLI `accepts exactly one signer` |
| Hardened release without a bundle, or the build wrote no record | refused before upload | CLI `refuses a release without a release-trust bundle`, `stops before activation when the build wrote no release record`, `readReleaseRecord` group |
| Legacy updater given a YAML declaring contract 1 or 2 / 3 | accept / reject | `security::tests::legacy_accepts_known_contract_versions_only` |
| Legacy build, release metadata, YAML | unchanged byte for byte | CLI `a legacy release omits the security record entirely`; P3 legacy tests unchanged; engine `Updater*` legacy filters |

Evidence class: unit and integration tests (Rust, Dart, Python) and engine
unit tests built in both profiles. There is no device, server or end-to-end
lifecycle evidence.

### 10.9 Deferred

- **P4b (server and protocol).** Not implemented here, because no server
  branch is part of this change:
  - a constrained online selection signer distinct from the artifact signer;
  - separately approved, signed artifact statements that the updater
    verifies;
  - authenticated installation-key provisioning;
  - server storage and enforcement of `security_record`: reject a patch
    upload whose profile, toolchain, root or delegations differ, so that
    `shorebird patch` follows the release's recorded profile (the
    `TODO(hardening-p4b)` in `patch_command.dart`).

  The server stores `UpdateReleaseMetadata` as opaque JSON today, so the
  record is kept but not enforced.
- **P5.** The `hardened-diversified` profile, a later contract version.
- **P6.**
  - A hardened CI toolchain built with `shorebird_release_root`, which writes
    a v2 `toolchain_security.json`.
  - Production values for `OPEN_SHOREBIRD_RELEASE_TRUST` and
    `OPEN_SHOREBIRD_RELEASE_ROOT`.
  - Lifecycle and device evidence for rotation and revocation.

### 10.10 Operator procedures

All private keys stay offline or in a KMS/HSM. None is committed. Test keys
are named `*.DEVELOPMENT-ONLY.*`.

1. **Root.** Create `n` Ed25519 keys (`openssl genpkey -algorithm ed25519`),
   then run `release_trust.py root --key K1 [--public-key P2 ...]
   --threshold k --expires T --output root.v1.json`.
2. **Delegates.** Create one build-manifest key per signer (or a KMS key
   exporting its public PEM) and the catalog key. Then run
   `release_trust.py delegate --root root.v1.json --root-key K1 ... --version 1
   --expires T --role build-manifest=BM.pub.pem@NB..NA --role
   artifact-catalog=CAT.pub.pem --output delegations.v1.json`.
3. **Bundle and pin.** Run `release_trust.py bundle --root root.v1.json
   --delegations delegations.v1.json --output release-trust.json`, then
   `release_trust.py pin root.v1.json`. Put the printed values in the GN args
   (`shorebird_release_root`, `shorebird_release_min_delegations_version`)
   and in `toolchain_security.json`.
4. **Build machines.** Set `SHOREBIRD_RELEASE_TRUST=release-trust.json`, and
   either `SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY` or
   `SHOREBIRD_BUILD_MANIFEST_SIGNER` with `SHOREBIRD_BUILD_MANIFEST_SIGNER_KEY_ID`.
5. **CI.** Before setting the production catalog secret, set the repository
   variables `OPEN_SHOREBIRD_RELEASE_TRUST` (bundle JSON) and
   `OPEN_SHOREBIRD_RELEASE_ROOT` (pin).
6. **Rotate or revoke a delegate.**
   1. Sign `delegations` version N+1 with `--previous` and `--revoke ID`.
   2. Re-bundle and ship it with the next release.
   3. For a compromise, also raise `shorebird_release_min_delegations_version`
      to N+1 in the next native release (§10.3).
7. **Rotate the root (planned).**
   1. Sign `root.v(N+1)` with `--previous root.vN.json --previous-key ...`.
   2. Add it to the bundle chain.
   3. Re-sign the delegations under it.
   4. New engines may pin it.

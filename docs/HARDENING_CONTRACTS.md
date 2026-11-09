# Hardening contracts, versions 1, 2 and 3

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
documents listed as v2 below; every other document stays v1. **Contract v3
(P5, the `hardened-diversified` profile) is implemented client-side**
(2026-11-09) in the Dart VM and dart2bytecode, the updater, engine build,
flutter_tools, CLI and scripts; §11 records it. Only `hardened-diversified`
documents use v3; legacy and hardened outputs are unchanged byte for byte.
**P6 (platform acceptance) is implemented** (2026-11-09) up to production
keys and physical devices; §12 records it, with the remaining operator and
device actions in §12.9.
The server parts of P1–P5 are not implemented yet. Evidence that informed it is in [HARDENING_P0_EVIDENCE.md](HARDENING_P0_EVIDENCE.md).

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
| Security policy v3 (P5) | `docs/schemas/security-policy.v3.schema.json` | `docs/schemas/examples/security-policy.v3.hardened-diversified.json` |
| Build manifest v3 (P5) | `docs/schemas/build-manifest.v3.schema.json` | `docs/schemas/examples/build-manifest.v3.ios-arm64.json` |
| Windows Authenticode identity (P6) | optional `package_identity.windows_signing_cert_sha256` in the v2 and v3 build-manifest schemas | `scripts/test_hardening_contracts.py` (§12.4) |
| Release record v3 (P5) | `docs/schemas/release-record.v3.schema.json` | `docs/schemas/examples/release-record.v3.json` |
| Build identity, artifact catalog v3 (P5) | `docs/schemas/build-identity.v3.schema.json`, `docs/schemas/artifact-catalog.v3.schema.json` | v1 examples with the v3 fields (tests) |
| Diversification profile (P5) | `docs/schemas/diversification-profile.v3.schema.json` | `scripts/testdata/diversification/profile.DEVELOPMENT-ONLY.json` |
| Diversification seed record (P5) | `docs/schemas/diversification-seed.v3.schema.json` | `scripts/testdata/diversification/seed.DEVELOPMENT-ONLY.json` (public test seed) |

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
  documents can name it. Contracts v1 and v2 nevertheless reject it
  everywhere, and `diversification_profile_id` must be `null`. Contract v3
  (§11, P5) defines it, and only v3 documents may use it.

## 2. Security profiles

| Field | `legacy` | `hardened` | `hardened-diversified` |
| --- | --- | --- | --- |
| `signatures` | `optional` or `required` (as the release shipped) | `required` | as `hardened` (v3, §11) |
| `patch_verification` | `strict` or `install_only` | `strict` | as `hardened` |
| `patch_response_verification` | `optional` or `required` | `required` | as `hardened` |
| `trust_roots` | empty unless signatures are required | ≥ 1 | as `hardened` |
| `update_origin_policy` | `any` (HTTP allowed, as today) | `https-only` | as `hardened` |
| `development_exceptions` | `false` (concept does not exist) | `false` in production; `true` only in visibly development builds | as `hardened` |
| `diversification_profile_id` | `null` | `null` | required: the profile ID (contract v3, §11.2) |
| `toolchain_id` | `null` or the build identity's ID | required: the build identity's `toolchain_id` | as `hardened` |

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
  `legacy`, and `hardened-diversified` is refused as reserved. (Contract v3
  accepts it on a diversified toolchain, §11.7.)
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
| `security_profile: hardened-diversified` | reject | reject (build also refuses it); accepted only by a `hardened-diversified` updater (§11.6) |
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
  `development`. To enable production signing,
  `scripts/provision_release_keys.sh` (§10.10) adds the public key with
  `--write-repo-pins` and sets the secret with `--apply-github`; by hand, use
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
| Android | `lib/<abi>/libflutter.so` of each engine jar Gradle resolved (P6, §12.2) |

Android had no expected value before P6, and a missing Android entry was
accepted. P6 measures the jars Gradle resolves (§12.2). Since then a
missing entry is `module_unbound` on every platform (§12.3).

The updater measures once per process. Its cost is logged on every hardened
start. Desktop figures are in §12.7; device figures are an operator action
(§12.9).

### 9.3 Platform package identity adapters

| Platform | Adapter | Status |
| --- | --- | --- |
| Android | SHA-256 of each signer certificate in the installed `base.apk` APK Signature Scheme v3.1/v3/v2 block (v1-only APKs have no identity). Every installed signer must be listed. Several allowed digests cover rotation and Play App Signing. | implemented; parser unit-tested, device untested |
| macOS | `SecCodeCheckValidity` of the running process, then its `kSecCodeInfoTeamIdentifier`. Ad-hoc or unsigned code has no team and fails. | implemented; tested on this host (ad-hoc path) |
| iOS | No public API exposes the running team identifier. A manifest that declares one fails closed (`package_identity_unsupported`), and the CLI and flutter_tools refuse to write one. | unsupported |
| Windows | `WinVerifyTrust` (offline) of the loaded engine module's file, opened share-read and re-measured equal to the loaded module; SHA-256 of the primary signer's leaf certificate (§12.4) | implemented (P6); tested on a Windows ARM64 VM |
| Linux | none: deployment trust (package manager, file ownership) | unsupported |

Identities are optional. They are declared with
`SHOREBIRD_ANDROID_SIGNING_CERT_SHA256` (hex, or keytool form with colons)
, `SHOREBIRD_APPLE_TEAM_IDS` and (P6)
`SHOREBIRD_WINDOWS_SIGNING_CERT_SHA256`. A declaration applies only to the platform
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
- **P6.** Done in §12. That covers the Android measurement source, the
  Windows Authenticode adapter, the hardened CI leg and toolchain tooling,
  final-artifact re-verification, and lifecycle negatives with startup cost
  on a macOS install. Device evidence and device cold-start cost are the
  operator actions in §12.9.

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
- **P5.** The `hardened-diversified` profile: implemented as contract v3
  (§11).
- **P6.** The hardened toolchain tooling and its CI leg are in §12.6.
  Production values for `OPEN_SHOREBIRD_RELEASE_TRUST` and
  `OPEN_SHOREBIRD_RELEASE_ROOT`, and device evidence for rotation and
  revocation, are operator actions (§12.9).

### 10.10 Operator procedures

All private keys stay offline or in a KMS/HSM. None is committed. Test keys
are named `*.DEVELOPMENT-ONLY.*`.

`scripts/provision_release_keys.sh` performs every step below from one private
directory (outside every git work tree, mode 0700) and is idempotent; see
[`RELEASE_KEYS.md`](RELEASE_KEYS.md) for the quickstart. It calls
`release_trust.py`'s signing functions, so the rules of §10.1–10.3 apply
unchanged. Production and DEVELOPMENT-ONLY directories cannot be mixed.

| Step | Command | Manual equivalent (`release_trust.py`) |
| --- | --- | --- |
| Root (k-of-n), delegates, bundle, pin | `init --production [--root-keys n --threshold k] [--external ROLE=PUB.pem]` | `root --key K1 [--public-key P2 ...] --threshold k --expires T --output root.v1.json`; `delegate --root root.v1.json --root-key K1 ... --version 1 --expires T --role build-manifest=BM.pub.pem --role artifact-catalog=CAT.pub.pem --output delegations.v1.json`; `bundle ...`; `pin root.v1.json` |
| Pins: GN args, `toolchain_security.json` fields, P2 trust store, server installer key | `--write-repo-pins` (writes `release-pins/`, `flutter/bin/internal/shorebird_artifact_trust.json`, `shorebird-server/signing/server-update.trusted-keys`) | copy the `pin` output into the GN args and `toolchain_security.json`; `artifact_catalog.py trust-entry` |
| Build machines | `env` prints `SHOREBIRD_RELEASE_TRUST` and `SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY` (or `SHOREBIRD_BUILD_MANIFEST_SIGNER` + `_KEY_ID`) | set them by hand |
| CI | `--apply-github` sets `OPEN_SHOREBIRD_RELEASE_TRUST` and `OPEN_SHOREBIRD_RELEASE_ROOT` **before** the production catalog secret, then the server update key variable before its secret | `gh variable set` / `gh secret set` in that order |
| Rotate or revoke a delegate | `rotate ROLE [--revoke] [--raise-floor]`: delegations N+1 with `--previous` (revocations carried forward); `--raise-floor` raises `shorebird_release_min_delegations_version` to N+1 for a compromise (§10.3) | `delegate --previous ... --revoke ID`, re-bundle |
| Rotate the root (planned) | `rotate root [--repin]`: root N+1 signed by both thresholds, delegations re-signed under it, chain re-bundled; `--repin` lets new engines pin it | `root --previous root.vN.json --previous-key ...`, then `delegate` and `bundle` |
| Check | `verify`: private directory, repository pins and GitHub variables agree; secrets exist | `release_trust.py verify --root-pin ... --production --at now` |

## 11. Contract v3: `hardened-diversified` (P5)

P5 defines the profile that P1 reserved. A `hardened-diversified` release is a
`hardened` release (every rule of §7, §9 and §10 applies unchanged) whose Dart
bytecode uses a release-specific opcode map. Contract v3 changes only the
documents listed as v3 below. Legacy and hardened builds never read a v3
document, and their YAML, manifests, bytecode, envelopes, release metadata and
updater behavior are unchanged byte for byte.

### 11.1 Scope and decisions

| Question (plan §5, §6, §8) | Decision |
| --- | --- |
| First target | The Dart bytecode interpreter (plan §5.1). Bytecode exists only for interpreter releases (iOS and iOS framework body/structural/full, macOS full), so the profile is refused for every other release (CLI) and for any engine built without `dart_enable_shorebird_interpreter` (GN). Native AOT instructions and the mixed-mode ARM64 path are not remapped. |
| What is diversified | One subprofile: a permutation of the wire bytes of the 176 public opcodes. `Trap` keeps byte 0, so zero-filled memory still traps. VM-internal opcodes keep their canonical bytes: they are produced by the VM itself and are refused in files. |
| Compact/wide pairs | Not adjacent on the wire. Width, operands and the wide flag are properties of the *semantic* opcode, which is decoded first; no consumer reads parity, ranges or the `+1` relation from a wire byte. |
| Where decoding happens | Compiler: only at the serialization boundary (`_writeBytecodeInstructions`/`_readBytecodeInstructions`), so code generation, jump patching, validation, golden text and the disassembler keep semantic bytes. VM: at every opcode read (`KernelBytecode::DecodeOpcode`, one 256-byte table lookup), never by rewriting loaded bytecode into a canonical copy (plan §5.3). |
| Seed scope and lifetime | One random 32-byte seed per application/native-release lineage, kept with the release records. Changing it needs a new native release and a fresh baseline; it is never re-randomized per patch or per process. |
| Map secrecy | Not a security boundary (plan §5.2). The engine necessarily contains the decode table. The seed and the profile document stay with the release records; the app carries only the nonsecret `profile_id`. |
| Profile binding | The profile id is compiled into the VM and the updater, declared in the bundled YAML, bound by the signed build manifest, recorded in the release record, written into every bytecode component header and every envelope's authenticated metadata. A mismatch anywhere is refused before any instruction is interpreted, and the envelope check runs before the key callback. |
| Native layout, BoringSSL | Reserved (`native_layout: null` in v3), see §11.8. |
| Obfuscation | Unchanged: interpreter releases still refuse `--obfuscate` (plan §5.1). Diversification does not imply name obfuscation. |

### 11.2 Profile, seed and derivation

`scripts/diversification_profile.py` is the reference implementation.

- **Profile** (`diversification-profile.v3.schema.json`), canonical JSON:
  `schema: open-shorebird.diversification-profile`, `contract_version: 3`,
  `bytecode: {scheme: opcode-permutation-v1, format_version: 3, opcode_table_sha256, wire_opcodes}`,
  `native_layout: null`.
  - `wire_opcodes[i]` is the wire byte of public opcode `i`, in the order of
    `PUBLIC_KERNEL_BYTECODES_LIST` (`dart-sdk/runtime/vm/constants_kbc.h`),
    which is also dart2bytecode's `Opcode` enum order.
  - `opcode_table_sha256` is the SHA-256 of the opcode names (`Trap`, `Dup`,
    …, without the `k` prefix) joined by `\n`. A consumer whose own table
    hashes differently refuses the profile.
  - Rules beyond the schema: a permutation of `0..N-1`, `N` = the table size
    (≤ 256), `wire_opcodes[0] == 0`, and fewer than 16 fixed points besides
    `Trap` (a derived profile has about one; this refuses hand-made weak maps).
- **`profile_id`** = `sha256:` + hex SHA-256 of the canonical profile bytes.
  The bytecode header carries the 32 raw digest bytes.
- **Encoded form** (GN, `build.rs`): base64 of the canonical bytes, at most
  8192 characters. Consumers reject non-canonical base64 or JSON and
  duplicate keys.
- **Seed record** (`diversification-seed.v3.schema.json`): `schema:
  open-shorebird.diversification-seed`, `contract_version: 3`, `derivation:
  open-shorebird.diversification.v1`, `seed` (canonical base64 of 32 random
  bytes), `profile_id`. Private: `new` refuses a directory inside a git work
  tree and writes mode 0600 files.
- **Derivation `open-shorebird.diversification.v1`**:
  1. `subkey = HMAC-SHA256(seed, "open-shorebird.diversification.v1/bytecode-opcode-permutation")`.
  2. Stream: `HMAC-SHA256(subkey, uint32_be(counter))` blocks for counter
     0, 1, …, read as big-endian `uint32` values.
  3. `below(n)`: draw until `x < 2^32 - (2^32 mod n)`, return `x mod n`.
  4. `L = [1, …, N-1]`; for `i` from `len(L)-1` down to 1: `j = below(i+1)`,
     swap `L[i]` and `L[j]`. `wire_opcodes = [0] + L`.
- **Development profile.** `scripts/testdata/diversification/` holds a
  deliberately public seed (`SHA-256("open-shorebird DEVELOPMENT-ONLY diversification seed v1")`)
  and its profile, id
  `sha256:832856d0bed27833434e3f26b65d03cedc313f9850c2577ca7565c4dc644f46d`.
  As with the development root (§10.4), every build input refuses it unless
  development exceptions are on, and `--production` refuses it outright.

### 11.3 Profiled bytecode format (`DBP3`)

| Field | Canonical (`DBC3`, unchanged) | Profiled |
| --- | --- | --- |
| magic (UInt32) | `0x44424333` | `0x44425033` (`DBP3`) |
| format version (UInt32) | 3 | 3 |
| profile digest | absent | 32 raw bytes: the profile id's digest |
| section table | 14 × (count, offset) | same; offsets account for the 32 extra bytes |
| instruction opcode bytes | semantic | `wire_opcodes[semantic]` |

Everything else (operands, jump offsets, constant pool, metadata) is
identical, so a profiled component differs from the canonical one only in its
header and its opcode bytes.

- **dart2bytecode** takes `--bytecode-profile=<profile.json>` (absent: output
  unchanged). It validates the profile against its own opcode table, writes
  `DBP3`, and reads (`--patch-base`, full-replacement admission,
  `dump_bytecode`) only components whose header names the same profile. A
  `DBC3` input under a profile, a `DBP3` input without one, and a different
  digest all fail with a typed error.
- **VM** (`dart_bytecode_profile` GN arg, §11.5). A profiled VM accepts only
  `DBP3` with its compiled digest; a canonical VM accepts only `DBC3` (it
  already refuses `DBP3` as an unknown magic). A patch is refused with
  `RejectBytecodePatch`; a base component fails startup as before. The
  full-replacement opcode admission (`ReadBytecode`) checks the wire byte range
  and then the decoded opcode, so its forbidden-opcode categories are
  unchanged. The interpreter, the breakpoint patcher (the saved byte is a
  wire byte), the VM-internal instruction arrays (encoded at compile time),
  hot reload, the FFI validator and the disassembler all go through the same
  decode/encode functions.
- **SBR wrapper** (full replacement). The wrapper is unchanged. Its component
  must be `DBP3` with the expected digest when the build is diversified, and
  `DBC3` otherwise (flutter_tools `shorebird_replacement.dart`,
  `open_aot_patch_tools` `full_replacement.dart`).

### 11.4 Envelope binding

Encrypted interpreter envelopes (`open-aot-vmcode-encrypted-v1`) gain one
optional metadata field, `bytecode_profile_id` (the profile id string). It is
part of the AES-GCM associated data like every metadata field.

- `open_aot_patch_tools` `PatchMetadata.bytecodeProfileId` writes it only when
  set, so canonical envelopes are byte-identical.
- A profiled VM requires it and requires it to equal its compiled id; a
  canonical VM refuses an envelope that carries it. Both checks run with the
  other metadata checks, before the key callback and before decryption. Old
  VMs refuse it as an unknown field.

### 11.5 Compiled inputs

| Input | Where | Rule |
| --- | --- | --- |
| `dart_bytecode_profile` | Dart GN (`runtime/runtime_args.gni`) | encoded profile (§11.2) or `""`. `gn gen` validates it against `constants_kbc.h` (`runtime/tools/bytecode_profile.py`) and defines `DART_BYTECODE_PROFILE`, the wire table and the digest for every VM target. Requires `dart_enable_shorebird_interpreter`. This is the single source of the map: the engine reads the same arg. |
| `shorebird_security_profile = "hardened-diversified"` | engine GN (`common/config.gni`) | requires `dart_bytecode_profile` and `dart_enable_shorebird_interpreter`, plus every hardened requirement (toolchain ID, release root). `dart_bytecode_profile` is refused for `legacy` and `hardened`. Defines `SHOREBIRD_SECURITY_HARDENED=1` and `SHOREBIRD_SECURITY_DIVERSIFIED=1`. |
| `SHOREBIRD_DIVERSIFICATION_PROFILE` | `build_rust_updater.py` → `build.rs` | set only from `dart_bytecode_profile`, scrubbed otherwise. `security_inputs.rs` validates it (§11.2 rules) and generates `COMPILED_DIVERSIFICATION_PROFILE_ID`. The development profile needs development exceptions. |

The updater's compiled profile is one of three values. `hardened-diversified`
is hardened for every P1–P4 rule (signatures, HTTPS, integrity gate, release
root). `shorebird_compiled_security_profile()` returns
`hardened-diversified`, and the engine's `Updater::Init` cross-check compares
it with the engine's compiled profile as before.

### 11.6 Declaration, admission and build manifest v3

- **YAML.** flutter_tools writes, for `SHOREBIRD_SECURITY_PROFILE=hardened-diversified`
  with `SHOREBIRD_DIVERSIFICATION_PROFILE_ID`:
  `security_profile: hardened-diversified`, `security_contract_version: 3`,
  `toolchain_id`, `diversification_profile_id`, and the v3 manifest line. A
  checked-in `shorebird.yaml` containing `diversification_profile_id` is
  rejected in every profile, like the other reserved keys.
- **Admission** (updater `security::admit`):

  | Compiled | YAML declares | Result |
  | --- | --- | --- |
  | `legacy` | anything with `hardened-diversified` or contract 3 | reject (unchanged rule: only 1 or 2, profile mismatch) |
  | `hardened` | `diversification_profile_id` present, or `hardened-diversified` | reject |
  | `hardened-diversified` | `hardened-diversified`, contract 3, matching ID | hardened checks, then the gate |
  | `hardened-diversified` | anything else (missing or other ID, contract 2, `hardened`) | reject before keys or patches |

- **Build manifest v3** (`build-manifest.v3.schema.json`): v2 plus
  `diversification_profile_id`, with `contract_version: 3` and
  `security_profile: hardened-diversified`. Token
  `shorebird_build_manifest: v3.<manifest>.<signature>.<release-trust>`. The
  signature domain stays `open-shorebird.build-manifest-signature.v2:`: the
  domain separates document types and trust models, and v3 changes neither;
  the payload's contract version and the token prefix separate the versions. A
  hardened gate refuses a `v3.` token and a diversified gate refuses `v2.`
  (`manifest_malformed`); a v3 manifest whose ID differs from the compiled ID
  fails `manifest_policy_mismatch`.

### 11.7 Toolchain, CLI and records

- **`toolchain_security.json` v3**: the v2 keys plus
  `diversification_profile_id`. v3 is used only by diversified toolchains,
  and a diversified toolchain must be v3. v1 and v2 files are read unchanged
  and must not carry the key.
- **`shorebird release --security-profile=hardened-diversified`**: every
  hardened refusal (§7.5, §10), plus:
  - the toolchain must be a v3 diversified toolchain;
  - `SHOREBIRD_DIVERSIFICATION_PROFILE` must name a valid profile whose ID
    equals the toolchain's;
  - the release must use the bytecode interpreter
    (`aot_patch_runtime_mode: dart-bytecode-interpreter`); every other
    platform or mode is refused before building.
  The build gets the hardened environment plus
  `SHOREBIRD_DIVERSIFICATION_PROFILE_ID`; every dart2bytecode invocation gets
  `--bytecode-profile`; every envelope gets `bytecode_profile_id`. Legacy and
  hardened argv, environments and envelopes are unchanged.
- **`shorebird patch`** on a diversified toolchain applies the same checks
  and inputs, so a patch can only be produced for the release's profile.
- **`release-record.v3`**: v2 plus `diversification_profile_id`; all records
  merged for one release must agree on it.
- **`security-policy.v3`**: v2 with `diversification_profile_id` a
  `sha256:` ID, required exactly for `hardened-diversified`, and `null`
  otherwise. `build-identity` and `artifact-catalog` accept contract v3 only
  for `hardened-diversified` with a non-null ID; the toolchain ID formula of
  §3 already includes the profile ID, so caches keyed by toolchain ID are
  profile-scoped. Hardened catalogs and diversified catalogs follow the same
  signer rule (§10.6).

### 11.8 Native layout and BoringSSL

- **BoringSSL symbol prefixing: evaluated and not adopted.** P0 found no
  BoringSSL export on any platform (`HARDENING_P0_EVIDENCE.md` §1, finding
  2), and release engines are stripped of local symbols except the Android
  Maven `libflutter.so`, which still carries debug information. Prefixing
  renames symbols that a shipped binary does not expose, so it does not change
  what a symbol-, offset- or signature-based hook sees. Stripping the Android
  library is the cheaper and stronger control and stays a P6 measurement
  item. BoringSSL keeps the pinned upstream revision, its GN build and its
  assembly; standard TLS and cryptography are unchanged.
- **Native layout diversification is reserved** (`native_layout` must be
  `null` in v3). Deterministic function ordering and Apple/Linux export
  minimization change shipped engine bytes on every platform, so they need
  native-host and device measurement with extraction and hook tools before a
  claim can be made (plan §5.4, §9). That evaluation is P5b. A later contract
  version may define the subprofile; v3 consumers refuse a non-null value.

### 11.9 Reject/accept matrix

Every row is covered by a named test. Evidence classes: **unit** (Rust, Dart,
Python, C++ unit tests), **build** (GN/`build.rs` refusals), and **host
runtime** (the AOT harnesses run a real VM built with and without the
DEVELOPMENT-ONLY profile and apply real patches). There is no device evidence
yet (§11.10).

Profile, seed and inputs:

| Case | Result | Tests |
| --- | --- | --- |
| Reference derivation of the published seed; 16 further seeds | the committed profile; valid, ≤ 15 fixed points | `test_diversification_profile.py` `DerivationTest`; Dart `bytecode_profile_test.dart` (same wire prefix) |
| Python reference, VM generator, dart2bytecode, updater, CLI agree on validity and ID | identical | `VmGeneratorAgreementTest`, `CrossRepoFixtureTest`; `security_inputs::tests::rendering_is_deterministic_rust`; CLI `computes the reference profile id of the shared fixture` |
| Non-canonical base64/JSON, duplicate or extra key, `native_layout` ≠ null, other schema/contract/scheme/format, wrong opcode-table hash, not a permutation, Trap moved, ≥ 16 fixed points, oversized | refused by every consumer; `gn gen` fails | `ProfileRulesTest`, `VmGeneratorAgreementTest.test_refuses_what_the_reference_refuses`; `malformed_diversification_profiles_fail_the_build`; py `test_malformed_profiles_are_refused`; CLI `BytecodeProfile refuses …`; Dart `invalid profiles are refused` |
| DEVELOPMENT-ONLY profile without development exceptions, or with `--production` | refused | `test_check_and_production`; `development_profile_needs_a_development_build`; py `test_development_profile_needs_development_exceptions`; flutter_tools `the DEVELOPMENT-ONLY diversification profile needs a development build`; CLI `refuses the DEVELOPMENT-ONLY profile on a production toolchain` |
| `new` into a git work tree (without `--development`) | refused; files mode 0600 otherwise | `CliTest.test_refusals`, `test_new_derive_verify_and_gn_args` |
| Diversified engine without toolchain ID, release root, interpreter or profile; profile for `legacy`/`hardened` | build fails; inherited value scrubbed | `diversified_has_every_hardened_requirement`, `diversification_profile_is_refused_outside_diversified` (Rust and py); GN asserts (spot-checked) |
| v3 documents: profile ID required exactly for `hardened-diversified`; v3 for another profile; v1/v2 naming a profile; DEVELOPMENT-ONLY ID in a production manifest | accept / reject | `test_hardening_contracts.py` `test_v3_is_only_for_hardened_diversified`, `test_diversification_and_development_rules`, `test_reserved_and_versioning` |

Bytecode and VM (host runtime, both builds):

| Case | Canonical VM | Diversified VM |
| --- | --- | --- |
| Own patch (body; structural and full replacement, plain and encrypted, non-PRODUCT and PRODUCT) | accept (DBC3) | accept (DBP3) |
| DBP3 patch from the DEVELOPMENT-ONLY profile | reject ("profile does not match this VM") | — |
| Canonical DBC3 patch | — | reject |
| Patch from another valid profile (two wire bytes swapped) | — | reject (digest) |
| Envelope with `bytecode_profile_id` | reject before the key callback | required; another ID rejected before the key callback |
| Envelope without `bytecode_profile_id` | accept (byte-identical) | reject before the key callback |

Host runs: macOS arm64 (local, canonical and DEVELOPMENT-ONLY builds, PRODUCT
structural and full replacement included) and the Linux x64 CI job. Tests:
`dart-sdk/tools/test_aot_bytecode_patch.py`
(`--bytecode-profile`, `--foreign-bytecode-profile`),
`test_aot_structural_patch.py` and `test_aot_full_replacement.py`
(`--bytecode-profile`), `scripts/verify_encrypted_aot_runtime.py`
(`RejectBeforeKey`: the key-request count is unchanged). dart2bytecode
(`bytecode_profile_test.dart`): DBP3 header and digest, a profiled component
differs from the canonical one only in header and opcode bytes and decodes back
to it; the component reader (shared by `--patch-base`, full-replacement
admission and `dump_bytecode`) refuses DBC3 under a profile, DBP3 without one
and another digest; `--patch-base` across profiles and an invalid
`--bytecode-profile` fail without writing output. Existing `patch_test`, `full_replacement_test`,
`bytecode_generator_test` (golden output) are unchanged and pass.

Updater, engine and flutter_tools:

| Case | Result | Tests |
| --- | --- | --- |
| Diversified updater, matching diversified YAML | accept; hardened floor forced | `security::tests::diversified_accepts_the_build_output_and_forces_the_hardened_floor`; `config::tests::hardened_admission::diversified_updater_boots_the_diversified_build_output` |
| Diversified updater: missing, other, uppercase or empty ID; contract 2; `hardened`, missing or unsigned declaration; duplicated ID | reject before keys or patches | `diversified_rejects_another_profile_or_declaration`; `diversified_refusals_publish_nothing_on_any_entry_point` |
| Hardened updater given the ID or the diversified output | reject (`UnexpectedDiversificationProfile` / contract 3 / `ProfileMismatch`) | `hardened_rejects_a_diversification_declaration`, `hardened_rejects_incompatible_identity_and_versions` |
| Legacy updater given the diversified declaration or contract 3 | reject | `legacy_updater_refuses_a_hardened_declaration`, `legacy_accepts_known_contract_versions_only` |
| Diversified keeps signatures and HTTPS | unsigned refused; `http` refused | `cache::signing::tests::diversified_policy_never_accepts_unsigned_content`; `hardened_download_refuses_http_before_any_request` |
| Gate: v3 token under diversified | accept (config, module and trust rules still apply) | `integrity::tests::diversified_gate_accepts_the_v3_build_output` |
| Gate: v3 token to hardened; v2 token to diversified; v2 manifest carrying the key | `manifest_malformed` | `each_gate_accepts_only_its_own_token_version` |
| Gate: other ID | `manifest_policy_mismatch` | `diversified_manifest_binds_the_compiled_profile` |
| Gate: missing, null, malformed or duplicate ID; contract 2 or `hardened` inside v3 | `manifest_malformed` / `manifest_policy_mismatch` | `diversified_manifest_binds_the_compiled_profile` |
| flutter_tools v3 output verified by the Rust gate | accept; refused by a hardened gate or under another profile | `integrity::tests::flutter_tools_v3_output_verifies`; Dart `matches the v3 token the updater verifies` |
| Compiled profile name across C API and engine; engine and updater compiled differently | `hardened-diversified`; engine refuses `Init` | `compiled_security_profile_names_each_contract_profile`; engine `UpdaterSecurityProfile.*` (incl. `LinkedRustUpdaterMatchesTheEngineProfile`) |
| flutter_tools YAML: v3 declarations; ID required exactly for diversified; ID is a reserved checked-in key in every profile | accept / reject | `hardened-diversified writes the contract v3 declarations`, `the diversification profile ID is required exactly for hardened-diversified`, `source yaml cannot contain the reserved declaration keys in any profile` |
| flutter_tools manifest and record | v3 token and `release-record.v3`; hardened record stays v2 | `hardened-diversified writes the v3 manifest and a release-record.v3`, `a hardened release record stays release-record.v2` |
| Catalog: v3 for own profile | accept | `hardened-diversified: a contract v3 catalog naming its own profile` |
| Catalog: other ID; v3 to hardened or legacy; v1 to diversified; unsigned; dev key; non-delegate, role-confused or revoked signer; no release trust | reject | `hardened-diversified: catalog profile, contract and signer rules`, `contract v3 is admissible only for hardened-diversified with a profile` |
| SBR wrapper: DBP3 with matching digest | accept | flutter_tools `accepts a DBP3 component bound to the configured profile`; open_aot_patch_tools `is accepted with the expected digest` |
| SBR wrapper: DBC3 under diversified; other or no ID; DBP3 under legacy or hardened; wrong version, digest or truncated header | reject | `refuses a DBC3 component, another profile, or no profile ID`, `refuses a DBP3 header with a wrong version or digest`; open_aot_patch_tools `DBP3 profiled component (contract v3)` group |

CLI and patch tools:

| Case | Result | Tests |
| --- | --- | --- |
| Diversified toolchain: iOS or iOS-framework interpreter release, macOS full replacement | accept; diversified variables, `--bytecode-profile`, envelope ID and v3 record | `release_command_test.dart` group `on a hardened-diversified toolchain` |
| Diversified toolchain: Android, Linux, Windows, iOS without the interpreter declaration, `ios-aot-link`, native-AOT override, macOS without full replacement | reject before building | `refuses a non-interpreter release` group; patch `refuses a non-interpreter patch` |
| No or invalid `SHOREBIRD_DIVERSIFICATION_PROFILE`; a profile the toolchain was not built with; DEVELOPMENT-ONLY profile on a production toolchain; development toolchain for release | reject before building | release and patch groups `on a hardened-diversified toolchain` |
| Every hardened refusal (§7.5, §10) | still applies | `still applies every hardened refusal`, `still applies the hardened refusals` |
| `hardened-diversified` on a legacy or hardened toolchain; `hardened`/`legacy` on a diversified one | reject | `refuses a hardened-diversified release`, `refuses a hardened release`, `refuses a legacy release` |
| `toolchain_security.json` v3 rules; v1/v2 naming a profile; v3 without the ID or for another profile | accept / reject | `security_profile_test.dart` group `refuses a contract v3 file`, `reads a contract v3 hardened-diversified toolchain` |
| Release records: v2 for diversified, v3 for another profile or for hardened, records that disagree | reject | `readReleaseRecord` group `hardened-diversified (release-record v3)` |
| Envelope `bytecode_profile_id`: omitted when unset; authenticated when set; malformed | byte-identical / tamper fails decryption / reject | `patch_artifact_test.dart` group `bytecode_profile_id (contract v3)` |

Legacy and hardened (byte identity): the existing golden and exact-output
tests are unchanged and pass (updater YAML and admission, `_kUpdaterGolden`,
flutter_tools `legacy output is unchanged`, hardened declaration and v2
manifest/record tests, CLI exact-argv and canonical-envelope tests,
dart2bytecode golden output, the canonical VM harnesses).

Implementation decisions recorded here (not in the plan):

- **iOS framework** releases use the same interpreter runtime as iOS apps and
  are admitted; every non-interpreter release is refused.
- **A legacy updater ignores a lone `diversification_profile_id` key**, as it
  ignores every unknown YAML key today; it still rejects the
  `hardened-diversified` declaration and contract 3. Legacy behavior stays
  byte-identical. The CLI and flutter_tools refuse the key in a checked-in
  `shorebird.yaml` for every profile.
- **The envelope field is optional and omitted when unset**, so canonical
  envelopes and their AAD are unchanged; a key present with a null or
  malformed value is refused.
- **The updater validates the encoded profile without a crypto dependency**:
  a valid profile has exactly one canonical encoding, which `build.rs`
  compares byte for byte; its SHA-256 is pinned against the `sha2` crate.

### 11.10 Deferred

- **P5b.** Native layout subprofile and its measurement (§11.8); extraction
  and TLS-hook tool assessment on control versus diversified builds (plan §9
  "Extraction resistance"); device performance against the ≤5% CPU / ≤10% RSS
  budgets; the native ARM64 FFI full-replacement fixture
  (`verify_encrypted_aot_runtime.py --full-replacement-ffi` refuses
  `--bytecode-profile` until it is wired). The CI job
  `custom-dart-sdk-diversified` (Linux x64) covers body, structural and full
  replacement, plain and encrypted, and PRODUCT structural and full
  replacement; macOS arm64 was checked locally (§11.9).
- **P4b.** Server storage and enforcement of the release record, which
  now includes `diversification_profile_id`.
- **P6.** `hardened_toolchain.py --profile hardened-diversified` produces the
  engine args, `toolchain_security.json` v3 and the toolchain ID, and
  `publish_artifact_catalog.sh` produces the v3 catalog (§12.6). Building
  the release-lineage engine with the private profile, and lifecycle and
  device evidence for diversified releases, are operator actions (§12.9).

### 11.11 Operator procedures

| Step | Command |
| --- | --- |
| Create a lineage profile (once per native-release lineage) | `scripts/diversification_profile.py new --output-dir PRIVATE` (prints the profile ID; writes `diversification-seed.json` and `diversification-profile.json`, mode 0600) |
| Engine build inputs | `scripts/diversification_profile.py gn-args PRIVATE/diversification-profile.json --production` prints `shorebird_security_profile`, `dart_bytecode_profile` and the `toolchain_security.json` fields |
| Release and patch machines | `SHOREBIRD_DIVERSIFICATION_PROFILE=PRIVATE/diversification-profile.json` |
| Reproduce or recover | `scripts/diversification_profile.py derive PRIVATE/diversification-seed.json` re-creates the profile byte for byte; a lost seed and profile mean a new native release with a new profile, never a guessed map |
| Check | `scripts/diversification_profile.py verify PROFILE --production --expect-id ID` |

## 12. P6 decisions (platform acceptance and rollout)

P6 closes the hand-offs of §9.7, §10.9 and §11.10 that need no production
key, owner secret or physical device, and turns each remaining one into a
single command for the operator (§12.9). It adds no contract version. The
one document change is an optional field, `windows_signing_cert_sha256`, in
`package_identity` of the v2 and v3 build manifests (§12.4). The updater and
flutter_tools that read and write it ship together in one toolchain, and a
release always runs the updater it was built with, so no released app ever
reads a manifest written by a newer toolchain. Legacy output and behaviour
are unchanged: every P6 path runs only for `hardened` and
`hardened-diversified`.

### 12.1 Evidence classes

| Class | What it shows | Where |
| --- | --- | --- |
| Unit / integration (host) | gate, adapters, packaging, re-verification and tooling rules, including every refusal | Rust updater suite, flutter_tools and CLI tests, Gradle plugin tests, `scripts/test_*.py` |
| Engine unit tests, hardened build | the engine compiled with `shorebird_security_profile = "hardened"` and a release-root pin runs its integrity and profile controls | `shorebird_unittests` of a hardened `macos_release_arm64` (local) and the CI leg in §12.6 |
| Windows host (ARM64 VM) | the Authenticode adapter on a real `WinVerifyTrust` | `windows_adapter_verifies_authenticode_on_the_open_file` and the integrity suite on the Parallels "CWindows 11" VM |
| Real install, desktop | the gate on a built, installed app: tamper, replace, reinstall, startup cost | `scripts/hardened_lifecycle.py desktop` on macOS arm64 (§12.7) |
| Device (Android, iOS) | the same on devices | operator action (§12.9); the runbook is `scripts/hardened_lifecycle.py android` / `ios` |

All P6 evidence uses DEVELOPMENT-ONLY trust material: the committed release
root, delegations and build-manifest key, and a throwaway patch key. That is
enough to show the gate's behaviour. It says nothing about production key
custody, which stays an operator action.

### 12.2 Android engine measurement

Gradle resolves `libflutter.so` from Maven (`io.flutter:<abi>_<mode>`), out
of flutter_tools' sight (§9.2). P6 hands those jars to the build:

- The Flutter Gradle plugin (`ShorebirdEngineJars.kt`, one optional
  `@InputFiles` on `BaseFlutterTask`, like `shorebirdTraceFile`) takes, for a
  hardened build only (`SHOREBIRD_SECURITY_PROFILE` set and not `legacy`),
  the per-ABI engine jars of the variant's `<variant>RuntimeClasspath`
  through an artifact view filtered to `io.flutter:*_<mode>` modules. It
  passes their absolute paths to `flutter assemble` as the JSON array
  `SHOREBIRD_ANDROID_ENGINE_JARS`. A legacy build never resolves anything
  new.
- flutter_tools (`shorebirdAndroidEngineModuleMeasurements`) opens each jar
  and measures its single `lib/<abi>/libflutter.so` under the §9.2 ELF rule.
  The ABIs map to `aarch64`, `arm`, `x86_64` and `x86`. It refuses to sign
  when the variable is unset or empty, is not a JSON array of strings, names
  a missing jar, or a jar has no `libflutter.so`, more than one, an unknown
  ABI, or repeats an ABI.

The packaged library is the jar's library: AGP copies it without change, and
strips only the symbol table, which is not in a `PT_LOAD` segment. The
Android re-verification (§12.5) checks exactly that on the distributed AAB
and APK.

### 12.3 Strict module binding on every platform

§9.2 accepted a missing module entry on Android only. Now that Android
entries exist, that exception is gone. A hardened manifest without an entry
for the running platform and architecture is refused with `module_unbound`
everywhere. A release built by an earlier toolchain keeps its own (earlier)
updater, so this does not affect apps already built.

The gate's cost is logged on every hardened start as
`Integrity check passed (N us)`, and the failure line also carries the
microseconds. That log line is the startup-cost measurement point of §12.7.

### 12.4 Windows Authenticode identity

| | |
| --- | --- |
| Declaration | `SHOREBIRD_WINDOWS_SIGNING_CERT_SHA256`: one or more SHA-256 digests (hex or colon form) of the DER Authenticode signing certificate. Several digests allow rotation. flutter_tools and the CLI refuse it for any other platform. |
| Manifest | `package_identity.windows_signing_cert_sha256`: a sorted, non-empty list of lowercase hex digests (optional; v2 and v3 schemas, validator uniqueness) |
| Adapter | `package_identity::windows_module_signers` checks the loaded engine module's file. It opens the file with `FILE_SHARE_READ` only, so nobody can write to or replace it during the check. It re-measures those bytes and requires them to equal the measurement of the loaded module, so the signer belongs to the bytes that run. It then calls `WinVerifyTrust(WINTRUST_ACTION_GENERIC_VERIFY_V2)` on the same handle, with no UI, `WTD_REVOKE_NONE` and `WTD_CACHE_ONLY_URL_RETRIEVAL`: the device check never uses the network, like the clock-free rule of §10.3. Only `ERROR_SUCCESS` is accepted. The signer is the leaf certificate of the primary signer (`WTHelperProvDataFromStateData` → `WTHelperGetProvSignerFromChain` → `WTHelperGetProvCertFromChain`), and its DER SHA-256 must be listed. The state is always closed. |
| Failures | an unsigned or invalid file, a file that differs from the loaded module, or no signer gives `package_identity_unavailable`. A signer not listed gives `package_identity_mismatch`. |
| Not covered | revocation (offline by design; rotation by listing digests is the control), catalog-signed files, and the signature of the executable or other DLLs (the engine module contains the updater, §9.2) |

### 12.5 Re-verification of the final distributed artifact

`shorebird release` (hardened and hardened-diversified) re-verifies what it
is about to upload, after the platform build and after the release record is
built. A failure stops the release before anything is uploaded. It runs the hidden flutter_tools command
`flutter shorebird-verify-artifact --artifact=<path> --platform=<p>
--record-manifest=<sha256>...` with the same security environment as the
build (`shorebird_release_artifact.dart`).

- It reads the artifact as a directory (`.app`, xcarchive, Windows
  `Release`, Linux bundle) without following symbolic links, or as a ZIP
  (`.aab`, `.apk`, `.ipa`). It finds every bundled `shorebird.yaml` and every
  packaged engine library: Android `lib/<abi>/libflutter.so`, iOS
  `Flutter.framework/Flutter` (simulator slices ignored), macOS each slice of
  `FlutterMacOS`, Windows `flutter_windows.dll` (architecture from the PE
  machine), Linux `libflutter_linux_gtk.so` (from the ELF machine).
- Every manifest is verified the way the updater verifies it: last line,
  single occurrence, `v2.`/`v3.` prefix, release trust against the compiled
  root, role key, Ed25519 signature, policy fields (profile, toolchain,
  development flag, root, delegations digest, diversification profile), and
  the configuration digest. Its digest must be one of the release record's
  `build_manifests`.
- Every packaged engine library must have a manifest module entry for its
  platform and architecture (`module_unbound`), with an equal measurement
  (`module_mismatch`).
- Refusal codes: `artifact_missing`, `no_config`, `no_engine`,
  `unsupported`, `ambiguous`, `not_recorded`, `module_unbound`,
  `module_mismatch`, and the manifest codes. The CLI prints the code and
  fails the release.

The CLI skips an Android archive (AAR), and logs that it did. Its host app
packages the engine, so the AAR has no engine to bind. Package identities
(APK signer, team ID, Authenticode) are applied by the store or the
operator after the build. Re-verification therefore checks binding and
measurement only, and the device checks identity.

### 12.6 Hardened toolchain tooling and CI

`scripts/hardened_toolchain.py` is the single description of a hardened or
hardened-diversified toolchain build. From the profile, the release-root pin
(`--release-root`/`OPEN_SHOREBIRD_RELEASE_ROOT`, `--release-pins` from
`provision_release_keys.sh`, or the DEVELOPMENT-ONLY root with
`--development`), the delegations floor and, for diversified builds, the
lineage profile, it prints or writes:

- `gn-args`: the engine `--gn-args` fragment (`shorebird_security_profile`,
  `shorebird_toolchain_id`, `shorebird_release_root`,
  `shorebird_release_min_delegations_version`, the development flag,
  `dart_bytecode_profile`);
- `toolchain-security`: `bin/internal/toolchain_security.json`, v2 for
  hardened and v3 for diversified, accepted by the CLI's
  `ToolchainSecurity.parse`;
- `verify-args`: checks a built `args.gn` against the description;
- `github-env`: the same values for a CI job.

The toolchain ID is computed the way `artifact_catalog.py` computes it (§3;
contract 3 plus the profile ID for diversified builds), so the engine, the
CLI and the catalog cannot disagree. Any DEVELOPMENT-ONLY input makes it a
development toolchain: the engine gets
`shorebird_security_development_exceptions = true`,
`toolchain_security.json` says so, and `shorebird release` refuses it.
`--production` refuses DEVELOPMENT-ONLY inputs outright.
`publish_artifact_catalog.sh` now also writes the v3 catalog of a diversified
toolchain (`OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE=hardened-diversified`
plus `OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE_ID`). It refuses one without
the profile ID, the production key or the release root.

CI (`open-shorebird-ci.yml`, checked by `verify_ci_workflow.rb`):

- `macos-engine-tests` gains one arm64 leg, `shorebird-hardened`. It builds
  the same engine configuration plus the `hardened_toolchain.py --development`
  GN args. `verify-args` checks its `args.gn`, and its `toolchain_security.json`
  is kept as evidence. It runs `shorebird_unittests`. The inventory
  validator requires every legacy control plus the integrity and
  security-profile controls. It also requires the log to show that the
  binary's compiled profile is hardened.
- `source-checks` runs `test_hardened_toolchain.py` and
  `test_hardened_lifecycle.py`. `cli-artifacts` runs the re-verification
  tests on every host.

The release engines CI publishes stay legacy. A published hardened toolchain
needs the production root (an owner variable). Its engine shares
`engine.version` with the legacy engines, so it also needs its own artifact
store and catalog origin. Both are operator actions (§12.9).

### 12.7 Lifecycle negatives and startup cost

`scripts/hardened_lifecycle.py` runs the lifecycle on a real installation in
four steps:

1. `baseline`: launched N times; the gate must pass.
2. `tampered-config`: `base_url` in the bundled YAML is redirected; the gate
   must refuse with `config_digest_mismatch`.
3. `replaced-engine`: one byte of a measured read-only section of the engine
   library is flipped (ELF `.rodata`, PE `.rdata`, Mach-O `__TEXT,__cstring`,
   every slice), and the package is re-signed the way an attacker can; the
   gate must refuse with `module_mismatch`.
4. `reinstalled`: the original is installed again and launched N times; the
   gate must pass.

It reads the gate's log lines, records the startup-cost samples and writes
JSON evidence. It exits 0 only if every expectation holds.

Result on macOS arm64 (2026-11-09, local hardened `macos_release_arm64`
engine with the DEVELOPMENT-ONLY root, a `flutter create` app, release
build). The artifact re-verification passed. Then:

| Step | Result |
| --- | --- |
| baseline ×5 | passed |
| tampered-config | refused `config_digest_mismatch`; the bundled release ran |
| replaced-engine (ad-hoc re-signed) | refused `module_mismatch`; the bundled release ran |
| reinstalled ×5 | passed |
| Gate cost (10 launches) | 9.0–66.1 ms, median 36.6 ms. The distribution is bimodal: about 9 ms when the framework pages are cached, and 55–66 ms otherwise. The gate hashes every non-writable section of the roughly 30 MB `FlutterMacOS` once per process. |

A debug-build host test (`integrity_gate_startup_cost_on_this_host`) gives
about 1.2 ms (p50) for the gate itself, excluding the measurement. So the
cost is the module measurement. A measurement that hashes only executable
sections, or the on-disk code-signing hash, would cost less. That is a
contract change that would need its own review, and it is not part of P6.

### 12.8 Reject/accept matrix (P6 additions)

| Case | Result | Tests |
| --- | --- | --- |
| Hardened Android build: engine jars from Gradle | one module entry per ABI | Gradle `ShorebirdEngineJarsTest`; flutter_tools `Android binds the libflutter.so of every engine jar Gradle resolved` |
| Android jars unset, empty, not JSON, missing, wrong layout, unknown or duplicate ABI | build refused | `Android refuses to sign without a measurable engine library for each jar` |
| Legacy Android build | no jar resolution, output unchanged | `only hardened builds hand engine jars to flutter assemble` |
| Hardened manifest without the running module (any platform, Android included) | reject `module_unbound` | `rejects_a_replaced_or_unmeasurable_engine_module` |
| Windows: listed signer / unlisted signer / malformed declaration | accept / `package_identity_mismatch` / manifest refused | `windows_authenticode_identity_binds_the_module_signer`, `windows_identity_shape_is_checked`; validator `test_windows_authenticode_identity_is_an_optional_p6_field` |
| Windows: unsigned file, file differs from loaded module, embedded-signed system file | `0x800B0100` refused / refused / one stable digest | `windows_adapter_verifies_authenticode_on_the_open_file` (Windows ARM64 VM) |
| Windows identity declared for another platform | build and release refused | flutter_tools `a Windows Authenticode identity is written only for Windows, only when declared`; CLI `security_profile_test.dart` |
| Distributed artifact: tampered YAML, replaced engine, extra unbound ABI, manifest not in the record, other toolchain, no config | release refused before upload | `shorebird_release_artifact_test.dart`; CLI `release_command_test.dart` (`a distributable that fails re-verification is never uploaded`) |
| Legacy release | never re-verified | `release_command_test.dart`; `re-verification is only for hardened toolchains` |
| Toolchain inputs: development root without `--development`, development inputs with `--production`, malformed pins, legacy engine with security args, diversified non-interpreter engine | refused | `test_hardened_toolchain.py` |
| Diversified catalog: other profile ID, missing ID, hardened verifier, development signature | refused | `test_artifact_catalog.py` `test_hardened_diversified_catalog_names_its_profile`, `test_hardened_diversified_catalog_needs_the_profile_id` |
| Installed macOS app: tampered YAML / replaced engine / reinstall | `config_digest_mismatch` / `module_mismatch` / pass | §12.7 (`hardened_lifecycle.py`; orchestration in `test_hardened_lifecycle.py`) |

### 12.9 Operator and device actions

Every action below is a single command. Each needs a production key, an owner
secret or variable, or a physical device. None was faked.

- [ ] **Production release root and delegations.**
      `scripts/provision_release_keys.sh` (`docs/RELEASE_KEYS.md`), then set
      `OPEN_SHOREBIRD_RELEASE_ROOT`, `OPEN_SHOREBIRD_RELEASE_TRUST` and the
      catalog key as described there.
- [ ] **Hardened toolchain engines.** Build with
      `--gn-args="$(scripts/hardened_toolchain.py --profile hardened --production gn-args)"`,
      or `--profile hardened-diversified --diversification-profile PRIVATE/diversification-profile.json`.
      Write `bin/internal/toolchain_security.json` with
      `scripts/hardened_toolchain.py ... --production toolchain-security --output ...`,
      and publish the catalog with `OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE`
      set to the profile, to a store separate from the legacy mirror.
- [ ] **Android device.** On a release built with the hardened toolchain and
      the release keystore, run
      `scripts/hardened_lifecycle.py --output android.json android --apk app-release.apk --package <id> --keystore <ks> --ks-pass env:KS_PASS`.
      It needs adb and build-tools. Also declare
      `SHOREBIRD_ANDROID_SIGNING_CERT_SHA256` to cover the APK-signer path.
- [ ] **iOS device.** On a signed `Runner.app`, run
      `scripts/hardened_lifecycle.py --output ios.json ios --app Runner.app --bundle-id <id> --device <udid> --identity "<Apple Development identity>"`.
      It uses `xcrun devicectl`. The re-signed mutated copies need a
      development profile that covers the device.
- [ ] **Windows and Linux installs.** Run
      `scripts/hardened_lifecycle.py --output windows.json desktop --platform windows --app build\windows\arm64\runner\Release`
      on a Windows host with an Authenticode-signed `flutter_windows.dll`,
      and the `linux` equivalent on a Linux bundle.
- [ ] **Startup cost on devices.** These are the `startup_cost` samples in
      the Android and iOS evidence. Compare them with the macOS figures in
      §12.7.

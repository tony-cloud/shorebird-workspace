# Hardening contracts, version 1

Status: **v1, frozen by stage P0** of
[the hardening plan](HARDENING_AND_UPSTREAM_DEPENDENCY_PLAN.md) on 2026-11-09.
P1 (secure release admission) and P2 (official-source resolution) implement
against this document. **P1 is implemented** (2026-11-09) in the updater,
engine, flutter_tools and CLI; §7 records its decisions and its
reject/accept matrix. The server, catalog and P2 parts are not implemented yet. Evidence that informed it is in [HARDENING_P0_EVIDENCE.md](HARDENING_P0_EVIDENCE.md).

Machine-checkable parts:

| Contract | Schema | Valid examples |
| --- | --- | --- |
| Security policy | `docs/schemas/security-policy.v1.schema.json` | `docs/schemas/examples/security-policy.legacy-default.json`, `security-policy.hardened.json` |
| Build-identity manifest | `docs/schemas/build-identity.v1.schema.json` | `docs/schemas/examples/build-identity.baseline-ae8a61a.json` |
| Artifact catalog | `docs/schemas/artifact-catalog.v1.schema.json` | `docs/schemas/examples/artifact-catalog.baseline-ae8a61a.json` |

`scripts/validate_hardening_contracts.py` checks a document against its schema
and the cross-field rules below. It has no third-party dependency: it implements
exactly the JSON Schema keywords the schemas use and refuses any other keyword.
`scripts/test_hardening_contracts.py` runs in the CI "Verify CI scripts" step.

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


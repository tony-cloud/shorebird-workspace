# Signed variant selection protocol v2

Status: implementation contract, not an implemented capability. The matching
[golden vectors](../testdata/variant_selection_v2.json) define deterministic
encoding, signature, and decision examples. They do not contain executable
patches or production keys. Existing component implementations remain v1.

## Scope and ownership

One installed application can select free, pro, or enterprise repeatedly while
retaining its bundle/package identity, application data, Shorebird app ID, native
release, and original patch baseline. Every switch is authorized online by the
operator or the application's authenticated backend. Offline operation continues
the last accepted selection; arbitrary disconnected switching is outside v2.
Selection takes effect on the next cold process launch, not by modifying running
isolates. Existing process-wide boot selection must also cover later engines in
that process.

Patch numbers identify immutable content. Selection epochs order authorization.
Selecting patch 2 after patch 3 is valid at a greater epoch. Selecting bundled
base is an explicit free selection. Neither operation rolls a patch back for
other installations. Channel remains rollout routing and CLI `--flavor` remains
build-time app-ID resolution; neither grants a license.

The server owns entitlement decisions and epoch allocation. The updater owns
durable accepted authorization and boot selection. The engine supplies immutable
release identity and applies the selected artifact. Native application adapters
own authenticated key provisioning and protected key storage. The VM continues
to validate/decrypt artifact metadata and original-base/runtime constraints; it
does not own licensing, selection epochs, or network access.

## Release and artifact identity

A release declares `selection_protocol` as `1` or `2` at creation; it is immutable
even while draft. Existing releases migrate explicitly to `1`.
A v2 release embeds strict artifact verification, required response signatures,
the release signing public key, and the following immutable scope fields:

| Field | Meaning |
| --- | --- |
| `app_id` | Canonical lowercase UUID for this installed app; shared by all variants. |
| `release_version` | Exact UTF-8 release version, without normalization. |
| `platform` | `android`, `ios`, `macos`, `windows`, or `linux`. |
| `arch` | Updater wire architecture: `arm`, `aarch64`, `x86`, or `x86_64`. |
| `base_artifact_hash` | Lowercase SHA-256 of the exact original patch baseline. For native patches this is the original AOT library; for interpreter patches it is the original released bytecode baseline. |
| `host_abi_hash` | Released full-replacement host ABI SHA-256 for `interpreter-full`; explicitly `null` for other profiles in this protocol version. |
| `runtime_profile` | `native-aot`, `interpreter-body`, `interpreter-structural`, or `interpreter-full`. |

The server release record must contain and validate the same scope. An existing
release/platform/architecture cannot be republished with another native host or
baseline. CLI publication must retain its native archive/plugin/asset comparison
against the original release. `base_artifact_hash` alone does not fingerprint
every native plugin: the immutable release record and native publication guard
are part of this invariant. `host_abi_hash` preserves the existing full-profile
host descriptor semantics; it is not a newly invented host hash format.

All patches for the scope are compiled against that original baseline, never
against the currently selected variant. A content number is immutable within
release/platform/architecture and cannot be re-uploaded with different bytes.
The signed `artifact_hash` has the existing updater meaning: the installed
artifact hash, not the compressed transport hash. For interpreter updates this
is the complete encrypted artifact consumed by the engine.

Add `client_id_hash = SHA256(UTF8(client_id))` to form the installation scope.
The raw client ID is neither normalized nor a credential. Its hash is routing
metadata, not proof of entitlement or hardware identity. A public check can read
an assigned selection but cannot create a paid entitlement. Key provisioning
must independently authenticate the account/installation and evaluate the grant.

## Stable selection descriptor

Every field below is required, including explicitly nullable fields. No unknown
or duplicate keys are permitted at any level. Integer-like values are JSON
strings, not JSON numbers; this avoids 32-bit and JavaScript precision loss.

```json
{
  "epoch": "11",
  "scope": {
    "app_id": "11111111-1111-4111-8111-111111111111",
    "release_version": "1.0.0+1",
    "platform": "macos",
    "arch": "aarch64",
    "client_id_hash": "<64 lowercase hex>",
    "base_artifact_hash": "<64 lowercase hex>",
    "host_abi_hash": null,
    "runtime_profile": "interpreter-structural"
  },
  "channel": "stable",
  "variant_id": "pro",
  "license_type": "pro",
  "entitlement_revision": "account-grant-7",
  "target": {
    "kind": "patch",
    "patch_number": "2",
    "artifact_hash": "<64 lowercase hex>",
    "encryption": "artifact-aes-256-gcm",
    "artifact_key_id": "licensed-pro-key"
  },
  "not_before": "1793491200",
  "offline_valid_until": null,
  "fallback": {"kind": "base", "variant_id": "free", "license_type": "free"}
}
```

Rules:

- `epoch`: canonical decimal `1..9223372036854775807`. No sign or leading zero.
  Store/compare with a 64-bit integer even on 32-bit targets. Exhaustion is a
  hard error; never wrap or reset. Request epoch `0` denotes no accepted grant.
- `patch_number`: canonical decimal `1..2147483647`, or `null` for base. Content
  numbers are not compared for selection freshness.
- Times: canonical decimal Unix seconds `0..253402300799`, UTC, no fraction.
  `not_before` is required. `offline_valid_until` is either a strictly greater
  time or explicit `null`. A grant is usable when `now >= not_before` and, if
  bounded, `now < offline_valid_until`. Equality at expiry is expired.
- `offline_valid_until: null` means a perpetual offline grant, permitted only
  when the authenticated backend's policy allows it. There is no implicit paid
  expiry or default lease duration. A policy requiring a bound must reject null.
- `variant_id`, `license_type`, and `channel` are nonempty ASCII identifiers
  matching `[a-z][a-z0-9_-]{0,63}`. There is no built-in numeric tier ordering.
- Release version is 1..256 UTF-8 bytes. Entitlement revision is a required,
  non-null string of 1..128 UTF-8 bytes. Artifact key ID is either explicit null
  where the target rules permit it or a string of 1..128 UTF-8 bytes. Reject
  U+0000..U+001F and U+007F in these strings, invalid UTF-8, and unpaired
  surrogates. Do not normalize Unicode.
- `entitlement_revision` is an opaque nonsecret backend policy revision. It is
  not a credential. A policy/grant change requires a new epoch even if the
  selected content is unchanged.
- For `target.kind: base`, variant and license are both `free`, patch number,
  artifact hash, and artifact key ID are null, and encryption is `none`.
- For a native patch, encryption is `transport-aes-256-gcm`, artifact key ID is
  null, and delivery must contain the existing authenticated transport encryption
  metadata. For an interpreter patch encryption is `artifact-aes-256-gcm`,
  artifact key ID is non-null, and delivery transport encryption is null. An
  interpreter envelope's authenticated key ID, flavor, and license must equal
  this descriptor's values. V2 does not negotiate plaintext patch delivery or
  double encryption.
- `fallback` is exactly the free base object shown above. It is unconditional
  availability of the bundled free host, not another paid grant. A free variant
  may itself select a patch; the bundled base remains its safe fallback.

### Existing content-expiry upper bound

An artifact's existing `offline_expires_at` is an immutable publisher-imposed
upper bound, independent of the installation's selection lease. If the artifact
has a finite expiry, assignment must supply a finite `offline_valid_until` no
later than `floor(artifact expiry as UTC Unix seconds)`; reject null or a later
deadline with 409 `selection_exceeds_content_expiry`. Do not silently change the
requested lease. The floor conversion can conservatively shorten validity by
less than one second and cannot extend a fractional content deadline. Normal
`not_before < offline_valid_until` validation still applies. A finite content cap
at or before the server's current time cannot be assigned. An artifact without
an expiry may receive a perpetual grant when the authenticated backend's policy
permits it. There is no new default content or paid-license duration.

For interpreter artifacts, the publisher must propagate the authenticated
envelope's expiry into the existing patch-artifact upload `offline_expires_at`
field, preserving the timestamp before the grant's whole-second conversion.
Absence is valid only when the envelope has no content expiry. The server trusts
this publisher attestation; the engine/VM must still independently enforce the
authenticated envelope deadline. For native artifacts the uploaded content cap
must constrain the signed descriptor so native clients do not depend on the
legacy unsigned/ignored response expiry field. Mapping changes, re-upload of the
same content identity, and same-epoch refresh cannot remove or extend this cap.

Current Dart protocol/client upload models do not forward `offline_expires_at`;
the v2 publisher integration must add that propagation. Device resolution also
checks the selected content cap and replaces an expired target with a newer
free-base grant, even if a persisted selection erroneously has a later deadline.
Neither content expiry nor selection expiry can be relaxed by the other layer.
The golden vectors use synthetic artifacts with no additional content cap;
their bounded leases are selection deadlines, so this rule does not change them.

### Deterministic descriptor digest

The stable digest is SHA-256 of a binary encoding, rendered as 64 lowercase hex
characters. This is deliberately independent of JSON key order, escaping,
whitespace, request nonce, and delivery URL.

Start with these exact 24 bytes: ASCII `shorebird-selection-v2` followed by two
bytes `00 01` (domain separator and encoding revision). Then append values in
this exact order; field names themselves are not encoded:

```text
epoch
scope.app_id
scope.release_version
scope.platform
scope.arch
scope.client_id_hash
scope.base_artifact_hash
scope.host_abi_hash
scope.runtime_profile
channel
variant_id
license_type
entitlement_revision
target.kind
target.patch_number
target.artifact_hash
target.encryption
target.artifact_key_id
not_before
offline_valid_until
fallback.kind
fallback.variant_id
fallback.license_type
```

For each value, encode null as one byte `00`. Encode a string as byte `01`, then
its UTF-8 byte length as an unsigned **four-byte big-endian** integer, then the
exact UTF-8 bytes. Decimal strings use the same string encoding. Null therefore
differs from an empty string even though the field validators reject empty
strings. Validate the descriptor before computing or trusting its digest.
`canonical_hex` and `canonical_base64` in each golden descriptor specify the
complete expected byte sequence, including the prefix. No trailing newline or
additional field is encoded.

## Device request, response, and signatures

Keep `POST /api/v1/patches/check`, with `selection_protocol: 2` in a v2 request.
Retain existing app/release/platform/arch/client/channel/nonce fields and add
`base_artifact_hash`, `host_abi_hash`, `runtime_profile`,
`accepted_selection_epoch`, and `accepted_selection_digest`. The final two are
`"0"` and null together on a fresh scope, otherwise a positive epoch and digest.
Canonicalize an omitted channel to `stable` before constructing the request;
the signed v2 request context always has a nonempty channel. A UUIDv4 nonce is
fresh for each check. Current patch number remains optional telemetry.

The successful outer response has exactly:

```text
version: 2                         # JSON integer, not string
selection: Descriptor
selection_digest: SHA256
delivery: Delivery | null
authentication: {payload: BASE64, signature: BASE64}
```

Decode `authentication.payload` using standard padded base64. The decoded bytes
are an exact UTF-8 JSON object with these fields:

```text
version: 2
request:
  app_id, release_version, platform, arch, channel, client_id_hash, nonce,
  base_artifact_hash, host_abi_hash, runtime_profile,
  accepted_selection_epoch, accepted_selection_digest
selection: Descriptor
selection_digest: SHA256
delivery: Delivery | null
```

Sign those original JSON bytes with **RSA PKCS#1 v1.5 / SHA-256**, matching current
`response_signing.go` and `response_auth.rs`. Do not sign the descriptor digest
alone and do not reserialize before signature verification. Use the release's
pinned RSA public key. Public-key encoding remains standard-base64 PKCS#1 DER,
and signature encoding remains standard padded base64. The existing accepted
RSA key-size range remains unchanged. Production signing uses managed release
keys; the golden file reuses the existing committed test-only RSA fixture.

JSON serialization of the signed payload need not be canonical: its exact bytes
are transmitted. After signature verification, strict parsing rejects duplicate
and unknown keys, then compares the parsed request to the actual outstanding
request and the parsed selection/digest/delivery to the outer response. JSON
member order is immaterial to comparison. Never log signed payloads: delivery
URLs may carry bearer credentials. Cap decoded payload and outer JSON at 64 KiB
each and URL bytes at 8192; retain existing encryption metadata limits.

For a patch, `Delivery` contains exactly `number` (decimal string), `hash`,
`download_url`, `hash_signature` (non-null), and `encryption` (object or null).
Number and hash must equal the stable target. `hash_signature` retains the
existing RSA signature over the lowercase ASCII artifact-hash string. Validate
encryption metadata using the existing native delivery contract and verify its
install/app/platform/arch binding. For base, delivery is null. Refreshed URLs or
transport key material do not change the stable grant; they remain fully signed
and must deliver the identical target content. Key providers must receive both
verified selection context and artifact/transport key context before key release.

Every successful v2 check carries an explicit selection, including when already
running that selection. There is no unsigned no-update shortcut, patch-number
comparison, or inference that a missing patch means base. Missing/invalid scope,
unavailable signing, and inconsistent selection return an error; the client
retains only its previously accepted authorization under its existing lease.

## Server mutation APIs and transactions

Implement these under the existing authenticated API group, with
`requireAppAccess(..., true)` and normal app/release ownership validation. A
backend must use its own authenticated authorization before requesting a paid
assignment; no user or device ID is itself authorization.

### Register and publish the immutable release scope

The existing create endpoint currently accepts version and Flutter metadata.
Release artifact upload separately records an uploaded file's hash, size and
per-platform signing policy. Those files may be APK/IPA/desktop archives or
supplements: their `hash` is **not** the original AOT/bytecode baseline hash.
The existing release PATCH updates platform status and opaque metadata; neither
path currently registers the v2 runtime identity. Add the following explicit
write path rather than interpreting arbitrary platform metadata as authority.

1. Extend `POST /api/v1/apps/{appId}/releases` with optional JSON integer
   `selection_protocol`, accepting only `1` or `2`; omission means `1`. Return
   it in `Release` on create and every release read. Migration assigns `1` to
   every existing release, without inferring v2 from patches or signing mode.
   Create the release and its protocol reservation atomically. Duplicate
   app/version creation returns 409, never an upsert or mode change. Neither
   release PATCH nor its `metadata` may override protocol or registered scope;
   reject those identity fields with 400 `immutable_release_identity`.
2. Use the existing release-artifact upload API to create and upload the actual
   primary package/archive and, when applicable, its baseline supplement.
   Every v2 artifact must declare the existing managed signing policy; preserve
   the existing per-platform key-consistency check. Nil, unsigned, and customer
   signing modes cannot bootstrap a v2 release in this slice. Artifact IDs are
   available in the upload response or subsequent artifact listing. Keep the
   uploaded-file hash and runtime baseline hash separate throughout the client.
3. Add `PUT /api/v1/apps/{appId}/releases/{releaseId}/selection-scopes/{platform}/{arch}`
   with this exact JSON body (all keys required, including nullable ones):

   ```json
   {
     "base_artifact_hash": "<64 lowercase hex measured from the runtime baseline>",
     "host_abi_hash": null,
     "runtime_profile": "interpreter-structural",
     "release_artifact_id": 101,
     "supplement_artifact_id": 102
   }
   ```

   IDs are positive JSON integers using the existing database-ID representation.
   `release_artifact_id` identifies the primary native release package/archive.
   `supplement_artifact_id` identifies the exact original baseline supplement;
   it is required for interpreter profiles and may be null for native AOT.
   The profile and hash rules are those in the scope table above. Both referenced
   artifacts must belong to this app/release/platform and have the identical
   managed signing policy. Do **not** require their artifact `arch` columns to
   equal the URL's runtime architecture: a universal package or an
   `ios_supplement`/`macos_supplement` artifact uses a different routing label.
   Instead, the trusted publisher attests that the referenced files contain the
   stated runtime architecture and baseline. One archive can back multiple scope
   rows with separately measured architecture-specific baseline hashes.

   Registration is allowed only for a protocol-2 release and a never-published
   platform whose current status is absent or `draft`. In one transaction, lock
   the parent release/platform, validate ownership/signing/reference constraints,
   and insert a unique `(release_id, platform, arch)` row carrying the body and
   the referenced artifacts' recorded hashes. Create explicit draft platform
   status if absent. The row is write-once: an exactly identical retry returns
   200 and the existing record, including after publication; a changed body is
   409 `selection_scope_immutable`, even before publication. A first insert is
   201. A legacy release returns 409 `selection_protocol_mismatch`. This slice
   deliberately requires a new release version to correct a registered binding.
   There is no scope-delete/rebind endpoint.

   Return `{scope, release_artifact_id, supplement_artifact_id, published}`;
   `scope` has exactly the immutable scope-table fields, with app/version from
   the release and platform/arch from the path, before adding any client hash.
   Add authenticated read-only
   `GET /api/v1/apps/{appId}/releases/{releaseId}/selection-scopes`, returning
   `{scopes: [...]}` with those same records, sorted by platform then arch.
4. Extend the existing `PATCH /api/v1/apps/{appId}/releases/{releaseId}` for v2
   publication. Alongside `platform`, `status: active`, and optional existing
   `metadata`, require `selection_arches`: a nonempty array of unique runtime
   architecture strings equal to the complete registered set for that platform.
   The publisher sends this only after completing uploads and verifying that
   the signed native package's compiled identity and referenced supplement agree
   with every registered scope. This authenticated active-status request attests
   completion; the server does not claim to inspect or execute the native build.
   In one transaction, validate scope existence, immutable artifact references
   and signing consistency, freeze the scope set, and set platform status active.
   Persist a `selection_published` boolean independently of mutable status.
   No variant assignment or device selection is issued until its platform is
   active and its exact scope is published. Missing/incomplete bindings return
   409 `selection_scope_incomplete`; absent `selection_arches` is 400. A repeated
   identical publication is idempotent. Old clients therefore cannot publish a
   partially configured v2 release by issuing the former status-only PATCH.

After first publication, no new architecture scope or release artifact may be
added to that platform, and registered artifact metadata/signing/IDs cannot be
replaced or deleted. Changing status to inactive cannot reopen registration;
active-to-draft is rejected for v2. Reactivation requires the same frozen arch
set and bindings. Other never-published platforms in the same release may still
complete their own registration/publication. V1 retains its existing artifact
and status flow; the additional scope and publication fields apply only to v2.
An inactive v2 platform returns a check error, retaining the client's existing
offline policy rather than implicitly changing its selection or protocol.

The publisher computes the runtime baseline hash from the exact original AOT
library or interpreter baseline bytes and supplies the full-profile host stamp
from the existing measured host-ABI pipeline. Registration is a trusted publisher
attestation, like the existing native archive comparison, not server-side proof
that a submitted archive contains those bytes. Referencing artifact IDs records
provenance without deriving runtime identity from outer archive hashes. The CLI
must verify both hashes independently when retrieving the release; the updater/
engine must independently measure or validate its installed immutable baseline
and full-profile host binding before accepting a matching selection. Echoing
the registered/requested hash into compiled YAML or a device request alone does
not establish that binding. Mismatches fail closed before patch execution.

Artifact storage remains a separate boundary: registered file hashes cannot
change, and later consumers verify downloaded archive/supplement bytes against
them. V2 upload allocation must reject a storage-key collision with a registered
artifact instead of issuing a new overwrite grant. An old upload grant or an
external storage overwrite cannot authorize changed bytes: a hash mismatch must
fail consumer retrieval, and the publisher must not attest successful publication
after detecting one. Server publication rejects changed recorded hashes or
references; it is not required to download, hash, or extract stored archives in
this slice. A mismatch never causes the scope to be updated. Publisher attestation
does not prove storage availability or establish that the server inspected the
outer file bytes or understood runtime members of an archive.

Reserve `(app_id, version)` permanently when creating a release, and seed that
reservation for every release present during migration. Deleting a release must
retain the reservation and cannot permit recreating the same namespace with a
different mode, native host, or reset epochs. Use a retained reservation/tombstone
or equivalent soft deletion; both stores must enforce it atomically. Correcting
or replacing an installed release requires a new version. Pre-migration deleted
releases cannot be reconstructed from absent records: deployment must choose a
fresh never-distributed version for its first v2 release. No status toggle,
delete/recreate sequence, observed client capability, or public check can promote
an installed legacy release to v2.

### Bounded v2 interpreter patch publication

For this v2 interpreter publication slice, the uploaded patch transport must be
the existing production encoder's `patch::make_patch(empty_base,
complete_encrypted_envelope)` representation: exactly one zstd frame containing
bipatch version 1 with header bytes `df b1 00 00 00 10 00 00` and exactly one
literal control. After the header, encode unsigned canonical base-128 varint
`add_len = 0`, unsigned canonical varint `copy_len = envelope byte length`, the
complete encrypted-envelope bytes, and unsigned canonical varint `seek = 0`.
The literal length must be positive. There are no other controls or decoded
trailing bytes; overlong or overflowing varints are invalid. The transport
therefore never reads or seeks an arbitrary release baseline.

Compressed input and reconstructed envelope are each limited to 128 MiB
(`134217728` bytes). Decoded bipatch bytes, the declared decoder window, and any
declared frame content size are limited to 128 MiB plus 32 bytes (`134217760`).
Reject dictionaries, skippable or concatenated frames, reserved block types,
truncation, and any compressed trailing bytes. Check the exact single-frame
boundary as well as applying bounded decompression; a successful decoder result
alone does not establish that the entire upload had the permitted form.

Before accepting an interpreter variant mapping, the server reads this bounded
transport from storage, reconstructs the encrypted envelope, verifies its
SHA-256 against the signed target `artifact_hash`, and validates the envelope's
declared app/base/variant/license/key policy against the mapping. The compressed
object hash is not the installed artifact hash. This validation does not decrypt
the envelope or prove VM compatibility; the engine still authenticates its
contents and enforces runtime, baseline, key and expiry constraints. The
original-release archive publication attestation above remains separate.

This is specifically a v2 interpreter patch-publication requirement, not a
restriction on legacy patches or the existing native encrypted transport. A
different interpreter diff representation requires a separately specified,
bounded reconstruction contract before acceptance. Acceptance tests must include
a fixture generated by the pinned production encoder and, where that executable
is available, an actual encoder round trip, plus rejection of trailing,
concatenated, skippable and dictionary frames and invalid literal controls.

### Map variants and assign installations

1. `PUT /api/v1/apps/{appId}/releases/{releaseId}/variants/{variantId}`
   takes `platform`, `arch`, `channel`, `license_type`, and `target` in the shape
   above. It registers a variant target after validating the immutable release
   registered binding, existing artifact/signature/encryption policy, and non-revoked
   content. The mapping key is release/platform/arch/channel/variant. Updating
   the mapping affects subsequent assignments, not existing grants. This first
   slice uses explicit backend assignments for content updates as well as tier
   changes; channel promotion alone does not silently change a v2 assignment.
2. `POST /api/v1/apps/{appId}/releases/{releaseId}/selections` takes `client_id`,
   `platform`, `arch`, `channel`, `variant_id`, `entitlement_revision`,
   `not_before`, `offline_valid_until`, `expected_epoch`, and UUIDv4
   `idempotency_key`. `expected_epoch` is a decimal string, initially `"0"`.
   Resolve the mapping and published immutable release scope on the server. Never accept
   an epoch, arbitrary artifact hash, or encryption downgrade from this request.
   Return `{selection, selection_digest}` after durable commit.

The selection-row key is release/platform/arch/client-ID hash, **not** channel
or variant. Within one database transaction: validate/reuse idempotency result,
check expected epoch, resolve the target and license policy, allocate old epoch
+ 1, build descriptor/digest, and persist the row plus idempotency result. Exact
idempotency retries return their original result without another increment;
reuse with a different normalized body is HTTP 409. Concurrent CAS failures are
409. Other successful mutations increment even when target content is unchanged.
Never derive an epoch from a content number, wall clock, or untrusted request.
SQLite and PostgreSQL must have equivalent transactional behavior and unique
constraints. Read-only roles cannot mutate mappings or selections.

A first public check may atomically initialize epoch 1 to perpetual free base
only; it must never infer a paid target. The default backend entitlement revision
for that row is `initial-free`. If a row already exists its channel must match
the request; a channel change requires an authenticated assignment. Request
epochs greater than server state indicate restore/inconsistency and return 409
`selection_state_behind`; never reset the client or trust its claimed number to
issue an arbitrary higher grant. Reconciliation must restore authoritative
server state or use a new native release scope.

Existing administrative content revocation remains effective. The chosen v2
implementation uses **eager atomic invalidation**, not a transient rollback flag
that is only noticed when a device happens to check. On a false-to-true rollback,
one transaction locks the parent release using the same serialization boundary
as assignment/check decisions, marks the content revoked, and replaces every
current selection targeting that release/patch number, across all variants,
platforms and architectures, with a free-base descriptor at its own old epoch + 1.
Each replacement uses `entitlement_revision: server-free-<epoch>`, the rollback's
captured current Unix second for `not_before`, and null `offline_valid_until`.
Persist each descriptor and its recomputed stable digest before commit. A repeat
rollback is idempotent and allocates no further epochs. If any affected epoch is
exhausted, fail and roll back the entire mutation, including the revoked flag;
never report a partial successful revocation. PostgreSQL and SQLite must provide
equivalent behavior, including with separate store instances/processes.

Consequently rollback followed immediately by rollforward, without an intervening
device check, still leaves those installations assigned to newer free-base grants.
Rollforward changes only content eligibility; it does not restore previous grant
rows. A future explicit authenticated assignment may select that content again at
another greater epoch. No wire revocation-generation field is needed for this
eager strategy. Existing rollback endpoints, including the legacy administrative
alias, must route through this same atomic store operation for v2 releases.

As a defensive resolution check, before answering a device check, if its selected
patch is revoked, its content cap has expired, or its grant has expired, atomically issue
a newer free-base descriptor with `entitlement_revision: server-free-<epoch>`,
where `<epoch>` is the newly allocated canonical decimal epoch, `not_before` is
the server's current Unix second, and `offline_valid_until` is null.
Use the exact same epoch/CAS rules. Free base itself has no expiry. A revoked
artifact cannot be newly assigned even if cached. Revocation updates take effect
on disconnected clients only when they reconnect or their lease ends.

A same-epoch check is only a refresh of the identical effective grant; it cannot
override revocation or carry a different fallback/target. Resolve revocation and
expiry inside the selection transaction **before** deciding to reuse an epoch.
Concurrent rollback and check/assignment must be serialized against the same
authoritative revocation state. Once a free-base replacement at epoch E+1 has
committed, no check whose authorization transaction linearizes afterward may
reissue the paid selection at E, including a check carrying stale accepted-epoch
telemetry. A decision authorized before the revocation can still be signed or
delivered afterward; it is an in-flight old decision, not a new refresh. Local
high-water enforcement rejects it if E+1 was accepted, and an unexpired grant
otherwise has the stated offline limitation. Do not claim wire-delivery ordering
across concurrent requests. Administrative rollforward only makes content eligible for
a future explicit assignment; it does not undo the base replacement or restore
an old selection at the same epoch. Mutation idempotency retries may return their
historical result for operation accounting, but must not issue device-response
authentication/delivery or overwrite the current selection from that result.

## Client decisions and crash-safe persistence

Apply decisions in this order: size/schema checks; full response signature;
request and immutable scope/installation equality; outer-response equality;
descriptor constraints/digest; delivery/signature/encryption consistency;
epoch/digest comparison. Invalid input changes no authorization or pointers.
For an otherwise valid candidate:

| Condition | Result |
| --- | --- |
| Candidate epoch below accepted epoch | `reject_replay`, no mutation. |
| Same epoch, different stable digest | `reject_epoch_conflict`, no mutation. |
| Same epoch, same digest | `refresh`, preserving epoch; refresh delivery/retry activation if permitted by time/key/content state. |
| Greater epoch | `accept`, journal the new authorization even when it selects a lower content number or base. |

Persist accepted epoch, digest, and verified signed descriptor/response in a
release selection journal outside `patches/{number}/`. It must survive artifact
cleanup, download/install errors, key failures, fallback, and server rollback.
Store the signed payload needed for boot re-verification, not only unsigned
parsed fields. Journal replacement must be atomic and durably precede exposure
of a bootable target; recover pointers from this authority after interrupted
writes. Do not span key-provider callbacks with updater/config/selection locks.

Time is evaluated **after** accepting a valid newer authorization. An already
expired or not-yet-valid grant therefore advances the high-water mark and chooses
free base, preventing an old paid grant from resurfacing. `not_before` is only an
activation constraint, not a request to keep an old grant until that time. Same
epoch refresh after expiry remains base; extending a lease needs a new epoch.
The golden vectors expose `authorization_action`, `activation`, `reason`, and
post-decision epoch/digest separately for this reason.

Golden action values are `accept`, `refresh`, or `reject`; activation values are
`selected_patch`, `base`, or `unchanged`. Rejection reasons include `replay`,
`epoch_conflict`, `context_mismatch`, `response_mismatch`, and `invalid_signature`.
Successful authorization can have activation reasons `ok`, `selected_base`,
`expired`, or `not_yet_valid`. `unchanged` means this rejected check changes no
authorization or pointers; independent cold-boot lease checks still apply.

Activation chooses only the accepted target while usable, or bundled free base.
Downloads can be staged; missing content, unavailable/wrong key, expired grants,
and rejected/crashed content use base on a cold launch. They do not restore an
older paid authorization or lower the epoch. Cached installed content must be
revalidated and selectable without downloading again. Content proven bad remains
quarantined; a selection epoch does not erase a content failure. Retryable key
failures preserve authenticated content and can recover within the same valid
grant. Preserve the distinction between running process content and next boot.

At boot verify the stored signature, full immutable/install context, journal
epoch/digest, target hash, artifact signature, lease, and key context before
execution. A stale per-patch authentication file cannot authorize selection.
Missing/corrupt v2 journal with cached patches means base, not reconstruction of
authorization from a patch directory or v1 pointer. An online check may recover
authoritative state; this fail-closed recovery does not claim protection against
complete rollback/reset of all local state.

Replace number-based cache pruning with bounded reference-based retention.
Retain pending/active content as required for the current process and download;
other content can be evicted and redownloaded after a future online assignment.
Do not erase the selection journal or application data to reclaim patch storage.
The existing bounded encrypted-transport and native-image limits still apply.

## Lease, compatibility, and data guarantees

Offline expiry needs a trustworthy time assumption. Device wall-clock checks
alone cannot defeat clock rollback. Preserve an accepted trusted-time floor
where supported, but do not claim it survives complete storage rollback or
guarantees elapsed time without an appropriate platform/online clock. A deployment
requiring that guarantee must supply a protected time policy or require online
authorization. Perpetual grants have no offline revocation guarantee.

The first v2-capable native release embeds these rules. Existing installed v1
updaters cannot be upgraded into v2 by ordinary Dart patches. Legacy releases
retain v1 explicitly; v2 releases reject v1/unsigned responses. A transport or
server error never changes release protocol mode. A new native release creates
a new epoch scope; changes to channel, variant, account entitlement, or patch
number do not. Signing-key rotation is outside this slice and must not silently
change the embedded trust root.

The acceptance fixture uses persistent user-data sentinels and compatible
additive schemas through repeated free/pro/enterprise switches. No reinstall,
storage wipe, updater-state injection, or identical-content republishing may
stand in for a switch. Arbitrary destructive application data migrations cannot
be reversed by this protocol. Native plugin binaries, engine code, entitlements,
and native-asset mappings remain part of the frozen installed host.

## Implementation boundaries and acceptance

Server changes belong in `internal/models/models.go`, `handlers/releases.go`,
handlers for device checks, response signing, scope registration, variant mapping
and assignment, `db/store.go`, both stores and migrations. Extend release create/
read types with protocol; add immutable scope rows, retained namespace reservations
and platform publication state. Add store transactions for scope registration,
guarded platform publication, and variant/selection mutation; publish only
committed results. CLI/protocol client changes add mapping/assignment operations
and original-release validation independently from build-time flavor selection,
including propagation of authenticated artifact expiry into upload metadata.

Updater changes belong in `network.rs`, `response_auth.rs`, a selection journal,
`updater.rs`, `cache/lifecycle.rs`, `cache/updater_state.rs`, and `native_boot.rs`.
Engine-facing C APIs expose copied verified selection context alongside the
existing immutable boot selection. Keep stable patch-number APIs, adding variant,
epoch and next-boot status. Engine interpreter validation replaces the single-key
legacy policy only for v2 with the verified selected artifact key; native key
callbacks receive the verified selection context without trusting caller strings.
VM artifact metadata verification remains mandatory.

Server and updater work can proceed independently against the frozen vectors.
The engine/CLI producers must also populate and validate the compiled release
binding before claiming any native v2 platform support. Platform ARM64 builds
and actual app execution remain separate gates from this wire contract.

Consumers must verify every vector's canonical bytes/digest, exact RSA signature,
strict context comparison, epoch action, lease action, and post-decision state.
The fixture uses synthetic artifact hashes and asserts selection decisions only;
it does not prove download, decryption, executable compatibility, or app startup.
Additional implementation checks must cover malformed/duplicate/unknown fields,
integer limits, invalid UTF-8, schema violations, altered signed delivery,
cross-install/base/platform replay, concurrent server CAS/idempotency, transaction
restart, journal crash points, cached reselection, content eviction, revocation,
and retryable versus quarantined failures.

The end-to-end sequence must reuse original content: free base -> pro patch 2 ->
enterprise patch 3 -> pro patch 2 -> free base -> enterprise patch 3 -> free base,
with increasing epochs, unchanged installed host, persistent data checks and
executed method replacement/addition. Repeat cold offline continuation and
missing/wrong-key recovery. Every platform/architecture needs its own runtime
evidence; this contract and host vector verification do not establish it.

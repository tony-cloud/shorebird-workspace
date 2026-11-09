# Release keys: fast provisioning and distribution

One command creates every Open Shorebird operator key, signs the P4 release
root of trust, writes the public pins into the workspace and sets the GitHub
variables and secrets in the required order. A second one-shot script installs
shorebird-server on a Linux host and verifies the signed release against the
pinned update key. The contracts behind them are in
[`HARDENING_CONTRACTS.md`](HARDENING_CONTRACTS.md) §8.1 and §10 and
[`shorebird-server/docs/SELF_UPDATE.md`](../shorebird-server/docs/SELF_UPDATE.md).

## Quickstart

On an offline or otherwise trusted machine with this workspace checked out:

```sh
# 1. A private directory OUTSIDE every git work tree, mode 0700 (an encrypted
#    volume or removable disk; keep an offline backup of it).
mkdir -m 700 /Volumes/OperatorKeys/open-shorebird

# 2. Create every missing key and the signed trust; show what would change.
scripts/provision_release_keys.sh init --production --private-dir /Volumes/OperatorKeys/open-shorebird

# 3. Write the public pins into the workspace, then review and commit them.
scripts/provision_release_keys.sh sync --private-dir /Volumes/OperatorKeys/open-shorebird --write-repo-pins

# 4. Set the GitHub variables and secrets (dry run without --apply-github).
scripts/provision_release_keys.sh sync --private-dir /Volumes/OperatorKeys/open-shorebird --apply-github \
    --server-release-token-file /Volumes/OperatorKeys/server-release-token   # optional, mode 0600

# 5. Check that the private directory, the workspace and GitHub agree.
scripts/provision_release_keys.sh verify --private-dir /Volumes/OperatorKeys/open-shorebird
```

Every command is idempotent: rerunning it creates only what is missing and
changes nothing else. Steps 2–4 can be one command
(`init --production ... --write-repo-pins --apply-github`).

Then install a server (on the Linux host, after a signed release is published,
see [Server releases](#server-releases)):

```sh
curl -fsSL https://raw.githubusercontent.com/tony-cloud/shorebird-server/main/deploy/linux/install.sh | sudo bash -s -- \
    --base-url https://patch.example.com
```

## What `init` does, in order

1. **Checks the private directory.** It must not be a symlink, must be outside
   every git work tree and the workspace, owned by you, and mode 0700 (key
   files 0600). A new directory must be empty and is created as either
   `--production` or `--development`; the mode is recorded and can never
   change.
2. **Creates every missing key** (PKCS#8 Ed25519, `keys/<role>-<n>.pem`, created
   exclusively so an existing file is never replaced):
   `root-1..n` (`--root-keys`, `--threshold`; default 1-of-1),
   `build-manifest-1`, `artifact-catalog-1` and `server-update-1`. A key that
   is already recorded must still be present and unchanged; a missing one is
   refused ("restore it from your backup"), because a pinned or signed key
   cannot be recreated.
3. **Signs root version 1 and delegations version 1** with
   `scripts/release_trust.py`'s functions (role separation, thresholds), and
   assembles `trust/release-trust.json`. Signed documents are never re-signed;
   if the private keys no longer match them, it stops and points to `rotate`.
4. **Verifies the bundle** against the compiled pin (expiry and windows
   included; `--production` refuses any DEVELOPMENT-ONLY root).
5. **Repository pins** (only with `--write-repo-pins`, otherwise it lists the
   files it would write):
   - `release-pins/release-pins.json`: the GN args, the
     `toolchain_security.json` v2 fields (`release_root`,
     `min_delegations_version`) for toolchain packaging, digests and key IDs;
   - `release-pins/release-root.gn`: `shorebird_release_root` and
     `shorebird_release_min_delegations_version`, ready for `args.gn` or
     `--gn-args`;
   - `release-pins/release-trust.json`: the public bundle for build machines;
   - `flutter/bin/internal/shorebird_artifact_trust.json`: the catalog key with
     usage `production` (a revoked catalog key is removed). This file is in the
     flutter submodule: commit it there and bump the submodule;
   - `shorebird-server/signing/server-update.trusted-keys`, and
     `shorebird-server/deploy/linux/install.sh` rebuilt so the installer pins
     the update key (production only).
6. **GitHub** (`--repo`, default `tony-cloud/shorebird-workspace`; a dry run
   unless `--apply-github`). In this order, each step only after the steps it
   depends on read back correctly:

   | # | Kind | Name | Value |
   | --- | --- | --- | --- |
   | 1 | variable | `OPEN_SHOREBIRD_RELEASE_TRUST` | the bundle JSON |
   | 2 | variable | `OPEN_SHOREBIRD_RELEASE_ROOT` | the root pin |
   | 3 | secret | `OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY` | catalog key PEM (after 1 and 2, §10.10) |
   | 4 | variable | `SHOREBIRD_SERVER_UPDATE_PUBLIC_KEYS` | pinned server update keys |
   | 5 | secret | `SHOREBIRD_SERVER_UPDATE_SIGNING_KEY` | server update key PEM (after 4) |
   | 6 | secret | `SHOREBIRD_SERVER_RELEASE_TOKEN` | only with `--server-release-token-file` |

   Values go to `gh variable set` / `gh secret set` on stdin, never in argv,
   and are never printed (variables are shown as a digest). Unchanged values
   are skipped. A value on GitHub that this private directory did not set is
   refused unless `--force-github`; a refused or failed step skips everything
   that depends on it. Which key each secret holds is recorded (key IDs only)
   so a rotation updates exactly the changed secret.
7. Prints the pin summary and the build-machine command (`env`).

`status` runs the same checks as `verify` and always exits 0; `verify` exits 1
on any disagreement between the private directory, the repository pins and
GitHub (variables by value, secrets by existence and recorded key ID).
`--skip-repo` and `--skip-github` narrow the check.

## Build machines

```sh
eval "$(scripts/provision_release_keys.sh env --private-dir /Volumes/OperatorKeys/open-shorebird)"
```

prints `SHOREBIRD_RELEASE_TRUST` and either
`SHOREBIRD_BUILD_MANIFEST_SIGNING_KEY` or, for a KMS/HSM signer,
`SHOREBIRD_BUILD_MANIFEST_SIGNER` plus `SHOREBIRD_BUILD_MANIFEST_SIGNER_KEY_ID`
(§10.5). Paths only, no key material.

## Hardened toolchain engines (P6)

```sh
scripts/hardened_toolchain.py --profile hardened --production \
  --release-pins release-pins/release-pins.json describe
```

reads the root pin and delegations floor written by `sync --write-repo-pins`
(or `OPEN_SHOREBIRD_RELEASE_ROOT`). It prints the engine GN args, the
toolchain ID and the `toolchain_security.json` the CLI reads. Use `gn-args`,
`toolchain-security --output <shorebird>/bin/internal/toolchain_security.json`
and `verify-args <out>/args.gn` for each step. For `hardened-diversified`,
add `--diversification-profile PRIVATE/diversification-profile.json`
(`HARDENING_CONTRACTS.md` §11.11). Then publish the catalog with
`OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE=hardened-diversified` and
`OPEN_SHOREBIRD_DIVERSIFICATION_PROFILE_ID`. `--production` refuses every
DEVELOPMENT-ONLY input. A Windows release that declares its Authenticode
signer sets `SHOREBIRD_WINDOWS_SIGNING_CERT_SHA256` on the build machine
(§12.4). The lifecycle runbook for devices is in §12.9.

## KMS/HSM signers

`init --production --external build-manifest=PUB.pem` (also `artifact-catalog`
or `server-update`; repeatable) records only the public key of a role whose
private key lives in a KMS or HSM. Nothing private is generated for it, and
its GitHub secret is reported as "not managed": configure CI to reach the
signer instead. Root keys are always local PEM files, kept offline.

## Rotation and revocation

| Command | Effect |
| --- | --- |
| `rotate build-manifest` / `rotate artifact-catalog` | New delegate key, delegations version N+1 (old key dropped and moved to `retired/`). |
| `... --revoke` | Also revokes the old key ID; revocations are carried into every later version. |
| `... --raise-floor` | Raises `shorebird_release_min_delegations_version` to N+1 (compromise: devices refuse older delegations once a release built with it ships, §10.3). |
| `rotate root [--root-keys n --threshold k]` | Root version N+1 signed by both thresholds, delegations re-signed under it; installed engines follow the chain. `--repin` pins the new version in new engines. |
| `rotate server-update --stage add` → `activate` → `retire` | Adds a second pinned update key (publish a release signed by the old key), then signs with the new key, then drops the old one (SELF_UPDATE.md, "Rotation"). |
| `renew` | Delegations version N+1 with the same keys and a later expiry. |

Each takes `--write-repo-pins` and `--apply-github` like `init` and updates
only what changed.

## Development

`init --development` builds the same layout from the committed, public
DEVELOPMENT-ONLY keys (`scripts/testdata/...`,
`shorebird-server/signing/server-update.DEVELOPMENT-ONLY.pem`), so every
verifier recognizes the result as development. It never pins a server
installer key, never writes over production pins and never syncs to
`tony-cloud/shorebird-workspace` (pass `--repo` for a fork). `--production`
refuses a development directory and every committed DEVELOPMENT-ONLY key, and
a development directory refuses production use.

## Server releases

With the two server variables/secrets set, dispatch the CI workflow with
`publish_server_release: true` (SELF_UPDATE.md, "CI"). The installer
(`shorebird-server/deploy/linux/README.md`) then downloads the newest signed
release, verifies it against the key pinned in step 5, installs it into
`/var/lib/shorebird-server/bin` with a root-owned
`/usr/local/bin/shorebird-server` symlink and the systemd unit, and creates
missing runtime secrets. Later upgrades use the server's signed self-update.

## Tests

`scripts/test_provision_release_keys.py` (fake `gh` on PATH, temporary private
directories) and `shorebird-server/deploy/linux/test_install.py` (staged
install in a temporary prefix) run in the CI source checks.

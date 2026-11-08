#!/usr/bin/env bash
# Writes, signs and verifies the artifact catalog of an assembled mirror
# (HARDENING_CONTRACTS.md section 8, "Artifact catalog signer"):
#
#   <mirror>/shorebird/<engine>/artifact-catalog.v1.json         canonical catalog
#   <mirror>/shorebird/<engine>/artifact-catalog.v1.json.sig     detached Ed25519 signature
#   ...and a .sha256 sidecar for each, as for every mirror file.
#
# Usage: publish_artifact_catalog.sh MIRROR_ROOT ENGINE_REVISION CUSTOM_BASE_URL
#
# Signing key: the PKCS#8 Ed25519 PEM in OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY
# (a CI secret) signs a production catalog; its public key must already be
# pinned with usage "production" in the trust store
# (OPEN_SHOREBIRD_ARTIFACT_CATALOG_TRUST, default
# flutter/bin/internal/shorebird_artifact_trust.json), otherwise verification
# fails here rather than in a client. Without the secret the catalog is signed
# with the repository's DEVELOPMENT-ONLY key and a loud warning: clients accept
# it only in the legacy profile, never for a hardened release.
#
# Catalog profile: a production-signed catalog is generated for the hardened
# profile (OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE=legacy opts out); a
# development-signed one is always legacy, and asking for hardened without the
# production key is an error.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -ne 3 ]]; then
  echo "usage: $0 MIRROR_ROOT ENGINE_REVISION CUSTOM_BASE_URL" >&2
  exit 64
fi
mirror_root="$1"
engine_revision="$2"
custom_base_url="$3"
trust="${OPEN_SHOREBIRD_ARTIFACT_CATALOG_TRUST:-$ROOT/flutter/bin/internal/shorebird_artifact_trust.json}"
catalog="$mirror_root/shorebird/$engine_revision/artifact-catalog.v1.json"
if [[ -n "${OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY:-}" ]]; then
  profile="${OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE:-hardened}"
else
  profile="${OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE:-legacy}"
  if [[ "$profile" == hardened ]]; then
    echo "error: a hardened artifact catalog needs the production key in OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY" >&2
    exit 1
  fi
fi
case "$profile" in
  legacy | hardened) ;;
  *) echo "error: OPEN_SHOREBIRD_ARTIFACT_CATALOG_PROFILE must be legacy or hardened, not $profile" >&2; exit 64 ;;
esac

python3 "$ROOT/scripts/artifact_catalog.py" generate \
  --mirror-root "$mirror_root" \
  --engine-revision "$engine_revision" \
  --custom-base-url "$custom_base_url" \
  --security-profile "$profile" \
  --workspace-root "$ROOT" \
  --output "$catalog"

key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
if [[ -n "${OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY:-}" ]]; then
  (umask 077 && printf '%s\n' "$OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY" > "$key_dir/signing-key.pem")
  python3 "$ROOT/scripts/artifact_catalog.py" sign \
    --catalog "$catalog" --key "$key_dir/signing-key.pem" --backend openssl
  rm -f "$key_dir/signing-key.pem"
  expected_usage=production
else
  message="OPEN_SHOREBIRD_ARTIFACT_CATALOG_SIGNING_KEY is not set: the artifact catalog is signed with the DEVELOPMENT-ONLY key and is not usable for hardened releases."
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::warning title=Development-signed artifact catalog::$message"
  fi
  echo "warning: $message" >&2
  python3 "$ROOT/scripts/artifact_catalog.py" sign \
    --catalog "$catalog" --key "$ROOT/scripts/testdata/artifact-catalog-signing-key.DEVELOPMENT-ONLY.pem"
  expected_usage=development
fi

verify_output="$(python3 "$ROOT/scripts/artifact_catalog.py" verify \
  --catalog "$catalog" --signature "$catalog.sig" --trust "$trust" \
  --profile "$profile" --engine-revision "$engine_revision" 2>&1)" || {
  printf '%s\n' "$verify_output" >&2
  echo "error: the signed artifact catalog does not verify against $trust" >&2
  exit 1
}
printf '%s\n' "$verify_output"
if ! grep -Fq "verified ($expected_usage)" <<<"$verify_output"; then
  echo "error: expected a $expected_usage-trusted signature; is the signing key pinned in $trust with usage \"$expected_usage\"?" >&2
  exit 1
fi

"$ROOT/scripts/write_sha256.sh" "$catalog"
"$ROOT/scripts/write_sha256.sh" "$catalog.sig"

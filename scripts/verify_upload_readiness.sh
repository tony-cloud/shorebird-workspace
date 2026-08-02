#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT/scripts/verify_ci_workflow.sh" \
  --require-upload-ready \
  "$ROOT/.github/workflows/open-shorebird-ci.yml"

echo "upload readiness check passed"

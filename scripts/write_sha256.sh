#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 && "$#" -ne 2 ]]; then
  echo "usage: $0 <artifact> [output]" >&2
  exit 64
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN=python3
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi

"$PYTHON_BIN" "$ROOT/scripts/write_sha256.py" "$@"

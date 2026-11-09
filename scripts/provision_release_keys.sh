#!/usr/bin/env bash
# Thin wrapper: scripts/provision_release_keys.py does the work (see --help).
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/provision_release_keys.py" "$@"

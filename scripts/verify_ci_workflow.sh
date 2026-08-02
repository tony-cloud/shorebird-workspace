#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKFLOW="$ROOT/.github/workflows/open-shorebird-ci.yml"
RUBY_ARGS=()

usage() {
  cat >&2 <<'EOF'
usage: verify_ci_workflow.sh [--require-tracked] [--require-clean] [--require-upload-ready] [workflow.yml]

Validates the Open Shorebird GitHub Actions workflow contract. The upload-ready
mode additionally requires every required CI support file to be tracked in its
own git checkout and every required owning checkout to be clean.
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --require-tracked|--require-clean|--require-upload-ready)
      RUBY_ARGS+=("$1")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "unknown argument: $1" >&2
      usage
      exit 64
      ;;
    *)
      WORKFLOW="$1"
      shift
      if [[ "$#" -gt 0 ]]; then
        echo "unexpected extra argument: $1" >&2
        usage
        exit 64
      fi
      ;;
  esac
done

if [[ ! -f "$WORKFLOW" ]]; then
  echo "missing workflow: $WORKFLOW" >&2
  exit 66
fi

command -v ruby >/dev/null 2>&1 || {
  echo "ruby is required to validate GitHub workflow YAML" >&2
  exit 127
}

if [[ "${#RUBY_ARGS[@]}" -eq 0 ]]; then
  ruby "$ROOT/scripts/verify_ci_workflow.rb" "$WORKFLOW"
else
  ruby "$ROOT/scripts/verify_ci_workflow.rb" "${RUBY_ARGS[@]}" "$WORKFLOW"
fi

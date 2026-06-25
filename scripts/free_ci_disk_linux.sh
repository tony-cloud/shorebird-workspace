#!/usr/bin/env bash
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "Refusing to free disk outside GitHub Actions." >&2
  exit 64
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Skipping Linux disk cleanup on $(uname -s)."
  exit 0
fi

if [[ "${CI_FREE_DISK_SPACE:-1}" == "0" ]]; then
  echo "Skipping disk cleanup because CI_FREE_DISK_SPACE=0."
  df -h
  exit 0
fi

if [[ "${RUNNER_ENVIRONMENT:-github-hosted}" != "github-hosted" &&
      "${CI_FREE_DISK_SPACE_FORCE:-0}" != "1" ]]; then
  echo "Skipping disk cleanup on ${RUNNER_ENVIRONMENT} runner."
  echo "Set CI_FREE_DISK_SPACE_FORCE=1 to opt in on non-hosted runners."
  df -h
  exit 0
fi

echo "Disk before cleanup:"
df -h

# GitHub-hosted Ubuntu images include large toolchains that are unrelated to
# Dart SDK, Flutter engine, and updater builds. Remove only well-known cache
# directories on ephemeral GitHub Actions runners.
for path in \
  /opt/ghc \
  /opt/hostedtoolcache/CodeQL \
  /usr/local/.ghcup \
  /usr/local/lib/android/sdk \
  /usr/local/share/boost \
  /usr/share/dotnet; do
  if [[ -e "$path" ]]; then
    echo "Removing $path"
    sudo rm -rf "$path"
  fi
done

echo "Disk after cleanup:"
df -h

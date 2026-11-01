#!/usr/bin/env python3
"""Restore the canonical ancestor tag used by Flutter's real version detection."""
import argparse
from pathlib import Path
import subprocess

TAG = '3.47.8'
REVISION = '737ba8f885dc774b53b442759999a312c9461e4e'
UPSTREAM = 'https://github.com/flutter/flutter.git'


def restore_version_tag(checkout, *, upstream=UPSTREAM, tag=TAG, revision=REVISION):
    def git(*args):
        return subprocess.run(['git', '-C', str(checkout), *args], check=True,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    ref = 'refs/tags/' + tag
    existing = subprocess.run(['git', '-C', str(checkout), 'show-ref', '--verify', '--quiet', ref])
    if existing.returncode not in (0, 1):
        raise ValueError('Flutter checkout is not a valid Git repository')
    if existing.returncode == 0:
        observed = git('rev-parse', ref + '^{commit}')
    else:
        git('fetch', '--no-tags', upstream, ref)
        observed = git('rev-parse', 'FETCH_HEAD^{commit}')
    if observed != revision:
        raise ValueError('Canonical Flutter version tag has an unexpected revision')
    # A valid version tag for a different Flutter lineage must never label this SDK.
    try:
        git('merge-base', '--is-ancestor', revision, 'HEAD')
    except subprocess.CalledProcessError:
        raise ValueError('Canonical Flutter version tag is not an ancestor of this checkout') from None
    if existing.returncode:
        git('update-ref', ref, revision, '0' * 40)
    return revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    args = parser.parse_args()
    restore_version_tag(args.checkout)
    print('Verified canonical Flutter version tag ' + TAG + ' at ' + REVISION)


if __name__ == '__main__':
    main()

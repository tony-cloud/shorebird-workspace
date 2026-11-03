#!/bin/bash
# usage: with_key.sh KEY_HEX cmd...   Substitutes the disposable key into the
# AppDelegate placeholder, runs cmd, and always restores the committed file.
set -u
F=$(cd "$(dirname "$0")/.." && pwd)/ios/Runner/AppDelegate.swift
KEY=$1; shift
cp "$F" "$F.orig"
sed -i '' "s/__PERF_KEY_HEX__/$KEY/" "$F"
"$@"; rc=$?
mv "$F.orig" "$F"
exit $rc

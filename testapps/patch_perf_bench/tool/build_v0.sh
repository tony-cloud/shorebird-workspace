#!/bin/bash
# V0: stock-style AOT release (TFA + inlining, no patch runtime): built from a
# copy of the app that has no shorebird.yaml asset, so the engine starts no updater.
# usage: build_v0.sh WORKDIR   (needs env.sh-style FLUTTER_ROOT/PATH/PUB_CACHE and ENG)
set -e
SRC=$(cd "$(dirname "$0")/.." && pwd); W=$1
rm -rf "$W/v0src"; mkdir -p "$W/v0src"
rsync -a --exclude build --exclude .dart_tool --exclude shorebird.yaml --exclude tool --exclude pubspec.lock "$SRC/" "$W/v0src/"
python3 - "$W/v0src/pubspec.yaml" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read().replace("  assets:\n    - shorebird.yaml\n",""); open(p,'w').write(s)
PY
cd "$W/v0src"
flutter pub get >/dev/null
flutter build ios --release -v --local-engine-src-path="$ENG" --local-engine=ios_release --local-engine-host=host_release_arm64 --dart-define=BENCH_VARIANT=V0

#!/bin/bash
set -u
cd /Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/perf-bench
. ./cli_env.sh
T=../../testapps/patch_perf_bench/tool
APP=../../testapps/patch_perf_bench
REL=$1
sed -i '' "s/^version: .*/version: $REL/" $APP/pubspec.yaml
$T/build_v0.sh $PWD > build_v0.log 2>&1 || { echo V0_BUILD_FAILED; exit 1; }
rm -rf apps/V0; mkdir -p apps/V0; cp -c -R v0src/build/ios/iphoneos/Runner.app apps/V0/
KEY=$(python3 -c "import json;print(json.load(open('$W/secrets.json'))['aes'])")
cd $APP; rm -rf build/ios
$T/with_key.sh $KEY $CLI --json --local-engine-src-path=$ENG --local-engine=ios_release --local-engine-host=host_release_arm64 release ios --dart-define=BENCH_VARIANT=V1 --export-options-plist=$W/ExportOptions.plist > $W/release_$REL.log 2>&1
grep -o "Published Release [^!]*" $W/release_$REL.log || { echo V1_RELEASE_FAILED; exit 2; }
cd - >/dev/null
rm -rf apps/V1; mkdir apps/V1; (cd apps/V1 && unzip -q ../../../../testapps/patch_perf_bench/build/ios/ipa/patch_perf_bench.ipa)
mkdir -p results/prev; mv results/V*_run*.json results/v2 results/prev/ 2>/dev/null; mkdir -p results/v2
$T/run_variant.sh V0 apps/V0/Runner.app results 3 || exit 3
echo V0_DONE

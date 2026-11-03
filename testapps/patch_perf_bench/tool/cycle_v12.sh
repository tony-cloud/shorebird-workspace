#!/bin/bash
# V1 runs, then publish the one-function body patch (tag 1 -> 2) and V2 runs.
set -u
cd /Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/perf-bench
. ./cli_env.sh
T=../../testapps/patch_perf_bench/tool
REL=$1
$T/run_variant.sh V1 apps/V1/Payload/Runner.app results 3 || exit 1
KEY=$(python3 -c "import json;print(json.load(open('$W/secrets.json'))['aes'])")
cd ../../testapps/patch_perf_bench
sed -i '' 's|return 1; // KERNEL_TAG|return 2; // KERNEL_TAG|' lib/kernel.dart
export SHOREBIRD_AOT_PATCH_KEY_HEX=$KEY SHOREBIRD_AOT_PATCH_KEY_ID=$(python3 -c "import json;print(json.load(open('$W/state.json'))['key_id'])") SHOREBIRD_AOT_PATCH_FLAVOR_ID=free SHOREBIRD_AOT_PATCH_LICENSE_TYPE=free
$T/with_key.sh $KEY $CLI --json --local-engine-src-path=$ENG --local-engine=ios_release --local-engine-host=host_release_arm64 patch ios --release-version=$REL --track=stable --dart-define=BENCH_VARIANT=V1 > $W/patch_v2_$REL.log 2>&1
grep -o "Published Patch [0-9]*" $W/patch_v2_$REL.log || { echo PATCH_FAILED; exit 2; }
sed -i '' 's|return 2; // KERNEL_TAG|return 1; // KERNEL_TAG|' lib/kernel.dart
cd ../../.agents/perf-bench
$T/run_device.sh V2_download results/v2 240
$T/run_variant.sh V2 x results 3 --no-install
echo CYCLE_DONE

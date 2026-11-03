#!/bin/bash
set -u
cd /Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/perf-bench
. ./cli_env.sh
T=../../testapps/patch_perf_bench/tool
REL=$1
KEY=$(python3 -c "import json;print(json.load(open('$W/secrets.json'))['aes'])")
cd ../../testapps/patch_perf_bench
sed -i '' 's|return 1; // KERNEL_TAG|return 2; // KERNEL_TAG|' lib/kernel.dart
export SHOREBIRD_AOT_PATCH_KEY_HEX=$KEY SHOREBIRD_AOT_PATCH_KEY_ID=$(python3 -c "import json;print(json.load(open('$W/state.json'))['key_id'])") SHOREBIRD_AOT_PATCH_FLAVOR_ID=free SHOREBIRD_AOT_PATCH_LICENSE_TYPE=free
$T/with_key.sh $KEY $CLI --json --local-engine-src-path=$ENG --local-engine=ios_release --local-engine-host=host_release_arm64 patch ios --release-version=$REL --track=stable --dart-define=BENCH_VARIANT=V3 > $W/patch_v3p.log 2>&1
rc=$?
sed -i '' 's|return 2; // KERNEL_TAG|return 1; // KERNEL_TAG|' lib/kernel.dart
grep -o "Published Patch [0-9]*" $W/patch_v3p.log || { echo PATCH_FAILED; tail -c 600 $W/patch_v3p.log; exit 2; }
cd ../../.agents/perf-bench
mkdir -p results/v3p
$T/run_device.sh V3p_download results/v3p 300
$T/run_variant.sh V3p x results 3 --no-install
echo CYCLE_DONE

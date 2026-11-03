. /Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/perf-bench/env.sh
W=/Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/perf-bench/work
export CLI=$E/cli/bin/shorebird
export ENG=/Volumes/TonyCL6APFS/git/shorebird-workspace/.agents/builds/engine-ios-native-host-gate-ci-final-20261102
export SHOREBIRD_E2E_CONFIG_DIR=$W/config
mkdir -p $W/config
export SHOREBIRD_HOSTED_URL=$(python3 -c "import json;print(json.load(open('$W/state.json'))['api_url'])")
export AUTH_SERVICE_URL=$SHOREBIRD_HOSTED_URL/auth
export SHOREBIRD_ARTIFACT_BASE_URL=$SHOREBIRD_HOSTED_URL
export SHOREBIRD_TOKEN=sb_api_$(python3 -c "import json;print(json.load(open('$W/secrets.json'))['access_token'])")

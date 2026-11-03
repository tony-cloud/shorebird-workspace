#!/bin/bash
# usage: run_variant.sh LABEL APP_DIR RESULTS_DIR [RUNS] [--no-install]
# Installs APP_DIR (a Runner.app) on the device (unless --no-install) and does
# RUNS cold launches. Key provisioning env is taken from the caller's
# DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_* (first launch only).
set -u
L=$1; APP=$2; OUT=$3; N=${4:-3}
D=${BENCH_DEVICE:-00008132-000C396801D9001C}
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "${5:-}" != "--no-install" ]; then
  xcrun devicectl device install app --device $D "$APP" >"$OUT/install_$L.log" 2>&1 || { echo install failed; exit 1; }
fi
for i in $(seq 1 $N); do
  "$HERE/run_device.sh" ${L}_run$i "$OUT" 240 || exit 1
  unset DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_HEX DEVICECTL_CHILD_OPEN_SHOREBIRD_TEST_KEY_ID
done

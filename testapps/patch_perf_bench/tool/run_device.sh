#!/bin/bash
# usage: run_device.sh LABEL OUTDIR [timeout_s]
# Cold-launches the installed bench app (terminating any running copy), waits for
# a NEW result file in the app Documents container and copies it back over USB.
set -u
L=$1; OUT=$2; T=${3:-240}
D=${BENCH_DEVICE:-00008132-000C396801D9001C}
B=${BENCH_BUNDLE:-org.tonycloud.patchPerfBench}
mkdir -p "$OUT"
ls_files() { xcrun devicectl device info files --device $D --domain-type appDataContainer --domain-identifier $B --subdirectory Documents 2>/dev/null | grep -o 'bench_[A-Za-z0-9_]*\.json' | sort -u; }
BEFORE=$(ls_files)
xcrun devicectl device process launch --device $D --terminate-existing $B >"$OUT/launch_$L.log" 2>&1 || { echo launch failed; exit 1; }
end=$((SECONDS+T))
while [ $SECONDS -lt $end ]; do
  sleep 5
  NEW=$(comm -13 <(echo "$BEFORE") <(ls_files) | head -1)
  if [ -n "$NEW" ]; then
    xcrun devicectl device copy from --device $D --domain-type appDataContainer --domain-identifier $B --source Documents/$NEW --destination "$OUT/$L.json" >/dev/null 2>&1 && { echo OK $OUT/$L.json; exit 0; }
  fi
done
echo TIMEOUT; exit 2

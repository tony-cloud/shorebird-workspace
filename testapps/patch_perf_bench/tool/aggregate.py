#!/usr/bin/env python3
"""Aggregate per-run bench JSON files into the validation report + markdown table.

usage: aggregate.py RESULTS_DIR OUT_JSON [--meta meta.json]
Files: <RESULTS_DIR>/<LABEL>_run<N>.json for each variant label.
Frame metrics use only runs with >= 1000 frames (display awake).
Per metric: median = median over runs of the per-run median; p90 = median over
runs of the per-run p90; ratio = variant median / V0 median.
"""
import glob, json, re, statistics, sys, os

res, out = sys.argv[1], sys.argv[2]
meta = {}
if '--meta' in sys.argv:
    meta = json.load(open(sys.argv[sys.argv.index('--meta') + 1]))
FRAME_MIN = 1000  # a full 10 s collection at 120 Hz is ~1200 frames; fewer means the display was asleep
runs = {}
for f in sorted(glob.glob(os.path.join(res, '*_run*.json'))):
    m = re.match(r'(.+)_run(\d+)\.json', os.path.basename(f))
    runs.setdefault(m.group(1), []).append(json.load(open(f)))

def metrics(d):
    r = {}
    for k, v in d['micro'].items():
        r['micro/' + k] = (v['median'], v['p90'], 'us_per_rep')
    for k, v in d['transitions'].items():
        r['transition/' + k] = (v['median'], v['p90'], v['unit'])
    fr = d['frames']
    if fr['frames'] < FRAME_MIN:  # screen was off / app not presented: no usable frame data
        return r
    r['frames/build_ms'] = (fr['build_ms']['median'], fr['build_ms']['p90'], 'ms')
    r['frames/raster_ms'] = (fr['raster_ms']['median'], fr['raster_ms']['p90'], 'ms')
    r['frames/total_span_ms'] = (fr['total_span_ms']['median'], fr['total_span_ms']['p90'], 'ms')
    r['frames/over_8_4ms'] = (fr['frames_over_8_4ms'], fr['frames_over_8_4ms'], 'count')
    r['frames/over_16_7ms'] = (fr['frames_over_16_7ms'], fr['frames_over_16_7ms'], 'count')
    r['frames/count'] = (fr['frames'], fr['frames'], 'frames')
    return r

table = {}
for label, rs in runs.items():
    per = [metrics(d) for d in rs]
    row = {}
    for k in max(per, key=len):
        have = [p for p in per if k in p]  # frame metrics skip runs without a usable frame window
        meds = [p[k][0] for p in have]; p90s = [p[k][1] for p in have]
        row[k] = dict(median=statistics.median(meds), p90=statistics.median(p90s),
                      unit=have[0][k][2], run_medians=meds, run_p90s=p90s, runs_used=len(have))
    table[label] = dict(runs=len(rs), kernel_tags=sorted({d['kernel_tag'] for d in rs}), metrics=row)
base = table.get('V0')
if base:
    for label, t in table.items():
        for k, v in t['metrics'].items():
            b = base['metrics'][k]['median']
            v['ratio_to_V0'] = (v['median'] / b) if b else None
json.dump(dict(meta=meta, variants=table), open(out, 'w'), indent=1)
labels = [l for l in ('V0', 'V1', 'V2', 'V3', 'V3p') if l in table]
print('| metric | unit | ' + ' | '.join(f'{l} med / p90 (x V0)' for l in labels) + ' |')
print('|---|---|' + '---|' * len(labels))
for k in table[labels[0]]['metrics']:
    cells = []
    for l in labels:
        v = table[l]['metrics'][k]
        r = v.get('ratio_to_V0')
        cells.append(f"{v['median']:.4g} / {v['p90']:.4g}" + (f" ({r:.2f}x)" if r else ''))
    print(f"| {k} | {table[labels[0]]['metrics'][k]['unit']} | " + ' | '.join(cells) + ' |')

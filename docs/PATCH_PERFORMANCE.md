# iOS patch-route performance (first on-device numbers)

Measured 2026-11-03 on a physical iPad Pro 13-inch (M4), release mode, development
signing (no JIT, no debugger), with the then-current iOS engine build
`.agents/builds/engine-ios-native-host-gate-ci-final-20261102` (the tables below, up to
the update section; the update re-measures with a rebuilt engine). Raw data, per-run
values and caveats: [validation/ios-patch-route-benchmarks-20261103.json](validation/ios-patch-route-benchmarks-20261103.json).
App and harness: `testapps/patch_perf_bench/` (`tool/` has the build/run/aggregate scripts).

Variants (same source): **V0** stock-style AOT (TFA + inlining, no patch runtime);
**V1** patchable AOT, `interpreter-structural` release, unpatched; **V2** V1 plus a
real `shorebird patch ios` (local server, device download, encrypted envelope)
changing exactly one function; **V3** `interpreter-full` release, unpatched (the
bundled replacement base runs the whole app in the interpreter); **V3p** V3 plus a
full-replacement patch (control, identical to V3). 3 cold launches each; values are
median over runs of per-run median / p90. Ratio is to V0.

| metric (unit) | V0 | V1 patchable AOT | V2 one fn patched | V3 full interp |
|---|---|---|---|---|
| numeric loop (ms/rep) | 6.17 / 6.68 | 6.18 / 6.59 (1.0x) | 123 / 124 (20.0x) | 117 / 118 (18.9x) |
| virtual+interface calls (ms) | 2.25 / 2.27 | 7.46 / 7.57 (3.3x) | 98.6 / 99.5 (43.9x) | 112 / 114 (50x) |
| List/Map ops (ms) | 4.74 / 4.97 | 31.8 / 32.3 (6.7x) | 82.6 / 83.1 (17.5x) | 83.7 / 84.4 (17.7x) |
| string building (ms) | 12.5 / 12.7 | 35.9 / 36.2 (2.9x) | 61.9 / 63.2 (5.0x) | 63.0 / 63.6 (5.1x) |
| object allocation (ms) | 6.80 / 6.90 | 176 / 177 (25.9x) | 210 / 214 (30.9x) | 185 / 188 (27.2x) |
| AOT calls kernel, trivial (ns/call) | 1.4 / 2.0 | 76.5 / 77.0 (54x) | 120 / 121 (86x) | 35.2 / 36.8 |
| kernel calls AOT leaf (ns/call) | 1.0 / 1.0 | 81.4 / 81.9 (78x) | 24.2 / 24.8 (23x) | 18.7 / 19.0 |
| frame build (ms) | 0.72 / 1.05 | 2.02 / 3.82 (2.8x) | 2.30 / 4.00 (3.2x) | 5.80 / 6.25 (8.1x) |
| frame total span (ms) | 1.39 / 1.72 | 2.23 / 4.13 (1.6x) | 2.45 / 4.23 (1.8x) | 5.97 / 6.44 (4.3x) |
| frames > 8.4 ms (of ~1200) | 0 | 0 | 0 | 6-9 |

## Conclusions

- **Patchable AOT vs normal AOT.** Pure arithmetic is unchanged (1.0x). The cost
  is per non-inlined static call/constructor: about 75-80 ns in V1 against about
  1 ns in V0, so small-object allocation is 26x slower, List/Map 6.7x, virtual
  dispatch 3.3x. Frame build is 2.8x slower, but no frame missed the 120 Hz budget.
  The binary grows from 2.5 MB to 47 MB (structural profile retains all
  declarations). The ~75 ns was an unoptimized call path, not inherent: see the
  update section below, which removes it.
- **Interpreter slowdown.** About 19-20x on arithmetic vs stock AOT (matches the
  reported 20x), 17.5x List/Map, 5x strings, 44-50x virtual calls, ~27-31x
  allocation. Versus V1 (which already pays call overhead) it is 20x on arithmetic,
  3.9x List/Map, 1.7x strings, 13x virtual calls, 1.2x allocation.
- **Transitions.** AOT to patched function: about 120 ns/call (about 45 ns over the V1
  AOT call). Interpreted to AOT leaf: about 24 ns/call (full interpreter 18.7 ns).
- **Frames.** The light list workload (27 items/frame each doing a 2000-iteration
  kernel) costs 0.72 ms (V0), 2.0 ms (V1), 2.3 ms (V2), 5.8 ms (V3) build time. Even
  fully interpreted, no frame exceeded 16.7 ms on the M4. Raster time is 0.07-0.08 ms
  for V1-V3 vs 0.43 ms for V0 (unexplained). Heavier build() work would hit the
  interpreter factor directly.

## Update: direct calls to patchable functions

The ~75-80 ns per call was a code-generation artifact: every call to a patchable
function went through the `CallStaticFunction` stub and a `PatchStaticCall` runtime
call (stack walk and static-call-table lookup). AOT call sites now load the target
`Function` from the object pool and call through `Function::entry_point`, which
the bytecode patch installer already swaps (dart-sdk `5d8c94d9aad`; the same commit
also stops marking private/final/sealed `dart:` classes as extendable in structural
mode). Host microbenchmark (M-series, product AOT, `--no-tfa --shorebird-structural-patching`,
`.agents/patch-call-diag/bench.dart`, ns/iter): non-inlined static call 88.6 to 1.9,
constructor allocation 88.9 to 2.7, closure call 88.7 to 3.3, List/Map mix 93.6 to 38.9.

### Re-measured on the iPad (2026-11-03, after the fix)

Same device, app, workloads, signing and protocol as above (3 cold launches per
variant, median over runs of per-run median / p90). Engine:
`.agents/builds/engine-ios-direct-calls-20261103` (same GN args as the previous
engine except the Dart SDK pin, dart-sdk `5d8c94d9aad`). V0 was re-measured in the
same session as a control; V3 was not re-measured. Raw data and per-run values:
[validation/ios-patch-route-benchmarks-direct-calls-20261103.json](validation/ios-patch-route-benchmarks-direct-calls-20261103.json).
"Before" is the first table above.

| metric (unit) | V0 before / after | V1 before | V1 after | V2 before | V2 after |
|---|---|---|---|---|---|
| numeric loop (ms/rep) | 6.17 / 6.63 | 6.18 (1.0x) | 6.63 (1.00x) | 123 (20.0x) | 118 (17.9x) |
| virtual+interface calls (ms) | 2.25 / 2.41 | 7.46 (3.3x) | 7.29 (3.0x) | 98.6 (43.9x) | 97.9 (40.6x) |
| List/Map ops (ms) | 4.74 / 5.14 | 31.8 (6.7x) | 8.55 (1.67x) | 82.6 (17.5x) | 45.8 (8.9x) |
| string building (ms) | 12.5 / 13.8 | 35.9 (2.9x) | 20.9 (1.51x) | 61.9 (5.0x) | 44.2 (3.2x) |
| object allocation (ms) | 6.80 / 6.93 | 176 (25.9x) | 11.5 (1.66x) | 210 (30.9x) | 215 (31.0x) |
| AOT calls kernel, trivial (ns/call) | 1.4 / 1.5 | 76.5 (54x) | 1.73 (1.2x) | 120 (86x) | 31.4 (21x) |
| kernel calls AOT leaf (ns/call) | 1.0 / 0.8 | 81.4 (78x) | 1.51 (1.8x) | 24.2 (23x) | 24.2 (29x) |
| frame build (ms) | 0.72 / 0.70 | 2.01 (2.8x) | 2.10 (3.0x) | 2.29 (3.2x) | 2.12 (3.0x) |
| frame total span (ms) | 1.39 / 1.28 | 2.23 (1.6x) | 2.75 (2.1x) | 2.45 (1.8x) | 2.40 (1.9x) |
| frames > 8.4 ms (of ~1200) | 0 / 0 | 0 | 0 | 0 | 0 |
| `App.framework/App` | 2.55 MB / 2,387,872 B | 47.1 MB | 44,969,968 B | same as V1 | same as V1 |

Ratios in parentheses are to the V0 of the same column set (before: previous
session; after: this session's control). The V0 control itself drifted 5-12%
slower between the two sessions (device state), so compare ratios. The earlier
App sizes were reported as "MB" without a recorded method; the new ones are exact
`ls -l` bytes (44.97 MB decimal), so the apparent size reduction is not
established; V1/V0 size is 18.5x before and 18.8x after.

What changed on device:

- The per-call cost is gone: AOT-to-kernel-tag calls 76.5 to 1.7 ns and
  kernel-calls-AOT-leaf 81.4 to 1.5 ns in V1. Object allocation went from 26x to
  1.66x of stock AOT, List/Map from 6.7x to 1.67x, strings from 2.9x to 1.51x,
  virtual calls only from 3.3x to 3.0x (not investigated; likely interface
  dispatch without TFA/devirtualization rather than static-call cost).
- Frame build time did not improve (2.0 to 2.1 ms, still 3x V0); the cause
  was not investigated (the static-call path is evidently not the bottleneck there). The V1 frame figures come from a single valid run (display
  asleep on the others), so treat the 2.75 ms V1 span as noisy. No frame exceeded
  8.4 ms in any variant.
- V2 (one function patched) is still bounded by the interpreted function: numeric
  and virtual rows are unchanged (about 18-41x). Rows that call unchanged AOT
  classes from the interpreted function improved: List/Map 82.6 to 45.8 ms and
  strings 61.9 to 44.2 ms. AOT-to-patched-function calls dropped from 120 to
  31.4 ns/call (the remaining cost is the transition into the interpreter); object
  allocation did not improve, 210 to 215 ms (not investigated; the allocation
  kernel runs inside the interpreted function).
- App size of V1 stays ~19x stock AOT: the structural profile still retains all
  declarations. The direct-call change did not shrink it in a way that can be
  distinguished from measurement-method differences.

## Deviations and limits

- The default `interpreter-body` release was built but its patch was rejected on
  device ("Bytecode patch introduces a library absent from the base snapshot"; app
  fell back to base), so V1/V2 use `interpreter-structural`. Cause not investigated.
- V0 is the fork's own Flutter/engine without the patch runtime; no upstream stock
  Flutter was used.
- In V2 only `patchedKernel(mode,n,Workset)` is patched; its virtual, collection and
  allocation kernels call unchanged AOT classes (mixed-mode). V3 gives pure
  interpreter numbers. `patchedKernel`/`aotLeaf` are `never-inline` in all variants.
- One device, 3 cold launches per variant; V1-V3 perform a startup update check
  (Local Network permission granted). Development signing (get-task-allow) was
  used to copy results out of the app container.

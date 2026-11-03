# iOS patch-route performance (first on-device numbers)

Measured 2026-11-03 on a physical iPad Pro 13-inch (M4), release mode, development
signing (no JIT, no debugger), with the existing iOS engine build
`.agents/builds/engine-ios-native-host-gate-ci-final-20261102`. Raw data, per-run
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
  declarations). Whether the ~75 ns is inherent or an unoptimized call path in
  the patchable build was not investigated; it decides patchable-AOT vs a linker.
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

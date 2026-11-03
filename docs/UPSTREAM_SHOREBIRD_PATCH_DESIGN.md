# Upstream Shorebird iOS patching: what is public, and what we can reuse

Clean-room study, 2026-11-03. Only public, openly licensed sources and public
documentation were read (see "Sources" and "Licenses"). The closed `aot-tools`
linker (`aot-tools.dill`) was not downloaded, decompiled or read. The Dart SDK
fork (`git@github.com:shorebirdtech/dart-sdk.git`, referenced from
`engine/DEPS:19`) returns HTTP 404 on the GitHub API, so the VM side
(interpreter, call-site mechanism) is **not public**; section 1 is therefore
assembled from the engine embedder code, the CLI/Flutter-tool sources and the
maintainers' public status posts, and marks inference as inference.

Local clones (shallow, kept out of git by `.gitignore` `.agents/`):
`.agents/upstream-shorebird-study/{engine,flutter,updater,buildroot}`;
issue #674 comments saved as `.agents/upstream-shorebird-study/issue674.txt`.
Our CLI fork is `shorebird/` (derived from the open-source CLI).

## Summary

- Upstream does **not** build a "patchable" release. The release is stock AOT
  (TFA and inlining on) compiled by `gen_snapshot`, plus a few extra
  *link-info* dumps and a `--deterministic` flag. The patch is compiled with
  the same compiler and flags from the new kernel. A linker then compares the
  two AOT snapshots and decides, per function, whether the patch can reuse the
  release's native code (runs at full speed) or must run in an in-VM ARM64
  simulator ("mixed mode"). Unchanged code keeps its direct `bl` calls; there
  is no per-call lookup on the common path.
- The execution engine for non-linked code is the Dart VM's ARM64 **simulator**
  (SIMARM64-style, executing real compiled machine code of the patch), not a
  bytecode interpreter. Public posts say "simulator", "mixed mode", "jumps
  between the CPU and simulator".
- The hard problem is stability of everything a function's machine code
  encodes: object-pool indices, class ids, field offsets, dispatch-table
  offsets, and relative call distances. Their answer is to compile the patch
  so those tables look as identical to the release's as possible (sorting
  against the release's dumped tables), then compare code. The CLI only
  threads files and flags through; the sorting/comparison logic is in the
  closed linker.
- A patch's quality is reported as "link percentage" (share of Dart code that
  stays on the CPU). Public numbers: 99% for no-change patches, ~30% before the
  object-pool fix and ~99% after in tests; 90% soft-warn, 20% hard-fail
  default (`--min-link-percentage`).
- For our design: the ideas worth taking are (a) keep release = stock AOT with
  TFA and inlining, (b) emit and persist class/field/dispatch/object-pool
  layout tables at release time and force the patch build to reproduce the
  release's layout, (c) per-function equality on normalised machine code,
  (d) route only high-fan-in callees through an indirect table (their "DD"
  cascade limiter). All of this is implementable from public Dart VM
  knowledge; none requires their closed code.

## 1. Runtime side

### 1.1 Patch file format and loading (public engine code)

- The patch artifact is `dlc.vmcode` (updater `library/src/cache/lifecycle.rs`
  lines 13, 385; `patch/README.md:26` calls `.vmcode` "the patch container").
  The CLI builds it as `out.vmcode` from `out.aot` via the linker
  (`shorebird/.../apple_patcher_mixin.dart` `vmcodeOutputPath`, ~line 118).
- Content, per the engine: "vmcode files are elf files prefixed with a
  shorebird linker header" (`engine/runtime/dart_snapshot.cc:81`). The engine
  calls `Shorebird_ReadLinkHeader(mapping, size)` to find the ELF offset, then
  `Dart_LoadELF(path, elf_offset, &error, &vm_data, &vm_instrs, &isolate_data,
  &isolate_instrs, /*load as read-only, not rx*/ false)` (lines 83-90). Only
  `isolate_data` and `isolate_instrs` are used from the patch; the VM snapshot
  from the patch is ignored: "The VM Snapshot is identical for all binaries
  produced by a given version of Dart. Our linker checks this and will fail to
  link if ever the VM snapshot changes" (lines 70-73). The ELF is loaded
  non-executable (data only), which is why the simulator can run it on iOS
  without JIT: the patch's machine code is read as data.
- The base snapshot stays mapped from `App.framework/App`. The engine calls
  `Shorebird_SetBaseSnapshots(isolate_data, isolate_instrs, vm_data,
  vm_instrs)` (`engine/shell/common/shorebird/shorebird.cc:54-64`, enabled by
  `SHOREBIRD_USE_INTERPRETER=1` on iOS, `engine/common/config.gni:80-82`), so
  the VM holds both the patch image and the base image. `shorebird.cc:236-240`
  adds the patch to the *front* of the library list instead of replacing the
  base ("to allow dart_snapshot.cc to still find the base snapshot").
- The download is a bidiff against a "patch diff base" of the release
  (`updater/patch/README.md`; CLI `generatePatchDiffBase` / `dump_blobs`,
  `apple_patcher_mixin.dart:~165-205`). `engine/shell/common/shorebird/
  snapshots_data_handle.{h,cc}` exposes the four base blobs (vm data/instrs,
  isolate data/instrs) as one seekable stream to the Rust updater so it can
  inflate the diff. The updater verifies a SHA-256 of the inflated file.
  Symbols `Shorebird_ReadLinkHeader` / `Shorebird_SetBaseSnapshots` live in the
  private Dart fork; the link header layout is not public.

### 1.2 Function mapping, interpreter, call mechanism (public posts only)

Source: maintainer status posts in
https://github.com/shorebirdtech/shorebird/issues/674 (comments dated
2023-08-28 to 2024-03-28; saved locally).

- Execution engine: a simulator. 2023-09-07: "mixed mode working both entering
  and leaving the simulator / real cpu... teach the Dart garbage collector
  about the two separate stacks and the 'stop execution' link-register markers
  the simulator uses". 2023-09-21: exceptions thrown "across multiple jumps
  in/out of the CPU/simulator". So the non-linked path is the Dart VM ARM64
  simulator running the patch's compiled code, with a second (simulated) stack
  and GC/exception support for transitions. (Inference from these posts: this
  is the VM's existing SIMARM64 extended to run on real ARM64 hosts, with the
  GC and exception unwinder taught both stacks.)
- The unit of reuse is a "function block" and its direct-jump graph.
  2023-10-23: the linker "knows how to analyze and understand not just
  function blocks but also direct jumps between those function blocks and
  produce a lookup table for the VM to use to know which function block
  sub-graphs it's safe to jump into on the CPU".
  Implication (inference): a function can run natively from the release only
  if its machine code is byte-equivalent to the patch's *and* everything it
  reaches by direct `bl` is also unchanged or entered through a safe stub,
  because a direct branch inside release code can only reach release code.
- Redirection: the first design handled redirection "in the callee with a
  function prologue" and was found "insufficient for the indirect jump
  approach (we'll need to interpose indirect jumps with a stub of some form)
  as well as possibly superfluous once we fix indirect jumps to use a stub
  (since direct jumps are now covered by our linker)" (2023-10-23). So the
  shipped mechanism is: static linker proof for direct calls (no runtime cost),
  stubs for indirect calls.
- Staging: first release linked only leaf functions ("functions which make no
  calls to other functions"); 2024-01-09 added linking of functions that jump
  from the CPU back to the simulator.
- Dispatch table / virtual calls: AOT virtual calls use the dispatch table
  (class-id indexed). The CLI supplements `App.dt.link` / `App.dispatch_table.
  json` and the 2024 release notes ("Count base dispatch table offsets kept on
  a patch", `shorebird/RELEASE_NOTES.md:18`) show the patch is made to reuse the
  release's dispatch-table offsets. Mechanism for patched targets is not public.

### 1.3 Inlining, TFA, class ids, fields, dispatch offsets

- Public docs: optimisations make changes non-local; "tiny changes can
  sometimes cause large sections of your application to 'un-link' and run in
  the interpreter" (https://docs.shorebird.dev/code-push/performance/; names
  type flow analysis and inlining). So TFA and inlining stay **on** in the
  release and are a *source of link loss*, not something they disable.
- Class ids, field offsets, dispatch offsets: gen_snapshot dumps a class table,
  field table and dispatch table at release and patch time (section 2). The
  patch build/linker then reorders so ids and offsets match the release
  (CLI debug-snapshot names `out.ct.aot` "CT-sorted", "CT + OP sort",
  `apple.dart:~235-245`; "CT" = class table, "OP" = object pool). Added
  classes need ids that do not shift existing ones; a release note says
  "Reserve a trailing cid window on popular rows" (`RELEASE_NOTES.md:17`).
- Object pool: the single biggest early problem, described in detail in #674
  (2024-02/03): inserting a constant shifts every later pool index so all code
  after it differs; fix = sort the patch's pool to match the release.

### 1.4 How they avoid a per-call lookup

- Linked code makes the same direct calls as stock AOT (zero added cost).
  Their cost sits at CPU/simulator transitions, not on every call.
- The cascade problem (callee changes, so every direct caller is un-linked) is
  limited by "DD", documented in the CLI: `--dd-max-bytes`: "Dynamic Dispatch
  table cascade byte threshold. Functions whose transitive caller tree exceeds
  this many bytes are routed through the indirect dispatch table"
  (`shorebird/.../common_arguments.dart:231-238`). A two-pass release build
  precomputes which functions get indirect slots (below). So indirection is
  paid only by popular callees whose cascade would be huge.

## 2. Build side

Open-source Flutter fork (`flutter/packages/flutter_tools/lib/src/base/
build.dart`, `AOTSnapshotter.build`), versus stock Flutter:

- gen_snapshot flags added: `--deterministic` ("improve snapshot stability and
  increase linking", line ~210), and on iOS/macOS the dump flags
  `--print_class_table_link_info_to=App.ct.link`,
  `--print_class_table_link_debug_info_to=App.class_table.json`,
  `--print_field_table_link_info_to=App.ft.link` (+ `_debug_info_to=
  App.field_table.json`), `--print_dispatch_table_link_info_to=App.dt.link`
  (+ `App.dispatch_table.json`) (lines 213-221). Snapshot kind stays
  `app-aot-assembly` on Apple. Normal flags, TFA and inlining are untouched.
- Optional 2-pass "DD" release (when `SHOREBIRD_DD_MAX_BYTES>0`,
  `_runDdAnalysisPass`, lines ~360-470): pass 1 builds an ELF
  (`--snapshot_kind=app-aot-elf`) with `--print_dd_function_identity_to=
  App.dd_identity.link`; then `analyze_snapshot --compute_dd_table=App.dd.link
  --dd_caller_links=App.dd_callers.link --dd_max_bytes=N`, then
  `analyze_snapshot --compute_dd_slot_mapping=App.dd_slots.link
  --dd_table_data=... --dd_caller_links=... --dd_function_identity=...`;
  pass 2 (the real build) adds `--dd_slot_mapping=App.dd_slots.link` and
  `--print_dd_resolution_to=App.dd_resolution.tsv`. These flags exist in the
  Dart fork's gen_snapshot/analyze_snapshot (private source).
- "Supplement" bundle: `LinkSupplement.create` (`targets/common.dart:~550-580`)
  copies all `App.{ct,dt,ft,dd,dd_callers,dd_identity,dd_slots}.link`, the
  `*.json` debug dumps, `dd_resolution.tsv` and the kernel into a `shorebird/`
  directory; the CLI stores it as the release's supplement artifact and
  restores it at patch time (`apple.dart copySupplementFilesToSnapshotDirs`,
  ~lines 82-135, patch copies renamed `App.*` to `out.*`).
- Patch build: same `flutter build ipa` path (so same gen_snapshot flags) to
  produce `out.aot` (ELF) and the patch's own `out.*.link` files, then the
  linker is invoked (`apple.dart runLinker`, ~196-325; `aot_tools.dart link`,
  ~335-385):
  `aot-tools link --base=<release App.framework/App> --patch=<out.aot>
  --analyze-snapshot=<analyze_snapshot> --output=<out.vmcode> --verbose
  [--gen-snapshot=<gen_snapshot> --kernel=<app.dill> --reporter=json
  --redirect-to=<link.jsonl>] [--dd-max-bytes=N] [--dump-debug-info=<dir>]
  [-- <split-debug-info/obfuscate gen_snapshot args>]`.
  Output: `out.vmcode`, and a JSONL stream from which the CLI reads the link
  percentage (`_extractLinkPercentage`) and `link_failure` events.
  So the linker receives: base snapshot, patch ELF, `analyze_snapshot`,
  `gen_snapshot` and the **kernel** (it re-runs gen_snapshot on the new kernel),
  plus the release `*.link` files placed beside the snapshots.
- Linker intermediate outputs (named in the CLI debug dump, `apple.dart:~235-
  250`): `out.aot` (initial), `out.ct.aot` (class-table sorted),
  `out.preDdOptimized.aot` (CT + object-pool sort, no DD), `out.ddOnly.aot`
  (CT + DD, no OP sort; supplies a patch `op.link`), `out.optimized.aot`
  (final). This shows the linker works by **recompiling the patch several
  times with gen_snapshot, steered by hint files derived from the release**,
  then comparing. The hint file formats are not documented.
- `analyze_snapshot` is built in the engine for iOS archives
  (`engine/lib/snapshot/BUILD.gn:241`, "Added by shorebird"); it is a Dart
  fork tool, extended with the `--compute_dd_*` options above.
- A second step after the link: diff base (`dump_blobs`) and bidiff.

## 3. Public description of the linker algorithm

Only high-level. Docs: https://docs.shorebird.dev/code-push/system-architecture/
("a Dart 'linker' decides per function what is reused from the previous
binary"); https://docs.shorebird.dev/code-push/performance/. Maintainer posts
(#674) give the most detail:

- Matching: functions are compared as machine code ("function blocks"),
  including their direct jumps; reuse requires identical code after the patch
  tables have been sorted to the release's (class table, field table, dispatch
  table, object pool; 2024-01/03 posts, supplement file names).
- "Link percentage" = share of Dart code (by function/size, per CLI output)
  that runs from the release's native code instead of the simulator. The CLI
  option text: "Minimum % of Dart code that must be linked (not interpreted)
  for the patch to publish. iOS only" (docs `patch` page, per lookup). Warns
  below 90%; defaults to failing below 20% from CLI 1.6.135
  (`RELEASE_NOTES.md`; PR shorebirdtech/shorebird#4334, which cites ~1% of
  patches below 20%).
- What forces interpretation (public statements): any change in a function's
  own code; constants added/removed (object pool shift) until sorted;
  adding/removing functions, classes, strings, types shifting snapshot indices
  ("index" stability, 2024-01-31); inlining/TFA ripple effects; changes that
  "shift dynamic dispatch" (1.6.118); `--split-debug-info` and obfuscation
  (several release notes: 1.6.40, 1.6.118); large callee cascades (DD).
- Metrics timeline: no-change patch 80% (Jan 2024) to 99% (Jan 31, 2024);
  small-change patch 3% to 12% to 30% (0.22-0.24), then ~99% with the
  object-pool sort ("most iOS patches run as fast as they would on Android",
  0.28.0, 2024-03-28).

## 4. Published performance claims

- "Some portions of Dart run up to 100x slower when run in our interpreter";
  "about 2-10x slower" for apps; "~112x slower than on the real cpu" for the
  first simulator-only alpha (#674, 2023).
- "9 out of 10 patches do not run into unexpected non-local changes... and
  exhibit no difference after patching"; "no good way to predict if a patch
  will cause a performance change" (docs performance page).
- "Typically this change is undetectable... you may see a performance
  difference" (docs patch page).
- Linked-percent metrics above are their only quantitative measure. They
  publish no per-benchmark numbers for patched code. Our own measured
  ~20x interpreter slowdown is not stated by them (the "20x" figure was not
  found in any public source).

## 5. Comparison with our design and recommendations

| | Upstream (public facts) | Ours |
|---|---|---|
| Release | stock AOT, TFA + inlining on, link tables dumped | patchable AOT, no TFA, structural profile, all calls patchable (76 ns/call) |
| Unpatched speed | 100% | 1.0x arithmetic, 3-26x on call-heavy |
| Patched code | native if proven identical, else ARM64 simulator running compiled code | bytecode interpreter (about 20x slower) |
| Decision point | build time, by comparing snapshots (closed linker) | run time, per call |
| Cost model | pay at transitions and for un-linked code | pay on every non-inlined call |

Implementable independently from public information:

1. **Make the release stock AOT** (TFA, inlining on), as upstream does, and
   dump layout tables at release time (class ids/field offsets/dispatch
   offsets/object-pool index mapping/function identity+size). The Dart VM is
   BSD and already has the data; gen_snapshot can write it (their flags show
   what they chose: class table, field table, dispatch table, DD identity).
2. **Patch builds must reproduce the release's layout**: deterministic
   compile, sort the class table/object pool/field table/dispatch table to
   the release's order, reserve id windows for new classes. Upstream took two
   weeks of "incredibly complicated" Dart compiler work for the object pool
   alone (#674, 2024-01-31); plan for it.
3. **Per-function equality**: after normalising PC-relative offsets, compare
   release and patch machine code per function (their "function blocks"). Mark
   a function linked only if it and its direct-call closure are unchanged, or
   if each outgoing edge is an indirect edge that can be redirected.
4. **Replace our runtime lookup with a table slot** for the edges that need it:
   an indirect call through a per-function (or per-callsite) slot costs a
   load plus `blr` (about 1-3 ns), versus our ~76 ns runtime call. Upstream
   only does this for high-fan-in callees ("DD"), giving direct calls
   elsewhere. A hybrid keeps full speed for the common case.
5. **Fallback engine**: ours is a bytecode interpreter (about 20x). Upstream
   runs the patch's compiled ARM64 code in the VM simulator; it needs the same
   GC/exception work (two stacks, stop markers). That is large but is also
   public in the sense that SIMARM64 is in the open Dart SDK. We can keep our
   interpreter and still adopt 1-4; the interpreter only changes the speed of
   un-linked code, and the fast-path design is independent of it.
6. **Transitions** (native to interpreted, interpreted to native) are the
   other cost; ours are 120 ns and 24 ns (PATCH_PERFORMANCE.md). Upstream's
   are not published.

What depends on the closed linker / private fork (do not assume we get it):
the link-header format and lookup table, the patch-vs-release comparison and
table-sorting algorithms, the hint-file formats consumed by gen_snapshot, the
DD cascade solver, the VM's mixed-mode implementation (stubs, simulator
transition, GC, exceptions), and the `--print_*_link_*` / `--dd_*` flag
implementations. Everything in this report on those parts is either a CLI/
Flutter-tool fact or a maintainer statement, not source.

## Sources

- Engine fork (BSD-3-Clause): `.agents/upstream-shorebird-study/engine`
  (branch `shorebird/dev`, commit e4575c74): `runtime/dart_snapshot.cc:60-130`,
  `shell/common/shorebird/shorebird.cc:54-64,225-250`,
  `shell/common/shorebird/snapshots_data_handle.{h,cc}`,
  `common/config.gni:76-82`, `lib/snapshot/BUILD.gn:241`, `DEPS:19`.
  Online: https://github.com/shorebirdtech/engine
- Flutter fork: `.agents/upstream-shorebird-study/flutter` (commit c3ecdcf0):
  `packages/flutter_tools/lib/src/base/build.dart` (AOTSnapshotter, DD pass),
  `build_system/targets/common.dart` (LinkSupplement), `shorebird/
  shorebird_defines.dart`. https://github.com/shorebirdtech/flutter
- Updater: `.agents/upstream-shorebird-study/updater` (commit cb1ddcbf):
  `patch/README.md`, `library/src/cache/lifecycle.rs`.
  https://github.com/shorebirdtech/updater
- Open-source CLI (our fork `shorebird/`, commit 03782b34):
  `packages/shorebird_cli/lib/src/platform/apple/apple.dart`,
  `commands/patch/apple_patcher_mixin.dart`, `executables/aot_tools.dart`,
  `common_arguments.dart:231`, `cache.dart:576-600`, `RELEASE_NOTES.md`.
  https://github.com/shorebirdtech/shorebird
- Issue 674 comments (Aug 2023 to Mar 2024):
  https://github.com/shorebirdtech/shorebird/issues/674
- Docs (read through a lookup helper, quotes short):
  https://docs.shorebird.dev/code-push/{faq,system-architecture,performance,
  patch,troubleshooting}/
- Release notes and PRs: https://github.com/shorebirdtech/shorebird/blob/main/
  RELEASE_NOTES.md; PR #4334; issues #1892, #2287, #3714, #2406.
- Not used: shorebirdtech/dart-sdk (not publicly accessible), `aot-tools.dill`
  (closed; the CLI fork keeps it behind an opt-in env var, never downloaded
  here), Medium post (403, unverified).

## Licenses

| Repo / source | License |
|---|---|
| shorebirdtech/engine | BSD-3-Clause (GitHub API; Flutter Authors `LICENSE`) |
| shorebirdtech/flutter | BSD-3-Clause (Flutter Authors `LICENSE`) |
| shorebirdtech/buildroot (cloned, only checked) | BSD-3-Clause-style (Chromium Authors `LICENSE`) |
| shorebirdtech/updater | MIT or Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`) |
| shorebirdtech/shorebird (CLI, via our fork) | MIT or Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`; GitHub reports NOASSERTION for dual files) |
| docs.shorebird.dev, issue threads, RELEASE_NOTES | no explicit license stated; used only as quoted facts with attribution, no text copied wholesale |
| shorebirdtech/dart-sdk, aot-tools | not public / closed; not read |

No code was copied from these sources into the repo. The facts above are
descriptions only; any implementation must be written independently.

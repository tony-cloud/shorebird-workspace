# iOS native-reuse code push: compiler and linker

Status 2026-11-04. Implemented and validated on host (macOS arm64, product AOT,
no compressed pointers, the same target features as iOS). The runtime half
(native execution of linked code, ARM64 simulation of the rest) is described in
[`RUNTIME.md`](RUNTIME.md). This document owns the release/patch layout
contract, the link table and the `vmcode` container; section 6 is the format
the runtime consumes.

This is an independent implementation. The only inputs from Shorebird's
closed `aot-tools` are the black-box observations in
[`../AOT_TOOLS_EXPLORATION.md`](../AOT_TOOLS_EXPLORATION.md). Public design
notes are in [`../UPSTREAM_SHOREBIRD_PATCH_DESIGN.md`](../UPSTREAM_SHOREBIRD_PATCH_DESIGN.md).
No closed code was copied, decompiled or reverse-engineered.

Code:

- Dart SDK branch `tonycloud/ios-aot-linker-20261104` (base `2513af5688a`):
  - `runtime/vm/compiler/aot/link_info.{h,cc}`: keys, sidecar reader/writer, static field remap, DD target set.
  - `runtime/vm/class_finalizer.cc`: class ids.
  - `runtime/vm/object.cc`: field offsets.
  - `runtime/vm/field_table.{h,cc}`: static field ids.
  - `runtime/vm/compiler/assembler/{object_pool_builder.h,assembler_base.cc}`: pool slots.
  - `runtime/vm/compiler/aot/dispatch_table_generator.cc`: selector offsets.
  - `runtime/vm/compiler/backend/flow_graph_compiler_arm64.cc`, `inliner.cc`: DD calls.
  - `runtime/vm/image_snapshot.cc`: code placement.
  - `runtime/vm/compiler/aot/precompiler.cc`: hooks.
- `tools/ios_linker/` in this repository: the analyzer, linker, checker,
  pool-hint pass, DD selection and experiment drivers (pure Dart, no packages).

## 1. Summary

| Step | What happens |
| --- | --- |
| Release | Normal TFA-optimized AOT. `--print_link_info_to=App.link` writes the layout sidecar. The release binary is **byte-identical to stock** (verified for all three test programs). Optional `--link_dd_targets` adds DD slots. |
| Patch | A complete optimized AOT snapshot of the new program, compiled twice with `--link_base`. Pass 1 uses the release sidecar. Pass 2 uses the release sidecar plus pool hints from pass 1. |
| Link | `ios_linker link` compares every compiled code entry of release and patch and writes `out.vmcode` = link header + the patch ELF. |
| Check | `ios_linker check` re-verifies the table from the two binaries alone, word by word and branch by branch. |
| Download | Updater `patch` tool (bidiff + zstd) of `out.vmcode` against the release data+text blob. |

Results (section 9): identical patches link 100%; changing a displayed string
links 100%; one caller edit 99.1%; a shared helper behind 80 callers 97.9%.
Behind 320 callers it is 93.0% without DD and 99.99% with DD (only the helper
itself, 32 bytes, stays unlinked). On the real Flutter app
(`testapps/patch_perf_bench`, normal TFA kernel) edits link 99.3–100%. Every
link table passed the independent checker.

## 2. Model

```mermaid
flowchart TD
  K1[Release kernel, TFA] --> R[gen_snapshot --print_link_info_to]
  R --> RA[App binary: stock bytes]
  R --> RL[App.link sidecar: layouts and code placement]
  K2[Patch kernel, TFA] --> P1[gen_snapshot pass 1 --link_base=App.link]
  RL --> P1
  P1 --> H[ios_linker pool-hints]
  RL --> H
  H --> P2[gen_snapshot pass 2 --link_base=constraints.link]
  K2 --> P2
  P2 --> PE[out.aot ELF + out.link]
  RA --> L[ios_linker link]
  RL --> L
  PE --> L
  L --> V[out.vmcode = link header + out.aot]
  V --> C[ios_linker check]
  V --> D[bidiff + zstd vs release data+text]
```

The central invariant is simple. **At run time every table comes from the
patch snapshot.** That covers the global object pool (PP), the dispatch table,
the class table, the static field table and the object store. The release
contributes only machine instructions. Release code therefore executes
*exactly* the patch's semantics whenever its instructions are byte-identical
to the patch's own compilation of the same entry, apart from direct branch
displacements, which are handled separately. The layout constraints in
section 3 exist only to make that byte identity likely. They are never needed
for correctness.

## 3. Release facts and patch constraints

### 3.1 Sidecar (`App.link`)

The sidecar is line-oriented, tab-separated, UTF-8, with the header
`#osb-link-info 1`. Keys never contain tabs or newlines.

| Line | Columns | Recorded when |
| --- | --- | --- |
| `meta` | name, value (`num_cids`, `num_statics`, `stub_pool_length`, `pool_length`, `dispatch_table_size`) | as produced |
| `class` | cid, class key, instance size, next field offset, type-arguments offset, super cid, **subtree end** | after class finalization and id assignment, **before tree shaking** |
| `field` | offset, unboxed size (0 = boxed), field key | same |
| `static` | field id, field key | same, from a heap walk over all `Field`s |
| `selector` | selector id, offset, selector key | after dispatch table layout |
| `pool` | index, entry type, patchability, key (empty = unused release slot) | after compilation |
| `uses` | function code key, comma list of pool indices requested in order | per successful function compile |
| `code` | image symbol, payload offset in the text image, payload size, code key | while the text image is written |
| `call` | owning code offset, return-pc offset, call kind, entry kind, target key | from the code's static call table |

Layout facts are recorded before the precompiler drops classes and fields. A
field or class that the release tree-shook still determined the offsets of
the code that was kept. Recording earlier was the fix that took identical
patches from 28% to 100%.

The sidecar is 0.86–1.0 MB for the fixtures and 3.6 MB for the Flutter app
(93–323 KB with `zstd -19`). It belongs in the release supplement, not in the
app.

Keys (`LinkInfo::*Key`):

| Entity | Key |
| --- | --- |
| Class | `library-url::ScrubbedName` |
| Field | `class key::[static:]name` |
| Function | `class key::name#kind/numParameters[s]`. A closure is keyed by its parent's key plus the closure name and its token offset relative to the outermost function. A tear-off is `parent::<tearoff>`. |
| Code | `fn:` function key; `alloc:` class key; `tts:` type key; `stub:` stub name |
| Selector | `name\|function kind\|smallest declaring class key\|order-independent hash of all declaring classes`. Remaining collisions are numbered in selector-id order. |
| Pool constant | content keys: `str:` length + FNV-64 of UTF-16 units; `int:`, `double:` bits, `type:` / `targs:` structural, `tparam:` class and index, `field:`, `function:`, `code:`, `closure:`, canonical `array:`, `ucall:` / `icdata:` / `mega:` selector + arguments descriptor, and `const:` class + canonical hash |
| Pool entry without a stable content key | `site:<function key>#<n>`: the n-th pool request of that function's code. This covers patchable call-site entries, native-call slots, non-canonical (identity) objects and function type parameters. |

Source URIs are part of identities. Release and patch must compile the app
from the same URIs. This is true for `package:` URIs; the fixtures use one
canonical file path.

### 3.2 Class ids

`ClassFinalizer::AssignClassIdsFromLinkBase` replaces `SortClasses` when
`--link_base` is set:

- Every class whose key is in the release keeps its release cid.
- All other classes get cids after the release's range, in hierarchy
  preorder (the same order `SortClasses` uses, for compact subtype ranges).
- Release cids that are unused by the new program stay as holes. The class
  table is grown first and the permutation completed with the empty rows.
  Holes are already normal in AOT, since `DropClasses` creates them.

Holes inside a cid range keep range checks valid, because no instances exist.
A new subclass outside its parent's range forces the patch's own range checks
to change, so those functions simply do not link. The patch's compilation is
always made with the patch's real cids.

### 3.3 Instance fields, unboxed fields, type arguments

`Class::CalculateFieldOffsetsFromLinkBase`:

1. Mark the words used by the header, by every ancestor field and by
   ancestors' type-argument fields, using their actual patch offsets.
2. A field whose key exists in the release keeps its release offset if
   - its representation is unchanged (boxed, or unboxed with the same size,
     from TFA's unboxing metadata via `is_unboxed` and `guarded_cid`),
   - the words are free, and
   - an unboxed field still fits the unboxed-field bitmap.
3. A class that introduces type arguments keeps the release type-arguments
   offset under the same rule.
4. Everything else is placed at
   `max(super end, end of placed fields, release subtree end of this class)`.
   - This covers new fields, fields whose unboxing changed, and fields whose
     words are taken.
   - The *subtree end* is the largest next-field offset of the class and all
     of its release subclasses. A field added to a superclass therefore can
     never collide with a subclass field that keeps its release offset.
5. Instance size = rounded end. Unused words are tagged slots initialized to
   null by allocation, so the GC sees null.

Measured: adding a field to `Model` changed only the code that allocates or
reads `Model` (p4). The runtime fixture added two fields (double and int) to
a superclass with a subclass; the patch's output matched stock.

### 3.4 Static field table

`LinkInfo::RemapStaticFieldIds` runs right after class ids, before any code
is compiled:

- It collects every static `Field` object, including clones, by heap walk.
- Fields known to the release get their release id. Other fields are
  appended after the release's count.
- Values in the initial, sentinel and isolate field tables move with the
  ids (`FieldTable::Remap`). Unused ids hold the sentinel.

An earlier dictionary-based version missed fields and left stale ids, so the
heap walk is required.

### 3.5 Global object pool

PP is the patch's pool. Base code is correct with any pool *contents*,
provided its indices are equal. Constraints:

1. **Reservation.** After the shared stub entries (identical for one SDK), the
   patch reserves `[stub_pool_length, release pool_length)` and fills it with
   immediate-0 placeholders. An entry whose key maps to a free release slot is
   placed there; all other entries are appended after the reserved range
   (`ObjectPoolBuilder::TryPlaceAtLinkSlot`). Unused release slots stay as
   zero immediates (8 bytes each).
2. **Pairs.** Patchable entries are allocated in adjacent pairs that one `ldp`
   loads (switchable call stub + data, native call entry + target, type-test
   cache + name). A patchable entry is placed only if the next slot is free
   too, and a patchable request immediately following a placed one goes into
   that next slot. One half of a pair can never be placed while the other is
   appended.
3. **Reachability.** Per-function compiles use a child builder whose entries
   form the per-`Code` "local pool". The precompiler and the AOT serializer
   use that local pool to decide which global entries are live. Placed entries
   are therefore also recorded as used-from-parent. Without this, the serializer
   silently dropped live objects (static field initializers, allocation stubs,
   type-test stubs) and the patch shrank by 107 KB.
4. **Second pass** (`ios_linker pool-hints`). The patch's pass-1 compilation
   records which pool indices each function requested, in order. For a
   function present in both builds with sequences of equal length, if position
   *j* uses release slot *b* in the release but an appended slot in the patch,
   and *b* is a hole in pass 1 with the same entry type, the hint assigns that
   patch key to *b*. Pairs move together. Pass 2 compiles with these hints.
   This is how a changed string reaches 100%: the new string takes the old
   string's slot, so the code using it is byte-identical and loads the new
   value through PP.

### 3.6 Dispatch table

Selector rows are fitted most-popular-first at their release offsets
(`fitter.FitAndAllocate(row, off, off)`), before the normal passes. A row that
no longer fits there (new cids extend it into a neighbour) is placed normally.
Code calling it then differs and does not link. The table itself is the
patch's; its entries point at patch `Code`, which the runtime maps to release
entry points or gates. New classes get cids after the release range, so a row
grows at its end.

Possible improvement: reserve a trailing cid window per popular row. Upstream
release notes mention this. It was not needed for these tests.

### 3.7 DD slots (release-time indirection)

With `--link_dd_targets=<file>` (one function key per line), every static call
to a listed function is emitted as follows (`GenerateStaticDartCall`):

```
ldr  x0, [pp, #slot(Function)]     ; FUNCTION_REG, pinned pool slot
ldr  lr, [x0, #Function::entry_point]
blr  lr                            ; checked entry
```

This replaces `bl target`. DD targets are never inlined. The `Function` is a
pool constant with a content key, so it keeps its slot. The caller's bytes no
longer depend on the callee's code. At run time the slot holds the *patch's*
`Function`, whose entry point the runtime sets to the release code (if linked)
or a gate. The same target file must be passed to the release and to every
patch build; it is part of the supplement. No sentinel slots exist. A DD
target removed by a patch is no longer called by any patch code, so release
code calling it cannot be linked (it would differ).

## 4. Code-entry identity analysis and the link rule

`tools/ios_linker/lib/src/linker.dart` analyzes every code entry in both text
images: functions, stubs, allocation stubs, type-testing stubs and
dispatchers. Its unit is the instructions payload, at a 4-byte granularity.

### 4.1 Normalization

The analyzer decodes every instruction word.

- **B/BL whose target leaves the entry** (direct calls and tail calls). The
  26-bit displacement is cleared and replaced by *(target entry,
  offset into target)*.
  - Each such branch must also appear in the compiler's static call table
    (the `call` lines). Otherwise the entry is unlinkable, because data can
    decode as a branch.
  - A target outside every entry (a trampoline) makes the entry unlinkable.
- **Branches, ADR and literal loads inside the entry** are compared as raw
  bytes. Their relative displacement is layout-independent.
- **ADRP, or ADR / conditional branch / literal load leaving the entry**: the
  entry is unlinkable. None occurred in the tests.
- **Not normalized: pool indices, field offsets, cids, selector offsets,
  static field ids, thread offsets, instance sizes, tags, immediates.** These
  are bytes and must be equal. Normalizing pool indices would be unsound,
  because PP is shared: base code would read the wrong slot.

### 4.2 Link rule (greatest fixpoint)

Patch entry P links to release entry B iff all of the following hold:

1. **Pairing.** B has the same key as P (unique in both builds). If P's key
   is unknown or ambiguous, B is the unique release entry with the same
   normalized content.
2. **Self equality.** The normalized bytes are equal, and the outgoing
   branches are at the same offsets, of the same kind (BL or B), and at the
   same offset into their targets.
3. **Callees.** For every outgoing branch *k*, P's target is itself linked to
   B's target. This is computed as the greatest fixpoint: start from all
   candidates and remove until stable. Mutually recursive groups whose
   members are all identical therefore link together.

The analyzer also exports a *self hash* (FNV-64 of the normalized bytes and
branch shape). It computes a *subgraph hash* by hashing Tarjan SCCs in
callee-first order over self hashes and callee subgraph hashes. These are
diagnostics: in all 40 measured cases every linked pair also had equal
subgraph hashes (`subgraph_hash_agreements` = `linked_entries`). Pool constants, field-table and dispatch
references do not enter the hash. They are already bytes of the entry, and
their *values* are the patch's at run time by construction. Stubs and
type-testing stubs are ordinary entries. Inlining effects are inside the
bytes.

### 4.3 Safety argument

Release code only executes when the runtime enters it through the link table:

- a dispatch-table, pool, closure, `Function` or TTS entry point that the
  runtime rewrote from a patch `Code` to the release address of a linked
  entry, or
- a direct branch from other release code.

Consider a linked release entry B for patch entry P:

- **Instructions.** B's instructions equal P's, except that direct branches
  go to release entries linked to the corresponding patch entries (rule 3),
  at the same offset.
- **Data.** Every other datum the instructions use is read through
  registers that point at patch structures (PP, THR, the dispatch table
  register, the class table via THR, the field table via THR), or is an
  immediate in the bytes. That includes pool slots, field offsets, cids,
  selector offsets, static ids and stub entry points.
- **Metadata.** Stack maps, exception handlers, pc descriptors and catch
  moves of P apply to B unchanged, because pc offsets within the payload are
  identical. The runtime uses P's metadata for B's frames (RUNTIME.md §6.1).

Executing B is therefore executing P. P is the new program's own optimized
compilation, so it is correct under the new program's TFA facts.

**Native code never reaches an unresolved or changed target:**

- **Direct branches** reach only linked entries (rule 3). The checker proves
  this from the binaries.
- **Indirect transfers** (dispatch table, switchable calls, closures, DD
  slots, TTS, stub calls via pool or THR) take their target from patch
  tables. The runtime contract is that every entry point visible to native
  code is a release address of a linked entry or a gate into the simulator.

Data that changed in the patch, such as a different string in a slot, is
consumed by identical instructions, exactly as the patch would consume it.

### 4.4 TFA's whole-program effects

TFA (devirtualization, unboxing and signature specialization,
call-site/receiver facts, tree shaking, selector numbering) is not modelled.
The patch is compiled with the new program's TFA. Any entry whose code
depends on a changed fact compiles differently and fails rule 2; its callers
follow by rule 3. Examples:

- **Devirtualization.** If the patch devirtualizes differently (a new
  override exists), the call sequence differs.
- **Unboxing.** If the patch unboxes a parameter that the release passed
  boxed, the caller and callee bytes both differ.
- **Tree shaking.** A removed function leaves no patch entry to pair with;
  its callers' code changed anyway.
- **Distant effects.** In f4, adding a use of `DateTime.now()` changed TFA
  facts for `DateTime`, and `DateTime.toIso8601String` stopped linking
  (880 B). That is the intended conservative behaviour.

Base code that would be wrong under the new program can never link: it would
have to equal code compiled from the new program. Selector ids that TFA
renumbers are irrelevant, because selectors are matched by key.

## 5. Tools and flags

| gen_snapshot flag | Use |
| --- | --- |
| `--print_link_info_to=F` | release and patch: write link info (no effect on output bytes) |
| `--link_base=F` | patch: constrain layouts to F (release `App.link` or hinted `constraints.link`) |
| `--link_dd_targets=F` | release and patches: DD-call the listed functions |
| `--trace_link_info` | print facts that could not be kept |

| `ios_linker` command | Use |
| --- | --- |
| `pool-hints --base-link --patch-link PASS1 --out F` | second-pass pool assignment |
| `link --base-elf --base-link --patch-elf --patch-link --out out.vmcode [--report --table]` | link table + vmcode. Exit 2 if a VM stub does not link (toolchain mismatch; RUNTIME.md §8.2). |
| `check --vmcode --base-elf --patch-elf [--base-link --patch-link]` | independent verification (section 8) |
| `dd-select --elf --link --max-bytes N --out F` | release DD target selection (section 7) |
| `diff-base --elf --out F` | release data+text blob for bidiff |

Drivers:

- `tools/ios_linker/scripts/run_matrix.py [--wide] [--dd]`: the controlled
  fixture.
- `run_flutter_app.py [--dd]`: the real app.
- `run_runtime_test.py`: executes constrained patches.

## 6. vmcode format (contract with the runtime)

All integers are little-endian.

| Offset | Type | Field |
| --- | --- | --- |
| 0 | u8[8] | magic `O S B V M C \0 \x01` |
| 8 | u32 | version = 1 |
| 12 | u32 | header bytes = ELF offset |
| 16 | u64 | ELF offset: a multiple of 16384 (iOS page), so the ELF can be mapped in place |
| 24 | u64 | ELF size |
| 32 | u32 | link count *n* |
| 36 | u32 | link entry size = 16 |
| 40 | u32 | links offset = 128 |
| 44 | u32 | flags (0) |
| 48 | u8[32] | SHA-256 of the release text image (`_kDartSnapshotText` bytes); the runtime must refuse a patch whose release differs |
| 80 | u8[32] | SHA-256 of the embedded ELF |
| 112 | u32 | release text image size |
| 116 | u32 | patch text image size |
| 120 | u32 | linked code bytes |
| 124 | u32 | patch code bytes |
| 128 | *n* × {u32 patch_offset, u32 release_offset, u32 size, u32 flags} | sorted by `patch_offset`, non-overlapping |
| … | zeros | up to the ELF offset |
| ELF offset | bytes | the patch ELF, byte-for-byte as written by gen_snapshot |

- **Offsets** are payload starts relative to the start of each snapshot's
  instructions image symbol (`_kDartSnapshotText`; older SDKs use
  `_kDartIsolateSnapshotInstructions`).
- **Coverage.** There is one record per *linked* entry. Every patch
  instructions entry not covered by a record is unlinked. Sizes are equal by
  construction and repeated for verification.
- **Entry points.** Monomorphic and unchecked entry offsets are equal inside
  a linked payload, so `release_offset + (patch_entry - patch_offset)` maps
  any entry point or return address.
- **Stubs.** Entries cover stubs, allocation stubs and type-testing stubs as
  well as functions. All `stub:` (VM) stubs are guaranteed linked, or `link`
  fails.
- **Header size.** The header size grows with *n*: 16 bytes per link, so
  about 88 KB for the 5,498-entry Flutter app, padded to 96 KB. Upstream's
  measured header was a count plus pairs. This format adds sizes, hashes and
  a version for fail-closed loading.

## 7. DD selection, cost model, and when a release must add DD

When f changes, every entry reaching f by direct branches unlinks. That is
f's *transitive caller tree*. A DD slot for f cuts the edges into f.

`dd-select` does the following:

1. Build the release call graph from an analysis compile (BL edges, from the
   same analyzer).
2. For every eligible f, compute the caller-tree bytes. Eligible means a
   plain function that is a target of direct calls; stubs, closures and
   tear-offs are excluded.
3. Visit candidates in decreasing order. Select f if its caller tree, with
   the incoming edges of already-selected targets cut, still exceeds
   `--max-bytes`.

The order matters. In the 320-caller fixture, `sharedLeaf` (26,620 B tree)
was taken first. `Model.evaluate` (26,568 B, still above the threshold after
the cut) was taken too, so a `sharedLeaf` edit leaves only `sharedLeaf`
unlinked.

**Cost per selected f:**

- Each call site grows from one instruction to three: about +8 B of code,
  plus one 8-byte pool slot per target.
- At run time each call does an extra dependent load and an indirect branch,
  with possible BTB pressure.
- The callee is entered at its checked entry, so redundant parameter type
  checks run when the caller had proved them.
- f is never inlined.

These costs are unmeasured on device. The hot-path impact must be measured
before DD is used broadly.

**Benefit:** when f changes, the caller tree it shields keeps running
natively.

A release *must* add DD ahead of time: slots cannot be added by a patch,
because the release's callers would have to change. The questions are
therefore "which functions are likely to change and have large caller
trees", and "how much release size and call cost is acceptable".

Measured trade-off:

| Release | Targets / call sites | Code bytes vs stock | ELF vs stock | Shared-helper patch |
| --- | ---: | ---: | ---: | --- |
| 80 callers | 66 / 893 | +5,360 (+1.49%) | +16,384 (page) | 97.93% → 97.96% |
| 320 callers | 68 / 1,214 | +7,928 (+2.09%) | +0 (page slack) | 92.97% → **99.99%** |
| Flutter app | 567 / 4,356 | +19,728 (+1.62%) | +16,384 | 99.50% → 99.53% (f1) |

With the 10,000-byte threshold taken from the public CLI, most selected
targets are SDK and framework internals with enormous caller trees, such as
`_StringBase._concatRangeNative` and `_GrowableList._allocateData`. Those
functions never change in an app patch, so their DD cost buys nothing.

**Recommendation (product decision):**

- Do not enable DD by default.
- If enabled, restrict candidates to the app's own libraries (or
  user-listed packages) and use a larger threshold.
- Later, rank candidates by *P(change) × caller bytes × hotness*, with the
  hotness supplied by a profile.

Restricting candidates is a one-line filter in `dd_select.dart` and is listed
as remaining work.

## 8. Verification

1. **Independent checker** (`checker.dart`). It does not use the linker's
   analysis or the compiler's records. For every linked range it decodes both
   binaries' words and requires identical words, except B/BL whose targets
   leave the range. Such a branch must hit another linked range at the
   corresponding release offset. Any other PC-relative instruction leaving
   the range fails.

   It also verifies:
   - the header and padding,
   - that the embedded ELF equals the patch ELF,
   - both hashes,
   - sorted, non-overlapping ranges,
   - that ranges are whole entries.

   **All 43 vmcode files of the final suite passed with 0 failures** (40
   matrix/app cases + 3 runtime pairs; for example 304,300 words and 24,176
   external branches on the Flutter app).
2. **Runtime execution of constrained patches** (`run_runtime_test.py`,
   `tools/ios_linker/test/runtime/v{1,2}.dart`). This exercises class id
   holes, superclass field additions with subclasses, unboxed doubles, new
   subclasses on dispatch rows, new statics, lazy statics, closures, switch
   patterns, exceptions, and collections. The release, a stock build of
   the patch source, and the two-pass constrained patch build run on
   `dartaotruntime`. **Outputs match** for v1→v2 (88.9% linked), v1→v1 (100%)
   and v2→v1 (98.65%).

   This validates the compiler side. Native execution of linked release code
   is the runtime worker's part.
3. **Release unchanged.** `--print_link_info_to` releases are byte-identical
   to stock ELFs for all three programs.

## 9. Measurements

Host gen_snapshot `ProductARM64` from this branch, product, arm64, no
compressed pointers, `--deterministic --strip`, ELF. Kernels:

- Fixture: engine `gen_kernel_aot`, `--aot --target=flutter --target-os=ios`,
  Flutter product platform.
- App: engine `frontend_server_aot`, `--aot --tfa`, product, iOS.

Reuse is code-byte weighted: linked payload bytes / patch payload bytes.
Download is the updater `patch` (bidiff + zstd) of `out.vmcode` against the
release data+text blob. Raw outputs are under `.agents/ios-linker-20261104/`
(`matrix/*/summary.json`, `flutter_app/*/summary.json`, `suite/step*.txt`).

### Fixture, 80 callers

Release ELF 738,264 bytes (identical to stock), 360,040 code bytes, diff
base 730,368. With DD: ELF 754,648, code 365,400.

| Change | Direct: reuse | Unlinked entries (bytes) | Download | DD: reuse | DD: unlinked | Upstream (direct) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| None | 100.00000% | 0 (0) | 13,949 | 100.00000% | 0 | 100.00000% |
| Displayed string | 100.00000% | 0 (0) | 15,722 | 100.00000% | 0 | 100.00000% |
| One caller's argument | 99.09232% | 8 (3,268) | 14,044 | 99.10345% | 8 | 99.15865% |
| Shared helper | 97.93023% | 89 (7,452) | 13,491 | 97.95840% | 89 | 98.16536% |
| Add a field and use it | 97.75880% | 91 (8,072) | 41,329 | 97.78729% | 91 | 98.00547% |
| Add a function | 99.73672% | 7 (948) | 38,160 | 99.73839% | 7 | 99.70944% |
| Remove a caller | 99.11435% | 7 (3,188) | 32,598 | 99.12515% | 7 | n/a |

vmcode: 771,032 bytes direct; 787,416 with DD.

### Fixture, 320 callers

Release ELF 803,800 bytes (identical to stock), 379,240 code bytes, diff
base 774,480. With DD: ELF 803,800, code 387,168, diff base 782,432.

| Change | Direct | Unlinked | Download | DD | DD unlinked | DD download |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| None | 100% | 0 | 15,250 | 100% | 0 | 14,959 |
| Displayed string | 100% | 0 | 16,931 | 100% | 0 | 16,655 |
| One caller's argument | 97.36595% | 8 (9,988) | 31,470 | 97.41571% | 8 (10,004) | 30,659 |
| Shared helper | 92.97226% | 329 (26,652) | 13,399 | **99.99173%** | **1 (32)** | 14,932 |
| Add a field | 92.81113% | 331 (27,272) | 41,354 | 97.24652% | 11 (10,664) | 41,244 |
| Add a function | 99.75004% | 7 (948) | 38,017 | 99.75310% | 7 (956) | 38,555 |
| Remove a caller | 97.38686% | 7 (9,908) | 31,398 | 97.43826% | 7 (9,916) | 30,551 |

Upstream, 320 callers, shared helper: 93.88595% direct, 99.98141% DD (2
unlinked entries). In the "one caller" and "add a field" cases most unlinked
bytes are `compute` itself (about 9.9 KB, calling 320 callers), which changed.

### Real Flutter app (`patch_perf_bench`, TFA)

Release ELF 2,311,128 bytes (identical to stock), 1,217,200 code bytes in
5,498 entries. Sidecar 3,603,306 bytes (323,243 with zstd -19). vmcode
2,409,432 bytes.

| Change | Reuse | Unlinked | Download | DD reuse | DD download |
| --- | ---: | ---: | ---: | ---: | ---: |
| None | 100.0000% | 0 | 42,351 | 100.0000% | 51,319 |
| Constant in `patchedKernel` | 99.5005% | 8 (6,080) | 42,228 | 99.5334% | 51,116 |
| UI string in a widget | 100.0000% | 0 | 48,359 | 100.0000% | 57,441 |
| Virtual method body (inlined into `patchedKernel`) | 99.5005% | 8 (6,080) | 42,475 | 99.5334% | 51,290 |
| Add a field and use it | 99.3037% | 14 (8,476) | 93,945 | 99.3387% | 111,294 |
| Widget padding constant | 100.0000% | 0 | 42,529 | 100.0000% | 51,500 |

Downloads are not comparable byte-for-byte with upstream's (83–113 KB).
Upstream diffs an unstripped iOS snapshot plus their header against its own
diff base. Ours uses stripped ELFs. Even for an identical patch the data
section differs: placeholder pool slots, a larger static table, and object
order. The header also scales with the link count.

## 10. Gaps

1. **iOS release binary format.** The tool reads ELF.
   - iOS releases are built as assembly and linked into a Mach-O
     `App.framework/App`. The text image layout is the same and offsets are
     relative to the same symbol, but the linker needs a Mach-O reader. The
     alternative is a release ELF from the same gen_snapshot run, which needs
     `--elf` and `--assembly` in one invocation or a determinism guarantee.
   - The diff base must likewise be the shipped blobs.
2. **The runtime has not consumed this format yet.** RUNTIME.md's spike uses
   identity linking. Integrating section 6 is its next step.
3. **The host build is not the iOS toolchain.** The engine's
   `gen_snapshot_arm64` (iOS) needs this branch rolled into the engine.
   Snapshot target features match (product, arm64, uncompressed pointers), but
   iOS-only codegen differences were not tested.
4. **Keys are heuristic.**
   - Collisions or instability only cost reuse, never correctness.
   - Known weak spots: closures keyed by relative token offset (an edit
     inside the parent moves later closures); `const:` keys depend on
     canonical hashes; function type parameters fall back to positional keys.
   - Obfuscation and `--split-debug-info` were not exercised. Keys are
     recorded before `Obfuscate()`, but obfuscated names would need the
     release's obfuscation map.
5. **Pool hints are positional per function.** They help when a function
   keeps its request sequence length. Larger edits fall back to appending.
6. **Dispatch rows** have no reserved cid windows. A new subclass that
   lengthens a popular row can force it to move.
7. **`--deterministic` and the data section.** Data is deterministic per
   input, but the patch's data differs from the release's even for identical
   input. This affects download size only.
8. **Instruction dedup.** gen_snapshot dedups identical instruction payloads.
   One payload is recorded under one canonical code key; others pair by
   content. That works, but the key of the canonical owner can vary.
9. **DD.**
   - Selection is not restricted to app code, and there is no hotness input.
   - Only arm64 codegen has the DD path; other architectures are unchanged.
   - The checked entry is always used.
10. **Sidecar size and format.** 3.6 MB of text for a small Flutter app. A
    binary or compressed form and dropping the `uses` lines (needed only for
    pool hints) are easy wins.
11. **Link-percentage gate.** No minimum-percentage policy exists yet.
    Upstream warns below 90% and fails below 20%.

## 11. Remaining work

1. **CLI (`shorebird/packages/shorebird_cli`).** On iOS release:
   - pass `--print_link_info_to` (and `--link_dd_targets` when enabled)
     through flutter_tools;
   - upload `App.link`, the DD target file, the kernel and toolchain identity
     as the release supplement.

   On iOS patch:
   - download the supplement;
   - run the two-pass patch build with `--link_base` (gen_snapshot already
     has the kernel, so the passes need no extra frontend run);
   - run `ios_linker link` and `check`, enforce a minimum link percentage,
     build the diff, and upload `vmcode`.

   `shorebird/packages/open_aot_patch_tools` is the natural home for the Dart
   tool.
2. **flutter_tools** (`packages/flutter_tools/lib/src/base/build.dart`
   `AOTSnapshotter`): add the flags, keep `App.link` next to the snapshot and
   out of the app bundle, and copy the release supplement back in at patch
   time.
3. **Release supplement contents.** Each file (`App.link`, `App.dd`,
   `app.dill`) is identified by SHA-256, together with the gen_snapshot
   version / SDK hash and the release text SHA-256 used in the vmcode header.
4. **Engine updater.** Verify the header hash against the installed
   `App.framework`, verify the ELF hash, then hand the table to the runtime
   (RUNTIME.md §9).
5. **Mach-O input** for `ios_linker` (gap 1), and an on-device run once the
   runtime consumes the table.
6. **DD policy** per section 7, and a device measurement of the per-call
   cost.

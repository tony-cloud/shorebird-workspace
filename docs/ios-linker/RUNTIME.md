# iOS mixed-mode runtime: executing a linked AOT patch

Status: design, host implementation (macOS arm64), engine integration and a first iPad run, 2026-11-04.
Owner of this doc: runtime worker. The compiler, linker and vmcode side lives
in `COMPILER_LINKER.md`; section 9 describes how the runtime consumes its
vmcode v1 output and where the two documents differ.

Code: `tony-cloud/dart-sdk` branch `tonycloud/ios-mixed-runtime-20261104`
(base `2513af5688a`, head `702d29f676c`; it merges the linker branch
`tonycloud/ios-aot-linker-20261104` at `86442b439ab`), worktree
`.agents/wt/runtime/dart-sdk`. The test program is in
`runtime/tests/mixed_mode/`; the repeatable test is
`tools/test_aot_mixed_mode.py` (section 10.5).

## 0. Verdict

**Go.** On the host, a patch AOT snapshot runs as follows:

- The patch's text is mapped **read-only**, so the CPU cannot execute any of it,
  just as on iOS.
- Every function whose machine code is unchanged runs from the release's
  signed native instructions.
- Changed functions and their direct callers run in the Dart VM's own ARM64
  simulator, compiled into the native runtime.

The spike passes these checks:

- calls in both directions;
- virtual (dispatch-table) calls and closure calls from native code into
  simulated code;
- exceptions crossing native and simulated frames in all three directions;
- 19 scavenges while simulated frames hold the only references to live
  objects;
- `async`/`await` in simulated code.

A stress mode that simulates all Dart code (94% of code bytes, everything
except the stubs) also passes. Linked code keeps native speed. Simulated code
is about 60–130× slower than native, which is in line with upstream's public
statements ("up to 100x", "~112x" for its first alpha).

**End to end with the real linker (section 10.5).** The runtime consumes the
linker's vmcode v1:

- `tools/ios_linker` builds the release and the patch, then runs link and
  check.
- The mixed runtime runs the vmcode against the stripped, signed Mach-O
  release. Its output equals a stock build of the patch.
- The runtime's linked and unlinked entry counts equal the linker's report.
- This holds for the template pair and all 14 cases of the 80/320-caller
  fixture, in Product (the iOS configuration: no compressed pointers),
  Release and Product with compressed pointers.

Every malformed or mismatched input falls back to the unpatched release
(19 cases, each checked for its reason).

**On the iPad (section 12).** The Flutter engine
(`tonycloud/ios-mixed-engine-20261104`) loads the vmcode with read-only patch
text, and a one-function patch's output is visible (`kernel_tag` 2). Linked
code stays at native speed (0.93–1.11× of V0). Simulated hot code runs 38–164×
slower, as on the host, and a corrupted patch boots the release. Frame metrics
are still open.

Correctness came out simpler than feared because simulated and native frames
share one machine stack. Two things decide whether this is shippable:

- **performance** of unlinked hot code, which is determined by link quality,
  i.e. the compiler worker's layout alignment and the DD indirection, and by
  simulator speed;
- **engine integration**.

## 1. Model

```mermaid
flowchart LR
  subgraph Signed["Signed, executable"]
    B[Release App text: linked code and stubs]
    G[Gates: 2-instruction trampolines in the engine binary]
    E[DartMixedSimEnter + simulator C++]
  end
  subgraph Data["Patch vmcode (read-only data)"]
    PD[Patch snapshot data: objects, pools, tables]
    PT[Patch text: read as data only]
  end
  B -- "call/return into unlinked code" --> G --> E
  E -- "branch to linked code or stub" --> B
  E -. "fetch and decode" .-> PT
  PD --> B
  PD --> E
```

- The VM deserializes **only the patch's** snapshot data: object store, class
  table, global object pool, dispatch table, and constants. The release's
  snapshot data is never deserialized; only its text is used.
- Every patch `Code` gets *native* entry points:
  - **linked**: the address of the identical code in the release text;
  - **unlinked**: a **gate**, a trampoline in the signed engine binary that is
    bound at load time to a patch pc.
- The CPU only ever branches to release text, engine text, or gates. The
  simulator fetches instructions from the patch text as data.

## 2. Which interpreter

**Recommendation: use the VM's own `simulator_arm64.cc`, extended for mixed
mode. Do not write a new interpreter now.**

Assessment of `runtime/vm/simulator_arm64.cc` at this revision:

- **It already builds into native arm64 binaries.** Upstream added
  `dart_force_simulator`, which defines `DART_INCLUDE_SIMULATOR` when host and
  target are both arm64, plus a runtime flag `--use_simulator`. It is used for
  the iOS 26 no-JIT debug fallback (`virtual_memory_posix.cc`:
  `FLAG_use_simulator = !can_jit`). That mode is all-or-nothing: every Dart
  call goes through `Simulator::Call` on a separate simulated stack, and
  runtime calls go through `Redirection` trampolines.
  - Mixed mode reuses the compile-time half (GN arg `dart_mixed_mode`, which
    defines `DART_INCLUDE_SIMULATOR`, `DART_MIXED_MODE` and `SIMULATOR_FFI`).
  - It sets `use_simulator` to default false. Every existing
    `FLAG_use_simulator` site then takes the native path.
- **It decodes everything AOT emits**, including LSE atomics
  (`DecodeAtomicMemory`) and SIMD, because Dart's simarm64 AOT CI runs on it.
  No missing instruction was hit, even when all of `dart:core`/`async`/
  `collection` ran under it (section 10.3).
- **It accesses host memory directly.** Its "memory" is the real heap. The
  weak-memory buffering (`--sim_buffer_memory`) is off by default. Unaligned
  accesses are allowed. Exclusive and atomic operations are implemented with
  real atomics, so simulated and native threads can share objects.
- **What mixed mode had to add** is small (section 10):
  - classify register branches and `bl`/`b` into "stay simulated" or "leave";
  - a `RunMixed(MixedRegs*)` entry and exit;
  - nothing else in the decoder.
- **Weakness: speed.** It is a decode-every-instruction switch interpreter:
  about 18–28 ns per simulated instruction on this host (section 11).

Alternatives considered:

| Option | Verdict |
| --- | --- |
| Custom pre-decoded / threaded ARM64 interpreter (decode once per instruction into a side table of handlers + operands) | Best long-term speed; typically 3–5× faster than a decode switch. Do it later behind the same `MixedBranch`/`RunMixed` interface; the rest of this design does not change. |
| Dart bytecode interpreter (our earlier route) | Rejected by the user: it needs a non-AOT release, which costs the 20× size and 1.5–3.9× slowdown. |
| Binary translation to anything executable | Impossible on iOS (no writable+executable memory). |
| Third-party ARM64 emulators (unicorn/QEMU, etc.) | Licensing (GPL) and integration cost; no benefit over the VM's own BSD simulator, which already understands Dart's ABI. No user decision is needed while we stay on the VM simulator. |

## 3. Execution model

### 3.1 One stack, flat simulator

- Simulated Dart frames live **on the ordinary thread stack**, interleaved with
  native Dart frames and linked by the same FP chain. The simulator's `SP`
  (R15) and `CSP` (R31) registers *are* the real values.
- The simulator's own C++ frames run on a separate per-thread **side stack**
  (512 KiB, `MixedMode::NewThreadArea`). Dart frames that simulated code
  pushes therefore never collide with the C++ that simulates them.
- The simulator is **flat**: it runs only while unlinked code executes and
  *returns to the CPU* for every branch to native code. Runtime calls,
  allocation stubs, write-barrier stubs, and type-testing stubs are all native
  code, so the simulator exits for them. Consequences:
  - The simulator never nests, and it holds no Dart state between activations;
    everything is in registers saved by the gate or on the shared stack.
  - Whenever the VM runs C++ (GC, safepoint, exception dispatch, runtime
    entries), there is no simulator activation at all. The stack is an
    ordinary stack whose only peculiarity is that some return addresses are
    gate addresses (section 6).
  - There is no `Redirection`/`hlt` machinery. C functions (leaf runtime calls,
    FFI) are reached natively through the same exit path.

### 3.2 Gates

- A gate is two instructions in the engine's signed `__TEXT`:
  `adr x16, . ; b DartMixedSimEnter`. The spike has 131072 of them (1 MiB),
  in `vm/mixed_mode_gates_arm64.S`.
- Each gate is bound once to a patch pc (`gate_target_[i]`); a lock-free hash
  map goes the other way (patch pc → gate).
- `x16` (IP0) is dead at every call and return boundary in both the Dart and
  the C ABI.

`DartMixedSimEnter`:

1. Loads `Thread::mixed_area_` via THR (x26).
2. Stores x0–x30, CSP and q0–q31 into the area.
3. Switches CSP to the side stack and calls `DartMixedRun(area)`.
4. `Simulator::RunMixed` loads the registers into the simulator, starts at
   `gate_target[x16]`, and runs until a branch leaves simulated code.
5. It stores the simulated state back into the area and returns the native
   target.
6. The assembly reloads every register (CSP included) and `br`s to the
   target. The area is heap memory, so reading it after `sp` moves is safe.

Flags (NZCV) are not carried; they are dead at every boundary where gates are
entered.

### 3.3 Branch classification inside the simulator

`Simulator::MixedBranch` handles `b`, `bl`, `br`, `blr` and `ret`. Conditional
branches never leave a function in Dart code.

1. A target inside the current unlinked entry: continue (cached
   `[sim_lo_, sim_hi_)`, so loops pay one compare).
2. A gate: continue at its bound patch pc.
3. A patch pc in an **unlinked** entry: continue simulating.
4. A patch pc in a **linked** entry, i.e. a `bl` from unlinked code to an
   unchanged callee: leave, at the *release copy*. Hot unchanged callees run
   natively even when called from simulated code.
5. Anything else (stubs, base code, C functions): leave.

**Return-address invariant.** Every return address the simulator produces is
CPU-executable. For `bl`/`blr`, LR is set to the *return gate* for the next
pc, even for simulated → simulated calls. On a non-linking exit (a tail call),
an LR that still holds a patch pc is converted to its gate. Frames therefore
never contain patch pcs.

The spike found that this invariant is necessary, not an optimization. The
first version kept patch pcs for simulated → simulated calls, and the CPU then
jumped into the read-only patch text in this case:

- the async `Suspend` stub returns, on behalf of the suspended function, to
  that function's caller;
- that caller was simulated, so the return address was a patch pc.

Any native code that unwinds or returns for a simulated frame has the same
problem. The gate rule removes the whole class of bug.

## 4. Native → simulated transitions (every way native code reaches code)

The guarantee comes from linking plus entry-point rewriting.

| Path | Mechanism |
| --- | --- |
| Direct `bl` (PC-relative) in release code | Never reaches unlinked code: the link rule (section 9) unlinks every entry with a PC-relative branch/`adr`/`adrp` into unlinked code, transitively (the caller cascade). |
| Dispatch-table call | The table is filled from `Code::EntryPointOf` during `ReadRoots`, after `ReadInstructions` has rewritten the entries, so unlinked targets hold gates. Verified: `sumAreas` (native) → `Circ.area` (simulated). |
| Switchable / monomorphic / IC calls | The miss handler writes `Code::MonomorphicEntryPoint()` (a gate or a release address) into the global pool. The initial `SwitchableCallMiss` entry is a linked stub. |
| Closures | `Function::entry_point_` and `Closure::entry_point_` caches are copied from `Code` in `PostLoad`, i.e. after rewriting. Verified with a tear-off and a lambda. |
| Type-testing stubs | `UpdateTypeTestingStubEntryPoint` reads the rewritten `Code` fields. |
| `InvokeDartCode` (C++ → Dart), FFI callbacks | C++ calls the *stub*, which must be linked: gates assume THR is in x26. VM stubs are identical for a given SDK build, and the linker must refuse a patch whose entry stubs differ. A simulate-everything mode that also unlinks stubs crashes here, as expected. |
| Exception handler resume | `JumpToExceptionHandler` stores the handler pc in `thread->resume_pc`, and `RunExceptionHandler` branches to it natively. Both it and `Exceptions::JumpToFrame` pass the pc through `MixedMode::ToNativePc` (release address or gate). |
| Async resume | The Suspend stub stores its raw (native) return address, a gate if the suspended function is simulated. Resume jumps to it. `SuspendState::Clone` copies the raw field, not the translated accessor. |
| Return into simulated code | A return gate, by the return-address invariant. |

## 5. Registers, pools, object store

- **THR (x26), NULL (x22), HEAP_BITS (x28), DISPATCH_TABLE (x21)**: the same
  values in both modes. The simulator simply carries the real register values.
  A thread interrupt (a stack-limit write) is seen by simulated overflow checks
  because they read THR memory.
- **PP (x27)**: the **patch's** global object pool, used by both linked
  release code and simulated code.
  - Linked release code is correct only because the compiler side keeps
    every pool index (and class id, field offset and dispatch offset) that the
    unchanged code uses at the release's value.
  - The *contents* of pool slots may differ. Upstream's "changed string, 100%
    linked" relies on exactly this. The bytes are identical; the object loaded
    is the patch's.
- **CODE_REG**: unused in bare-instructions AOT.
- **Dart SP (R15) and CSP**: real stack addresses (section 3.1). The CSP
  convention (`saved_stack_limit - 4096` in Dart code, SP-aligned across C
  calls) is preserved because the simulator runs the same instructions.
- **Object store and constants**: from the patch only. The release's snapshot
  data is not loaded. Only the release *text* must stay mapped; on iOS it is
  `App.framework/App`, which the engine has loaded anyway.

## 6. GC, stack walking, exceptions, safepoints, async, overflow, profiler

### 6.1 One rule: describe every frame by its patch pc

All metadata (stack maps, pc descriptors, exception tables, catch moves) is
keyed by offsets into the *patch's* instructions. `MixedMode::ToPatchPc` maps:

- a gate to its bound patch pc;
- a release-text pc to the patch pc at the same offset. In general this goes
  through the link records (per-entry deltas, section 9); identity linking
  (testing) uses deltas as well.

It is applied in:

- `StackFrame::GetCallerPc`;
- the explicit-pc `StackFrameIterator` constructor;
- the suspended-frame visitor (`UntaggedSuspendState`);
- `SuspendState::pc()`;
- `Code::PayloadStartOf`, so `pc - PayloadStart()` offsets are consistent.

Each is two range compares in a mixed build; non-mixed builds are unchanged.

### 6.2 Each subsystem

- **GC.** Safepoints happen only inside native stubs or the runtime, so there
  is no simulator state to scan. Stack maps of simulated frames are applied
  like any other frame's, because the frames are ordinary frames.
  - Verified: `changedGc` keeps 200 lists referenced only from its simulated
    frame and allocates both natively and while simulated. 19 moving
    scavenges ran during the test, then every list was verified.
  - The global pool and objects in the snapshot image are immovable.
- **Exceptions.** The handler search runs on translated pcs, and the handler
  is resumed through `ToNativePc`. Verified in all directions:
  - thrown in a native frame, caught in a simulated frame;
  - thrown in a simulated frame, caught in a native frame;
  - simulated → native → simulated throw, caught in the outer simulated frame.
- **Async.** Section 4. Verified with an `await` loop in a simulated function
  called from a simulated, async `main`.
- **Stack overflow.** One stack, so the normal `stack_limit` checks apply to
  simulated frames. The side stack is separate and bounded: the simulator does
  not recurse, and signal handlers run on it while simulating.
- **Profiler.** With `--profiler --profile_period=100` the spike runs
  correctly. Samples taken while simulating land in C++ simulator frames and
  are not attributed to Dart.
  - Production needs the sampler to detect "in simulator" (a flag in
    `MixedRegs`) and walk from the simulated FP/PC.
  - Dart frames below are already walkable via `top_exit_frame_info`.
- **Deoptimization.** Not applicable in AOT.
- **Debugger / service.** No breakpoints in AOT release builds.

## 7. Loading

`Deserializer` (`app_snapshot.cc`):

1. When the instructions table of the registered patch image is created
   (start pc, end pc and all entry offsets are known before any `Code` is
   read), `MixedMode::LinkImage` decides, for every entry, whether it is
   linked:
   - from the vmcode's link records (section 9), each verified against the
     code;
   - or, as a testing aid, by identity for a plain patch ELF.

   This runs before any cluster is allocated. If linking fails, the
   deserializer stops with "mixed-mode patch rejected: …" and the embedder
   creates the isolate group again from the release.
2. `ReadInstructions` rewrites `entry_point_`, `unchecked_entry_point_`,
   `monomorphic_entry_point_` and `monomorphic_unchecked_entry_point_`:
   - **linked**: release address;
   - **unlinked**: a gate per distinct address.

   Every later copy then inherits native addresses: the dispatch table
   (`ReadRoots`), `Function`/`Closure` entry caches and TTS entry points
   (`PostLoad`), and pool entries (runtime).
3. `InstructionsTable` stays in patch space, and `PayloadStartOf` translates,
   so `ReversePc`, stack maps and handler lookup are unchanged.

Embedder (host: `bin/main_impl.cc`, `MixedModeLoad`; production: the engine):

- Load the release normally (dlopen of the signed Mach-O).
- Read the patch. For a vmcode container, `ParseVmcode`:
  - validates the header and the zero padding;
  - checks the SHA-256 of the ELF;
  - checks the SHA-256 of the loaded release `_kDartSnapshotText` region,
    `release text image size` bytes long, against header offset 48.
- Map the patch ELF at the header's ELF offset, with its executable segment
  **read-only** (`elf_loader_map_text_read_only`).
- Call `Dart_MixedModeRegisterImages(patch_text, release_text, links,
  link_count, &error)` before the isolate group is created. It validates the
  records structurally.
- On any failure, run the release unpatched
  ("mixed: patch rejected (reason); running the release").

A mixed build must keep native `dlopen` loading. `snapshot_utils.cc` disables
it under `DART_INCLUDE_SIMULATOR` and maps the Mach-O by hand, which hardened
runtime kills (SIGKILL, no crash report). The spike fixes the guard for
`DART_MIXED_MODE`.

## 8. Soundness checklist

1. **Link rule.** An entry may be linked only if its bytes equal the release
   copy *and* every PC-relative reference (`b`, `bl`, `adr`, `adrp`) leaving it
   lands in a linked entry, iterated to a fixed point.
   - Identical bytes alone are not enough: `bl` targets the release's stale
     callee. The spike implements this in `LinkImage`.
   - The compiler-side linker owns the proof that layouts (cids, field offsets,
     pool indices, dispatch offsets, BSS/relocations) agree.
2. **Stubs that C++ enters** (`InvokeDartCode`, FFI callback trampolines,
   `RunExceptionHandler`, `JumpToFrame`, Suspend/Resume) must be linked. VM
   stubs only change with the SDK, so the linker should hard-fail otherwise.
3. **Gate capacity** is fixed at engine build.
   - Need: about 1–4 per unlinked entry, plus one per call site in unlinked
     code that executes, plus handler pcs.
   - Measured: 117 gates for the targeted patch, 3951 when simulating all Dart
     code.
   - Policy: 64–128K gates (0.5–1 MiB of text; the host build has 131072).
     The runtime refuses a patch when 4 × unlinked entries + call sites in
     unlinked code + 1024 exceeds the gate count, so it never runs out
     later. The refusal falls back to the release.
4. **One patch per process.** Gates are process-global. Isolate groups
   spawned from the same snapshot share them; a different snapshot never
   matches `IsPatchImage`.
5. **Integrity.** The simulator executes patch bytes. The engine must verify
   the vmcode hash/signature before mapping, as the updater already does for
   downloads. No writable+executable memory is created anywhere; all
   executable code is signed engine or App text.
6. **Thread safety.** Gate allocation is mutex-protected with lock-free reads.
   Each Thread has its own area, side stack and simulator.
7. **App Store policy** (guideline 2.5.2, interpreted code) is the same
   question upstream already faces with the same technique. This is not a
   technical blocker, and I am not giving legal advice.

## 9. Contract with the linker: consuming vmcode v1

The container is the one in `COMPILER_LINKER.md` section 6: a 128-byte
header, records `{patch_offset, release_offset, size, flags}` sorted by patch
offset (one per *linked* entry), zero padding, and the patch ELF at a
16 KiB-aligned offset.

What the runtime checks, all failing closed to the release:

| Where | Check | Reason printed |
| --- | --- | --- |
| Embedder | Magic `OSBVMC\0\x01`, length ≥ 128 | `bad vmcode magic` |
| Embedder | Version 1 | `unsupported vmcode version` |
| Embedder | Header fields: ELF offset (twice), alignment, entry size 16, links offset 128, flags 0, table ≤ ELF offset, ELF size | `malformed vmcode header` |
| Embedder | Padding all zero | `malformed vmcode padding` |
| Embedder | SHA-256 of the ELF (offset 80) | `vmcode ELF hash mismatch` |
| Embedder | SHA-256 of the loaded release text (offset 48) | `patch was linked against a different release` |
| `RegisterImages` | Records: flags 0, 4-byte aligned, sorted and disjoint, inside both images | `malformed link record`, `link records are not sorted or overlap`, `link record outside the patch or release image` |
| `RegisterImages` | A release image is mapped when there are records | `patch links release code but no release image is mapped` |
| `LinkImage` | Each record starts a patch instructions entry | `link record does not start a patch code entry` |
| `LinkImage` | Its size is the entry's payload; only `brk #0` alignment padding may follow | `link record size differs from the patch code entry` |
| `LinkImage` | Word-wise verification (below) | `linked code does not match the release copy` |
| `LinkImage` | Gate budget (section 8.3) | `patch needs more gates than the runtime provides` |
| After loading | `InvokeDartCode`, `JumpToFrame`, `RunExceptionHandler` and `FfiCallbackTrampoline` linked | `a stub entered from C++ is not linked to the release` |

**Word-wise verification.** The linker normalizes B/BL displacements, so the
patch and release bytes of a linked entry may differ in those words. For each
word of a linked entry the runtime requires one of:

- the words are equal and are not a pc-relative reference that leaves the
  entry;
- the word is a B or BL that leaves the entry, the release copy is the same
  kind of branch, and it reaches the release copy of a *linked* patch entry at
  the same offset.

Any other pc-relative reference leaving the entry (b.cond, cbz, tbz, ldr
literal, adr, adrp) rejects the patch. This re-checks the linker's rule 3 on
the device, with the bytes the CPU will execute. It cannot re-check layout
compatibility (pools, cids, field offsets), which is the linker's proof.

**Translation.** Linked entries have a per-entry delta (release − patch).
Release pcs map back to patch pcs through the linked release ranges, sorted
and binary-searched (`BaseToPatch`).

### 9.1 Notes against COMPILER_LINKER.md (root `558fe31`)

1. **Matches.** The record layout, the one-record-per-linked-entry coverage,
   payload offsets relative to `_kDartSnapshotText`, sizes equal to the
   payload, the VM-stub guarantee and the ADRP refusal all match. The runtime
   additionally refuses the patch itself when a C-entry stub is unlinked.
2. **Record size versus entry span.** A record's size is the payload. The
   instructions table only gives entry starts, so the runtime accepts a
   trailing gap of fewer than 16 bytes made of `brk #0` words (the last
   payload before the image end). It was found when the first real vmcode
   was rejected.
3. **Release hash.** Earlier, the linker hashed the ELF `_kDartSnapshotText`.
   The shipped Mach-O differs from the ELF in three `InstructionsSection`
   header words (`bss_offset`, `instructions_relocated_address`,
   `build_id_offset`; text offsets 0x50–0x67), so that hash never matched at
   runtime. The current linker (section 6.1) hashes the shipped Mach-O text,
   which is what the runtime hashes. Fixed upstream of this note.
4. **Mach-O `--base` input and symbols.** The linker bounds the text region
   by the next symbol in the section. gen_snapshot's direct
   `app-aot-macho-dylib` output keeps local function symbols even with
   `--strip`, so `link --base App.dylib` fails ("only 128 bytes before the
   next symbol"). Real App.frameworks are stripped by Xcode, and the test
   strips with `strip -x`. Using the sidecar size and ignoring local symbols
   would make the linker robust to unstripped input.
5. **Hashes cover the ELF and the release, not the header or records.** The
   runtime does not trust records: it validates them structurally and against
   the code (above). The engine should still verify a signature over the
   whole vmcode file before use.
6. **Host versus device.** iOS codegen differs from macOS in the write-barrier
   stubs (atomic memory is unsupported on iOS). Host results prove the
   mechanism, not iOS link percentages.

## 10. Spike

### 10.1 Changes (dart-sdk branch above)

| Area | Change |
| --- | --- |
| `vm/mixed_mode.{h,cc}` | Images, link decision, gates, pc translation, statistics, per-thread area |
| `vm/mixed_mode_gates_arm64.S` | Gates and `DartMixedSimEnter` |
| Simulator | `RunMixed`, `MixedBranch`, return-address invariant |
| VM hooks | Deserializer hooks; `PayloadStartOf`; stack-frame, suspend-state and resume pc translation; `Thread::mixed_area_` (appended after all generated-code fields, so offsets used by gen_snapshot are unchanged) |
| Build | GN `dart_mixed_mode` / `dart_mixed_area_offset` |
| Embedder | Read-only ELF text, release load, vmcode parsing and SHA-256 checks, `Dart_MixedModeRegisterImages`, retry with the release on rejection, native dlopen kept |
| Tests | `tools/test_aot_mixed_mode.py` (section 10.5) |

The spike's link table is computed in the runtime by identity: equal bytes at
equal image offsets plus the PC-relative closure. The base is the *same
program compiled with K=3* and the patch has K=5. Every edit is a same-size
immediate change, so both compiles share one layout. Production replaces this
with the compiler worker's table.

### 10.2 Commands (all exit 0 unless noted)

```
# Tree: .agents/wt/runtime/dart-sdk; SDK pinned to MacOSX14.0 (the 27.0 SDK's
# .tbd stubs are unreadable by this lld).
DART_GN_ARGS='mac_sdk_path="/Library/Developer/CommandLineTools/SDKs/MacOSX14.0.sdk"' \
  python3 tools/build.py -m release -a arm64 --no-rbe runtime_precompiled dart   # 131 s
ninja -C xcodebuild/ReleaseARM64 gen_snapshot dartaotruntime   # after VM edits (snapshot hash)
# xcodebuild/ReleaseMixedARM64/args.gn = ReleaseARM64 args + dart_mixed_mode = true,
#   dart_mixed_area_offset = 3904
gn gen xcodebuild/ReleaseMixedARM64 && ninja -C xcodebuild/ReleaseMixedARM64 dartaotruntime
# Spike dir .agents/wt/runtime/spike:
./gen.sh            # base: K=3 Mach-O dylib (signed); patch: K=5 ELF
dartaotruntime base.dylib                                   # native reference
DART_MIXED_BASE=$PWD/base.dylib DART_MIXED_VERBOSE=1 \
  ReleaseMixedARM64/dartaotruntime [--verbose_gc] patch.aot [n]
DART_MIXED_UNLINK_BELOW=0xad144 ...                         # stress: simulate all Dart code
```

### 10.3 Results

- **Link.** 3699 entries; 3685 identical; 6 cascade-unlinked; **20 unlinked
  (99.358% of code bytes linked)**. The unlinked set is exactly:
  - the changed functions, two implicit tear-off closures, `main`, and four
    `main` lambdas.
  - Earlier, `linkedCatcher`/`linkedPassThrough` were *also* unlinked: TFA
    saw a single closure target and turned `f(i)` into a direct
    `bl changedThrower`. This is the documented ripple effect, and the
    PC-relative cascade caught it correctly. The test now gives those
    helpers a second target.
- **Identical patch** (hello, same program as ELF patch vs Mach-O release):
  3650/3650 entries linked (100%), 0 transitions, correct output.
- **Functional (patch K=5): 11/11 PASS, exit 0.** The checks:
  - simulated leaf;
  - simulated → native calls;
  - dispatch-table call native → simulated;
  - closure tear-off and lambda native → simulated;
  - native helpers stay native;
  - exception native → simulated handler;
  - exception simulated → native handler;
  - simulated → native → simulated throw, caught in simulated code;
  - GC with simulated frames (19 scavenges, contents verified);
  - async/await in simulated code.
- **Patch text never executed by the CPU.** Before the `resume_pc` and
  return-gate fixes, the run died with `SIGBUS (BUS_ADRALN)` *at patch pcs*.
  That is the iOS-equivalent failure, and it shows the read-only mapping is
  enforced.
- **Stress** (`DART_MIXED_UNLINK_BELOW`): 3527/3699 entries simulated
  (5.9% linked: only stubs); 11/11 PASS; 18 scavenges; about 800K
  transitions; 3951 gates.
- **Profiler** (`--profiler --profile_period=100`): 11/11 PASS.

### 10.4 Performance

Host Apple Silicon. Release-mode runtimes, n = 20M, medians of 3 runs. The
native column runs the *release* natively (`ReleaseARM64/dartaotruntime
base.dylib`). The mixed column runs the patch (`ReleaseMixedARM64`).

| Benchmark | Native | Mixed | Ratio | Per op (mixed) |
| --- | ---: | ---: | ---: | ---: |
| `numLoopB` numeric loop, changed → simulated | 62.6 ms | 3601 ms | 57.6× | 180 ns/iter vs 3.1 |
| `numLoopN` same body, linked → native | 57.3 ms | 50.3 ms | 0.9× | native |
| `callLoopSimToSim` (5M calls, both simulated) | 6.2 ms | 698 ms | 113× | 140 ns/call |
| `callLoopSimToNative` (5M calls to a linked leaf) | 6.2 ms | 947 ms | 153× | 189 ns/iter |
| `callLoopNative` (linked caller, linked callee) | 10.3 ms | 7.1 ms | 0.7× | native |
| `applyN(changedLeaf)`: native loop, closure → simulated (5M) | 34.7 ms | 1052 ms | 30× | 210 ns/call |
| `applyN(tinyN)`: native → native closure | 26.8 ms | 20.3 ms | 0.8× | native |

What these numbers mean:

- **Linked code runs at native speed.** Ratios of 0.7–0.9× are noise: same
  instructions, same pool.
- **Simulated straight-line code costs about 18–28 ns per instruction**, the
  VM simulator's decode-every-instruction design.
- **A simulated → native → simulated round trip** costs about 50 ns over
  simulating the callee (189 vs 140 ns/iteration). A native → simulated → native
  round trip including simulating two tiny functions costs about 210 ns. Each
  costs a 63-register save/restore plus a hash lookup for the return gate.
- **Implication.** Hot code must stay linked. Performance work, in order:
  1. Link quality (compiler worker: layout alignment, DD indirection to cut
     cascades). This is by far the largest lever: a hot loop that stays
     linked is about 60–150× faster than the same loop simulated.
  2. Simulator speed: a pre-decoded or threaded interpreter, keeping the
     interface.
  3. Transition cost: save only the Dart-ABI-live registers (q8–q15 low halves
     are callee-saved; most q registers are dead at call boundaries). The
     return-gate and branch-target caches are done (section 10.6).

### 10.5 vmcode v1 end to end, fail-closed cases, configurations

`tools/test_aot_mixed_mode.py --release-dir <dir> --mixed-dir <mixed dir>
[--linker …/ios_linker.dart --dart <dart>] [--bench]` (macOS arm64). The
release dir provides gen_snapshot, dartaotruntime and vm_platform.dill.

**Part A** uses the template (release K=3, patch K=5, an unrelated release
K=4). It covers:

- the release run natively;
- the patch with identity linking (11/11 checks, with scavenges);
- the patch as a vmcode built from the runtime's own link records;
- the stress mode;
- 19 fail-closed cases. Each one must run the release (K=3, or K=4) and print
  the expected reason:
  - gate exhaustion;
  - truncated vmcode; bad magic; bad version; malformed header; nonzero
    padding;
  - ELF hash mismatch; a different release; a release hash mismatch;
  - record not at an entry start; record size mismatch; nonzero flags;
    overlapping records; record outside the release;
  - unmapped release (vmcode and identity);
  - C-entry stubs unlinked (no records, and identity "unlink all");
  - a record that links changed code.

**Part B** runs the linker at root `558fe31` (sidecar format 2) on each
release/patch pair:

1. Release: ELF and sidecar, plus a stripped, signed Mach-O dylib (the
   `--base`).
2. Patch: pass 1, `pool-hints`, constrained final build.
3. `link`, then `check`.
4. The mixed runtime runs the vmcode.

The test requires:

- the vmcode's release hash to equal the dylib text hash;
- output equal to a stock build of the patch;
- runtime linked/unlinked counts equal to `report.json`.

Pairs:

- the template (K=3 → K=5);
- a pure-Dart port of the 80- and 320-caller fixture, cases p0–p6
  (`run_matrix.py`'s fixture uses `dart:ui`).

`patch_perf_bench` is a Flutter app, so it is not practical on the host
runtime; it belongs with the device run.

Results: **38/38 PASS in each configuration**, exit 0.

| Configuration | Mixed runtime | Identity (template) | vmcode cases |
| --- | --- | --- | --- |
| Product, uncompressed (iOS configuration) | `ProductMixedARM64` (area offset 3864) | 1493 linked / 20 unlinked, 98.44% bytes | 15/15 |
| Release | `ReleaseMixedARM64` (3904) | 3679 / 20, 99.36% | 15/15 |
| Product, compressed pointers (robustness only) | `ProductMixedARM64C` (3872) | 1499 / 20, 98.45% | 15/15 |

Product (uncompressed) linker results. Runtime counts equal the report in
every row; output equals stock in every row.

| Pair / case | Linked | Unlinked | Linked bytes |
| --- | ---: | ---: | ---: |
| template K=3 → K=5 | 1493 | 20 | 98.44% |
| 80 callers p0 identical | 1541 | 0 | 100% |
| 80 p1 string | 1541 | 0 | 100% |
| 80 p2 one caller | 1537 | 4 | 99.15% |
| 80 p3 shared leaf | 1456 | 85 | 97.79% |
| 80 p4 field layout | 1456 | 85 | 97.79% |
| 80 p5 new function | 1539 | 3 | 99.90% |
| 80 p6 remove caller | 1537 | 3 | 99.18% |
| 320 callers p0 / p1 | 1781 | 0 | 100% |
| 320 p2 one caller | 1776 | 4 | 97.14% |
| 320 p3 shared leaf | 1456 | 325 | 92.03% |
| 320 p4 field layout | 1456 | 325 | 92.03% |
| 320 p5 new function | 1779 | 3 | 99.91% |
| 320 p6 remove caller | 1777 | 3 | 97.16% |

The Release configuration leaves 25 entries unlinked even for the identical
patch p0 (98.30%), against 0 in Product. That comes from the linker, and the
runtime agrees with it.

### 10.6 Simulator speed-ups

These are cheap and keep the decoder unchanged:

- a per-simulator branch-target cache (destination → simulated entry range,
  or native target);
- a return-gate cache;
- fast paths for `mov xd, xm` and 64-bit `ldr`/`str` with an unsigned
  offset.

All are guarded by a link generation counter. Product (compressed), median of
3, interleaved A/B runs:

| Benchmark | Before | After | Change |
| --- | ---: | ---: | ---: |
| `numLoopB` simulated arithmetic loop | 2976 ms | 2287 ms | −23% |
| `callLoopSimToNative` | 742 ms | 525 ms | −29% |
| `callLoopSimToSim` | 577 ms | 465 ms | −19% |
| `applyN(changedLeaf)` native → simulated closure | 1043 ms | 805 ms | −23% |

After these changes, the Product (uncompressed) mixed/native ratios are:

| Benchmark | Ratio |
| --- | ---: |
| `numLoopB` | 59.6× |
| `callLoopSimToNative` | 128× |
| `callLoopSimToSim` | 111× |
| `applyN(changedLeaf)` (native baseline 16.5 ms) | 37.5× |
| Linked code | 1.0× |

The profile is now spread over the bitfield, multiply, add/sub-shifted and
immediate decoders, so further fast paths give diminishing returns. The next
real step is a pre-decoded (or threaded) interpreter that caches decoded
instructions per entry. That is future work.

Side observation: with compressed pointers, the *native* closure-call loop
`applyN` takes 168 ms against 16.5 ms uncompressed. That is not a mixed-mode
cost, and iOS does not use compressed pointers.

## 11. Remaining work

### Runtime (dart-sdk)

Done:

- vmcode v1 consumption;
- fail-closed rejection;
- Product, Release and compressed configurations;
- the repeatable test;
- the first simulator speed-ups.

Next:

1. Make the profiler simulator-aware (section 6); sampling already runs
   correctly without it.
2. A pre-decoded or threaded interpreter, and leaner transitions (save only
   the Dart-ABI-live registers).
3. Deferred loading units: either refuse them in patches or link each unit,
   since `ReadInstructions` for deferred units goes through the same hook.
4. Turn `tools/test_aot_mixed_mode.py` into a CI step (it needs a macOS
   arm64 bot with the mixed and baseline builds).
5. Device: the iOS engine build with `dart_mixed_mode`, and a run of
   `patch_perf_bench` on the iPad (coordinator-scheduled).

### Engine (`flutter/engine/src/flutter`)

Done (section 12): the loader, the GN wiring, an ios_release build and the
first iPad run. Next:

1. Re-measure the frame metrics on the iPad (section 12.4).
2. Done (section 14): the signed path end to end on the iPad, through the
   CLI, the open-source server and the updater.
3. Make the profiler simulator-aware and report link and transition counts
   through FML logging on device.
4. Fix the iOS 27 SDK linking in the build itself. Local builds currently
   patch `toolchain.ninja` to use Xcode's ld.

### CLI / linker

1. "Hot unlinked" warnings. The link-percent gate and stub hard-fail exist
   (exit codes 3 and 2).
2. A gate-budget check at link time, mirroring section 8.3.
3. Accept unstripped Mach-O releases (section 9.1, note 4).

## 12. Engine integration and first device run (2026-11-04)

### 12.1 Engine changes

Branch `tony-cloud/flutter` `tonycloud/ios-mixed-engine-20261104` (commit
`c0e94d8e7eb`). Its DEPS pins dart-sdk `263d230e9b0`.

- **GN.**
  - `shorebird_mixed_mode` (`common/config.gni`, iOS only) defines
    `SHOREBIRD_MIXED_MODE`. It is used together with Dart's
    `dart_mixed_mode`.
  - The Dart side now adds its mixed defines only in the default (target)
    toolchain, so the ios_release gen_snapshot (host toolchain) is unchanged:
    `strings` finds no `DartMixed` symbol in it.
  - `shorebird_mixed_dev_patch`, for test engines only, enables the
    development hook described below.
- **Patch path.**
  - `ConfigureShorebird` (iOS) turns a `.vmcode` boot patch from the updater
    into `Settings::shorebird_mixed_patch_path`. It does not install an
    interpreter patch.
  - The updater has already verified the signed hash over the whole file,
    plus the header geometry, the ELF hash and the release text hash against
    the base stream.
  - The development hook reads
    `<home>/Library/Application Support/shorebird/mixed_dev_patch.vmcode`.
- **Loading.** `DartSnapshot::IsolateSnapshotFromSettings` calls
  `runtime/shorebird/mixed_patch.cc`, which:
  - validates the container;
  - checks the SHA-256 of the ELF and of the mapped release
    `_kDartSnapshotText` (`release text image size` bytes);
  - calls `Dart_LoadELF` with read-only text;
  - calls `Dart_MixedModeRegisterImages`, then `Dart_MixedModeLinkSnapshot`.

  All of this happens before the VM starts. Any failure logs
  "Shorebird mixed mode: patch … rejected (reason); running the release" and
  returns the release snapshot. The decision is made once per process. The
  VM snapshot stays the release's.
- **Dart API** (new public header `include/dart_mixed_mode_api.h`):
  - `Dart_MixedModeLinkSnapshot` reads the instructions table from the
    snapshot header and links before any isolate or heap exists. The
    deserializer then skips linking.
  - The C-entry stub check still runs at isolate creation. On iOS those stubs
    come from the release VM snapshot, so it passes by construction.
- **Gates.** They now read the `Thread::mixed_area_` offset from a variable,
  so `dart_mixed_area_offset` is unused and the same source works for every
  configuration.

### 12.2 Build

- Out dir: `.agents/builds/engine-ios-mixed-20261104/out/ios_release`. Its
  args are the closed-world args plus:
  - `dart_mixed_mode`, `shorebird_mixed_mode` and
    `shorebird_mixed_dev_patch` set to `true`;
  - `dart_version_git_info = false`.
- `dart_version_git_info = false` is needed because otherwise the VM's SDK
  hash is the git hash of the Dart checkout, which no longer matches kernels
  from the closed-world host frontend server (gen_snapshot fails with
  "ApiError").
- Sources: an APFS clone of `flutter/` at `.agents/wt/engine/flutter`. Its
  `third_party/dart` symlink resolves to the runtime dart-sdk worktree.
- The engine `__text` grows by 1.18 MB: 1 MiB of gates (131072) plus the
  simulator.

### 12.3 Patches

The app is `testapps/patch_perf_bench`, built with `flutter build ios
--release` and `--extra-gen-snapshot-options=--print_link_info_to`. The
patches go through the same pipeline as `tools/ios_linker` (pass 1,
pool-hints, constrained build, link with the Mach-O App as `--base`, check).

| Patch | Linked entries | Linked bytes |
| --- | --- | --- |
| Identical | 5498/5498 | 100% |
| `patchedKernel` tag | 5490/5498 | 99.50% |
| `Chip.weight` | 5496/5498 | 99.91% |

All three pass check. The vmcode file is 2.4 MB.

### 12.4 Device results

iPad Pro 13-inch (M4); full table in `docs/PATCH_PERFORMANCE.md`; dataset in
`docs/validation/ios-mixed-mode-device-20261104.json`.

- **The patch runs on iOS.** With its text never executable, the P1 runs
  report `kernel_tag` 2.
- **Linked code is native.** The identical patch, and every metric a patch
  does not touch, stay within 0.93–1.11× of V0.
- **Simulated hot code is 38–164× slower** (string building 7×).
  Transitions cost about 95–165 ns per call. This matches the host
  measurements (section 10).
- **A corrupted patch boots the release** ("vmcode ELF hash mismatch").
- **Base stream sizes agree.** `Dart_SnapshotDataSize` (1078368) and
  `Dart_SnapshotInstrSize` (1218736) equal the App's `__TEXT,__const` and
  `__TEXT,__text` sizes. So the engine's base stream for the updater has the
  same sizes as `ios_linker diff-base`.
- **Not measured: frames.** A local-network permission dialog of another app
  stayed on screen, so the bench app was never active.

## 13. Risks

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Hot code unlinked by cascades (TFA/inlining) → 30–150× slowdowns | High (user-visible jank) | DD indirection, layout alignment, CLI warnings; upstream reports about 9/10 patches unaffected |
| Simulator speed | Medium | Pre-decoded interpreter (3–5× expected) |
| Hidden native consumers of patch addresses, like `resume_pc`, the async `Suspend` return and `SuspendState::Clone` | Medium | Return-gate invariant, plus all entry fields rewritten at the source. Stress mode (all Dart code simulated) is the regression net |
| Layout drift between release and patch makes "identical bytes" use wrong pools or cids | High if wrong | Compiler-side proof plus link rule; runtime byte re-check in debug |
| Gate exhaustion | Low | Budget check when linking; fail closed (tested) |
| Malformed or mismatched vmcode | Low | Hashes plus structural and word-wise record checks; fail closed (19 tested cases) |
| App Store policy | Not technical | Same technique as upstream; product decision |
| Build fragility: `DART_INCLUDE_SIMULATOR` side effects (snapshot loader guard), hardened runtime, the 27.0 SDK `.tbd`/lld mismatch | Low | Guards fixed; SDK pinned |

| Hot loop moved into the simulator by a small edit to an inlined or hosting function (P1/P2 on device: 54–164×) | High | Link quality (DD indirection), CLI "hot unlinked" warnings, simulator speed |

Left running: nothing.

## 14. Signed path on the iPad, no dev hook (2026-11-04)

`shorebird release ios` and `shorebird patch ios` with
`aot_patch_runtime_mode: ios-aot-link`, published to the open-source server,
delivered by the device updater and booted by the engine. The engine is the
section 12 build with `shorebird_mixed_dev_patch = false` (the hook is not
compiled in; `strings Flutter | grep mixed_dev_patch` is empty). Dataset:
`docs/validation/ios-mixed-mode-signed-device-20261104.json`.

### 14.1 Setup

- CLI: `shorebird` `tonycloud/ios-aot-link-cli-20261104` compiled with the
  vended Dart, plus one fix (below); `SHOREBIRD_IOS_LINKER` names
  `tools/ios_linker/bin/ios_linker.dart` (2b0f4ff). Flutter = the engine
  branch (it carries the flutter_tools commits); engine passed as
  `--local-engine-src-path … --local-engine=ios_release
  --local-engine-host=host_release_arm64`.
- Server: shorebird-server 3479ee0, sqlite + local storage, managed signing
  key (`PATCH_SIGNING_MASTER_KEY`), `PATCH_DELIVERY_ENCRYPTION=off`.
  Publishing used the LAN URL; device traffic went through a cloudflared
  quick tunnel (the app's `base_url`; the server restarted with
  `SERVER_BASE_URL` = tunnel between publishing and launching, so download
  URLs are tunnel URLs and the iPad never needs local-network permission).
- shorebird.yaml: `patch_verification: strict`,
  `patch_response_verification: required`; the release embeds
  `patch_public_key`.
- Scripts: `.agents/signed-ios-e2e-20261104/{serve.py, serve_mode.sh,
  cycle.sh, launch_probe.sh, env.sh}`.

CLI fix (shorebird `66baabf8`, not pushed): `iosLinkerRef` was missing from
the root `runScoped` in `bin/shorebird.dart`, so `release ios` with the
profile failed in `IosReleaser.assertArgsAreValid` ("read(ScopedRef<IosLinker>)
was called in a scope which does not contain a corresponding value"). The host
e2e tests did not catch it because they build their own scope.

### 14.2 Results

| step | result |
| --- | --- |
| `release ios` | exit 0, release 1.0.0+1; supplement zip 7,291,184 B; text 1,218,736 B sha256 `5fa06684…`, data 1,078,336 B |
| V0 ×3 | installs and runs unpatched, `kernel_tag` 1; patch checks reach the server through the tunnel |
| P0 identical (patch 1) | link 100% (5498/5498); vmcode 2,409,432 B; download 41,242 B; installed, `last_booted_patch` 1 |
| P1 KERNEL_TAG 1→2 (patch 2) | link 99.50107% (8 unlinked, 6080 B, 1243 gates); download 41,477 B; `kernel_tag` 2 on the next launch |
| P2 Chip.weight 3→4 (patch 3) | link 99.91236% (2 unlinked, 1068 B); download 41,605 B; getter 160× (the inlining callers are simulated) |
| rollback patch 3 | launch 1 still runs patch 3 and uninstalls it; launch 2 runs the release and downloads patch 2; launch 3 boots patch 2 (`kernel_tag` 2) |
| rollback patches 2, 1 | next launch runs patch 2 and uninstalls it; the following one runs the release, nothing installed |
| tamper A (patch 4 object replaced by patch 2's valid diff) | inflates, signed hash differs: `Bad / InstallHashMismatch`; event `corrupt_patch`; release keeps running (`kernel_tag` 1 ×2) |
| tamper B (patch 5, one byte of the stored diff flipped) | zstd "Data corruption detected": `Bad / InvalidPatchBytes`; release keeps running (`kernel_tag` 1 ×2) |

Micro ratios match the dev-hook run (section 12.4): P0 0.93–1.01×; P1
7.5–178× on the simulated kernels, transitions 120–130×; P2 getter 160×,
everything else 0.98–1.01×. Full table in `docs/PATCH_PERFORMANCE.md`.

Download size: even the identical patch costs 41 KB. The vmcode is 2.4 MB, of
which 98,304 B are the header and link records (5498 × 16 B) that have no
counterpart in the release; bidiff+zstd against data+text reduces the whole
file to 1.7%. Real edits add a few hundred bytes.

### 14.3 Findings

1. CLI scope bug above (fixed on the CLI branch).
2. Updater: a corrupted diff (zstd checksum failure) is reported with
   `failure_category` `filesystem` instead of `corrupt_patch`; the message
   also says "Writing reconstructed patch …". The rejection itself is right.
3. Updater message for an install hash mismatch reads "the binary is different
   from the version … submitted to Shorebird", which points at the release
   rather than the patch.
4. Engine-level vmcode rejection (release text hash, ELF hash, geometry) was
   not reachable through the signed path without the signing key; it remains
   covered by the 19 host cases (section 10.5) and the dev-hook REJ run.
5. Frames were not measured: the ETS6 local-network dialog of another app was
   still on screen at the start and end of the run.

## 15. Pre-decoded interpreter for unlinked code (2026-11-04)

Code: `tony-cloud/dart-sdk` branch `tonycloud/ios-fast-interp-20261104`,
commit `0f4fcf3f694` on top of `263d230e9b0`
(`tonycloud/ios-mixed-runtime-20261104`). Worktree `.agents/wt/interp/dart-sdk`.
The core is a new file, `runtime/vm/simulator_arm64_mixed.cc`. Host
measurements only; the iPad run is scheduled separately.

It replaces the decode-every-instruction simulator loop for unlinked code
(section 10.6 left this as the next step). There is no JIT and no writable and
executable memory: decoded instructions are plain data in `calloc`'d tables,
and the only code that runs is the runtime's own handlers. So the App Store and
iOS constraints are unchanged.

### 15.1 Design

- **Lazy pre-decoding per entry.** On first use, an unlinked entry is decoded
  into one 32-byte `FastOp` per instruction (handler pointer plus operands),
  with an `H_End` op past its end.
  - The table is published with a release store; readers use acquire loads.
  - Decoding is serialized by a mutex. Tables are keyed by link generation.
- **Threaded dispatch.** Each handler ends in a `[[clang::musttail]]` call to
  the next op's handler. The interpreter state lives in argument registers:
  `ip`, the `MixedRegs` register file, and three lazy-flag words.
  Straight-line handlers have no frame. Slow paths (first decode of a branch
  target, link-register fix-ups, unresolved `br`/`blr`) are separate handlers
  reached by tail calls.
- **Register file is the thread's `MixedRegs` area** (no copy in or out).
  - Slot 32 reads as zero and slot 33 absorbs writes, so XZR needs no branch.
    `MixedRegs` is now 816 bytes: `v` at 272, `c_stack_top` at 784,
    `exit_target` at 792.
  - The old simulator's `registers_` and `vregisters_` point into the same
    area.
- **Lazy NZCV.** Flag-setting ops record their operands and kind
  (sub/add/logic, 32/64-bit). Conditions are evaluated only when read.
  `cmp`/`tst`/`subs`/`adds`/`ands` followed by `b.cond` is fused into one op;
  the `b.cond` keeps its own op for branches that land on it.
- **Superinstructions**, chosen from host profiles of the template, the
  benchmarks and stress mode:
  - specific: `mov`+`mov`, `mov`+`b`, `mov`+`uxtw`, `movz`+`mul`, `ldr`/`str`
    pairs, the stack-limit check (`ldr`+`cmp`+`b.ls`), and the 5-instruction
    write-barrier filter;
  - generic: pairs of any two of 20 straight-line bodies (400 handlers).
  `DART_MIXED_SUPER=0` turns them off.
- **Fallback.** Anything not covered (SIMD beyond moves and logic, atomics,
  exclusives, barriers, `adrp`, bitfield inserts, and others) runs through the
  generic decoder for that one instruction (`Simulator::MixedOldStep`).
  Control then returns to the threaded ops.
- **Branches.**
  - Direct branches inside an entry become op offsets.
  - Calls to other unlinked entries go through a per-call-site pointer to the
    callee's table.
  - `br`/`blr`/`ret` look up the per-gate op table.
  - Branches to native code exit with the native target. The return-gate
    invariant and `FixLinkRegister` are unchanged.
- **Leaner gates.** v0–v7 are always saved and reloaded. v8–v31 are skipped in
  two cases:
  - *Entry:* skipped when the gate's `DartMixedGateFlags` byte is set. The
    interpreter sets it after a conservative backward liveness analysis of the
    target entry finds that v8–v31 cannot be read. Any FP/SIMD instruction
    counts as reading all of them, as does any exit from the entry, `br`, or
    fall-through.
  - *Exit:* skipped when the exiting entry contains no FP/SIMD instruction or
    load whose register field is 8 or higher. The skip is signalled in bit 0
    of `DartMixedRun`'s result.
  - Why this is sound: the Dart ABI passes and returns FP values only in
    v0–v7, and no FPU value lives across a Dart call. So any v8–v31 value that
    native code reads after an exit must have been written by the exiting
    function itself. That covers a value live across a register-preserving
    stub, and the d8–d15 that an FFI callback's epilogue restores. The
    "V-free function, skip all V" variant was rejected: register calls pass
    doubles in v0–v5.
- **Verification mode** (`DART_MIXED_INTERP=verify`). Every op is replayed with
  the generic decoder from the same state. It compares x0–x30, SP, v0–v31,
  NZCV, the next pc or exit target, and the memory written by fused stores.
  - When a gate skipped saving v8–v31, they are poisoned before simulating.
  - When an exit skips reloading them, they are reloaded as poison instead.
  - So an unsound liveness result shows up as wrong program output.
  - `DART_MIXED_PROFILE_OUT=<file>` writes per-pc execution counts.
  - `DART_MIXED_INTERP=simulator` selects the old loop.

### 15.2 Validation

| Command (from the worktree) | Result |
| --- | --- |
| `tools/test_aot_mixed_mode.py --release-dir xcodebuild/ProductARM64 --mixed-dir xcodebuild/ProductMixedARM64 --linker <ios_linker 2b0f4ff> --dart tools/sdks/dart-sdk/bin/dart` | exit 0, **38/38 PASS**, `ALL PASSED` |
| same with `ReleaseARM64` / `ReleaseMixedARM64` | exit 0, **38/38 PASS**, `ALL PASSED` |
| Mode checks: one patch run in 9 runtime modes, each compared line by line with the default run (11 program checks: GC with simulated frames, exceptions across sim/native frames, async/await) | Product: default, stress, verify, verify+stress, nosuper+stress and simulator+stress all PASS. Release: those plus `--profiler --profile_period=100` PASS |
| Verify, stress (all Dart code but stubs simulated, Release) | 27,009,060 ops / 43,528,746 instructions, **0 mismatches** |

The matrices use a frozen copy of the root linker (`git archive 2b0f4ff
tools/ios_linker`). The test template gained the four device-style micros
(below), so a template patch now has 28 unlinked entries.

`tools/test_aot_mixed_mode.py` also gets a fix: the shifted and resized
fail-closed cases now pick a link record of at least 32 bytes. Before the fix,
the middle record was 8 bytes for the new template, and the script crashed.

**Pre-existing failure, not addressed:** `--profiler` combined with stress
mode crashes the sampler in `ProfilerDartStackWalker::walk` (SIGSEGV at a
small address). The `263d230e9b0` runtime crashes the same way, with the
same patch and flags. Section 6 already lists "make the profiler
simulator-aware" as open work. The profiler without stress passes.

### 15.3 Performance (host, M3 Max)

n = 20M, interleaved runs (native, before, after), median of 5. Product,
uncompressed. *Before* is `263d230e9b0` built in the same configuration;
*after* is `0f4fcf3f694`. The native column runs the release natively.

| Benchmark | Native | Before | Ratio | After | Ratio | After/before |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `numLoopB` numeric loop, simulated | 37.7 ms | 2203.5 ms | 58.5× | 167.5 ms | **4.4×** | 0.076 |
| `callLoopSimToNative` | 4.1 ms | 518.0 ms | 127× | 82.9 ms | **20.4×** | 0.160 |
| `callLoopSimToSim` | 5.3 ms | 456.0 ms | 85.4× | 38.9 ms | **7.3×** | 0.085 |
| `applyN(changedLeaf)` native → simulated closure | 16.2 ms | 605.8 ms | 37.5× | 95.9 ms | **5.9×** | 0.158 |
| `virtualB` virtual + interface calls | 8.7 ms | 1211.3 ms | 140× | 116.3 ms | **13.4×** | 0.096 |
| `listMapB` List/Map | 11.3 ms | 482.9 ms | 42.7× | 73.9 ms | **6.5×** | 0.153 |
| `allocB` allocation | 18.5 ms | 778.7 ms | 42.1× | 113.6 ms | **6.1×** | 0.146 |
| `getterB` inlined getter | 6.6 ms | 1058.2 ms | 161× | 72.8 ms | **11.1×** | 0.069 |
| linked code (`numLoopN`, `callLoopNative`, `applyN(tinyN)`) | | | 1.0× | | 1.0× | 0.99–1.00 |

Release (median of 3): the same picture, with numLoopB 59.0× → 4.4×,
SimToNative 133× → 20.6×, SimToSim 109× → 9.5×, virtual 137× → 13.3×,
List/Map 42× → 6.8×, allocation 45× → 6.3×, and getter 159× → 11.1×.

**Per-instruction cost.**

| Loop | Before | After |
| --- | --- | --- |
| `numLoopB` (21 instructions per iteration) | 110 ns/iteration, 5.2 ns per instruction | 8.4 ns/iteration, **0.40 ns per instruction** |
| `getterB` (10 instructions per iteration) | 5.3 ns per instruction | **0.36 ns per instruction** |

**Transitions.** Call-heavy micros now cost 0.09–0.16× of their previous
time. In a host profile of `callLoopSimToNative`, the largest remaining items
are the gate save/restore assembly (30 GPRs, plus v0–v7 and the
v8–v31 that are not skipped) and the C++ entry (`DartMixedRun`,
`RunMixedFast`).

**Contributions, as measured during development.**

| Change | Effect |
| --- | --- |
| First version: pre-decoded threaded ops, lazy flags, cmp+b.cond fusion | numLoopB 58.7× → 6.5× |
| Frameless branch handlers, superinstructions, generic pairs | numLoopB 6.5× → 4.0–4.4× |
| v8–v31 skip at entry | applyN 121 → 103 ms |
| v8–v31 skip at exit | SimToNative 93 → 83 ms, allocation 133 → 118 ms |

Device ratios scale differently, because the native baselines differ.
Section 12.4 gave 38–164× before this change.

### 15.4 Risks and limits

- **Coverage of the fast paths.** Uncovered instructions are correct but
  cost one generic decode each. `DART_MIXED_PROFILE_OUT` shows which ones are
  hot in a real patch.
- **The V-register skips rely on the Dart ABI** (FP arguments and results in
  v0–v7; no FPU value across a Dart call) and on stubs always being native.
  Verify mode poisons skipped registers on entry and exit, and stress mode
  passes with it. A future ABI change, such as callee-saved FPU registers in
  Dart code, would require revisiting `ComputeHighVLiveness` and
  `EntryMayWriteHighV`.
- **Memory.** 32 bytes per instruction of each unlinked entry actually
  executed, kept for the life of the process. Stress mode decodes 239 entries
  into 442 KB; a normal patch decodes 28 entries into 58 KB.
- **Profiler.** The stress + profiler crash above predates this change.
  Samples taken while interpreting are not attributed to Dart frames.
- **Not yet measured on the device.**

Left running: nothing.

### 15.x Device check on the signed path (2026-11-04)

Engine `tonycloud/ios-mixed-engine-20261104` 3de9503a936 (DEPS → 0f4fcf3f694),
production `ios_release` rebuilt incrementally, run through the section 14
signed path on the iPad (release 1.0.0+2, patches P0/P1/P2, rollback, one
tampered patch). No device-only failures: P1 reports `kernel_tag` 2, the
rollback returns to the release, and the tampered patch is rejected by the
updater (`InstallHashMismatch`).

Simulated metrics vs V0, old simulator → pre-decoded interpreter: numeric
52.8× → 4.8×, virtual 178× → 19.8×, list/map 37.3× → 6.8×, strings 7.5× →
2.6×, allocation 35.8× → 6.3×, getter (P2) 160× → 12.2×; transitions
native → simulated 120× → 11.5×, simulated → native 130× → 21.5×. Linked code
unchanged. Table: `docs/PATCH_PERFORMANCE.md`; dataset
`docs/validation/ios-mixed-mode-signed-device-fastinterp-20261104.json`.

Build note: changing `vm_sources.gni` re-runs gn on the next ninja call, which
rewrites `toolchain.ninja` and drops the local Xcode-ld solink patch; reapply
it before building (section 11, engine item 4).


# aot-tools black-box experiment: STOPPED at the terms check

Date: 2026-11-03. Status: **no experiment was run.** `aot-tools.dill` was not
downloaded, executed, or inspected. The brief's hard rule says to stop and
report the exact clause if the governing terms prohibit this use; this is the
result of that check. No P0-P7 table exists because nothing was run.

## What governs the download

- Artifact (upstream CLI, commit 4eb9d800, `shorebird_env.dart`
  `defaultDownloadUrl = https://download.shorebird.dev`, `cache.dart`
  `AotToolsArtifact.storageUrl`):
  `https://download.shorebird.dev/shorebird/<engine revision>/aot-tools.dill`.
  A HEAD request (headers only, no body fetched) for upstream revision
  `baf23dc375905d9103ca4325b062cadb7b8f90e5` redirects (302) to
  `https://storage.googleapis.com/download.shorebird.dev/shorebird/baf23dc.../aot-tools.dill`.
  HEAD for `.../LICENSE` and `.../LICENSE.txt` at the same prefix also returned
  302 (not a usable signal). No license file is evidenced next to the artifact.
- Our fork's `cache.dart` no longer points at that host (default
  `http://localhost:8080/artifacts`), and gates the download behind
  `SHOREBIRD_ENABLE_LEGACY_AOT_TOOLS`.
- The open-source repos (CLI etc.) are Apache-2.0/MIT (shorebirdtech/shorebird
  README). That licence covers the open source only; `aot-tools.dill` is the
  closed component and is not covered by it (inference: the repo README says
  nothing about it, and the CLI code itself calls it "the legacy closed
  aot_tools.dill").
- Terms of Service index https://shorebird.dev/terms lists two products. Saved
  copies and extracted text are in `.agents/aot-tools-blackbox/terms/`
  (`codepush-tos.pdf`, `ci-tos.pdf`, `cp.txt`, `ci.txt`, `SHA256SUMS`).
  - Code Push ToS (Last updated Aug 15, 2025): https://shorebird.dev/legal/Terms%20of%20Service%20-%20Shorebird%20Code%20Push.pdf
  - CI ToS: https://shorebird.dev/legal/Terms%20of%20Service%20-%20Shorebird%20CI.pdf

## Clauses that bear on this use (Code Push ToS, the applicable one)

Preamble: "BY ACCESSING, USING, SUBSCRIBING, PURCHASING, OR DOWNLOADING ANY OF
THE SERVICES, YOU ... AGREE TO BE BOUND ... WHETHER OR NOT YOU ARE A
REGISTERED USER." So downloading binds us even without an account.

Section E (License Grant): the licence is "solely during the applicable term of
Your use of the Services ... limited, personal, non-exclusive, non-transferable,
non-sub-licensable license to: (i) access ..., download, install and use the
Services; (ii) build and distribute to Your applications built with the
Services ...; and (iii) use the Documentation".

Section I (Proprietary Rights): "the Services, and its materials, including,
without limitation, software ... are the exclusive property of Shorebird and its
licensors. Except as explicitly provided herein, nothing in this Agreement shall
be deemed to create a license ... and You agree not to sell, license, rent,
modify, distribute, copy, reproduce, ... adapt, edit or create derivative works
from any materials or content accessible on the Service. Use of the Content ...
for any purpose not expressly permitted by this Agreement is strictly
prohibited."

CI ToS (for completeness), Section E: licence limited to "internal development
and testing purposes in connection with Your software development workflows";
you will not "Use the Services in any manner that exceeds the scope of the
license", nor "Reverse engineer, decompile, disassemble, or otherwise attempt to
discover the source code, object code, underlying structure, ideas, algorithms,
or know-how related to the Services", nor "create derivative works". The Code
Push ToS has no explicit reverse-engineering or benchmarking clause; the CI ToS
has the former.

## Determination

Judgement (not legal advice): the grant covers using the Services to build your
own applications. Running the closed linker outside that purpose, to learn its
behaviour as research for a competing open replacement, is not "expressly
permitted", and Section I makes any non-expressly-permitted use of Shorebird's
software "strictly prohibited". Black-box observation of the algorithm's
behaviour is exactly what the CI ToS forbids for "underlying structure, ideas,
algorithms, or know-how" (although that is a different product's ToS, it shows
the vendor's intent and may be argued to apply). Per the brief's rule, this is
treated as prohibited-or-at-least-not-clearly-permitted, so the run was
stopped. If the user disagrees (for example after legal advice, or a written
permission from Shorebird at contact@shorebird.dev), the experiment can be
resumed from the plan below.

## What can be established from open sources only (no execution)

From the open CLI (`apple.dart runLinker`, `executables/aot_tools.dart`):

- Link command: `aot-tools [--trace=F] link --base=<release App snapshot>
  --patch=<patch AOT> --analyze-snapshot=<analyze_snapshot ios> --output=<vmcode>
  --verbose --gen-snapshot=<gen_snapshot ios> --kernel=<patch dill>
  --reporter=json --redirect-to=<dir>/link.jsonl [--dd-max-bytes=N]
  [--dump-debug-info=DIR] [-- <split-debug-info args>]`.
- Result: `link.jsonl` JSON lines; `link_success.link_percentage`, or a
  `link_failure` event with `reason` and `details` (keys seen in the CLI:
  `dart_version`, `snapshot_version`, `features`). So the linker rejects a
  patch when Dart SDK / snapshot version differs or when build features differ
  (e.g. `--dwarf-stack-traces`). This is documented in the open CLI's hint
  text, not observed by running.
- Intermediate snapshots named in the CLI: `out.aot`, `out.ct.aot` (CT-sorted),
  `out.preDdOptimized.aot`, `out.ddOnly.aot`, `out.optimized.aot` (CT + OP sort
  + DD activation). The linker invokes gen_snapshot itself (hence `--kernel`).
- Other subcommands referenced: `--version`, `link --help`, `link_metadata
  <debugDir>` (JSON), `dump_blobs --analyze-snapshot --output --snapshot`.
- Matching: the upstream CLI pins Flutter revision in `bin/internal/flutter.version`
  (4eb9d800: baf23dc375905d9103ca4325b062cadb7b8f90e5, public fork
  github.com/shorebirdtech/flutter); aot-tools, gen_snapshot and analyze_snapshot
  must all come from that same engine revision. Our workspace pins our own
  engine (bin/internal/engine.version 44a553c3...), for which no upstream
  aot-tools exists, so a matching setup would need the upstream revision,
  not ours.

## Deferred plan (if cleared)

Download aot-tools.dill and upstream gen_snapshot/analyze_snapshot for
baf23dc... into `.agents/aot-tools-blackbox/dl/`, build base+patch with a tiny
Flutter app for P0-P7, run the link command above for each, record exit code,
`link.jsonl`, sizes, timings. Nothing here has been done.

## Comparison with our design

Not possible without measurements. Only observation from the open CLI: upstream
emits a single `link_percentage` (fraction of patch code linked to the base) and
treats version/feature mismatch as hard rejection, which fits our design in
docs/PATCH_PERFORMANCE.md only at the level of "report how much is reused".

## Cleanup state

Downloaded only: the two ToS PDFs, the ToS HTML, and extracted text
(`.agents/aot-tools-blackbox/terms/`, about 0.4 MB) plus an empty `dl/`.
No aot-tools, engine, or Flutter artifacts fetched. Nothing committed.

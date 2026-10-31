# Full replacement iOS device fixture

Use a fresh private work directory and prepare with `--full-replacement`. This
profile is mutually exclusive with `--structural-updates`; the existing default
fixture and additive structural fixture keep their existing policies.

```sh
python3 testapps/license_flavor_patch_test/tool/ios_device_e2e.py plan \
  --work-dir /private/tmp/shorebird-full-replacement --full-replacement \
  --include-working-tree
```

The read-only plan shows the phased sequence. Mutating phases require
`--execute`. Prepare persists the selected profile, and serve writes
`aot_patch_full_replacement: true` to the copied app's source `shorebird.yaml`.
Pass `--signed` on release for physical-device validation, following the runner's
existing dedicated bundle, distribution signing, strict interpreter, install and
key-provisioned launch checks. No engine builds are started by this runner.

By default prepare checks out committed CLI and Flutter refs. The explicit
`--include-working-tree` option overlays each checkout's tracked diff from HEAD
and nonignored untracked regular files into its isolated clone. Both selected
refs must resolve to their source checkout's HEAD. Original checkouts are read
only; symlinks/special untracked files and destination aliases are rejected.
The private session retains the tracked patches and records their SHA-256 and
untracked-file hashes in `state.json`. Ignored build/cache artifacts are still
handled by the existing toolchain phase, never by the source overlay.

The free baseline uses a const `LicenseState`, old constructor defaults,
positional method/static signatures, static initializers, a field/method/class
that will be removed, and an imported library that will be deleted. The patch:

- changes the existing const layout, removes `removedField`, and changes
  constructor defaults observed by byte-identical `unchanged_caller.dart`;
- checks nested generic constants and canonical identity;
- changes method and static function signatures and static initialization;
- removes `removedMethod`, `RemovedLicense`, and `removed_library.dart`;
- adds a library containing a generic Flutter widget override and helper.

Applying the patch templates deletes baseline-only fixture files from the app's
lib directory. Removal is a compiler/domain input assertion; Dart cannot look up
an absent declaration reflectively in this profile. Artifact validation must
also check that the replacement manifest lists the removed library.

Both versions emit status only after the first frame completes and an attached,
nonempty `RenderParagraph` contains the exact expected text. Semantic assertions
run in the widget's build before creating that text. A failed build cannot leave
the expected rendered paragraph and therefore cannot report success. The
observer's existing payload stays `license:free/pro-feature:off` for baseline
and `license:pro/pro-feature:enabled` for patch.

The CLI must bundle `shorebird_replacement_base.sbr` before signing, bind its
host ABI, and build updates against the original release wrapper. Its temporary
current-source baseline used for native archive comparison is separate from
that binding. The runner invokes the normal CLI pipeline; it does not supply an
alternate wrapper or bypass native archive checks, signing or encryption.

These templates are test inputs. Successful Python checks or preparation do not
establish Dart compilation, an authenticated update, or physical-device success.
Acceptance still requires the parent runner's signed free launch, encrypted pro
patch, patched relaunch, artifact/manifest inspection, and retained device logs.

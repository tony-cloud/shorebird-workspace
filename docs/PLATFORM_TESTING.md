# Linux And macOS Platform Testing

The first complete runtime proof was built on Windows. The next platform pass
should verify the same source layout on Linux and macOS, then use macOS to
prepare iOS testing without introducing JIT or writable-executable-memory
requirements.

## Prerequisites

Linux:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.23+
- Python and standard build dependencies required by Dart/Flutter

macOS:

- Git
- Bash
- Dart or Flutter on `PATH`
- Go 1.23+
- Xcode command line tools
- Full Xcode for iOS simulator/device follow-up

Both platforms need network access for a fresh `git submodule update` and
`gclient sync`.

## Bootstrap

Linux:

```bash
./scripts/bootstrap_linux.sh
```

macOS:

```bash
./scripts/bootstrap_macos.sh
```

Useful environment variables:

| Variable | Effect |
| --- | --- |
| `SKIP_GCLIENT_SYNC=1` | Do not run `gclient sync`. Useful when dependencies are already present. |
| `SKIP_TESTS=1` | Prepare checkout only; skip Dart/Go tests. |
| `DART_BIN=/path/to/dart` | Override the Dart executable. |
| `FLUTTER_BIN=/path/to/flutter` | Override the Flutter executable. |
| `GO_BIN=/path/to/go` | Override the Go executable. |
| `RUN_IOS_SMOKE=1` | On macOS, run a no-codesign iOS build smoke check when Flutter/Xcode are available. |

## Expected Checks

The bootstrap scripts run:

1. `git submodule update --init --recursive`
2. platform-specific `.gclient` generation
3. optional `gclient sync`
4. public updater sync into the Flutter engine third-party location
5. Shorebird CLI focused tests for self-hosted `base_url` behavior
6. open AOT patch tool tests
7. self-hosted server Go tests
8. license/flavor AOT verification when `dart-sdk-new/out/ReleaseX64AotPatch/args.gn`
   already exists

## iOS Preparation Notes

The macOS script writes:

```python
target_os = ["mac", "ios"]
```

That prepares gclient for iOS dependencies. The actual iOS runtime test should
still be run later on macOS after the Flutter engine/Dart SDK patch build is
available. The AOT patching design remains iOS-compatible only if patch loading
uses mapped snapshot data/instructions and does not rely on JIT, the KBC
interpreter, or writable executable memory.

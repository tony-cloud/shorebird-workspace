# Structural iOS device fixture

Use `ios_device_e2e.py prepare --structural-updates` in a new private work directory.
The runner enables the structural profile in the release policy and copies the
base templates into the app. The patch phase replaces those files with the patch
templates after the base release has been published.

The patch adds a library, a generic Flutter widget with an overridden `build`, a
helper function, and ordinary and late-final fields on an existing class. It
checks the original field value, added initializers, arithmetic, and a generic
type test. The status observer receives `license:pro` only after the new widget
has built successfully and the first frame completes. The baseline reports
`license:free` after its first frame.

These are test inputs, not a statement that physical-device validation passed.
Use the runner's distribution-signing and strict no-JIT checks before installing.

#!/usr/bin/env python3
"""Keep independently tested server/updater protocol vectors identical.

Each component vendors the fixture so its own repository can be tested alone.
This workspace check detects drift between those copies and the contract input.
Cryptographic/decision validation remains in the Go and Rust test suites.
"""

import hashlib
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    canonical = root / "testdata/variant_selection_v2.json"
    expected = canonical.read_bytes()
    for relative in (
        "shorebird-server/testdata/variant_selection_v2.json",
        "updater/library/test_fixtures/variant_selection_v2.json",
    ):
        if (root / relative).read_bytes() != expected:
            raise SystemExit(f"Variant selection protocol vectors differ: {relative}")
    print("Variant selection vectors agree:", hashlib.sha256(expected).hexdigest())


if __name__ == "__main__":
    main()

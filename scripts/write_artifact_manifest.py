#!/usr/bin/env python3
"""Write an open Shorebird artifact proxy manifest.

The artifact proxy expects this file at:

  /shorebird/<shorebird-engine-revision>/artifacts_manifest.yaml

It maps a custom Shorebird engine revision back to the upstream Flutter engine
revision for unchanged artifacts, and lists the artifact paths that should be
served from the open Shorebird mirror.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


DEFAULT_ARTIFACT_OVERRIDES = (
    "flutter_infra_release/flutter/$engine/android-arm64-release/artifacts.zip",
    "flutter_infra_release/flutter/$engine/android-arm64-release/symbols.zip",
    "flutter_infra_release/flutter/$engine/linux-x64-release/artifacts.zip",
    "flutter_infra_release/flutter/$engine/linux-x64-release/linux-x64-flutter-gtk.zip",
    "flutter_infra_release/flutter/$engine/ios-release/artifacts.zip",
    "flutter_infra_release/flutter/$engine/flutter_patched_sdk_product.zip",
    "flutter_infra_release/flutter/$engine/flutter-web-sdk.zip",
    "flutter_infra_release/flutter/$engine/darwin-arm64-release/FlutterMacOS.framework.zip",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--flutter-engine-revision",
        required=True,
        help="Upstream Flutter engine revision used for non-overridden artifacts.",
    )
    parser.add_argument(
        "--storage-bucket",
        default="shorebird",
        help="Bucket/path prefix under the Shorebird artifact mirror.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path. Writes to stdout when omitted.",
    )
    return parser.parse_args()


def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_manifest(flutter_engine_revision: str, storage_bucket: str) -> str:
    lines = [
        f"flutter_engine_revision: {yaml_quote(flutter_engine_revision)}",
        f"storage_bucket: {yaml_quote(storage_bucket)}",
        "artifact_overrides:",
    ]
    lines.extend(
        f"  - {yaml_quote(override)}" for override in DEFAULT_ARTIFACT_OVERRIDES
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    manifest = build_manifest(
        flutter_engine_revision=args.flutter_engine_revision,
        storage_bucket=args.storage_bucket,
    )

    if args.output is None:
        sys.stdout.write(manifest)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

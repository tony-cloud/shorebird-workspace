#!/usr/bin/env python3
"""Package built Android JARs using Flutter's public POM generator."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def package(out_dir: Path, mirror_root: Path, engine: str, artifact: str,
            include_embedding: bool = False) -> None:
    root = Path(__file__).resolve().parents[1]
    generator = root / 'flutter/engine/src/flutter/tools/androidx/generate_pom_file.py'
    artifacts = [artifact]
    if include_embedding:
        artifacts.append('flutter_embedding_release')
    for name in artifacts:
        jar = out_dir / f'{name}.jar'
        if not jar.is_file() or jar.stat().st_size == 0:
            raise ValueError(f'missing built Maven JAR: {jar}')
        with zipfile.ZipFile(jar) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise ValueError(f'invalid built Maven JAR: {jar}')
        version = f'1.0.0-{engine}'
        destination = mirror_root / 'download.flutter.io/io/flutter' / name / version
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='shorebird-maven-pom-') as tmp:
            command = [sys.executable, str(generator), '--destination', tmp,
                       '--engine-version', engine, '--engine-artifact-id', name]
            if name == 'flutter_embedding_release':
                command.extend(['--include-embedding-dependencies', 'true'])
            subprocess.run(command, check=True)
            sources = {'jar': jar, 'pom': Path(tmp) / f'{name}.pom',
                       'maven-metadata.xml': Path(tmp) / f'{name}.maven-metadata.xml'}
            for extension, source in sources.items():
                target = destination / f'{name}-{version}.{extension}'
                shutil.copyfile(source, target)
                target.with_name(target.name + '.sha1').write_text(
                    hashlib.sha1(target.read_bytes()).hexdigest() + '\n', encoding='ascii')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', required=True, type=Path)
    parser.add_argument('--mirror-root', required=True, type=Path)
    parser.add_argument('--engine-revision', required=True)
    parser.add_argument('--artifact', required=True,
                        choices=['armeabi_v7a_release', 'arm64_v8a_release', 'x86_64_release'])
    parser.add_argument('--include-embedding', action='store_true')
    args = parser.parse_args()
    package(args.out_dir, args.mirror_root, args.engine_revision, args.artifact,
            args.include_embedding)


if __name__ == '__main__':
    main()

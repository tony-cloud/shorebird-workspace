#!/usr/bin/env python3
"""Exercise public Flutter POM generation, payload copies, and Maven hashes."""

import hashlib
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from package_android_maven import package

with tempfile.TemporaryDirectory(prefix='shorebird-maven-test-') as tmp:
    root = Path(tmp)
    out = root / 'out'
    out.mkdir()
    for artifact in ['armeabi_v7a_release', 'arm64_v8a_release',
                     'x86_64_release', 'flutter_embedding_release']:
        with zipfile.ZipFile(out / f'{artifact}.jar', 'w') as jar:
            jar.writestr('libflutter.so' if artifact != 'flutter_embedding_release'
                         else 'io/flutter/Flutter.class', f'built {artifact}')
    for artifact in ['armeabi_v7a_release', 'arm64_v8a_release', 'x86_64_release']:
        package(out, root / 'shorebird', 'engine123', artifact,
                artifact == 'arm64_v8a_release')
    files = list((root / 'shorebird').rglob('*'))
    payloads = [path for path in files if path.is_file() and path.suffix != '.sha1']
    assert len(payloads) == 12
    for path in payloads:
        assert path.with_name(path.name + '.sha1').read_text().strip() == hashlib.sha1(path.read_bytes()).hexdigest()
        if path.suffix == '.jar':
            artifact = path.parent.parent.name
            assert path.read_bytes() == (out / f'{artifact}.jar').read_bytes()
        elif path.suffix == '.pom':
            pom = ET.parse(path)
            ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
            assert pom.findtext('m:version', namespaces=ns) == '1.0.0-engine123'
            assert pom.findtext('m:artifactId', namespaces=ns) == path.parent.parent.name
    (out / 'armeabi_v7a_release.jar').write_bytes(b'')
    try:
        package(out, root / 'missing', 'engine123', 'armeabi_v7a_release')
    except ValueError:
        pass
    else:
        raise AssertionError('accepted an empty built JAR')
print('Android Maven packaging smoke test passed')

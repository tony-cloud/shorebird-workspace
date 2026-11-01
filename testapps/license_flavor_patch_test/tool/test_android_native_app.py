"""Compiled-manifest policy and read-only artifact serving contracts."""
import tempfile
from pathlib import Path
import unittest
import urllib.error
import urllib.request

from android_native_app import ArtifactMirror, validate_manifest_xml


PACKAGE = 'dev.shorebird.e2e.fixture'
MANIFEST = '''<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="dev.shorebird.e2e.fixture">
<uses-sdk android:minSdkVersion="24"/>
<application android:name="android.app.Application" android:debuggable="false" android:allowBackup="false">
<activity android:name=".ControlActivity" android:exported="true"/>
<activity android:name="dev.shorebird.e2e.fixture.MainActivity" android:exported="false"/>
</application></manifest>'''


class ManifestTest(unittest.TestCase):
    def test_only_explicit_test_control_is_exported(self):
        result = validate_manifest_xml(MANIFEST, PACKAGE)
        self.assertEqual(result['exported_activities'], ['ControlActivity'])
        self.assertFalse(result['debuggable'])

    def test_merged_policy_changes_are_rejected(self):
        variants = [
            MANIFEST.replace('android:debuggable="false"', 'android:debuggable="true"'),
            MANIFEST.replace('android:allowBackup="false"', 'android:allowBackup="true"'),
            MANIFEST.replace('android.app.Application', 'dev.shorebird.Prewarmer'),
            MANIFEST.replace('MainActivity" android:exported="false"', 'MainActivity" android:exported="true"'),
            MANIFEST.replace('</application>', '<provider android:name="Prewarm"/></application>'),
            MANIFEST.replace('</application>', '<service android:name="Background"/></application>'),
            MANIFEST.replace('minSdkVersion="24"', 'minSdkVersion="22"'),
        ]
        for xml in variants:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                validate_manifest_xml(xml, PACKAGE)
        with self.assertRaises(ValueError):
            validate_manifest_xml(MANIFEST, 'dev.shorebird.other')


class MirrorTest(unittest.TestCase):
    def test_serves_only_regular_mirror_files_and_closes_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'shorebird').mkdir()
            (root / 'shorebird/artifact.zip').write_bytes(b'fixture-artifact')
            (root / 'shorebird/link.zip').symlink_to(root / 'shorebird/artifact.zip')
            mirror = ArtifactMirror(root)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(mirror.url + '/shorebird/artifact.zip', timeout=2) as response:
                    self.assertEqual(response.read(), b'fixture-artifact')
                for path in ('/shorebird/', '/shorebird/link.zip', '/shorebird/../shorebird/artifact.zip',
                             '/shorebird/artifact.zip?query=1', '/missing'):
                    with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(mirror.url + path, timeout=2)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
            finally:
                mirror.close()
            with self.assertRaises(urllib.error.URLError):
                opener.open(mirror.url + '/shorebird/artifact.zip', timeout=2)


if __name__ == '__main__':
    unittest.main()

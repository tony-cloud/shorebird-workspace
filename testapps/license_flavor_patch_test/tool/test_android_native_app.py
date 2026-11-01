"""Compiled-manifest policy and read-only artifact serving contracts."""
import tempfile
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from android_native_app import ArtifactMirror, validate_manifest_xml, verify_apk_manifest


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

    def test_omitted_debug_attribute_uses_release_default(self):
        result = validate_manifest_xml(MANIFEST.replace(' android:debuggable="false"', ''), PACKAGE)
        self.assertFalse(result['debuggable'])

    def test_compiled_apk_resolved_debug_mode_must_be_false(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            analyzer = sdk / 'cmdline-tools/latest/bin/apkanalyzer'
            analyzer.parent.mkdir(parents=True)
            analyzer.touch()
            xml = MANIFEST.replace(' android:debuggable="false"', '')
            for code, value in [(0, 'false\n'), (0, 'true\n'), (0, ''), (1, 'false\n')]:
                with self.subTest(code=code, value=value), patch('android_native_app.subprocess.run',
                        side_effect=[subprocess.CompletedProcess([], 0, xml),
                                     subprocess.CompletedProcess([], code, value)]) as run:
                    if code == 0 and value.strip() == 'false':
                        self.assertFalse(verify_apk_manifest(sdk / 'fixture.apk', PACKAGE, sdk)['debuggable'])
                    else:
                        with self.assertRaisesRegex(ValueError, 'non-debuggable'):
                            verify_apk_manifest(sdk / 'fixture.apk', PACKAGE, sdk)
                    self.assertEqual(run.call_args.args[0][1:3], ['manifest', 'debuggable'])

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

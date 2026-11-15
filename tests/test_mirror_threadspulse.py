import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("mirror", Path(__file__).parents[1] / "scripts/mirror_threadspulse.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class MirrorTests(unittest.TestCase):
    def artifact(self, name="Threads_455.0.0_Pulse_1.27.0_sideload", **kw):
        return dict(name=name, expired=False, size_in_bytes=100, **kw)

    def test_only_sideload(self):
        self.assertIsNotNone(m.select_artifact([self.artifact()]))
        self.assertIsNone(m.select_artifact([self.artifact("Threads_455.0.0_Pulse_1.27.0_noplugins")]))

    def test_expired_and_oversize(self):
        a=self.artifact(); a["expired"]=True
        self.assertIsNone(m.select_artifact([a]))
        a=self.artifact(); a["size_in_bytes"]=m.MAX_BYTES+1
        self.assertIsNone(m.select_artifact([a]))

    def test_ambiguous(self):
        with self.assertRaises(ValueError): m.select_artifact([self.artifact(),self.artifact()])

    def test_exact_marker(self):
        self.assertTrue(m.marker_matches({"body":"Artifact: 123\n"},123))
        self.assertFalse(m.marker_matches({"body":"Artifact: 1234\n"},123))
        self.assertFalse(m.marker_matches({"body":"Artifact: 123\n","draft":True},123))

    def make_ipa(self, path, bid="com.burbn.barcelona", extensions=True):
        with zipfile.ZipFile(path,"w") as z:
            z.writestr("Payload/Threads.app/Info.plist",plistlib.dumps({"CFBundleIdentifier":bid,"CFBundleShortVersionString":"455.0.0","MinimumOSVersion":"16.3"}))
            if extensions: z.writestr("Payload/Threads.app/PlugIns/Share.appex/Info.plist",plistlib.dumps({}))

    def test_valid_ipa(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.ipa";self.make_ipa(p)
            self.assertEqual(m.validate_ipa(p)["version"],"455.0.0")

    def test_wrong_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.ipa";self.make_ipa(p,bid="evil.app")
            with self.assertRaises(ValueError):m.validate_ipa(p)

    def test_reject_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.ipa";self.make_ipa(p,extensions=False)
            with self.assertRaises(ValueError):m.validate_ipa(p)

    def test_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.ipa";p.write_bytes(b"not zip")
            with self.assertRaises(zipfile.BadZipFile):m.validate_ipa(p)

if __name__ == "__main__":unittest.main()

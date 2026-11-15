import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("sidestore",Path(__file__).parents[1]/"scripts/gen_sidestore.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def unsigned():return struct.pack("<IIIIIIII",0xfeedfacf,0,0,2,0,0,0,0)


def signed(kind=0xfade7171):
    body=plistlib.dumps({"com.apple.security.application-groups":["group.example"]})
    blob=struct.pack(">II",kind,len(body)+8)+body
    signature=struct.pack(">III",0xfade0cc0,20+len(blob),1)+struct.pack(">II",5,20)+blob
    return struct.pack("<IIIIIIII",0xfeedfacf,0,0,2,1,16,0,0)+struct.pack("<IIII",0x1d,16,48,len(signature))+signature


class SideStoreTests(unittest.TestCase):
    def test_unsigned(self):self.assertEqual(m.entitlement_keys(unsigned()),set())
    def test_signed_xml(self):self.assertEqual(m.entitlement_keys(signed()),{"com.apple.security.application-groups"})
    def test_der_fails_closed(self):
        with self.assertRaises(ValueError):m.entitlement_keys(signed(0xfade7172))
    def test_not_macho(self):
        with self.assertRaises(ValueError):m.entitlement_keys(b"not an app")
    def test_fat(self):
        thin=signed();fat=struct.pack(">II",0xcafebabe,1)+struct.pack(">IIIII",0,0,28,len(thin),0)+thin
        self.assertEqual(m.entitlement_keys(fat),{"com.apple.security.application-groups"})

    def setup_source(self,root):
        (root/"scripts").mkdir()
        app={"name":"Example","bundleIdentifier":"com.example","developerName":"Author","localizedDescription":"Test",
             "iconURL":"https://example.org/icon.png","versions":[{"version":"wrong-alias","buildVersion":"wrong","downloadURL":"https://example.org/app.ipa","date":"2026-11-15T00:00:00Z","size":100}]}
        alias=copy.deepcopy(app);alias.update(name="Example Beta",bundleIdentifier="com.example.beta")
        (root/"source.json").write_text(json.dumps({"apps":[app,alias]}))
        (root/"sources.json").write_text(json.dumps({"apps":[{"name":"Example Beta","bundleIdSuffix":".beta"}]}))
        return app

    def test_real_versions_permissions_alias_filter_and_unchanged_lc(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.setup_source(root);before=(root/"source.json").read_bytes()
            entry={"bundleIdentifier":"com.example","version":"1.2.3","buildVersion":"7","minOSVersion":"16.0","size":100,"sha256":"a"*64,"appPermissions":{"entitlements":["test.entitlement"],"privacy":{"NSCameraUsageDescription":"Camera"}}}
            with patch.object(m,"metadata",return_value=(entry,None)):m.generate(root)
            source=json.loads((root/"sidestore.json").read_text());self.assertEqual(len(source["apps"]),1)
            app=source["apps"][0];self.assertEqual(app["versions"][0]["version"],"1.2.3")
            self.assertEqual(app["appPermissions"],entry["appPermissions"])
            self.assertEqual((root/"source.json").read_bytes(),before)
            with patch.object(m,"metadata",side_effect=AssertionError("cache should prevent network")):m.generate(root)

    def test_failed_new_download_keeps_previous_app(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);app=self.setup_source(root)
            (root/"sidestore.json").write_text(json.dumps({"apps":[app]}))
            with patch.object(m,"metadata",side_effect=ValueError("download failed")):m.generate(root)
            self.assertEqual(json.loads((root/"sidestore.json").read_text())["apps"],[app])

    def test_refuse_empty_on_initial_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.setup_source(root)
            with patch.object(m,"metadata",side_effect=ValueError("download failed")):
                with self.assertRaises(ValueError):m.generate(root)
            self.assertFalse((root/"sidestore.json").exists())

if __name__=="__main__":unittest.main()

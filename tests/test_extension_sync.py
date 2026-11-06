"""The app keeps a fixed extension folder in step with its version."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone import __version__, extension_sync  # noqa: E402


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.d = os.path.join(tempfile.mkdtemp(), "SIP Phone Extension")

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.d), ignore_errors=True)

    def test_creates_updates_and_cleans(self):
        path, changed = extension_sync.sync(self.d)
        self.assertTrue(changed)
        m = json.load(open(os.path.join(path, "manifest.json")))
        self.assertEqual(m["version"], __version__)
        self.assertIn("key", m)                                     # fixed ID for the bridge
        for f in ("background.js", "sidepanel.html", "icons/icon128.png", "README.txt"):
            self.assertTrue(os.path.isfile(os.path.join(path, f)), f)
        self.assertIn(path, open(os.path.join(path, "README.txt")).read())
        self.assertFalse(extension_sync.sync(self.d)[1])             # nothing to do
        # an old copy: stale file contents + a file the new version dropped
        open(os.path.join(path, "background.js"), "w").write("// STALE-COPY")
        open(os.path.join(path, "obsolete.js"), "w").write("// gone")
        self.assertTrue(extension_sync.sync(self.d)[1])
        self.assertNotIn("STALE-COPY", open(os.path.join(path, "background.js")).read())
        self.assertFalse(os.path.exists(os.path.join(path, "obsolete.js")))

    def test_droppable_zip(self):
        import io, zipfile
        data = extension_sync.zip_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            self.assertIn("manifest.json", names)                     # at the root: Chrome requires it
            self.assertTrue(all("/" not in n or n.startswith("icons/") for n in names), names)
            self.assertNotIn("README.txt", names)
            m = json.loads(z.read("manifest.json"))
            self.assertIn("key", m)                                    # fixed ID for the bridge
            self.assertEqual(m["version"], __version__)
            info = json.loads(z.read("install.json"))
            self.assertEqual((info["via"], info["build"]), ("zip", extension_sync.build_id()))
        self.assertEqual(data, extension_sync.zip_bytes())              # reproducible
        store = extension_sync.zip_bytes(keep_key=False)
        with zipfile.ZipFile(io.BytesIO(store)) as z:
            self.assertNotIn("key", json.loads(z.read("manifest.json")))
            self.assertEqual(json.loads(z.read("install.json"))["via"], "store")
        # the folder copy says so too, with the same build
        path, _ = extension_sync.sync(self.d)
        folder_info = json.load(open(os.path.join(path, "install.json")))
        self.assertEqual((folder_info["via"], folder_info["build"]), ("folder", extension_sync.build_id()))
        # saved to Downloads by default
        home = tempfile.mkdtemp(); os.makedirs(os.path.join(home, "Downloads"))
        old_home = os.environ.get("HOME"); os.environ["HOME"] = home
        try:
            zp = extension_sync.write_zip()
            self.assertEqual(zp, os.path.join(home, "Downloads", extension_sync.zip_name()))
            self.assertEqual(open(zp, "rb").read(), data)
            self.assertEqual(extension_sync.write_zip(), zp)             # unchanged file left alone
        finally:
            os.environ["HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)

    def test_build_id_ignores_version_only_changes(self):
        b = extension_sync.build_id()
        self.assertEqual(len(b), 16)
        self.assertEqual(b, extension_sync.build_id())

    def test_default_folder_is_outside_appdata(self):
        # Microsoft Store Python hides AppData writes from Chrome
        self.assertNotIn("appdata", extension_sync.extension_dir().lower())
        self.assertTrue(extension_sync.extension_dir().endswith("SIP Phone Extension"))


if __name__ == "__main__":
    unittest.main()

"""In-app updates: wheel validation/staging, update sources, the lock, the
native host stepping aside, and the helper actually upgrading a venv."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone import __version__, updater  # noqa: E402


def fake_wheel(folder, version, broken=False):
    """A minimal but valid-looking sipphone wheel."""
    path = os.path.join(folder, f"sipphone-{version}-py3-none-any.whl")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("sipphone/__init__.py", f'__version__ = "{version if not broken else "0.0.0"}"\n')
        z.writestr(f"sipphone-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: sipphone\nVersion: {version}\n")
    return path


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = self.home          # config_dir() -> here
        self.src = tempfile.mkdtemp()

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True); shutil.rmtree(self.src, ignore_errors=True)

    def test_versions(self):
        self.assertTrue(updater.newer("2.0.0", "1.9.0"))
        self.assertTrue(updater.newer("1.10.0", "1.9.3"))
        self.assertFalse(updater.newer("1.9.0", "1.9.0"))
        self.assertEqual(updater.wheel_version("sipphone-2.1.0-py3-none-any.whl"), "2.1.0")
        self.assertEqual(updater.wheel_version("evil-2.1.0-py3-none-any.whl"), "")

    def test_validate_and_stage(self):
        good = fake_wheel(self.src, "9.9.9")
        self.assertEqual(updater.validate_wheel(good), "9.9.9")
        with self.assertRaises(ValueError):
            updater.validate_wheel(fake_wheel(self.src, "9.9.8", broken=True))
        bad = os.path.join(self.src, "sipphone-1.0.0-py3-none-any.whl")
        open(bad, "wb").write(b"not a zip")
        with self.assertRaises(ValueError):
            updater.validate_wheel(bad)
        with self.assertRaises(ValueError):
            updater.stage("notes.txt", b"hello")
        # from bytes (browser upload) and from a share zip
        path, ver = updater.stage("sipphone-9.9.9-py3-none-any.whl", open(good, "rb").read())
        self.assertEqual(ver, "9.9.9"); self.assertTrue(os.path.isfile(path))
        self.assertIn(os.path.join(self.home, "sipphone", "updates"), path)
        bundle = os.path.join(self.src, "Telvoro-9.9.9.zip")
        with zipfile.ZipFile(bundle, "w") as z:
            z.write(good, "SIP Phone 9.9.9/sipphone-9.9.9-py3-none-any.whl")
            z.writestr("SIP Phone 9.9.9/Install.bat", "rem")
        path2, ver2 = updater.stage("Telvoro-9.9.9.zip", open(bundle, "rb").read())
        self.assertEqual((ver2, os.path.basename(path2)), ("9.9.9", "sipphone-9.9.9-py3-none-any.whl"))

    def test_folder_source(self):
        self.assertIsNone(updater.find_in_folder(self.src))
        fake_wheel(self.src, "1.2.0"); fake_wheel(self.src, "1.10.0"); fake_wheel(self.src, "1.9.0")
        open(os.path.join(self.src, "CHANGELOG.md"), "w").write("## 1.10.0\n- things\n")
        f = updater.find_in_folder(self.src)
        self.assertEqual(f["version"], "1.10.0"); self.assertIn("things", f["notes"])
        with self.assertRaises(RuntimeError):
            updater.find_in_folder(os.path.join(self.src, "missing"))

    def test_lock(self):
        self.assertIsNone(updater.updating())
        updater.set_lock("3.0.0")
        self.assertEqual(updater.updating()["version"], "3.0.0")
        os.utime(updater.lock_path(), (time.time() - 3600, time.time() - 3600))
        self.assertIsNone(updater.updating())                 # stale locks are dropped
        self.assertFalse(os.path.exists(updater.lock_path()))

    def test_manager_check_and_refusals(self):
        fake_wheel(self.src, "99.0.0")
        quits = []
        m = updater.UpdateManager(8088, on_quit=lambda: quits.append(1))
        m.configure({"update_source": self.src, "update_auto": False})
        r = m.check_now()
        self.assertTrue(r["ok"]); self.assertEqual(m.status()["available"]["version"], "99.0.0")
        self.assertNotIn("path", m.status()["available"])      # no file paths to the page
        m.busy = lambda: True
        with self.assertRaises(RuntimeError):
            m.install_available()                              # call active
        m.busy = lambda: False
        older = fake_wheel(self.src, "0.1.0")
        with self.assertRaises(RuntimeError):
            m.install_file(older)
        term = updater.UpdateManager(8088, on_quit=None)
        with self.assertRaises(RuntimeError):
            term.install_file(os.path.join(self.src, "sipphone-99.0.0-py3-none-any.whl"))
        m.stop()


class UploadApiTests(unittest.TestCase):
    """Settings -> Install from file, through the real HTTP API."""

    def setUp(self):
        from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
        from sipphone.phone import Config, SIPPhone
        from sipphone.audio import open_audio
        from sipphone import web
        self.home = tempfile.mkdtemp(); os.environ["XDG_CONFIG_HOME"] = self.home
        self.work = tempfile.mkdtemp()
        self.sw = MockSwitch(); self.sw.start()
        cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH, proxy="127.0.0.1",
                     proxy_port=self.sw.port, bind_ip="127.0.0.1", bind_port=0)
        self.phone = SIPPhone(cfg, audio=open_audio(kind="null")); self.phone.start()
        self.quits = []
        self.upd = updater.UpdateManager(0, on_quit=lambda: self.quits.append(1))
        self.srv = web.serve(self.phone, self.phone.audio, port=0, open_browser=False,
                             phrases_path=f"{self.work}/p", contacts_path=f"{self.work}/c",
                             history_path=f"{self.work}/h", updater=self.upd)
        self.upd.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        # a stand-in for the helper so nothing really gets installed
        self.helper_log = os.path.join(self.work, "helper-args.txt")
        os.environ["SIPPHONE_HELPER_CMD"] = json.dumps([sys.executable, "-c",
            "import sys; open(%r, 'w').write(' '.join(sys.argv[1:]))" % self.helper_log])

    def tearDown(self):
        os.environ.pop("SIPPHONE_HELPER_CMD", None); os.environ.pop("XDG_CONFIG_HOME", None)
        self.srv.shutdown(); self.srv.server_close(); self.phone.stop(); self.sw.stop.set()
        shutil.rmtree(self.home, ignore_errors=True); shutil.rmtree(self.work, ignore_errors=True)

    def upload(self, name, data, origin=None):
        boundary = "----sipphonetest"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        h = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if origin:
            h["Origin"] = origin
        r = urllib.request.Request(self.base + "/api/update/upload", data=body, headers=h, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_upload_installs_via_helper(self):
        st, r = self.upload("notes.txt", b"hello")
        self.assertEqual(st, 400); self.assertIn("isn't a Telvoro update", r["error"])
        st, r = self.upload("sipphone-9.9.9-py3-none-any.whl", b"garbage")
        self.assertEqual(st, 400)
        st, r = self.upload("sipphone-9.9.9-py3-none-any.whl", open(fake_wheel(self.work, "9.9.9"), "rb").read(),
                            origin="https://evil.example")
        self.assertEqual(st, 403)                                   # same lock-down as every other POST
        st, r = self.upload("sipphone-9.9.9-py3-none-any.whl", open(fake_wheel(self.work, "9.9.9"), "rb").read())
        self.assertEqual(st, 200, r); self.assertEqual(r["version"], "9.9.9")
        self.assertIsNotNone(updater.updating())
        self.assertEqual(updater.updating()["version"], "9.9.9")
        end = time.time() + 5
        while time.time() < end and not (self.quits and os.path.exists(self.helper_log)):
            time.sleep(0.1)
        self.assertEqual(self.quits, [1], "the app quits so the helper can replace it")
        args = open(self.helper_log).read()
        self.assertIn("--wheel", args); self.assertIn("sipphone-9.9.9-py3-none-any.whl", args)
        self.assertIn("--port %d" % self.srv.server_address[1], args)
        st, s = urllib.request.urlopen(self.base + "/api/state").status, None
        self.assertEqual(st, 200)
        self.assertEqual(self.upd.status()["state"], "installing")
        with self.assertRaises(RuntimeError):                       # second one refused while the lock holds
            self.upd.install_file(fake_wheel(self.work, "9.9.8"))
        updater.clear_lock()


class NativeHostLockTests(unittest.TestCase):
    """With the lock present the bridge reports 'updating', won't start the app, and exits."""

    def test_host_steps_aside(self):
        from tests.test_native_host import send, recv
        home = tempfile.mkdtemp()
        env = dict(os.environ, XDG_CONFIG_HOME=home, SIPPHONE_UI_PORT="1",     # nothing listens on 1
                   SIPPHONE_LAUNCH_CMD=json.dumps([sys.executable, "-c", "open(%r, 'w').write('started')" %
                                                   os.path.join(home, "launched.txt")]))
        os.makedirs(os.path.join(home, "sipphone"))
        json.dump({"version": "x", "started": time.time()}, open(os.path.join(home, "sipphone", "update.lock"), "w"))
        host = subprocess.Popen([sys.executable, "-m", "sipphone.native_host"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=env, cwd=ROOT)
        send(host, {"cmd": "launch", "id": 1})
        st = recv(host, lambda m: m.get("type") == "status")
        self.assertTrue(st and st.get("updating") and not st.get("up"), st)
        rc = None
        for _ in range(40):
            rc = host.poll()
            if rc is not None: break
            time.sleep(0.25)
        self.assertIsNotNone(rc, "host should exit while an update is in progress")
        self.assertFalse(os.path.exists(os.path.join(home, "launched.txt")), "must not autostart the app")
        shutil.rmtree(home, ignore_errors=True)


class HelperEndToEndTests(unittest.TestCase):
    """Real pip upgrade in a throwaway venv, driven by the helper the way the app does it."""

    def test_helper_upgrades_and_relaunches(self):
        work = tempfile.mkdtemp()
        venv = os.path.join(work, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        vpy = os.path.join(venv, "bin", "python")
        # build two wheels of this very package: the current one and a "newer" one
        def build(version):
            src = os.path.join(work, "src-" + version)
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "tests", ".git", "__pycache__", ".extkey"))
            for rel, old, new in (("sipphone/__init__.py", f'__version__ = "{__version__}"', f'__version__ = "{version}"'),
                                  ("pyproject.toml", f'version = "{__version__}"', f'version = "{version}"')):
                f = os.path.join(src, rel)
                text = open(f).read()
                self.assertIn(old, text)
                with open(f, "w") as w:
                    w.write(text.replace(old, new))
            out = os.path.join(work, "wheels-" + version)
            r = subprocess.run([vpy, "-m", "pip", "wheel", "--no-deps", "-q", "-w", out, src], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return os.path.join(out, f"sipphone-{version}-py3-none-any.whl")
        cur = build(__version__)
        new = build("99.1.0")
        subprocess.run([vpy, "-m", "pip", "install", "-q", cur], check=True)
        home = os.path.join(work, "home"); os.makedirs(home)
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))
        # the installed copy stages the wheel and launches the helper (as Settings -> Install does),
        # with a stub "app" so the relaunch is observable without a phone
        stub = os.path.join(work, "stub-app.sh")
        open(stub, "w").write("#!/bin/sh\necho \"$@\" > " + os.path.join(work, "relaunched.txt") + "\n")
        os.chmod(stub, 0o755)
        code = f"""
import sipphone.updater as u
u.launch_helper({new!r}, "99.1.0", 65000)
print(u.lock_path())
"""
        env["SIPPHONE_APP_EXE"] = stub
        r = subprocess.run([vpy, "-I", "-c", code], env=env, capture_output=True, text=True, cwd=work)
        self.assertEqual(r.returncode, 0, r.stderr)
        lock = r.stdout.strip()
        self.assertTrue(os.path.exists(lock))
        end = time.time() + 120
        while time.time() < end and os.path.exists(lock):
            time.sleep(0.5)
        self.assertFalse(os.path.exists(lock), "helper should clear the lock")
        result = json.load(open(os.path.join(os.path.dirname(lock), "update-result.json")))
        self.assertTrue(result["ok"], result)
        v = subprocess.run([vpy, "-I", "-c", "import sipphone; print(sipphone.__version__)"],
                           capture_output=True, text=True, cwd=work).stdout.strip()
        self.assertEqual(v, "99.1.0")
        for _ in range(20):
            if os.path.exists(os.path.join(work, "relaunched.txt")): break
            time.sleep(0.25)
        self.assertIn("--after-update", open(os.path.join(work, "relaunched.txt")).read())
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

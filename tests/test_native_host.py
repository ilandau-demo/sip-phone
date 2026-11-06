"""Native-messaging host: Chrome wire format against a live phone UI."""
import json, os, struct, subprocess, sys, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from sipphone.phone import Config, SIPPhone, CallState
from sipphone.audio import open_audio
from sipphone import web


def send(p, msg):
    data = json.dumps(msg).encode()
    p.stdin.write(struct.pack("<I", len(data)) + data); p.stdin.flush()


def recv(p, want=None, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        head = p.stdout.read(4)
        if len(head) < 4:
            return None
        (n,) = struct.unpack("<I", head)
        msg = json.loads(p.stdout.read(n))
        if want is None or want(msg):
            return msg
    return None


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.sw = MockSwitch(); self.sw.start()
        cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH, proxy="127.0.0.1",
                     proxy_port=self.sw.port, bind_ip="127.0.0.1", bind_port=0, display_name="Demo Desk",
                     rtp_port_range=(32000, 33000))
        self.phone = SIPPhone(cfg, audio=open_audio(kind="null")); self.phone.start()
        self.assertTrue(self.phone.register(wait=True))
        d = os.path.join(os.path.dirname(__file__), "..", ".nh-tmp"); os.makedirs(d, exist_ok=True)
        self.srv = web.serve(self.phone, self.phone.audio, port=0, open_browser=False,
                             phrases_path=os.path.join(d, "p.txt"), contacts_path=os.path.join(d, "c.json"),
                             history_path=os.path.join(d, "h.json"))
        env = dict(os.environ, SIPPHONE_UI_PORT=str(self.srv.server_address[1]))
        self.host = subprocess.Popen([sys.executable, "-m", "sipphone.native_host", "--no-autostart"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                     cwd=os.path.join(os.path.dirname(__file__), ".."))

    def tearDown(self):
        try: self.host.stdin.close()
        except Exception: pass
        self.host.wait(5)
        self.srv.shutdown(); self.phone.stop(); self.sw.stop.set()

    def test_status_push_and_ping(self):
        st = recv(self.host, lambda m: m.get("type") == "status")
        self.assertTrue(st["up"]); self.assertTrue(st["registered"]); self.assertEqual(st["name"], "Demo Desk")
        send(self.host, {"cmd": "ping", "id": 1})
        self.assertEqual(recv(self.host, lambda m: m.get("type") == "pong")["id"], 1)

    def test_call_from_selection_text(self):
        recv(self.host, lambda m: m.get("type") == "status")
        send(self.host, {"cmd": "call", "id": 7, "number": "Call us: (616) 555-0199 today"})
        r = recv(self.host, lambda m: m.get("type") == "result" and m.get("id") == 7)
        self.assertTrue(r["ok"], r); self.assertEqual(r["number"], "6165550199")
        st = recv(self.host, lambda m: m.get("type") == "status" and (m.get("call") or {}).get("state") == "connected")
        self.assertIsNotNone(st, "status push for connected call")
        send(self.host, {"cmd": "hangup", "id": 8})
        self.assertTrue(recv(self.host, lambda m: m.get("id") == 8)["ok"])

    def test_incoming_call_is_pushed_and_answerable(self):
        recv(self.host, lambda m: m.get("type") == "status")
        self.sw.call_phone()
        st = recv(self.host, lambda m: m.get("type") == "status" and (m.get("call") or {}).get("state") == "incoming")
        self.assertIsNotNone(st); self.assertEqual(st["call"]["number"], "5550001111")
        send(self.host, {"cmd": "answer", "id": 3})
        self.assertTrue(recv(self.host, lambda m: m.get("id") == 3)["ok"])
        self.assertTrue(recv(self.host, lambda m: m.get("type") == "status" and (m.get("call") or {}).get("state") == "connected"))

    def test_error_is_reported_not_crash(self):
        recv(self.host, lambda m: m.get("type") == "status")
        send(self.host, {"cmd": "call", "id": 9, "number": "no digits here"})
        r = recv(self.host, lambda m: m.get("id") == 9)
        self.assertFalse(r["ok"]); self.assertIn("No phone number", r["error"])


if __name__ == "__main__":
    unittest.main()


class BridgeRegistrationTests(unittest.TestCase):
    """The app registers the bridge itself (per-browser manifest dirs here; registry on Windows)."""

    def test_register_status_unregister(self):
        import json as _json, shutil, tempfile
        from sipphone import native_host as nh
        home = tempfile.mkdtemp()
        old_home, old_exe = os.environ.get("HOME"), nh.host_exe_path
        exe = os.path.join(home, "sipphone-native-host"); open(exe, "w").write("#!/bin/sh\n")
        os.environ["HOME"] = home
        nh.host_exe_path = lambda: exe
        try:
            self.assertFalse(nh.bridge_status()["registered"])
            r = nh.register_bridge()
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["changed"])
            self.assertEqual(len(r["where"]), 3)                        # Chrome, Chromium, Edge
            m = _json.load(open(r["path"]))
            self.assertEqual(m["name"], "com.sipphone.host"); self.assertEqual(m["path"], exe)
            self.assertIn("chrome-extension://ekooaigkfmobhjmioleeahcanjnhhheh/", m["allowed_origins"])
            self.assertIn("/.config/google-chrome/NativeMessagingHosts/", r["path"])
            self.assertFalse(nh.register_bridge()["changed"])            # idempotent
            self.assertTrue(nh.bridge_status()["registered"])
            nh.unregister_bridge()
            self.assertFalse(nh.bridge_status()["registered"])
            nh.host_exe_path = lambda: os.path.join(home, "missing")
            self.assertFalse(nh.register_bridge()["ok"])
        finally:
            os.environ["HOME"] = old_home; nh.host_exe_path = old_exe
            shutil.rmtree(home, ignore_errors=True)

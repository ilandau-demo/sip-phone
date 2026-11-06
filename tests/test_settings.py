"""Settings screen: config writer, /api/settings, live re-register, API lock-down."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM  # noqa: E402
from sipphone import config_store, web  # noqa: E402
from sipphone.cli import load_config  # noqa: E402
from sipphone.phone import SIPPhone  # noqa: E402
from sipphone.audio import open_audio  # noqa: E402

TEMPLATE = os.path.join(ROOT, "config.example.ini")


def wait(pred, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.ini")
        shutil.copy(TEMPLATE, self.p)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_preserves_comments_and_layout(self):
        before = open(TEMPLATE, encoding="utf-8").read().splitlines()
        config_store.update(self.p, {"account": {"username": "5551112222", "password": "x"},
                                     "admin": {"hide_user": "jdoe1"}})
        after = open(self.p, encoding="utf-8").read().splitlines()
        self.assertEqual(len(before), len(after))
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(changed), 3)
        for a, b in changed:   # comment kept, in the same column
            self.assertEqual(a[a.index(";"):], b[b.index(";"):])
            self.assertEqual(a.index(";"), b.index(";"))

    def test_special_characters_round_trip(self):
        pw = "p%ss;w#rd%(x)s"
        config_store.update(self.p, {"account": {"password": pw}})
        cfg, _, _ = load_config(self.p)
        self.assertEqual(cfg.password, pw)

    def test_rejects_values_that_would_become_comments(self):
        with self.assertRaises(ValueError):
            config_store.update(self.p, {"account": {"password": "abc ;def"}})
        with self.assertRaises(ValueError):
            config_store.update(self.p, {"account": {"password": "a\nb"}})

    def test_adds_missing_keys_and_sections(self):
        open(self.p, "w").write("[account]\nusername = 1\npassword = 2\ndomain = d\n")
        config_store.update(self.p, {"account": {"display_name": "Desk"}, "admin": {"username": "adm"}})
        cp = config_store.read(self.p)
        self.assertEqual(cp["account"]["display_name"], "Desk")
        self.assertEqual(cp["admin"]["username"], "adm")
        self.assertTrue(os.path.exists(self.p + ".bak"))


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        import logging; logging.basicConfig(level=logging.WARNING)
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.ini")
        shutil.copy(TEMPLATE, self.p)
        self.sw = MockSwitch(); self.sw.start()
        # start like a brand-new install: template account, but pointed at the mock
        config_store.update(self.p, {"account": {"proxy": "127.0.0.1", "proxy_port": str(self.sw.port),
                                                 "domain": REALM},
                                     "local": {"bind_port": "0", "bind_ip": "127.0.0.1"},
                                     "audio": {"backend": "null"}})
        cfg, _, _ = load_config(self.p)
        self.phone = SIPPhone(cfg, audio=open_audio(kind="null")); self.phone.start()
        self.assertFalse(self.phone.register(wait=True, timeout=3))   # template creds fail
        self.monitors = []

        def build_monitor(admin):
            class FakeMon:
                def __init__(s, a): s.a = a; s.stopped = False
                def stop(s): s.stopped = True
                def snapshot(s): return {"users": [], "active": 0, "total": 0, "ready": True}
            m = FakeMon(admin); self.monitors.append(m); return m

        self.restarts = []
        self.srv = web.serve(self.phone, self.phone.audio, port=0, open_browser=False,
                             phrases_path=f"{self.d}/p.txt", contacts_path=f"{self.d}/c.json",
                             history_path=f"{self.d}/h.json", config_path=self.p,
                             build_monitor=build_monitor, on_restart=lambda: self.restarts.append(1))
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        self.phone.stop(); self.sw.stop.set()
        shutil.rmtree(self.d, ignore_errors=True)

    def req(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_boards_api(self):
        st, b = self.req("/api/boards")
        self.assertEqual((st, b), (200, {"hidden": [], "boards": []}))
        st, b = self.req("/api/boards", {"hidden": ["recent", "bogus"], "boards": [
            {"name": " Sales floor ", "members": [{"type": "team", "id": "alice@ex.com"}, {"type": "contact", "id": "555"},
                                                  {"type": "team", "id": "alice@ex.com"}, {"type": "nope", "id": "x"}]},
            {"name": "", "members": []}]})
        self.assertEqual(st, 200)
        self.assertEqual(b["hidden"], ["recent"])
        self.assertEqual(len(b["boards"]), 1)
        self.assertEqual(b["boards"][0]["name"], "Sales floor")
        self.assertEqual(b["boards"][0]["members"], [{"type": "team", "id": "alice@ex.com"}, {"type": "contact", "id": "555"}])
        self.assertTrue(b["boards"][0]["id"])
        self.assertEqual(self.req("/api/boards")[1], b)                      # persisted next to contacts
        self.assertTrue(os.path.exists(os.path.join(self.d, "boards.json")))
        st, e = self.req("/api/boards", {"hidden": ["team", "contacts", "recent"], "boards": []})
        self.assertEqual(st, 400); self.assertIn("at least one tab", e["error"])

    def test_first_run_then_fix_account_live(self):
        st, s = self.req("/api/state")
        self.assertTrue(s["needs_setup"]); self.assertTrue(s["has_settings"])
        st, g = self.req("/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(g["values"]["account"]["password"], "")
        self.assertFalse(g["secrets_set"]["account.password"])          # template password isn't "set"
        # wrong password first: saved, re-registers, fails
        st, r = self.req("/api/settings", {"values": {"account": {"username": USER, "auth_username": AUTH,
                                                                  "password": "wrong%pw", "display_name": "Demo"}}})
        self.assertEqual(st, 200, r)
        self.assertIn("account", r["applied"])
        self.assertTrue(wait(lambda: not self.phone.registered and "auth" in self.srv.ui_state.reg_detail))
        # right password: registers without a restart
        st, r = self.req("/api/settings", {"values": {"account": {"password": PASS}}})
        self.assertEqual(r["changed"], ["account.password"])
        self.assertTrue(wait(lambda: self.phone.registered), self.srv.ui_state.reg_detail)
        st, s = self.req("/api/state")
        self.assertFalse(s["needs_setup"]); self.assertEqual(s["display_name"], "Demo")
        # passwords are never sent back, blank keeps the saved one
        raw = json.dumps(self.req("/api/settings")[1])
        self.assertNotIn(PASS, raw); self.assertNotIn("wrong%pw", raw)
        st, r = self.req("/api/settings", {"values": {"account": {"password": "", "display_name": "Demo 2"}}})
        self.assertEqual(r["changed"], ["account.display_name"])
        self.assertEqual(load_config(self.p)[0].password, PASS)
        self.assertTrue(self.phone.registered)

    def test_validation(self):
        st, r = self.req("/api/settings", {"values": {"account": {"proxy_port": "99999"}}})
        self.assertEqual(st, 400); self.assertIn("Proxy port", r["error"])
        st, r = self.req("/api/settings", {"values": {"account": {"domain": ""}}})
        self.assertEqual(st, 400)
        st, r = self.req("/api/settings", {"values": {"admin": {"poll_seconds": "0.1"}}})
        self.assertEqual(st, 400)

    def test_team_view_rebuilt_and_restart_flags(self):
        st, r = self.req("/api/settings", {"values": {"admin": {"username": "adm", "password": "pw",
                                                                "directory_user": "jdoe1", "pickup_code": "*98"}}})
        self.assertIn("team", r["applied"])
        self.assertEqual(len(self.monitors), 1)
        self.assertEqual(self.monitors[0].a["password"], "pw")
        self.assertIs(self.srv.ui_state.monitor, self.monitors[0])
        self.assertEqual(self.srv.ui_state.pickup_code, "*98")
        self.assertTrue(self.req("/api/state")[1]["has_monitor"])
        # turning it off stops the poller
        st, r = self.req("/api/settings", {"values": {"admin": {"username": ""}}})
        self.assertTrue(self.monitors[0].stopped)
        self.assertIsNone(self.srv.ui_state.monitor)
        # restart-only setting
        st, r = self.req("/api/settings", {"values": {"local": {"bind_port": "5070"}}})
        self.assertEqual(r["restart_required"], ["local.bind_port"])
        self.assertTrue(self.req("/api/state")[1]["restart_pending"])
        st, r = self.req("/api/restart", {})
        self.assertTrue(wait(lambda: self.restarts == [1]))

    def test_api_locked_to_this_app(self):
        evil = {"Origin": "https://evil.example"}
        st, r = self.req("/api/call", {"number": "5551000"}, evil)
        self.assertEqual(st, 403)
        st, r = self.req("/api/settings", {"values": {"account": {"password": "x"}}}, evil)
        self.assertEqual(st, 403)
        # form posts (no preflight) are refused
        st, r = self.req("/api/hangup", {}, {"Content-Type": "text/plain"})
        self.assertEqual(st, 403)
        # DNS rebinding: right IP, wrong Host
        st, r = self.req("/api/settings", None, {"Host": f"attacker.example:{self.port}"})
        self.assertEqual(st, 403)
        # allowed: the page itself, the extension, local programs (no Origin)
        for o in (self.base, "chrome-extension://abcdefghijklmnopabcdefghijklmnop", None):
            st, r = self.req("/api/register", {}, {"Origin": o} if o else {})
            self.assertEqual(st, 200, o)


if __name__ == "__main__":
    unittest.main()

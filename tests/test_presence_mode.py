"""Team-view-only mode: no registration, no calls, presence from XSI Events, UI state."""
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
from tests.mock_xsi_events import MockXsi, USER as XU, PW as XPW, call_xml  # noqa: E402
from tests.test_presence_engine import TEAM, BOB, wait  # noqa: E402
from sipphone import config_store, web  # noqa: E402
from sipphone.cli import load_config, make_monitor, app_mode  # noqa: E402
from sipphone.phone import SIPPhone, CallState  # noqa: E402
from sipphone.audio import open_audio  # noqa: E402


class PresenceModeTests(unittest.TestCase):
    def setUp(self):
        import logging; logging.basicConfig(level=logging.WARNING)
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.ini")
        shutil.copy(os.path.join(ROOT, "config.example.ini"), self.p)
        self.sw = MockSwitch(); self.sw.start()
        from sipphone.presence import manager as xsi_events
        xsi_events.HEARTBEAT = 0.5
        self.mock = MockXsi(TEAM, dnd={"carol1003@nextiva.com": True})
        self.mock.calls[BOB] = [call_xml("c1", "Active", "Terminator", "Acme", "6165559999")]
        config_store.update(self.p, {"app": {"mode": "presence"},
                                     "account": {"username": USER, "auth_username": AUTH, "password": PASS,
                                                 "domain": REALM, "proxy": "127.0.0.1",
                                                 "proxy_port": str(self.sw.port), "display_name": "Front Desk"},
                                     "local": {"bind_port": "0"}, "audio": {"backend": "null"},
                                     "presence": {"mode": "events", "xsi_user": XU, "xsi_password": XPW},
                                     "admin": {"xsi_host": self.mock.base, "verify_tls": "false"}})
        loaded = load_config(self.p)
        self.assertEqual(loaded.app["mode"], "presence")
        cfg, _, adm = loaded
        self.phone = SIPPhone(cfg, audio=open_audio(kind="null"))
        self.phone.presence_only = True
        self.phone.start()
        self.mon = make_monitor(adm, self.phone)
        self.srv = web.serve(self.phone, self.phone.audio, port=0, open_browser=False,
                             phrases_path=f"{self.d}/p", contacts_path=f"{self.d}/c", history_path=f"{self.d}/h",
                             config_path=self.p, monitor=self.mon, started_mode="presence",
                             build_monitor=lambda o: make_monitor(o, self.phone))
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        m = self.srv.ui_state.monitor
        if m: m.stop()
        self.srv.shutdown(); self.srv.server_close()
        self.phone.stop(); self.sw.stop.set(); self.mock.stop()
        from sipphone.presence import manager as xsi_events
        xsi_events.HEARTBEAT = 15.0
        shutil.rmtree(self.d, ignore_errors=True)

    def req(self, path, body=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_no_registration_but_presence_works(self):
        self.assertFalse(self.phone.register(wait=True, timeout=2))
        self.assertEqual(self.sw.register_count, 0)                 # never registered
        self.assertTrue(wait(lambda: self.mon.snapshot()["connected"] and
                             {u["name"]: u["status"] for u in self.mon.snapshot()["users"]}.get("Bob Brown") == "on-call"))
        self.assertEqual(self.mock.blf_requests, 0)                   # no SIP SUBSCRIBE, no BLF service reads
        self.assertNotIn("SUBSCRIBE", self.sw.events)
        st, s = self.req("/api/state")
        self.assertEqual(s["mode"], "presence"); self.assertFalse(s["needs_setup"])
        self.assertTrue(s["has_monitor"]); self.assertFalse(s["registered"])
        st, r = self.req("/api/call", {"number": "5551000"})
        self.assertEqual(st, 400); self.assertIn("Team view only", r["error"])
        st, r = self.req("/api/pickup", {"extension": "1001"})
        self.assertEqual(st, 400)
        self.assertNotIn("INVITE", self.sw.events)
        # an incoming INVITE is declined outright
        self.sw.phone_addr = (self.phone.local_ip, self.phone.local_port)
        self.sw.call_phone()
        self.assertTrue(wait(lambda: self.sw.out_final is not None))
        self.assertEqual(self.sw.out_final.status, 603)
        self.assertFalse(self.phone.calls)

    def test_presence_api(self):
        self.assertTrue(wait(lambda: self.mon.snapshot()["connected"] and self.mon.snapshot()["ready"] and
                             all(u["status"] != "unknown" for u in self.mon.snapshot()["users"])))
        st, h = self.req("/api/presence/health")
        self.assertEqual(st, 200); self.assertTrue(h["enabled"] and h["connected"]); self.assertEqual(h["subscriptions"]["ok"], 3)
        st, p = self.req("/api/presence/person?id=bob1002")
        self.assertEqual((st, p["name"], p["status"]), (200, "Bob Brown", "on-call"))
        self.assertEqual(self.req("/api/presence/person?id=nobody")[0], 404)
        st, r = self.req("/api/presence/search?q=ali")
        self.assertEqual([u["name"] for u in r["users"]], ["Alice Adams"])
        st, r = self.req("/api/presence/search?status=on-call,dnd")
        self.assertEqual(sorted(u["name"] for u in r["users"]), ["Bob Brown", "Carol Chen"])
        # availability notification: rule, transition, delivery through /api/state
        st, r = self.req("/api/presence/watch", {"user_id": "bob1002@nextiva.com", "events": ["after-call"], "name": "Bob"})
        self.assertEqual(st, 200); self.assertIn("bob1002@nextiva.com", r["rules"])
        self.mock.push("bob1002@nextiva.com", "CallReleasedEvent", call_xml("c1", "Released", "Terminator"))
        self.assertTrue(wait(lambda: self.req("/api/state")[1]["notifications"]))
        n = self.req("/api/state")[1]["notifications"][-1]
        self.assertEqual((n["event"], n["text"]), ("after-call", "Bob Brown becomes available after a call"))
        self.assertEqual(self.req("/api/presence/watch")[1]["rules"], {})            # one-shot rule consumed
        self.assertTrue(any(e["kind"] == "notify" for e in self.req("/api/state")[1]["events"]))

    def test_switch_modes_live(self):
        mgr = self.srv.ui_state.settings
        g = mgr.get()
        self.assertEqual((g["mode"], g["started_mode"]), ("presence", "presence"))
        # to phone: registers right away, but audio needs a restart
        r = mgr.save({"app": {"mode": "phone"}})
        self.assertIn("mode", r["applied"])
        self.assertIn("app.mode", r["restart_required"])
        self.assertFalse(self.phone.presence_only)
        self.assertTrue(wait(lambda: self.phone.registered), self.srv.ui_state.reg_detail)
        self.assertEqual(self.req("/api/state")[1]["mode"], "phone")
        # back to Team view only: unregisters, refuses calls
        r = mgr.save({"app": {"mode": "presence"}})
        self.assertTrue(self.phone.presence_only)
        self.assertTrue(wait(lambda: not self.phone.registered))
        self.assertEqual(r["restart_required"], [])
        self.assertEqual(self.req("/api/call", {"number": "5551000"})[0], 400)

    def test_setup_rules_per_mode(self):
        mgr = self.srv.ui_state.settings
        with self.assertRaises(ValueError):                       # presence needs a source
            mgr.save({"app": {"mode": "presence"}, "presence": {"mode": ""}})
        with self.assertRaises(ValueError):                       # the SIP BLF source is gone
            mgr.save({"presence": {"mode": "blf"}})
        with self.assertRaises(ValueError):                       # admin source needs the login
            mgr.save({"presence": {"mode": "admin"}, "admin": {"username": ""}})
        # admin source: no SIP account needed at all
        r = mgr.save({"presence": {"mode": "admin"}, "admin": {"username": "xsi.1.admin", "password": "pw"},
                      "account": {"username": "", "domain": ""}})
        self.assertIn("team", r["applied"])
        self.assertFalse(mgr.needs_setup())
        self.assertIsNotNone(self.srv.ui_state.monitor)
        config_store.update(self.p, {"admin": {"password": ""}})
        self.assertTrue(mgr.needs_setup())

    def test_app_mode_parsing(self):
        for v, want in (("", "phone"), ("phone", "phone"), ("Presence", "presence"), ("team-only", "presence")):
            self.assertEqual(app_mode(v), want)


if __name__ == "__main__":
    unittest.main()


class EventsSourceTests(unittest.TestCase):
    """The 'events' presence source through Settings and make_monitor."""

    def test_events_source_end_to_end(self):
        from tests.mock_xsi_events import MockXsi, USER as XU, PW as XPW, call_xml
        from tests.test_presence_engine import TEAM, BOB
        from sipphone.presence import manager as xsi_events
        xsi_events.HEARTBEAT = 0.5
        mock = MockXsi(TEAM, dnd={TEAM[2][0]: True})
        mock.calls[BOB] = [call_xml("c1", "Active", "Terminator", "Acme", "6165559999")]
        d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini")
        shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
        sw = MockSwitch(); sw.start()
        config_store.update(p, {"app": {"mode": "presence"}, "account": {"proxy": "127.0.0.1", "proxy_port": str(sw.port)},
                                "local": {"bind_port": "0"}, "audio": {"backend": "null"},
                                "presence": {"mode": "events", "xsi_user": XU, "xsi_password": XPW},
                                "admin": {"xsi_host": mock.base, "verify_tls": "false"}})
        loaded = load_config(p); cfg, _, adm = loaded
        self.assertEqual(adm["mode"], "events")
        phone = SIPPhone(cfg, audio=open_audio(kind="null")); phone.presence_only = True; phone.start()
        mon = make_monitor(adm, phone)
        srv = web.serve(phone, phone.audio, port=0, open_browser=False, phrases_path=f"{d}/p", contacts_path=f"{d}/c",
                        history_path=f"{d}/h", config_path=p, monitor=mon, started_mode="presence",
                        build_monitor=lambda o: make_monitor(o, phone))
        try:
            mgr = srv.ui_state.settings
            self.assertFalse(mgr.needs_setup())                              # no SIP account needed
            self.assertTrue(wait(lambda: mon.snapshot()["connected"] and
                                 {u["name"]: u["status"] for u in mon.snapshot()["users"]}.get("Bob Brown") == "on-call"))
            st = {u["name"]: u["status"] for u in mon.snapshot()["users"]}
            self.assertEqual(st["Carol Chen"], "dnd")
            self.assertEqual(type(mon).__name__, "PresenceManager")
            t = mgr.test_admin({"presence": {"mode": "events", "xsi_user": XU, "xsi_password": ""}})
            self.assertTrue(t["ok"], t); self.assertIn("Event channel connected", t["message"])
            self.assertIn("3 of 3 people monitored", t["message"])
            with self.assertRaises(ValueError):                                 # login required
                mgr.save({"presence": {"mode": "events", "xsi_user": ""}})
        finally:
            srv.ui_state.monitor.stop(); srv.shutdown(); srv.server_close(); phone.stop(); sw.stop.set(); mock.stop()
            xsi_events.HEARTBEAT = 15.0
            shutil.rmtree(d, ignore_errors=True)

"""Team-view-only mode: no registration, no calls, presence from XSI Events, UI state."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM  # noqa: E402
from tests.mock_xsi_events import MockXsi, USER as XU, PW as XPW, call_xml  # noqa: E402
from tests.test_presence_engine import TEAM, ALICE, BOB, wait  # noqa: E402
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
        self.assertTrue(self.req("/api/state")[1]["notifications"])                 # toast, not a transcript line
        self.assertFalse(any(e["kind"] == "notify" for e in self.req("/api/state")[1]["events"]))

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


class ExtensionHelloTests(PresenceModeTests):
    """A store-installed extension (unknown ID) introduces itself and joins the bridge allow-list."""
    def hello(self, ext_id, origin):
        h = {"Content-Type": "application/json"}
        if origin is not None: h["Origin"] = origin
        r = urllib.request.Request(self.base + "/api/extension/hello", data=json.dumps({"id": ext_id}).encode(),
                                   headers=h, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_no_registration_but_presence_works(self): pass   # inherited setup only

    def test_unknown_id_is_added_when_origin_matches(self):
        from sipphone.native_host import allowed_extension_ids
        import sipphone.native_host as nh
        from sipphone import cli
        new = "abcdefghijklmnopabcdefghijklmnop"
        orig_find, orig_reg = cli.find_config, nh.register_bridge
        cli.find_config = lambda explicit="": self.p       # the allow-list reads this test's config
        nh.register_bridge = lambda exe="": {"ok": True, "where": ["test"], "path": "", "note": "", "changed": True}
        try:
            self.assertNotIn(new, allowed_extension_ids())
            code, j = self.hello(new, f"chrome-extension://{new}")
            self.assertEqual(code, 200); self.assertTrue(j["ok"]); self.assertTrue(j["added"])
            self.assertIn(new, allowed_extension_ids())
            cp = config_store.read(self.p)
            self.assertIn(new, cp.get("extension", "ids"))
            code, j = self.hello(new, f"chrome-extension://{new}")        # second time: known, nothing added
            self.assertEqual(code, 200); self.assertFalse(j["added"])
        finally:
            cli.find_config = orig_find; nh.register_bridge = orig_reg

    def test_mismatched_or_missing_origin_is_refused(self):
        bad = "ppppppppppppppppppppppppppppppp" + "p"
        code, _ = self.hello(bad, "chrome-extension://abcdefghijklmnopabcdefghijklmnop")
        self.assertEqual(code, 403)
        code, _ = self.hello(bad, "http://evil.example")
        self.assertEqual(code, 403)
        code, _ = self.hello(bad, None)                   # local program without Origin: still refused
        self.assertEqual(code, 403)
        code, _ = self.hello("short", "chrome-extension://short")
        self.assertNotEqual(code, 200)
        from sipphone.native_host import allowed_extension_ids
        self.assertNotIn(bad, allowed_extension_ids())


class StreamTests(PresenceModeTests):
    """/api/stream pushes team changes the moment the engine applies them."""
    def test_no_registration_but_presence_works(self): pass

    def read_sse(self, resp, kinds, t=5.0):
        """Collect SSE messages until one of `kinds` arrives (or timeout)."""
        import socket
        end = time.time() + t
        got = []
        buf = b""
        resp.fp.raw._sock.settimeout(0.5) if hasattr(resp.fp, "raw") else None
        while time.time() < end:
            try:
                chunk = resp.fp.read1(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                msg, buf = buf.split(b"\n\n", 1)
                kind, data = "", ""
                for line in msg.decode().splitlines():
                    if line.startswith("event: "): kind = line[7:]
                    elif line.startswith("data: "): data = line[6:]
                if kind:
                    got.append((kind, json.loads(data)))
                    if (callable(kinds) and kinds(kind, got[-1][1])) or (not callable(kinds) and kind in kinds):
                        return got
        return got

    def test_stream_opens_with_snapshots_and_pushes_changes(self):
        self.assertTrue(wait(lambda: self.req("/api/monitor")[1].get("ready"), 20))
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.request("GET", "/api/stream?since=0")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        got = self.read_sse(resp, {"team"})
        kinds = [k for k, _ in got]
        self.assertEqual(kinds[:2], ["state", "team"])
        team0 = got[1][1]
        self.assertTrue(team0["full"]); self.assertEqual(len(team0["users"]), 3)
        self.assertEqual(team0["meta"]["enabled"], True)
        # push: only the changed person arrives, fast
        t0 = time.time()
        self.mock.push(BOB, "CallReleasedEvent", call_xml("c1", "Released", "Terminator"))
        got = self.read_sse(resp, lambda k, p: k == "team" and any(u["user_id"] == BOB for u in p["users"]), 5)
        took = time.time() - t0
        team = [p for k, p in got if k == "team"][-1]
        self.assertFalse(team["full"])
        self.assertEqual([u["user_id"] for u in team["users"]], [BOB])
        self.assertEqual(team["users"][0]["status"], "available")
        self.assertLess(took, 0.5, f"push took {took:.3f}s")
        print(f"\n   stream: change pushed in {took*1000:.0f} ms")
        # an event line is pushed as state with the new event
        self.srv.ui_state.add("event", "hello stream")
        got = self.read_sse(resp, lambda k, p: k == "state" and any(e["text"] == "hello stream" for e in p["events"]), 5)
        self.assertTrue(got and got[-1][0] == "state")
        # render metrics come back
        code, j = self.req("/api/presence/metrics", {"render": [0.02, 0.03]})
        self.assertEqual(code, 200)
        h = self.req("/api/presence/health")[1]
        self.assertEqual(h["latency"]["render"]["n"], 2)
        self.assertGreaterEqual(h["latency"]["engine"]["n"], 1)
        self.assertEqual(self.srv.ui_state.stream_clients(), 1)
        resp.close(); conn.close()                      # the server notices on its next keep-alive
        self.assertTrue(wait(lambda: self.srv.ui_state.stream_clients() == 0, 10))

    def test_bridge_follows_the_stream(self):
        """The native host learns of a change from the push stream (not its 1 s poll)."""
        from sipphone import native_host as nh
        self.assertTrue(wait(lambda: self.req("/api/monitor")[1].get("ready"), 20))
        old_base, nh.BASE = nh.BASE, self.base
        nh._follower = None
        sent = []
        class Ch:
            def send(self, m): sent.append((time.time(), dict(m)))
        stop = threading.Event()
        t = threading.Thread(target=nh.pump, args=(Ch(), stop, 1.0), daemon=True); t.start()
        try:
            self.assertTrue(wait(lambda: nh._follower is not None and nh._follower.fresh(), 10))
            self.assertTrue(wait(lambda: any(m.get("up") for _, m in sent), 5))
            time.sleep(1.2)                        # past the next poll tick: the pump now sleeps on the stream
            n0 = len(sent); t0 = time.time()
            self.mock.push(ALICE, "CallReceivedEvent", call_xml("r1", "Alerting", "Terminator", "Dana", "6165551234"))
            self.assertTrue(wait(lambda: any("Alice Adams" in (m.get("team") or {}).get("ringing", []) for _, m in sent[n0:]), 3))
            at = next(ts for ts, m in sent[n0:] if "Alice Adams" in (m.get("team") or {}).get("ringing", []))
            took = at - t0
            print(f"\n   bridge saw the ring {took*1000:.0f} ms after the push")
            self.assertLess(took, 0.3)
        finally:
            stop.set(); nh.BASE = old_base; nh._follower = None

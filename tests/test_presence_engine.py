"""The presence engine: directory + live XSI Events for both logins, states,
dedupe/stale handling, renewal, reconnection, health, search, notifications."""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from tests.mock_xsi_events import MockXsi, USER, PW, ADMIN, APW, call_xml  # noqa: E402
from sipphone.authentication import UserCredentials, AdminCredentials  # noqa: E402
from sipphone.presence import manager as pm  # noqa: E402
from sipphone.presence import PresenceManager, AvailabilityWatcher, search_people, filter_people  # noqa: E402
from sipphone.presence.event_processor import DocSplitter  # noqa: E402

ALICE, BOB, CAROL = "alice1001@nextiva.com", "bob1002@nextiva.com", "carol1003@nextiva.com"
TEAM = [(ALICE, "Alice", "Adams", "1001"), (BOB, "Bob", "Brown", "1002"), (CAROL, "Carol", "Chen", "1003")]


def wait(pred, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def user_creds(mock, user=USER, pw=PW):
    return UserCredentials(user, pw, host=mock.base, verify_tls=False)


class EngineTests(unittest.TestCase):
    def setUp(self):
        import logging; logging.basicConfig(level=logging.WARNING)
        pm.HEARTBEAT = 0.5
        self.mock = MockXsi(TEAM, dnd={CAROL: True})
        self.mock.calls[BOB] = [call_xml("c-bob", "Active", "Originator", "Acme Lab", "6165559999", time.time() - 40)]
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        pm.HEARTBEAT = 15.0
        self.mock.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def by_name(self, mon):
        return {u["name"]: u for u in mon.snapshot()["users"]}

    def test_user_login_live_presence(self):
        mon = PresenceManager(user_creds(self.mock), cache_path=os.path.join(self.d, "dir.json"))
        mon.start()
        self.assertTrue(wait(lambda: mon.snapshot()["ready"] and all(u["status"] != "unknown" for u in mon.snapshot()["users"])))
        snap = mon.snapshot()
        self.assertEqual((snap["source"], snap["login"], snap["total"], snap["list_source"]), ("events", "user", 3, "company directory"))
        self.assertEqual(self.mock.blf_requests, 0)
        u = self.by_name(mon)
        self.assertEqual(u["Alice Adams"]["status"], "available")
        self.assertEqual((u["Alice Adams"]["extension"], u["Alice Adams"]["department"], u["Alice Adams"]["monitor"]), ("1001", "Sales", "ok"))
        self.assertEqual(u["Bob Brown"]["status"], "on-call")
        self.assertEqual((u["Bob Brown"]["with_name"], u["Bob Brown"]["with_number"], u["Bob Brown"]["direction"]), ("Acme Lab", "6165559999", "outbound"))
        self.assertAlmostEqual(u["Bob Brown"]["since"], time.time() - 40, delta=3)
        self.assertEqual(u["Carol Chen"]["status"], "dnd"); self.assertTrue(u["Carol Chen"]["dnd"])
        self.assertFalse(snap["stale"])
        self.assertEqual(sorted(set(p for _, p in self.mock.subs.values())), ["Do Not Disturb", "Standard Call"])
        self.assertTrue(wait(lambda: len(self.mock.acks) >= 6))
        # live transitions
        self.mock.push(ALICE, "CallReceivedEvent", call_xml("c-al", "Alerting", "Terminator", "Dana", "6165551234"))
        self.assertTrue(wait(lambda: self.by_name(mon)["Alice Adams"]["status"] == "ringing"))
        self.mock.push(ALICE, "CallAnsweredEvent", call_xml("c-al", "Active", "Terminator", "Dana", "6165551234"))
        self.assertTrue(wait(lambda: self.by_name(mon)["Alice Adams"]["status"] == "on-call"))
        self.mock.push(ALICE, "CallHeldEvent", call_xml("c-al", "Held", "Terminator"))
        self.assertTrue(wait(lambda: self.by_name(mon)["Alice Adams"]["status"] == "on-hold"))
        self.assertEqual(self.by_name(mon)["Alice Adams"]["with_name"], "Dana")
        self.mock.push(ALICE, "CallReleasedEvent", call_xml("c-al", "Released", "Terminator"))
        self.assertTrue(wait(lambda: self.by_name(mon)["Alice Adams"]["status"] == "available"))
        self.mock.push(CAROL, "DoNotDisturbEvent", "<xsi:info><xsi:active>false</xsi:active></xsi:info>")
        self.assertTrue(wait(lambda: self.by_name(mon)["Carol Chen"]["status"] == "available"))
        self.assertTrue(wait(lambda: self.mock.heartbeats >= 2, 5))
        h = mon.health()
        self.assertTrue(h["connected"]); self.assertEqual(h["subscriptions"]["ok"], 3); self.assertEqual(h["reconnects"], [])
        self.assertGreater(h["subscriptions"]["next_expiry"], time.time())
        self.assertFalse(h["directory"]["from_cache"])
        # details for one person
        p = mon.person("alice1001")
        self.assertEqual((p["name"], p["monitor"], p["calls"]), ("Alice Adams", "ok", []))
        # the directory cache was written, and a fresh engine starts from it
        self.assertTrue(os.path.exists(os.path.join(self.d, "dir.json")))
        mon.stop()
        self.assertTrue(wait(lambda: not self.mock.subs and not self.mock.channels))
        mon2 = PresenceManager(user_creds(self.mock), cache_path=os.path.join(self.d, "dir.json"))
        self.assertEqual(mon2.directory.status()["people"], 3); self.assertTrue(mon2.directory.from_cache)

    def test_admin_login_same_engine_no_offline(self):
        self.mock.registrations[ALICE] = 0
        pm.REGISTRATION_SWEEP = 0.5
        try:
            creds = AdminCredentials(ADMIN, APW, host=self.mock.base, verify_tls=False, directory_user="me1001")
            mon = PresenceManager(creds, concurrency=2, registration_sweep=True)
            mon.start()
            self.assertTrue(wait(lambda: mon.snapshot()["ready"] and self.by_name(mon).get("Bob Brown", {}).get("status") == "on-call"))
            self.assertEqual(mon.snapshot()["login"], "admin")
            time.sleep(1.5)                                                         # a sweep has run
            self.assertEqual(self.by_name(mon)["Alice Adams"]["status"], "available")   # no device registered: still Available
            self.assertEqual(self.by_name(mon)["Bob Brown"]["status"], "on-call")
            self.assertTrue(mon.health()["registration_sweep"])
            mon.stop()
        finally:
            pm.REGISTRATION_SWEEP = 300.0

    def test_refused_stay_unknown_and_reconnect(self):
        self.mock.allowed.discard(CAROL)
        mon = PresenceManager(user_creds(self.mock), registration_sweep=False)
        changes = []
        mon.on_change(lambda p, o, n: changes.append((p["name"], o, n)))
        mon.start()
        self.assertTrue(wait(lambda: mon.snapshot()["ready"] and self.by_name(mon).get("Alice Adams", {}).get("status") == "available"))
        u = self.by_name(mon)
        self.assertEqual((u["Carol Chen"]["status"], u["Carol Chen"]["monitor"]), ("unknown", "not-monitored"))
        self.assertIn("Not monitored", u["Carol Chen"]["detail"])
        self.assertEqual(mon.snapshot()["refused"], 1)
        self.assertEqual(mon.snapshot()["last_error"], "")
        self.assertEqual(self.mock.registration_requests, 0)                                  # user login: no sweep
        # the switch drops the channel: last-known state is kept, flagged stale, then we come back
        for cid in list(self.mock.channels):
            self.mock.channels.pop(cid)
        self.assertTrue(wait(lambda: not mon.snapshot()["connected"], 10))
        snap = mon.snapshot()
        self.assertTrue(snap["stale"]); self.assertEqual(self.by_name(mon)["Bob Brown"]["status"], "on-call")
        self.assertTrue(self.by_name(mon)["Bob Brown"]["stale"])
        self.assertTrue(wait(lambda: mon.snapshot()["connected"] and not mon.snapshot()["stale"], 20))
        self.assertEqual(len(mon.health()["reconnects"]), 1)
        self.assertFalse(any(n == "available" and o == "unknown" and name == "Carol Chen" for name, o, n in changes))
        mon.stop()

    def test_duplicate_and_stale_events_ignored(self):
        mon = PresenceManager(user_creds(self.mock), registration_sweep=False)
        mon.start()
        self.assertTrue(wait(lambda: mon.snapshot()["ready"] and self.by_name(mon)["Alice Adams"]["status"] == "available"))
        from tests.mock_xsi_events import ev
        doc = ev(ALICE, "CallReceivedEvent", call_xml("c-x", "Alerting", "Terminator", "Dana", "555"), seq=50)
        for q in list(self.mock.channels.values()):
            q.put(doc); q.put(doc)                                              # redelivered
        self.assertTrue(wait(lambda: self.by_name(mon)["Alice Adams"]["status"] == "ringing"))
        self.assertTrue(wait(lambda: mon.processor.duplicates == 1))
        old = ev(ALICE, "CallReleasedEvent", call_xml("c-x", "Released", "Terminator"), seq=10)   # older than seq 50
        for q in list(self.mock.channels.values()):
            q.put(old)
        self.assertTrue(wait(lambda: mon.processor.stale_dropped == 1))
        self.assertEqual(self.by_name(mon)["Alice Adams"]["status"], "ringing")
        self.assertEqual(mon.health()["duplicates_dropped"], 1)
        mon.stop()

    def test_wrong_password_and_empty_directory(self):
        mon = PresenceManager(user_creds(self.mock, pw="nope"))
        mon.start()
        self.assertTrue(wait(lambda: "authentication" in mon.snapshot()["last_error"]))
        mon.stop()
        empty = MockXsi([])
        mon2 = PresenceManager(user_creds(empty))
        mon2.start()
        self.assertTrue(wait(lambda: "lists no people" in mon2.snapshot()["last_error"]))
        mon2.stop(); empty.stop()

    def test_ack_path_fallback_and_failure_is_visible(self):
        self.mock.ack_mode = "async"
        mon = PresenceManager(user_creds(self.mock), registration_sweep=False)
        mon.start()
        self.assertTrue(wait(lambda: len(self.mock.acks) >= 6))
        self.assertIn("/com.broadsoft.async/", mon.client._ack_path)
        self.assertEqual(mon.snapshot()["ack_error"], "")
        self.mock.ack_mode = "fail"
        self.mock.push(ALICE, "CallReceivedEvent", call_xml("c-al", "Alerting", "Terminator", "Dana", "6165551234"))
        self.assertTrue(wait(lambda: "HTTP 404" in mon.snapshot()["ack_error"]))
        self.assertEqual(self.by_name(mon)["Alice Adams"]["status"], "ringing")
        mon.stop()

    def test_subscription_renewal(self):
        mon = PresenceManager(user_creds(self.mock), registration_sweep=False, expires=4)
        mon.start()
        self.assertTrue(wait(lambda: mon.snapshot()["ready"]))
        self.assertTrue(wait(lambda: mon.health()["subscriptions"]["renewals"] >= 3, 10))     # renewed before lapsing
        self.assertTrue(mon.snapshot()["connected"])
        mon.stop()


class PureTests(unittest.TestCase):
    def test_splitter(self):
        s = DocSplitter()
        self.assertEqual(s.feed(b'<?xml version="1.0"?><a><b/></a><Heart'), [b'<?xml version="1.0"?><a><b/></a>'])
        self.assertEqual(s.feed(b'beat/>  <c>x</c>'), [b'<Heartbeat/>', b'<c>x</c>'])

    def test_search_and_filters(self):
        people = [{"user_id": "a@x", "name": "Alice Adams", "first": "Alice", "last": "Adams", "extension": "1001", "department": "Sales", "status": "available", "monitor": "ok"},
                  {"user_id": "b@x", "name": "Bob Brown", "first": "Bob", "last": "Brown", "extension": "1002", "department": "Support", "status": "on-call", "monitor": "ok"},
                  {"user_id": "c@x", "name": "Carol Chen", "first": "Carol", "last": "Chen", "extension": "2001", "department": "Sales", "status": "unknown", "monitor": "not-monitored"}]
        self.assertEqual([p["name"] for p in search_people(people, "ali")], ["Alice Adams"])
        self.assertEqual([p["name"] for p in search_people(people, "1002")], ["Bob Brown"])
        self.assertEqual([p["name"] for p in search_people(people, "sales")], ["Alice Adams", "Carol Chen"])
        self.assertEqual([p["name"] for p in search_people(people, "chen carol")], ["Carol Chen"])
        self.assertEqual(len(search_people(people, "")), 3)
        self.assertEqual([p["name"] for p in filter_people(people, statuses=["available", "unknown"])], ["Alice Adams", "Carol Chen"])
        self.assertEqual([p["name"] for p in filter_people(people, department="sales", monitored_only=True)], ["Alice Adams"])
        self.assertEqual([p["name"] for p in filter_people(people, on_call_only=True)], ["Bob Brown"])

    def test_availability_notifications(self):
        d = tempfile.mkdtemp(); got = []
        w = AvailabilityWatcher(os.path.join(d, "n.json"), notify=got.append, min_gap=0.2)
        w.set_rule("a@x", ["available", "after-call"], name="Alice")
        w.set_rule("b@x", ["dnd"], name="Bob", once=False)
        p = lambda uid, name, stale=False: {"user_id": uid, "name": name, "stale": stale}  # noqa: E731
        w.on_change(p("a@x", "Alice"), "unknown", "available")          # available fires; after-call doesn't (old wasn't a call)
        self.assertEqual([n["event"] for n in got], ["available"])
        self.assertEqual(w.list_rules()["a@x"]["events"], ["after-call"])  # one-shot rule consumed
        w.on_change(p("a@x", "Alice"), "on-call", "available")
        self.assertEqual([n["event"] for n in got], ["available", "after-call"])
        self.assertNotIn("a@x", w.list_rules())
        w.on_change(p("b@x", "Bob", stale=True), "available", "dnd")      # stale: never
        w.on_change(p("b@x", "Bob"), "available", "dnd")
        w.on_change(p("b@x", "Bob"), "available", "dnd")                  # within min_gap: no repeat
        self.assertEqual(len(got), 3)
        time.sleep(0.25)
        w.on_change(p("b@x", "Bob"), "available", "dnd")
        self.assertEqual(len(got), 4)
        self.assertIn("b@x", AvailabilityWatcher(os.path.join(d, "n.json")).list_rules())   # persisted
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


def big_team(n):
    return [(f"agent{i:04d}@nextiva.com", f"Agent{i}", "Smith", str(2000 + i)) for i in range(n)]


class ThroughputTests(unittest.TestCase):
    """Several channels per channel set: each one is gated on its own ack, so a
    burst of events clears N times faster. The mock gates like the Xsp does."""

    def setUp(self):
        pm.HEARTBEAT = 0.5
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        pm.HEARTBEAT = 15.0
        shutil.rmtree(self.d, ignore_errors=True)

    def run_burst(self, people, channels, events=40, rtt=0.04):
        team = big_team(people)
        mock = MockXsi(team)
        mock.ack_gate = True; mock.ack_delay = rtt
        mon = PresenceManager(user_creds(mock), cache_path=os.path.join(self.d, f"dir{channels}.json"),
                              concurrency=12, channels=channels)
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"] and len(mock.subs) == 2 * people, 60))
            self.assertTrue(wait(lambda: len(mock.acks) >= 2 * people, 60))     # initial states drained
            time.sleep(0.3)
            ids = [t[0] for t in team]
            t0 = time.time()
            for i in range(events):
                mock.push(ids[i % people], "CallReceivedEvent", call_xml(f"b{i}", "Alerting", "Terminator", "Dana", "6165551234"))
            self.assertTrue(wait(lambda: sum(1 for u in mon.snapshot()["users"] if u["status"] == "ringing") >= min(events, people), 30))
            took = time.time() - t0
            return took, mon.health(), mock
        finally:
            mon.stop(); mock.stop()

    def test_more_channels_clear_a_burst_faster(self):
        slow, h1, _ = self.run_burst(people=60, channels=1)
        fast, h4, m4 = self.run_burst(people=60, channels=4)
        self.assertEqual(h1["channels"]["open"], 1)
        self.assertEqual(h4["channels"]["open"], 4)
        # 40 gated events at 40 ms each ≈ 1.6 s on one channel; four channels ≈ 0.4 s
        print(f"\n   burst of 40 gated events: 1 channel {slow:.2f}s, 4 channels {fast:.2f}s")
        self.assertGreater(slow, 1.0, f"one channel should be ack-gated: {slow:.2f}s")
        self.assertLess(fast, slow / 2, f"four channels should be much faster: {fast:.2f}s vs {slow:.2f}s")
        lat = h4["latency"]
        self.assertGreater(lat["ack_rtt"]["n"], 30)
        self.assertGreaterEqual(lat["ack_rtt"]["p50"], 0.03)
        self.assertLess(lat["engine"]["p95"], 0.05)
        self.assertEqual(lat["channels"], 4)
        self.assertIsNotNone(lat["end_to_end_p95"])

    def test_channel_count_is_automatic_and_capped_by_the_platform(self):
        self.assertEqual(pm.auto_channels(11), 1)
        self.assertEqual(pm.auto_channels(120), 2)
        self.assertEqual(pm.auto_channels(300), 4)
        team = big_team(5)
        mock = MockXsi(team); mock.max_channels = 1
        mon = PresenceManager(user_creds(mock), cache_path=os.path.join(self.d, "cap.json"), channels=3)
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
            h = mon.health()
            self.assertEqual(h["channels"]["open"], 1)          # asked for 3, platform allowed 1
            self.assertEqual(h["channels"]["wanted"], 3)
            self.assertTrue(mon.snapshot()["connected"])
        finally:
            mon.stop(); mock.stop()

    def test_ack_path_is_remembered_across_restarts(self):
        team = big_team(3)
        mock = MockXsi(team); mock.ack_mode = "async"          # only the async path acks
        cache = os.path.join(self.d, "ack.json")
        mon = PresenceManager(user_creds(mock), cache_path=cache)
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
            mock.push(team[0][0], "CallReceivedEvent", call_xml("x", "Alerting"))
            self.assertTrue(wait(lambda: mock.acks, 5))
            self.assertTrue(wait(lambda: mon.directory.cache.get("ack_path", "").startswith("/com.broadsoft.async"), 5))
        finally:
            mon.stop()
        import json
        self.assertIn("async", json.load(open(cache))["extra"]["ack_path"])
        mock.acks.clear(); mock.ack_mode = "fail_plain_count"
        # a new engine starts with the remembered path: the first ack goes straight to the async path
        requests_before = []
        mon2 = PresenceManager(user_creds(mock), cache_path=cache)
        mon2.start()
        try:
            self.assertTrue(wait(lambda: mon2.snapshot()["ready"], 20))
            self.assertEqual(mon2.client.ack_path, "/com.broadsoft.async/com.broadsoft.xsi-events/v2.0/channel/eventresponse")
        finally:
            mon2.stop(); mock.stop()


class ClusterTests(unittest.TestCase):
    """xsi.nextiva.com-style front ends: several Xsps behind one name. Heartbeats
    and acks must reach the server holding the channel, or it answers 404."""

    def setUp(self):
        pm.HEARTBEAT = 0.3
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        pm.HEARTBEAT = 15.0
        shutil.rmtree(self.d, ignore_errors=True)

    def test_heartbeats_carry_the_node_cookie(self):
        mock = MockXsi(TEAM); mock.sticky_cookie = True
        mon = PresenceManager(user_creds(mock), cache_path=os.path.join(self.d, "c.json"))
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
            hb = mock.heartbeats
            self.assertTrue(wait(lambda: mock.heartbeats >= hb + 4, 5))
            snap = mon.snapshot()
            self.assertTrue(snap["connected"]); self.assertFalse(snap["stale"])
            self.assertEqual(mon.reconnects, [])
            self.assertEqual(mon.client.cookies.get("XSPNODE"), "node-a")
        finally:
            mon.stop(); mock.stop()

    def test_everything_after_the_channel_goes_to_the_same_server(self):
        import socket as S
        from sipphone.presence import connection as C
        mock = MockXsi(TEAM)
        mock_host = mock.base.replace("127.0.0.1", "localhost")      # a name, like xsi.nextiva.com
        seen = []
        real = S.create_connection
        def spy(addr, *a, **k):
            seen.append(addr[0]); return real(addr, *a, **k)
        C.socket.create_connection = spy
        try:
            creds = UserCredentials(USER, PW, host=mock_host, verify_tls=False)
            mon = PresenceManager(creds, cache_path=os.path.join(self.d, "p.json"))
            mon.start()
            try:
                self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
                self.assertEqual(mon.client.stream_ip, "127.0.0.1")
                n = len(seen)
                mock.push(ALICE, "CallReceivedEvent", call_xml("p1", "Alerting"))
                self.assertTrue(wait(lambda: mock.acks, 5))
                time.sleep(0.8)                                       # heartbeats
                self.assertTrue(seen[n:] == [] or set(seen[n:]) == {"127.0.0.1"}, seen[n:])
            finally:
                mon.stop()
        finally:
            C.socket.create_connection = real
            mock.stop()


class GroupNameTests(unittest.TestCase):
    """Groups found in the directory can be given display names in Settings."""

    def test_rename_applies_everywhere_and_resets(self):
        d = tempfile.mkdtemp()
        HG = "hg_sales@nextiva.com"; CC = "cc_support@nextiva.com"
        mock = MockXsi(TEAM, directory=TEAM + [(HG, "hg_sales", "Hunt Group", "1100"),
                                               (CC, "cc_support", "Call Center", "1200")])
        mon = PresenceManager(user_creds(mock), cache_path=os.path.join(d, "directory-cache.json"),
                              show_services=["hunt_group"])
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
            groups = {g["user_id"]: g for g in mon.groups()}
            self.assertIn(HG, groups); self.assertIn(CC, groups)        # every group found, listed or not
            self.assertTrue(groups[HG]["shown"]); self.assertFalse(groups[CC]["shown"])
            before = [s["name"] for s in mon.snapshot()["services"]]
            fired = []
            mon.on_update(lambda: fired.append(1))
            mon.rename_group(HG, "  Sales   line ")
            self.assertTrue(fired)                                       # the push stream is told
            svc = mon.snapshot()["services"][0]
            self.assertEqual(svc["name"], "Sales line")
            self.assertEqual(svc["original_name"], before[0])
            self.assertTrue(svc["renamed"])
            self.assertTrue(os.path.exists(os.path.join(d, "group-names.json")))
            # a fresh engine (restart) reads the saved name
            from sipphone.presence.group_names import GroupNames
            self.assertEqual(GroupNames(os.path.join(d, "group-names.json")).get(HG), "Sales line")
            mon.rename_group(HG, "")
            self.assertEqual(mon.snapshot()["services"][0]["name"], before[0])
            self.assertNotIn("renamed", mon.snapshot()["services"][0])
        finally:
            mon.stop(); mock.stop()
            shutil.rmtree(d, ignore_errors=True)


class OrgGroupNameTests(unittest.TestCase):
    def test_people_group_id_gets_a_display_name(self):
        d = tempfile.mkdtemp()
        mock = MockXsi(TEAM); mock.groups = {ALICE: "3149647G", BOB: "3149647G", CAROL: "5550001G"}
        mon = PresenceManager(user_creds(mock), cache_path=os.path.join(d, "directory-cache.json"))
        mon.start()
        try:
            self.assertTrue(wait(lambda: mon.snapshot()["ready"], 20))
            g = {x["id"]: x for x in mon.org_groups()}
            self.assertEqual(g["3149647G"]["members"], 2); self.assertFalse(g["3149647G"]["renamed"])
            mon.rename_org_group("3149647G", "Arizona")
            users = {u["user_id"]: u for u in mon.snapshot()["users"]}
            self.assertEqual(users[ALICE]["group"], "Arizona"); self.assertEqual(users[ALICE]["group_id"], "3149647G")
            self.assertEqual(users[CAROL]["group"], "5550001G")
            self.assertEqual(mon.person(BOB)["group"], "Arizona")
            from sipphone.presence.search import search_people
            self.assertEqual({u["user_id"] for u in search_people(mon.snapshot()["users"], "arizona")}, {ALICE, BOB})
            mon.rename_org_group("3149647G", "")
            self.assertEqual({u["user_id"]: u for u in mon.snapshot()["users"]}[ALICE]["group"], "3149647G")
        finally:
            mon.stop(); mock.stop(); shutil.rmtree(d, ignore_errors=True)

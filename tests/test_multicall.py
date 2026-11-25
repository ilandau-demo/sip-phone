"""Several calls at once: up to three calls on a line, call waiting on a busy
line, one call with the audio (every other call is held and stays held), a
consultation call tied to the call it was placed for, mute per call, and the
same rules across lines and through the web API."""
import dataclasses
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sipphone import web                                                      # noqa: E402
from sipphone.lines import Lines                                              # noqa: E402
from sipphone.phone import MAX_CALLS_PER_LINE, CallState, Config, SIPPhone   # noqa: E402
from tests.test_flows import AUTH, PASS, REALM, USER, CapturingAudio, MockSwitch   # noqa: E402

logging.basicConfig(level=logging.WARNING)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def switch():
    sw = MockSwitch()
    sw.answer_delay = 0.05
    sw.start()
    return sw


def config(sw):
    return Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH,
                  proxy="127.0.0.1", proxy_port=sw.port, bind_ip="127.0.0.1", bind_port=0,
                  register_expires=120, keepalive_seconds=1, rtp_port_range=(33000, 34000))


def talking(calls):
    return [c for c in calls if c.state == CallState.CONNECTED and not c.held]


def ring(sw, phone, n):
    """Have the switch call the phone; returns the new ringing call once there are n live calls."""
    before = set(phone.calls)
    sw.out_event.clear()
    sw.out_final = None
    sw.call_phone()
    assert wait_for(lambda: len(phone.live_calls()) == n), "the call did not ring"
    return next(c for cid, c in phone.calls.items() if cid not in before)


class OneLineTests(unittest.TestCase):
    def setUp(self):
        self.sw = switch()
        self.audio = CapturingAudio()
        self.audio.mic_muted = False
        self.phone = SIPPhone(config(self.sw), audio=self.audio)
        self.phone.start()
        self.assertTrue(self.phone.register(wait=True))

    def tearDown(self):
        self.phone.stop()
        self.sw.stop.set()

    def dial(self, number):
        c = self.phone.call(number)
        self.assertTrue(wait_for(lambda: c.state == CallState.CONNECTED), f"{number} did not connect")
        self.assertTrue(wait_for(lambda: self.phone.router.foreground == {c.call_id}), "the new call should have the audio")
        return c

    def test_three_outbound_calls_then_the_line_is_full(self):
        self.assertEqual((MAX_CALLS_PER_LINE, self.phone.max_calls), (3, 3))
        c1, c2, c3 = self.dial("5551001"), self.dial("5551002"), self.dial("5551003")
        self.assertTrue(wait_for(lambda: c1.local_hold and c2.local_hold))
        self.assertEqual(talking(self.phone.live_calls()), [c3])
        self.assertIs(self.phone.focus_call(), c3)
        self.assertIn("sendonly", self.sw.reinvites)
        with self.assertRaises(RuntimeError) as e:
            self.phone.call("5551004")
        self.assertIn("already has 3 calls", str(e.exception))
        self.assertEqual(len(self.phone.live_calls()), 3)

    def test_call_waiting_and_a_fourth_caller_gets_busy(self):
        c1 = self.dial("5551001")
        w1 = ring(self.sw, self.phone, 2)
        self.assertEqual(w1.state, CallState.INCOMING)                # rings instead of 486
        self.assertEqual(talking(self.phone.live_calls()), [c1])    # ringing does not touch the talking call
        w2 = ring(self.sw, self.phone, 3)
        self.assertEqual(w2.state, CallState.INCOMING)
        self.sw.out_event.clear()
        self.sw.out_final = None
        self.sw.call_phone()                                          # a fourth
        self.assertTrue(self.sw.out_event.wait(3))
        self.assertEqual(self.sw.out_final.status, 486)
        self.assertEqual(len(self.phone.live_calls()), 3)
        self.phone.answer(w2.call_id)
        self.assertTrue(wait_for(lambda: w2.state == CallState.CONNECTED and c1.local_hold))
        self.assertTrue(wait_for(lambda: self.phone.router.foreground == {w2.call_id}))
        self.assertEqual(w1.state, CallState.INCOMING)                # still ringing

    def test_hanging_up_leaves_the_others_on_hold(self):
        c1, c2 = self.dial("5551001"), self.dial("5551002")
        self.assertTrue(wait_for(lambda: c1.local_hold))
        self.phone.hangup(c2.call_id)
        self.assertTrue(wait_for(lambda: c2.state == CallState.ENDED))
        time.sleep(0.4)
        self.assertTrue(c1.held and c1.state == CallState.CONNECTED)   # nothing resumes by itself
        self.assertEqual(self.phone.router.foreground, set())
        self.assertIs(self.phone.focus_call(), c1)
        self.phone.resume(c1.call_id)
        self.assertTrue(wait_for(lambda: not c1.held and self.phone.router.foreground == {c1.call_id}))

    def test_a_call_takes_the_audio_when_it_connects(self):
        c1 = self.dial("5551001")
        self.sw.answer_delay = 0.6
        c2 = self.phone.call("5551002")
        self.assertTrue(wait_for(lambda: c2.state == CallState.CONNECTED, 5))
        self.assertTrue(wait_for(lambda: c1.local_hold and self.phone.router.foreground == {c2.call_id}))
        w = ring(self.sw, self.phone, 3)
        self.assertEqual(self.phone.router.foreground, {c2.call_id})
        self.phone.answer(w.call_id)
        self.assertTrue(wait_for(lambda: w.state == CallState.CONNECTED and c2.local_hold))
        self.assertTrue(wait_for(lambda: self.phone.router.foreground == {w.call_id}))
        self.assertEqual(talking(self.phone.live_calls()), [w])

    def test_a_refused_hold_still_frees_the_audio(self):
        c1 = self.dial("5551001")
        normal = self.sw._on_request

        def refuse_reinvite(req, addr):
            if req.method == "INVITE" and req.to_addr.tag:
                return self.sw._reply(req, 488, addr, to_tag=False)
            return normal(req, addr)
        self.sw._on_request = refuse_reinvite
        self.phone.hold(c1.call_id)
        self.assertTrue(c1.held)                                      # at once, before the far end answers
        self.assertNotIn(c1.call_id, self.phone.router.foreground)
        self.assertTrue(wait_for(lambda: c1.local_hold), "the hold stays on this side when the far end refuses")
        self.assertEqual(c1.state, CallState.CONNECTED)
        self.sw._on_request = normal
        c2 = self.dial("5551002")
        self.assertEqual(talking(self.phone.live_calls()), [c2])

    def test_a_consultation_call_belongs_to_its_call(self):
        c1 = self.dial("5551001")
        cons = self.phone.consult("5551002")
        self.assertTrue(wait_for(lambda: cons.state == CallState.CONNECTED and c1.local_hold))
        self.assertEqual((cons.role, cons.parent), ("consult", c1.call_id))
        self.assertEqual(self.phone.pair_of(cons), (c1, cons))
        self.assertEqual(self.phone.pair_of(c1), (c1, cons))
        self.assertIs(self.phone.primary_call(), c1)
        self.assertIs(self.phone.consult_call(), cons)
        c3 = self.dial("5551003")                                     # the consultation leg is one of the three
        self.assertEqual(self.phone.pair_of(c3), (c3, None))
        with self.assertRaises(RuntimeError):
            self.phone.call("5551004")
        self.phone.hangup(c1.call_id)
        self.assertTrue(wait_for(lambda: c1.state == CallState.ENDED))
        self.assertTrue(wait_for(lambda: (cons.role, cons.parent) == ("primary", None)), "an orphaned consultation call becomes a normal call")

    def test_another_call_parks_a_conference(self):
        c1 = self.dial("5551001")
        cons = self.phone.consult("5551002")
        self.assertTrue(wait_for(lambda: cons.state == CallState.CONNECTED and c1.local_hold))
        self.phone.conference()
        self.assertTrue(wait_for(lambda: self.phone.router.foreground == {c1.call_id, cons.call_id} and not c1.held and not cons.held))
        c3 = self.dial("5551003")
        self.assertTrue(wait_for(lambda: c1.local_hold and cons.local_hold))
        self.assertEqual(talking(self.phone.live_calls()), [c3])

    def test_mute_follows_the_call(self):
        c1 = self.dial("5551001")
        self.phone.mute(True, c1.call_id)
        self.assertTrue(c1.muted and self.audio.mic_muted)
        c2 = self.dial("5551002")
        self.assertTrue(wait_for(lambda: not self.audio.mic_muted), "the new call is not muted")
        self.phone.mute(True, c1.call_id)                             # muting a held call waits for it
        self.assertFalse(self.audio.mic_muted)
        self.phone.resume(c1.call_id)
        self.assertTrue(wait_for(lambda: not c1.held and c2.local_hold))
        self.assertTrue(wait_for(lambda: self.audio.mic_muted), "the mute comes back with its call")

    def test_only_the_call_with_the_audio_is_in_the_foreground(self):
        c1, c2 = self.dial("5551001"), self.dial("5551002")
        self.assertTrue(wait_for(lambda: c1.local_hold))
        for _ in range(3):
            self.phone.resume(c1.call_id)
            self.assertTrue(wait_for(lambda: self.phone.router.foreground == {c1.call_id} and c2.local_hold and not c1.held))
            self.phone.resume(c2.call_id)
            self.assertTrue(wait_for(lambda: self.phone.router.foreground == {c2.call_id} and c1.local_hold and not c2.held))
        self.assertEqual(talking(self.phone.live_calls()), [c2])


class TwoLines(unittest.TestCase):
    def setUp(self):
        self.sw1, self.sw2 = switch(), switch()
        self.audio = CapturingAudio()
        base = config(self.sw1)
        p1 = SIPPhone(base, audio=self.audio)
        p1.start()
        self.lines = Lines(p1, label="Front desk")
        self.lines.set_line(2, dataclasses.replace(base, proxy_port=self.sw2.port, display_name="Sales demo"), "Sales demo")
        self.assertTrue(p1.register(wait=True))
        self.assertTrue(wait_for(lambda: self.lines.lines[2].phone.registered))
        self.p1, self.p2 = p1, self.lines.lines[2].phone

    def tearDown(self):
        self.lines.stop()
        self.sw1.stop.set()
        self.sw2.stop.set()

    def dial(self, number, line):
        c = self.lines.call(number, line=line)
        self.assertTrue(wait_for(lambda: c.state == CallState.CONNECTED), f"{number} did not connect")
        self.assertTrue(wait_for(lambda: self.lines.router.foreground == {c.call_id}))
        return c

    def live(self):
        return [c for c in self.lines.calls.values() if c.state != CallState.ENDED]


class LinesTests(TwoLines):
    def test_every_line_dials_while_the_others_are_busy(self):
        a = self.dial("5551001", 1)
        b = self.dial("5552001", 2)
        self.assertTrue(wait_for(lambda: a.local_hold))
        c = self.dial("5551002", 1)                                   # back on line 1, which already holds a call
        self.assertTrue(wait_for(lambda: b.local_hold))
        d = self.dial("5552002", 2)
        self.assertTrue(wait_for(lambda: c.local_hold))
        self.assertEqual(talking(self.live()), [d])
        self.assertIs(self.lines.focused_call(), d)
        self.assertEqual([(i["calls"], i["max_calls"]) for i in self.lines.info()], [(2, 3), (2, 3)])
        self.assertEqual((a.line_n, b.line_n, c.line_n, d.line_n), (1, 2, 1, 2))

    def test_inbound_on_any_line_three_per_line(self):
        b = self.dial("5552001", 2)
        rings = [ring(self.sw1, self.p1, n) for n in (1, 2, 3)]        # three callers on line 1 while talking on line 2
        self.assertTrue(all(c.state == CallState.INCOMING for c in rings))
        self.assertEqual(talking(self.live()), [b])
        self.sw1.out_event.clear()
        self.sw1.out_final = None
        self.sw1.call_phone()
        self.assertTrue(self.sw1.out_event.wait(3))
        self.assertEqual(self.sw1.out_final.status, 486)             # line 1 is full
        w = ring(self.sw2, self.p2, 2)                                # line 2 still has room
        self.assertEqual(w.state, CallState.INCOMING)
        self.lines.answer(rings[0].call_id)
        self.assertTrue(wait_for(lambda: rings[0].state == CallState.CONNECTED and b.local_hold))
        self.assertTrue(wait_for(lambda: self.lines.router.foreground == {rings[0].call_id}))
        self.assertEqual([i["calls"] for i in self.lines.info()], [3, 2])

    def test_transfer_and_conference_on_one_line_leave_the_other_held(self):
        a = self.dial("5551001", 1)
        b = self.dial("5552001", 2)
        self.assertTrue(wait_for(lambda: a.local_hold))
        cons = self.lines.consult("5552002")
        self.assertTrue(wait_for(lambda: cons.state == CallState.CONNECTED and b.local_hold))
        self.assertEqual(cons.line_n, 2)
        self.assertEqual(self.lines.pair_of(cons), (b, cons))
        self.lines.conference()
        self.assertTrue(wait_for(lambda: self.lines.router.foreground == {b.call_id, cons.call_id}))
        self.assertTrue(a.local_hold and a.state == CallState.CONNECTED)
        self.lines.resume(a.call_id)                                  # going back to line 1 parks the three-way
        self.assertTrue(wait_for(lambda: b.local_hold and cons.local_hold and not a.held))
        self.assertTrue(wait_for(lambda: self.lines.router.foreground == {a.call_id}))


class ApiTests(TwoLines):
    def setUp(self):
        super().setUp()
        d = tempfile.mkdtemp()
        self.srv = web.serve(self.lines, self.lines.audio, port=0, open_browser=False, phrases_path=f"{d}/p",
                             contacts_path=f"{d}/c", history_path=f"{d}/h")
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def state(self):
        with urllib.request.urlopen(self.base + "/api/state", timeout=5) as r:
            return json.loads(r.read())

    def test_dial_from_any_line_during_a_call(self):
        self.assertEqual(self.post("/api/call", {"number": "5551001", "line": 1})[0], 200)
        self.assertTrue(wait_for(lambda: self.state().get("talking")))
        self.assertEqual(self.post("/api/call", {"number": "5552001", "line": 2})[0], 200)
        self.assertTrue(wait_for(lambda: (self.state().get("call") or {}).get("number") == "5552001"
                                 and self.state()["call"]["state"] == "connected" and self.state()["talking"]))
        s = self.state()
        self.assertEqual(s["max_calls"], 3)
        self.assertEqual([(l["calls"], l["max_calls"]) for l in s["lines"]], [(1, 3), (1, 3)])
        self.assertTrue(wait_for(lambda: [(o["number"], o["local_hold"]) for o in self.state()["other_calls"]] == [("5551001", True)]))
        self.assertEqual(self.state()["other_calls"][0]["line"], "Front desk")

    def test_hang_up_without_an_id_ends_only_the_call_on_screen(self):
        a = self.dial("5551001", 1)
        b = self.dial("5552001", 2)
        self.assertTrue(wait_for(lambda: a.local_hold))
        self.assertEqual(self.post("/api/hangup")[0], 200)
        self.assertTrue(wait_for(lambda: b.state == CallState.ENDED))
        time.sleep(0.3)
        self.assertTrue(a.state == CallState.CONNECTED and a.held)
        s = self.state()
        self.assertFalse(s["talking"])
        self.assertEqual((s["call"]["number"], s["call"]["local_hold"], s["other_calls"]), ("5551001", True, []))
        self.assertEqual(self.post("/api/hold", {"call_id": a.call_id})[0], 200)      # the same button resumes
        self.assertTrue(wait_for(lambda: not a.held and self.state()["talking"]))
        self.assertEqual(self.post("/api/hangup")[0], 200)
        self.assertTrue(wait_for(lambda: a.state == CallState.ENDED))
        self.assertEqual(self.post("/api/hangup")[0], 200)                             # nothing to end: no error

    def test_a_full_line_says_so(self):
        for n in ("5552001", "5552002", "5552003"):
            self.dial(n, 2)
        status, body = self.post("/api/call", {"number": "5552004", "line": 2})
        self.assertGreaterEqual(status, 400)
        self.assertIn("already has 3 calls", body.get("error", ""))
        self.assertEqual(self.post("/api/call", {"number": "5551001", "line": 1})[0], 200)   # another line still dials
        self.assertTrue(wait_for(lambda: len(self.live()) == 4))


if __name__ == "__main__":
    unittest.main()

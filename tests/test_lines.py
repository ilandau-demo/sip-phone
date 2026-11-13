"""Several SIP accounts at once: each line registers on its own, calls ring on
any line, one call has the audio (answering or dialling on another line puts
the others on hold), Re-register starts clean, failed registrations retry."""
import dataclasses
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sipphone.lines import Lines, SharedAudio, line_configs                  # noqa: E402
from sipphone.phone import CallState, Config, SIPPhone                     # noqa: E402
from tests.test_flows import AUTH, PASS, REALM, USER, CapturingAudio, MockSwitch   # noqa: E402

logging.basicConfig(level=logging.WARNING)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class LinesTests(unittest.TestCase):
    def setUp(self):
        self.sw1, self.sw2 = MockSwitch(), MockSwitch()
        self.sw1.start(); self.sw2.start()
        self.audio = CapturingAudio()
        self.base = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH,
                           proxy="127.0.0.1", proxy_port=self.sw1.port, bind_ip="127.0.0.1", bind_port=0,
                           register_expires=120, keepalive_seconds=1, rtp_port_range=(31000, 32000))
        p1 = SIPPhone(self.base, audio=self.audio)
        p1.start()
        self.lines = Lines(p1, label="Front desk")
        cfg2 = dataclasses.replace(self.base, proxy_port=self.sw2.port, display_name="Sales demo")
        self.lines.set_line(2, cfg2, "Sales demo")
        self.events = []
        self.lines.on_call_state = lambda c: self.events.append((getattr(c, "line_n", 0), c.state))

    def tearDown(self):
        self.lines.stop()
        self.sw1.stop.set(); self.sw2.stop.set()

    def test_each_line_registers_and_shares_one_audio_device(self):
        self.assertTrue(self.lines.primary.register(wait=True))
        self.assertTrue(wait_for(lambda: self.lines.lines[2].phone.registered))
        info = self.lines.info()
        self.assertEqual([i["label"] for i in info], ["Front desk", "Sales demo"])
        self.assertTrue(all(i["registered"] for i in info))
        self.assertIsInstance(self.lines.audio, SharedAudio)
        self.assertIs(self.lines.lines[2].phone.router, self.lines.primary.router)
        self.assertEqual(self.sw1.register_count > 0, True); self.assertTrue(self.sw2.register_count > 0)

    def test_answering_a_second_line_holds_the_first(self):
        self.lines.primary.register(wait=True)
        self.assertTrue(wait_for(lambda: self.lines.lines[2].phone.registered))
        # a call on line 1
        rang = []
        self.lines.on_incoming_call = rang.append
        self.sw1.call_phone()
        self.assertTrue(wait_for(lambda: len(rang) == 1))
        self.lines.answer()
        c1 = rang[0]
        self.assertTrue(wait_for(lambda: c1.state == CallState.CONNECTED))
        self.assertEqual(c1.line_n, 1)
        self.assertIn(c1.call_id, self.lines.router.foreground)
        # line 2 rings while we talk on line 1 — call waiting, not busy
        self.sw2.call_phone()
        self.assertTrue(wait_for(lambda: len(rang) == 2))
        c2 = rang[1]
        self.assertEqual((c2.line_n, c2.state), (2, CallState.INCOMING))
        self.assertIs(self.lines.focused_call(), c1)                     # still talking on line 1
        self.lines.answer(c2.call_id)
        self.assertTrue(wait_for(lambda: c2.state == CallState.CONNECTED))
        self.assertTrue(wait_for(lambda: c1.local_hold, 3), "line 1 should be put on hold")
        self.assertEqual(self.lines.router.foreground, {c2.call_id})       # only one call has the audio
        self.assertIs(self.lines.focused_call(), c2)
        self.assertTrue(any("sendonly" in r for r in self.sw1.reinvites))
        # resume line 1 → line 2 goes on hold
        self.lines.resume(c1.call_id)
        self.assertTrue(wait_for(lambda: not c1.local_hold and c2.local_hold, 3))
        self.assertTrue(wait_for(lambda: self.lines.router.foreground == {c1.call_id}, 3))
        # hang up line 2 by id; line 1 stays
        self.lines.hangup(c2.call_id)
        self.assertTrue(wait_for(lambda: c2.state == CallState.ENDED))
        self.assertEqual(c1.state, CallState.CONNECTED)

    def test_dialling_out_on_a_chosen_line(self):
        self.lines.primary.register(wait=True)
        self.assertTrue(wait_for(lambda: self.lines.lines[2].phone.registered))
        self.lines.select(2)
        self.assertEqual(self.lines.cfg.display_name, "Sales demo")       # current line drives the header
        call = self.lines.call("5551234")
        self.assertIn(call.call_id, self.lines.lines[2].phone.calls)
        self.assertTrue(wait_for(lambda: call.state == CallState.CONNECTED, 5))
        self.assertEqual(call.line_label, "Sales demo")
        call1 = self.lines.call("5559999", line=1)                          # explicit line → line 1, line 2 held
        self.assertIn(call1.call_id, self.lines.primary.calls)
        self.assertTrue(wait_for(lambda: call.local_hold, 3))

    def test_reregister_starts_clean_and_reports(self):
        self.assertTrue(self.lines.primary.register(wait=True))
        old_id = self.lines.primary.register_call_id
        self.lines.primary._auth_cache["stale"] = {"realm": "stale"}
        ok, detail = self.lines.reregister(1)
        self.assertTrue(ok, detail)
        self.assertIn("expires", detail)
        self.assertNotEqual(self.lines.primary.register_call_id, old_id)
        self.assertNotIn("stale", self.lines.primary._auth_cache)

    def test_failed_registration_retries_by_itself(self):
        p = self.lines.lines[2].phone
        self.assertTrue(wait_for(lambda: p.registered))
        good = p.cfg.password
        p.cfg.password = "wrong"
        ok, detail = p.reregister(timeout=5)
        self.assertFalse(ok)
        self.assertIn("authentication failed", detail)
        self.assertGreater(p._reg_retry_at, time.time())
        p.cfg.password = good
        p._reg_retry_at = time.time()                     # don't wait 30 s in a test
        self.assertTrue(wait_for(lambda: p.registered, 8), "should register again without a click")

    def test_removing_a_line(self):
        self.assertTrue(wait_for(lambda: self.lines.lines[2].phone.registered))
        self.lines.select(2)
        self.lines.set_line(2, None)
        self.assertNotIn(2, self.lines.lines)
        self.assertEqual(self.lines.current, 1)


class LineConfigTests(unittest.TestCase):
    def test_line_sections_become_configs(self):
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string("""
[line2]
label = Sales demo
username = 2485550002
password = pw2
domain = nextiva.com
[line3]
enabled = false
username = 2485550003
password = pw3
[line4]
label = No user
[line5]
username = 2485550005
password = pw5
""")
        base = Config(username="2485550001", password="pw1", domain="nextiva.com", bind_port=5060)
        got = line_configs(cp, base)
        self.assertEqual(sorted(got), [2, 5])
        c2, label = got[2]
        self.assertEqual((c2.username, c2.password, c2.domain, c2.proxy, c2.bind_port, label),
                         ("2485550002", "pw2", "nextiva.com", "nextiva.com", 0, "Sales demo"))
        self.assertEqual(got[5][0].domain, "nextiva.com")                  # falls back to line 1's domain
        self.assertEqual(got[5][0].auth_username, "2485550005")


if __name__ == "__main__":
    unittest.main()

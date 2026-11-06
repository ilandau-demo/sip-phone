"""End-to-end tests against a mock registrar / B2BUA on localhost.

The mock behaves like a BroadSoft-style platform: every REGISTER and INVITE
is challenged with 401 Digest (MD5, qop=auth), Via gets received/rport, and
answered calls echo RTP back.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sipphone import digest, g711, message, sdp  # noqa: E402
from sipphone.message import Address, Request, Response, rand_token  # noqa: E402
from sipphone.phone import CallState, Config, SIPPhone  # noqa: E402
from sipphone.rtp import build_header, parse_packet  # noqa: E402

USER, AUTH, PASS, REALM = "2485551234", "2485551234_auth", "s3cret", "nextiva.com"


class MockSwitch(threading.Thread):
    """Minimal registrar + UAS + UAC for tests."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.registered_contact = None
        self.register_count = 0
        self.invite_auth_ok = False
        self.bye_received = threading.Event()
        self.cancel_received = threading.Event()
        self.ack_received = threading.Event()
        self.events = []
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(("127.0.0.1", 0))
        self.rtp_sock.settimeout(0.2)
        self.rtp_port = self.rtp_sock.getsockname()[1]
        self.rtp_remote = None
        self.rtp_rx = 0
        self.answer_delay = 0.3
        self.challenge_invites = True
        self.reject_with = None  # e.g. 486
        self.reinvites = []
        self.refer_to = ""
        self.transfer_result = 200
        # for outbound (switch -> phone) calls
        self.out_final = None
        self.out_event = threading.Event()
        self.phone_addr = None

    def run(self) -> None:
        threading.Thread(target=self._rtp_echo, daemon=True).start()
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            if data.strip() == b"":
                continue
            msg = message.parse(data)
            if isinstance(msg, Request):
                self.events.append(msg.method)
                self._on_request(msg, addr)
            else:
                self._handle_response(msg, addr)

    def _rtp_echo(self) -> None:
        while not self.stop.is_set():
            try:
                data, addr = self.rtp_sock.recvfrom(2048)
            except socket.timeout:
                continue
            self.rtp_rx += 1
            self.rtp_remote = addr
            if getattr(self, "rtp_log", None) is not None:
                self.rtp_log.append(data)
            self.rtp_sock.sendto(data, addr)  # echo

    # --- helpers -------------------------------------------------------
    def _reply(self, req: Request, status: int, addr, extra=None, body=b"", ctype="", to_tag=True):
        r = Response(status)
        via = req.headers.get("Via", "")
        if "rport" in via and "received" not in via:
            via = via.replace(";rport", f";received={addr[0]};rport={addr[1]}")
        r.headers.add("Via", via)
        r.headers.add("From", req.headers.get("From", ""))
        to = req.headers.get("To", "")
        if to_tag and "tag=" not in to and status != 100:
            to += f";tag={rand_token(8)}"
        r.headers.add("To", to)
        r.headers.add("Call-ID", req.call_id)
        r.headers.add("CSeq", req.headers.get("CSeq", ""))
        for k, v in (extra or {}).items():
            r.headers.add(k, v)
        if body:
            r.headers.add("Content-Type", ctype)
        r.body = body
        self.sock.sendto(r.to_bytes(), addr)
        return r

    def _challenge(self, req, addr):
        self._reply(req, 401, addr, {"WWW-Authenticate":
                    f'Digest realm="{REALM}", nonce="{rand_token(16)}", algorithm=MD5, qop="auth"'},
                    to_tag=False)

    def _authed(self, req) -> bool:
        a = req.headers.get("Authorization")
        if not a:
            return False
        p = digest.parse_challenge(a)
        return p.get("username") == AUTH and digest.verify(a, PASS, req.method, req.body)

    # --- request handling ---------------------------------------------
    def _on_request(self, req: Request, addr) -> None:
        m = req.method
        if m == "REGISTER":
            if not self._authed(req):
                return self._challenge(req, addr)
            self.register_count += 1
            exp = int(req.headers.get("Expires", "3600"))
            c = req.headers.get("Contact", "")
            self.registered_contact = c if exp else None
            self.phone_addr = addr
            self._reply(req, 200, addr, {"Contact": f"{c};expires={exp}", "Expires": exp}, to_tag=False)
        elif m == "INVITE":
            if req.to_addr.tag:  # re-INVITE inside an existing dialog
                self.reinvites.append(sdp.parse(req.body).direction)
                offer = sdp.parse(req.body)
                ans_dir = {"sendonly": "recvonly", "recvonly": "sendonly"}.get(offer.direction, "sendrecv")
                body = sdp.build_answer("127.0.0.1", self.rtp_port, offer, ans_dir)
                self._reply(req, 200, addr, {"Contact": f"<sip:switch@127.0.0.1:{self.port}>"},
                            body=body, ctype="application/sdp", to_tag=False)
                return
            if self.challenge_invites and not self._authed(req):
                return self._challenge(req, addr)
            self.invite_auth_ok = True
            threading.Thread(target=self._answer_invite, args=(req, addr), daemon=True).start()
        elif m == "ACK":
            self.ack_received.set()
        elif m == "CANCEL":
            self.cancel_received.set()
            self._reply(req, 200, addr, to_tag=False)
        elif m == "BYE":
            self.bye_received.set()
            self._reply(req, 200, addr)
        elif m == "OPTIONS":
            self._reply(req, 200, addr)
        elif m == "REFER":
            self.refer_to = req.headers.get("Refer-To", "")
            self._reply(req, 202, addr, to_tag=False)
            # report the transfer outcome like a real switch would
            def notify():
                time.sleep(0.2)
                n = Request("NOTIFY", f"sip:{USER}@{addr[0]}:{addr[1]}")
                n.headers.add("Via", f"SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK{rand_token(12)}")
                n.headers.add("From", req.headers.get("To"))
                n.headers.add("To", req.headers.get("From"))
                n.headers.add("Call-ID", req.call_id)
                n.headers.add("CSeq", "1 NOTIFY")
                n.headers.add("Event", "refer")
                n.headers.add("Subscription-State", "terminated;reason=noresource")
                n.headers.add("Content-Type", "message/sipfrag;version=2.0")
                n.body = f"SIP/2.0 {self.transfer_result} OK\r\n".encode()
                self.sock.sendto(n.to_bytes(), addr)
            threading.Thread(target=notify, daemon=True).start()
        else:
            self._reply(req, 501, addr)

    def _answer_invite(self, req: Request, addr) -> None:
        self._reply(req, 100, addr, to_tag=False)
        tag = rand_token(8)
        req.headers.set("To", f"{req.headers.get('To')};tag={tag}")
        self._reply(req, 180, addr, to_tag=False)
        if self.cancel_received.wait(self.answer_delay):
            self._reply(req, 487, addr, to_tag=False)
            return
        if self.reject_with:
            self._reply(req, self.reject_with, addr, to_tag=False)
            return
        offer = sdp.parse(req.body)
        self.rtp_remote = (offer.ip, offer.port)
        body = sdp.build_answer("127.0.0.1", self.rtp_port, offer)
        self._reply(req, 200, addr, {"Contact": f"<sip:switch@127.0.0.1:{self.port}>"},
                    body=body, ctype="application/sdp", to_tag=False)

    # --- switch-originated call -----------------------------------------
    def call_phone(self) -> None:
        assert self.phone_addr
        self.out_call_id = f"{rand_token(12)}@switch"
        self.out_tag = rand_token(8)
        inv = Request("INVITE", f"sip:{USER}@{self.phone_addr[0]}:{self.phone_addr[1]}")
        inv.headers.add("Via", f"SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK{rand_token(12)}")
        inv.headers.add("Max-Forwards", 70)
        inv.headers.add("From", f'"Alice" <sip:5550001111@{REALM}>;tag={self.out_tag}')
        inv.headers.add("To", f"<sip:{USER}@{REALM}>")
        inv.headers.add("Call-ID", self.out_call_id)
        inv.headers.add("CSeq", "1 INVITE")
        inv.headers.add("Contact", f"<sip:switch@127.0.0.1:{self.port}>")
        inv.headers.add("P-Asserted-Identity", f'"Alice" <sip:5550001111@{REALM}>')
        inv.headers.add("Content-Type", "application/sdp")
        inv.body = sdp.build_offer("127.0.0.1", self.rtp_port)
        self.out_invite = inv
        self.sock.sendto(inv.to_bytes(), self.phone_addr)

    def _handle_response(self, resp: Response, addr) -> None:
        if resp.cseq[1] == "INVITE" and resp.status >= 200:
            self.out_final = resp
            if 200 <= resp.status < 300:
                ans = sdp.parse(resp.body)
                self.rtp_remote = (ans.ip, ans.port)
            ack = Request("ACK", Address.parse(resp.headers.get("Contact", "")).uri
                          if resp.headers.get("Contact") else self.out_invite.uri)
            ack.headers.add("Via", f"SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK{rand_token(12)}")
            ack.headers.add("From", self.out_invite.headers.get("From"))
            ack.headers.add("To", resp.headers.get("To"))
            ack.headers.add("Call-ID", resp.call_id)
            ack.headers.add("CSeq", "1 ACK")
            self.sock.sendto(ack.to_bytes(), addr)
            self.out_event.set()
        elif resp.cseq[1] == "BYE":
            self.out_event.set()

    def send_bye_to_phone(self) -> None:
        bye = Request("BYE", f"sip:{USER}@{self.phone_addr[0]}:{self.phone_addr[1]}")
        bye.headers.add("Via", f"SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK{rand_token(12)}")
        bye.headers.add("From", self.out_invite.headers.get("From"))
        bye.headers.add("To", self.out_final.headers.get("To"))
        bye.headers.add("Call-ID", self.out_call_id)
        bye.headers.add("CSeq", "2 BYE")
        self.out_event.clear()
        self.sock.sendto(bye.to_bytes(), self.phone_addr)

    def send_rtp_to_phone(self, n: int = 10) -> None:
        pcm = struct.pack("<160h", *([1000] * 160))
        for i in range(n):
            pkt = build_header(0, i, i * 160, 0x1234) + g711.pcm16_to_ulaw(pcm)
            self.rtp_sock.sendto(pkt, self.rtp_remote)
            time.sleep(0.02)


class CapturingAudio:
    name = "capture"

    def __init__(self) -> None:
        self.frames = []
        self.tone = struct.pack("<160h", *([2000] * 160))

    def start(self): ...
    def stop(self): ...
    def read_frame(self):
        return self.tone
    def write_frame(self, pcm):
        self.frames.append(pcm)


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import logging
        logging.basicConfig(level=logging.WARNING)

    def setUp(self):
        self.switch = MockSwitch()
        self.switch.start()
        self.audio = CapturingAudio()
        cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH,
                     proxy="127.0.0.1", proxy_port=self.switch.port, bind_ip="127.0.0.1",
                     bind_port=0, register_expires=120, keepalive_seconds=1,
                     rtp_port_range=(30000, 31000))
        self.phone = SIPPhone(cfg, audio=self.audio)
        self.states = []
        self.phone.on_call_state = lambda c: self.states.append(c.state)
        self.phone.start()

    def tearDown(self):
        self.phone.stop()
        self.switch.stop.set()

    def wait_for(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------
    def test_register_with_digest_challenge(self):
        self.assertTrue(self.phone.register(wait=True))
        self.assertTrue(self.phone.registered)
        self.assertEqual(self.switch.events[:2], ["REGISTER", "REGISTER"])  # 401 then authed
        self.assertIn(f"sip:{USER}@127.0.0.1:{self.phone.local_port}", self.switch.registered_contact)

    def test_register_wrong_password_fails(self):
        self.phone.cfg.password = "wrong"
        self.assertFalse(self.phone.register(wait=True))
        self.assertFalse(self.phone.registered)

    def test_outbound_call_full_lifecycle(self):
        self.assertTrue(self.phone.register(wait=True))
        call = self.phone.call("6165550199")
        self.assertEqual(call.state, CallState.CALLING)
        self.assertTrue(self.wait_for(lambda: call.state == CallState.CONNECTED))
        self.assertTrue(self.switch.invite_auth_ok)
        self.assertTrue(self.switch.ack_received.wait(2))
        # RTP: our tone goes out, echo comes back decoded to the speaker
        self.assertTrue(self.wait_for(lambda: len(self.audio.frames) >= 5, 3))
        self.assertGreaterEqual(self.switch.rtp_rx, 5)
        sample = struct.unpack("<h", self.audio.frames[-1][:2])[0]
        self.assertTrue(1900 <= sample <= 2100, f"G.711 round trip off: {sample}")
        self.phone.send_dtmf("5#")
        time.sleep(0.5)
        self.phone.hangup()
        self.assertTrue(self.switch.bye_received.wait(2))
        self.assertEqual(call.state, CallState.ENDED)
        self.assertEqual(call.rtp.codec, "PCMU")

    def test_hold_resume_and_mute(self):
        self.phone.register(wait=True)
        call = self.phone.call("1000")
        self.assertTrue(self.wait_for(lambda: call.state == CallState.CONNECTED))
        self.phone.hold()
        self.assertTrue(self.wait_for(lambda: call.local_hold))
        self.assertEqual(self.switch.reinvites, ["sendonly"])
        self.assertEqual(call.rtp.direction, "sendonly")
        self.phone.resume()
        self.assertTrue(self.wait_for(lambda: not call.local_hold))
        self.assertEqual(self.switch.reinvites, ["sendonly", "sendrecv"])
        # mute: packets keep flowing (NAT) but carry silence
        self.phone.mute(True)
        self.assertTrue(call.rtp.muted)
        time.sleep(0.3)
        self.phone.mute(False)
        self.assertFalse(call.rtp.muted)
        self.phone.hangup()
        self.assertTrue(self.switch.bye_received.wait(2))

    def test_blind_transfer(self):
        self.phone.register(wait=True)
        call = self.phone.call("1000")
        self.assertTrue(self.wait_for(lambda: call.state == CallState.CONNECTED))
        self.phone.transfer("2000")
        self.assertTrue(self.wait_for(lambda: call.state == CallState.ENDED))
        self.assertEqual(self.switch.refer_to, f"<sip:2000@{REALM}>")
        self.assertIn("transferred to 2000", call.end_reason)
        self.assertTrue(self.switch.bye_received.wait(2))

    def test_blind_transfer_failure_keeps_call(self):
        self.phone.register(wait=True)
        self.switch.transfer_result = 486
        call = self.phone.call("1000")
        self.assertTrue(self.wait_for(lambda: call.state == CallState.CONNECTED))
        self.phone.transfer("2000")
        self.assertTrue(self.wait_for(lambda: call.transferring is not None, 1))
        self.assertTrue(self.wait_for(lambda: call.transferring is None))
        self.assertEqual(call.state, CallState.CONNECTED)
        self.phone.hangup()

    def test_outbound_call_busy(self):
        self.phone.register(wait=True)
        self.switch.reject_with = 486
        call = self.phone.call("1000")
        self.assertTrue(self.wait_for(lambda: call.state == CallState.ENDED))
        self.assertIn("486", call.end_reason)
        self.assertTrue(self.switch.ack_received.wait(2))  # failure ACKed

    def test_outbound_cancel_while_ringing(self):
        self.phone.register(wait=True)
        self.switch.answer_delay = 5
        call = self.phone.call("1000")
        # credentials are cached from REGISTER, so the INVITE carries Authorization up front
        self.assertTrue(self.wait_for(lambda: self.switch.invite_auth_ok))
        self.assertEqual(self.switch.events.count("INVITE"), 1)
        time.sleep(0.3)
        self.phone.hangup()
        self.assertTrue(self.switch.cancel_received.wait(2))
        self.assertTrue(self.switch.ack_received.wait(3))  # ACK to 487
        self.assertEqual(call.state, CallState.ENDED)

    def test_inbound_call_answer_and_far_end_bye(self):
        self.assertTrue(self.phone.register(wait=True))
        incoming = []
        self.phone.on_incoming_call = incoming.append
        self.switch.call_phone()
        self.assertTrue(self.wait_for(lambda: incoming))
        call = incoming[0]
        self.assertEqual(call.state, CallState.INCOMING)
        self.assertEqual(call.remote_number, "5550001111")
        self.assertEqual(call.remote_display, "Alice")
        self.phone.answer()
        self.assertTrue(self.switch.out_event.wait(2))
        self.assertEqual(self.switch.out_final.status, 200)
        self.assertTrue(self.wait_for(lambda: call.state == CallState.CONNECTED))
        self.switch.send_rtp_to_phone(10)
        self.assertTrue(self.wait_for(lambda: len(self.audio.frames) >= 10, 3))
        self.switch.send_bye_to_phone()
        self.assertTrue(self.wait_for(lambda: call.state == CallState.ENDED))
        self.assertTrue(self.switch.out_event.wait(2))  # 200 OK to BYE
        self.assertIn("far end", call.end_reason)

    def test_inbound_call_reject(self):
        self.phone.register(wait=True)
        incoming = []
        self.phone.on_incoming_call = incoming.append
        self.switch.call_phone()
        self.assertTrue(self.wait_for(lambda: incoming))
        self.phone.reject()
        self.assertTrue(self.switch.out_event.wait(2))
        self.assertEqual(self.switch.out_final.status, 486)

    def test_options_keepalive_answered(self):
        self.phone.register(wait=True)
        opt = Request("OPTIONS", f"sip:{USER}@127.0.0.1")
        opt.headers.add("Via", f"SIP/2.0/UDP 127.0.0.1:{self.switch.port};branch=z9hG4bKx1")
        opt.headers.add("From", f"<sip:ping@{REALM}>;tag=1")
        opt.headers.add("To", f"<sip:{USER}@{REALM}>")
        opt.headers.add("Call-ID", "opt1")
        opt.headers.add("CSeq", "1 OPTIONS")
        got = []
        self.switch._handle_response = lambda r, a: got.append(r)
        self.switch.sock.sendto(opt.to_bytes(), ("127.0.0.1", self.phone.local_port))
        self.assertTrue(self.wait_for(lambda: got))
        self.assertEqual(got[0].status, 200)


class UnitTests(unittest.TestCase):
    def test_message_roundtrip(self):
        raw = (b"INVITE sip:bob@biloxi.com SIP/2.0\r\n"
               b"Via: SIP/2.0/UDP pc33.atlanta.com;branch=z9hG4bK776asdhds;rport\r\n"
               b"Max-Forwards: 70\r\n"
               b"t: Bob <sip:bob@biloxi.com>\r\n"
               b"f: \"Alice Smith\" <sip:alice@atlanta.com>;tag=1928301774\r\n"
               b"i: a84b4c76e66710@pc33.atlanta.com\r\n"
               b"CSeq: 314159 INVITE\r\n"
               b"Contact: <sip:alice@pc33.atlanta.com>\r\n"
               b"Content-Type: application/sdp\r\n"
               b"Content-Length: 4\r\n\r\nv=0\r\n")
        m = message.parse(raw)
        self.assertIsInstance(m, Request)
        self.assertEqual(m.method, "INVITE")
        self.assertEqual(m.from_addr.display, "Alice Smith")
        self.assertEqual(m.from_addr.tag, "1928301774")
        self.assertEqual(m.to_addr.user, "bob")
        self.assertEqual(m.cseq, (314159, "INVITE"))
        self.assertEqual(m.body, b"v=0\r")  # Content-Length honoured
        self.assertIn(b"From: \"Alice Smith\"", m.to_bytes())
        v = message.parse_via(m.headers.get("Via"))
        self.assertEqual(v["host"], "pc33.atlanta.com")
        self.assertIn("rport", v["params"])

    def test_digest_rfc2617_vector(self):
        chal = {"realm": "testrealm@host.com", "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
                "qop": "auth", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
        hdr = digest.build_authorization(chal, "Mufasa", "Circle Of Life", "GET",
                                         "/dir/index.html", nonce_count=1, cnonce="0a4f113b")
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', hdr)
        self.assertTrue(digest.verify(hdr, "Circle Of Life", "GET"))

    def test_g711_roundtrip(self):
        pcm = struct.pack("<8h", 0, 100, -100, 1000, -1000, 10000, -10000, 32000)
        for enc, dec in ((g711.pcm16_to_ulaw, g711.ulaw_to_pcm16), (g711.pcm16_to_alaw, g711.alaw_to_pcm16)):
            out = struct.unpack("<8h", dec(enc(pcm)))
            for a, b in zip(struct.unpack("<8h", pcm), out):
                self.assertLess(abs(a - b), max(64, abs(a) * 0.1) + 1, (a, b))
        # known µ-law reference points
        self.assertEqual(g711.pcm16_to_ulaw(struct.pack("<h", 0)), b"\xff")
        self.assertEqual(g711.pcm16_to_alaw(struct.pack("<h", 0)), b"\xd5")

    def test_sdp_offer_answer(self):
        offer = sdp.parse(sdp.build_offer("10.0.0.5", 12000))
        self.assertEqual((offer.ip, offer.port), ("10.0.0.5", 12000))
        self.assertEqual(offer.audio_payloads(), [0, 8])
        self.assertEqual(offer.dtmf_payload(), 101)
        ans = sdp.parse(sdp.build_answer("10.0.0.6", 14000, offer))
        self.assertEqual(ans.audio_payloads(), [0])
        self.assertEqual(ans.codec_name(0), "PCMU")

    def test_rtp_header(self):
        pkt = build_header(0, 7, 160, 0xDEADBEEF, marker=True) + b"\xff" * 160
        pt, seq, ts, ssrc, marker, payload = parse_packet(pkt)
        self.assertEqual((pt, seq, ts, ssrc, marker, len(payload)), (0, 7, 160, 0xDEADBEEF, True, 160))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TTSTests(unittest.TestCase):
    """Text typed -> synthesised -> resampled -> G.711 -> RTP on the wire."""

    @staticmethod
    def fake_engine(text):
        # 1 s of a 440 Hz tone at 22050 Hz, like a Piper voice would return
        import math
        rate = 22050
        pcm = struct.pack(f"<{rate}h", *(int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)))
        return pcm, rate

    def test_resample_and_framing(self):
        from sipphone import tts
        pcm, rate = self.fake_engine("x")
        out = tts.resample_to_8k(pcm, rate)
        self.assertEqual(len(out), 8000 * 2)          # 1 s at 8 kHz
        self.assertEqual(len(list(tts.frames_of(out))), 50)  # 50 x 20 ms

    def test_tts_frames_reach_far_end_as_rtp(self):
        from sipphone.tts import TTSAudio
        switch = MockSwitch(); switch.start()
        received = switch.rtp_log = []
        aud = TTSAudio(self.fake_engine, gain=1.0)
        cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH, proxy="127.0.0.1",
                     proxy_port=switch.port, bind_ip="127.0.0.1", bind_port=0, rtp_port_range=(31000, 32000))
        phone = SIPPhone(cfg, audio=aud); phone.start()
        try:
            self.assertTrue(phone.register(wait=True))
            call = phone.call("100")
            end = time.time() + 5
            while call.state != CallState.CONNECTED and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(call.state, CallState.CONNECTED)
            received.clear()
            aud.say("hello from the softphone")
            end = time.time() + 5
            while aud.spoken < 50 and time.time() < end:
                time.sleep(0.05)
            time.sleep(0.2)  # let the last packets land
            self.assertGreaterEqual(aud.spoken, 50, "all 50 speech frames should have been sent")
            # decode what the switch heard and check the tone is there (not silence)
            pcm = b"".join(g711.ulaw_to_pcm16(parse_packet(d)[5]) for d in received if parse_packet(d)[0] == 0)
            samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
            self.assertGreater(len(samples), 8000 + 160, f"only {len(samples)} samples captured")
            self.assertGreater(max(abs(s) for s in samples), 8000)
            # ~440 Hz: count zero crossings in the loudest 8000-sample window
            loud = max(range(0, len(samples) - 8000, 160), key=lambda i: sum(abs(s) for s in samples[i:i+8000]))
            w = samples[loud:loud + 8000]
            zc = sum(1 for a, b in zip(w, w[1:]) if (a < 0) != (b < 0))
            self.assertTrue(800 <= zc <= 960, f"zero crossings {zc}, expected ~880 for 440 Hz")
            phone.hangup()
        finally:
            phone.stop(); switch.stop.set()


class STTTests(unittest.TestCase):
    """Far-end audio -> utterance segmentation -> engine -> text."""

    def test_segmentation_and_recognition(self):
        import math
        from sipphone.stt import Transcriber
        seen = []
        got = []
        tr = Transcriber(engine=lambda pcm: seen.append(len(pcm)) or f"utterance {len(seen)}",
                         on_text=got.append, silence_ms=200, min_speech_ms=100)
        tone = struct.pack("<160h", *(int(6000 * math.sin(2 * math.pi * 300 * i / 8000)) for i in range(160)))
        quiet = b"\x00" * 320
        for _ in range(30): tr.feed(quiet)          # noise floor
        for _ in range(25): tr.feed(tone)           # 500 ms speech
        for _ in range(15): tr.feed(quiet)          # 300 ms silence -> flush
        for _ in range(40): tr.feed(tone)           # second utterance
        tr.end_of_call()
        end = time.time() + 2
        while len(got) < 2 and time.time() < end: time.sleep(0.02)
        self.assertEqual(got, ["utterance 1", "utterance 2"])
        self.assertGreaterEqual(seen[0], 25 * 320)      # includes the speech
        self.assertLess(seen[0], (25 + 12) * 320)      # but not endless trailing silence

    def test_short_blips_ignored(self):
        from sipphone.stt import Transcriber
        got = []
        tr = Transcriber(engine=lambda pcm: "x", on_text=got.append, silence_ms=100, min_speech_ms=300)
        loud = struct.pack("<160h", *([5000] * 160))
        for _ in range(3): tr.feed(loud)            # 60 ms click
        for _ in range(10): tr.feed(b"\x00" * 320)
        time.sleep(0.2)
        self.assertEqual(got, [])

    def test_listening_audio_passes_through(self):
        from sipphone.stt import Transcriber, ListeningAudio
        from sipphone.tts import TTSAudio
        inner = TTSAudio(lambda t: (b"\x00" * 16000, 8000))
        la = ListeningAudio(inner, Transcriber(engine=lambda p: "", silence_ms=100))
        la.say("hi")                 # passthrough to TTSAudio
        la.mic_muted = True
        self.assertTrue(inner.mic_muted)
        la.write_frame(b"\x00" * 320)
        self.assertEqual(la.name, "tts+stt")


class XSITests(unittest.TestCase):
    """Admin monitor against a mock BroadWorks Xsi-Actions server."""

    GROUP = '''<?xml version="1.0" encoding="ISO-8859-1"?>
<Group xmlns="http://schema.broadsoft.com/xsi">
  <groupDirectory>
    <directoryDetails><userId>alice@ex.com</userId><firstName>Alice</firstName>
      <lastName>Smith</lastName><number>+13015550001</number><extension>1001</extension></directoryDetails>
    <directoryDetails><userId>bob@ex.com</userId><firstName>Bob</firstName>
      <lastName>Jones</lastName><number>+13015550002</number><extension>1002</extension></directoryDetails>
    <directoryDetails><userId>carol@ex.com</userId><firstName>Carol</firstName>
      <lastName>Lee</lastName><number>+13015550003</number><extension>1003</extension></directoryDetails>
  </groupDirectory>
</Group>'''
    CALLS_ACTIVE = '''<?xml version="1.0"?><Calls xmlns="http://schema.broadsoft.com/xsi">
  <call inConference="false"><callId>callhalf-99:0</callId>
  <uri>/v2.0/user/alice@ex.com/calls/callhalf-99:0</uri></call></Calls>'''
    CALLS_EMPTY = '''<?xml version="1.0"?><Calls xmlns="http://schema.broadsoft.com/xsi"></Calls>'''
    DETAIL = '''<?xml version="1.0"?><Call xmlns="http://schema.broadsoft.com/xsi">
  <callId>callhalf-99:0</callId><personality>Originator</personality><state>Active</state>
  <remoteParty><name>Dana Client</name><address>tel:+13015559999</address>
  <userId>dana@ex.com</userId><callType>Group</callType></remoteParty>
  <startTime>1600000000000</startTime><answerTime>1600000005000</answerTime></Call>'''
    DETAIL_RINGING = '''<?xml version="1.0"?><Call xmlns="http://schema.broadsoft.com/xsi">
  <callId>c2</callId><personality>Terminator</personality><state>Alerting</state>
  <remoteParty><name>Ed Caller</name><address>tel:+13015558888</address></remoteParty>
  <startTime>1600000100000</startTime></Call>'''
    HOOK_OFF = '<HookStatus xmlns="http://schema.broadsoft.com/xsi"><hookStatus>Off-Hook</hookStatus></HookStatus>'
    HOOK_ON  = '<HookStatus xmlns="http://schema.broadsoft.com/xsi"><hookStatus>On-Hook</hookStatus></HookStatus>'

    def _server(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        cases = self.cases
        auth_seen = []
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                auth_seen.append(self.headers.get("Authorization"))
                import urllib.parse as _u
                path = _u.unquote(self.path)
                body = None
                for frag, xml in cases.items():
                    if frag in path:
                        body = xml; break
                if body is None:
                    self.send_response(404); self.end_headers(); return
                data = body.encode()
                self.send_response(200); self.send_header("Content-Type","application/xml")
                self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
        srv = ThreadingHTTPServer(("127.0.0.1",0), H); srv.daemon_threads=True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.auth_seen = auth_seen
        return srv

    def test_list_users_and_auth_header(self):
        from sipphone.xsi import XSIClient
        self.cases = {"/directories/Group": self.GROUP}
        srv = self._server()
        try:
            c = XSIClient(f"http://127.0.0.1:{srv.server_address[1]}", "admin@ex.com", "pw")
            users = c.list_group_users()
            self.assertEqual([u.user_id for u in users], ["alice@ex.com","bob@ex.com","carol@ex.com"])
            self.assertEqual(users[0].name, "Alice Smith")
            self.assertEqual(users[0].extension, "1001")
            import base64
            self.assertEqual(self.auth_seen[0], "Basic " + base64.b64encode(b"admin@ex.com:pw").decode())
        finally:
            srv.shutdown()

    def test_call_status_on_call_with_remote_party(self):
        from sipphone.xsi import XSIClient, MonitoredUser
        self.cases = {"/calls/HookStatus": self.HOOK_OFF, "/calls/callhalf-99:0": self.DETAIL, "/calls": self.CALLS_ACTIVE}
        srv = self._server()
        try:
            c = XSIClient(f"http://127.0.0.1:{srv.server_address[1]}", "admin@ex.com", "pw")
            u = c.status_for(MonitoredUser(user_id="alice@ex.com", name="Alice Smith"))
            self.assertEqual(u.status, "on-call")
            self.assertEqual(u.with_name, "Dana Client")
            self.assertEqual(u.with_number, "tel:+13015559999")
            self.assertEqual(u.direction, "outbound")
            self.assertAlmostEqual(u.since, 1600000000.0, places=0)
        finally:
            srv.shutdown()

    def test_idle_and_ringing(self):
        from sipphone.xsi import XSIClient, MonitoredUser
        self.cases = {"/calls/HookStatus": self.HOOK_ON, "/calls": self.CALLS_EMPTY}
        srv = self._server()
        try:
            c = XSIClient(f"http://127.0.0.1:{srv.server_address[1]}", "a", "b")
            u = c.status_for(MonitoredUser(user_id="bob@ex.com"))
            self.assertEqual(u.status, "idle")
            self.assertEqual(u.with_name, "")
        finally:
            srv.shutdown()
        self.cases = {"/calls/HookStatus": self.HOOK_OFF, "/calls/c2": self.DETAIL_RINGING, "/calls": self.CALLS_ACTIVE.replace("callhalf-99:0","c2")}
        srv = self._server()
        try:
            c = XSIClient(f"http://127.0.0.1:{srv.server_address[1]}", "a", "b")
            u = c.status_for(MonitoredUser(user_id="carol@ex.com"))
            self.assertEqual(u.status, "ringing")
            self.assertEqual(u.direction, "inbound")
            self.assertEqual(u.with_name, "Ed Caller")
        finally:
            srv.shutdown()

    def test_auth_failure_recorded(self):
        from sipphone.xsi import XSIClient, MonitoredUser, XSIError
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class H(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_GET(self): self.send_response(401); self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1",0), H); srv.daemon_threads=True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = XSIClient(f"http://127.0.0.1:{srv.server_address[1]}", "a", "b")
            with self.assertRaises(XSIError):
                c.list_group_users()
            u = c.status_for(MonitoredUser(user_id="x"))
            self.assertEqual(u.status, "error")
            self.assertIn("authentication", u.detail)
        finally:
            srv.shutdown()

    def test_service_classification(self):
        from sipphone.xsi import service_type
        self.assertEqual(service_type("Sales Hunt Group", "x"), "hunt_group")
        self.assertEqual(service_type("Main Auto Attendant", "x"), "auto_attendant")
        self.assertEqual(service_type("Support Call Center", "x"), "call_center")
        self.assertEqual(service_type("Alice Smith", "alice"), "")
        self.assertEqual(service_type("Whatever", "SR^TeamDefault_"), "other")


class ConsoleTests(unittest.TestCase):
    """Warm transfer, conference, and pickup against the mock switch."""

    def setUp(self):
        import logging; logging.basicConfig(level=logging.WARNING)
        self.switch = MockSwitch(); self.switch.start()
        self.audio = CapturingAudio()
        cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH,
                     proxy="127.0.0.1", proxy_port=self.switch.port, bind_ip="127.0.0.1",
                     bind_port=0, register_expires=120, rtp_port_range=(30000, 31000))
        self.phone = SIPPhone(cfg, audio=self.audio); self.phone.start()
        self.assertTrue(self.phone.register(wait=True))

    def tearDown(self):
        self.phone.stop(); self.switch.stop.set()

    def wait(self, pred, t=5.0):
        end = time.time() + t
        while time.time() < end:
            if pred(): return True
            time.sleep(0.02)
        return False

    def _connected_primary(self):
        call = self.phone.call("5551000")
        self.assertTrue(self.wait(lambda: call.state == CallState.CONNECTED))
        return call

    def test_pickup_places_fac_call(self):
        # pickup is just a call to <pickup_code><ext>; verify the INVITE target
        self.phone.call("*971001")
        self.assertTrue(self.wait(lambda: self.switch.invite_auth_ok))
        self.assertTrue(self.wait(lambda: self.phone.active_call and self.phone.active_call.state == CallState.CONNECTED))
        self.assertIn("*971001", self.phone.active_call.remote_uri)

    def test_consult_holds_primary_and_dials(self):
        primary = self._connected_primary()
        self.switch.reinvites.clear()
        consult = self.phone.consult("5552000")
        self.assertTrue(self.wait(lambda: consult.state == CallState.CONNECTED))
        self.assertTrue(self.wait(lambda: primary.local_hold))
        self.assertIn("sendonly", self.switch.reinvites)          # primary held
        self.assertEqual(self.phone.consult_call(), consult)
        self.assertEqual(self.phone.primary_call(), primary)
        # audio foreground is the consult call only
        self.assertEqual(self.phone.router.foreground, {consult.call_id})

    def test_cancel_consult_resumes_primary(self):
        primary = self._connected_primary()
        self.phone.consult("5552000")
        self.assertTrue(self.wait(lambda: self.phone.consult_call() and self.phone.consult_call().state == CallState.CONNECTED))
        self.phone.cancel_consult()
        self.assertTrue(self.wait(lambda: not primary.local_hold))
        self.assertTrue(self.wait(lambda: self.phone.consult_call() is None))
        self.assertEqual(self.phone.primary_call(), primary)

    def test_complete_attended_transfer(self):
        primary = self._connected_primary()
        consult = self.phone.consult("5552000")
        self.assertTrue(self.wait(lambda: consult.state == CallState.CONNECTED))
        time.sleep(0.3)
        self.phone.complete_transfer()
        # REFER sent with Replaces; switch 202 + NOTIFY -> primary transferred
        self.assertTrue(self.wait(lambda: primary.state == CallState.ENDED))
        self.assertIn("transferred", primary.end_reason)
        self.assertIn("REFER", self.switch.events)
        self.assertIn("Replaces", self.switch.refer_to)

    def test_conference_bridges_both_legs(self):
        primary = self._connected_primary()
        consult = self.phone.consult("5552000")
        self.assertTrue(self.wait(lambda: consult.state == CallState.CONNECTED))
        time.sleep(0.3)
        self.phone.conference()
        self.assertTrue(self.wait(lambda: self.phone.router.conference, 3))
        self.assertTrue(primary.in_conference and consult.in_conference)
        self.assertEqual(self.phone.router.foreground, {primary.call_id, consult.call_id})
        self.assertTrue(self.wait(lambda: not primary.local_hold, 3))   # primary resumed for the bridge

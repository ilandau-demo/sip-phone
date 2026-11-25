"""SIP user agent: registration, outbound/inbound calls, dialog handling.

Implements the UAC/UAS behaviour of RFC 3261 that a phone needs:
  * REGISTER with Digest authentication (401 / 407), refresh, 423 handling,
    NAT address learning from Via `received`/`rport`
  * INVITE / 100 / 180 / 183 / 200 / ACK, CANCEL, BYE, re-INVITE (hold)
  * OPTIONS keep-alive replies, CRLF NAT keep-alive
  * UDP retransmission (Timer A/E style back-off) for INVITE / non-INVITE
"""
from __future__ import annotations

import enum
import logging
import threading
import time
import uuid
from urllib.parse import quote as urllib_quote
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import digest, g711, sdp
from .message import Address, Request, Response, Message, rand_token, parse_via
from .rtp import RTPSession, allocate_rtp_socket
from .transport import UDPTransport, local_ip_towards

log = logging.getLogger("sipphone.ua")

T1 = 0.5
T2 = 4.0
USER_AGENT = "sipphone/1.0 (SIP/2.0 Python)"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, NOTIFY, INFO, REFER, UPDATE"


@dataclass
class Config:
    # --- account (Nextiva: Users -> Device -> Generic SIP Phone) ---
    username: str            # "SIP Username" (the line/extension identity)
    password: str
    domain: str              # "Domain", e.g. ro3.nextiva.com / xxx.nextiva.com
    auth_username: str = ""  # "Authentication Name" if different from username
    display_name: str = ""
    proxy: str = ""          # outbound proxy host; defaults to domain
    proxy_port: int = 5060
    # --- local ---
    bind_ip: str = "0.0.0.0"
    bind_port: int = 5060
    public_ip: str = ""      # set to force the address advertised in Contact/SDP
    rtp_port_range: Tuple[int, int] = (10000, 20000)
    register_expires: int = 3600
    keepalive_seconds: int = 25
    codecs: Tuple[str, ...] = ("PCMU", "PCMA")
    user_agent: str = USER_AGENT

    def __post_init__(self) -> None:
        self.auth_username = self.auth_username or self.username
        self.proxy = self.proxy or self.domain


class CallState(enum.Enum):
    IDLE = "idle"
    CALLING = "calling"          # we sent INVITE, awaiting final response
    INCOMING = "incoming"        # we received INVITE, not yet answered
    CONNECTED = "connected"
    ENDED = "ended"


@dataclass
class Call:
    call_id: str
    local_tag: str
    direction: str                      # "out" | "in"
    remote_uri: str
    remote_display: str = ""
    remote_tag: Optional[str] = None
    local_cseq: int = 1
    remote_cseq: int = 0
    state: CallState = CallState.IDLE
    remote_target: Optional[str] = None     # Contact of far end
    route_set: List[str] = field(default_factory=list)
    invite: Optional[Request] = None         # the INVITE we sent/received
    invite_response: Optional[Response] = None
    rtp: Optional[RTPSession] = None
    local_sdp: bytes = b""
    remote_media: Optional[sdp.MediaDescription] = None
    end_reason: str = ""
    started_at: float = 0.0
    on_hold: bool = False          # far end put us on hold
    local_hold: bool = False       # we put them on hold
    muted: bool = False
    transferring: Optional[str] = None   # target while a REFER is in flight
    role: str = "primary"                # primary | consult
    parent: Optional[str] = None         # consult: the call_id of the call it was placed for
    in_conference: bool = False
    hold_pending: bool = False           # our hold re-INVITE is on its way
    auto_held: bool = False              # put on hold because another call took the audio
    created: float = field(default_factory=time.time)
    _src_addr: Optional[tuple] = None

    @property
    def remote_number(self) -> str:
        return Address(self.remote_uri).user or self.remote_uri

    @property
    def held(self) -> bool:
        """On hold from our side, or about to be."""
        return self.local_hold or self.hold_pending


# How many calls one line carries at once (ringing, being placed, talking or on hold).
MAX_CALLS_PER_LINE = 3


def focus_of(calls, router=None) -> Optional[Call]:
    """The call the screen and the no-argument actions are about.

    Only one call has the audio. That one comes first; with nobody talking it
    is the newest call being placed, then the newest one ringing, then the
    newest one on hold."""
    live = sorted((c for c in calls if c.state != CallState.ENDED), key=lambda c: c.created)
    if not live:
        return None
    fg = router.foreground if router is not None else None
    talking = [c for c in live if c.state == CallState.CONNECTED and not c.held
               and (fg is None or c.call_id in fg)]
    if not talking:
        talking = [c for c in live if c.state == CallState.CONNECTED and not c.held]
    if talking:
        return talking[-1]
    for state in (CallState.CALLING, CallState.INCOMING):
        some = [c for c in live if c.state == state]
        if some:
            return some[-1]
    return live[-1]


class _Pending:
    """A client transaction awaiting a final response, with UDP retransmission."""

    def __init__(self, request: Request, addr, on_response: Callable[[Response, "_Pending"], None],
                 on_timeout: Callable[[], None], is_invite: bool) -> None:
        self.request = request
        self.addr = addr
        self.on_response = on_response
        self.on_timeout = on_timeout
        self.is_invite = is_invite
        self.provisional_seen = False
        self.done = threading.Event()
        self.key = (request.call_id, request.cseq[0], request.cseq[1])


def _mix_frames(frames):
    frames = [f for f in frames if f]
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    try:
        import numpy as np
        acc = None
        for f in frames:
            a = np.frombuffer(f, dtype="<i2").astype(np.int32)
            acc = a if acc is None else acc[: len(a)] + a[: len(acc)]
        return np.clip(acc, -32768, 32767).astype("<i2").tobytes()
    except Exception:
        return frames[0]


class AudioRouter:
    """Routes one real mic/speaker across every call (all lines share one).

    - foreground: the call_ids that currently exchange real audio. A held or
      background call is not foreground, so its RTP sends silence and its
      received audio is dropped (and it never steals mic frames).
    - conference: when True, every foreground call hears the mic plus the other
      legs, and the speaker plays all legs mixed — a local 3-way bridge.
    - owners: the call (or conference pair) that most recently took the audio.
      A resume that completes after another call took over is put back on hold.
    """

    def __init__(self, audio) -> None:
        self.audio = audio
        self.foreground = set()
        self.owners = set()
        self.conference = False
        self._rx = {}
        self._mic_cache = None
        self._mic_t = 0.0
        self._lock = threading.Lock()
        # taking the audio and finishing a resume must not interleave, or a
        # resume that lands in between leaves two calls talking
        self.turn = threading.RLock()

    def _read_mic(self):
        now = time.monotonic()
        if now - self._mic_t > 0.01:
            self._mic_cache = (self.audio.read_frame() if self.audio else None)
            self._mic_t = now
        return self._mic_cache

    def mic_for(self, call_id):
        def source():
            if call_id not in self.foreground:
                return None
            mic = self._read_mic()
            if self.conference:
                with self._lock:
                    others = [f for cid, f in self._rx.items() if cid != call_id and f]
                return _mix_frames([mic] + others)
            return mic
        return source

    def speaker_for(self, call_id):
        def sink(pcm):
            if call_id not in self.foreground:
                return
            with self._lock:
                self._rx[call_id] = pcm
                mixed = _mix_frames(list(self._rx.values())) if self.conference else pcm
            if self.audio:
                self.audio.write_frame(mixed)
        return sink

    def claim(self, *call_ids):
        self.owners = set(c for c in call_ids if c)

    def set_foreground(self, *call_ids):
        self.foreground = set(c for c in call_ids if c)

    def add_foreground(self, call_id):
        self.foreground.add(call_id)

    def drop(self, call_id):
        self.foreground.discard(call_id)
        with self._lock:
            self._rx.pop(call_id, None)
        if len(self.foreground) < 2:
            self.conference = False


class SIPPhone:
    def __init__(self, cfg: Config, audio=None, router: Optional["AudioRouter"] = None) -> None:
        self.cfg = cfg
        self.audio = audio
        # several lines share one router (one mic/speaker); a single line makes its own
        self.router = router or (AudioRouter(audio) if audio else None)
        self._audio_started = False
        self.transport = UDPTransport(cfg.bind_ip, cfg.bind_port, self._on_message)
        self.resolve_error = ""
        try:
            self.proxy_addr = self._resolve(cfg.proxy, cfg.proxy_port)
        except RuntimeError as e:
            # Keep running so the settings screen can fix it; register() retries.
            self.resolve_error = str(e)
            self.proxy_addr = ("192.0.2.1", cfg.proxy_port or 5060)   # TEST-NET, never answers
        self.local_ip = cfg.public_ip or local_ip_towards(self.proxy_addr[0], self.proxy_addr[1])
        self.local_port = self.transport.port
        self.registered = False
        self.register_expiry = 0.0
        self.register_call_id = f"{uuid.uuid4()}@{self.local_ip}"
        self.register_cseq = 0
        self.register_tag = rand_token(10)
        self.reg_detail = "not registered yet"
        self._reg_retry_at = 0.0            # when to try again after a failed registration
        self._reg_backoff = 0.0
        self.calls: Dict[str, Call] = {}
        # Team-view-only mode: the line is never registered and calls are refused.
        self.presence_only = False
        # several calls on this line: one has the audio, the rest are on hold
        self.max_calls = MAX_CALLS_PER_LINE
        # with several lines, Lines points this at all of them (hold across lines)
        self.hold_others_hook: Optional[Callable[[Optional[str]], object]] = None
        self._pending: Dict[tuple, _Pending] = {}
        self._auth_cache: Dict[str, Dict[str, str]] = {}   # realm -> challenge
        self._nc: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._timer_thread = threading.Thread(target=self._timers, name="sip-timers", daemon=True)
        self._pending_acks: Dict[str, list] = {}  # call-id -> [resp, dst, next_tx, interval, deadline]
        # user callbacks
        self.on_incoming_call: Optional[Callable[[Call], None]] = None
        self.on_call_state: Optional[Callable[[Call], None]] = None
        self.on_registration: Optional[Callable[[bool, str], None]] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.transport.start()
        self._timer_thread.start()
        log.info("SIP UA listening on %s:%d (advertising %s), proxy %s:%d",
                 self.cfg.bind_ip, self.local_port, self.local_ip, *self.proxy_addr)

    def stop(self) -> None:
        for c in list(self.calls.values()):
            if c.state not in (CallState.ENDED, CallState.IDLE):
                self.hangup(c.call_id)
        if self.registered:
            self.unregister(wait=True)
        self._stop.set()
        self.transport.stop()

    @staticmethod
    def _resolve(host: str, port: int) -> Tuple[str, int]:
        import socket
        if not host:
            raise RuntimeError("no SIP domain set — open Settings and enter your account")
        try:
            return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][:2]
        except socket.gaierror as e:
            raise RuntimeError(f"cannot resolve SIP server {host!r}: {e}")

    # ------------------------------------------------------------------ helpers
    @property
    def aor(self) -> str:
        return f"sip:{self.cfg.username}@{self.cfg.domain}"

    @property
    def contact_uri(self) -> str:
        return f"sip:{self.cfg.username}@{self.local_ip}:{self.local_port}"

    def _new_request(self, method: str, uri: str, call_id: str, cseq: int,
                     from_addr: str, to_addr: str) -> Request:
        r = Request(method, uri)
        h = r.headers
        h.add("Via", f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{rand_token(20)};rport")
        h.add("Max-Forwards", 70)
        h.add("From", from_addr)
        h.add("To", to_addr)
        h.add("Call-ID", call_id)
        h.add("CSeq", f"{cseq} {method}")
        if method not in ("ACK", "CANCEL", "BYE"):
            h.add("Contact", f"<{self.contact_uri}>")
        h.add("User-Agent", self.cfg.user_agent)
        if method in ("INVITE", "REGISTER", "OPTIONS"):
            h.add("Allow", ALLOW)
        return r

    def _send_request(self, req: Request, addr=None, on_response=None, on_timeout=None) -> _Pending:
        addr = addr or self.proxy_addr
        self._apply_cached_auth(req)
        p = _Pending(req, addr, on_response or (lambda r, p: None), on_timeout or (lambda: None),
                     req.method == "INVITE")
        if req.method != "ACK":
            with self._lock:
                self._pending[p.key] = p
        self.transport.send(req, addr)
        if req.method != "ACK":
            threading.Thread(target=self._retransmit, args=(p,), daemon=True).start()
        return p

    def _retransmit(self, p: _Pending) -> None:
        interval, elapsed, limit = T1, 0.0, 32.0
        while not p.done.wait(interval):
            elapsed += interval
            if elapsed >= limit:
                with self._lock:
                    self._pending.pop(p.key, None)
                p.done.set()
                log.warning("%s timed out (no response)", p.request.method)
                p.on_timeout()
                return
            if self._stop.is_set():
                return
            # INVITE stops retransmitting once a provisional arrives
            if p.is_invite and p.provisional_seen:
                continue
            self.transport.send(p.request, p.addr)
            interval = min(interval * 2, T2)

    def _apply_cached_auth(self, req: Request) -> None:
        """Pre-emptively add Authorization if we've seen a challenge from this realm."""
        for realm, ch in self._auth_cache.items():
            hdr = "Proxy-Authorization" if ch.get("_proxy") else "Authorization"
            if hdr in req.headers:
                continue
            self._nc[realm] = self._nc.get(realm, 0) + 1
            req.headers.add(hdr, digest.build_authorization(
                ch, self.cfg.auth_username, self.cfg.password, req.method, req.uri, req.body,
                nonce_count=self._nc[realm]))

    def _respond(self, req: Request, status: int, addr, extra: Optional[Dict[str, str]] = None,
                 body: bytes = b"", content_type: str = "") -> Response:
        resp = Response(status)
        h = resp.headers
        for v in req.headers.get_all("Via"):
            h.add("Via", v)
        for rr in req.headers.get_all("Record-Route"):
            h.add("Record-Route", rr)
        h.add("From", req.headers.get("From", ""))
        to = req.headers.get("To", "")
        h.add("To", to)
        h.add("Call-ID", req.call_id)
        h.add("CSeq", req.headers.get("CSeq", ""))
        if status != 100 and req.method in ("INVITE", "OPTIONS", "REGISTER"):
            h.add("Contact", f"<{self.contact_uri}>")
        h.add("User-Agent", self.cfg.user_agent)
        if req.method in ("INVITE", "OPTIONS"):
            h.add("Allow", ALLOW)
        for k, v in (extra or {}).items():
            h.set(k, v)
        if body:
            h.add("Content-Type", content_type)
        resp.body = body
        self.transport.send(resp, self._via_reply_addr(req, addr))
        return resp

    @staticmethod
    def _via_reply_addr(req: Request, src_addr) -> Tuple[str, int]:
        """RFC 3581: reply to the source of the request (rport)."""
        return src_addr

    # ------------------------------------------------------------------ registration
    def set_presence_only(self, on: bool) -> None:
        """Switch between a real phone and a Team-view-only client (live)."""
        if on == self.presence_only:
            return
        self.presence_only = on
        if on:
            for c in list(self.calls.values()):
                if c.state not in (CallState.ENDED, CallState.IDLE):
                    self.hangup(c.call_id)
            if self.registered:
                self.unregister(wait=True)
            self.registered = False
            self.register_expiry = 0.0
            self._set_registered(False, "Team view only")

    def register(self, expires: Optional[int] = None, wait: bool = False, timeout: float = 10.0) -> bool:
        expires = self.cfg.register_expires if expires is None else expires
        if self.presence_only and expires:
            self._set_registered(False, "Team view only")
            return False
        if self.resolve_error:
            try:
                self.proxy_addr = self._resolve(self.cfg.proxy, self.cfg.proxy_port)
                self.resolve_error = ""
                self.local_ip = self.cfg.public_ip or local_ip_towards(*self.proxy_addr)
                self.register_call_id = f"{uuid.uuid4()}@{self.local_ip}"
            except RuntimeError as e:
                self.resolve_error = str(e)
                if expires:
                    self._set_registered(False, self.resolve_error)
                return False
        done = threading.Event()
        result = {"ok": False}

        def on_response(resp: Response, p: _Pending) -> None:
            if resp.status in (401, 407):
                if self._handle_challenge(resp, p):
                    return  # re-sent with credentials
                self._set_registered(False, "authentication failed")
                done.set()
            elif resp.status == 423:
                mn = int(resp.headers.get("Min-Expires", "3600"))
                log.info("registrar wants expires >= %d; retrying", mn)
                self.cfg.register_expires = mn
                self.register(mn)
            elif 200 <= resp.status < 300:
                self._learn_public_address(resp)
                granted = self._granted_expires(resp, expires)
                self.register_expiry = time.time() + granted
                result["ok"] = True
                self._set_registered(expires > 0, "unregistered" if expires == 0 else f"expires {granted}s")
                done.set()
            elif resp.status >= 300:
                self._set_registered(False, f"{resp.status} {resp.reason}")
                done.set()

        def on_timeout() -> None:
            self._set_registered(False, "timeout")
            done.set()

        self.register_cseq += 1
        me = Address(self.aor, self.cfg.display_name)
        req = self._new_request("REGISTER", f"sip:{self.cfg.domain}", self.register_call_id,
                                self.register_cseq, f"{me};tag={self.register_tag}", str(me))
        req.headers.set("Contact", f"<{self.contact_uri}>;expires={expires}" if expires else "*")
        req.headers.add("Expires", expires)
        if expires == 0:
            req.headers.set("Contact", f"<{self.contact_uri}>")
        self._send_request(req, on_response=on_response, on_timeout=on_timeout)
        if wait:
            done.wait(timeout)
            return result["ok"]
        return True

    def unregister(self, wait: bool = False) -> None:
        self.register(0, wait=wait, timeout=3)

    def reregister(self, timeout: float = 10.0) -> Tuple[bool, str]:
        """Register again from a clean slate and wait for the answer.

        Re-resolves the server, forgets the learned NAT address and any cached
        Digest challenge, and starts a fresh REGISTER dialog — so a network
        change, laptop sleep or a stale nonce doesn't leave the line stuck.
        Returns (registered, detail)."""
        if self.presence_only:
            return False, "Team view only"
        with self._lock:
            self._auth_cache.clear()
            self._nc.clear()
            try:
                self.proxy_addr = self._resolve(self.cfg.proxy, self.cfg.proxy_port)
                self.resolve_error = ""
            except RuntimeError as e:
                self.resolve_error = str(e)
            self.local_ip = self.cfg.public_ip or local_ip_towards(self.proxy_addr[0], self.proxy_addr[1])
            self.local_port = self.transport.port
            self.register_call_id = f"{uuid.uuid4()}@{self.local_ip}"
            self.register_tag = rand_token(10)
            self.register_cseq = 0
            self._reg_backoff, self._reg_retry_at = 0.0, 0.0
        if self.resolve_error:
            self._set_registered(False, self.resolve_error)
            return False, self.resolve_error
        self.reg_detail = "registering…"
        ok = self.register(wait=True, timeout=timeout)
        if not ok and self.reg_detail == "registering…":
            self._set_registered(False, "no answer from the server")
        return ok, self.reg_detail

    def reconfigure(self, **changes) -> bool:
        """Apply new account settings live and re-register.

        Accepts any Config field except bind_ip/bind_port (the socket stays
        where it is). Calls in progress are left alone. Returns the result of
        the new registration (waits up to 10 s)."""
        changes.pop("bind_ip", None); changes.pop("bind_port", None)
        old_addr = self.proxy_addr
        if self.registered:
            try:
                self.unregister(wait=True)          # drop the old binding (best effort)
            except Exception:
                pass
        with self._lock:
            for k, v in changes.items():
                if hasattr(self.cfg, k):
                    setattr(self.cfg, k, v)
            if not self.cfg.proxy:
                self.cfg.proxy = self.cfg.domain
            if not self.cfg.auth_username:
                self.cfg.auth_username = self.cfg.username
            try:
                self.proxy_addr = self._resolve(self.cfg.proxy, self.cfg.proxy_port)
                self.resolve_error = ""
            except RuntimeError as e:
                self.proxy_addr = old_addr
                self.resolve_error = str(e)
                self._set_registered(False, str(e))
                return False
            self.local_ip = self.cfg.public_ip or local_ip_towards(*self.proxy_addr)
            self.local_port = self.transport.port
            self._auth_cache.clear()
            self._nc.clear()
            self.register_call_id = f"{uuid.uuid4()}@{self.local_ip}"
            self.register_tag = rand_token(10)
            self.register_cseq = 0
            self.register_expiry = 0.0
            self.registered = False
        return self.register(wait=True)

    def _granted_expires(self, resp: Response, requested: int) -> int:
        for c in resp.headers.get_all("Contact"):
            a = Address.parse(c)
            if self.local_ip in a.uri and a.params.get("expires"):
                return int(a.params["expires"])
        return int(resp.headers.get("Expires", requested))

    def _learn_public_address(self, resp: Response) -> None:
        """If the registrar tells us our public address (rport/received), adopt it."""
        if self.cfg.public_ip:
            return
        vias = resp.headers.get_all("Via")
        if not vias:
            return
        v = parse_via(vias[0])
        received = v["params"].get("received")
        rport = v["params"].get("rport")
        if received and rport and (received != self.local_ip or int(rport) != self.local_port):
            log.info("NAT detected: public address is %s:%s (local %s:%d) — re-registering with it",
                     received, rport, self.local_ip, self.local_port)
            self.local_ip = received
            self.local_port = int(rport)
            self.register()

    def _set_registered(self, ok: bool, detail: str) -> None:
        self.registered = ok
        self.reg_detail = detail
        if ok or detail in ("unregistered", "Team view only") or self.presence_only:
            self._reg_backoff, self._reg_retry_at = 0.0, 0.0
        else:
            # a failed registration is retried by itself: 30 s, 60 s, … up to 5 min
            self._reg_backoff = min(300.0, (self._reg_backoff * 2) or 30.0)
            self._reg_retry_at = time.time() + self._reg_backoff
        log.info("registration: %s (%s)", "OK" if ok else ("ended" if detail == "unregistered" else "FAILED"), detail)
        if self.on_registration:
            self.on_registration(ok, detail)

    def _handle_challenge(self, resp: Response, p: _Pending) -> bool:
        """Re-send the challenged request with Digest credentials.

        Returns False if credentials were already supplied (i.e. they're wrong).
        """
        original = p.request
        proxy = resp.status == 407
        hdr = "Proxy-Authenticate" if proxy else "WWW-Authenticate"
        chal_val = resp.headers.get(hdr)
        if not chal_val:
            return False
        try:
            chal = digest.parse_challenge(chal_val)
        except ValueError as e:
            log.error("bad challenge: %s", e)
            return False
        realm = chal.get("realm", "")
        stale = chal.get("stale", "").lower() == "true"
        auth_hdr = "Proxy-Authorization" if proxy else "Authorization"
        if auth_hdr in original.headers and not stale:
            # we already sent credentials for this nonce: they are wrong
            self._auth_cache.pop(realm, None)
            return False
        chal["_proxy"] = "1" if proxy else ""
        self._auth_cache[realm] = chal
        self._nc[realm] = 0
        # new transaction: new branch & CSeq, same Call-ID / From tag
        n, method = original.cseq
        new = Request(original.method, original.uri)
        for k, v in original.headers.items():
            if k.lower() in ("via", "cseq", "authorization", "proxy-authorization", "content-length"):
                continue
            new.headers.add(k, v)
        new.headers._items.insert(0, ["Via", f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch=z9hG4bK{rand_token(20)};rport"])
        new.headers.add("CSeq", f"{n + 1} {method}")
        new.body = original.body
        if method == "REGISTER":
            self.register_cseq = n + 1
        else:
            call = self.calls.get(original.call_id)
            if call:
                call.local_cseq = n + 1
                call.invite = new
        self._send_request(new, on_response=p.on_response, on_timeout=p.on_timeout)
        return True

    def _pending_for(self, req: Optional[Request]) -> Optional[_Pending]:
        if req is None:
            return None
        with self._lock:
            return self._pending.get((req.call_id, req.cseq[0], req.cseq[1]))

    # ------------------------------------------------------------------ outbound call
    def _check_room(self) -> None:
        if len(self.live_calls()) >= self.max_calls:
            raise RuntimeError(f"This line already has {self.max_calls} calls — hang one up first")

    def call(self, number: str, role: str = "primary", parent: Optional[str] = None) -> Call:
        if self.presence_only:
            raise RuntimeError("This copy is set to Team view only — switch the mode in Settings to make calls")
        self._check_room()
        if role == "primary":
            self._hold_others(None)        # dialling takes the audio: whatever was talking goes on hold
        if "@" not in number:
            target = f"sip:{number}@{self.cfg.domain}"
        else:
            target = number if number.startswith("sip") else f"sip:{number}"
        call = Call(call_id=f"{uuid.uuid4()}@{self.local_ip}", local_tag=rand_token(10),
                    direction="out", remote_uri=target, role=role, parent=parent if role == "consult" else None)
        call.rtp = self._new_rtp(call.call_id)
        call.local_sdp = sdp.build_offer(self.local_ip, call.rtp.local_port, self.cfg.codecs)
        me = Address(self.aor, self.cfg.display_name)
        req = self._new_request("INVITE", target, call.call_id, call.local_cseq,
                                f"{me};tag={call.local_tag}", f"<{target}>")
        req.headers.add("Content-Type", "application/sdp")
        req.body = call.local_sdp
        call.invite = req
        call.state = CallState.CALLING
        with self._lock:
            self.calls[call.call_id] = call
        self._notify(call)
        self._send_request(req, on_response=lambda r, p: self._on_invite_response(call, r, p),
                           on_timeout=lambda: self._end_call(call, "no answer from server"))
        return call

    def _new_rtp(self, call_id: str) -> RTPSession:
        sock = allocate_rtp_socket(self.cfg.bind_ip, self.cfg.rtp_port_range)
        if self.router:
            return RTPSession(sock, mic_source=self.router.mic_for(call_id),
                              speaker_sink=self.router.speaker_for(call_id))
        return RTPSession(sock)

    def _on_invite_response(self, call: Call, resp: Response, p: _Pending) -> None:
        inv = p.request
        if call.state == CallState.ENDED:
            if 200 <= resp.status < 300:
                self._learn_dialog(call, resp)
                self._ack(call, resp, inv)
                self._send_bye(call)  # late answer after we gave up
            elif resp.status >= 300:
                self._ack(call, resp, inv)
            return
        to_tag = resp.to_addr.tag
        if to_tag and not call.remote_tag:
            call.remote_tag = to_tag
        if resp.status < 200:
            if resp.status == 183 and resp.content_type == "application/sdp" and resp.body:
                self._apply_remote_sdp(call, resp.body)   # early media
            log.info("call %s: %d %s", call.remote_number, resp.status, resp.reason)
            self._notify(call)
            return
        if resp.status in (401, 407):
            self._ack(call, resp, inv)
            call.remote_tag = None
            if not self._handle_challenge(resp, p):
                self._end_call(call, "authentication failed")
            return
        if 200 <= resp.status < 300:
            call.invite_response = resp
            self._learn_dialog(call, resp)
            self._ack(call, resp, inv)
            if resp.content_type == "application/sdp" and resp.body:
                self._apply_remote_sdp(call, resp.body)
            self._connected(call)
            return
        # 3xx..6xx — ACK the failure (RFC 3261 §17.1.1.3)
        self._ack(call, resp, inv)
        self._end_call(call, f"{resp.status} {resp.reason}")

    def _learn_dialog(self, call: Call, msg: Message) -> None:
        contact = msg.headers.get("Contact")
        if contact:
            call.remote_target = Address.parse(contact).uri
        rr = msg.headers.get_all("Record-Route")
        if rr:
            # UAC: route set is Record-Route in reverse order
            call.route_set = [Address.parse(r).uri for r in reversed(rr)] if isinstance(msg, Response) \
                else [Address.parse(r).uri for r in rr]

    def _ack(self, call: Call, resp: Response, inv: Request) -> None:
        if 200 <= resp.status < 300:
            # ACK for 2xx is a new transaction sent to the dialog target
            uri = call.remote_target or inv.uri
            ack = self._new_request("ACK", uri, call.call_id, inv.cseq[0],
                                    inv.headers.get("From", ""), resp.headers.get("To", ""))
            for r in call.route_set:
                ack.headers.add("Route", f"<{r}>")
            self.transport.send(ack, self.proxy_addr)
        else:
            ack = Request("ACK", inv.uri)
            ack.headers.add("Via", inv.headers.get("Via", ""))  # same branch
            ack.headers.add("Max-Forwards", 70)
            ack.headers.add("From", inv.headers.get("From", ""))
            ack.headers.add("To", resp.headers.get("To", ""))
            ack.headers.add("Call-ID", inv.call_id)
            ack.headers.add("CSeq", f"{inv.cseq[0]} ACK")
            for a in inv.headers.get_all("Authorization"):
                ack.headers.add("Authorization", a)
            self.transport.send(ack, self.proxy_addr)

    # ------------------------------------------------------------------ inbound call
    def _on_invite(self, req: Request, addr) -> None:
        call_id = req.call_id
        with self._lock:
            existing = self.calls.get(call_id)
        if existing and existing.state == CallState.CONNECTED:
            self._on_reinvite(existing, req, addr)
            return
        if existing:
            self._respond(req, 100, addr)  # retransmission
            return
        if self.presence_only:
            self._respond(req, 603, addr)      # not a phone right now
            return
        if len(self.live_calls()) >= self.max_calls:
            self._respond(req, 486, addr)      # the line is full
            return
        if req.content_type != "application/sdp" or not req.body:
            self._respond(req, 488, addr)
            return
        try:
            media = sdp.parse(req.body)
            if not media.audio_payloads():
                raise ValueError("no G.711")
        except ValueError:
            self._respond(req, 488, addr)
            return
        frm = req.from_addr
        call = Call(call_id=call_id, local_tag=rand_token(10), direction="in",
                    remote_uri=frm.uri, remote_display=frm.display, remote_tag=frm.tag,
                    remote_cseq=req.cseq[0], invite=req, remote_media=media,
                    state=CallState.INCOMING)
        self._learn_dialog(call, req)
        with self._lock:
            self.calls[call_id] = call
        self._respond(req, 100, addr)
        req.headers.set("To", f"{req.headers.get('To')};tag={call.local_tag}")
        call.invite_response = self._respond(req, 180, addr)
        call._src_addr = addr  # type: ignore[attr-defined]
        pai = req.headers.get("P-Asserted-Identity")
        log.info("incoming call from %s <%s>%s", frm.display, frm.uri, f" ({pai})" if pai else "")
        self._notify(call)
        if self.on_incoming_call:
            self.on_incoming_call(call)

    def answer(self, call_id: Optional[str] = None) -> Call:
        call = self._get_call(call_id, CallState.INCOMING)
        assert call.invite and call.remote_media
        call.rtp = self._new_rtp(call.call_id)
        call.local_sdp = sdp.build_answer(self.local_ip, call.rtp.local_port, call.remote_media)
        self._apply_remote_sdp(call, call.invite.body, answerer=True)
        resp = self._respond(call.invite, 200, call._src_addr,  # type: ignore[attr-defined]
                             body=call.local_sdp, content_type="application/sdp")
        call.invite_response = resp
        # retransmit 200 OK until ACK arrives (RFC 3261 §13.3.1.4)
        self._pending_acks[call.call_id] = [resp, call._src_addr, time.time() + T1, T1, time.time() + 32]  # type: ignore[attr-defined]
        self._connected(call)
        return call

    def reject(self, call_id: Optional[str] = None, status: int = 486) -> None:
        call = self._get_call(call_id, CallState.INCOMING)
        assert call.invite
        self._respond(call.invite, status, call._src_addr)  # type: ignore[attr-defined]
        self._end_call(call, f"rejected ({status})", send_bye=False)

    def _on_reinvite(self, call: Call, req: Request, addr) -> None:
        """Hold / resume / codec change from the far end."""
        if req.content_type == "application/sdp" and req.body:
            try:
                media = sdp.parse(req.body)
            except ValueError:
                self._respond(req, 488, addr)
                return
            call.remote_media = media
            call.on_hold = media.direction in ("sendonly", "inactive")
            if call.rtp:
                call.rtp.set_remote(media.ip, media.port)
                call.rtp.direction = {"sendonly": "recvonly", "recvonly": "sendonly"}.get(
                    media.direction, media.direction)
            direction = {"sendonly": "recvonly", "recvonly": "sendonly"}.get(media.direction, "sendrecv")
            if call.local_hold and direction == "sendrecv":
                direction = "sendonly"
            body = sdp.build_answer(self.local_ip, call.rtp.local_port if call.rtp else 0, media, direction)
        else:
            body = call.local_sdp
        call.remote_cseq = req.cseq[0]
        if not req.to_addr.tag:
            req.headers.set("To", f"{req.headers.get('To')};tag={call.local_tag}")
        resp = self._respond(req, 200, addr, body=body, content_type="application/sdp")
        self._pending_acks[call.call_id] = [resp, addr, time.time() + T1, T1, time.time() + 32]
        log.info("call %s: %s", call.remote_number, "on hold" if call.on_hold else "resumed")
        self._notify(call)

    # ------------------------------------------------------------------ in-dialog
    def hangup(self, call_id: Optional[str] = None) -> None:
        call = self._get_call(call_id)
        if call.state == CallState.CALLING:
            # RFC 3261 §9.1: CANCEL goes to the same destination with the INVITE's branch.
            # The final 487 is ACKed by _on_invite_response (call already ENDED).
            self._send_cancel(call)
            self._end_call(call, "cancelled", send_bye=False)
        elif call.state == CallState.INCOMING:
            self.reject(call.call_id, 603)
        elif call.state == CallState.CONNECTED:
            self._send_bye(call)
            self._end_call(call, "hung up", send_bye=False)

    def _send_cancel(self, call: Call) -> None:
        inv = call.invite
        if not inv:
            return
        cancel = Request("CANCEL", inv.uri)
        cancel.headers.add("Via", inv.headers.get("Via", ""))
        cancel.headers.add("Max-Forwards", 70)
        cancel.headers.add("From", inv.headers.get("From", ""))
        cancel.headers.add("To", inv.headers.get("To", ""))
        cancel.headers.add("Call-ID", inv.call_id)
        cancel.headers.add("CSeq", f"{inv.cseq[0]} CANCEL")
        cancel.headers.add("User-Agent", self.cfg.user_agent)
        self._send_request(cancel)

    def _dialog_request(self, call: Call, method: str) -> Request:
        """Build a request inside an established dialog (RFC 3261 §12.2.1.1)."""
        call.local_cseq += 1
        if call.direction == "out":
            frm = call.invite.headers.get("From", "")
            to = call.invite_response.headers.get("To", "") if call.invite_response else call.invite.headers.get("To", "")
            if call.remote_tag and "tag=" not in to:
                to = f"{to};tag={call.remote_tag}"
        else:
            frm = call.invite.headers.get("To", "")
            if "tag=" not in frm:
                frm = f"{frm};tag={call.local_tag}"
            to = call.invite.headers.get("From", "")
        uri = call.remote_target or call.remote_uri
        req = self._new_request(method, uri, call.call_id, call.local_cseq, frm, to)
        for r in call.route_set:
            req.headers.add("Route", f"<{r}>")
        return req

    def _send_bye(self, call: Call) -> None:
        self._send_request(self._dialog_request(call, "BYE"), on_response=lambda r, p: None)

    # ---- hold / mute / transfer ----
    def hold(self, call_id: Optional[str] = None) -> None:
        self._reinvite(self._get_call(call_id, CallState.CONNECTED), hold=True)

    def resume(self, call_id: Optional[str] = None) -> None:
        call = self._get_call(call_id, CallState.CONNECTED)
        self._take_audio(call)             # whatever else is talking goes on hold first
        self._reinvite(call, hold=False)

    def mute(self, on: bool = True, call_id: Optional[str] = None) -> None:
        call = self._get_call(call_id, CallState.CONNECTED)
        call.muted = on
        if self.audio is not None and hasattr(self.audio, "mic_muted"):
            # TTS backend: mute the mic, keep typed speech. One microphone for every
            # call, so it only follows the call that has the audio (see _sync_mute).
            if not call.held and (self.router is None or call.call_id in self.router.foreground):
                self.audio.mic_muted = on
        elif call.rtp:
            call.rtp.muted = on
        self._notify(call)

    def _sync_mute(self, call: Call) -> None:
        """The call that just got the audio brings its own mute setting with it."""
        if self.audio is not None and hasattr(self.audio, "mic_muted"):
            self.audio.mic_muted = call.muted

    def _reinvite(self, call: Call, hold: bool) -> None:
        """Send a re-INVITE with a=sendonly (hold) or a=sendrecv (resume).

        A hold takes effect on our side at once — the call leaves the audio —
        and stays in effect even if the far end refuses or never answers the
        re-INVITE, so another call can always take over."""
        if not call.rtp:
            raise RuntimeError("no media on this call")
        direction = "sendonly" if hold else "sendrecv"
        req = self._dialog_request(call, "INVITE")
        req.headers.add("Content-Type", "application/sdp")
        req.body = sdp.build_offer(self.local_ip, call.rtp.local_port, self.cfg.codecs, direction=direction)
        call.local_sdp = req.body
        if hold:
            call.hold_pending = True
            if self.router:
                self.router.drop(call.call_id)
            self._leave_conference(call)
            self._notify(call)

        def settle(ok: bool, why: str = "") -> None:
            if call.state != CallState.CONNECTED:
                call.hold_pending = False
                return
            if hold:
                call.hold_pending = False
                call.local_hold = True
                if call.rtp:
                    call.rtp.direction = "sendonly"
                if ok:
                    log.info("call %s: placed on hold", call.remote_number)
                else:
                    log.warning("call %s: the far end did not confirm the hold (%s); held on this side",
                                call.remote_number, why)
            elif ok:
                with (self.router.turn if self.router else threading.RLock()):
                    if self.router and self.router.owners and call.call_id not in self.router.owners:
                        # another call took the audio while this resume was on its way
                        log.info("call %s: resumed too late, back on hold", call.remote_number)
                        call.auto_held = True
                        self._reinvite(call, hold=True)
                        return
                    call.local_hold = False
                    call.auto_held = False
                    if call.rtp:
                        call.rtp.direction = "sendrecv"
                    if self.router:
                        self.router.add_foreground(call.call_id)
                self._sync_mute(call)
                log.info("call %s: resumed", call.remote_number)
            else:
                log.warning("call %s: resume refused (%s)", call.remote_number, why)
            self._notify(call)

        def on_response(resp: Response, p: _Pending) -> None:
            if resp.status < 200:
                return
            if resp.status in (401, 407):
                self._ack(call, resp, p.request)
                if not self._handle_challenge(resp, p):
                    settle(False, "authentication failed")
                return
            self._ack(call, resp, p.request)
            if 200 <= resp.status < 300:
                if resp.content_type == "application/sdp" and resp.body:
                    self._apply_remote_sdp(call, resp.body)
                settle(True)
            else:
                settle(False, f"{resp.status} {resp.reason}")

        self._send_request(req, on_response=on_response, on_timeout=lambda: settle(False, "no answer"))

    def transfer(self, target: str, call_id: Optional[str] = None) -> None:
        """Blind transfer (RFC 3515 REFER). The call ends once the far end reports success."""
        call = self._get_call(call_id, CallState.CONNECTED)
        if "@" not in target:
            target = f"sip:{target}@{self.cfg.domain}"
        elif not target.startswith("sip"):
            target = f"sip:{target}"
        req = self._dialog_request(call, "REFER")
        req.headers.add("Refer-To", f"<{target}>")
        req.headers.add("Referred-By", f"<{self.aor}>")
        call.transferring = target

        def on_response(resp: Response, p: _Pending) -> None:
            if resp.status < 200:
                return
            if resp.status in (401, 407):
                if not self._handle_challenge(resp, p):
                    call.transferring = None
                    log.warning("REFER auth failed")
                return
            if 200 <= resp.status < 300:
                log.info("transfer to %s accepted; waiting for the far end", target)
                # If no NOTIFY arrives (some platforms skip it), complete after a grace period.
                threading.Timer(8.0, lambda: self._transfer_done(call, "no progress report")).start()
            else:
                call.transferring = None
                log.warning("transfer refused: %d %s", resp.status, resp.reason)
            self._notify(call)

        def on_timeout() -> None:
            call.transferring = None
            self._notify(call)

        self._send_request(req, on_response=on_response, on_timeout=on_timeout)
        self._notify(call)

    def _transfer_done(self, call: Call, how: str) -> None:
        if call.state == CallState.CONNECTED and call.transferring:
            log.info("transfer complete (%s); leaving the call", how)
            self._send_bye(call)
            self._end_call(call, f"transferred to {Address(call.transferring).user or call.transferring}")

    # ---- several calls on one line: one has the audio, the rest are on hold ----
    def live_calls(self) -> List[Call]:
        """Every call that hasn't ended, oldest first."""
        with self._lock:
            return sorted((c for c in self.calls.values() if c.state != CallState.ENDED),
                          key=lambda c: c.created)

    def focus_call(self) -> Optional[Call]:
        """The call this line's no-argument actions are about (see focus_of)."""
        return focus_of(self.live_calls(), self.router)

    def pair_of(self, call: Optional[Call]) -> Tuple[Optional[Call], Optional[Call]]:
        """(call, its consultation call). A consultation call belongs to the call
        it was placed for; any other call can have one."""
        if call is None:
            return None, None
        with self._lock:
            if call.role == "consult":
                parent = self.calls.get(call.parent or "")
                if parent is not None and parent.state == CallState.ENDED:
                    parent = None
                return parent, call
            kids = [c for c in self.calls.values() if c.state != CallState.ENDED
                    and c.role == "consult" and c.parent == call.call_id]
        return call, (kids[-1] if kids else None)

    def hold_others(self, keep: Optional[str] = None) -> List[Call]:
        """Put every talking call on this line except `keep` on hold."""
        held = []
        for c in self.live_calls():
            if c.call_id == keep or c.state != CallState.CONNECTED or c.held:
                continue
            try:
                c.auto_held = True
                self._reinvite(c, hold=True)
                held.append(c)
            except Exception as e:
                c.auto_held = False
                log.warning("could not hold %s: %s", c.remote_number, e)
        return held

    def _hold_others(self, keep: Optional[str] = None) -> None:
        (self.hold_others_hook or self.hold_others)(keep)

    def _take_audio(self, call: Call) -> None:
        """`call` is about to have the audio: everything else that is talking, on
        any line, goes on hold."""
        if not self.router:
            return self._hold_others(call.call_id)
        with self.router.turn:
            # claim first: a resume that completes from here on sees the new
            # owner and goes back on hold; one that completed before is talking
            # and is held by the sweep below
            self.router.claim(call.call_id)
            self._hold_others(call.call_id)

    def _leave_conference(self, call: Call) -> None:
        """A leg that is held or ends takes the three-way apart."""
        if not call.in_conference:
            return
        with self._lock:
            for c in self.calls.values():
                if c.in_conference and (c is call or c.parent == call.call_id or c.call_id == call.parent):
                    c.in_conference = False
        if self.router:
            self.router.conference = False

    # ---- consult, attended transfer, conference ----
    def primary_call(self) -> Optional[Call]:
        return self.pair_of(self.focus_call())[0]

    def consult_call(self) -> Optional[Call]:
        return self.pair_of(self.focus_call())[1]

    def consult(self, number: str) -> Call:
        """Start an attended (warm) transfer: hold the current call and dial the
        target so you can talk before completing the transfer or conferencing."""
        primary, existing = self.pair_of(self.focus_call())
        if not primary or primary.state != CallState.CONNECTED:
            raise RuntimeError("no connected call to transfer")
        if existing:
            raise RuntimeError("a consultation call is already in progress")
        self._check_room()
        if not primary.held:
            self._reinvite(primary, hold=True)
        return self.call(number, role="consult", parent=primary.call_id)

    def cancel_consult(self) -> None:
        """Drop the consultation call and return to the held party."""
        primary, consult = self.pair_of(self.focus_call())
        if consult:
            self.hangup(consult.call_id)
        if primary and primary.state == CallState.CONNECTED and primary.held:
            self.resume(primary.call_id)

    def complete_transfer(self) -> None:
        """Finish an attended transfer: connect the held party to the consulted
        party (REFER with Replaces), then drop both of our legs."""
        primary, consult = self.pair_of(self.focus_call())
        if not primary or not consult:
            raise RuntimeError("need a held call and a consultation call")
        # Refer the primary to the consult's remote party, replacing the consult dialog.
        target = consult.remote_target or consult.remote_uri
        from_tag = consult.local_tag
        to_tag = consult.remote_tag or ""
        replaces = f"{consult.call_id};to-tag={to_tag};from-tag={from_tag}"
        refer = self._dialog_request(primary, "REFER")
        refer.headers.add("Refer-To",
                          f"<{target}?Replaces={urllib_quote(replaces)}>")
        refer.headers.add("Referred-By", f"<{self.aor}>")
        primary.transferring = target

        def on_response(resp: Response, p: _Pending) -> None:
            if resp.status < 200:
                return
            if resp.status in (401, 407):
                if not self._handle_challenge(resp, p):
                    primary.transferring = None
                return
            if 200 <= resp.status < 300:
                log.info("attended transfer accepted")
                # drop our consult leg; primary leaves on NOTIFY or grace timer
                self.hangup(consult.call_id)
                threading.Timer(8.0, lambda: self._transfer_done(primary, "no progress report")).start()
            else:
                primary.transferring = None
                log.warning("attended transfer refused: %d %s", resp.status, resp.reason)
            self._notify(primary)

        self._send_request(refer, on_response=on_response)

    def conference(self, number: Optional[str] = None) -> None:
        """Three-way conference. With `number`, dials it and bridges once it
        answers; with an existing consultation call, bridges that immediately."""
        primary, consult = self.pair_of(self.focus_call())
        if not primary or primary.state not in (CallState.CONNECTED,):
            raise RuntimeError("no connected call to conference")
        if consult is None:
            if not number:
                raise RuntimeError("no one to conference in")
            self._check_room()
            if not primary.held:
                self._reinvite(primary, hold=True)
            consult = self.call(number, role="consult", parent=primary.call_id)
            consult._conf_on_answer = True  # type: ignore[attr-defined]
            return
        self._bridge(primary, consult)

    def _bridge(self, primary: Call, consult: Call) -> None:
        if consult.state != CallState.CONNECTED:
            raise RuntimeError("the other party hasn't answered yet")
        # the pair takes the audio together (one of the two is the call on screen,
        # so nothing else is talking)
        if self.router:
            self.router.claim(primary.call_id, consult.call_id)
        for leg in (primary, consult):
            if leg.held:
                self._reinvite(leg, hold=False)
        if self.router:
            self.router.set_foreground(primary.call_id, consult.call_id)
            self.router.conference = True
        primary.in_conference = consult.in_conference = True
        log.info("conference: bridging %s and %s", primary.remote_number, consult.remote_number)
        self._notify(primary)
        self._notify(consult)

    def _on_notify(self, call: Call, req: Request) -> None:
        """NOTIFY for a REFER subscription carries a SIP status line (RFC 3515 §2.4.5)."""
        if "sipfrag" not in req.content_type or not call.transferring:
            return
        line = req.body.decode(errors="replace").strip().split("\n")[0]
        parts = line.split()
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if 200 <= code < 300:
            self._transfer_done(call, line)
        elif code >= 300:
            log.warning("transfer failed: %s", line)
            call.transferring = None
            self._notify(call)

    def send_dtmf(self, digits: str, call_id: Optional[str] = None) -> None:
        call = self._get_call(call_id, CallState.CONNECTED)
        if call.rtp:
            call.rtp.send_dtmf(digits)

    # ------------------------------------------------------------------ media
    def _apply_remote_sdp(self, call: Call, body: bytes, answerer: bool = False) -> None:
        try:
            media = sdp.parse(body)
        except ValueError as e:
            log.error("bad SDP from far end: %s", e)
            return
        call.remote_media = media
        pts = media.audio_payloads()
        if not pts or not call.rtp:
            return
        pt = pts[0]
        call.rtp.codec = media.codec_name(pt)
        call.rtp.pt = pt
        call.rtp.encode = g711.ENCODERS[call.rtp.codec]
        call.rtp.decode = g711.DECODERS[call.rtp.codec]
        call.rtp.dtmf_pt = media.dtmf_payload()
        call.rtp.set_remote(media.ip, media.port)
        if not call.rtp._rx.is_alive():
            call.rtp.start()
        log.info("media: %s pt=%d -> %s:%d", call.rtp.codec, pt, media.ip, media.port)

    def _connected(self, call: Call) -> None:
        call.state = CallState.CONNECTED
        call.started_at = time.time()
        # A call that connects takes the audio; whatever else was talking, on
        # this line or another, goes on hold.
        self._take_audio(call)
        if self.router:
            self.router.add_foreground(call.call_id)
        self._sync_mute(call)
        if self.audio and not self._audio_started:
            try:
                self.audio.start()
                self._audio_started = True
            except Exception as e:
                log.warning("audio start failed: %s", e)
        self._notify(call)
        # A consult call placed for conferencing bridges as soon as it answers.
        if getattr(call, "_conf_on_answer", False):
            primary = self.pair_of(call)[0]
            if primary:
                try:
                    self._bridge(primary, call)
                except RuntimeError as e:
                    log.warning("auto-conference failed: %s", e)

    def _end_call(self, call: Call, reason: str, send_bye: bool = False) -> None:
        if call.state == CallState.ENDED:
            return
        if send_bye and call.state == CallState.CONNECTED:
            self._send_bye(call)
        call.state = CallState.ENDED
        call.end_reason = reason
        call.hold_pending = False
        if call.rtp:
            call.rtp.stop()
        if self.router:
            self.router.drop(call.call_id)
        self._leave_conference(call)
        with self._lock:
            # a consultation call whose first call is gone is an ordinary call from here on
            for c in self.calls.values():
                if c.state != CallState.ENDED and c.role == "consult" and c.parent == call.call_id:
                    c.role, c.parent = "primary", None
        # Stop the audio device only when no calls remain live.
        live = [c for c in self.calls.values() if c.state != CallState.ENDED and c is not call]
        if self.audio and self._audio_started and not live:
            try:
                self.audio.stop()
                self._audio_started = False
            except Exception:
                pass
        self._pending_acks.pop(call.call_id, None)
        log.info("call %s ended: %s", call.remote_number, reason)
        self._notify(call)

    def _notify(self, call: Call) -> None:
        if self.on_call_state:
            try:
                self.on_call_state(call)
            except Exception:
                log.exception("on_call_state callback failed")

    def _get_call(self, call_id: Optional[str], want: Optional[CallState] = None) -> Call:
        with self._lock:
            if call_id:
                call = self.calls.get(call_id)
            else:
                # no call named: the one with the audio if it fits, else the newest that does
                live = sorted((c for c in self.calls.values() if c.state != CallState.ENDED),
                              key=lambda c: c.created)
                fits = [c for c in live if want is None or c.state == want]
                focus = focus_of(live, self.router)
                call = focus if (focus is not None and focus in fits) else (fits[-1] if fits else focus)
        if not call:
            raise RuntimeError("no active call")
        if want and call.state != want:
            raise RuntimeError(f"call is {call.state.value}, not {want.value}")
        return call

    @property
    def active_call(self) -> Optional[Call]:
        return self.focus_call()

    # ------------------------------------------------------------------ dispatch
    def _on_message(self, msg: Message, addr) -> None:
        if isinstance(msg, Response):
            self._on_response(msg)
        else:
            self._on_request(msg, addr)

    def _on_response(self, resp: Response) -> None:
        key = (resp.call_id, resp.cseq[0], resp.cseq[1])
        with self._lock:
            p = self._pending.get(key)
        if not p:
            log.debug("response for unknown transaction %s", key)
            return
        if resp.status >= 200:
            p.done.set()
            with self._lock:
                self._pending.pop(key, None)
        else:
            p.provisional_seen = True
        p.on_response(resp, p)

    def _on_request(self, req: Request, addr) -> None:
        m = req.method
        if m == "INVITE":
            self._on_invite(req, addr)
        elif m == "ACK":
            self._pending_acks.pop(req.call_id, None)
        elif m == "BYE":
            call = self.calls.get(req.call_id)
            self._respond(req, 200 if call else 481, addr)
            if call:
                self._end_call(call, "far end hung up")
        elif m == "CANCEL":
            call = self.calls.get(req.call_id)
            self._respond(req, 200, addr)
            if call and call.state == CallState.INCOMING and call.invite:
                self._respond(call.invite, 487, addr)
                self._end_call(call, "caller cancelled")
        elif m == "OPTIONS":
            self._respond(req, 200, addr, {"Accept": "application/sdp"})
        elif m in ("NOTIFY", "INFO", "UPDATE", "MESSAGE"):
            if m == "INFO" and "dtmf" in req.content_type:
                log.info("DTMF (INFO): %s", req.body.decode(errors="replace").strip())
            self._respond(req, 200, addr)
            call = self.calls.get(req.call_id)
            if m == "NOTIFY" and call:
                self._on_notify(call, req)
        elif m == "REFER":
            self._respond(req, 603, addr)  # transfers not supported
        else:
            self._respond(req, 501, addr)

    # ------------------------------------------------------------------ timers
    def _timers(self) -> None:
        last_keepalive = time.time()
        while not self._stop.wait(0.25):
            now = time.time()
            # registration refresh at ~90 % of the granted interval
            if self.registered and self.register_expiry and now > self.register_expiry - max(
                    30, self.cfg.register_expires * 0.1):
                self.register_expiry = 0
                log.info("refreshing registration")
                self.register()
            # a failed registration retries by itself (backoff set in _set_registered)
            if (not self.registered and self._reg_retry_at and now >= self._reg_retry_at
                    and not self.presence_only):
                self._reg_retry_at = 0.0
                log.info("retrying registration")
                threading.Thread(target=self.reregister, kwargs={"timeout": 15}, daemon=True,
                                 name="sip-reg-retry").start()
            # NAT keep-alive
            if now - last_keepalive >= self.cfg.keepalive_seconds:
                last_keepalive = now
                try:
                    self.transport.send_raw(b"\r\n\r\n", self.proxy_addr)
                except OSError:
                    pass
            # 200 OK retransmission until ACK
            for cid, entry in list(self._pending_acks.items()):
                resp, dst, nxt, interval, deadline = entry
                if now > deadline:
                    self._pending_acks.pop(cid, None)
                    call = self.calls.get(cid)
                    if call:
                        self._end_call(call, "no ACK received", send_bye=True)
                elif now >= nxt:
                    self.transport.send(resp, dst)
                    entry[3] = min(interval * 2, T2)
                    entry[2] = now + entry[3]

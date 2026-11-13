"""Up to eight SIP accounts ("lines") on one computer.

Each line is its own ``SIPPhone`` — its own registration, local SIP port and
calls — but they share one microphone/speaker through a single
``AudioRouter``. Only one call has the audio at a time: placing, answering or
resuming a call on one line puts the live calls on every other line on hold
(the same rule a desk phone with line keys follows). A call that rings on one
line while you talk on another rings normally with Answer / Decline.

``Lines`` looks like a ``SIPPhone`` to the rest of the app (web UI, scripts,
settings): calls from every line are in ``calls``, actions are routed to the
line that owns the call, and anything not about calls goes to the *current*
line — the one chosen in the header's account drop-down, used for dialling
and Re-register.

Line 1 is the ``[account]`` section of config.ini; lines 2–8 are ``[line2]``
… ``[line8]`` with the same keys plus ``label`` and ``enabled``.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Callable, Dict, List, Optional, Tuple

from .phone import Call, CallState, Config, SIPPhone

log = logging.getLogger("sipphone.lines")

MAX_LINES = 8
LINE_KEYS = ("label", "enabled", "display_name", "username", "auth_username", "password", "domain",
             "proxy", "proxy_port")


class SharedAudio:
    """One audio device used by several lines: started by the first call to
    need it, stopped when the last one is done. Everything else passes through."""

    def __init__(self, inner) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_users", 0)
        object.__setattr__(self, "_lk", threading.Lock())

    def start(self) -> None:
        with self._lk:
            if self._users == 0:
                self._inner.start()
            object.__setattr__(self, "_users", self._users + 1)

    def stop(self) -> None:
        with self._lk:
            n = max(0, self._users - 1)
            object.__setattr__(self, "_users", n)
            if n == 0:
                self._inner.stop()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value) -> None:
        setattr(self._inner, name, value)


class Line:
    def __init__(self, n: int, phone: SIPPhone, label: str = "") -> None:
        self.n = n
        self.phone = phone
        self.label = label or default_label(n, phone.cfg)

    def info(self, current: int) -> dict:
        p = self.phone
        return {"n": self.n, "label": self.label, "name": p.cfg.display_name or p.cfg.username,
                "account": f"{p.cfg.username}@{p.cfg.domain}", "registered": bool(p.registered),
                "detail": getattr(p, "reg_detail", ""), "current": self.n == current,
                "busy": any(c.state != CallState.ENDED for c in p.calls.values())}


def default_label(n: int, cfg: Config) -> str:
    return cfg.display_name or (cfg.username if n > 1 else "") or f"Line {n}"


def line_configs(cp, base: Config) -> Dict[int, Tuple[Config, str]]:
    """{n: (Config, label)} for the enabled extra lines in config.ini."""
    out: Dict[int, Tuple[Config, str]] = {}
    for n in range(2, MAX_LINES + 1):
        sec = f"line{n}"
        if not cp.has_section(sec):
            continue
        g = lambda k, d="": (cp.get(sec, k) if cp.has_option(sec, k) else d).strip()  # noqa: E731
        if g("enabled", "true").lower() in ("false", "no", "0", "off"):
            continue
        user, dom = g("username"), g("domain") or base.domain
        if not user or not dom:
            continue
        try:
            port = int(g("proxy_port", "5060") or 5060)
        except ValueError:
            port = 5060
        cfg = dataclasses.replace(base, username=user, password=g("password"), domain=dom,
                                  auth_username=g("auth_username") or user, display_name=g("display_name"),
                                  proxy=g("proxy") or dom, proxy_port=port, bind_port=0, public_ip="")
        out[n] = (cfg, g("label"))
    return out


class Lines:
    """Several SIPPhones behind the SIPPhone interface the app already uses."""

    def __init__(self, primary: SIPPhone, label: str = "") -> None:
        if primary.audio is not None and not isinstance(primary.audio, SharedAudio):
            primary.audio = SharedAudio(primary.audio)
        self.audio = primary.audio
        self.router = primary.router
        self.lines: Dict[int, Line] = {}
        self.current = 1
        self.last_reg_line: Optional[Line] = None
        self._lock = threading.RLock()
        self._on_state: Optional[Callable[[Call], None]] = None
        self._on_incoming: Optional[Callable[[Call], None]] = None
        self._on_reg: Optional[Callable[[bool, str], None]] = None
        self._attach(1, primary, label)

    # -- the SIPPhone callbacks the web UI sets ----------------------------------
    on_call_state = property(lambda self: self._on_state, lambda self, f: setattr(self, "_on_state", f))
    on_incoming_call = property(lambda self: self._on_incoming, lambda self, f: setattr(self, "_on_incoming", f))
    on_registration = property(lambda self: self._on_reg, lambda self, f: setattr(self, "_on_reg", f))

    def _attach(self, n: int, phone: SIPPhone, label: str) -> Line:
        line = Line(n, phone, label)
        # keep whatever callbacks the phone already had (tests, the CLI) chained in
        prev_state, prev_in, prev_reg = phone.on_call_state, phone.on_incoming_call, phone.on_registration

        def tag(call: Call) -> None:
            call.line_n = line.n              # type: ignore[attr-defined]
            call.line_label = line.label      # type: ignore[attr-defined]

        def st(call: Call) -> None:
            tag(call)
            if prev_state:
                prev_state(call)
            if self._on_state:
                self._on_state(call)

        def inc(call: Call) -> None:
            tag(call)
            if prev_in:
                prev_in(call)
            if self._on_incoming:
                self._on_incoming(call)

        def reg(ok: bool, detail: str) -> None:
            self.last_reg_line = line
            if prev_reg:
                prev_reg(ok, detail)
            if self._on_reg:
                self._on_reg(ok, detail)

        phone.on_call_state, phone.on_incoming_call, phone.on_registration = st, inc, reg
        with self._lock:
            self.lines[n] = line
        return line

    # -- line management ---------------------------------------------------------
    @property
    def primary(self) -> SIPPhone:
        return self.lines[1].phone

    @property
    def phone(self) -> SIPPhone:
        """The current line's phone (dialling, Re-register, header)."""
        line = self.lines.get(self.current) or self.lines[1]
        return line.phone

    def line_of(self, call_or_id) -> Optional[Line]:
        cid = getattr(call_or_id, "call_id", call_or_id)
        for line in list(self.lines.values()):
            if cid in line.phone.calls:
                return line
        return None

    def select(self, n: int) -> None:
        if n not in self.lines:
            raise RuntimeError("That line isn't set up")
        self.current = n

    def info(self) -> List[dict]:
        return [l.info(self.current) for _, l in sorted(self.lines.items())]

    def set_line(self, n: int, cfg: Optional[Config], label: str = "") -> None:
        """Add, update or (cfg None) remove line n (2–8), live."""
        if not 2 <= n <= MAX_LINES:
            raise ValueError("lines 2 to 8 can be changed here")
        with self._lock:
            line = self.lines.get(n)
        if cfg is None:
            if line:
                self._remove(n)
            return
        if line:
            line.label = label or default_label(n, cfg)
            old = line.phone.cfg
            kw = {f: getattr(cfg, f) for f in ("username", "password", "domain", "auth_username",
                                                "display_name", "proxy", "proxy_port")}
            if any(getattr(old, f) != v for f, v in kw.items() if f != "display_name"):
                threading.Thread(target=line.phone.reconfigure, kwargs=kw, daemon=True,
                                 name=f"line{n}-reconfigure").start()
            else:
                line.phone.cfg.display_name = cfg.display_name
            return
        phone = SIPPhone(cfg, audio=self.audio, router=self.router)
        phone.presence_only = self.primary.presence_only
        self._attach(n, phone, label)
        phone.start()
        if not phone.presence_only:
            threading.Thread(target=phone.register, daemon=True, name=f"line{n}-register").start()
        log.info("line %d added (%s@%s)", n, cfg.username, cfg.domain)

    def _remove(self, n: int) -> None:
        with self._lock:
            line = self.lines.pop(n, None)
            if self.current == n:
                self.current = 1
        if line:
            try:
                line.phone.stop()
            except Exception:
                log.exception("stopping line %d", n)
            log.info("line %d removed", n)

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        for line in list(self.lines.values()):
            line.phone.start()

    def stop(self) -> None:
        for _, line in sorted(self.lines.items(), reverse=True):
            try:
                line.phone.stop()
            except Exception:
                log.exception("stopping line %d", line.n)

    def register(self, *a, **kw):
        return self.phone.register(*a, **kw)

    def register_all(self) -> None:
        for line in list(self.lines.values()):
            threading.Thread(target=line.phone.register, daemon=True).start()

    def reregister(self, n: Optional[int] = None, timeout: float = 10.0) -> Tuple[bool, str]:
        line = self.lines.get(n or self.current) or self.lines[1]
        return line.phone.reregister(timeout=timeout)

    @property
    def registered(self) -> bool:
        return self.phone.registered

    @property
    def presence_only(self) -> bool:
        return self.primary.presence_only

    @presence_only.setter
    def presence_only(self, on: bool) -> None:
        for line in self.lines.values():
            line.phone.presence_only = on

    def set_presence_only(self, on: bool) -> None:
        for line in list(self.lines.values()):
            line.phone.set_presence_only(on)

    def reconfigure(self, **kw):
        return self.primary.reconfigure(**kw)        # [account] = line 1

    # -- calls -------------------------------------------------------------------
    @property
    def calls(self) -> Dict[str, Call]:
        out: Dict[str, Call] = {}
        for line in list(self.lines.values()):
            out.update(line.phone.calls)
        return out

    def _live(self) -> List[Call]:
        return [c for c in self.calls.values() if c.state != CallState.ENDED]

    def focused_call(self) -> Optional[Call]:
        """The call that has the audio, else the newest ringing one, else the newest live one."""
        live = self._live()
        talking = [c for c in live if c.state == CallState.CONNECTED and not c.local_hold
                   and self.router is not None and c.call_id in self.router.foreground]
        if not talking:
            talking = [c for c in live if c.state in (CallState.CONNECTED, CallState.CALLING)
                       and not c.local_hold]
        if talking:
            return talking[-1]
        ringing = [c for c in live if c.state == CallState.INCOMING]
        if ringing:
            return ringing[-1]
        return live[-1] if live else None

    @property
    def active_call(self) -> Optional[Call]:
        return self.focused_call()

    def _owner(self, call_id: Optional[str]) -> Tuple[SIPPhone, Optional[str]]:
        if call_id:
            line = self.line_of(call_id)
            if line:
                return line.phone, call_id
        f = self.focused_call()
        if f is not None:
            line = self.line_of(f)
            if line:
                return line.phone, f.call_id
        return self.phone, None

    def hold_others(self, keep: Optional[str] = None, keep_line: Optional[int] = None) -> List[Call]:
        """Put every talking call on another line on hold (one call has the audio)."""
        held = []
        for line in list(self.lines.values()):
            if keep_line is not None and line.n == keep_line:
                continue
            for c in list(line.phone.calls.values()):
                if c.call_id == keep or c.state != CallState.CONNECTED or c.local_hold:
                    continue
                if self.router is not None:
                    self.router.drop(c.call_id)          # silence it now, the re-INVITE follows
                try:
                    line.phone.hold(c.call_id)
                    held.append(c)
                except Exception as e:
                    log.warning("could not hold %s on line %d: %s", c.remote_number, line.n, e)
        return held

    def call(self, number: str, role: str = "primary", line: Optional[int] = None) -> Call:
        target = self.lines.get(line or self.current) or self.lines[1]
        if role == "primary":
            self.hold_others(keep_line=target.n)
        return target.phone.call(number, role=role)

    def answer(self, call_id: Optional[str] = None) -> Call:
        if not call_id:
            ringing = [c for c in self._live() if c.state == CallState.INCOMING]
            if not ringing:
                raise RuntimeError("no ringing call")
            call_id = ringing[-1].call_id
        phone, cid = self._owner(call_id)
        self.hold_others(keep=cid, keep_line=self.line_of(cid).n if self.line_of(cid) else None)
        return phone.answer(cid)

    def reject(self, call_id: Optional[str] = None, status: int = 486) -> None:
        if not call_id:
            ringing = [c for c in self._live() if c.state == CallState.INCOMING]
            if not ringing:
                raise RuntimeError("no ringing call")
            call_id = ringing[-1].call_id
        phone, cid = self._owner(call_id)
        phone.reject(cid, status)

    def hangup(self, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        phone.hangup(cid)

    def hold(self, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        phone.hold(cid)

    def resume(self, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        line = self.line_of(cid) if cid else None
        self.hold_others(keep=cid, keep_line=line.n if line else None)
        phone.resume(cid)

    def mute(self, on: bool = True, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        phone.mute(on, cid)

    def send_dtmf(self, digits: str, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        phone.send_dtmf(digits, cid)

    def transfer(self, target: str, call_id: Optional[str] = None) -> None:
        phone, cid = self._owner(call_id)
        phone.transfer(target, cid)

    def _focused_phone(self) -> SIPPhone:
        return self._owner(None)[0]

    def primary_call(self) -> Optional[Call]:
        f = self.focused_call()
        return self._focused_phone().primary_call() if f is not None else None

    def consult_call(self) -> Optional[Call]:
        f = self.focused_call()
        return self._focused_phone().consult_call() if f is not None else None

    def consult(self, number: str) -> Call:
        return self._focused_phone().consult(number)

    def cancel_consult(self) -> None:
        self._focused_phone().cancel_consult()

    def complete_transfer(self) -> None:
        self._focused_phone().complete_transfer()

    def conference(self, number: Optional[str] = None) -> None:
        self._focused_phone().conference(number)

    # -- everything else: the current line ---------------------------------------
    def __getattr__(self, name):
        if name.startswith("_") or name in ("lines",):
            raise AttributeError(name)
        return getattr(self.phone, name)

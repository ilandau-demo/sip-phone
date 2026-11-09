"""Local browser UI for the softphone.

    python -m sipphone --config config.ini --tts piper --ui

Serves a single page on http://127.0.0.1:8088 (loopback only) backed by a tiny
JSON API over the running SIPPhone / TTSAudio objects.  Standard library only.
"""
from __future__ import annotations

import collections
import json
import queue
import logging
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, Optional
from urllib.parse import urlparse, parse_qs

from .phone import Call, CallState, SIPPhone

log = logging.getLogger("sipphone.web")

HTML_PATH = os.path.join(os.path.dirname(__file__), "ui.html")
DEFAULT_PHRASES = [
    "Hello, this is an automated call. Please hold for a moment.",
    "Can you hear me clearly?",
    "Thank you, goodbye.",
]


class UIState:
    """Thread-safe event log + snapshot builder shared by the HTTP handlers."""

    def __init__(self, phone: SIPPhone, audio, phrases_path: str, contacts_path: str = "contacts.json",
                 history_path: str = "history.json") -> None:
        self.phone = phone
        self.audio = audio
        self.phrases_path = phrases_path
        self.contacts_path = contacts_path
        self.history_path = history_path
        self.boards_path = os.path.join(os.path.dirname(os.path.abspath(contacts_path)), "boards.json")
        from .scripts import ScriptStore, ScriptRunner
        self.scripts = ScriptStore(os.path.join(os.path.dirname(os.path.abspath(contacts_path)), "scripts.json"))
        self.script_runner = ScriptRunner(audio, lambda: bool(phone.active_call and phone.active_call.state == CallState.CONNECTED),
                                          on_event=self.add)
        self.on_quit = None
        self._monitor = None
        self.watcher = None           # presence.AvailabilityWatcher (rules live next to contacts)
        self.notifications: list = []  # recent availability notifications, newest last
        self.pickup_code = "*97"
        self.settings = None          # SettingsManager, when the config file is known
        self.warnings: list = []      # startup problems to show in the UI
        self.mode = "presence" if getattr(phone, "presence_only", False) else "phone"
        self.started_mode = self.mode
        self.updater = None           # updater.UpdateManager, in app mode
        self.voice = None             # voice.VoiceEngine
        self._history = self._load_history()
        self._answered: set = set()
        # -- push stream (/api/stream): one queue per connected page/bridge ----------
        self._streams: list = []
        self._stream_lock = threading.Lock()
        self._team_dirty = threading.Event()
        self._team_last: dict = {}          # user_id -> json of the person as last sent
        self._team_meta_last = ""
        self._team_seq = 0
        self._state_sig = ""
        self._ticker_started = False
        stt = getattr(audio, "stt", None)
        if stt is not None:
            prev = stt.on_text
            def heard(text: str) -> None:
                if prev:
                    prev(text)
                self.add("heard", text)
            stt.on_text = heard
        self.events: Deque[dict] = collections.deque(maxlen=500)
        self.next_id = 1
        self.lock = threading.Lock()
        self.reg_detail = "Team view only" if getattr(phone, "presence_only", False) else "registering…"
        self.last_error = ""
        # hook into phone callbacks (chain any existing ones)
        prev_state, prev_in, prev_reg = phone.on_call_state, phone.on_incoming_call, phone.on_registration
        self._last_state: dict = {}

        def on_state(call: Call) -> None:
            if prev_state:
                prev_state(call)
            key = call.call_id
            if self._last_state.get(key) == call.state:
                return
            self._last_state[key] = call.state
            if call.state == CallState.CALLING:
                name = self.lookup(call.remote_number)
                self.add("event", f"Calling {name + ' · ' if name else ''}{call.remote_number}")
            elif call.state == CallState.CONNECTED:
                codec = call.rtp.codec if call.rtp else "?"
                self._answered.add(key)
                self.add("event", f"Connected to {call.remote_number} ({codec})")
            elif call.state == CallState.ENDED:
                dur = f" after {self._fmt(time.time() - call.started_at)}" if call.started_at else ""
                self.add("event", f"Call ended: {call.end_reason}{dur}")
                self._record_history(call)

        def on_incoming(call: Call) -> None:
            if prev_in:
                prev_in(call)
            name = self.lookup(call.remote_number) or call.remote_display
            who = f"{name} ({call.remote_number})" if name else call.remote_number
            self.add("event", f"Incoming call from {who}")

        def on_reg(ok: bool, detail: str) -> None:
            if prev_reg:
                prev_reg(ok, detail)
            self.reg_detail = detail
            self.add("event", f"Registered ({detail})" if ok else f"Registration failed: {detail}")

        phone.on_call_state, phone.on_incoming_call, phone.on_registration = on_state, on_incoming, on_reg

    # -- call history ------------------------------------------------------------
    def _load_history(self) -> list:
        try:
            with open(self.history_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return []

    def _record_history(self, call: Call) -> None:
        answered = call.call_id in self._answered
        if call.direction == "in":
            outcome = "answered" if answered else ("declined" if "rejected" in call.end_reason else "missed")
        else:
            outcome = "answered" if answered else ("cancelled" if "cancel" in call.end_reason else "failed")
        entry = {"t": time.time(), "direction": call.direction, "number": call.remote_number,
                 "name": self.lookup(call.remote_number) or call.remote_display, "outcome": outcome,
                 "duration": int(time.time() - call.started_at) if call.started_at else 0,
                 "reason": call.end_reason}
        with self.lock:
            self._history.insert(0, entry)
            del self._history[200:]
            try:
                with open(self.history_path, "w", encoding="utf-8") as f:
                    json.dump(self._history, f, indent=1)
            except OSError:
                log.warning("could not write %s", self.history_path)

    def history(self) -> list:
        with self.lock:
            return list(self._history[:50])

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"

    def _warnings(self) -> list:
        out = list(self.warnings)
        try:
            from .authentication import credential_store
            if credential_store.last_problem:
                out.append("Password: " + credential_store.last_problem)
        except Exception:
            pass
        return out

    def add(self, kind: str, text: str) -> None:
        with self.lock:
            self.events.append({"id": self.next_id, "kind": kind, "text": text, "t": time.time()})
            self.next_id += 1
        if self.stream_clients():
            threading.Thread(target=self._push_state, kwargs={"force": True}, daemon=True).start()

    # -- presence engine hookup --------------------------------------------------
    @property
    def monitor(self):
        return self._monitor

    @monitor.setter
    def monitor(self, mon) -> None:
        self._monitor = mon
        if mon is not None and getattr(mon, "on_change", None):
            mon.on_change(self._presence_changed)
        if mon is not None and getattr(mon, "on_update", None):
            mon.on_update(self._team_dirty.set)
        with self._stream_lock:
            self._team_last = {}
            self._team_meta_last = ""
        self._team_dirty.set()

    # -- push stream ---------------------------------------------------------------
    # Pages and the browser bridge used to poll /api/state and /api/monitor every
    # second; that put up to a second between a presence event and the screen.
    # Now they open /api/stream once and get pushed: `team` carries only the
    # people whose row changed (computed the moment the engine applies an
    # event), `state` the call/phone snapshot (checked every 200 ms in-process,
    # and immediately on every new event line), `event` each new event line.
    def stream_open(self, since: int) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=500)
        q.since = since                                   # type: ignore[attr-defined]
        with self._stream_lock:
            self._streams.append(q)
            start = not self._ticker_started
            self._ticker_started = True
        if start:
            threading.Thread(target=self._stream_ticker, name="ui-stream", daemon=True).start()
        return q

    def stream_close(self, q) -> None:
        with self._stream_lock:
            if q in self._streams:
                self._streams.remove(q)

    def stream_clients(self) -> int:
        with self._stream_lock:
            return len(self._streams)

    def _stream_put(self, kind: str, payload: dict, only=None) -> None:
        with self._stream_lock:
            targets = list(self._streams) if only is None else [only]
        for q in targets:
            try:
                q.put_nowait((kind, payload))
            except queue.Full:                 # a page that stopped reading: drop it
                self.stream_close(q)

    def _state_signature(self, snap: dict) -> str:
        return json.dumps({k: v for k, v in snap.items() if k not in ("events", "expires_in", "notifications")},
                          sort_keys=True, default=str)

    def _push_state(self, force: bool = False) -> None:
        """Send the phone snapshot to every stream if it changed (per-client event
        lines ride along, so nothing is ever lost between pushes)."""
        with self._stream_lock:
            clients = list(self._streams)
        if not clients:
            return
        snap = self.snapshot(0)
        sig = self._state_signature(snap)
        changed = sig != self._state_sig
        self._state_sig = sig
        for q in clients:
            since = getattr(q, "since", 0)
            with self.lock:
                ev = [e for e in self.events if e["id"] > since]
            if not (changed or force or ev):
                continue
            out = dict(snap, events=ev)
            if ev:
                q.since = ev[-1]["id"]          # type: ignore[attr-defined]
            self._stream_put("state", out, only=q)

    def _push_team(self) -> None:
        mon = self._monitor
        if not self.stream_clients():
            with self._stream_lock:
                self._team_last = {}; self._team_meta_last = ""
            return
        if mon is None:
            payload = {"enabled": False}
            if self._team_meta_last != "off":
                self._team_meta_last = "off"
                self._stream_put("team", payload)
            return
        snap = mon.snapshot(); snap["enabled"] = True
        users = snap.pop("users")
        meta = snap
        t_applied = meta.get("t", time.time())
        cur = {u["user_id"]: json.dumps(u, sort_keys=True, default=str) for u in users}
        with self._stream_lock:
            removed = [uid for uid in self._team_last if uid not in cur]
            changed = [u for u in users if self._team_last.get(u["user_id"]) != cur[u["user_id"]]]
            full = not self._team_last or len(changed) > len(users) // 2
            self._team_last = cur
            meta_json = json.dumps({k: v for k, v in meta.items() if k not in ("t", "latency", "last_poll")},
                                   sort_keys=True, default=str)
            meta_changed = meta_json != self._team_meta_last
            self._team_meta_last = meta_json
            self._team_seq += 1
            seq = self._team_seq
        if not (changed or removed or meta_changed or full):
            return
        self._stream_put("team", {"seq": seq, "full": full, "users": users if full else changed,
                                  "removed": removed, "meta": meta, "t": t_applied})

    def team_opening(self) -> dict:
        """Full team payload for a page that just connected; also seeds the
        diff baseline so the next push carries only what changed."""
        mon = self._monitor
        if mon is None:
            return {"enabled": False}
        ts = mon.snapshot(); ts["enabled"] = True
        users = ts.pop("users")
        with self._stream_lock:
            self._team_last = {u["user_id"]: json.dumps(u, sort_keys=True, default=str) for u in users}
            self._team_meta_last = json.dumps({k: v for k, v in ts.items() if k not in ("t", "latency", "last_poll")},
                                              sort_keys=True, default=str)
            seq = self._team_seq
        return {"seq": seq, "full": True, "users": users, "removed": [], "meta": ts, "t": ts.get("t", time.time())}

    def _stream_ticker(self) -> None:
        last_state = 0.0
        while True:
            fired = self._team_dirty.wait(timeout=0.2)
            if fired:
                self._team_dirty.clear()
                time.sleep(0.015)                     # coalesce a burst of events into one push
                self._team_dirty.clear()
                try:
                    self._push_team()
                except Exception:                   # pragma: no cover
                    log.exception("team push failed")
            now = time.time()
            if now - last_state >= 0.2:
                last_state = now
                try:
                    self._push_state()
                except Exception:                   # pragma: no cover
                    log.exception("state push failed")

    def _presence_changed(self, person: dict, old: str, new: str) -> None:
        w = self.watcher
        if w is not None:
            w.on_change(person, old, new)

    def ensure_watcher(self):
        if self.watcher is None:
            from .presence import AvailabilityWatcher
            path = os.path.join(os.path.dirname(os.path.abspath(self.contacts_path)), "watch.json")
            self.watcher = AvailabilityWatcher(path, notify=self._notify)
        return self.watcher

    def _notify(self, n: dict) -> None:
        with self.lock:
            n = dict(n, id=self.next_id)
            self.notifications.append(n)
            del self.notifications[:-20]
        self.add("notify", n["text"])

    def _call_dict(self, call) -> dict:
        return {"state": call.state.value, "number": call.remote_number,
                "name": self.lookup(call.remote_number) or call.remote_display,
                "direction": call.direction,
                "elapsed": self._fmt(time.time() - call.started_at) if call.started_at else "",
                "codec": call.rtp.codec if call.rtp else "", "hold": call.on_hold,
                "local_hold": call.local_hold, "muted": call.muted,
                "transferring": call.transferring, "conference": call.in_conference}

    def snapshot(self, since: int) -> dict:
        p = self.phone
        call = p.primary_call() or p.active_call
        consult = p.consult_call()
        c = self._call_dict(call) if call else None
        if c and consult:
            c["consult"] = self._call_dict(consult)
        tts = None
        if hasattr(self.audio, "say") and (self.voice is None or self.voice.tts_enabled):
            tts = {"queued": round(self.audio.queued_seconds(), 1), "speaking": self.audio.speaking}
        stt_obj = getattr(self.audio, "stt", None)
        stt = ({"busy": stt_obj.busy, "enabled": stt_obj.enabled}
               if stt_obj is not None and (self.voice is None or self.voice.stt_enabled) else None)
        with self.lock:
            ev = [e for e in self.events if e["id"] > since]
        from . import __version__
        upd = self.updater.status() if self.updater is not None else None
        try:
            from .extension_sync import build_id
            ext_build = build_id()
        except Exception:
            ext_build = ""
        return {"registered": p.registered, "stt": stt, "can_quit": self.on_quit is not None,
                "extension_build": ext_build,
                "mode": "presence" if p.presence_only else "phone", "started_mode": self.started_mode,
                "update": upd,
                "has_monitor": self.monitor is not None, "version": __version__,
                "has_settings": self.settings is not None,
                "needs_setup": bool(self.settings and self.settings.setup_needed),
                "restart_pending": bool(self.settings and self.settings.pending),
                "warnings": self._warnings(),
                "display_name": p.cfg.display_name, "pickup_code": self.pickup_code, "reg_detail": self.reg_detail,
                "account": f"{p.cfg.username}@{p.cfg.domain}",
                "expires_in": max(0, int(p.register_expiry - time.time())) if p.register_expiry else 0,
                "call": c, "tts": tts, "events": ev, "audio": getattr(self.audio, "name", "none"),
                "notifications": list(self.notifications[-5:]), "script": self.script_runner.status()}

    # -- quick phrases persisted next to the config ------------------------
    def phrases(self) -> list:
        try:
            with open(self.phrases_path, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            return lines
        except FileNotFoundError:
            return list(DEFAULT_PHRASES)

    # -- contacts --------------------------------------------------------------
    def contacts(self) -> list:
        try:
            with open(self.contacts_path, encoding="utf-8") as f:
                data = json.load(f)
            return [c for c in data if isinstance(c, dict) and c.get("number")]
        except (FileNotFoundError, ValueError):
            return []

    def save_contacts(self, items: list) -> list:
        clean = []
        for c in items:
            number = "".join(ch for ch in str(c.get("number", "")) if ch not in " -()")
            name = str(c.get("name", "")).strip()
            if number:
                clean.append({"name": name or number, "number": number})
        clean.sort(key=lambda c: c["name"].lower())
        with open(self.contacts_path, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        return clean

    # -- boards: custom tabs mixing teammates and contacts; pinned built-in tabs ----
    BUILTIN_TABS = ("team", "contacts", "recent")

    def boards(self) -> dict:
        try:
            with open(self.boards_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError
        except (FileNotFoundError, ValueError):
            data = {}
        hidden = [t for t in data.get("hidden", []) if t in self.BUILTIN_TABS]
        boards = []
        for b in data.get("boards", []):
            if isinstance(b, dict) and str(b.get("name", "")).strip():
                boards.append(self._clean_board(b))
        return {"hidden": hidden, "boards": boards}

    @staticmethod
    def _clean_board(b: dict) -> dict:
        members, seen = [], set()
        for m in b.get("members", []):
            if not isinstance(m, dict):
                continue
            kind, ident = str(m.get("type", "")).strip(), str(m.get("id", "")).strip()
            if kind in ("team", "contact", "service") and ident and (kind, ident) not in seen:
                seen.add((kind, ident))
                members.append({"type": kind, "id": ident})
        bid = str(b.get("id", "")).strip() or f"b{int(time.time() * 1000)}"
        return {"id": bid, "name": str(b.get("name", "")).strip()[:40], "members": members}

    def save_boards(self, data: dict) -> dict:
        hidden = [t for t in data.get("hidden", []) if t in self.BUILTIN_TABS]
        boards = [self._clean_board(b) for b in data.get("boards", [])
                  if isinstance(b, dict) and str(b.get("name", "")).strip()]
        ids = set()
        for b in boards:                                   # unique ids
            while b["id"] in ids:
                b["id"] += "x"
            ids.add(b["id"])
        if len(hidden) >= len(self.BUILTIN_TABS) and not boards:
            raise ValueError("Keep at least one tab pinned")
        clean = {"hidden": hidden, "boards": boards}
        with open(self.boards_path, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        return clean

    def lookup(self, number: str) -> str:
        for c in self.contacts():
            if c["number"][-10:] == number[-10:] and len(number) >= 7:
                return c["name"]
        return ""

    def save_phrases(self, lines: list) -> None:
        with open(self.phrases_path, "w", encoding="utf-8") as f:
            f.write("\n".join(l.strip() for l in lines if l.strip()) + "\n")


EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://")


def make_handler(state: UIState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

        # -- who may talk to us -------------------------------------------------
        # The API only listens on loopback, but any web page you visit could
        # still try to reach it from your browser. So:
        #  * Host must be 127.0.0.1/localhost (defeats DNS rebinding),
        #  * POSTs must be JSON and come from this page, the extension, or a
        #    local program (no Origin header) — never from another website.
        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").strip().lower()
            return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True                    # native host, app, curl: not a browser page
            port = self.server.server_address[1]
            origin = origin.strip().lower()
            return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}") \
                or origin.startswith(EXTENSION_SCHEMES)

        def _upload(self, ctype: str, ctype_full: str, n: int) -> None:
            """Settings -> Updates -> 'Install from file': a .whl or the share zip."""
            from . import updater
            if state.updater is None:
                return self._json({"error": "Updates aren't available in this mode"}, 400)
            if ctype != "multipart/form-data":
                return self._forbid("Content-Type must be multipart/form-data")
            if n > updater.MAX_WHEEL + 10_000:
                return self._json({"error": "That file is too large to be a Nextiva Companion App update"}, 413)
            raw = self.rfile.read(n)
            from email.parser import BytesParser
            from email.policy import default as _policy
            msg = BytesParser(policy=_policy).parsebytes(b"Content-Type: " + ctype_full.encode() + b"\r\n\r\n" + raw)
            name, data = "", b""
            for part in msg.iter_parts():
                if part.get_filename():
                    name, data = part.get_filename(), part.get_payload(decode=True) or b""
                    break
            if not name:
                return self._json({"error": "No file was sent"}, 400)
            try:
                path, ver = updater.stage(name, data)
                if state.updater.on_quit is None:
                    return self._json({"ok": True, "staged": True, "version": ver, "path": path,
                                       "message": f"Saved {ver}. Install it with:  python -m pip install --upgrade \"{path}\""})
                return self._json(state.updater.install_file(path))
            except (ValueError, RuntimeError) as e:
                return self._json({"error": str(e)}, 400)

        def _forbid(self, why: str) -> None:
            log.warning("blocked %s %s: %s", self.command, self.path, why)
            self._json({"error": f"forbidden: {why}"}, 403)

        def _stream(self, since: int) -> None:
            """Server-Sent Events: the page gets pushed instead of polling."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = state.stream_open(since)

            def send(kind: str, payload) -> None:
                data = json.dumps(payload, default=str)
                self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                self.wfile.flush()
            try:
                # opening snapshot: the page can paint without any other request
                snap = state.snapshot(since)
                if snap["events"]:
                    q.since = snap["events"][-1]["id"]   # type: ignore[attr-defined]
                send("state", snap)
                send("team", state.team_opening())
                last = time.time()
                while True:
                    try:
                        kind, payload = q.get(timeout=1.0)
                    except queue.Empty:
                        if time.time() - last >= 3:        # also how a gone client is noticed
                            self.wfile.write(b": keep-alive\n\n"); self.wfile.flush()
                            last = time.time()
                        continue
                    send(kind, payload)
                    last = time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.stream_close(q)

        def _json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _voice_post(self, path: str, body: dict, v):
            r = v.registry
            if path == "/api/voice/providers/add":
                p = r.add(str(body.get("type", "")), str(body.get("name", "")), body.get("config") or {}, enabled=bool(body.get("enabled", True)))
                if body.get("make_default"):
                    r.set_defaults(**({"stt": p["id"]} if p.get("stt") else {"tts": p["id"]}))
                return self._json({"provider": p, **v.status()})
            if path == "/api/voice/providers/update":
                r.update(str(body.get("id", "")), name=body.get("name"), config=body.get("config"), enabled=body.get("enabled"))
                return self._json(v.status())
            if path == "/api/voice/providers/remove":
                r.remove(str(body.get("id", "")))
                return self._json(v.status())
            if path == "/api/voice/defaults":
                allowed = {k: body[k] for k in ("tts", "tts_fallback", "stt", "voice", "rate", "pitch", "volume", "streaming", "assignments") if k in body}
                for k in ("rate", "pitch", "volume"):
                    if k in allowed:
                        allowed[k] = max(0.25, min(4.0, float(allowed[k])))
                r.set_defaults(**allowed)
                return self._json(v.status())
            if path == "/api/voice/test":
                wav, err = v.test_voice(str(body.get("provider", "")), str(body.get("voice", "")), str(body.get("text", "")),
                                        body.get("rate"), body.get("volume"))
                if err:
                    return self._json({"error": err}, 400)
                self.send_response(200); self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav))); self.end_headers(); self.wfile.write(wav)
                return None
            if path == "/api/voice/say":
                if not hasattr(state.audio, "say"):
                    return self._json({"error": "no audio path"}, 400)
                state.audio.say(str(body.get("text", "")))
                return self._json({"ok": True})
            if path == "/api/voice/models/download":
                return self._json(v.models.download(str(body.get("id", ""))))
            if path == "/api/voice/models/remove":
                v.models.remove(str(body.get("id", "")))
                return self._json(v.models.status())
            if path == "/api/voice/models/verify":
                return self._json(v.models.verify(str(body.get("id", ""))))
            if path == "/api/voice/models/dir":
                d = str(body.get("path", "")).strip()
                if not d or not os.path.isdir(os.path.dirname(os.path.abspath(d))):
                    return self._json({"error": "folder not found"}, 400)
                v.models.set_dir(d); v.models_dir = d; v.registry.models_dir = d
                for pid in list(v.registry._instances):
                    v.registry._drop_instance(pid)
                return self._json(v.models.status())
            return self._json({"error": "not found"}, 404)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._host_ok():
                return self._forbid("bad Host header")
            if u.path == "/":
                with open(HTML_PATH, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/state":
                since = 0
                for kv in u.query.split("&"):
                    if kv.startswith("since="):
                        since = int(kv[6:] or 0)
                self._json(state.snapshot(since))
            elif u.path == "/api/stream":
                self._stream(int((q.get("since") or ["0"])[0] or 0))
            elif u.path == "/api/phrases":
                self._json({"phrases": state.phrases()})
            elif u.path == "/api/scripts":
                self._json({"scripts": state.scripts.load(), "default_pause": __import__("sipphone.scripts", fromlist=["x"]).DEFAULT_PAUSE})
            elif u.path == "/api/scripts/export":
                from .scripts import to_text
                sc = next((x for x in state.scripts.load() if x["id"] == q.get("id", [""])[0]), None)
                if not sc:
                    return self._json({"error": "not found"}, 404)
                data = to_text(sc["steps"]).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{sc["name"]}.txt"')
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            elif u.path == "/api/contacts":
                self._json({"contacts": state.contacts()})
            elif u.path == "/api/boards":
                self._json(state.boards())
            elif u.path == "/api/history":
                self._json({"history": state.history()})
            elif u.path == "/api/version":
                from . import __version__
                self._json({"version": __version__})
            elif u.path == "/api/settings":
                if state.settings is None:
                    return self._json({"error": "settings are not available in this mode"}, 404)
                self._json(state.settings.get())
            elif u.path == "/api/update":
                self._json(state.updater.status() if state.updater is not None else {"enabled": False})
            elif u.path == "/api/extension":
                from . import extension_sync as ext
                from .native_host import bridge_status
                from .native_host import KNOWN_EXTENSIONS
                self._json({"build": ext.build_id(), "version": __import__("sipphone").__version__,
                            "zip_name": ext.zip_name(), "folder": ext.extension_dir(),
                            "downloads": ext.downloads_dir(), "bridge": bridge_status(),
                            "known": [{"id": i, "kind": k} for i, k in KNOWN_EXTENSIONS.items()],
                            "connected": getattr(state, "extension_seen", None)})
            elif u.path == "/api/extension.zip":
                # the droppable zip, as a download (Settings -> Browser extension)
                from . import extension_sync as ext
                data = ext.zip_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{ext.zip_name()}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/monitor":
                if state.monitor is None:
                    self._json({"enabled": False})
                else:
                    snap = state.monitor.snapshot(); snap["enabled"] = True
                    self._json(snap)
            elif u.path == "/api/voice":
                v = state.voice
                self._json({"enabled": v is not None, **(v.status() if v is not None else {})})
            elif u.path == "/api/voice/models":
                v = state.voice
                self._json(v.models.status() if v is not None else {"catalog": []})
            elif u.path == "/api/presence/health":
                m = state.monitor
                self._json({"enabled": m is not None, **(m.health() if m is not None and getattr(m, "health", None) else {})})
            elif u.path == "/api/presence/person":
                m = state.monitor
                uid = q.get("id", [""])[0]
                p = m.person(uid) if m is not None and getattr(m, "person", None) and uid else None
                self._json(p or {"error": "not found"}, 200 if p else 404)
            elif u.path == "/api/presence/search":
                from .presence import search_people, filter_people
                snap = state.monitor.snapshot() if state.monitor is not None else {"users": []}
                people = filter_people(snap["users"], statuses=[x for x in q.get("status", [""])[0].split(",") if x],
                                       department=q.get("department", [""])[0], location=q.get("location", [""])[0],
                                       on_call_only=q.get("on_call", [""])[0] in ("1", "true"),
                                       monitored_only=q.get("monitored", [""])[0] in ("1", "true"))
                self._json({"users": search_people(people, q.get("q", [""])[0], int(q.get("limit", ["0"])[0] or 0))})
            elif u.path == "/api/presence/org-groups":
                mon = state.monitor
                if mon is None or not hasattr(mon, "org_groups"):
                    return self._json({"groups": [], "error": "Team view is off"})
                self._json({"groups": mon.org_groups()})
            elif u.path == "/api/presence/groups":
                mon = state.monitor
                if mon is None or not hasattr(mon, "groups"):
                    return self._json({"groups": [], "error": "Team view is off"})
                self._json({"groups": mon.groups()})
            elif u.path == "/api/presence/watch":
                w = state.ensure_watcher()
                self._json({"rules": w.list_rules(), "events": __import__("sipphone.presence.notifications", fromlist=["x"]).EVENTS,
                            "recent": state.notifications[-20:]})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._host_ok():
                return self._forbid("bad Host header")
            if not self._origin_ok():
                return self._forbid("requests from other websites are not allowed")
            ctype_full = self.headers.get("Content-Type") or ""
            ctype = ctype_full.split(";")[0].strip().lower()
            n = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/update/upload":
                return self._upload(ctype, ctype_full, n)
            if ctype != "application/json":
                return self._forbid("Content-Type must be application/json")
            if n > 1_000_000:
                return self._json({"error": "request too large"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json({"error": "bad JSON"}, 400)
            if not isinstance(body, dict):
                body = {}
            p, aud = state.phone, state.audio
            try:
                if u.path == "/api/update/check":
                    if state.updater is None:
                        raise RuntimeError("Updates aren't available in this mode")
                    return self._json(state.updater.check_now())
                elif u.path == "/api/update/install":
                    if state.updater is None:
                        raise RuntimeError("Updates aren't available in this mode")
                    return self._json(state.updater.install_available())
                elif u.path == "/api/extension/zip":
                    # save the droppable zip (default: Downloads); used by the bridge
                    # when a zip-installed extension needs new files
                    from . import extension_sync as ext
                    to = str(body.get("to", "") or "")
                    path = ext.write_zip("" if to in ("", "downloads") else to)
                    return self._json({"ok": True, "path": path, "name": os.path.basename(path),
                                       "folder": os.path.dirname(path)})
                elif u.path == "/api/extension/register-bridge":
                    from .native_host import register_bridge
                    r = register_bridge()
                    if not r["ok"]:
                        raise RuntimeError(r["note"] or "couldn't register the bridge")
                    return self._json({"ok": True, "where": r["where"], "path": r["path"]})
                elif u.path == "/api/presence/metrics":
                    # the page reports how long after the engine applied a change it was painted
                    mon = state.monitor
                    vals = body.get("render") or []
                    if mon is not None and isinstance(vals, list):
                        for v in vals[:200]:
                            try:
                                mon.metrics.rendered(float(v))
                            except (TypeError, ValueError):
                                pass
                    return self._json({"ok": True})
                elif u.path == "/api/extension/seen":
                    # the bridge reports which copy of the extension launched it
                    ext_id = str(body.get("id", "") or "").strip().lower()
                    if ext_id and not (len(ext_id) == 32 and ext_id.isalpha()):
                        raise RuntimeError("not an extension ID")
                    state.extension_seen = {"id": ext_id, "kind": str(body.get("kind", "") or "")[:40],
                                            "at": time.time()}
                    return self._json({"ok": True})
                elif u.path == "/api/extension/hello":
                    # A copy of the extension the bridge doesn't know yet (a Chrome
                    # Web Store install has its own ID) introduces itself. The ID is
                    # taken from the request's Origin — set by the browser, not the
                    # page — and must match what the body claims; then it joins the
                    # bridge's allow-list and the manifest is rewritten so the next
                    # connectNative succeeds. An extension that can reach this API
                    # already has everything the bridge offers, so nothing is gained
                    # by making it ask a human first.
                    from .native_host import allowed_extension_ids, register_bridge
                    ext_id = str(body.get("id", "") or "").strip().lower()
                    origin = (self.headers.get("Origin") or "").strip().lower().rstrip("/")
                    if not (len(ext_id) == 32 and ext_id.isalpha()):
                        raise RuntimeError("not an extension ID")
                    if origin != f"chrome-extension://{ext_id}":
                        return self._forbid("extension ID doesn't match the request origin")
                    added = False
                    if ext_id not in allowed_extension_ids():
                        if state.settings is None:
                            raise RuntimeError("Settings are not available in this mode")
                        from . import config_store
                        cp = config_store.read(state.settings.path)
                        cur = cp.get("extension", "ids", fallback="") if cp.has_section("extension") else ""
                        ids = [t.strip() for t in cur.replace(";", ",").split(",") if t.strip()]
                        ids.append(ext_id)
                        config_store.update(state.settings.path, {"extension": {"ids": ",".join(ids)}})
                        added = True
                        log.info("browser extension %s added to the bridge allow-list", ext_id)
                    r = register_bridge()
                    return self._json({"ok": bool(r["ok"]), "added": added, "known": True,
                                       "registered": bool(r["ok"]), "note": r.get("note", "")})
                elif u.path == "/api/extension/open":
                    from . import extension_sync as ext
                    page = ext.open_extensions_page()
                    if not page:
                        raise RuntimeError("Couldn't find Chrome or Edge — open chrome://extensions yourself")
                    return self._json({"ok": True, "page": page})
                elif u.path == "/api/settings":
                    if state.settings is None:
                        raise RuntimeError("Settings are not available in this mode")
                    return self._json(state.settings.save(body.get("values", body)))
                elif u.path == "/api/settings/test-admin":
                    if state.settings is None:
                        raise RuntimeError("Settings are not available in this mode")
                    return self._json(state.settings.test_admin(body.get("values", body)))
                elif u.path == "/api/restart":
                    if state.settings is None or state.settings.on_restart is None:
                        raise RuntimeError("Restart isn't available here — close and reopen Nextiva Companion App")
                    state.add("event", "Restarting…")
                    threading.Timer(0.3, state.settings.on_restart).start()
                elif u.path == "/api/call":
                    number = "".join(ch for ch in str(body.get("number", "")) if ch not in " -()")
                    if not number:
                        raise RuntimeError("Enter a number to call")
                    p.call(number)
                elif u.path == "/api/answer":
                    p.answer()
                elif u.path == "/api/reject":
                    p.reject()
                elif u.path == "/api/hangup":
                    # end every live leg (covers consult/conference too)
                    import sipphone.phone as _ph
                    for cl in [c for c in p.calls.values() if c.state != _ph.CallState.ENDED]:
                        p.hangup(cl.call_id)
                elif u.path == "/api/hold":
                    pc = p.primary_call() or p.active_call
                    if pc and pc.local_hold:
                        p.resume(pc.call_id); state.add("event", "Resumed")
                    else:
                        p.hold(pc.call_id if pc else None); state.add("event", "On hold")
                elif u.path == "/api/mute":
                    on = not (p.active_call.muted if p.active_call else False)
                    p.mute(on); state.add("event", "Microphone muted" if on else "Microphone on")
                elif u.path == "/api/pickup":
                    ext = "".join(ch for ch in str(body.get("extension", "")) if ch not in " -()")
                    if not ext:
                        raise RuntimeError("No extension to pick up")
                    p.call(f"{state.pickup_code}{ext}")
                    state.add("event", f"Answering call on extension {ext}")
                elif u.path == "/api/transfer":
                    target = "".join(ch for ch in str(body.get("target", "")) if ch not in " -()")
                    if not target:
                        raise RuntimeError("Enter a number to transfer to")
                    p.transfer(target)
                    state.add("event", f"Transferring to {state.lookup(target) or target}…")
                elif u.path == "/api/consult":
                    target = "".join(ch for ch in str(body.get("target", "")) if ch not in " -()")
                    if not target:
                        raise RuntimeError("Enter a number to consult")
                    p.consult(target)
                    state.add("event", f"Consulting {state.lookup(target) or target} (call held)")
                elif u.path == "/api/consult-complete":
                    p.complete_transfer()
                    state.add("event", "Completing transfer")
                elif u.path == "/api/consult-cancel":
                    p.cancel_consult()
                    state.add("event", "Cancelled — back to the held call")
                elif u.path == "/api/conference":
                    target = "".join(ch for ch in str(body.get("target", "")) if ch not in " -()")
                    p.conference(target or None)
                    state.add("event", "Conferencing" + (f" {state.lookup(target) or target}" if target else " the two calls"))
                elif u.path == "/api/stt":
                    stt = getattr(aud, "stt", None)
                    if stt is None:
                        raise RuntimeError("Speech-to-text is off. Start with --stt whisper")
                    stt.enabled = bool(body.get("enabled", True))
                elif u.path == "/api/dtmf":
                    digits = str(body.get("digits", ""))
                    p.send_dtmf(digits)
                    state.add("dtmf", digits)
                elif u.path == "/api/say":
                    text = str(body.get("text", "")).strip()
                    if not hasattr(aud, "say"):
                        raise RuntimeError("Text-to-speech is off. Start with --tts piper")
                    call = p.active_call
                    if not call or call.state != CallState.CONNECTED:
                        raise RuntimeError("Connect a call first, then speak")
                    if text:
                        aud.say(text)
                        state.add("said", text)
                elif u.path == "/api/shh":
                    state.script_runner.stop()
                    if hasattr(aud, "stop_speaking"):
                        aud.stop_speaking()
                        state.add("event", "Stopped speaking")
                elif u.path == "/api/register":
                    p.register()
                elif u.path == "/api/quit":
                    if state.on_quit is None:
                        raise RuntimeError("Quit from the terminal window (Ctrl+C)")
                    state.add("event", "Shutting down")
                    threading.Timer(0.3, state.on_quit).start()
                elif u.path == "/api/phrases":
                    state.save_phrases(list(body.get("phrases", [])))
                    return self._json({"phrases": state.phrases()})
                elif u.path == "/api/scripts":
                    return self._json({"scripts": state.scripts.save(list(body.get("scripts", [])))})
                elif u.path == "/api/scripts/import":
                    from .scripts import parse_text, DEFAULT_PAUSE
                    steps = parse_text(str(body.get("text", "")), float(body.get("default_pause", DEFAULT_PAUSE) or DEFAULT_PAUSE))
                    if not steps:
                        return self._json({"error": "No lines found in that file"}, 400)
                    scripts = state.scripts.load()
                    name = str(body.get("name", "")).strip() or "Imported script"
                    scripts.append({"name": name, "steps": steps})
                    return self._json({"scripts": state.scripts.save(scripts)})
                elif u.path == "/api/scripts/run":
                    sc = next((x for x in state.scripts.load() if x["id"] == str(body.get("id", ""))), None)
                    if not sc:
                        return self._json({"error": "script not found"}, 404)
                    state.script_runner.run(sc)
                    return self._json({"script": state.script_runner.status()})
                elif u.path == "/api/scripts/stop":
                    state.script_runner.stop()
                    return self._json({"script": state.script_runner.status()})
                elif u.path == "/api/contacts":
                    return self._json({"contacts": state.save_contacts(list(body.get("contacts", [])))})
                elif u.path == "/api/boards":
                    return self._json(state.save_boards(body))
                elif u.path.startswith("/api/voice/"):
                    v = state.voice
                    if v is None:
                        return self._json({"error": "voice engine not running"}, 400)
                    try:
                        return self._voice_post(u.path, body, v)
                    except (ValueError, KeyError) as e:
                        return self._json({"error": str(e)}, 400)
                elif u.path == "/api/presence/org-groups":
                    mon = state.monitor
                    if mon is None or not hasattr(mon, "rename_org_group"):
                        return self._json({"error": "Team view is off — choose a presence source first"}, 400)
                    gid = str(body.get("id", "")).strip()
                    if not gid or gid not in {g["id"] for g in mon.org_groups()}:
                        return self._json({"error": "unknown group"}, 400)
                    mon.rename_org_group(gid, str(body.get("name", "") or ""))
                    return self._json({"ok": True, "groups": mon.org_groups()})
                elif u.path == "/api/presence/groups":
                    mon = state.monitor
                    if mon is None or not hasattr(mon, "rename_group"):
                        return self._json({"error": "Team view is off — choose a presence source first"}, 400)
                    uid = str(body.get("user_id", "")).strip()
                    if not uid or uid not in {g["user_id"] for g in mon.groups()}:
                        return self._json({"error": "unknown group"}, 400)
                    mon.rename_group(uid, str(body.get("name", "") or ""))
                    return self._json({"ok": True, "groups": mon.groups()})
                elif u.path == "/api/presence/watch":
                    w = state.ensure_watcher()
                    uid = str(body.get("user_id", "")).strip()
                    if not uid:
                        return self._json({"error": "user_id required"}, 400)
                    rule = w.set_rule(uid, list(body.get("events", [])), name=str(body.get("name", "")),
                                      once=bool(body.get("once", True)), sound=bool(body.get("sound", True)))
                    return self._json({"rule": rule, "rules": w.list_rules()})
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except (RuntimeError, ValueError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # pragma: no cover
                log.exception("ui action failed")
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def serve(phone: SIPPhone, audio, host: str = "127.0.0.1", port: int = 8088,
          open_browser: bool = True, phrases_path: str = "phrases.txt",
          contacts_path: str = "contacts.json", history_path: str = "history.json",
          on_quit=None, monitor=None, pickup_code: str = "*97", config_path: str = "",
          build_monitor=None, on_restart=None, warnings=None, started_mode: str = "",
          update_opts=None, updater=None, voice=None) -> ThreadingHTTPServer:
    state = UIState(phone, audio, phrases_path, contacts_path, history_path)
    state.voice = voice
    state.on_quit = on_quit
    state.monitor = monitor
    state.pickup_code = pickup_code or "*97"
    state.warnings = list(warnings or [])
    state.started_mode = started_mode or state.mode
    state.updater = updater
    if config_path:
        from .settings import SettingsManager
        state.settings = SettingsManager(config_path, phone, state, build_monitor=build_monitor,
                                         on_restart=on_restart, started_mode=state.started_mode,
                                         on_update_opts=(updater.configure if updater is not None else None))
        if updater is not None and update_opts:
            updater.configure(update_opts)
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    srv.daemon_threads = True
    srv.ui_state = state  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, name="sip-ui", daemon=True).start()
    url = f"http://{host}:{srv.server_address[1]}/"
    log.info("UI at %s", url)
    print(f"*** UI: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    return srv

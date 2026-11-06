"""PresenceManager: the one presence engine, for a user login or an Admin XSI login.

  directory  ──►  targets  ◄──  events channel (Standard Call + Do Not Disturb)
                     │
                 snapshot() / health() / person()  → web UI, extension, notifications

Nothing here polls for presence. The directory is re-read every 15 minutes
(people join and leave), subscriptions are renewed before they lapse, and an
optional registration sweep (every 5 min, admin logins usually) tells Offline
from Available. Everything else arrives as events.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Dict, List, Optional

from ..authentication import AdminCredentials, UserCredentials
from ..xsi import XSIClient, XSIError
from .connection import EXPIRES, EventsClient
from .directory import Directory
from .event_processor import EventProcessor
from .models import (BUSY, MONITOR_ERROR, MONITOR_OK, MONITOR_PENDING, MONITOR_REFUSED, STATE_LABELS, MonitoredUser,
                     Target, person_dict, sort_people, status_of)
from .subscriptions import SubscriptionManager

log = logging.getLogger("sipphone.presence")

HEARTBEAT = 15.0
STALE_AFTER = 90.0            # no heartbeat/event for this long → presence is stale
REGISTRATION_SWEEP = 300.0
DIRECTORY_REFRESH = 900.0
RETRY_PENDING_EVERY = 300.0

ChangeListener = Callable[[dict, str, str], None]     # (person, old_status, new_status)


class PresenceManager:
    """Same start()/stop()/snapshot() shape the web UI and extension bridge expect."""

    def __init__(self, credentials, cache_path: str = "", users: Optional[List[str]] = None,
                 hide: Optional[List[str]] = None, show_services: Optional[List[str]] = None,
                 concurrency: int = 6, registration_sweep: bool = True,
                 client: Optional[EventsClient] = None, actions: Optional[XSIClient] = None,
                 expires: int = EXPIRES) -> None:
        self.creds = credentials
        self.kind = getattr(credentials, "kind", "user")
        self.actions = actions or XSIClient(credentials.host, credentials.login, credentials.password,
                                            verify_tls=credentials.verify_tls, user_domain=credentials.user_domain)
        self.client = client or EventsClient(credentials.host, credentials.login, credentials.password,
                                             credentials.verify_tls)
        self.directory = Directory(self.actions, credentials.directory_seed,
                                   getattr(credentials, "directory_type", "Enterprise"),
                                   cache_path=cache_path, hide=hide, user_domain=credentials.user_domain,
                                   show_services=show_services, refresh_seconds=DIRECTORY_REFRESH)
        self.subs = SubscriptionManager(self.client, concurrency=concurrency, expires=expires)
        self.configured = [u.strip() for u in (users or []) if u.strip()]
        self.registration_sweep = registration_sweep
        self._sweep_allowed: Optional[bool] = None
        self.targets: Dict[str, Target] = {}
        self.order: List[str] = []
        self._lock = threading.RLock()
        self.processor = EventProcessor(self.targets, self._ack_later, lock=self._lock)
        self.ready = False
        self.connected = False
        self.last_error = ""
        self.last_heartbeat = 0.0
        self.reconnects: List[dict] = []                      # [{at, reason}] newest last
        self.session_started = 0.0
        self.ack_error = ""
        self._acks: List[str] = []
        self._ack_thread: Optional[threading.Thread] = None
        self._prev_status: Dict[str, str] = {}
        self.listeners: List[ChangeListener] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="presence", daemon=True)
        self.list_source = "configured list" if self.configured else "company directory"

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.subs.unsubscribe_all(list(self.targets.values()))
            if self.client.channel_id:
                self.client.delete_channel()
        except Exception:
            pass
        self.client.close_stream()
        self.client.close_pool()

    def on_change(self, fn: ChangeListener) -> None:
        self.listeners.append(fn)

    # -- who to watch -----------------------------------------------------------
    def _targets_from_directory(self) -> None:
        """(Re)build the target list from the directory, keeping live state for
        people we already have so a reconnect doesn't blank the Team view."""
        if self.configured:
            people = [self.directory.find(u) or MonitoredUser(user_id=self.actions._uid(u)) for u in self.configured]
            for p in people:
                p.user_id = self.actions._uid(p.user_id)
                p.name = p.name or p.user_id.split("@")[0]
        else:
            people = [MonitoredUser(**{k: getattr(d, k) for k in ("user_id", "name", "first", "last", "number", "extension",
                                                                   "department", "title", "email", "mobile", "location", "group")})
                      for d in self.directory.people]
            for p in people:
                p.user_id = self.actions._uid(p.user_id)
            if not people and not self.directory.from_cache:
                raise XSIError("the company directory lists no people")
        with self._lock:
            new: Dict[str, Target] = {}
            for p in people:
                old = self.targets.get(p.user_id)
                if old is not None:
                    old.user = p
                    new[p.user_id] = old
                else:
                    new[p.user_id] = Target(user=p)
            self.targets.clear()
            self.targets.update(new)
            self.order = [p.user_id for p in people]

    # -- main loop -----------------------------------------------------------------
    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 5.0
            except XSIError as e:
                self._session_failed(str(e))
            except Exception as e:                     # pragma: no cover
                self._session_failed(f"{type(e).__name__}: {e}")
                log.exception("presence session failed")
            finally:
                self.client.close_stream()
            if self._stop.wait(backoff + random.uniform(0, backoff / 2)):   # exponential backoff with jitter
                return
            backoff = min(backoff * 2, 120.0)

    def _session_failed(self, reason: str) -> None:
        with self._lock:
            self.last_error = reason
            self.connected = False
            self.reconnects.append({"at": time.time(), "reason": reason})
            del self.reconnects[:-20]
        log.warning("presence: %s", reason)

    def _session(self) -> None:
        self.session_started = time.time()
        try:
            self.directory.refresh()
        except XSIError as e:
            if not self.directory.entries and not self.configured:
                raise
            log.warning("directory refresh failed (%s); using the cached copy", e)
        self._targets_from_directory()
        with self._lock:
            self.last_error = ""
        stream, splitter, pending = self.client.open_channel(expires=self.subs.expires)
        with self._lock:
            self.connected = True
            self.last_heartbeat = time.time()
        log.info("event channel %s open; subscribing to %d people (%s)", self.client.channel_id,
                 len(self.targets), self.creds.describe())
        self.subs.subscribe_all(list(self.targets.values()), self._stop)
        if self._stop.is_set():
            return
        with self._lock:
            refused = sum(1 for t in self.targets.values() if t.monitor == MONITOR_REFUSED)
            errors = [t.error for t in self.targets.values() if t.monitor == MONITOR_ERROR]
            self.ready = True
            if self.targets and refused == len(self.targets):
                self.last_error = ("the phone system lets this login watch nobody in the directory — "
                                   "ask your provider for monitoring rights")
            elif self.targets and len(errors) == len(self.targets):
                self.last_error = errors[0]
        for doc in pending:
            self._handle(doc)
        self._emit_changes()
        session_over = threading.Event()
        failure: List[str] = []

        def keepalive() -> None:
            beat = time.time()
            retried = swept = time.time()
            while not session_over.wait(0.5):
                now = time.time()
                try:
                    if now - beat >= HEARTBEAT:
                        self.client.heartbeat()
                        beat = now
                        with self._lock:
                            self.last_heartbeat = now
                    if now >= self.client.channel_expiry - self.subs.expires * 0.4:
                        self.client.refresh_channel(self.subs.expires)
                    with self._lock:
                        targets = list(self.targets.values())
                    self.subs.renew_due(targets)
                    if now - retried >= RETRY_PENDING_EVERY:
                        retried = now
                        if self.subs.retry_pending(targets):
                            self._emit_changes()
                    if self.directory.due():
                        try:
                            if self.directory.refresh():
                                self._targets_from_directory()
                                with self._lock:
                                    fresh = [t for t in self.targets.values() if t.monitor == MONITOR_PENDING]
                                self.subs.subscribe_all(fresh, self._stop)
                                self._emit_changes()
                        except XSIError as e:
                            log.warning("directory refresh failed: %s", e)
                    if self.registration_sweep and now - swept >= REGISTRATION_SWEEP:
                        swept = now
                        self._sweep_registrations(targets)
                except Exception as e:
                    failure.append(f"heartbeat failed: {e}")
                    self.client.close_stream()           # makes the read loop notice
                    return

        threading.Thread(target=keepalive, name="presence-keepalive", daemon=True).start()
        try:
            self._read_loop(stream, splitter)
        finally:
            session_over.set()
            with self._lock:
                self.connected = False
        if failure:
            raise XSIError(failure[0])

    def _read_loop(self, stream, splitter) -> None:
        import http.client
        import socket
        while not self._stop.is_set():
            try:
                chunk = stream.read1(4096)
            except socket.timeout:
                continue
            except (http.client.HTTPException, OSError, AttributeError, ValueError) as e:
                if self._stop.is_set():
                    return
                raise XSIError(f"the event channel dropped ({type(e).__name__})")
            if not chunk:
                raise XSIError("the event channel was closed by the server")
            for doc in splitter.feed(chunk):
                kind = self._handle(doc)
                if kind == "terminated":
                    raise XSIError("the event channel was terminated by the server")
            self._emit_changes()

    def _handle(self, doc: bytes) -> str:
        kind = self.processor.handle(doc)
        if kind == "heartbeat":
            with self._lock:
                self.last_heartbeat = time.time()
        return kind

    # -- registrations (Offline) ------------------------------------------------------
    def _sweep_registrations(self, targets: List[Target]) -> None:
        if self._sweep_allowed is False:
            return
        from concurrent.futures import ThreadPoolExecutor
        monitored = [t for t in targets if t.monitor == MONITOR_OK]

        def one(t: Target) -> None:
            if self._stop.is_set() or self._sweep_allowed is False:
                return
            try:
                t.registered = self.actions.registration_count(t.user.user_id) > 0
                self._sweep_allowed = True
            except XSIError as e:
                if "403" in str(e) or "forbidden" in str(e).lower():
                    self._sweep_allowed = False
                    log.info("registration sweep not permitted for this login; Offline won't be shown")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, monitored))
        self._emit_changes()

    # -- acknowledgements -----------------------------------------------------------------
    def _ack_later(self, event_id: str) -> None:
        with self._lock:
            self._acks.append(event_id)
            if self._ack_thread is None or not self._ack_thread.is_alive():
                self._ack_thread = threading.Thread(target=self._ack_worker, name="presence-ack", daemon=True)
                self._ack_thread.start()

    def _ack_worker(self) -> None:
        while True:
            with self._lock:
                if not self._acks:
                    self._ack_thread = None
                    return
                event_id = self._acks.pop(0)
            try:
                self.client.ack(event_id)
                if self.ack_error:
                    with self._lock:
                        self.ack_error = ""
            except Exception as e:
                if not self.ack_error:
                    log.warning("event acknowledgement failing (%s) — presence will lag", e)
                with self._lock:
                    self.ack_error = str(e)

    # -- change notifications -----------------------------------------------------------
    def _emit_changes(self) -> None:
        """Tell listeners about status transitions (only while live, never from
        stale or disconnected state — see section 7.4)."""
        if not self.listeners:
            return
        changes = []
        with self._lock:
            stale = self.is_stale()
            for uid in self.order:
                t = self.targets.get(uid)
                if t is None:
                    continue
                st = status_of(t)
                old = self._prev_status.get(uid)
                self._prev_status[uid] = st
                if old is not None and old != st and not stale and self.connected:
                    changes.append((person_dict(t, False), old, st))
        for person, old, new in changes:
            for fn in self.listeners:
                try:
                    fn(person, old, new)
                except Exception:                  # pragma: no cover
                    log.exception("presence listener failed")

    # -- reading -------------------------------------------------------------------------------
    def is_stale(self) -> bool:
        return (not self.connected) or (time.time() - max(self.last_heartbeat, self.processor.last_event) > STALE_AFTER)

    def person(self, user_id: str) -> Optional[dict]:
        """Everything we know about one person, for the details panel."""
        with self._lock:
            t = self.targets.get(user_id) or self.targets.get(self.actions._uid(user_id))
            if t is None:
                short = user_id.lower().split("@")[0]
                t = next((x for x in self.targets.values() if x.user.user_id.lower().split("@")[0] == short), None)
            if t is None:
                d = self.directory.find(user_id)
                return None if d is None else dict(d.to_dict(), monitor="none", stale=False, state_label="Unknown")
            d = person_dict(t, self.is_stale())
            d["calls"] = [{"call_id": c.call_id, "state": c.state, "personality": c.personality, "name": c.name,
                           "number": c.number, "start": c.start} for c in t.calls.values()]
            d["subscriptions"] = dict(t.subs)
            return d

    def health(self) -> dict:
        with self._lock:
            ts = list(self.targets.values())
            return {"connected": self.connected, "ready": self.ready, "stale": self.is_stale(),
                    "last_event": self.processor.last_event, "last_heartbeat": self.last_heartbeat,
                    "channel_id": self.client.channel_id, "channel_expiry": self.client.channel_expiry,
                    "session_started": self.session_started,
                    "subscriptions": {"ok": sum(1 for t in ts if t.monitor == MONITOR_OK),
                                      "refused": sum(1 for t in ts if t.monitor == MONITOR_REFUSED),
                                      "error": sum(1 for t in ts if t.monitor == MONITOR_ERROR),
                                      "pending": sum(1 for t in ts if t.monitor == MONITOR_PENDING),
                                      "next_expiry": min((t.sub_expiry for t in ts if t.sub_expiry), default=0.0),
                                      "renewals": self.subs.renewals},
                    "reconnects": list(self.reconnects), "ack_error": self.ack_error,
                    "duplicates_dropped": self.processor.duplicates, "stale_dropped": self.processor.stale_dropped,
                    "directory": self.directory.status(), "registration_sweep": self._sweep_allowed,
                    "login": self.creds.describe(), "last_error": self.last_error, "requests": self.client.requests}

    def snapshot(self) -> dict:
        with self._lock:
            stale = self.is_stale()
            out = [person_dict(self.targets[uid], stale) for uid in self.order if uid in self.targets]
            services = [x.to_dict() for x in self.directory.services]
            ready, err, connected = self.ready, self.last_error, self.connected
            refused = sum(1 for t in self.targets.values() if t.monitor == MONITOR_REFUSED)
            last = max(self.processor.last_event, self.last_heartbeat)
        sort_people(out)
        active = sum(1 for u in out if u["status"] in BUSY)
        return {"users": out, "services": services, "ready": ready, "last_poll": last, "last_error": err, "active": active,
                "total": len(out), "source": "events", "login": self.kind, "connected": connected, "stale": stale,
                "list_source": self.list_source, "refused": refused, "ack_error": self.ack_error,
                "directory": self.directory.status(), "states": STATE_LABELS}

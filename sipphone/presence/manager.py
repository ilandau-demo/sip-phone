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
import os
import random
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from ..authentication import AdminCredentials, UserCredentials
from ..xsi import XSIClient, XSIError
from .connection import EXPIRES, EventsClient
from .directory import Directory
from .event_processor import EventProcessor
from .metrics import LatencyMetrics
from .group_names import FILE_NAME as GROUP_NAMES_FILE, ORG_FILE_NAME as ORG_GROUP_NAMES_FILE, GroupNames
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
UpdateListener = Callable[[], None]                    # "something changed; read snapshot()"

MAX_CHANNELS = 8            # configurable ceiling; automatic choice stops at 4
_PLATFORM_TIME = re.compile(rb"<[^>]*?(?:answerTime|startTime)>(\d{10,})<")


def auto_channels(people: int) -> int:
    """How many event channels to open for a directory of this size. One
    channel moves about one event per round trip (the Xsp waits for each ack);
    busier teams get more channels, each with its own ack gate."""
    if people <= 50:
        return 1
    if people <= 150:
        return 2
    return 4


class PresenceManager:
    """Same start()/stop()/snapshot() shape the web UI and extension bridge expect."""

    def __init__(self, credentials, cache_path: str = "", users: Optional[List[str]] = None,
                 hide: Optional[List[str]] = None, show_services: Optional[List[str]] = None,
                 concurrency: int = 6, registration_sweep: bool = True,
                 client: Optional[EventsClient] = None, actions: Optional[XSIClient] = None,
                 expires: int = EXPIRES, channels: int = 0) -> None:
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
        self._ack_threads: List[threading.Thread] = []
        self._prev_status: Dict[str, str] = {}
        self.listeners: List[ChangeListener] = []
        self.update_listeners: List[UpdateListener] = []
        self.group_names = GroupNames(os.path.join(os.path.dirname(cache_path), GROUP_NAMES_FILE) if cache_path else "")
        # display names for the BroadWorks group a person belongs to (e.g. "3149647G" → "Arizona")
        self.org_group_names = GroupNames(os.path.join(os.path.dirname(cache_path), ORG_GROUP_NAMES_FILE) if cache_path else "")
        self.metrics = LatencyMetrics()
        self.channels_wanted = max(0, min(MAX_CHANNELS, int(channels or 0)))   # 0 = automatic
        self.channels_open = 0
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

    def groups(self) -> List[dict]:
        """Every group the directory found (shown or not), with display names."""
        with self._lock:
            shown = {x.user_id for x in self.directory.services}
            found = [dict(x.to_dict(), shown=x.user_id in shown) for x in self.directory.entries if getattr(x, "kind", "")]
        return self.group_names.apply(found)

    def org_groups(self) -> List[dict]:
        """The groups people belong to (the directory's group ID), with how many
        people each has and any display name."""
        names = self.org_group_names.all()
        counts: Dict[str, int] = {}
        with self._lock:
            for x in self.directory.entries:
                g = (getattr(x, "group", "") or "").strip()
                if g and not getattr(x, "kind", ""):
                    counts[g] = counts.get(g, 0) + 1
        out = [{"id": g, "name": names.get(g, g), "original_name": g, "renamed": g in names, "members": n}
               for g, n in counts.items()]
        out.sort(key=lambda x: x["name"].lower())
        return out

    def rename_org_group(self, group_id: str, name: str) -> Dict[str, str]:
        names = self.org_group_names.set(group_id, name)
        self._notify_update()
        return names

    def rename_group(self, user_id: str, name: str) -> Dict[str, str]:
        names = self.group_names.set(user_id, name)
        self._notify_update()
        return names

    def on_update(self, fn: UpdateListener) -> None:
        """Called (from the engine's threads) whenever anything may have changed:
        an event applied, a sweep done, the connection state flipped. The push
        stream reads snapshot() and sends the differences."""
        self.update_listeners.append(fn)

    def _notify_update(self) -> None:
        for fn in self.update_listeners:
            try:
                fn()
            except Exception:                  # pragma: no cover
                log.exception("presence update listener failed")

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
        remembered = self.directory.cache.get("ack_path", "")
        if remembered:
            self.client.remember_ack_path(remembered)
        first = self.client.add_channel(expires=self.subs.expires)
        wanted = self.channels_wanted or auto_channels(len(self.targets))
        for _ in range(1, wanted):
            try:
                self.client.add_channel(expires=self.subs.expires)
            except XSIError as e:
                log.info("the platform allowed %d event channel(s) (%s)", len(self.client.channels), e)
                break
        channels = list(self.client.channels)
        self.channels_open = len(channels)
        self.metrics.channels = len(channels)
        self.client.warm()                         # the first ack must not pay for a handshake
        with self._lock:
            self.connected = True
            self.last_heartbeat = time.time()
        log.info("event channel %s open (%d channel%s); subscribing to %d people (%s)", first.id, len(channels),
                 "" if len(channels) == 1 else "s", len(self.targets), self.creds.describe())
        self._notify_update()
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
        for ch in channels:
            for doc in ch.pending:
                self._handle(doc, time.time())
            ch.pending = []
        self._emit_changes()
        self._notify_update()
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
                            self._notify_update()
                    if self.directory.due():
                        try:
                            if self.directory.refresh():
                                self._targets_from_directory()
                                with self._lock:
                                    fresh = [t for t in self.targets.values() if t.monitor == MONITOR_PENDING]
                                self.subs.subscribe_all(fresh, self._stop)
                                self._emit_changes()
                                self._notify_update()
                        except XSIError as e:
                            log.warning("directory refresh failed: %s", e)
                    if self.registration_sweep and now - swept >= REGISTRATION_SWEEP:
                        swept = now
                        self._sweep_registrations(targets)
                    if self.client.ack_path and self.directory.cache.get("ack_path") != self.client.ack_path:
                        self.directory.cache.set("ack_path", self.client.ack_path)
                except Exception as e:
                    failure.append(f"heartbeat failed: {e}")
                    self.client.close_stream()           # makes the read loops notice
                    return

        def reader(ch) -> None:
            try:
                self._read_loop(ch.response, ch.splitter)
            except XSIError as e:
                if not self._stop.is_set():
                    failure.append(str(e))
            except Exception as e:                      # pragma: no cover
                failure.append(f"{type(e).__name__}: {e}")
            finally:
                session_over.set()

        threading.Thread(target=keepalive, name="presence-keepalive", daemon=True).start()
        readers = [threading.Thread(target=reader, args=(ch,), name=f"presence-read-{i}", daemon=True)
                   for i, ch in enumerate(channels)]
        for t in readers:
            t.start()
        try:
            while not session_over.wait(0.5):
                pass
        finally:
            session_over.set()
            self.client.close_stream()                 # one channel down → restart the whole session
            for t in readers:
                t.join(timeout=5)
            with self._lock:
                self.connected = False
                self.channels_open = 0
            self._notify_update()
        if failure:
            raise XSIError(failure[0])
        if not self._stop.is_set():
            raise XSIError("the event channel was closed")

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
            t_recv = time.time()
            docs = splitter.feed(chunk)
            for doc in docs:
                kind = self._handle(doc, t_recv)
                if kind == "terminated":
                    raise XSIError("the event channel was terminated by the server")
            if docs:
                self._emit_changes()
                self._notify_update()

    def _handle(self, doc: bytes, t_recv: float = 0.0) -> str:
        kind = self.processor.handle(doc)
        if kind == "heartbeat":
            with self._lock:
                self.last_heartbeat = time.time()
            return kind
        if kind and kind not in ("duplicate", "stale", "terminated", "") and t_recv:
            platform_ms = None
            if kind in ("CallReceivedEvent", "CallAnsweredEvent", "CallOriginatedEvent"):
                m = _PLATFORM_TIME.search(doc)
                if m:
                    platform_ms = int(m.group(1))
            self.metrics.event(t_recv, time.time(), platform_ms)
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
        self._notify_update()

    # -- acknowledgements -----------------------------------------------------------------
    def _ack_workers_wanted(self) -> int:
        # one in-flight ack per channel (each channel is gated on its own ack),
        # at least two so a slow ack never holds the whole set
        return max(2, self.channels_open or 1)

    def _ack_later(self, event_id: str) -> None:
        with self._lock:
            self._acks.append(event_id)
            self.metrics.queue(len(self._acks))
            # every live worker is busy with an ack (idle ones exit), so a new
            # one is needed whenever there is room for it
            self._ack_threads = [t for t in getattr(self, "_ack_threads", []) if t.is_alive()]
            if len(self._ack_threads) < self._ack_workers_wanted():
                t = threading.Thread(target=self._ack_worker, name=f"presence-ack-{len(self._ack_threads)}", daemon=True)
                self._ack_threads.append(t)
                t.start()

    def _ack_worker(self) -> None:
        while True:
            with self._lock:
                if not self._acks:
                    return
                event_id = self._acks.pop(0)
                depth = len(self._acks)
            try:
                t0 = time.time()
                self.client.ack(event_id)
                self.metrics.ack(time.time() - t0, depth)
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
                return None if d is None else self.org_group_names.apply_people(
                    [dict(d.to_dict(), monitor="none", stale=False, state_label="Unknown")])[0]
            d = person_dict(t, self.is_stale())
            self.org_group_names.apply_people([d])
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
                    "login": self.creds.describe(), "last_error": self.last_error, "requests": self.client.requests,
                    "channels": {"open": self.channels_open, "wanted": self.channels_wanted or auto_channels(len(ts)),
                                 "ids": [c.id for c in self.client.channels]},
                    "latency": self.metrics.summary()}

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
        self.group_names.apply(services)
        self.org_group_names.apply_people(out)
        return {"users": out, "services": services, "ready": ready, "last_poll": last, "last_error": err, "active": active,
                "total": len(out), "source": "events", "login": self.kind, "connected": connected, "stale": stale,
                "list_source": self.list_source, "refused": refused, "ack_error": self.ack_error,
                "directory": self.directory.status(), "states": STATE_LABELS,
                "latency": self.metrics.summary(), "channels": self.channels_open, "t": time.time()}

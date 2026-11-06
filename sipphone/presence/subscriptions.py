"""Subscriptions to the event packages, a few at a time, renewed before they
lapse; refusals are remembered per person and not retried every session."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List

from ..xsi import XSIError
from .connection import EventsClient, EXPIRES
from .models import MONITOR_ERROR, MONITOR_OK, MONITOR_PENDING, MONITOR_REFUSED, Target

log = logging.getLogger("sipphone.presence")
PACKAGES = ("Standard Call", "Do Not Disturb")


class SubscriptionManager:
    def __init__(self, client: EventsClient, concurrency: int = 6, expires: int = EXPIRES,
                 refused_retry: float = 3600.0) -> None:
        self.client = client
        self.concurrency = max(1, concurrency)
        self.expires = expires
        self.refused_retry = refused_retry
        self.refused_at: Dict[str, float] = {}        # user_id -> when the server last refused
        self.renewals = 0
        self.failures = 0

    def subscribe_all(self, targets: Iterable[Target], stop: threading.Event) -> None:
        targets = list(targets)

        def one(t: Target) -> None:
            if stop.is_set():
                return
            last = self.refused_at.get(t.user.user_id, 0.0)
            if last and time.time() - last < self.refused_retry:
                t.monitor, t.error = MONITOR_REFUSED, "not monitored (remembered)"
                return
            self.subscribe_one(t)
        with ThreadPoolExecutor(max_workers=min(self.concurrency, max(1, len(targets)))) as pool:
            list(pool.map(one, targets))

    def subscribe_one(self, t: Target) -> None:
        t.subs = {}
        for pkg in PACKAGES:
            try:
                t.subs[pkg] = self.client.subscribe(t.user.user_id, pkg, self.expires)
            except XSIError as e:
                msg = str(e)
                if msg.startswith("not monitored"):
                    t.monitor, t.error = MONITOR_REFUSED, msg
                    self.refused_at[t.user.user_id] = time.time()
                elif pkg != PACKAGES[0]:             # calls subscribed, DND refused: still monitored
                    log.info("no %s events for %s: %s", pkg, t.user.user_id, msg)
                    break
                else:
                    t.monitor, t.error = MONITOR_ERROR, msg
                    self.failures += 1
                log.warning("subscribe %s for %s: %s", pkg, t.user.user_id, msg)
                if t.monitor != MONITOR_OK:
                    return
            except Exception as e:                   # pragma: no cover
                t.monitor, t.error = MONITOR_ERROR, f"{type(e).__name__}: {e}"
                self.failures += 1
                return
        t.monitor, t.error = MONITOR_OK, ""
        t.sub_expiry = time.time() + self.expires

    def renew_due(self, targets: Iterable[Target], margin: float = 0.4) -> int:
        """Refresh subscriptions that are within `margin` of their lifetime of lapsing."""
        n = 0
        for t in targets:
            if t.monitor != MONITOR_OK or not t.sub_expiry:
                continue
            if time.time() >= t.sub_expiry - self.expires * margin:
                try:
                    for sid in list(t.subs.values()):
                        self.client.refresh_subscription(sid, self.expires)
                    t.sub_expiry = time.time() + self.expires
                    self.renewals += 1
                    n += 1
                except XSIError as e:
                    log.warning("renewing %s: %s — resubscribing", t.user.user_id, e)
                    self.subscribe_one(t)
        return n

    def retry_pending(self, targets: Iterable[Target]) -> int:
        """Try again for people whose refusal has aged out or who errored."""
        n = 0
        for t in targets:
            if t.monitor == MONITOR_ERROR or (t.monitor == MONITOR_REFUSED and
                                              time.time() - self.refused_at.get(t.user.user_id, 0) >= self.refused_retry):
                self.subscribe_one(t)
                n += 1
        return n

    def unsubscribe_all(self, targets: Iterable[Target]) -> None:
        for t in targets:
            for sid in list(t.subs.values()):
                self.client.unsubscribe(sid)
            t.subs = {}

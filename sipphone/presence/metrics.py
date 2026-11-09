"""Latency bookkeeping for the presence engine.

Every event is timed through the pipeline so "presence is slow" can be
attributed instead of argued about:

    platform   Xsp clock (startTime / answerTime in the event)  →  bytes read here
    engine     bytes read  →  state applied
    ack_rtt    acknowledgement request round trip
    render     state applied  →  painted in a browser (reported back by the page)

Numbers are kept in small ring buffers; ``summary()`` gives p50 / p95 / max for
the recent window plus the ack queue depth and the event rate.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

WINDOW = 500            # samples kept per series
RATE_WINDOW = 60.0      # seconds for events-per-minute


def percentile(values, q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[k]


class Series:
    def __init__(self, size: int = WINDOW) -> None:
        self.values: Deque[float] = deque(maxlen=size)
        self.max = 0.0
        self.count = 0

    def add(self, v: float) -> None:
        self.values.append(v)
        self.count += 1
        if v > self.max:
            self.max = v

    def summary(self) -> dict:
        vals = list(self.values)
        return {"p50": round(percentile(vals, 0.5), 3), "p95": round(percentile(vals, 0.95), 3),
                "max": round(self.max, 3), "n": self.count, "recent": len(vals)}


class LatencyMetrics:
    """Thread-safe collector; all times in seconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.platform = Series()
        self.engine = Series()
        self.ack_rtt = Series()
        self.render = Series()
        self.events: Deque[float] = deque(maxlen=5000)      # receive times, for the rate
        self.ack_queue_max = 0
        self.ack_queue_now = 0
        self.channels = 0
        self.started = time.time()

    # -- recording --------------------------------------------------------------
    def event(self, t_recv: float, t_applied: float, platform_ms: Optional[int] = None) -> None:
        with self._lock:
            self.events.append(t_recv)
            self.engine.add(max(0.0, t_applied - t_recv))
            if platform_ms:
                lag = t_recv - platform_ms / 1000.0
                if -5.0 < lag < 60.0:            # ignore clock-skew nonsense
                    self.platform.add(max(0.0, lag))

    def ack(self, rtt: float, queue_now: int) -> None:
        with self._lock:
            self.ack_rtt.add(rtt)
            self.ack_queue_now = queue_now
            if queue_now > self.ack_queue_max:
                self.ack_queue_max = queue_now

    def queue(self, depth: int) -> None:
        with self._lock:
            self.ack_queue_now = depth
            if depth > self.ack_queue_max:
                self.ack_queue_max = depth

    def rendered(self, seconds: float) -> None:
        if 0 <= seconds < 60:
            with self._lock:
                self.render.add(seconds)

    # -- reading ---------------------------------------------------------------
    def events_per_minute(self) -> float:
        now = time.time()
        with self._lock:
            n = sum(1 for t in self.events if now - t <= RATE_WINDOW)
        window = min(RATE_WINDOW, max(1.0, now - self.started))
        return round(n * 60.0 / window, 1)

    def summary(self) -> dict:
        with self._lock:
            out: Dict[str, object] = {"platform": self.platform.summary(), "engine": self.engine.summary(),
                                      "ack_rtt": self.ack_rtt.summary(), "render": self.render.summary(),
                                      "ack_queue": {"now": self.ack_queue_now, "max": self.ack_queue_max},
                                      "channels": self.channels}
        out["events_per_minute"] = self.events_per_minute()
        # the headline: how long after the platform emitted it is a change on screen?
        eng = out["engine"]["p95"]; ren = out["render"]["p95"]; plat = out["platform"]["p95"]
        out["visible_p95"] = round(eng + ren, 3)                       # our part
        out["end_to_end_p95"] = round(plat + eng + ren, 3) if out["platform"]["recent"] else None
        return out

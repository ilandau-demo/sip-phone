"""Availability notifications: "tell me when Alice becomes available".

Rules are per person and per event; the watcher listens to the presence
manager's change stream, which only fires on real transitions while live,
so there is nothing to notify from stale or disconnected state.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("sipphone.presence")

EVENTS = {
    "available": "becomes available",
    "after-call": "becomes available after a call",
    "dnd": "turns on Do Not Disturb",
    "busy": "goes on a call",
}
CALLISH = ("on-call", "on-hold", "ringing", "dialing")


class AvailabilityWatcher:
    """rules: {user_id: {"name": str, "events": [...], "once": bool}}"""

    def __init__(self, path: str = "", notify: Optional[Callable[[dict], None]] = None,
                 min_gap: float = 20.0) -> None:
        self.path = path
        self.notify = notify or (lambda n: None)
        self.min_gap = min_gap
        self.rules: Dict[str, dict] = {}
        self.history: List[dict] = []
        self._last: Dict[str, float] = {}          # (user_id:event) -> last notified
        self._lock = threading.Lock()
        self.load()

    # -- storage ----------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.rules = {k: v for k, v in data.get("rules", {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.rules = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"rules": self.rules}, f, indent=2)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            log.warning("could not save notification rules: %s", e)

    # -- rules ----------------------------------------------------------------------
    def set_rule(self, user_id: str, events: List[str], name: str = "", once: bool = True, sound: bool = True) -> dict:
        events = [e for e in events if e in EVENTS]
        with self._lock:
            if not events:
                self.rules.pop(user_id, None)
            else:
                self.rules[user_id] = {"name": name or user_id, "events": events, "once": bool(once), "sound": bool(sound)}
            self.save()
            return dict(self.rules.get(user_id, {}))

    def remove(self, user_id: str) -> None:
        with self._lock:
            self.rules.pop(user_id, None)
            self.save()

    def list_rules(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.rules.items()}

    # -- the listener -----------------------------------------------------------------
    def on_change(self, person: dict, old: str, new: str) -> None:
        uid = person.get("user_id", "")
        rule = self.rules.get(uid)
        if not rule or person.get("stale"):
            return
        fired = []
        ev = set(rule.get("events", []))
        if "available" in ev and new == "available" and old != "available":
            fired.append("available")
        if "after-call" in ev and new == "available" and old in CALLISH:
            fired.append("after-call")
        if "dnd" in ev and new == "dnd":
            fired.append("dnd")
        if "busy" in ev and new in CALLISH and old not in CALLISH:
            fired.append("busy")
        if not fired:
            return
        now = time.time()
        with self._lock:
            for e in fired:
                key = f"{uid}:{e}"
                if now - self._last.get(key, 0) < self.min_gap:      # no repeats for flapping state
                    continue
                self._last[key] = now
                n = {"user_id": uid, "name": person.get("name") or rule.get("name") or uid, "event": e,
                     "text": f"{person.get('name') or rule.get('name') or uid} {EVENTS[e]}", "status": new,
                     "at": now, "sound": rule.get("sound", True)}
                self.history.append(n)
                del self.history[:-50]
                if rule.get("once", True):
                    ev.discard(e)
                    if ev:
                        rule["events"] = sorted(ev)
                    else:
                        self.rules.pop(uid, None)
                    self.save()
                try:
                    self.notify(n)
                except Exception:                  # pragma: no cover
                    log.exception("notification sink failed")

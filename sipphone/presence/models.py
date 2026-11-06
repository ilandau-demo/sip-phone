"""Presence state model shared by the desktop app and (as a port) the extension.

States (section 6.3): available, ringing, dialing, on-call, on-hold, dnd,
offline, unknown. Call state and Do Not Disturb are tracked separately; a
user we may not monitor stays "unknown" — never "available".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..xsi import MonitoredUser, SERVICE_LABELS   # noqa: F401  (re-exported)

STATES = ("available", "ringing", "dialing", "on-call", "on-hold", "dnd", "offline", "unknown")
STATE_LABELS = {"available": "Available", "ringing": "Ringing", "dialing": "Calling out", "on-call": "On a call",
                "on-hold": "On hold", "dnd": "Do Not Disturb", "offline": "Offline", "unknown": "Unknown"}
BUSY = ("ringing", "dialing", "on-call", "on-hold")
# sort order in a plain list: who needs attention first
ORDER = {"ringing": 0, "on-call": 1, "on-hold": 2, "dialing": 3, "dnd": 4, "available": 5, "offline": 6, "unknown": 7}
MONITOR_OK, MONITOR_REFUSED, MONITOR_ERROR, MONITOR_PENDING = "ok", "not-monitored", "error", "pending"


@dataclass
class Call:
    call_id: str
    state: str = ""
    personality: str = ""
    name: str = ""
    number: str = ""
    start: float = 0.0


@dataclass
class Target:
    """One monitored person: directory record + live state + subscription health."""
    user: MonitoredUser
    calls: Dict[str, Call] = field(default_factory=dict)
    hook: str = ""
    dnd: bool = False
    registered: Optional[bool] = None          # None = not known (no registration sweep)
    subs: Dict[str, str] = field(default_factory=dict)      # package -> subscriptionId
    sub_expiry: float = 0.0                                 # epoch when the subscriptions lapse
    monitor: str = MONITOR_PENDING                          # ok | not-monitored | error | pending
    error: str = ""
    synced: bool = False                                    # an initial state arrived
    last_update: float = 0.0                                # epoch of the last event for this person
    last_seq: int = 0                                       # highest sequenceNumber seen

    @property
    def status(self) -> str:
        return status_of(self)


def clean_number(addr: str) -> str:
    if not addr:
        return ""
    return re.sub(r"^(tel|sip):", "", addr).split(";")[0].split("@")[0].replace("+1", "", 1)


def status_of(t: Target) -> str:
    """Derive the state from calls, hook, DND and monitoring. Fills the user's
    with_* fields as a side effect so the snapshot carries them."""
    calls = list(t.calls.values())
    low = lambda c: c.state.lower()                    # noqa: E731
    active = [c for c in calls if low(c) in ("active", "answered")]
    held = [c for c in calls if low(c) in ("held", "remoteheld", "remote held")]
    alerting = [c for c in calls if low(c) in ("alerting", "ringing")]
    u = t.user
    if active or held or alerting:
        if active:
            chosen, st = active[0], "on-call"
        elif held:
            chosen, st = held[0], "on-hold"
        else:
            chosen = alerting[0]
            st = "dialing" if "originat" in chosen.personality.lower() else "ringing"
        u.with_name, u.with_number = chosen.name, chosen.number
        u.direction = "outbound" if "originat" in chosen.personality.lower() else "inbound"
        u.since = chosen.start if st in ("on-call", "on-hold") else 0.0
        return st
    u.with_name = u.with_number = u.direction = ""
    u.since = 0.0
    if t.hook.lower() == "off-hook":
        return "on-call"
    if t.dnd:
        return "dnd"
    if t.monitor in (MONITOR_REFUSED, MONITOR_ERROR, MONITOR_PENDING) or not t.synced:
        return "unknown"
    if t.registered is False:
        return "offline"
    return "available"


def person_dict(t: Target, stale: bool, now: Optional[float] = None) -> dict:
    """The wire form of one person (what /api/monitor and the UI see)."""
    st = status_of(t)
    u = t.user
    u.status = st
    u.dnd = t.dnd
    u.detail = ("Not monitored — your login may not watch this person" if t.monitor == MONITOR_REFUSED
                else t.error if t.monitor == MONITOR_ERROR else "")
    d = u.to_dict()
    d.update({"monitor": t.monitor, "stale": bool(stale and t.monitor == MONITOR_OK),
              "last_update": t.last_update, "state_label": STATE_LABELS.get(st, st),
              "sub_expiry": t.sub_expiry, "registered": t.registered})
    return d


def sort_people(people: List[dict]) -> List[dict]:
    people.sort(key=lambda u: (ORDER.get(u["status"], 9), u["name"].lower()))
    return people


def now() -> float:
    return time.time()

"""Directory entries that aren't people: hunt groups, auto attendants, paging...

The Xsi directory lists every "user" the switch knows, including service
instances. Each is classified into a category; the Team view shows only people,
and the categories the user has ticked are shown as speed dials (no presence).
"""
import re
from typing import Dict, Iterable, List, Tuple

from .xsi import MonitoredUser

# (key, label, keywords matched against the display name, word-bounded, case-insensitive)
CATEGORIES: List[Tuple[str, str, List[str]]] = [
    ("hunt", "Hunt groups", ["hunt group", "hunt-group", "hunt"]),
    ("callcenter", "Call centers / queues", ["call center", "call centre", "call queue", "queue"]),
    ("attendant", "Auto attendants", ["auto attendant", "auto-attendant", "attendant", "ivr"]),
    ("paging", "Group paging", ["group paging", "paging", "page group"]),
    ("park", "Call parks", ["call park", "park"]),
    ("meetme", "Conference bridges", ["meet-me", "meet me", "conference bridge", "collaborate"]),
    ("voicemail", "Voice portal / voicemail", ["voice portal", "voice messaging", "voicemail"]),
    ("other", "Other services", ["flexible seating", "broadworks anywhere", "find me", "find-me",
                                 "route point", "instant group", "trunk group", "anywhere"]),
]
KEYS = [k for k, _, _ in CATEGORIES]
LABELS = {k: lbl for k, lbl, _ in CATEGORIES}

_pats = [(k, re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I))
         for k, _, words in CATEGORIES]


def classify(name: str, user_id: str = "", extra_keywords: Iterable[str] = ()) -> str:
    """Category key for a directory entry, or '' when it is a person."""
    if "^" in (user_id or ""):                     # e.g. SR^TeamDefault_… service instances
        return "other"
    for k, pat in _pats:
        if pat.search(name or ""):
            return k
    for w in extra_keywords:
        w = (w or "").strip()
        if w and re.search(r"\b" + re.escape(w) + r"\b", name or "", re.I):
            return "other"
    return ""


def parse_show(value) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").replace(";", ",").split(",")
    out = []
    for s in items:
        s = str(s).strip().lower()
        if s in KEYS and s not in out:
            out.append(s)
    return out


def split(users: Iterable[MonitoredUser], show: Iterable[str] = (), people_only: bool = True,
          extra_keywords: Iterable[str] = ()) -> Tuple[List[MonitoredUser], List[dict]]:
    """(people, speed_dials). Services in `show` become speed dials; the rest are dropped.

    With people_only False, nothing is filtered and everything stays a 'person'
    (the old users_only=false behaviour)."""
    show = set(parse_show(show))
    people, dials = [], []
    for u in users:
        cat = classify(u.name, u.user_id, extra_keywords)
        if not cat or not people_only:
            people.append(u)
        elif cat in show:
            dials.append({"user_id": u.user_id, "name": u.name or u.user_id,
                          "number": u.number or u.extension, "extension": u.extension,
                          "category": cat, "category_label": LABELS[cat]})
    dials.sort(key=lambda d: (KEYS.index(d["category"]), d["name"].lower()))
    return people, dials


def counts(users: Iterable[MonitoredUser], extra_keywords: Iterable[str] = ()) -> Dict[str, int]:
    """How many directory entries fall in each category ('' = people)."""
    out: Dict[str, int] = {}
    for u in users:
        c = classify(u.name, u.user_id, extra_keywords)
        out[c] = out.get(c, 0) + 1
    return out

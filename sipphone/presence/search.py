"""Search-as-you-type and presence filters over a snapshot (pure functions;
the UI does the same locally, this serves the API and the voice assistant)."""
from __future__ import annotations

from typing import Iterable, List, Optional

FIELDS = ("name", "first", "last", "extension", "number", "department", "title", "email", "location", "group", "user_id")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def matches(person: dict, query: str) -> bool:
    q = _norm(query)
    if not q:
        return True
    terms = q.split()
    hay = " ".join(_norm(str(person.get(f, ""))) for f in FIELDS)
    return all(t in hay for t in terms)


def search_people(people: Iterable[dict], query: str, limit: int = 0) -> List[dict]:
    out = [p for p in people if matches(p, query)]
    if query.strip():
        q = _norm(query)
        # exact extension / name-prefix hits first
        out.sort(key=lambda p: (0 if _norm(p.get("extension")) == q else
                                1 if _norm(p.get("name")).startswith(q) or _norm(p.get("last")).startswith(q) else 2,
                                _norm(p.get("name"))))
    return out[:limit] if limit else out


def filter_people(people: Iterable[dict], statuses: Optional[Iterable[str]] = None, department: str = "",
                  location: str = "", on_call_only: bool = False, favorites: Optional[Iterable[str]] = None,
                  monitored_only: bool = False) -> List[dict]:
    st = set(s.lower() for s in (statuses or []) if s)
    fav = set(favorites or [])
    dep, loc = _norm(department), _norm(location)
    out = []
    for p in people:
        if st and p.get("status") not in st:
            continue
        if dep and _norm(p.get("department")) != dep:
            continue
        if loc and _norm(p.get("location")) != loc:
            continue
        if on_call_only and p.get("status") not in ("on-call", "on-hold", "ringing", "dialing"):
            continue
        if fav and p.get("user_id") not in fav:
            continue
        if monitored_only and p.get("monitor") != "ok":
            continue
        out.append(p)
    return out

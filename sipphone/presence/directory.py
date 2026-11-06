"""The enterprise directory: people to monitor, services to list, and the
search index. Read through XSI with whichever login we have."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Set

from ..xsi import MonitoredUser, XSIClient, XSIError, hidden_match, service_type
from .cache import DirectoryCache

log = logging.getLogger("sipphone.presence")


class Directory:
    """Holds the current directory split into people and services, refreshes it
    in the background, and keeps a lowercase index for search-as-you-type."""

    def __init__(self, client: XSIClient, seed: str, directory_type: str = "Enterprise",
                 cache_path: str = "", hide: Optional[List[str]] = None, user_domain: str = "",
                 show_services: Optional[List[str]] = None, refresh_seconds: float = 900.0) -> None:
        self.client = client
        self.seed = seed
        self.directory_type = directory_type
        self.cache = DirectoryCache(cache_path)
        self.hide = set(h.strip().lower().replace("@" + (user_domain or ""), "") for h in (hide or []) if h.strip())
        self.user_domain = user_domain or ""
        self.show_services: Set[str] = set(k.strip().lower() for k in (show_services or []) if k.strip())
        self.refresh_seconds = refresh_seconds
        self.entries: List[MonitoredUser] = []
        self.people: List[MonitoredUser] = []
        self.services: List[MonitoredUser] = []
        self.loaded_at = 0.0
        self.synced_at = 0.0                   # last successful read from the server
        self.from_cache = False
        self.error = ""
        self._lock = threading.Lock()
        cached = self.cache.load()
        if cached:
            self._set(cached, from_cache=True)

    # -- loading ---------------------------------------------------------------
    def _set(self, entries: List[MonitoredUser], from_cache: bool) -> None:
        people, services = [], []
        me = self.client._uid(self.client.username).lower()
        for d in entries:
            d.kind = d.kind or service_type(d.name, d.user_id)
            if d.kind:
                if d.kind in self.show_services and not hidden_match(d, self.hide, self.user_domain):
                    services.append(d)
            elif self.client._uid(d.user_id).lower() != me and not hidden_match(d, self.hide, self.user_domain):
                people.append(d)
        services.sort(key=lambda x: (x.kind, (x.name or x.user_id).lower()))
        people.sort(key=lambda x: (x.name or x.user_id).lower())
        with self._lock:
            self.entries, self.people, self.services = entries, people, services
            self.loaded_at = time.time()
            self.from_cache = from_cache

    def refresh(self) -> bool:
        """Read the directory from the server. Returns True if it changed."""
        try:
            entries = self.client.list_directory_users(self.seed, self.directory_type)
        except XSIError as e:
            self.error = str(e)
            raise
        self.error = ""
        self.synced_at = time.time()
        changed = self._differs(entries)
        self._set(entries, from_cache=False)
        if changed:
            self.cache.save(entries)
        return changed

    def _differs(self, entries: List[MonitoredUser]) -> bool:
        key = lambda u: (u.user_id, u.name, u.number, u.extension, u.department, u.title, u.email, u.mobile, u.location)  # noqa: E731
        return sorted(map(key, entries)) != sorted(map(key, self.entries))

    def due(self) -> bool:
        return time.time() - self.synced_at >= self.refresh_seconds

    # -- lookups ---------------------------------------------------------------
    def find(self, user_id: str) -> Optional[MonitoredUser]:
        uid = user_id.lower()
        short = uid.split("@")[0]
        with self._lock:
            for u in self.entries:
                if u.user_id.lower() == uid or u.user_id.lower().split("@")[0] == short:
                    return u
        return None

    def status(self) -> dict:
        with self._lock:
            return {"people": len(self.people), "services": len(self.services), "entries": len(self.entries),
                    "loaded_at": self.loaded_at, "synced_at": self.synced_at, "from_cache": self.from_cache,
                    "error": self.error, "seed": self.seed, "type": self.directory_type}

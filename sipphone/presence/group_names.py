"""Display names for groups (hunt groups, call centers, auto attendants, ...).

The directory often lists services by their ID ("hg_sales@nextiva.com") or a
terse system name. People choose a friendlier name in Settings; it is kept in
``group-names.json`` beside the directory cache, keyed by the service's user
ID, and applied everywhere the group appears (Team tab, boards, speed dials,
transfer list, search).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict

FILE_NAME = "group-names.json"
MAX_LEN = 80


class GroupNames:
    def __init__(self, path: str = "") -> None:
        self.path = path
        self._names: Dict[str, str] = {}
        self._mtime = -1.0
        self._lock = threading.Lock()

    def _reload(self) -> None:
        if not self.path:
            return
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            if self._mtime != 0:
                self._names, self._mtime = {}, 0
            return
        if m == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._names = {str(k): str(v) for k, v in (data.get("names") or {}).items() if str(v).strip()}
        except (OSError, ValueError, AttributeError):
            self._names = {}
        self._mtime = m

    def all(self) -> Dict[str, str]:
        with self._lock:
            self._reload()
            return dict(self._names)

    def get(self, user_id: str) -> str:
        return self.all().get(user_id, "")

    def set(self, user_id: str, name: str) -> Dict[str, str]:
        """Set a display name; an empty name goes back to the directory's."""
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id required")
        name = " ".join((name or "").split())[:MAX_LEN]
        with self._lock:
            self._reload()
            if name:
                self._names[user_id] = name
            else:
                self._names.pop(user_id, None)
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"names": self._names}, f, indent=1, ensure_ascii=False)
                os.replace(tmp, self.path)
                self._mtime = os.path.getmtime(self.path)
            return dict(self._names)

    def apply(self, services: list) -> list:
        """Service dicts with display names applied (original kept as
        ``original_name``), re-sorted by kind then shown name."""
        names = self.all()
        for d in services:
            d["original_name"] = d.get("name", "")
            if d.get("user_id") in names:
                d["name"] = names[d["user_id"]]
                d["renamed"] = True
        services.sort(key=lambda x: (x.get("kind", ""), (x.get("name") or "").lower()))
        return services

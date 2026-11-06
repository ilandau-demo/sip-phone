"""Directory cache on disk: the Team view comes up instantly from the last
known directory while the live one loads, and searching never waits on the
server."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

from ..xsi import MonitoredUser

log = logging.getLogger("sipphone.presence")
FIELDS = ("user_id", "name", "first", "last", "number", "extension", "department", "title", "email",
          "mobile", "location", "group", "kind")


class DirectoryCache:
    def __init__(self, path: str = "") -> None:
        self.path = path
        self.saved_at = 0.0

    def load(self) -> List[MonitoredUser]:
        if not self.path or not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.saved_at = float(data.get("saved_at", 0))
            return [MonitoredUser(**{k: d.get(k, "") for k in FIELDS}) for d in data.get("entries", [])
                    if isinstance(d, dict) and d.get("user_id")]
        except (OSError, ValueError, TypeError) as e:
            log.warning("directory cache unreadable (%s); ignoring", e)
            return []

    def save(self, entries: List[MonitoredUser]) -> None:
        if not self.path:
            return
        data = {"saved_at": time.time(), "entries": [{k: getattr(u, k, "") for k in FIELDS} for u in entries]}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
            self.saved_at = data["saved_at"]
        except OSError as e:
            log.warning("could not save the directory cache: %s", e)

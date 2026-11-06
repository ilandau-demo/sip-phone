"""Admin monitoring over the BroadWorks / Nextiva Xsi-Actions REST API.

With group-admin or supervisor credentials this polls the Xtended Services
Interface to show which users in the group are on calls and who they are
speaking with.

Endpoints used (base: https://<xsp-host>/com.broadsoft.xsi-actions/v2.0):
  GET /user/<userId>/directories/Group        -> enumerate users in the group
  GET /user/<userId>/calls                     -> a user's active calls
  GET /user/<userId>/calls/<callId>            -> call state, personality, remote party

Auth is HTTP Basic over HTTPS (RFC: Xsi §3.2.2). Responses are XML in the
http://schema.broadsoft.com/xsi namespace; we parse namespace-agnostically so
minor schema differences between BroadWorks releases don't break parsing.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Tuple, Dict, List, Optional

log = logging.getLogger("sipphone.xsi")

XSI_ACTIONS = "com.broadsoft.xsi-actions/v2.0"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# BroadWorks/Nextiva directory entries that are service instances rather than
# people. Call Parks are intentionally NOT here — they are treated as users.
# Matched against the display name; userIds containing '^' (e.g. SR^TeamDefault_)
# are always service instances.
DEFAULT_SERVICE_KEYWORDS = [
    "hunt group", "group paging", "call center", "auto attendant", "auto-attendant",
    "voice portal", "voice messaging", "voicemail", "meet-me", "meet me",
    "flexible seating", "broadworks anywhere", "find me", "find-me", "route point",
    "instant group", "trunk group", "collaborate",
]


def build_service_matcher(keywords):
    """Return is_service(name, user_id) for the given exclusion keywords."""
    kws = [re.escape(k.strip()) for k in (keywords or []) if k.strip()]
    pat = re.compile(r"\b(" + "|".join(kws) + r")\b", re.I) if kws else None

    def is_service(name: str, user_id: str) -> bool:
        if "^" in (user_id or ""):
            return True
        return bool(pat and pat.search(name or ""))
    return is_service


_default_matcher = build_service_matcher(DEFAULT_SERVICE_KEYWORDS)

# Service categories the Team view can show as speed dials (no presence).
# Order matters: the first category whose keyword appears in the name wins.
SERVICE_TYPES = [
    ("hunt_group", "Hunt groups", ["hunt group"]),
    ("call_center", "Call centers / queues", ["call center", "call centre", "queue"]),
    ("auto_attendant", "Auto attendants", ["auto attendant", "auto-attendant"]),
    ("paging", "Paging groups", ["group paging", "paging"]),
    ("voice_portal", "Voice portal / voicemail", ["voice portal", "voice messaging", "voicemail"]),
    ("conference", "Conference bridges", ["meet-me", "meet me", "collaborate", "conference"]),
    ("other", "Other services", []),
]
SERVICE_LABELS = {k: lbl for k, lbl, _ in SERVICE_TYPES}
_type_pats = [(k, re.compile(r"\b(" + "|".join(re.escape(w) for w in kws) + r")\b", re.I))
              for k, _, kws in SERVICE_TYPES if kws]


def service_type(name: str, user_id: str, is_service=None) -> str:
    """Category key for a service directory entry, or '' for a person."""
    for k, pat in _type_pats:
        if pat.search(name or ""):
            return k
    return "other" if (is_service or _default_matcher)(name or "", user_id or "") else ""


def is_service_instance(name: str, user_id: str) -> bool:
    """True if a directory entry is a service instance (default keyword set)."""
    return _default_matcher(name, user_id)


def _find(elem: ET.Element, name: str) -> Optional[ET.Element]:
    for e in elem.iter():
        if _localname(e.tag) == name:
            return e
    return None


def _text(elem: Optional[ET.Element], name: str, default: str = "") -> str:
    if elem is None:
        return default
    found = None
    for e in elem.iter():
        if _localname(e.tag) == name:
            found = e
            break
    return (found.text or default).strip() if found is not None else default


def _children(elem: ET.Element, name: str) -> List[ET.Element]:
    return [e for e in elem.iter() if _localname(e.tag) == name]


@dataclass
class MonitoredUser:
    user_id: str
    name: str = ""
    number: str = ""
    extension: str = ""
    department: str = ""
    first: str = ""
    last: str = ""
    title: str = ""
    email: str = ""
    mobile: str = ""
    location: str = ""
    group: str = ""
    # live status, filled by the presence engine
    status: str = "unknown"            # available | ringing | dialing | on-call | on-hold | dnd | offline | unknown
    with_name: str = ""                # who they're speaking with
    with_number: str = ""
    direction: str = ""                # inbound | outbound
    since: float = 0.0                 # epoch of call start, if known
    dnd: bool = False                  # Do Not Disturb active
    detail: str = ""                   # free-text note (e.g. error reason)
    kind: str = ""                     # service category (hunt_group, ...) or '' for a person

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "name": self.name or self.user_id,
                "number": self.number or self.extension, "extension": self.extension,
                "department": self.department, "status": self.status,
                "with_name": self.with_name, "with_number": self.with_number,
                "direction": self.direction, "since": self.since, "dnd": self.dnd,
                "detail": self.detail, "kind": self.kind, "kind_label": SERVICE_LABELS.get(self.kind, ""),
                "first": self.first, "last": self.last, "title": self.title, "email": self.email,
                "mobile": self.mobile, "location": self.location, "group": self.group}


class XSIError(Exception):
    pass


class XSIClient:
    def __init__(self, host: str, username: str, password: str, timeout: float = 8.0,
                 verify_tls: bool = True, user_domain: str = "nextiva.com") -> None:
        if not host:
            raise XSIError("no Xsi host configured")
        if "://" not in host:
            host = "https://" + host
        self.base = host.rstrip("/") + "/" + XSI_ACTIONS
        self.username = username
        self.password = password
        self.timeout = timeout
        self.user_domain = (user_domain or "").strip().lstrip("@")
        self.remote_party_ok = True   # cleared if /calls isn't usable on this platform
        self.dnd_ok = True            # cleared if the DoNotDisturb service isn't readable
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._ctx = None
        if host.startswith("https") and not verify_tls:
            import ssl
            self._ctx = ssl._create_unverified_context()

    def _uid(self, user_id: str) -> str:
        """Directory userIds come back bare (jsmith1234567); the call APIs
        need the full jsmith1234567@nextiva.com form."""
        if "@" in user_id or not self.user_domain:
            return user_id
        return f"{user_id}@{self.user_domain}"

    def _get(self, path: str) -> ET.Element:
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, headers={"Authorization": self._auth,
                                                   "Accept": "application/xml"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise XSIError("authentication failed — check the Xsi user ID and password")
            if e.code == 403:
                raise XSIError("forbidden — this account may lack privileges to monitor other users")
            raise XSIError(f"HTTP {e.code} for {path}")
        except urllib.error.URLError as e:
            raise XSIError(f"cannot reach Xsi host: {e.reason}")
        try:
            return ET.fromstring(data)
        except ET.ParseError as e:
            raise XSIError(f"bad XML from {path}: {e}")

    # -- directory ------------------------------------------------------------
    def list_directory_users(self, seed_user: str = "", directory: str = "Enterprise") -> List[MonitoredUser]:
        """Enumerate a directory (Enterprise by default, or Group).

        `seed_user` is the userId whose directory to read; it defaults to the
        login name. On Nextiva the admin login has no directory of its own, so
        pass a real user (e.g. jdoe1234567) as the seed and the whole
        enterprise/group comes back. Entries without a userId are skipped.
        """
        who = seed_user or self.username
        dirname = {"enterprise": "Enterprise", "group": "Group"}.get(
            (directory or "Enterprise").lower(), directory)
        root = self._get(f"/user/{urllib.parse.quote(who)}/directories/{dirname}")
        users: List[MonitoredUser] = []
        for d in _children(root, "directoryDetails"):
            uid = _text(d, "userId")
            if not uid:
                continue
            first, last = _text(d, "firstName"), _text(d, "lastName")
            users.append(MonitoredUser(
                user_id=uid, name=(first + " " + last).strip() or uid, first=first, last=last,
                number=_text(d, "number"), extension=_text(d, "extension"),
                department=_text(d, "department"), title=_text(d, "title"),
                email=_text(d, "emailAddress") or _text(d, "email"), mobile=_text(d, "mobile"),
                location=_text(d, "location"), group=_text(d, "groupId")))
        return users

    def list_group_users(self, seed_user: str = "") -> List[MonitoredUser]:
        """Backward-compatible alias for the Group directory."""
        return self.list_directory_users(seed_user, "Group")

    def registration_count(self, user_id: str) -> int:
        """How many devices the user is registered on (0 = offline)."""
        root = self._get(f"/user/{urllib.parse.quote(self._uid(user_id))}/profile/Registrations")
        return len(_children(root, "registration"))

    def is_monitorable_user(self, user_id: str) -> bool:
        """True if this id is a real user (its call-status endpoint exists).

        Service instances — auto attendants, hunt groups, call centers, voice
        portals — appear in the enterprise directory but have no user call
        resource, so /calls returns 404 for them. Transient/auth errors are
        treated as "keep" so a blip doesn't drop a real person.
        """
        try:
            self.user_calls(user_id)
            return True
        except XSIError as e:
            return "404" not in str(e)

    # -- call status ----------------------------------------------------------
    def hook_status(self, user_id: str) -> str:
        """On-hook (idle) / off-hook (line in use), per Xsi §3.5.4.37.

        Returns "on", "off", or "" (unknown). This is the reliable presence
        signal on Nextiva, where /calls can 500 for directory users.
        """
        root = self._get(f"/user/{urllib.parse.quote(self._uid(user_id))}/calls/HookStatus")
        val = _text(root, "hookStatus").lower()
        if "off" in val:
            return "off"
        if "on" in val:
            return "on"
        return ""

    def dnd_active(self, user_id: str) -> bool:
        """Whether Do Not Disturb is on for the user (Xsi §3.5.3.40)."""
        root = self._get(f"/user/{urllib.parse.quote(self._uid(user_id))}/services/DoNotDisturb")
        val = (_text(root, "active") or _text(root, "isActive")).lower()
        return val == "true"

    def user_calls(self, user_id: str) -> List[str]:
        """Return the callIds of a user's active calls (empty if idle)."""
        root = self._get(f"/user/{urllib.parse.quote(self._uid(user_id))}/calls")
        ids = []
        for c in _children(root, "call"):
            cid = _text(c, "callId")
            if cid:
                ids.append(cid)
        return ids

    def call_detail(self, user_id: str, call_id: str) -> dict:
        root = self._get(f"/user/{urllib.parse.quote(self._uid(user_id))}/calls/{urllib.parse.quote(call_id)}")
        remote = _find(root, "remoteParty")
        state = _text(root, "state")
        personality = _text(root, "personality")  # Originator | Terminator | Click-to-Dial
        start = _text(root, "startTime") or _text(root, "answerTime")
        try:
            since = int(start) / 1000.0 if start else 0.0
        except ValueError:
            since = 0.0
        return {"state": state, "personality": personality,
                "with_name": _text(remote, "name"),
                "with_number": _text(remote, "address") or _text(remote, "userDN") or _text(remote, "userId"),
                "since": since}

    @staticmethod
    def _classify(state: str, personality: str) -> str:
        s, p = state.lower(), personality.lower()
        if s in ("active", "answered"):
            return "on-call"
        if s in ("held", "remoteheld"):
            return "on-hold"
        if s in ("alerting", "ringing"):
            return "ringing" if "terminat" in p else "dialing"
        if s in ("detached", "releasing"):
            return "on-call"
        return "on-call" if s else "idle"

    def _status_from_calls(self, user: MonitoredUser, details: List[dict]) -> str:
        """Derive a status from a user's active-call details, filling who/when."""
        if not details:
            return "idle"
        low = lambda d: d["state"].lower()
        active = [d for d in details if low(d) in ("active", "answered")]
        held = [d for d in details if low(d) in ("held", "remoteheld")]
        alerting = [d for d in details if low(d) in ("alerting", "ringing")]
        if active:
            chosen, status = active[0], "on-call"
        elif held:
            chosen, status = held[0], "on-hold"
        elif alerting:
            chosen = alerting[0]
            status = "dialing" if "originat" in chosen["personality"].lower() else "ringing"
        else:
            chosen, status = details[0], "on-call"
        user.with_name = chosen["with_name"]
        user.with_number = chosen["with_number"]
        user.direction = ("outbound" if "originat" in chosen["personality"].lower()
                          or "dial" in chosen["personality"].lower() else "inbound")
        user.since = chosen["since"]
        return status

    def status_for(self, user: MonitoredUser) -> MonitoredUser:
        """Fill a user's live status in place. Never raises; records errors.

        Priority: an active/held call, then a ringing (alerting) call, then Do
        Not Disturb, then idle. Ringing needs the /calls list; where that isn't
        usable (HTTP 500 on Nextiva) it falls back to HookStatus, which only
        distinguishes on-a-call from idle.
        """
        user.with_name = user.with_number = user.direction = ""
        user.since = 0.0
        user.dnd = False

        if self.dnd_ok:
            try:
                user.dnd = self.dnd_active(user.user_id)
            except XSIError:
                self.dnd_ok = False

        status = None
        if self.remote_party_ok:
            try:
                details = [self.call_detail(user.user_id, cid)
                           for cid in self.user_calls(user.user_id)]
                status = self._status_from_calls(user, details)
            except XSIError as e:
                self.remote_party_ok = False
                log.info("call list unavailable (%s); using HookStatus only", e)

        if status is None:  # /calls not usable — fall back to HookStatus
            try:
                hook = self.hook_status(user.user_id)
            except XSIError as e:
                user.status, user.detail = "error", str(e)
                return user
            status = "on-call" if hook == "off" else "idle"

        if status == "idle" and user.dnd:
            status = "dnd"
        user.status, user.detail = status, ""
        return user


def hidden_match(u: MonitoredUser, hide, user_domain: str = "") -> bool:
    """True if any hide_user token matches this user's id, extension or number."""
    if not hide:
        return False
    dom = "@" + (user_domain or "")
    uid = u.user_id.lower()
    keys = {uid, uid.replace(dom, ""), (u.extension or "").lower(), (u.number or "").lower()}
    num_digits = "".join(ch for ch in (u.number or "") if ch.isdigit())
    ext_digits = "".join(ch for ch in (u.extension or "") if ch.isdigit())
    for h in hide:
        if h in keys:
            return True
        if h and h in uid:                       # token appears in the userId
            return True
        hd = "".join(ch for ch in h if ch.isdigit())
        if hd and hd == ext_digits:              # matches the extension
            return True
        if hd and num_digits and num_digits.endswith(hd) and len(hd) >= 3:
            return True                          # matches the end of the DID (e.g. last 4)
    return False

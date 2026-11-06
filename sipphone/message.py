"""SIP 2.0 message model: parsing and serialisation per RFC 3261 §7.

Only the parts a UA needs. Header names are stored case-insensitively and
compact forms (v, f, t, i, m, c, l, s, k) are expanded on parse.
"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SIP_VERSION = "SIP/2.0"

COMPACT = {
    "v": "Via", "f": "From", "t": "To", "i": "Call-ID", "m": "Contact",
    "c": "Content-Type", "l": "Content-Length", "s": "Subject", "k": "Supported",
    "e": "Content-Encoding",
}

# canonical capitalisation for the wire
_CANON = {
    "via": "Via", "from": "From", "to": "To", "call-id": "Call-ID", "cseq": "CSeq",
    "contact": "Contact", "max-forwards": "Max-Forwards", "content-type": "Content-Type",
    "content-length": "Content-Length", "user-agent": "User-Agent", "expires": "Expires",
    "allow": "Allow", "supported": "Supported", "www-authenticate": "WWW-Authenticate",
    "proxy-authenticate": "Proxy-Authenticate", "authorization": "Authorization",
    "proxy-authorization": "Proxy-Authorization", "route": "Route",
    "record-route": "Record-Route", "server": "Server", "subject": "Subject",
    "reason": "Reason", "p-asserted-identity": "P-Asserted-Identity",
    "session-expires": "Session-Expires", "min-se": "Min-SE", "require": "Require",
    "rseq": "RSeq", "rack": "RAck", "event": "Event", "subscription-state": "Subscription-State",
    "privacy": "Privacy", "accept": "Accept", "date": "Date", "warning": "Warning",
    "retry-after": "Retry-After", "min-expires": "Min-Expires", "x-nextiva": "X-Nextiva",
}

REASON_PHRASES = {
    100: "Trying", 180: "Ringing", 181: "Call Is Being Forwarded", 182: "Queued",
    183: "Session Progress", 200: "OK", 202: "Accepted", 300: "Multiple Choices",
    301: "Moved Permanently", 302: "Moved Temporarily", 400: "Bad Request",
    401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    406: "Not Acceptable", 407: "Proxy Authentication Required", 408: "Request Timeout",
    415: "Unsupported Media Type", 420: "Bad Extension", 422: "Session Interval Too Small",
    480: "Temporarily Unavailable", 481: "Call/Transaction Does Not Exist",
    482: "Loop Detected", 483: "Too Many Hops", 486: "Busy Here", 487: "Request Terminated",
    488: "Not Acceptable Here", 500: "Server Internal Error", 501: "Not Implemented",
    503: "Service Unavailable", 603: "Decline",
}

_ALNUM = string.ascii_letters + string.digits


def rand_token(n: int = 16) -> str:
    return "".join(random.choice(_ALNUM) for _ in range(n))


def canonical(name: str) -> str:
    low = name.lower()
    if low in COMPACT:
        return COMPACT[low]
    if low in _CANON:
        return _CANON[low]
    return "-".join(p.capitalize() for p in name.split("-"))


class SIPError(Exception):
    pass


@dataclass
class Address:
    """A name-addr / addr-spec with header parameters: `"Bob" <sip:bob@x>;tag=1`."""
    uri: str
    display: str = ""
    params: Dict[str, Optional[str]] = field(default_factory=dict)

    _RE = re.compile(r'^\s*(?:"([^"]*)"\s*|([^<"]*?)\s*)?<([^>]+)>\s*(.*)$')

    @classmethod
    def parse(cls, value: str) -> "Address":
        m = cls._RE.match(value)
        if m:
            display = m.group(1) if m.group(1) is not None else (m.group(2) or "").strip()
            uri = m.group(3).strip()
            rest = m.group(4)
        else:
            # bare addr-spec; header params follow the first ';'
            if ";" in value:
                uri, rest = value.split(";", 1)
                rest = ";" + rest
            else:
                uri, rest = value, ""
            uri, display = uri.strip(), ""
        params: Dict[str, Optional[str]] = {}
        for p in rest.split(";"):
            p = p.strip()
            if not p:
                continue
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.strip().lower()] = v.strip()
            else:
                params[p.lower()] = None
        return cls(uri=uri, display=display, params=params)

    @property
    def tag(self) -> Optional[str]:
        return self.params.get("tag")

    @property
    def user(self) -> str:
        m = re.match(r"^(?:sips?:)?([^@;]+)@", self.uri)
        return m.group(1) if m else ""

    @property
    def host(self) -> str:
        m = re.match(r"^(?:sips?:)?(?:[^@]+@)?([^;:>]+)", self.uri)
        return m.group(1) if m else ""

    def __str__(self) -> str:
        s = f'"{self.display}" <{self.uri}>' if self.display else f"<{self.uri}>"
        for k, v in self.params.items():
            s += f";{k}" if v is None else f";{k}={v}"
        return s


class Headers:
    """Ordered, case-insensitive multi-map."""

    def __init__(self) -> None:
        self._items: List[List[str]] = []  # [name, value]

    def add(self, name: str, value) -> None:
        self._items.append([canonical(name), str(value)])

    def set(self, name: str, value) -> None:
        self.remove(name)
        self.add(name, value)

    def remove(self, name: str) -> None:
        low = name.lower()
        self._items = [i for i in self._items if i[0].lower() != low]

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        low = name.lower()
        for n, v in self._items:
            if n.lower() == low:
                return v
        return default

    def get_all(self, name: str) -> List[str]:
        low = name.lower()
        return [v for n, v in self._items if n.lower() == low]

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def items(self):
        return [tuple(i) for i in self._items]

    def copy(self) -> "Headers":
        h = Headers()
        h._items = [list(i) for i in self._items]
        return h


class Message:
    def __init__(self) -> None:
        self.headers = Headers()
        self.body: bytes = b""

    # ----- convenience accessors ------------------------------------------
    @property
    def call_id(self) -> str:
        return self.headers.get("Call-ID", "")

    @property
    def cseq(self) -> tuple:
        v = self.headers.get("CSeq", "0 UNKNOWN").split()
        return int(v[0]), (v[1] if len(v) > 1 else "")

    @property
    def from_addr(self) -> Address:
        return Address.parse(self.headers.get("From", ""))

    @property
    def to_addr(self) -> Address:
        return Address.parse(self.headers.get("To", ""))

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").lower()

    def _serialise(self, start_line: str) -> bytes:
        self.headers.set("Content-Length", len(self.body))
        lines = [start_line]
        for n, v in self.headers.items():
            lines.append(f"{n}: {v}")
        head = "\r\n".join(lines) + "\r\n\r\n"
        return head.encode("utf-8") + self.body


class Request(Message):
    def __init__(self, method: str = "", uri: str = "") -> None:
        super().__init__()
        self.method = method
        self.uri = uri

    def to_bytes(self) -> bytes:
        return self._serialise(f"{self.method} {self.uri} {SIP_VERSION}")

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.uri} cseq={self.cseq[0]}>"


class Response(Message):
    def __init__(self, status: int = 200, reason: str = "") -> None:
        super().__init__()
        self.status = status
        self.reason = reason or REASON_PHRASES.get(status, "")

    def to_bytes(self) -> bytes:
        return self._serialise(f"{SIP_VERSION} {self.status} {self.reason}")

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.reason} cseq={self.cseq}>"


def parse(data: bytes) -> Optional[Message]:
    """Parse one SIP message. Returns None for keep-alive CRLF padding."""
    if not data.strip():
        return None
    try:
        head, _, body = data.partition(b"\r\n\r\n")
        text = head.decode("utf-8", errors="replace")
    except Exception as e:  # pragma: no cover
        raise SIPError(f"undecodable message: {e}")
    lines = text.split("\r\n")
    start = lines[0].strip()
    if start.startswith(SIP_VERSION):
        parts = start.split(" ", 2)
        if len(parts) < 2:
            raise SIPError(f"bad status line: {start!r}")
        msg: Message = Response(int(parts[1]), parts[2] if len(parts) > 2 else "")
    else:
        parts = start.split(" ")
        if len(parts) != 3 or parts[2] != SIP_VERSION:
            raise SIPError(f"bad request line: {start!r}")
        msg = Request(parts[0], parts[1])

    # unfold continuation lines
    folded: List[str] = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and folded:
            folded[-1] += " " + line.strip()
        else:
            folded.append(line)
    for line in folded:
        if not line.strip() or ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        # Several headers may be comma-combined; keep Via/Route/Record-Route/
        # Contact split so dialog logic sees one value per entry.
        if canonical(name) in ("Via", "Route", "Record-Route", "Contact", "Allow", "Supported") \
                and "," in value and canonical(name) != "Contact":
            for part in _split_commas(value):
                msg.headers.add(name, part)
        else:
            msg.headers.add(name, value)

    clen = msg.headers.get("Content-Length")
    if clen is not None and clen.isdigit():
        body = body[: int(clen)]
    msg.body = body
    return msg


def _split_commas(value: str) -> List[str]:
    """Split on commas that are not inside quotes or angle brackets."""
    out, buf, depth, quoted = [], [], 0, False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == "<":
            depth += 1
        elif not quoted and ch == ">":
            depth -= 1
        if ch == "," and not quoted and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def parse_via(value: str) -> dict:
    """`SIP/2.0/UDP host:port;branch=...;rport=...` -> dict."""
    proto, _, rest = value.partition(" ")
    parts = [p.strip() for p in rest.split(";")]
    hostport = parts[0]
    host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "")
    params: Dict[str, Optional[str]] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.lower()] = v
        elif p:
            params[p.lower()] = None
    return {"proto": proto, "host": host or hostport, "port": int(port) if port else 5060,
            "params": params}

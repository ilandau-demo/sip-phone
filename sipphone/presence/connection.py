"""The Xsi-Events channel and the plain requests around it (one keep-alive
connection per server; acknowledgements with a path fallback)."""
from __future__ import annotations

import base64
import http.client
import logging
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from ..xsi import XSIError
from .event_processor import DocSplitter, _text

log = logging.getLogger("sipphone.presence")

EVENTS_PATH = "/com.broadsoft.xsi-events/v2.0"
ASYNC_PATH = "/com.broadsoft.async" + EVENTS_PATH           # streaming servlet on most deployments
APP_ID = "sipphone"
EXPIRES = 3600


class Channel:
    """One open event stream of the channel set."""

    def __init__(self, channel_id: str, response, conn, splitter: DocSplitter, pending: List[bytes],
                 expiry: float) -> None:
        self.id = channel_id
        self.response = response
        self.conn = conn
        self.splitter = splitter
        self.pending = pending
        self.expiry = expiry
        self.opened = time.time()

    def close(self) -> None:
        sock = getattr(self.conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)      # wakes a thread blocked in read1() at once
            except OSError:
                pass
        for c in (self.response, self.conn):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.response = self.conn = None


class EventsClient:
    """The event channel(s) of one channel set plus the plain requests around
    them. Several channels per set raise throughput: the Xsp spreads a set's
    events across its open channels and gates each one on its own
    acknowledgement."""

    def __init__(self, host: str, username: str, password: str, verify_tls: bool = True,
                 timeout: float = 20.0, channel_set_id: str = "") -> None:
        if "://" not in host:
            host = "https://" + host
        u = urllib.parse.urlparse(host)
        self.scheme = u.scheme
        self.netloc = u.netloc
        self.timeout = timeout
        self.username = username
        self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._ctx = None
        if self.scheme == "https":
            self._ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        self.channel_set_id = channel_set_id or ("sipphone-" + uuid.uuid5(uuid.NAMESPACE_DNS, username + socket.gethostname()).hex[:12])
        self.channels: List[Channel] = []
        self.stream_netloc = ""              # the server that holds our streams (heartbeats go there)
        self._ack_path = ""                  # eventresponse path that works on this Xsp
        self._pool: Dict[str, list] = {}
        self._pool_lock = threading.Lock()
        self.requests = 0

    # -- compatibility: the first channel's id / the earliest expiry ---------------
    @property
    def channel_id(self) -> str:
        return self.channels[0].id if self.channels else ""

    @property
    def channel_expiry(self) -> float:
        return min((c.expiry for c in self.channels), default=0.0)

    def remember_ack_path(self, path: str) -> None:
        """Reuse the eventresponse path that worked last time (saved in the
        directory cache) so the first ack after a restart doesn't probe."""
        if path in (EVENTS_PATH + "/channel/eventresponse", ASYNC_PATH + "/channel/eventresponse"):
            self._ack_path = path

    @property
    def ack_path(self) -> str:
        return self._ack_path

    def warm(self, netloc: str = "") -> None:
        """Open (and pool) a keep-alive connection so the first ack is not
        slowed by a TCP + TLS handshake."""
        netloc = netloc or self.stream_netloc or self.netloc
        try:
            conn = self._conn(netloc)
            conn.connect()
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._pool_lock:
                self._pool.setdefault(netloc, []).append(conn)
        except Exception:
            pass

    # -- plain requests ------------------------------------------------------------
    def _conn(self, netloc: str = ""):
        netloc = netloc or self.netloc
        host, _, port = netloc.partition(":")
        if self.scheme == "https":
            return http.client.HTTPSConnection(host, int(port) if port else 443, timeout=self.timeout, context=self._ctx)
        return http.client.HTTPConnection(host, int(port) if port else 80, timeout=self.timeout)

    def request(self, method: str, path: str, body: str = "", netloc: str = "") -> tuple:
        netloc = netloc or self.netloc
        self.requests += 1
        with self._pool_lock:
            lst = self._pool.setdefault(netloc, [])
            conn = lst.pop() if lst else None
        for attempt in (1, 2):
            if conn is None:
                conn = self._conn(netloc)
                try:
                    conn.connect()
                    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
            try:
                conn.request(method, path, body=body.encode() if body else None,
                             headers={"Authorization": self._auth, "Content-Type": "application/xml; charset=utf-8",
                                      "Accept": "application/xml", "Connection": "keep-alive"})
                r = conn.getresponse()
                data = r.read()
            except (http.client.HTTPException, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                if attempt == 2:
                    raise
                continue
            if r.getheader("Connection", "").lower() == "close" or r.version < 11:
                conn.close()
            else:
                with self._pool_lock:
                    self._pool.setdefault(netloc, []).append(conn)
            return r.status, data
        raise XSIError("request failed")

    def close_pool(self) -> None:
        with self._pool_lock:
            conns = [c for lst in self._pool.values() for c in lst]
            self._pool.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    def subscribe(self, target: str, package: str, expires: int = EXPIRES) -> str:
        body = ('<?xml version="1.0" encoding="UTF-8"?><Subscription xmlns="http://schema.broadsoft.com/xsi">'
                f"<event>{package}</event><expires>{expires}</expires><channelSetId>{self.channel_set_id}</channelSetId>"
                f"<applicationId>{APP_ID}</applicationId></Subscription>")
        st, data = self.request("POST", f"{EVENTS_PATH}/user/{urllib.parse.quote(target)}", body)
        if st == 401:
            raise XSIError("authentication failed — check the XSI user ID and password")
        if st == 403:
            raise XSIError(f"not monitored — your login may not watch {target}")
        if st == 404:
            raise XSIError(f"not found — {target} is not a user on this system")
        if st != 200:
            raise XSIError(f"HTTP {st} subscribing to {package} for {target}")
        sid = _text(ET.fromstring(data), "subscriptionId")
        if not sid:
            raise XSIError("no subscriptionId in the reply")
        return sid

    def refresh_subscription(self, sid: str, expires: int = EXPIRES) -> None:
        body = ('<?xml version="1.0" encoding="UTF-8"?><Subscription xmlns="http://schema.broadsoft.com/xsi">'
                f"<expires>{expires}</expires></Subscription>")
        st, _ = self.request("PUT", f"{EVENTS_PATH}/subscription/{sid}", body)
        if st != 200:
            raise XSIError(f"HTTP {st} refreshing subscription")

    def unsubscribe(self, sid: str) -> None:
        try:
            self.request("DELETE", f"{EVENTS_PATH}/subscription/{sid}")
        except Exception:
            pass

    def ack(self, event_id: str) -> None:
        """The Xsp delivers the next event on a channel only after the previous one
        is acknowledged (or its response timeout passes), so a failing ack shows
        up as presence lagging seconds. Try the plain path, then the async one."""
        body = ('<?xml version="1.0" encoding="UTF-8"?><EventResponse xmlns="http://schema.broadsoft.com/xsi">'
                f"<eventID>{event_id}</eventID><statusCode>200</statusCode><reason>OK</reason></EventResponse>")
        paths = [self._ack_path] if self._ack_path else [EVENTS_PATH + "/channel/eventresponse",
                                                         ASYNC_PATH + "/channel/eventresponse"]
        last = 0
        for path in paths:
            st, _ = self.request("POST", path, body, netloc=self.stream_netloc)
            if st in (200, 204):
                self._ack_path = path
                return
            last = st
        raise XSIError(f"HTTP {last} acknowledging event {event_id}")

    def heartbeat(self) -> None:
        for ch in list(self.channels):
            st, _ = self.request("PUT", f"{EVENTS_PATH}/channel/{ch.id}/heartbeat", netloc=self.stream_netloc)
            if st not in (200, 204):
                raise XSIError(f"HTTP {st} on heartbeat")

    def refresh_channel(self, expires: int = EXPIRES) -> None:
        body = f'<Channel xmlns="http://schema.broadsoft.com/xsi"><expires>{expires}</expires></Channel>'
        for ch in list(self.channels):
            self.request("PUT", f"{EVENTS_PATH}/channel/{ch.id}", body, netloc=self.stream_netloc)
            ch.expiry = time.time() + expires

    def delete_channel(self) -> None:
        for ch in list(self.channels):
            try:
                self.request("DELETE", f"{EVENTS_PATH}/channel/{ch.id}", netloc=self.stream_netloc)
            except Exception:
                pass

    # -- the stream --------------------------------------------------------------------
    def open_channel(self, read_timeout: float = 60.0, expires: int = EXPIRES):
        """Open one more channel of the set. Returns (stream, splitter, pending
        docs) like before; the Channel is also appended to ``self.channels``."""
        ch = self.add_channel(read_timeout, expires)
        return ch.response, ch.splitter, ch.pending

    def add_channel(self, read_timeout: float = 60.0, expires: int = EXPIRES) -> "Channel":
        body = ('<?xml version="1.0" encoding="UTF-8"?><Channel xmlns="http://schema.broadsoft.com/xsi">'
                f"<channelSetId>{self.channel_set_id}</channelSetId><priority>1</priority><weight>100</weight>"
                f"<expires>{expires}</expires></Channel>")
        last = ""
        for path in (ASYNC_PATH + "/channel", EVENTS_PATH + "/channel"):
            conn = self._conn()
            conn.timeout = read_timeout
            try:
                conn.request("POST", path, body=body.encode(),
                             headers={"Authorization": self._auth, "Content-Type": "application/xml; charset=utf-8",
                                      "Accept": "application/xml", "Connection": "keep-alive"})
                r = conn.getresponse()
                if r.status == 401:
                    raise XSIError("authentication failed — check the XSI user ID and password")
                if r.status != 200:
                    last = f"HTTP {r.status} opening the event channel at {path}"
                    r.read(); conn.close()
                    continue
                splitter = DocSplitter()
                docs: List[bytes] = []
                deadline = time.time() + self.timeout
                while not docs and time.time() < deadline:
                    chunk = r.read1(4096)
                    if not chunk:
                        break
                    docs = splitter.feed(chunk)
                if not docs:
                    last = "the event channel opened but sent no channel id"
                    conn.close()
                    continue
                cid = _text(ET.fromstring(docs[0]), "channelId")
                if not cid:
                    last = "no channelId in the channel reply"
                    conn.close()
                    continue
                self.stream_netloc = self.netloc
                ch = Channel(cid, r, conn, splitter, docs[1:], time.time() + expires)
                self.channels.append(ch)
                return ch
            except (OSError, http.client.HTTPException) as e:
                last = f"cannot reach the XSI events service: {e}"
                try:
                    conn.close()
                except Exception:
                    pass
        raise XSIError(last or "could not open the event channel")

    def close_stream(self) -> None:
        chans, self.channels = self.channels, []
        for ch in chans:
            ch.close()

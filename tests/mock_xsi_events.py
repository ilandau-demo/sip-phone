"""A small BroadWorks look-alike: Xsi-Actions (directory, registrations) and
Xsi-Events (streaming channel, subscriptions, heartbeats, acks).

Two logins: USER (may subscribe to `allowed`) and ADMIN (may subscribe to
anyone and read registrations)."""
import base64
import urllib.parse
import json
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PW = "me1001@nextiva.com", "pw"
ADMIN, APW = "xsi.admin", "adminpw"
XSI = "http://schema.broadsoft.com/xsi"


def ev(target, kind, inner, seq=1):
    return (f'<?xml version="1.0" encoding="UTF-8"?><xsi:Event xsi1:type="xsi:SubscriptionEvent" '
            f'xmlns:xsi1="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsi="{XSI}">'
            f"<xsi:eventID>{uuid.uuid4()}</xsi:eventID><xsi:sequenceNumber>{seq}</xsi:sequenceNumber>"
            f"<xsi:userId>{USER}</xsi:userId><xsi:targetId>{target}</xsi:targetId>"
            f'<xsi:eventData xsi1:type="xsi:{kind}">{inner}</xsi:eventData></xsi:Event>').encode()


def call_xml(cid, state, personality="Terminator", name="", number="", start=None):
    rp = f"<xsi:remoteParty><xsi:name>{name}</xsi:name><xsi:address>tel:{number}</xsi:address></xsi:remoteParty>" if (name or number) else ""
    st = f"<xsi:startTime>{int((start or time.time()) * 1000)}</xsi:startTime>"
    return (f"<xsi:call><xsi:callId>{cid}</xsi:callId><xsi:personality>{personality}</xsi:personality>"
            f"<xsi:state>{state}</xsi:state>{rp}{st}</xsi:call>")


class MockXsi:
    def __init__(self, monitored, directory=None, allowed=None, dnd=None):
        """monitored: the people this login may subscribe to [(userId, first, last, ext)];
        directory: everyone the Enterprise directory lists (defaults to monitored)."""
        self.monitored = monitored
        self.directory = directory or monitored
        self.allowed = set(allowed if allowed is not None else [m[0] for m in monitored])
        self.blf_requests = 0
        self.registrations = {}                   # userId -> count (default 1); admin login only
        self.registration_requests = 0
        self.dnd = dict(dnd or {})
        self.ack_mode = "any"                     # any | async | fail: which eventresponse path works
        self.calls = {}                           # userId -> [call xml]
        self.subs = {}                            # subscriptionId -> (target, package)
        self.acks = []
        self.heartbeats = 0
        self.channels = {}                        # channelId -> queue
        self.closed = threading.Event()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.closed.set()
        self.srv.shutdown(); self.srv.server_close()

    def push(self, target, kind, inner):
        for q in list(self.channels.values()):
            q.put(ev(target, kind, inner))

    def set_calls(self, target, calls, kind="CallUpdatedEvent"):
        self.calls[target] = calls
        for c in calls:
            self.push(target, kind, c)

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"           # keep-alive, like the real Xsp
            def log_message(self, *a):
                pass

            def _auth(self):
                h = self.headers.get("Authorization", "")
                who = base64.b64decode(h[6:]).decode() if h.startswith("Basic ") else ""
                self.admin = who == f"{ADMIN}:{APW}"
                ok = self.admin or who == f"{USER}:{PW}"
                if not ok:
                    self.send_response(401); self.end_headers()
                return ok

            def _xml(self, body, status=200):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(status); self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                if not self._auth():
                    return
                m = re.match(r"/com\.broadsoft\.xsi-actions/v2\.0/user/([^/]+)/profile/Registrations$", self.path)
                if m:
                    mock.registration_requests += 1
                    if not self.admin:
                        return self._xml(f'<ErrorInfo xmlns="{XSI}"><summary>forbidden</summary></ErrorInfo>', 403)
                    uid = urllib.parse.unquote(m.group(1))
                    n = mock.registrations.get(uid, 1)
                    regs = "".join(f"<registration><deviceName>dev{i}</deviceName></registration>" for i in range(n))
                    return self._xml(f'<Registrations xmlns="{XSI}">{regs}</Registrations>')
                if "BusyLampField" in self.path:               # 2.4: the app must never touch the BLF service
                    mock.blf_requests += 1
                    return self._xml(f'<ErrorInfo xmlns="{XSI}"><summary>BLF access is not used</summary></ErrorInfo>', 403)
                if re.match(r"/com\.broadsoft\.xsi-actions/v2\.0/user/[^/]+/directories/Enterprise", self.path):
                    items = "".join(f"<directoryDetails><userId>{u}</userId><firstName>{f}</firstName><lastName>{l}</lastName>"
                                    f"<extension>{e}</extension><department>Sales</department></directoryDetails>"
                                    for u, f, l, e in mock.directory)
                    return self._xml(f'<Enterprise xmlns="{XSI}"><enterpriseDirectory>{items}</enterpriseDirectory></Enterprise>')
                self._xml("<nope/>", 404)

            def do_POST(self):
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                if self.path.endswith("xsi-events/v2.0/channel"):
                    if "/com.broadsoft.async/" not in self.path:
                        return self._xml("<nope/>", 404)          # Nextiva-style: streaming only on the async path
                    cid = uuid.uuid4().hex[:8]
                    q = queue.Queue(); mock.channels[cid] = q
                    self.send_response(200); self.send_header("Content-Type", "application/xml")
                    self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                    first = f'<?xml version="1.0"?><Channel xmlns="{XSI}"><channelId>{cid}</channelId><expires>3600</expires></Channel>'.encode()
                    self._chunk(first)
                    try:
                        while not mock.closed.is_set() and cid in mock.channels:
                            try:
                                doc = q.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            self._chunk(doc)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    mock.channels.pop(cid, None)
                    return
                if self.path.endswith("/channel/eventresponse"):
                    if mock.ack_mode == "fail" or (mock.ack_mode == "async" and "/com.broadsoft.async/" not in self.path):
                        return self._xml("<nope/>", 404)
                    mock.acks.append(re.search(r"<eventID>([^<]+)", body).group(1))
                    return self._xml("")
                m = re.match(r"/com\.broadsoft\.xsi-events/v2\.0/user/([^/]+)$", self.path)
                if m:
                    target = urllib.parse.unquote(m.group(1))
                    pkg = re.search(r"<event>([^<]+)", body).group(1)
                    if target not in mock.allowed and not self.admin:
                        return self._xml(f'<ErrorInfo xmlns="{XSI}"><summary>not monitored</summary></ErrorInfo>', 403)
                    sid = uuid.uuid4().hex[:8]
                    mock.subs[sid] = (target, pkg)
                    self._xml(f'<Subscription xmlns="{XSI}"><subscriptionId>{sid}</subscriptionId><expires>3600</expires></Subscription>')
                    # initial state, like BroadWorks does right after a subscription
                    if pkg == "Standard Call":
                        calls = "".join(mock.calls.get(target, []))
                        mock.push(target, "CallSubscriptionEvent", f"<xsi:calls>{calls}</xsi:calls><xsi:hookStatus>"
                                  f"{'Off-Hook' if calls else 'On-Hook'}</xsi:hookStatus>")
                    elif pkg == "Do Not Disturb":
                        mock.push(target, "DoNotDisturbEvent", f"<xsi:info><xsi:active>{'true' if mock.dnd.get(target) else 'false'}</xsi:active></xsi:info>")
                    return
                self._xml("<nope/>", 404)

            def do_PUT(self):
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                m = re.match(r"/com\.broadsoft\.xsi-events/v2\.0/channel/([^/]+)/heartbeat$", self.path)
                if m:
                    mock.heartbeats += 1
                    q = mock.channels.get(m.group(1))
                    if q is None:
                        return self._xml("<nope/>", 404)
                    q.put(f'<ChannelHeartbeat xmlns="{XSI}"/>'.encode())
                    return self._xml("")
                self._xml("")

            def do_DELETE(self):
                if not self._auth():
                    return
                m = re.match(r"/com\.broadsoft\.xsi-events/v2\.0/subscription/([^/]+)$", self.path)
                if m:
                    mock.subs.pop(m.group(1), None)
                m = re.match(r"/com\.broadsoft\.xsi-events/v2\.0/channel/([^/]+)$", self.path)
                if m:
                    mock.channels.pop(m.group(1), None)
                self._xml("")

            def _chunk(self, data):
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n"); self.wfile.flush()

        return H

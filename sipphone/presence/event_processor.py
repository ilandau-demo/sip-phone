"""Xsi-Events documents → state changes.

DocSplitter cuts the chunked stream into whole XML documents; EventProcessor
parses them, drops duplicates and stale events, and applies them to Targets.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

from .models import Call, Target, clean_number

log = logging.getLogger("sipphone.presence")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(e: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if e is None:
        return None
    for c in e.iter():
        if _local(c.tag) == name:
            return c
    return None


def _text(e: Optional[ET.Element], name: str, default: str = "") -> str:
    f = _find(e, name)
    return (f.text or "").strip() if f is not None and f.text else default


def _children(e: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in e.iter() if _local(c.tag) == name]


def _type(e: ET.Element) -> str:
    for k, v in e.attrib.items():
        if _local(k) == "type":
            return v.split(":")[-1]
    return ""


class DocSplitter:
    """Cuts a byte stream into whole XML documents (chunk boundaries aren't
    visible to us, so we look for the root element's end)."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: bytes) -> List[bytes]:
        self.buf += data
        out = []
        while True:
            doc = self._next()
            if doc is None:
                break
            out.append(doc)
        return out

    def _next(self) -> Optional[bytes]:
        b = self.buf.lstrip()
        if not b:
            self.buf = b""
            return None
        start = 0
        if b.startswith(b"<?xml"):
            end_decl = b.find(b"?>")
            if end_decl < 0:
                return None
            start = end_decl + 2
        m = re.compile(rb"<([A-Za-z_][\w:.-]*)").search(b, start)
        if not m:
            return None
        root = m.group(1)
        gt = b.find(b">", m.end())
        if gt < 0:
            return None
        if b[gt - 1:gt] == b"/":                       # <ChannelHeartbeat .../>
            doc, self.buf = b[:gt + 1], b[gt + 1:]
            return doc
        close = b"</" + root + b">"
        i = b.find(close, gt)
        if i < 0:
            return None
        doc, self.buf = b[:i + len(close)], b[i + len(close):]
        return doc


def parse_call(el: ET.Element) -> Call:
    c = Call(call_id=_text(el, "callId"), state=_text(el, "state"), personality=_text(el, "personality"))
    rp = _find(el, "remoteParty")
    if rp is not None:
        c.name = _text(rp, "name")
        c.number = clean_number(_text(rp, "address") or _text(rp, "userDN"))
    for k in ("answerTime", "startTime"):
        t = _text(el, k)
        if t.isdigit():
            c.start = int(t) / 1000.0
            break
    return c


class EventProcessor:
    """Applies event documents to targets. Returns the event kind, or
    'heartbeat' / 'terminated' / 'duplicate' / 'stale' / ''."""

    def __init__(self, targets: Dict[str, Target], ack: Callable[[str], None], lock=None,
                 dedupe_size: int = 512) -> None:
        self.targets = targets
        self.ack = ack
        self.lock = lock
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self.dedupe_size = dedupe_size
        self.last_event = 0.0
        self.duplicates = 0
        self.stale_dropped = 0

    def _target_for(self, target_id: str) -> Optional[Target]:
        t = self.targets.get(target_id)
        if t is None:
            short = target_id.lower().split("@")[0]
            t = next((x for x in self.targets.values() if x.user.user_id.lower().split("@")[0] == short), None)
        return t

    def handle(self, doc: bytes) -> str:
        try:
            root = ET.fromstring(doc)
        except ET.ParseError:
            log.debug("unparsable event: %r", doc[:200])
            return ""
        name = _local(root.tag)
        if name == "ChannelHeartbeat":
            return "heartbeat"
        if name != "Event":
            return name
        event_id = _text(root, "eventID")
        data = _find(root, "eventData")
        kind = _type(data) if data is not None else _type(root)
        if kind == "ChannelTerminatedEvent":
            return "terminated"
        if event_id:
            self.ack(event_id)
            if event_id in self._seen:                 # redelivered (our ack was late): apply once
                self.duplicates += 1
                return "duplicate"
            self._seen[event_id] = None
            while len(self._seen) > self.dedupe_size:
                self._seen.popitem(last=False)
        seq_text = _text(root, "sequenceNumber")
        seq = int(seq_text) if seq_text.isdigit() else 0
        target_id = _text(root, "targetId")
        lock = self.lock
        if lock:
            lock.acquire()
        try:
            t = self._target_for(target_id)
            if t is None or data is None:
                return kind
            # sequence numbers are per subscription; an event older than the last one
            # we applied for this person is stale (reordered on reconnect) and skipped,
            # except snapshots, which always carry the whole truth
            if seq and t.last_seq and seq < t.last_seq and kind != "CallSubscriptionEvent":
                self.stale_dropped += 1
                return "stale"
            if seq:
                t.last_seq = max(t.last_seq, seq)
            self._apply(t, kind, data)
            t.last_update = self.last_event = time.time()
        finally:
            if lock:
                lock.release()
        return kind

    @staticmethod
    def _apply(t: Target, kind: str, data: ET.Element) -> None:
        if kind == "CallSubscriptionEvent":
            t.calls = {}
            for c in _children(data, "call"):
                pc = parse_call(c)
                if pc.call_id and pc.state.lower() not in ("released", "detached"):
                    t.calls[pc.call_id] = pc
            t.hook = _text(data, "hookStatus")
            t.synced = True
        elif kind == "HookStatusEvent":
            t.hook = _text(data, "hookStatus")
            t.synced = True
        elif kind in ("DoNotDisturbEvent", "DoNotDisturbSubscriptionEvent"):
            t.dnd = _text(data, "active").lower() == "true"
            t.synced = True
        elif kind.startswith("Call") or kind.startswith("Conference"):
            for c in _children(data, "call"):
                pc = parse_call(c)
                if not pc.call_id:
                    continue
                if pc.state.lower() in ("released", "detached") or kind in ("CallReleasedEvent", "CallDetachedEvent"):
                    t.calls.pop(pc.call_id, None)
                else:
                    old = t.calls.get(pc.call_id)
                    if old and not pc.start:
                        pc.start = old.start
                    if old and not pc.name and not pc.number:
                        pc.name, pc.number = old.name, old.number
                    t.calls[pc.call_id] = pc
            if kind in ("CallReleasedEvent", "CallReleasingEvent") and not _children(data, "call"):
                t.calls = {}
            # the call list is the truth once call events flow; the hook status from the
            # initial snapshot must not keep someone "on a call" after their calls end
            t.hook = "Off-Hook" if t.calls else "On-Hook"
            t.synced = True

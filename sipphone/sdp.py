"""Minimal SDP (RFC 4566) offer/answer for a single G.711 audio stream."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# payload type -> (encoding name, clock rate)
STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}
DTMF_PT = 101


@dataclass
class MediaDescription:
    ip: str
    port: int
    payloads: List[int]
    rtpmap: Dict[int, str] = field(default_factory=dict)   # pt -> "PCMU/8000"
    fmtp: Dict[int, str] = field(default_factory=dict)
    direction: str = "sendrecv"
    ptime: int = 20

    def codec_name(self, pt: int) -> str:
        if pt in self.rtpmap:
            return self.rtpmap[pt].split("/")[0].upper()
        if pt in STATIC_PT:
            return STATIC_PT[pt][0]
        return f"PT{pt}"

    def dtmf_payload(self) -> Optional[int]:
        for pt, enc in self.rtpmap.items():
            if enc.lower().startswith("telephone-event"):
                return pt
        return None

    def audio_payloads(self) -> List[int]:
        return [pt for pt in self.payloads if self.codec_name(pt) in ("PCMU", "PCMA")]


def build_offer(ip: str, port: int, codecs=("PCMU", "PCMA"), session_id: Optional[int] = None,
                direction: str = "sendrecv", dtmf: bool = True) -> bytes:
    sid = session_id or int(time.time())
    pts = [0 if c == "PCMU" else 8 for c in codecs]
    fmt = " ".join(str(p) for p in pts) + (f" {DTMF_PT}" if dtmf else "")
    lines = [
        "v=0",
        f"o=- {sid} {sid} IN IP4 {ip}",
        "s=sipphone",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
    ]
    for c, pt in zip(codecs, pts):
        lines.append(f"a=rtpmap:{pt} {c}/8000")
    if dtmf:
        lines.append(f"a=rtpmap:{DTMF_PT} telephone-event/8000")
        lines.append(f"a=fmtp:{DTMF_PT} 0-16")
    lines += ["a=ptime:20", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode()


def build_answer(ip: str, port: int, offer: MediaDescription, direction: str = "sendrecv") -> bytes:
    """Pick the first mutually supported audio codec from the offer."""
    chosen = offer.audio_payloads()[:1]
    if not chosen:
        raise ValueError("no supported audio codec in offer")
    dtmf = offer.dtmf_payload()
    sid = int(time.time())
    fmt = " ".join(str(p) for p in chosen) + (f" {dtmf}" if dtmf is not None else "")
    lines = [
        "v=0",
        f"o=- {sid} {sid} IN IP4 {ip}",
        "s=sipphone",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {fmt}",
    ]
    for pt in chosen:
        lines.append(f"a=rtpmap:{pt} {offer.codec_name(pt)}/8000")
    if dtmf is not None:
        lines.append(f"a=rtpmap:{dtmf} telephone-event/8000")
        lines.append(f"a=fmtp:{dtmf} 0-16")
    lines += ["a=ptime:20", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode()


def parse(body: bytes) -> MediaDescription:
    """Return the first audio media section of an SDP body."""
    text = body.decode("utf-8", errors="replace")
    session_ip: Optional[str] = None
    media: Optional[MediaDescription] = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        key, val = line[0], line[2:]
        if key == "c":
            parts = val.split()
            if len(parts) >= 3:
                ip = parts[2].split("/")[0]
                if media is None:
                    session_ip = ip
                else:
                    media.ip = ip
        elif key == "m":
            parts = val.split()
            if parts[0] == "audio" and media is None:
                media = MediaDescription(ip=session_ip or "0.0.0.0", port=int(parts[1]),
                                         payloads=[int(p) for p in parts[3:] if p.isdigit()])
            elif media is not None:
                break  # only first audio section
        elif key == "a" and media is not None:
            if val.startswith("rtpmap:"):
                pt, _, enc = val[7:].partition(" ")
                media.rtpmap[int(pt)] = enc.strip()
            elif val.startswith("fmtp:"):
                pt, _, params = val[5:].partition(" ")
                media.fmtp[int(pt)] = params.strip()
            elif val in ("sendrecv", "sendonly", "recvonly", "inactive"):
                media.direction = val
            elif val.startswith("ptime:"):
                try:
                    media.ptime = int(val[6:])
                except ValueError:
                    pass
    if media is None:
        raise ValueError("no audio m= line in SDP")
    return media

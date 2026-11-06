"""RTP (RFC 3550) audio session with G.711 and RFC 4733 DTMF events."""
from __future__ import annotations

import logging
import queue
import random
import socket
import struct
import threading
import time
from typing import Callable, Optional, Tuple

from . import g711

log = logging.getLogger("sipphone.rtp")

SAMPLE_RATE = 8000
PTIME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * PTIME_MS // 1000   # 160
FRAME_BYTES_PCM = FRAME_SAMPLES * 2
SILENCE_PCM = b"\x00" * FRAME_BYTES_PCM


def build_header(pt: int, seq: int, ts: int, ssrc: int, marker: bool = False) -> bytes:
    b0 = 0x80  # V=2, P=0, X=0, CC=0
    b1 = (0x80 if marker else 0) | (pt & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)


def parse_packet(data: bytes) -> Optional[Tuple[int, int, int, int, bool, bytes]]:
    if len(data) < 12:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
    if b0 >> 6 != 2:
        return None
    cc = b0 & 0x0F
    ext = b0 & 0x10
    padding = b0 & 0x20
    offset = 12 + cc * 4
    if ext:
        if len(data) < offset + 4:
            return None
        _, ext_len = struct.unpack("!HH", data[offset:offset + 4])
        offset += 4 + ext_len * 4
    payload = data[offset:]
    if padding and payload:
        payload = payload[: -payload[-1]]
    return b1 & 0x7F, seq, ts, ssrc, bool(b1 & 0x80), payload


def allocate_rtp_socket(bind_ip: str = "0.0.0.0", port_range=(10000, 20000)) -> socket.socket:
    """Bind an even UDP port for RTP (RTCP would be port+1)."""
    lo, hi = port_range
    for _ in range(200):
        port = random.randrange(lo, hi) & ~1
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((bind_ip, port))
            return s
        except OSError:
            s.close()
    raise OSError("no free RTP port")


class RTPSession:
    """Bidirectional RTP audio.

    `mic_source()` is called every 20ms and must return 320 bytes of 16-bit
    mono 8kHz PCM (or None for silence); `speaker_sink(pcm)` receives decoded
    frames from the far end.
    """

    def __init__(self, sock: socket.socket, codec: str = "PCMU", payload_type: int = 0,
                 dtmf_payload_type: Optional[int] = 101,
                 mic_source: Optional[Callable[[], Optional[bytes]]] = None,
                 speaker_sink: Optional[Callable[[bytes], None]] = None) -> None:
        self.sock = sock
        self.sock.settimeout(0.2)
        self.local_port = sock.getsockname()[1]
        self.codec = codec
        self.pt = payload_type
        self.dtmf_pt = dtmf_payload_type
        self.mic_source = mic_source
        self.speaker_sink = speaker_sink
        self.remote: Optional[Tuple[str, int]] = None
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.encode = g711.ENCODERS[codec]
        self.decode = g711.DECODERS[codec]
        self._dtmf_queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._tx = threading.Thread(target=self._tx_loop, name="rtp-tx", daemon=True)
        self._rx = threading.Thread(target=self._rx_loop, name="rtp-rx", daemon=True)
        self.rx_packets = 0
        self.tx_packets = 0
        self.direction = "sendrecv"
        self.muted = False

    def set_remote(self, ip: str, port: int) -> None:
        self.remote = (ip, port)

    def start(self) -> None:
        self._rx.start()
        self._tx.start()

    def stop(self) -> None:
        self._stop.set()
        for t in (self._tx, self._rx):
            if t.is_alive():
                t.join(timeout=1)
        try:
            self.sock.close()
        except OSError:
            pass

    def send_dtmf(self, digits: str) -> None:
        for d in digits:
            self._dtmf_queue.put(d)

    # ------------------------------------------------------------------
    _DTMF_EVENTS = {**{str(i): i for i in range(10)}, "*": 10, "#": 11,
                    "A": 12, "B": 13, "C": 14, "D": 15}

    def _send(self, pt: int, payload: bytes, marker: bool = False) -> None:
        if not self.remote:
            return
        pkt = build_header(pt, self.seq, self.ts, self.ssrc, marker) + payload
        try:
            self.sock.sendto(pkt, self.remote)
            self.tx_packets += 1
        except OSError as e:
            log.debug("rtp send failed: %s", e)
        self.seq = (self.seq + 1) & 0xFFFF

    def _send_dtmf_event(self, digit: str) -> None:
        """RFC 4733: 160ms tone = 8 packets of 20ms, 3 end packets."""
        ev = self._DTMF_EVENTS.get(digit.upper())
        if ev is None or self.dtmf_pt is None:
            return
        start_ts = self.ts
        duration = 0
        for i in range(8):
            duration += FRAME_SAMPLES
            payload = struct.pack("!BBH", ev, 10, duration)  # volume 10 dBm0
            saved = self.ts
            self.ts = start_ts
            self._send(self.dtmf_pt, payload, marker=(i == 0))
            self.ts = saved + FRAME_SAMPLES
            time.sleep(PTIME_MS / 1000)
        for _ in range(3):
            payload = struct.pack("!BBH", ev, 0x80 | 10, duration)  # end bit
            saved = self.ts
            self.ts = start_ts
            self._send(self.dtmf_pt, payload)
            self.ts = saved
        self.ts += FRAME_SAMPLES * 3  # inter-digit gap

    def _tx_loop(self) -> None:
        next_t = time.monotonic()
        first = True
        while not self._stop.is_set():
            # DTMF has priority over audio frames
            try:
                digit = self._dtmf_queue.get_nowait()
            except queue.Empty:
                digit = None
            if digit:
                self._send_dtmf_event(digit)
                next_t = time.monotonic()
                continue
            if self.direction in ("sendrecv", "sendonly") and self.remote:
                pcm = None
                if self.mic_source:
                    try:
                        pcm = self.mic_source()
                    except Exception:
                        log.exception("mic source failed")
                if self.muted or not pcm or len(pcm) != FRAME_BYTES_PCM:
                    pcm = SILENCE_PCM
                self._send(self.pt, self.encode(pcm), marker=first)
                first = False
            self.ts = (self.ts + FRAME_SAMPLES) & 0xFFFFFFFF
            next_t += PTIME_MS / 1000
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.2:  # fell badly behind; resync
                next_t = time.monotonic()

    def _rx_loop(self) -> None:
        learned = False
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = parse_packet(data)
            if not parsed:
                continue
            pt, seq, ts, ssrc, marker, payload = parsed
            self.rx_packets += 1
            # Symmetric RTP: latch onto the first source we hear from (NAT help)
            if not learned and self.remote and addr != self.remote:
                log.info("symmetric RTP: switching remote media to %s:%d", *addr)
                self.remote = addr
            learned = True
            if pt == self.dtmf_pt:
                if len(payload) >= 4 and payload[1] & 0x80:
                    ev = payload[0]
                    digit = next((k for k, v in self._DTMF_EVENTS.items() if v == ev), "?")
                    log.info("DTMF received: %s", digit)
                continue
            if pt == self.pt and self.speaker_sink and self.direction in ("sendrecv", "recvonly"):
                try:
                    self.speaker_sink(self.decode(payload))
                except Exception:
                    log.exception("speaker sink failed")

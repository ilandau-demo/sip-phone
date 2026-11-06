"""UDP transport for SIP with a receive thread and NAT keep-alives."""
from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Optional, Tuple

from . import message

log = logging.getLogger("sipphone.transport")

Addr = Tuple[str, int]


def local_ip_towards(host: str, port: int = 5060) -> str:
    """Pick the local interface address the OS would use to reach `host`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class UDPTransport:
    def __init__(self, bind_ip: str = "0.0.0.0", bind_port: int = 5060,
                 on_message: Optional[Callable[[message.Message, Addr], None]] = None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, bind_port))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.on_message = on_message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sip-udp-rx", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()

    def send(self, msg: message.Message, addr: Addr) -> None:
        data = msg.to_bytes()
        log.debug("TX -> %s:%d\n%s", addr[0], addr[1], data.decode(errors="replace"))
        self.sock.sendto(data, addr)

    def send_raw(self, data: bytes, addr: Addr) -> None:
        self.sock.sendto(data, addr)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if data in (b"\r\n", b"\r\n\r\n", b""):
                continue  # keep-alive pong
            log.debug("RX <- %s:%d\n%s", addr[0], addr[1], data.decode(errors="replace"))
            try:
                msg = message.parse(data)
            except message.SIPError as e:
                log.warning("dropping unparsable datagram from %s: %s", addr, e)
                continue
            if msg is not None and self.on_message:
                try:
                    self.on_message(msg, addr)
                except Exception:  # keep the rx thread alive
                    log.exception("handler error")

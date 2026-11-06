"""Microphone / speaker I/O at 8 kHz mono 16-bit, framed to 20 ms.

Uses `sounddevice` (PortAudio) when installed. Falls back to a silent
`NullAudio` device so the SIP stack still works for signalling tests, and to
`FileAudio` for playing a WAV file / recording the far end to disk.
"""
from __future__ import annotations

import collections
import logging
import threading
import wave
from typing import Deque, Optional

from .rtp import FRAME_BYTES_PCM, SAMPLE_RATE

log = logging.getLogger("sipphone.audio")


class NullAudio:
    name = "null"

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read_frame(self) -> Optional[bytes]:
        return None
    def write_frame(self, pcm: bytes) -> None: ...


class FileAudio:
    """Play a WAV (8k mono 16-bit) to the far end and record what we hear."""
    name = "file"

    def __init__(self, play_path: Optional[str] = None, record_path: Optional[str] = None) -> None:
        self._play = wave.open(play_path, "rb") if play_path else None
        if self._play:
            if (self._play.getframerate(), self._play.getnchannels(), self._play.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                raise ValueError("play file must be 8000 Hz, mono, 16-bit PCM")
        self._rec = wave.open(record_path, "wb") if record_path else None
        if self._rec:
            self._rec.setnchannels(1)
            self._rec.setsampwidth(2)
            self._rec.setframerate(SAMPLE_RATE)
        self._lock = threading.Lock()

    def start(self) -> None: ...

    def stop(self) -> None:
        with self._lock:
            if self._play:
                self._play.close()
                self._play = None
            if self._rec:
                self._rec.close()
                self._rec = None

    def read_frame(self) -> Optional[bytes]:
        with self._lock:
            if not self._play:
                return None
            data = self._play.readframes(FRAME_BYTES_PCM // 2)
            if len(data) < FRAME_BYTES_PCM:
                self._play.rewind()  # loop
                data = (data + self._play.readframes(FRAME_BYTES_PCM // 2))[:FRAME_BYTES_PCM]
            return data

    def write_frame(self, pcm: bytes) -> None:
        with self._lock:
            if self._rec:
                self._rec.writeframes(pcm)


class SoundDeviceAudio:
    """Live mic/speaker via PortAudio. Jitter buffer of ~60 ms on playback."""
    name = "sounddevice"

    def __init__(self, input_device=None, output_device=None, jitter_frames: int = 3) -> None:
        import sounddevice as sd  # noqa: F401  (import error surfaces here)
        self._sd = sd
        self.input_device = input_device
        self.output_device = output_device
        self._mic: Deque[bytes] = collections.deque(maxlen=10)
        self._spk: Deque[bytes] = collections.deque(maxlen=25)
        self._jitter = jitter_frames
        self._primed = False
        self._stream = None
        self._lock = threading.Lock()

    def _callback(self, indata, outdata, frames, time_info, status):  # noqa: D401
        if status:
            log.debug("audio status: %s", status)
        with self._lock:
            self._mic.append(bytes(indata))
            if not self._primed and len(self._spk) >= self._jitter:
                self._primed = True
            if self._primed and self._spk:
                chunk = self._spk.popleft()
            else:
                chunk = b"\x00" * FRAME_BYTES_PCM
                if not self._spk:
                    self._primed = False
        outdata[:] = chunk[: len(outdata)].ljust(len(outdata), b"\x00")

    def start(self) -> None:
        self._stream = self._sd.RawStream(
            samplerate=SAMPLE_RATE, blocksize=FRAME_BYTES_PCM // 2, dtype="int16", channels=1,
            device=(self.input_device, self.output_device), callback=self._callback, latency="low")
        self._stream.start()
        log.info("audio started: %s", self._sd.query_devices(self._stream.device[0])["name"]
                 if self._stream.device else "default")

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._mic.popleft() if self._mic else None

    def write_frame(self, pcm: bytes) -> None:
        with self._lock:
            self._spk.append(pcm)


def open_audio(kind: str = "auto", **kw):
    """Factory: 'auto' tries sounddevice then falls back to null."""
    if kind == "null":
        return NullAudio()
    if kind == "file":
        return FileAudio(kw.get("play"), kw.get("record"))
    try:
        return SoundDeviceAudio(kw.get("input_device"), kw.get("output_device"))
    except Exception as e:
        if kind == "sounddevice":
            raise
        log.warning("sounddevice unavailable (%s); running without audio hardware", e)
        return NullAudio()


def list_devices() -> str:
    try:
        import sounddevice as sd
        return str(sd.query_devices())
    except Exception as e:
        return f"sounddevice not available: {e}"

"""Speech-to-text of the far end: what the caller says shows up as text.

Architecture
------------
``Transcriber`` sits on the speaker path.  Every decoded 20 ms frame from the
RTP receiver is passed to ``feed(pcm)`` (and still forwarded to the real
speaker).  Frames are grouped into utterances with a simple energy gate: speech
starts when a frame is above the noise floor, and the utterance ends after
~700 ms of silence or 20 s of speech.  Each utterance goes to a worker thread
that calls the engine, and the resulting text is handed to ``on_text``.

Engines (``engine=``):

* ``whisper`` – faster-whisper (``pip install faster-whisper``).  The model
                (default ``base.en``, ~75 MB) downloads itself on first use.
                Good accuracy on 8 kHz telephone audio; ~0.5-2 s per utterance on CPU.
* ``vosk``    – Vosk (``pip install vosk``) with a model folder, e.g.
                vosk-model-small-en-us-0.15 from https://alphacephei.com/vosk/models
                Lighter and faster, less accurate.
* any callable ``(pcm16_8k_bytes) -> str``.
"""
from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from typing import Callable, Optional, Union

from .rtp import FRAME_BYTES_PCM

log = logging.getLogger("sipphone.stt")

Recognizer = Callable[[bytes], str]   # 8 kHz mono int16 PCM -> text


def _rms(pcm: bytes) -> float:
    try:
        import numpy as np
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    except ImportError:
        n = len(pcm) // 2
        if not n:
            return 0.0
        s = struct.unpack(f"<{n}h", pcm[: n * 2])
        return (sum(v * v for v in s) / n) ** 0.5


# ----------------------------------------------------------------------------
# engines
# ----------------------------------------------------------------------------
class WhisperEngine:
    def __init__(self, model: str = "base.en", device: str = "cpu", compute_type: str = "int8",
                 language: Optional[str] = "en") -> None:
        from faster_whisper import WhisperModel  # type: ignore
        self.model = WhisperModel(model or "base.en", device=device, compute_type=compute_type)
        self.language = language

    def __call__(self, pcm: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        # whisper wants 16 kHz: simple 2x linear upsample is plenty for 8 kHz telephone audio
        audio16 = np.interp(np.arange(0, len(audio), 0.5), np.arange(len(audio)), audio).astype(np.float32)
        segments, _ = self.model.transcribe(audio16, language=self.language, beam_size=1,
                                            vad_filter=False, condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments).strip()


class VoskEngine:
    def __init__(self, model: str = "vosk-model-small-en-us-0.15") -> None:
        import vosk  # type: ignore
        vosk.SetLogLevel(-1)
        self.model = vosk.Model(model)
        self._vosk = vosk

    def __call__(self, pcm: bytes) -> str:
        import json
        rec = self._vosk.KaldiRecognizer(self.model, 8000)
        rec.AcceptWaveform(pcm)
        return json.loads(rec.FinalResult()).get("text", "").strip()


def make_engine(name: str, **kw) -> Recognizer:
    name = (name or "whisper").lower()
    if name in ("whisper", "faster-whisper"):
        return WhisperEngine(**{k: v for k, v in kw.items() if k in ("model", "device", "compute_type", "language") and v})
    if name == "vosk":
        return VoskEngine(**{k: v for k, v in kw.items() if k in ("model",) and v})
    raise ValueError(f"unknown stt engine {name!r}")


# ----------------------------------------------------------------------------
# the transcriber
# ----------------------------------------------------------------------------
class Transcriber:
    def __init__(self, engine: Union[str, Recognizer] = "whisper",
                 on_text: Optional[Callable[[str], None]] = None,
                 silence_ms: int = 700, max_utterance_s: float = 20.0,
                 threshold: float = 300.0, min_speech_ms: int = 300, **engine_kw) -> None:
        self.recognize: Recognizer = make_engine(engine, **engine_kw) if isinstance(engine, str) else engine
        self.on_text = on_text
        self.silence_frames = max(1, silence_ms // 20)
        self.max_frames = int(max_utterance_s * 50)
        self.min_frames = max(1, min_speech_ms // 20)
        self.threshold = threshold
        self.noise = threshold / 3   # running noise-floor estimate
        self._buf = bytearray()
        self._speech_frames = 0
        self._quiet_frames = 0
        self._in_speech = False
        self._jobs: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.enabled = True
        self.active = lambda: True          # the voice engine says whether a recognizer is configured
        self.utterances = 0
        self.busy = False
        threading.Thread(target=self._run, name="stt", daemon=True).start()

    # -- audio in --------------------------------------------------------------
    def feed(self, pcm: bytes) -> None:
        if not self.enabled or len(pcm) != FRAME_BYTES_PCM or not self.active():
            return
        level = _rms(pcm)
        gate = max(self.threshold, self.noise * 3)
        # adaptive noise floor, learned from non-speech frames only
        if level <= gate:
            self.noise = self.noise * 0.98 + level * 0.02
        if level > gate:
            if not self._in_speech:
                self._in_speech = True
                self._speech_frames = 0
            self._buf += pcm
            self._speech_frames += 1
            self._quiet_frames = 0
        elif self._in_speech:
            self._buf += pcm
            self._quiet_frames += 1
            if self._quiet_frames >= self.silence_frames:
                self._flush()
        if self._in_speech and len(self._buf) >= self.max_frames * FRAME_BYTES_PCM:
            self._flush()

    def _flush(self) -> None:
        if self._speech_frames >= self.min_frames:
            self._jobs.put(bytes(self._buf))
        self._buf.clear()
        self._in_speech = False
        self._speech_frames = self._quiet_frames = 0

    def end_of_call(self) -> None:
        if self._in_speech:
            self._flush()

    # -- worker ----------------------------------------------------------------
    def _run(self) -> None:
        while True:
            pcm = self._jobs.get()
            if pcm is None:
                return
            self.busy = True
            t0 = time.time()
            try:
                text = self.recognize(pcm)
            except Exception:
                log.exception("speech recognition failed")
                text = ""
            self.busy = False
            if text:
                self.utterances += 1
                log.info("heard (%.1fs audio, %.1fs): %s", len(pcm) / 16000, time.time() - t0, text)
                if self.on_text:
                    try:
                        self.on_text(text)
                    except Exception:
                        log.exception("on_text failed")


class ListeningAudio:
    """Wraps any audio backend; copies far-end frames to a Transcriber."""

    def __init__(self, inner, transcriber: Transcriber) -> None:
        self.inner = inner
        self.stt = transcriber
        self.name = f"{getattr(inner, 'name', 'audio')}+stt"

    def __getattr__(self, item):  # say(), stop_speaking(), mic_muted ... pass through
        return getattr(self.inner, item)

    def __setattr__(self, key, value):
        if key in ("inner", "stt", "name"):
            object.__setattr__(self, key, value)
        else:
            setattr(self.inner, key, value)

    def start(self) -> None:
        self.inner.start()

    def stop(self) -> None:
        self.stt.end_of_call()
        self.inner.stop()

    def read_frame(self):
        return self.inner.read_frame()

    def write_frame(self, pcm: bytes) -> None:
        self.stt.feed(pcm)
        self.inner.write_frame(pcm)

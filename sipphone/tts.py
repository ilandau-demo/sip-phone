"""Text-to-speech into the RTP stream.

Architecture
------------
The RTP sender already pulls one 20 ms PCM frame (320 bytes, 8 kHz mono int16)
from ``audio.read_frame()`` every 20 ms.  ``TTSAudio`` is an audio backend that
keeps a FIFO of such frames: ``say(text)`` synthesises on a worker thread,
resamples to 8 kHz, slices into 20 ms frames and appends them; ``read_frame``
pops one per tick (silence when the queue is empty).  Nothing in the SIP / RTP
code changes — the far end just hears the text as G.711 audio.

``TTSAudio`` can also wrap a real microphone backend: speech frames are mixed
on top of (or replace) the mic, and what the far end says still goes to the
speaker.

Engines (pick with ``engine=``):

* ``piper``   – offline neural TTS (``pip install piper-tts`` + a voice .onnx).
                Best quality / latency trade-off; runs on CPU.  Recommended.
* ``espeak``  – offline, robotic, zero setup where ``espeak-ng`` is installed.
* ``openai``  – OpenAI ``/v1/audio/speech`` (needs ``OPENAI_API_KEY``).
* ``elevenlabs`` – ElevenLabs API (needs ``ELEVENLABS_API_KEY``).
* any callable ``text -> (pcm16_bytes, sample_rate)`` for your own engine.
"""
from __future__ import annotations

import collections
import io
import logging
import os
import queue
import shutil
import struct
import subprocess
import threading
import wave
from typing import Callable, Deque, Optional, Tuple, Union

from .rtp import FRAME_BYTES_PCM, SAMPLE_RATE

log = logging.getLogger("sipphone.tts")

Synth = Callable[[str], Tuple[bytes, int]]  # text -> (int16 mono PCM, sample_rate)


# ----------------------------------------------------------------------------
# resampling / framing helpers
# ----------------------------------------------------------------------------
def resample_to_8k(pcm: bytes, rate: int) -> bytes:
    """Linear-interpolation resample of int16 mono PCM to 8 kHz."""
    if rate == SAMPLE_RATE or not pcm:
        return pcm
    try:
        import numpy as np
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        n_out = int(len(x) * SAMPLE_RATE / rate)
        if n_out == 0:
            return b""
        # crude anti-alias: box filter when downsampling by an integer-ish factor
        factor = rate / SAMPLE_RATE
        if factor > 1.5:
            k = int(factor)
            x = np.convolve(x, np.ones(k) / k, mode="same")
        src = np.linspace(0, len(x) - 1, n_out)
        y = np.interp(src, np.arange(len(x)), x)
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        n = len(pcm) // 2
        s = struct.unpack(f"<{n}h", pcm[: n * 2])
        n_out = int(n * SAMPLE_RATE / rate)
        out = []
        for i in range(n_out):
            pos = i * rate / SAMPLE_RATE
            j = int(pos)
            frac = pos - j
            a = s[min(j, n - 1)]
            b = s[min(j + 1, n - 1)]
            out.append(int(a + (b - a) * frac))
        return struct.pack(f"<{n_out}h", *out)


def apply_gain(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm
    try:
        import numpy as np
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        n = len(pcm) // 2
        return struct.pack(f"<{n}h", *(max(-32768, min(32767, int(v * gain)))
                                       for v in struct.unpack(f"<{n}h", pcm)))


def frames_of(pcm: bytes):
    """Yield 320-byte frames; the last one is zero-padded."""
    for i in range(0, len(pcm), FRAME_BYTES_PCM):
        chunk = pcm[i:i + FRAME_BYTES_PCM]
        if len(chunk) < FRAME_BYTES_PCM:
            chunk = chunk.ljust(FRAME_BYTES_PCM, b"\x00")
        yield chunk


def _wav_bytes_to_pcm(data: bytes) -> Tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")
    if ch == 2:  # downmix
        n = len(raw) // 4
        pairs = struct.unpack(f"<{n * 2}h", raw[: n * 4])
        raw = struct.pack(f"<{n}h", *((pairs[i] + pairs[i + 1]) // 2 for i in range(0, n * 2, 2)))
    return raw, rate


# ----------------------------------------------------------------------------
# engines
# ----------------------------------------------------------------------------
class PiperEngine:
    """Offline neural TTS. `pip install piper-tts`, then download a voice:
         python3 -m piper.download_voices en_US-lessac-medium
    """

    def __init__(self, model: str = "en_US-lessac-medium.onnx", length_scale: float = 1.0) -> None:
        from piper import PiperVoice  # type: ignore
        from piper.config import SynthesisConfig  # type: ignore
        self.voice = PiperVoice.load(model)
        self.cfg = SynthesisConfig(length_scale=length_scale)
        self.rate = self.voice.config.sample_rate

    def __call__(self, text: str) -> Tuple[bytes, int]:
        out = bytearray()
        for chunk in self.voice.synthesize(text, self.cfg):
            out += chunk.audio_int16_bytes
        return bytes(out), self.rate


class EspeakEngine:
    """Shells out to espeak-ng / espeak and reads WAV from stdout."""

    def __init__(self, voice: str = "en-us", speed: int = 160) -> None:
        self.bin = shutil.which("espeak-ng") or shutil.which("espeak")
        if not self.bin:
            raise RuntimeError("espeak-ng not found on PATH")
        self.voice, self.speed = voice, speed

    def __call__(self, text: str) -> Tuple[bytes, int]:
        res = subprocess.run([self.bin, "-v", self.voice, "-s", str(self.speed), "--stdout", text],
                             capture_output=True, check=True)
        return _wav_bytes_to_pcm(res.stdout)


class OpenAIEngine:
    """OpenAI text-to-speech; asks for PCM at 24 kHz and resamples."""

    def __init__(self, model: str = "gpt-4o-mini-tts", voice: str = "alloy", api_key: Optional[str] = None) -> None:
        self.model, self.voice = model, voice
        self.key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY not set")

    def __call__(self, text: str) -> Tuple[bytes, int]:
        import json
        import urllib.request
        body = json.dumps({"model": self.model, "voice": self.voice, "input": text,
                           "response_format": "pcm"}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/speech", data=body,
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), 24000


class ElevenLabsEngine:
    def __init__(self, voice_id: str = "21m00Tcm4TlvDq8ikWAM", api_key: Optional[str] = None,
                 model: str = "eleven_turbo_v2_5") -> None:
        self.voice_id, self.model = voice_id, model
        self.key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not self.key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")

    def __call__(self, text: str) -> Tuple[bytes, int]:
        import json
        import urllib.request
        # pcm_8000 is also offered, but µ-law 8k is the telephony preset; ask for 16k PCM
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=pcm_16000"
        req = urllib.request.Request(url, data=json.dumps({"text": text, "model_id": self.model}).encode(),
                                     headers={"xi-api-key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), 16000


def make_engine(name: str, **kw) -> Synth:
    name = (name or "auto").lower()
    if name == "auto":
        for candidate in ("piper", "espeak"):
            try:
                return make_engine(candidate, **kw)
            except Exception as e:
                log.debug("tts engine %s unavailable: %s", candidate, e)
        raise RuntimeError("no TTS engine available: pip install piper-tts (+ a voice) or install espeak-ng")
    if name == "piper":
        return PiperEngine(**{k: v for k, v in kw.items() if k in ("model", "length_scale") and v})
    if name == "espeak":
        return EspeakEngine(**{k: v for k, v in kw.items() if k in ("voice", "speed") and v})
    if name == "openai":
        return OpenAIEngine(**{k: v for k, v in kw.items() if k in ("model", "voice", "api_key") and v})
    if name == "elevenlabs":
        return ElevenLabsEngine(**{k: v for k, v in kw.items() if k in ("voice_id", "api_key", "model") and v})
    raise ValueError(f"unknown tts engine {name!r}")


# ----------------------------------------------------------------------------
# the audio backend
# ----------------------------------------------------------------------------
class TTSAudio:
    """Audio backend whose "microphone" is a queue of synthesised speech frames.

    mic:   optional real audio backend (SoundDeviceAudio).  When set, the far end
           hears the mic normally and TTS is mixed over it (``mode="mix"``) or
           replaces it while speech is playing (``mode="duck"``).  Incoming audio
           is always forwarded to ``mic.write_frame`` so you still hear the call.
    """
    name = "tts"

    def __init__(self, engine: Union[str, Synth] = "auto", mic=None, mode: str = "duck",
                 gain: float = 0.9, **engine_kw) -> None:
        self.synth: Synth = make_engine(engine, **engine_kw) if isinstance(engine, str) else engine
        self.mic = mic
        self.mode = mode
        self.gain = gain
        self._frames: Deque[bytes] = collections.deque()
        self._jobs: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="tts-synth", daemon=True)
        self._worker.start()
        self.spoken = 0  # frames sent so far (for tests / status)
        self.mic_muted = False

    # -- public ---------------------------------------------------------------
    def say(self, text: str) -> None:
        """Queue text; it is synthesised in the background and streamed out."""
        text = text.strip()
        if text:
            self._jobs.put(text)

    def stop_speaking(self) -> None:
        """Flush anything queued and currently playing."""
        with self._jobs.mutex:
            self._jobs.queue.clear()
        with self._lock:
            self._frames.clear()

    @property
    def speaking(self) -> bool:
        with self._lock:
            return bool(self._frames) or not self._jobs.empty()

    def queued_seconds(self) -> float:
        with self._lock:
            return len(self._frames) * 0.02

    # -- audio backend interface ----------------------------------------------
    def start(self) -> None:
        if self.mic:
            self.mic.start()

    def stop(self) -> None:
        self.stop_speaking()
        if self.mic:
            self.mic.stop()

    def read_frame(self) -> Optional[bytes]:
        with self._lock:
            speech = self._frames.popleft() if self._frames else None
        mic = self.mic.read_frame() if self.mic else None
        if self.mic_muted:
            mic = None
        if speech is None:
            return mic
        self.spoken += 1
        if mic is None or self.mode == "duck":
            return speech
        return _mix(speech, mic)

    def write_frame(self, pcm: bytes) -> None:
        if self.mic:
            self.mic.write_frame(pcm)

    # -- worker ---------------------------------------------------------------
    def _run(self) -> None:
        while True:
            text = self._jobs.get()
            if text is None:
                return
            try:
                pcm, rate = self.synth(text)
                pcm = apply_gain(resample_to_8k(pcm, rate), self.gain)
                frames = list(frames_of(pcm))
                frames.append(b"\x00" * FRAME_BYTES_PCM)  # small gap between utterances
                with self._lock:
                    self._frames.extend(frames)
                log.info("tts: queued %.1fs for %r", len(frames) * 0.02, text[:40])
            except Exception:
                log.exception("tts synthesis failed for %r", text[:40])


def _mix(a: bytes, b: bytes) -> bytes:
    try:
        import numpy as np
        x = np.frombuffer(a, dtype="<i2").astype(np.int32) + np.frombuffer(b, dtype="<i2").astype(np.int32)
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        n = len(a) // 2
        return struct.pack(f"<{n}h", *(max(-32768, min(32767, p + q)) for p, q in
                                       zip(struct.unpack(f"<{n}h", a), struct.unpack(f"<{n}h", b))))

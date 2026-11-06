"""PCM helpers shared by providers and the engine: WAV in/out, resampling,
gain, 20 ms framing, and local playback for "Test voice"."""
from __future__ import annotations

import io
import struct
import wave
from typing import Tuple

from ..rtp import FRAME_BYTES_PCM, SAMPLE_RATE


def resample(pcm: bytes, rate: int, target: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resample of int16 mono PCM."""
    if rate == target or not pcm:
        return pcm
    try:
        import numpy as np
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        n_out = int(len(x) * target / rate)
        if n_out == 0:
            return b""
        factor = rate / target
        if factor > 1.5:
            k = int(factor)
            x = np.convolve(x, np.ones(k) / k, mode="same")
        src = np.linspace(0, len(x) - 1, n_out)
        y = np.interp(src, np.arange(len(x)), x)
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        n = len(pcm) // 2
        s = struct.unpack(f"<{n}h", pcm[: n * 2])
        n_out = int(n * target / rate)
        out = []
        for i in range(n_out):
            pos = i * rate / target
            j = int(pos)
            frac = pos - j
            a = s[min(j, n - 1)]
            b = s[min(j + 1, n - 1)]
            out.append(int(a + (b - a) * frac))
        return struct.pack(f"<{n_out}h", *out)


def resample_to_8k(pcm: bytes, rate: int) -> bytes:
    return resample(pcm, rate, SAMPLE_RATE)


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


def wav_to_pcm(data: bytes) -> Tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")
    if ch == 2:
        n = len(raw) // 4
        pairs = struct.unpack(f"<{n * 2}h", raw[: n * 4])
        raw = struct.pack(f"<{n}h", *((pairs[i] + pairs[i + 1]) // 2 for i in range(0, n * 2, 2)))
    return raw, rate


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return buf.getvalue()


def duration(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate if rate else 0.0


def mix(a: bytes, b: bytes) -> bytes:
    try:
        import numpy as np
        x = np.frombuffer(a, dtype="<i2").astype(np.int32) + np.frombuffer(b, dtype="<i2").astype(np.int32)
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        n = len(a) // 2
        return struct.pack(f"<{n}h", *(max(-32768, min(32767, p + q)) for p, q in
                                       zip(struct.unpack(f"<{n}h", a), struct.unpack(f"<{n}h", b))))


def play_locally(pcm: bytes, rate: int, device=None) -> bool:
    """Play through the speakers (for voice tests). Returns False without sounddevice."""
    try:
        import numpy as np
        import sounddevice as sd
        sd.play(np.frombuffer(pcm, dtype="<i2"), rate, device=device)
        return True
    except Exception:
        return False

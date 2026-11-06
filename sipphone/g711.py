"""G.711 µ-law (PCMU) and A-law (PCMA) codecs.

Pure-Python table implementation (the stdlib `audioop` module was removed in
Python 3.13). Uses numpy for the bulk conversions when it is available.
"""
from __future__ import annotations

import struct

try:
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

BIAS = 0x84
CLIP = 32635


def _linear_to_ulaw_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sample = -sample
        sign = 0x80
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_to_linear_sample(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample -= BIAS
    return -sample if sign else sample


_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _linear_to_alaw_sample(sample: int) -> int:
    """Classic Sun g711.c linear2alaw (13-bit input via >>3)."""
    pcm_val = sample >> 3
    if pcm_val >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm_val = -pcm_val - 1
    seg = 8
    for i, end in enumerate(_SEG_AEND):
        if pcm_val <= end:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm_val >> 1) & 0x0F
    else:
        aval |= (pcm_val >> seg) & 0x0F
    return aval ^ mask


def _alaw_to_linear_sample(a: int) -> int:
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if a & 0x80 else -t


# full decode tables (256 entries) and 14-bit-indexed encode tables
ULAW_DECODE = [_ulaw_to_linear_sample(i) for i in range(256)]
ALAW_DECODE = [_alaw_to_linear_sample(i) for i in range(256)]
# encode by top 14 bits: index = (sample >> 2) & 0x3FFF for signed 16-bit
ULAW_ENCODE = [_linear_to_ulaw_sample(struct.unpack("<h", struct.pack("<H", (i << 2) & 0xFFFF))[0])
               for i in range(16384)]
ALAW_ENCODE = [_linear_to_alaw_sample(struct.unpack("<h", struct.pack("<H", (i << 2) & 0xFFFF))[0])
               for i in range(16384)]

if _np is not None:
    _ULAW_DECODE_NP = _np.array(ULAW_DECODE, dtype=_np.int16)
    _ALAW_DECODE_NP = _np.array(ALAW_DECODE, dtype=_np.int16)
    _ULAW_ENCODE_NP = _np.array(ULAW_ENCODE, dtype=_np.uint8)
    _ALAW_ENCODE_NP = _np.array(ALAW_ENCODE, dtype=_np.uint8)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    if _np is not None:
        s = _np.frombuffer(pcm, dtype="<i2")
        idx = (s.astype(_np.int32) >> 2) & 0x3FFF
        return _ULAW_ENCODE_NP[idx].tobytes()
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return bytes(ULAW_ENCODE[(s >> 2) & 0x3FFF] for s in samples)


def ulaw_to_pcm16(data: bytes) -> bytes:
    if _np is not None:
        return _ULAW_DECODE_NP[_np.frombuffer(data, dtype=_np.uint8)].astype("<i2").tobytes()
    return struct.pack(f"<{len(data)}h", *(ULAW_DECODE[b] for b in data))


def pcm16_to_alaw(pcm: bytes) -> bytes:
    if _np is not None:
        s = _np.frombuffer(pcm, dtype="<i2")
        idx = (s.astype(_np.int32) >> 2) & 0x3FFF
        return _ALAW_ENCODE_NP[idx].tobytes()
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return bytes(ALAW_ENCODE[(s >> 2) & 0x3FFF] for s in samples)


def alaw_to_pcm16(data: bytes) -> bytes:
    if _np is not None:
        return _ALAW_DECODE_NP[_np.frombuffer(data, dtype=_np.uint8)].astype("<i2").tobytes()
    return struct.pack(f"<{len(data)}h", *(ALAW_DECODE[b] for b in data))


ENCODERS = {"PCMU": pcm16_to_ulaw, "PCMA": pcm16_to_alaw}
DECODERS = {"PCMU": ulaw_to_pcm16, "PCMA": alaw_to_pcm16}

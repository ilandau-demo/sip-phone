from ..provider_interface import Availability, ProviderInfo, STTProvider, module_available, module_version  # noqa: F401


def upsample_8k_to_16k(pcm: bytes):
    import numpy as np
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return np.interp(np.arange(0, len(audio), 0.5), np.arange(len(audio)), audio).astype(np.float32)

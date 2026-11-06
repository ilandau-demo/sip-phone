"""The modular voice engine: providers for TTS and STT behind one interface."""
from .manager import VoiceEngine                                     # noqa: F401
from .provider_interface import TTSProvider, STTProvider, Voice, Availability, ProviderInfo   # noqa: F401
from .registry import register, Registry                              # noqa: F401

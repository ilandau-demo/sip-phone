"""The contract every voice provider implements.

A provider is a small adapter around one engine (Piper, a Windows voice, an
HTTP endpoint, …). The softphone, the presence engine and the extension never
import an engine directly: they ask the VoiceEngine, which asks a provider.

TTS providers return 16-bit mono PCM plus its sample rate; the engine resamples
to the telephone rate. STT providers take 8 kHz 16-bit mono PCM and return text.
Capabilities are declared, not assumed: a provider without pitch control or
streaming still works, the engine just doesn't ask for them.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class Voice:
    id: str
    name: str
    language: str = ""
    gender: str = ""
    description: str = ""
    local: bool = True                     # processed on this computer

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "language": self.language, "gender": self.gender,
                "description": self.description, "local": self.local}


@dataclass
class Availability:
    ok: bool
    state: str              # available | not-configured | model-required | unavailable
    reason: str = ""        # plain words: what is missing or wrong
    action: str = ""        # what to do about it (install, download, configure)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "state": self.state, "reason": self.reason, "action": self.action}


@dataclass
class ProviderInfo:
    """Static description of a provider type, for the settings page."""
    type: str
    title: str
    kind: str               # local | remote | native
    description: str
    pip: str = ""           # package to install, if any
    fields: List[dict] = field(default_factory=list)   # [{key, label, kind(text|secret|path|select|number), default, options, help}]
    stt: bool = False       # True for speech-to-text providers


class CancelledError(Exception):
    pass


class TTSProvider:
    """Subclasses set `info` (ProviderInfo) and implement `_synth`."""
    info: ProviderInfo
    capabilities: Dict[str, bool] = {"streaming": False, "rate": True, "pitch": False, "volume": True,
                                     "voices": True, "install": False}
    version: str = ""

    def __init__(self, config: Optional[dict] = None, models_dir: str = "") -> None:
        self.config = dict(config or {})
        self.models_dir = models_dir
        self._loaded = False
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def availability(self) -> Availability:
        """Cheap check: no model loading, no network. Override."""
        return Availability(True, "available")

    def load(self) -> None:
        """Load models or connections. Called once, lazily, on the engine's worker thread."""
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    # -- voices --------------------------------------------------------------
    def voices(self) -> List[Voice]:
        return []

    def default_voice(self) -> str:
        v = self.voices()
        return v[0].id if v else ""

    # -- speech --------------------------------------------------------------
    def synthesize(self, text: str, voice: str = "", rate: float = 1.0, pitch: float = 1.0,
                   volume: float = 1.0, cancel: Optional[threading.Event] = None) -> Tuple[bytes, int]:
        with self._lock:
            if not self._loaded:
                self.load()
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            return self._synth(text, voice or self.default_voice(), rate, pitch, volume, cancel)

    def _synth(self, text: str, voice: str, rate: float, pitch: float, volume: float,
               cancel: Optional[threading.Event]) -> Tuple[bytes, int]:
        raise NotImplementedError

    def health(self) -> dict:
        a = self.availability()
        return {"loaded": self._loaded, **a.to_dict()}


class STTProvider:
    info: ProviderInfo
    capabilities: Dict[str, bool] = {"streaming": False, "languages": False}
    version: str = ""

    def __init__(self, config: Optional[dict] = None, models_dir: str = "") -> None:
        self.config = dict(config or {})
        self.models_dir = models_dir
        self._loaded = False
        self._lock = threading.Lock()

    def availability(self) -> Availability:
        return Availability(True, "available")

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def transcribe(self, pcm8k: bytes) -> str:
        with self._lock:
            if not self._loaded:
                self.load()
            return self._recognize(pcm8k)

    def _recognize(self, pcm8k: bytes) -> str:
        raise NotImplementedError

    def health(self) -> dict:
        return {"loaded": self._loaded, **self.availability().to_dict()}


def module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def module_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return ""

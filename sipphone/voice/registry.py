"""Provider registry: which provider types exist, and the user's configured
instances (voice.json). Third-party types can be registered at import time
with `register(cls)`; they get no privileges beyond the provider interface."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Type

from .provider_interface import ProviderInfo, STTProvider, TTSProvider

log = logging.getLogger("sipphone.voice")

TTS_TYPES: Dict[str, Type[TTSProvider]] = {}
STT_TYPES: Dict[str, Type[STTProvider]] = {}


def register(cls) -> None:
    info: ProviderInfo = cls.info
    (STT_TYPES if info.stt else TTS_TYPES)[info.type] = cls


def _builtin() -> None:
    from .tts.piper import PiperTTS
    from .tts.kokoro import KokoroTTS
    from .tts.native import NativeTTS
    from .tts.remote import OpenAICompatibleTTS, ElevenLabsTTS
    from .tts.custom import CommandTTS
    from .stt.faster_whisper import FasterWhisperSTT
    from .stt.whisper import WhisperSTT
    from .stt.vosk import VoskSTT
    from .stt.sherpa_onnx import SherpaOnnxSTT
    from .stt.custom import CommandSTT
    for c in (PiperTTS, KokoroTTS, NativeTTS, OpenAICompatibleTTS, ElevenLabsTTS, CommandTTS,
              FasterWhisperSTT, WhisperSTT, VoskSTT, SherpaOnnxSTT, CommandSTT):
        register(c)


_builtin()

SECRET_FIELDS = ("api_key", "token", "password")


class Registry:
    """Configured provider instances + defaults, persisted in voice.json."""

    def __init__(self, path: str, models_dir: str = "") -> None:
        self.path = path
        self.models_dir = models_dir
        self.providers: List[dict] = []            # [{id, type, name, enabled, config}]
        self.defaults: dict = {"tts": "", "tts_fallback": "", "stt": "", "voice": "", "rate": 1.0, "pitch": 1.0,
                               "volume": 0.9, "streaming": True, "assignments": {}}
        self._instances: Dict[str, object] = {}
        self._lock = threading.RLock()
        self.load()

    # -- storage ---------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.providers = [p for p in data.get("providers", []) if isinstance(p, dict) and p.get("id") and p.get("type")]
            self.defaults.update({k: v for k, v in data.get("defaults", {}).items() if k in self.defaults})
            from ..authentication.credential_store import reveal
            for p in self.providers:
                for k in SECRET_FIELDS:
                    if k in p.get("config", {}):
                        p["config"][k] = reveal(p["config"][k])
        except (OSError, ValueError) as e:
            log.warning("voice.json unreadable (%s); starting empty", e)

    def save(self) -> None:
        if not self.path:
            return
        from ..authentication.credential_store import protect, restrict_file
        out = {"providers": [], "defaults": self.defaults}
        for p in self.providers:
            q = dict(p, config=dict(p.get("config", {})))
            for k in SECRET_FIELDS:
                if q["config"].get(k):
                    q["config"][k] = protect(q["config"][k])
            out["providers"].append(q)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        os.replace(self.path + ".tmp", self.path)
        restrict_file(self.path)

    # -- types ------------------------------------------------------------------
    @staticmethod
    def types() -> List[dict]:
        out = []
        for t, cls in list(TTS_TYPES.items()) + list(STT_TYPES.items()):
            i = cls.info
            out.append({"type": t, "title": i.title, "kind": i.kind, "description": i.description, "pip": i.pip,
                        "fields": i.fields, "stt": i.stt, "capabilities": dict(getattr(cls, "capabilities", {}))})
        return out

    # -- instances --------------------------------------------------------------
    def get(self, pid: str) -> Optional[dict]:
        return next((p for p in self.providers if p["id"] == pid), None)

    def instance(self, pid: str):
        with self._lock:
            if pid in self._instances:
                return self._instances[pid]
            p = self.get(pid)
            if not p:
                return None
            cls = TTS_TYPES.get(p["type"]) or STT_TYPES.get(p["type"])
            if cls is None:
                return None
            inst = cls(p.get("config", {}), models_dir=self.models_dir)
            self._instances[pid] = inst
            return inst

    def add(self, ptype: str, name: str = "", config: Optional[dict] = None, pid: str = "", enabled: bool = True) -> dict:
        cls = TTS_TYPES.get(ptype) or STT_TYPES.get(ptype)
        if cls is None:
            raise ValueError(f"unknown provider type {ptype!r}")
        cfg = {f["key"]: f.get("default", "") for f in cls.info.fields}
        cfg.update({k: v for k, v in (config or {}).items() if isinstance(v, (str, int, float, bool))})
        pid = pid or f"{ptype}-{int(time.time() * 1000) % 100000}"
        with self._lock:
            if self.get(pid):
                raise ValueError(f"provider {pid!r} already exists")
            p = {"id": pid, "type": ptype, "name": name or cls.info.title, "enabled": bool(enabled), "config": cfg,
                 "stt": bool(cls.info.stt)}
            self.providers.append(p)
            self.save()
        return p

    def update(self, pid: str, name: Optional[str] = None, config: Optional[dict] = None, enabled: Optional[bool] = None) -> dict:
        with self._lock:
            p = self.get(pid)
            if not p:
                raise ValueError("no such provider")
            if name is not None:
                p["name"] = name
            if enabled is not None:
                p["enabled"] = bool(enabled)
            if config:
                for k, v in config.items():
                    if k in SECRET_FIELDS and v == "":
                        continue                                 # blank secret = keep
                    p["config"][k] = v
                self._drop_instance(pid)
            self.save()
            return p

    def remove(self, pid: str) -> None:
        with self._lock:
            self.providers = [p for p in self.providers if p["id"] != pid]
            self._drop_instance(pid)
            for k in ("tts", "tts_fallback", "stt"):
                if self.defaults.get(k) == pid:
                    self.defaults[k] = ""
            self.defaults["assignments"] = {k: v for k, v in self.defaults.get("assignments", {}).items() if v != pid}
            self.save()

    def _drop_instance(self, pid: str) -> None:
        inst = self._instances.pop(pid, None)
        if inst is not None:
            try:
                inst.unload()
            except Exception:
                pass

    def set_defaults(self, **kw) -> dict:
        with self._lock:
            for k, v in kw.items():
                if k in self.defaults and v is not None:
                    self.defaults[k] = v
            self.save()
            return dict(self.defaults)

    def tts_providers(self) -> List[dict]:
        return [p for p in self.providers if not p.get("stt")]

    def stt_providers(self) -> List[dict]:
        return [p for p in self.providers if p.get("stt")]

"""VoiceEngine: the one door to text-to-speech and speech-to-text.

    softphone / presence / API  →  VoiceEngine  →  Registry  →  provider

* speak() never raises into the caller: a provider failure logs, tries the
  fallback provider, and finally returns silence — calls and presence go on.
* synthesis runs on the caller's worker (TTSAudio already has one); test_voice()
  for the settings page runs on a thread of its own.
* switching the default provider, voice or rate takes effect on the next sentence.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .audio_output import apply_gain, pcm_to_wav, resample_to_8k
from .model_manager import ModelManager
from .provider_interface import CancelledError, STTProvider, TTSProvider
from .registry import Registry

log = logging.getLogger("sipphone.voice")


class VoiceEngine:
    def __init__(self, config_dir: str, models_dir: str = "") -> None:
        self.config_dir = config_dir
        self.models_dir = models_dir or os.path.join(config_dir, "models")
        os.makedirs(self.models_dir, exist_ok=True)
        self.registry = Registry(os.path.join(config_dir, "voice.json"), models_dir=self.models_dir)
        self.models = ModelManager(self.models_dir)
        self.cancel = threading.Event()
        self.last_error = ""
        self.stats = {"synth": 0, "synth_failed": 0, "fallbacks": 0, "recognized": 0, "recognize_failed": 0}
        self._lock = threading.Lock()

    # -- migration from [tts]/[stt] config sections ------------------------------------
    def migrate_from_config(self, tts: dict, stt: dict) -> bool:
        """Create providers for a pre-2.4 config once. Returns True if anything changed."""
        if self.registry.providers:
            return False
        changed = False
        eng = (tts.get("engine") or "").strip().lower()
        if eng == "piper":
            cfg = {}
            model = (tts.get("model") or "").strip()
            if model and os.path.isabs(model):
                cfg["voices_dir"] = os.path.dirname(model)
                cfg["voice"] = os.path.splitext(os.path.basename(model))[0]
            elif model:
                cfg["voice"] = model[:-5] if model.endswith(".onnx") else model
            p = self.registry.add("piper", "Piper", cfg, pid="piper")
            self.registry.set_defaults(tts=p["id"], voice=cfg.get("voice", ""))
            changed = True
        elif eng in ("openai", "elevenlabs"):
            p = self.registry.add("openai-compatible" if eng == "openai" else "elevenlabs",
                                  config={"model": tts.get("model") or "", "voice": tts.get("voice") or ""} if eng == "openai" else {"voice": tts.get("voice") or ""})
            self.registry.set_defaults(tts=p["id"])
            changed = True
        elif eng == "espeak":
            p = self.registry.add("native", "System voices", pid="native")
            self.registry.set_defaults(tts=p["id"])
            changed = True
        if not self.registry.get("native"):
            self.registry.add("native", "System voices", pid="native")
            changed = True
        if not self.registry.defaults.get("tts_fallback"):
            self.registry.set_defaults(tts_fallback="native")
        seng = (stt.get("engine") or "").strip().lower()
        if seng in ("whisper", "faster-whisper"):
            p = self.registry.add("faster-whisper", "Faster-Whisper", {"model": stt.get("model") or "base.en"}, pid="faster-whisper")
            self.registry.set_defaults(stt=p["id"]); changed = True
        elif seng == "vosk":
            p = self.registry.add("vosk", "Vosk", {"model": stt.get("model") or ""}, pid="vosk")
            self.registry.set_defaults(stt=p["id"]); changed = True
        if tts.get("gain"):
            try:
                self.registry.set_defaults(volume=float(tts["gain"]))
            except ValueError:
                pass
        return changed

    # -- providers ------------------------------------------------------------------------------
    def tts_provider(self, pid: str = "", purpose: str = "") -> Optional[TTSProvider]:
        d = self.registry.defaults
        pid = pid or d.get("assignments", {}).get(purpose, "") or d.get("tts", "")
        p = self.registry.get(pid)
        if not p or not p.get("enabled", True) or p.get("stt"):
            return None
        inst = self.registry.instance(pid)
        return inst if isinstance(inst, TTSProvider) else None

    def stt_provider(self, pid: str = "") -> Optional[STTProvider]:
        pid = pid or self.registry.defaults.get("stt", "")
        p = self.registry.get(pid)
        if not p or not p.get("enabled", True) or not p.get("stt"):
            return None
        inst = self.registry.instance(pid)
        return inst if isinstance(inst, STTProvider) else None

    @property
    def tts_enabled(self) -> bool:
        return self.tts_provider() is not None

    @property
    def stt_enabled(self) -> bool:
        return self.stt_provider() is not None

    # -- speech -------------------------------------------------------------------------------------
    def synthesize(self, text: str, purpose: str = "call", voice: str = "", provider: str = "",
                   rate: Optional[float] = None, pitch: Optional[float] = None, volume: Optional[float] = None,
                   cancel: Optional[threading.Event] = None) -> Tuple[bytes, int]:
        """(pcm16, sample_rate) from the chosen provider, else the fallback, else silence."""
        d = self.registry.defaults
        rate = d["rate"] if rate is None else rate
        pitch = d["pitch"] if pitch is None else pitch
        volume = d["volume"] if volume is None else volume
        voice = voice or (d.get("voice") if not provider and not d.get("assignments", {}).get(purpose) else "")
        chain: List[str] = []
        first = provider or d.get("assignments", {}).get(purpose, "") or d.get("tts", "")
        if first:
            chain.append(first)
        if d.get("tts_fallback") and d["tts_fallback"] not in chain:
            chain.append(d["tts_fallback"])
        cancel = cancel or self.cancel
        for i, pid in enumerate(chain):
            prov = self.tts_provider(pid)
            if prov is None:
                continue
            v = voice if (voice and i == 0 and any(x.id == voice for x in prov.voices())) else ""
            try:
                t0 = time.time()
                pcm, sr = prov.synthesize(text, v, rate, pitch, volume, cancel)
                self.stats["synth"] += 1
                if i > 0:
                    self.stats["fallbacks"] += 1
                log.info("voice: %s spoke %d chars in %.2fs", pid, len(text), time.time() - t0)
                if i == 0:
                    self.last_error = ""
                else:
                    self.last_error += f" (fell back to {pid})"
                return pcm, sr
            except CancelledError:
                return b"", 8000
            except Exception as e:
                self.stats["synth_failed"] += 1
                self.last_error = f"{pid}: {e}"
                log.warning("voice provider %s failed: %s", pid, e)
        return b"", 8000

    def synth_8k(self, text: str, purpose: str = "call") -> Tuple[bytes, int]:
        """What the telephone path wants: 8 kHz PCM (volume already applied)."""
        pcm, sr = self.synthesize(text, purpose)
        return resample_to_8k(pcm, sr), 8000

    def recognize(self, pcm8k: bytes) -> str:
        prov = self.stt_provider()
        if prov is None:
            return ""
        try:
            text = prov.transcribe(pcm8k)
            self.stats["recognized"] += 1
            return text
        except Exception as e:
            self.stats["recognize_failed"] += 1
            self.last_error = f"{self.registry.defaults.get('stt')}: {e}"
            log.warning("speech recognition failed: %s", e)
            return ""

    def cancel_all(self) -> None:
        self.cancel.set()
        self.cancel = threading.Event()

    def test_voice(self, provider: str, voice: str = "", text: str = "", rate: Optional[float] = None,
                   volume: Optional[float] = None) -> Tuple[bytes, str]:
        """WAV bytes for the settings page's Test button (the browser plays it)."""
        text = text or "This is a test of the selected voice."
        prov = self.tts_provider(provider)
        if prov is None:
            return b"", "That provider is not available."
        a = prov.availability()
        if not a.ok:
            return b"", a.reason + (" " + a.action if a.action else "")
        try:
            d = self.registry.defaults
            pcm, sr = prov.synthesize(text, voice, d["rate"] if rate is None else rate, d["pitch"],
                                      d["volume"] if volume is None else volume)
            return pcm_to_wav(pcm, sr), ""
        except Exception as e:
            return b"", f"{e}"

    # -- reporting -------------------------------------------------------------------------------------
    def status(self) -> dict:
        d = self.registry.defaults
        provs = []
        for p in self.registry.providers:
            inst = self.registry.instance(p["id"])
            avail = inst.availability().to_dict() if inst else {"ok": False, "state": "unavailable", "reason": "unknown provider type"}
            voices = []
            if inst and not p.get("stt") and avail["ok"]:
                try:
                    voices = [v.to_dict() for v in inst.voices()]
                except Exception as e:
                    log.debug("voices() failed for %s: %s", p["id"], e)
            info = inst.info if inst else None
            provs.append({"id": p["id"], "type": p["type"], "name": p["name"], "enabled": p.get("enabled", True), "stt": p.get("stt", False),
                          "kind": info.kind if info else "", "title": info.title if info else p["type"],
                          "version": getattr(inst, "version", "") if inst else "", "availability": avail, "voices": voices,
                          "capabilities": dict(getattr(inst, "capabilities", {})) if inst else {},
                          "config": {k: ("" if k in ("api_key", "token", "password") else v) for k, v in p.get("config", {}).items()},
                          "has_secret": any(p.get("config", {}).get(k) for k in ("api_key", "token", "password")),
                          "loaded": bool(getattr(inst, "_loaded", False)) if inst else False,
                          "is_default": p["id"] in (d.get("tts"), d.get("stt")), "is_fallback": p["id"] == d.get("tts_fallback")})
        return {"providers": provs, "defaults": dict(d), "types": Registry.types(), "models_dir": self.models_dir,
                "tts_enabled": self.tts_enabled, "stt_enabled": self.stt_enabled, "last_error": self.last_error,
                "stats": dict(self.stats), "purposes": {"call": "Calls (spoken to the caller)", "notifications": "Voice notifications", "assistant": "Voice assistant"}}

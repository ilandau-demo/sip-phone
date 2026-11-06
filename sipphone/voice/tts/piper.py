"""Piper: offline neural voices (.onnx + .onnx.json)."""
from __future__ import annotations

import glob
import os
import threading
from typing import List, Optional, Tuple

from .base import Availability, ProviderInfo, TTSProvider, Voice, CancelledError, module_available, module_version


class PiperTTS(TTSProvider):
    info = ProviderInfo("piper", "Piper (offline neural)", "local",
                        "Natural voices that run on this computer. Needs the piper-tts package and at least one voice.",
                        pip="piper-tts",
                        fields=[{"key": "voices_dir", "label": "Voice folder", "kind": "path", "default": "",
                                 "help": "Folder with .onnx voices (blank = the models folder). Add your own Piper-compatible voices here."},
                                {"key": "voice", "label": "Default voice", "kind": "voice", "default": ""}])
    capabilities = {"streaming": True, "rate": True, "pitch": False, "volume": True, "voices": True, "install": True}

    def __init__(self, config=None, models_dir: str = "") -> None:
        super().__init__(config, models_dir)
        self._voices: dict = {}

    def _dirs(self) -> List[str]:
        out = []
        parent = os.path.dirname(self.models_dir.rstrip(os.sep)) if self.models_dir else ""   # the config folder (older installs kept voices there)
        for d in (self.config.get("voices_dir", ""), self.models_dir, parent):
            if d and os.path.isdir(d) and d not in out:
                out.append(d)
        return out

    def _scan(self) -> dict:
        found = {}
        for d in self._dirs():
            for f in sorted(glob.glob(os.path.join(d, "**", "*.onnx"), recursive=True)):
                if os.path.isfile(f + ".json"):
                    found[os.path.splitext(os.path.basename(f))[0]] = f
        return found

    def availability(self) -> Availability:
        if not module_available("piper"):
            return Availability(False, "unavailable", "The piper-tts package is not installed.", "pip install piper-tts")
        if not self._scan():
            return Availability(False, "model-required", "No Piper voice found.", "Download a voice from the model manager, or point Voice folder at your own .onnx files.")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("piper-tts")

    def voices(self) -> List[Voice]:
        out = []
        for vid in self._scan():
            parts = vid.split("-")
            lang = parts[0] if parts and "_" in parts[0] else ""
            out.append(Voice(vid, vid.replace("_", " ").replace("-", " · "), language=lang, description="Piper voice"))
        return out

    def default_voice(self) -> str:
        want = self.config.get("voice") or ""
        ids = list(self._scan())
        return want if want in ids else (ids[0] if ids else "")

    def load(self) -> None:
        self._loaded = True            # voices load on first use, per voice

    def _voice(self, vid: str):
        if vid not in self._voices:
            from piper import PiperVoice  # type: ignore
            path = self._scan().get(vid)
            if not path:
                raise RuntimeError(f"Piper voice {vid!r} not found")
            self._voices[vid] = PiperVoice.load(path)
        return self._voices[vid]

    def _synth(self, text: str, voice: str, rate: float, pitch: float, volume: float,
               cancel: Optional[threading.Event]) -> Tuple[bytes, int]:
        from piper.config import SynthesisConfig  # type: ignore
        v = self._voice(voice)
        cfg = SynthesisConfig(length_scale=1.0 / max(0.25, min(4.0, rate)), volume=max(0.0, min(2.0, volume)))
        out = bytearray()
        for chunk in v.synthesize(text, cfg):
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            out += chunk.audio_int16_bytes
        return bytes(out), v.config.sample_rate

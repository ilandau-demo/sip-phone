"""Vosk: light offline recognition from a model folder."""
from __future__ import annotations

import os

from .base import Availability, ProviderInfo, STTProvider, module_available, module_version


class VoskSTT(STTProvider):
    info = ProviderInfo("vosk", "Vosk (offline, light)", "local",
                        "Fast and small; less accurate than Whisper. Needs a model folder.",
                        pip="vosk", stt=True,
                        fields=[{"key": "model", "label": "Model folder", "kind": "path", "default": "", "help": "blank = models/vosk/<first model>"}])

    def _model_dir(self) -> str:
        m = self.config.get("model") or ""
        if m and os.path.isdir(m):
            return m
        base = os.path.join(self.models_dir, "vosk") if self.models_dir else ""
        if base and os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                if os.path.isdir(os.path.join(base, d)):
                    return os.path.join(base, d)
        return ""

    def availability(self) -> Availability:
        if not module_available("vosk"):
            return Availability(False, "unavailable", "The vosk package is not installed.", "pip install vosk")
        if not self._model_dir():
            return Availability(False, "model-required", "No Vosk model folder.", "Download a Vosk model from the model manager.")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("vosk")

    def load(self) -> None:
        import vosk  # type: ignore
        vosk.SetLogLevel(-1)
        self._vosk = vosk
        self._m = vosk.Model(self._model_dir())
        self._loaded = True

    def _recognize(self, pcm8k: bytes) -> str:
        import json
        rec = self._vosk.KaldiRecognizer(self._m, 8000)
        rec.AcceptWaveform(pcm8k)
        return json.loads(rec.FinalResult()).get("text", "").strip()

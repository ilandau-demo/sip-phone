"""Kokoro (kokoro-onnx): a small, high-quality offline model with many voices."""
from __future__ import annotations

import os
import threading
from typing import List, Optional, Tuple

from .base import Availability, ProviderInfo, TTSProvider, Voice, CancelledError, module_available, module_version

VOICES = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis"]


class KokoroTTS(TTSProvider):
    info = ProviderInfo("kokoro", "Kokoro (offline neural)", "local",
                        "An 82M-parameter model with natural voices, running on this computer. Needs the kokoro-onnx package and its two model files.",
                        pip="kokoro-onnx",
                        fields=[{"key": "model", "label": "Model file", "kind": "path", "default": "", "help": "kokoro-v1.0.onnx (blank = models folder)"},
                                {"key": "voices_file", "label": "Voices file", "kind": "path", "default": "", "help": "voices-v1.0.bin (blank = models folder)"},
                                {"key": "voice", "label": "Default voice", "kind": "voice", "default": "af_heart"}])
    capabilities = {"streaming": False, "rate": True, "pitch": False, "volume": True, "voices": True, "install": True}

    def _files(self):
        md = os.path.join(self.models_dir, "kokoro") if self.models_dir else ""
        model = self.config.get("model") or (os.path.join(md, "kokoro-v1.0.onnx") if md else "")
        voices = self.config.get("voices_file") or (os.path.join(md, "voices-v1.0.bin") if md else "")
        return model, voices

    def availability(self) -> Availability:
        if not module_available("kokoro_onnx"):
            return Availability(False, "unavailable", "The kokoro-onnx package is not installed.", "pip install kokoro-onnx soundfile")
        model, voices = self._files()
        if not (model and os.path.isfile(model) and voices and os.path.isfile(voices)):
            return Availability(False, "model-required", "Kokoro model files not found.", "Download Kokoro from the model manager.")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("kokoro-onnx")

    def voices(self) -> List[Voice]:
        return [Voice(v, v, language="en-US" if v.startswith("a") else "en-GB", gender="female" if v[1] == "f" else "male") for v in VOICES]

    def default_voice(self) -> str:
        return self.config.get("voice") or "af_heart"

    def load(self) -> None:
        from kokoro_onnx import Kokoro  # type: ignore
        model, voices = self._files()
        self._k = Kokoro(model, voices)
        self._loaded = True

    def _synth(self, text, voice, rate, pitch, volume, cancel) -> Tuple[bytes, int]:
        import numpy as np
        samples, sr = self._k.create(text, voice=voice, speed=max(0.5, min(2.0, rate)), lang="en-us")
        if cancel is not None and cancel.is_set():
            raise CancelledError()
        pcm = np.clip(np.asarray(samples) * 32767 * max(0.0, min(2.0, volume)), -32768, 32767).astype("<i2").tobytes()
        return pcm, int(sr)

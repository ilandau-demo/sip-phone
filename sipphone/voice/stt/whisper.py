"""openai-whisper (the reference PyTorch implementation)."""
from __future__ import annotations

from .base import Availability, ProviderInfo, STTProvider, module_available, module_version, upsample_8k_to_16k


class WhisperSTT(STTProvider):
    info = ProviderInfo("whisper", "Whisper (PyTorch)", "local",
                        "The original Whisper. Heavier than Faster-Whisper; useful if you already have it and a GPU.",
                        pip="openai-whisper", stt=True,
                        fields=[{"key": "model", "label": "Model", "kind": "select", "default": "base.en",
                                 "options": ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]},
                                {"key": "language", "label": "Language", "kind": "text", "default": "en"}])

    def availability(self) -> Availability:
        if not module_available("whisper"):
            return Availability(False, "unavailable", "The openai-whisper package is not installed.", "pip install openai-whisper")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("openai-whisper")

    def load(self) -> None:
        import whisper  # type: ignore
        kw = {"download_root": __import__("os").path.join(self.models_dir, "whisper")} if self.models_dir else {}
        self._m = whisper.load_model(self.config.get("model") or "base.en", **kw)
        self._loaded = True

    def _recognize(self, pcm8k: bytes) -> str:
        audio16 = upsample_8k_to_16k(pcm8k)
        r = self._m.transcribe(audio16, language=(self.config.get("language") or None), fp16=False)
        return (r.get("text") or "").strip()

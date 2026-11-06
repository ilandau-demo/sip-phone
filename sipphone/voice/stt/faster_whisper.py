"""faster-whisper: CTranslate2 Whisper, CPU-friendly; the model downloads itself."""
from __future__ import annotations

from .base import Availability, ProviderInfo, STTProvider, module_available, module_version, upsample_8k_to_16k


class FasterWhisperSTT(STTProvider):
    info = ProviderInfo("faster-whisper", "Faster-Whisper (offline)", "local",
                        "Whisper speech recognition optimised for CPU. The model downloads on first use (tiny.en 75 MB … small.en 480 MB).",
                        pip="faster-whisper", stt=True,
                        fields=[{"key": "model", "label": "Model", "kind": "select", "default": "base.en",
                                 "options": ["tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "medium", "large-v3"]},
                                {"key": "language", "label": "Language", "kind": "text", "default": "en", "help": "blank = detect"},
                                {"key": "compute_type", "label": "Compute type", "kind": "select", "default": "int8", "options": ["int8", "int8_float16", "float16", "float32"]}])
    capabilities = {"streaming": False, "languages": True}

    def availability(self) -> Availability:
        if not module_available("faster_whisper"):
            return Availability(False, "unavailable", "The faster-whisper package is not installed.", "pip install faster-whisper")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("faster-whisper")

    def load(self) -> None:
        from faster_whisper import WhisperModel  # type: ignore
        kw = {}
        if self.models_dir:
            kw["download_root"] = __import__("os").path.join(self.models_dir, "whisper")
        self._m = WhisperModel(self.config.get("model") or "base.en", device="cpu", compute_type=self.config.get("compute_type") or "int8", **kw)
        self._loaded = True

    def _recognize(self, pcm8k: bytes) -> str:
        audio16 = upsample_8k_to_16k(pcm8k)
        lang = (self.config.get("language") or "").strip() or None
        segments, _ = self._m.transcribe(audio16, language=lang, beam_size=1, vad_filter=False, condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments).strip()

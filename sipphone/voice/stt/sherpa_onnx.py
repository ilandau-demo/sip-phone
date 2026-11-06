"""sherpa-onnx offline recognizer (e.g. Zipformer / Whisper / Paraformer models)."""
from __future__ import annotations

import os

from .base import Availability, ProviderInfo, STTProvider, module_available, module_version, upsample_8k_to_16k


class SherpaOnnxSTT(STTProvider):
    info = ProviderInfo("sherpa-onnx", "Sherpa-ONNX (offline)", "local",
                        "Offline recognition with ONNX models (transducer/zipformer, Whisper, Paraformer, …).",
                        pip="sherpa-onnx", stt=True,
                        fields=[{"key": "model_dir", "label": "Model folder", "kind": "path", "default": "", "help": "Folder with encoder/decoder/joiner .onnx and tokens.txt (transducer)"},
                                {"key": "type", "label": "Model type", "kind": "select", "default": "transducer", "options": ["transducer", "whisper", "paraformer"]}])

    def availability(self) -> Availability:
        if not module_available("sherpa_onnx"):
            return Availability(False, "unavailable", "The sherpa-onnx package is not installed.", "pip install sherpa-onnx")
        if not os.path.isdir(self.config.get("model_dir") or ""):
            return Availability(False, "model-required", "No model folder.", "Point Model folder at a sherpa-onnx model.")
        return Availability(True, "available")

    @property
    def version(self) -> str:
        return module_version("sherpa-onnx")

    def load(self) -> None:
        import sherpa_onnx  # type: ignore
        d = self.config["model_dir"]
        f = lambda n: os.path.join(d, n)  # noqa: E731
        kind = self.config.get("type") or "transducer"
        if kind == "transducer":
            enc = next((f(x) for x in os.listdir(d) if x.startswith("encoder") and x.endswith(".onnx")), f("encoder.onnx"))
            dec = next((f(x) for x in os.listdir(d) if x.startswith("decoder") and x.endswith(".onnx")), f("decoder.onnx"))
            joi = next((f(x) for x in os.listdir(d) if x.startswith("joiner") and x.endswith(".onnx")), f("joiner.onnx"))
            self._r = sherpa_onnx.OfflineRecognizer.from_transducer(encoder=enc, decoder=dec, joiner=joi, tokens=f("tokens.txt"), num_threads=2)
        elif kind == "whisper":
            enc = next(f(x) for x in os.listdir(d) if "encoder" in x and x.endswith(".onnx"))
            dec = next(f(x) for x in os.listdir(d) if "decoder" in x and x.endswith(".onnx"))
            tok = next(f(x) for x in os.listdir(d) if x.endswith("tokens.txt"))
            self._r = sherpa_onnx.OfflineRecognizer.from_whisper(encoder=enc, decoder=dec, tokens=tok, num_threads=2)
        else:
            self._r = sherpa_onnx.OfflineRecognizer.from_paraformer(paraformer=f("model.onnx"), tokens=f("tokens.txt"), num_threads=2)
        self._loaded = True

    def _recognize(self, pcm8k: bytes) -> str:
        s = self._r.create_stream()
        s.accept_waveform(16000, upsample_8k_to_16k(pcm8k))
        self._r.decode_stream(s)
        return (s.result.text or "").strip()

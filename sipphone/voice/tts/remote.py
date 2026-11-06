"""Remote providers: an OpenAI-compatible speech endpoint (OpenAI, a self-hosted
server such as Kokoro-FastAPI or openedai-speech), or ElevenLabs."""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import List, Optional, Tuple

from ..audio_output import wav_to_pcm
from .base import Availability, ProviderInfo, TTSProvider, Voice, CancelledError


class OpenAICompatibleTTS(TTSProvider):
    info = ProviderInfo("openai-compatible", "OpenAI-compatible API", "remote",
                        "Any server with a /v1/audio/speech endpoint: OpenAI itself, or a self-hosted Kokoro / Piper / XTTS server on another computer.",
                        fields=[{"key": "base_url", "label": "Base URL", "kind": "text", "default": "https://api.openai.com/v1", "help": "e.g. http://192.168.1.20:8880/v1"},
                                {"key": "api_key", "label": "API key", "kind": "secret", "default": "", "help": "Blank if the server needs none"},
                                {"key": "model", "label": "Model", "kind": "text", "default": "gpt-4o-mini-tts"},
                                {"key": "voice", "label": "Default voice", "kind": "text", "default": "alloy"},
                                {"key": "voices", "label": "Voices to offer", "kind": "text", "default": "alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer", "help": "Comma-separated"}])
    capabilities = {"streaming": False, "rate": True, "pitch": False, "volume": True, "voices": True, "install": False}

    def availability(self) -> Availability:
        if not (self.config.get("base_url") or "").strip():
            return Availability(False, "not-configured", "No server address.", "Enter the base URL of the speech API.")
        if "api.openai.com" in self.config.get("base_url", "") and not (self.config.get("api_key") or os.environ.get("OPENAI_API_KEY")):
            return Availability(False, "not-configured", "No API key.", "Enter your OpenAI API key.")
        return Availability(True, "available")

    def voices(self) -> List[Voice]:
        return [Voice(v.strip(), v.strip(), local=False) for v in (self.config.get("voices") or "").split(",") if v.strip()]

    def default_voice(self) -> str:
        return self.config.get("voice") or "alloy"

    def _synth(self, text, voice, rate, pitch, volume, cancel) -> Tuple[bytes, int]:
        base = self.config["base_url"].rstrip("/")
        key = self.config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        body = json.dumps({"model": self.config.get("model") or "gpt-4o-mini-tts", "voice": voice, "input": text,
                           "response_format": "wav", "speed": max(0.25, min(4.0, rate))}).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"{base}/audio/speech", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if cancel is not None and cancel.is_set():
            raise CancelledError()
        pcm, sr = wav_to_pcm(data)
        if volume != 1.0:
            from ..audio_output import apply_gain
            pcm = apply_gain(pcm, volume)
        return pcm, sr


class ElevenLabsTTS(TTSProvider):
    info = ProviderInfo("elevenlabs", "ElevenLabs", "remote", "ElevenLabs cloud voices (API key required).",
                        fields=[{"key": "api_key", "label": "API key", "kind": "secret", "default": ""},
                                {"key": "voice", "label": "Voice ID", "kind": "text", "default": "21m00Tcm4TlvDq8ikWAM"},
                                {"key": "model", "label": "Model", "kind": "text", "default": "eleven_turbo_v2_5"}])
    capabilities = {"streaming": False, "rate": False, "pitch": False, "volume": True, "voices": False, "install": False}

    def availability(self) -> Availability:
        if not (self.config.get("api_key") or os.environ.get("ELEVENLABS_API_KEY")):
            return Availability(False, "not-configured", "No API key.", "Enter your ElevenLabs API key.")
        return Availability(True, "available")

    def voices(self) -> List[Voice]:
        v = self.config.get("voice") or "21m00Tcm4TlvDq8ikWAM"
        return [Voice(v, v, local=False)]

    def default_voice(self) -> str:
        return self.config.get("voice") or "21m00Tcm4TlvDq8ikWAM"

    def _synth(self, text, voice, rate, pitch, volume, cancel) -> Tuple[bytes, int]:
        key = self.config.get("api_key") or os.environ.get("ELEVENLABS_API_KEY", "")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000"
        req = urllib.request.Request(url, data=json.dumps({"text": text, "model_id": self.config.get("model") or "eleven_turbo_v2_5"}).encode(),
                                     headers={"xi-api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            pcm = r.read()
        if volume != 1.0:
            from ..audio_output import apply_gain
            pcm = apply_gain(pcm, volume)
        return pcm, 16000

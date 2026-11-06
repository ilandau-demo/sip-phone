"""Bring your own recognizer: a program that reads a 16-bit 8 kHz WAV path (or
stdin) and prints the text."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile

from ..audio_output import pcm_to_wav
from .base import Availability, ProviderInfo, STTProvider


class CommandSTT(STTProvider):
    info = ProviderInfo("command-stt", "Custom program", "local",
                        "Run your own recognizer: it gets the WAV path as {wav} (or the WAV on stdin) and prints the text.",
                        stt=True,
                        fields=[{"key": "command", "label": "Command", "kind": "text", "default": "", "help": "e.g.  my-asr --file {wav}"},
                                {"key": "workdir", "label": "Working folder", "kind": "path", "default": ""}])

    def availability(self) -> Availability:
        if not (self.config.get("command") or "").strip():
            return Availability(False, "not-configured", "No command set.", "Enter the program to run.")
        return Availability(True, "available")

    def _recognize(self, pcm8k: bytes) -> str:
        cmd = self.config["command"]
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            with open(wav, "wb") as f:
                f.write(pcm_to_wav(pcm8k, 8000))
            args = shlex.split(cmd.replace("{wav}", wav), posix=os.name != "nt")
            res = subprocess.run(args, input=None if "{wav}" in cmd else pcm_to_wav(pcm8k, 8000), capture_output=True,
                                 timeout=120, cwd=self.config.get("workdir") or None, check=True)
            return res.stdout.decode("utf-8", errors="replace").strip()
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

"""Bring your own: a command-line program that writes WAV for the text it
reads on stdin (or gets as an argument). Anything that can produce a WAV file
becomes a voice provider without touching the softphone."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
from typing import List, Optional, Tuple

from ..audio_output import wav_to_pcm
from .base import Availability, ProviderInfo, TTSProvider, Voice, CancelledError


class CommandTTS(TTSProvider):
    info = ProviderInfo("command", "Custom program", "local",
                        "Run your own TTS program. It receives the text on stdin (or as {text}) and must write a 16-bit WAV to {out} or stdout.",
                        fields=[{"key": "command", "label": "Command", "kind": "text", "default": "",
                                 "help": "e.g.  my-tts --voice {voice} --rate {rate} -o {out}   (placeholders: {text} {voice} {rate} {out})"},
                                {"key": "voices", "label": "Voices", "kind": "text", "default": "default", "help": "Comma-separated names passed as {voice}"},
                                {"key": "voice", "label": "Default voice", "kind": "text", "default": "default"},
                                {"key": "workdir", "label": "Working folder", "kind": "path", "default": ""}])
    capabilities = {"streaming": False, "rate": True, "pitch": False, "volume": True, "voices": True, "install": False}

    def availability(self) -> Availability:
        cmd = (self.config.get("command") or "").strip()
        if not cmd:
            return Availability(False, "not-configured", "No command set.", "Enter the program to run.")
        exe = shlex.split(cmd, posix=os.name != "nt")[0]
        import shutil
        if not (shutil.which(exe) or os.path.isfile(exe)):
            return Availability(False, "unavailable", f"{exe} was not found.", "Check the command path.")
        return Availability(True, "available")

    def voices(self) -> List[Voice]:
        return [Voice(v.strip(), v.strip()) for v in (self.config.get("voices") or "default").split(",") if v.strip()]

    def default_voice(self) -> str:
        return self.config.get("voice") or (self.voices()[0].id if self.voices() else "")

    def _synth(self, text, voice, rate, pitch, volume, cancel) -> Tuple[bytes, int]:
        fd, out = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            cmd = self.config["command"]
            uses_out, uses_text = "{out}" in cmd, "{text}" in cmd
            rendered = cmd.replace("{out}", out).replace("{voice}", voice).replace("{rate}", f"{rate:.2f}").replace("{text}", text)
            args = shlex.split(rendered, posix=os.name != "nt")
            res = subprocess.run(args, input=None if uses_text else text.encode("utf-8"), capture_output=True,
                                 timeout=120, cwd=self.config.get("workdir") or None)
            if res.returncode != 0:
                raise RuntimeError(f"{args[0]} exited {res.returncode}: {res.stderr.decode(errors='replace')[-200:]}")
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            if uses_out:
                with open(out, "rb") as f:
                    data = f.read()
            else:
                data = res.stdout
            pcm, sr = wav_to_pcm(data)
            if volume != 1.0:
                from ..audio_output import apply_gain
                pcm = apply_gain(pcm, volume)
            return pcm, sr
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

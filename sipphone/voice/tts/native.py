"""The operating system's own voices: Windows (SAPI / OneCore via PowerShell),
macOS (`say`), Linux (espeak-ng). Always there as a fallback."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import List, Optional, Tuple

from ..audio_output import wav_to_pcm
from .base import Availability, ProviderInfo, TTSProvider, Voice, CancelledError


class NativeTTS(TTSProvider):
    info = ProviderInfo("native", "System voices", "native",
                        "The voices built into Windows, macOS or Linux. No download; quality depends on the system.",
                        fields=[{"key": "voice", "label": "Default voice", "kind": "voice", "default": ""}])
    capabilities = {"streaming": False, "rate": True, "pitch": False, "volume": True, "voices": True, "install": False}

    def _kind(self) -> str:
        if sys.platform == "win32":
            return "windows"
        if sys.platform == "darwin":
            return "mac"
        return "espeak"

    def availability(self) -> Availability:
        k = self._kind()
        if k == "windows":
            return Availability(True, "available") if shutil.which("powershell") else Availability(False, "unavailable", "PowerShell not found.")
        if k == "mac":
            return Availability(True, "available") if shutil.which("say") else Availability(False, "unavailable", "`say` not found.")
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            return Availability(True, "available")
        return Availability(False, "unavailable", "espeak-ng is not installed.", "apt install espeak-ng")

    def voices(self) -> List[Voice]:
        k = self._kind()
        try:
            if k == "windows":
                ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      "$s.GetInstalledVoices() | ForEach-Object { $i = $_.VoiceInfo; \"$($i.Name)|$($i.Culture)|$($i.Gender)\" }")
                out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20).stdout
                return [Voice(l.split("|")[0], l.split("|")[0], l.split("|")[1] if "|" in l else "", (l.split("|") + ["", ""])[2].lower())
                        for l in out.splitlines() if l.strip()]
            if k == "mac":
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=20).stdout
                vs = []
                for l in out.splitlines():
                    parts = l.split()
                    if len(parts) >= 2:
                        vs.append(Voice(parts[0], parts[0], parts[1]))
                return vs
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            out = subprocess.run([exe, "--voices=en"], capture_output=True, text=True, timeout=20).stdout
            vs = []
            for l in out.splitlines()[1:]:
                parts = l.split()
                if len(parts) >= 4:
                    vs.append(Voice(parts[1], parts[3], parts[1]))
            return vs or [Voice("en-us", "en-us", "en-US")]
        except Exception:
            return []

    def default_voice(self) -> str:
        return self.config.get("voice") or ""

    def _synth(self, text, voice, rate, pitch, volume, cancel) -> Tuple[bytes, int]:
        k = self._kind()
        fd, path = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            if k == "windows":
                r = max(-10, min(10, int(round((rate - 1.0) * 10))))
                vol = max(0, min(100, int(volume * 100)))
                sel = f"$s.SelectVoice('{voice}');" if voice else ""
                ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"{sel} $s.Rate = {r}; $s.Volume = {vol}; $s.SetOutputToWaveFile('{path}'); "
                      "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text, capture_output=True, text=True, timeout=60, check=True)
            elif k == "mac":
                args = ["say", "-o", path, "--data-format=LEI16@22050", "-r", str(int(175 * rate))]
                if voice:
                    args += ["-v", voice]
                subprocess.run(args + [text], capture_output=True, timeout=60, check=True)
            else:
                exe = shutil.which("espeak-ng") or shutil.which("espeak")
                subprocess.run([exe, "-v", voice or "en-us", "-s", str(int(160 * rate)), "-a", str(int(100 * volume)), "-w", path, text],
                               capture_output=True, timeout=60, check=True)
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            with open(path, "rb") as f:
                return wav_to_pcm(f.read())
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

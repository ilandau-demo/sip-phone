"""Call scripts: lines spoken one after another into the active call, with a
pause between them. One button runs the whole script.

Text format (import/export):
    Hello, this is an automated test call.
    pause 3
    Press 1 to confirm your appointment.
    [2]
    Goodbye.
A line that is just `pause N`, `wait N`, `[N]`, `(N)` or `--- N` sets the pause
(seconds) after the previous spoken line; otherwise DEFAULT_PAUSE applies.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("sipphone.scripts")
DEFAULT_PAUSE = 2.0
_PAUSE = re.compile(r"^\s*(?:pause|wait|---+|\[|\()\s*(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)?\s*[\]\)]?\s*$", re.I)


def parse_text(content: str, default_pause: float = DEFAULT_PAUSE) -> List[dict]:
    steps: List[dict] = []
    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _PAUSE.match(line)
        if m:
            if steps:
                steps[-1]["pause"] = float(m.group(1))
            continue
        steps.append({"text": line, "pause": default_pause})
    return steps


def to_text(steps: List[dict]) -> str:
    out = []
    for s in steps:
        out.append(str(s.get("text", "")))
        out.append(f"pause {float(s.get('pause', DEFAULT_PAUSE)):g}")
    return "\n".join(out) + ("\n" if out else "")


def clean_script(s: dict) -> dict:
    steps = []
    for st in s.get("steps", []):
        if not isinstance(st, dict):
            continue
        text = str(st.get("text", "")).strip()
        if not text:
            continue
        try:
            pause = max(0.0, min(600.0, float(st.get("pause", DEFAULT_PAUSE))))
        except (TypeError, ValueError):
            pause = DEFAULT_PAUSE
        steps.append({"text": text, "pause": pause})
    sid = str(s.get("id", "")).strip() or f"s{int(time.time() * 1000)}"
    return {"id": sid, "name": str(s.get("name", "")).strip()[:60] or "Script", "steps": steps}


class ScriptStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> List[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return [clean_script(s) for s in data.get("scripts", []) if isinstance(s, dict)]
        except (FileNotFoundError, ValueError):
            return []

    def save(self, scripts: List[dict]) -> List[dict]:
        clean, ids = [], set()
        for s in scripts:
            c = clean_script(s)
            while c["id"] in ids:
                c["id"] += "x"
            ids.add(c["id"])
            clean.append(c)
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"scripts": clean}, f, indent=2)
        os.replace(self.path + ".tmp", self.path)
        return clean


class ScriptRunner:
    """Speaks a script's lines in order on the audio path; stop() interrupts.
    `connected()` is polled so a script ends with the call."""

    def __init__(self, audio, connected: Callable[[], bool], on_event: Optional[Callable[[str, str], None]] = None) -> None:
        self.audio = audio
        self.connected = connected
        self.on_event = on_event or (lambda k, t: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.script: Optional[dict] = None
        self.step = 0
        self.finished = ""           # "done" | "stopped" | "call ended" | error text

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> Optional[dict]:
        if self.script is None:
            return None
        return {"running": self.running, "id": self.script["id"], "name": self.script["name"], "step": self.step,
                "total": len(self.script["steps"]), "line": self.script["steps"][self.step - 1]["text"] if 0 < self.step <= len(self.script["steps"]) else "",
                "finished": "" if self.running else self.finished}

    def run(self, script: dict) -> None:
        if not hasattr(self.audio, "say"):
            raise RuntimeError("Text-to-speech is off — add a voice provider in Settings → Voice engine")
        if not script.get("steps"):
            raise RuntimeError("This script has no lines")
        if not self.connected():
            raise RuntimeError("Connect a call first, then play the script")
        self.stop()
        with self._lock:
            self._stop = threading.Event()
            self.script, self.step, self.finished = script, 0, ""
            self._thread = threading.Thread(target=self._play, args=(script, self._stop), name="script", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            self._stop.set()
            try:
                self.audio.stop_speaking()
            except Exception:
                pass
            t.join(timeout=3)

    def _play(self, script: dict, stop: threading.Event) -> None:
        self.on_event("event", f"Script “{script['name']}” started")
        try:
            for i, st in enumerate(script["steps"], 1):
                if stop.is_set():
                    self.finished = "stopped"; break
                if not self.connected():
                    self.finished = "call ended"; break
                self.step = i
                self.audio.say(st["text"])
                self.on_event("said", st["text"])
                # wait for the line to finish playing, then the pause
                deadline = time.time() + 120
                time.sleep(0.3)
                while self.audio.speaking and time.time() < deadline and not stop.is_set() and self.connected():
                    time.sleep(0.1)
                if stop.wait(float(st.get("pause", DEFAULT_PAUSE))):
                    self.finished = "stopped"; break
            else:
                self.finished = "done"
        except Exception as e:
            log.exception("script failed")
            self.finished = f"error: {e}"
        self.on_event("event", {"done": f"Script “{script['name']}” finished", "stopped": "Script stopped",
                                "call ended": "Script ended with the call"}.get(self.finished, f"Script failed ({self.finished})"))

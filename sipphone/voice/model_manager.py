"""Optional model manager: a small catalog of downloadable voices and
recognizers, with sizes, licenses and progress. Nothing is bundled with the
installer; the user picks what to fetch and where it lives."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import urllib.request
import zipfile
from typing import Dict, List, Optional

log = logging.getLogger("sipphone.voice")

HF = "https://huggingface.co"
PIPER = HF + "/rhasspy/piper-voices/resolve/main"
# id, title, provider type, files [(url, relative path)], size MB, license, notes
CATALOG: List[dict] = [
    {"id": "piper-en_US-lessac-medium", "title": "Piper · Lessac (US English, medium)", "provider": "piper", "size_mb": 63,
     "license": "MIT (model) / public-domain speech data", "requirements": "CPU, ~150 MB RAM", "local": True,
     "files": [(f"{PIPER}/en/en_US/lessac/medium/en_US-lessac-medium.onnx", "piper/en_US-lessac-medium.onnx"),
               (f"{PIPER}/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json", "piper/en_US-lessac-medium.onnx.json")]},
    {"id": "piper-en_US-amy-medium", "title": "Piper · Amy (US English, medium)", "provider": "piper", "size_mb": 63,
     "license": "MIT (model) / CC0 speech data", "requirements": "CPU, ~150 MB RAM", "local": True,
     "files": [(f"{PIPER}/en/en_US/amy/medium/en_US-amy-medium.onnx", "piper/en_US-amy-medium.onnx"),
               (f"{PIPER}/en/en_US/amy/medium/en_US-amy-medium.onnx.json", "piper/en_US-amy-medium.onnx.json")]},
    {"id": "piper-en_GB-alan-medium", "title": "Piper · Alan (British English, medium)", "provider": "piper", "size_mb": 63,
     "license": "MIT (model) / CC-BY speech data", "requirements": "CPU, ~150 MB RAM", "local": True,
     "files": [(f"{PIPER}/en/en_GB/alan/medium/en_GB-alan-medium.onnx", "piper/en_GB-alan-medium.onnx"),
               (f"{PIPER}/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json", "piper/en_GB-alan-medium.onnx.json")]},
    {"id": "piper-en_US-ryan-high", "title": "Piper · Ryan (US English, high)", "provider": "piper", "size_mb": 115,
     "license": "MIT (model) / CC0 speech data", "requirements": "CPU, ~250 MB RAM", "local": True,
     "files": [(f"{PIPER}/en/en_US/ryan/high/en_US-ryan-high.onnx", "piper/en_US-ryan-high.onnx"),
               (f"{PIPER}/en/en_US/ryan/high/en_US-ryan-high.onnx.json", "piper/en_US-ryan-high.onnx.json")]},
    {"id": "kokoro-v1.0", "title": "Kokoro v1.0 (82M, many voices)", "provider": "kokoro", "size_mb": 340,
     "license": "Apache-2.0", "requirements": "CPU, ~1 GB RAM; pip install kokoro-onnx", "local": True,
     "files": [("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", "kokoro/kokoro-v1.0.onnx"),
               ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", "kokoro/voices-v1.0.bin")]},
    {"id": "vosk-small-en-us", "title": "Vosk · small English (US)", "provider": "vosk", "size_mb": 40,
     "license": "Apache-2.0", "requirements": "CPU, ~300 MB RAM; pip install vosk", "local": True,
     "files": [("https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip", "vosk/vosk-model-small-en-us-0.15.zip")], "unzip": "vosk"},
    {"id": "faster-whisper-base.en", "title": "Faster-Whisper · base.en", "provider": "faster-whisper", "size_mb": 75,
     "license": "MIT", "requirements": "CPU, ~500 MB RAM; pip install faster-whisper", "local": True, "managed": True,
     "notes": "Downloaded by faster-whisper itself on first use."},
]


class ModelManager:
    def __init__(self, models_dir: str) -> None:
        self.models_dir = models_dir
        self.jobs: Dict[str, dict] = {}           # model id -> {state, progress, bytes, total, error}
        self._lock = threading.Lock()

    def set_dir(self, path: str) -> None:
        self.models_dir = path
        os.makedirs(path, exist_ok=True)

    def installed(self, entry: dict) -> bool:
        if entry.get("managed"):
            return False
        for _, rel in entry["files"]:
            target = os.path.join(self.models_dir, rel)
            if entry.get("unzip"):
                if not os.path.isdir(os.path.join(self.models_dir, entry["unzip"])) or not any(
                        os.path.isdir(os.path.join(self.models_dir, entry["unzip"], d)) for d in os.listdir(os.path.join(self.models_dir, entry["unzip"]))):
                    return False
            elif not os.path.isfile(target):
                return False
        return True

    def status(self) -> dict:
        out = []
        for e in CATALOG:
            d = {k: v for k, v in e.items() if k != "files"}
            d["installed"] = self.installed(e)
            d["job"] = self.jobs.get(e["id"])
            d["pip_installed"] = self._pip_ok(e["provider"])
            out.append(d)
        return {"models_dir": self.models_dir, "catalog": out, "custom": self._custom_files()}

    @staticmethod
    def _pip_ok(provider: str) -> bool:
        from .provider_interface import module_available
        return module_available({"piper": "piper", "kokoro": "kokoro_onnx", "vosk": "vosk", "faster-whisper": "faster_whisper"}.get(provider, provider))

    def _custom_files(self) -> List[dict]:
        """User-provided voices found in the models folder that aren't in the catalog."""
        out = []
        if not self.models_dir or not os.path.isdir(self.models_dir):
            return out
        known = {rel for e in CATALOG for _, rel in e.get("files", [])}
        for root, _, files in os.walk(self.models_dir):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.models_dir).replace(os.sep, "/")
                if rel not in known and f.endswith(".onnx"):
                    out.append({"path": rel, "size_mb": round(os.path.getsize(os.path.join(root, f)) / 1e6, 1)})
        return out

    def download(self, model_id: str) -> dict:
        e = next((x for x in CATALOG if x["id"] == model_id), None)
        if not e:
            raise ValueError("unknown model")
        if e.get("managed"):
            raise ValueError("this model is downloaded by its provider on first use")
        with self._lock:
            if self.jobs.get(model_id, {}).get("state") == "downloading":
                return self.jobs[model_id]
            self.jobs[model_id] = {"state": "downloading", "progress": 0.0, "bytes": 0, "total": int(e["size_mb"] * 1e6), "error": ""}
        threading.Thread(target=self._fetch, args=(e,), name="model-download", daemon=True).start()
        return self.jobs[model_id]

    def _fetch(self, e: dict) -> None:
        job = self.jobs[e["id"]]
        try:
            done = 0
            for url, rel in e["files"]:
                target = os.path.join(self.models_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".part"
                req = urllib.request.Request(url, headers={"User-Agent": "sipphone"})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    if total:
                        job["total"] = max(job["total"], done + total)
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        job["bytes"] = done
                        job["progress"] = min(0.99, done / job["total"]) if job["total"] else 0.0
                os.replace(tmp, target)
                if e.get("unzip") and target.endswith(".zip"):
                    with zipfile.ZipFile(target) as z:
                        z.extractall(os.path.join(self.models_dir, e["unzip"]))
                    os.unlink(target)
            if not self.installed(e):
                raise RuntimeError("files missing after download")
            job.update(state="done", progress=1.0)
        except Exception as ex:
            log.warning("download %s failed: %s", e["id"], ex)
            job.update(state="error", error=str(ex))

    def remove(self, model_id: str) -> None:
        e = next((x for x in CATALOG if x["id"] == model_id), None)
        if not e:
            raise ValueError("unknown model")
        for _, rel in e["files"]:
            p = os.path.join(self.models_dir, rel)
            if e.get("unzip"):
                shutil.rmtree(os.path.join(self.models_dir, e["unzip"]), ignore_errors=True)
            elif os.path.isfile(p):
                os.unlink(p)
        self.jobs.pop(model_id, None)

    def verify(self, model_id: str) -> dict:
        e = next((x for x in CATALOG if x["id"] == model_id), None)
        if not e:
            raise ValueError("unknown model")
        out = {}
        for _, rel in e.get("files", []):
            p = os.path.join(self.models_dir, rel)
            if os.path.isfile(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                out[rel] = {"size": os.path.getsize(p), "sha256": h.hexdigest()}
            else:
                out[rel] = {"missing": True}
        return out

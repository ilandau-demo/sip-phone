"""The modular voice engine: registry, provider interface, fallback and
failure isolation, config migration, model manager, API."""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone.voice import VoiceEngine, TTSProvider, STTProvider, Voice, Availability, ProviderInfo, register  # noqa: E402
from sipphone.voice import registry as reg  # noqa: E402
from sipphone.voice.audio_output import wav_to_pcm, pcm_to_wav  # noqa: E402


def tone(seconds=0.2, rate=16000):
    import math
    return struct.pack(f"<{int(rate * seconds)}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(rate * seconds))))


class FakeTTS(TTSProvider):
    info = ProviderInfo("fake-tts", "Fake voice", "local", "test double", fields=[{"key": "fail", "label": "fail", "kind": "text", "default": ""},
                                                                                   {"key": "voice", "label": "voice", "kind": "voice", "default": "v1"}])
    capabilities = {"streaming": False, "rate": True, "pitch": False, "volume": True, "voices": True, "install": False}
    calls = []

    def availability(self):
        if self.config.get("fail") == "unavailable":
            return Availability(False, "unavailable", "broken on purpose", "fix it")
        return Availability(True, "available")

    def voices(self):
        return [Voice("v1", "Voice one"), Voice("v2", "Voice two")]

    def _synth(self, text, voice, rate, pitch, volume, cancel):
        FakeTTS.calls.append((self.config.get("name", "?"), text, voice, rate))
        if self.config.get("fail") == "raise":
            raise RuntimeError("synth exploded")
        return tone(0.1), 16000


class FakeSTT(STTProvider):
    info = ProviderInfo("fake-stt", "Fake ears", "local", "test double", stt=True, fields=[{"key": "fail", "label": "fail", "kind": "text", "default": ""}])

    def _recognize(self, pcm8k):
        if self.config.get("fail"):
            raise RuntimeError("asr exploded")
        return f"heard {len(pcm8k)} bytes"


register(FakeTTS); register(FakeSTT)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        FakeTTS.calls = []
        self.v = VoiceEngine(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_registry_defaults_and_fallback_isolation(self):
        v = self.v
        self.assertFalse(v.tts_enabled); self.assertEqual(v.synthesize("hi"), (b"", 8000))     # nothing configured: silence, no error
        a = v.registry.add("fake-tts", "A", {"name": "A"}, pid="a")
        b = v.registry.add("fake-tts", "B", {"name": "B"}, pid="b")
        v.registry.set_defaults(tts="a", tts_fallback="b", voice="v2", rate=1.5)
        self.assertTrue(v.tts_enabled)
        pcm, sr = v.synthesize("hello")
        self.assertEqual((sr, len(pcm) > 0), (16000, True))
        self.assertEqual(FakeTTS.calls[-1], ("A", "hello", "v2", 1.5))
        # provider A breaks: B takes over, nothing raises
        v.registry.update("a", config={"fail": "raise"})
        pcm, sr = v.synthesize("again")
        self.assertEqual(FakeTTS.calls[-1][0], "B"); self.assertEqual(v.stats["fallbacks"], 1); self.assertIn("a:", v.last_error)
        # both broken: silence, still no exception
        v.registry.update("b", config={"fail": "raise"})
        self.assertEqual(v.synthesize("x"), (b"", 8000))
        # assignments: a different provider for notifications
        c = v.registry.add("fake-tts", "C", {"name": "C"}, pid="c")
        v.registry.set_defaults(assignments={"notifications": "c"})
        v.synthesize("ping", purpose="notifications")
        self.assertEqual(FakeTTS.calls[-1][0], "C")
        # disabling removes it from the chain; removing clears defaults
        v.registry.update("c", enabled=False)
        self.assertIsNone(v.tts_provider("c"))
        v.registry.remove("a")
        self.assertEqual(v.registry.defaults["tts"], "")
        # persisted
        v2 = VoiceEngine(self.d)
        self.assertEqual([p["id"] for p in v2.registry.providers], ["b", "c"])
        self.assertEqual(v2.registry.defaults["tts_fallback"], "b")

    def test_stt_isolation_and_status(self):
        v = self.v
        self.assertEqual(v.recognize(b"\x00" * 320), "")
        v.registry.add("fake-stt", "Ears", pid="ears"); v.registry.set_defaults(stt="ears")
        self.assertEqual(v.recognize(b"\x00" * 320), "heard 320 bytes")
        v.registry.update("ears", config={"fail": "1"})
        self.assertEqual(v.recognize(b"\x00" * 320), "")                                        # failure → empty text, no raise
        st = v.status()
        self.assertTrue(any(p["id"] == "ears" and p["stt"] and p["is_default"] for p in st["providers"]))
        self.assertIn("fake-tts", [t["type"] for t in st["types"]])
        self.assertEqual(st["stats"]["recognize_failed"], 1)

    def test_availability_states_and_test_voice(self):
        v = self.v
        v.registry.add("fake-tts", "A", {"name": "A", "fail": "unavailable"}, pid="a")
        st = v.status()["providers"][0]["availability"]
        self.assertEqual((st["state"], st["reason"]), ("unavailable", "broken on purpose"))
        wav, err = v.test_voice("a")
        self.assertIn("broken on purpose", err)
        v.registry.update("a", config={"fail": ""})
        wav, err = v.test_voice("a", "v1", "testing")
        self.assertEqual(err, ""); self.assertEqual(wav_to_pcm(wav)[1], 16000)
        # built-in types report plainly when their package is missing (no import crash)
        p = v.registry.add("kokoro", "Kokoro", pid="k")
        av = v.registry.instance("k").availability()
        self.assertIn(av.state, ("unavailable", "model-required", "available"))
        n = v.registry.add("native", "System", pid="native")
        self.assertIn(v.registry.instance("native").availability().state, ("available", "unavailable"))

    def test_secrets_protected_in_voice_json(self):
        v = self.v
        v.registry.add("openai-compatible", "Cloud", {"base_url": "https://api.openai.com/v1", "api_key": "sk-secret"}, pid="cloud")
        raw = open(os.path.join(self.d, "voice.json")).read()
        self.assertNotIn("sk-secret", raw)
        self.assertEqual(VoiceEngine(self.d).registry.get("cloud")["config"]["api_key"], "sk-secret")
        self.assertEqual(v.status()["providers"][0]["config"]["api_key"], "")                  # never sent to the browser
        self.assertTrue(v.status()["providers"][0]["has_secret"])
        v.registry.update("cloud", config={"api_key": ""})                                      # blank keeps the key
        self.assertEqual(v.registry.get("cloud")["config"]["api_key"], "sk-secret")

    def test_migration_from_old_config(self):
        v = self.v
        changed = v.migrate_from_config({"engine": "piper", "model": "en_US-amy-medium", "gain": "0.8"}, {"engine": "whisper", "model": "small.en"})
        self.assertTrue(changed)
        ids = [p["id"] for p in v.registry.providers]
        self.assertIn("piper", ids); self.assertIn("native", ids); self.assertIn("faster-whisper", ids)
        d = v.registry.defaults
        self.assertEqual((d["tts"], d["tts_fallback"], d["stt"], d["voice"], d["volume"]), ("piper", "native", "faster-whisper", "en_US-amy-medium", 0.8))
        self.assertEqual(v.registry.get("faster-whisper")["config"]["model"], "small.en")
        self.assertFalse(v.migrate_from_config({"engine": "piper"}, {}))                         # only once
        # an unconfigured old install still gets the system voice as a provider
        v2 = VoiceEngine(tempfile.mkdtemp())
        v2.migrate_from_config({}, {})
        self.assertEqual([p["id"] for p in v2.registry.providers], ["native"])


class CustomCommandTests(unittest.TestCase):
    def test_command_provider_runs_a_program(self):
        d = tempfile.mkdtemp()
        script = os.path.join(d, "mytts.py")
        open(script, "w").write("import sys, wave, struct, math\n"
                                "out = sys.argv[sys.argv.index('-o') + 1]; text = sys.stdin.read()\n"
                                "w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)\n"
                                "w.writeframes(struct.pack('<%dh' % 1600, *([0] * 1600))); w.close()\n")
        v = VoiceEngine(d)
        v.registry.add("command", "Mine", {"command": f'"{sys.executable}" "{script}" --voice {{voice}} -o {{out}}'}, pid="mine")
        v.registry.set_defaults(tts="mine")
        pcm, sr = v.synthesize("hello")
        self.assertEqual((sr, len(pcm)), (8000, 3200))
        # and a custom recognizer
        asr = os.path.join(d, "myasr.py"); open(asr, "w").write("import sys; print('the words', file=sys.stdout)\n")
        v.registry.add("command-stt", "My ears", {"command": f'"{sys.executable}" "{asr}" {{wav}}'}, pid="ears")
        v.registry.set_defaults(stt="ears")
        self.assertEqual(v.recognize(b"\x00" * 3200), "the words")
        shutil.rmtree(d, ignore_errors=True)


class ModelManagerTests(unittest.TestCase):
    def test_download_remove_verify(self):
        from sipphone.voice import model_manager as mm
        payload = b"ONNX" * 2500

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                body = payload if self.path.endswith(".onnx") else b'{"sample_rate": 22050}'
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        d = tempfile.mkdtemp()
        old = mm.CATALOG[:]
        mm.CATALOG[:] = [{"id": "t-voice", "title": "Test voice", "provider": "piper", "size_mb": 0.01, "license": "MIT", "local": True,
                          "files": [(base + "/v.onnx", "piper/v.onnx"), (base + "/v.onnx.json", "piper/v.onnx.json")]}]
        try:
            m = mm.ModelManager(d)
            self.assertFalse(m.status()["catalog"][0]["installed"])
            m.download("t-voice")
            end = time.time() + 10
            while time.time() < end and m.jobs["t-voice"]["state"] == "downloading":
                time.sleep(0.05)
            self.assertEqual(m.jobs["t-voice"]["state"], "done", m.jobs)
            self.assertTrue(m.status()["catalog"][0]["installed"])
            ver = m.verify("t-voice")
            self.assertEqual(ver["piper/v.onnx"]["size"], len(payload))
            # the Piper provider now sees the voice
            from sipphone.voice.tts.piper import PiperTTS
            self.assertEqual([x.id for x in PiperTTS({}, models_dir=d).voices()], ["v"])
            m.remove("t-voice")
            self.assertFalse(m.status()["catalog"][0]["installed"])
        finally:
            mm.CATALOG[:] = old
            srv.shutdown(); shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

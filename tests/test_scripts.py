"""Call scripts: text format, runner (pauses, stop, call end), API."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone.scripts import parse_text, to_text, ScriptRunner, ScriptStore, clean_script  # noqa: E402


class FakeAudio:
    name = "fake"

    def __init__(self):
        self.said, self._until = [], 0.0

    def say(self, text):
        self.said.append((text, time.time())); self._until = time.time() + 0.3     # "plays" for 0.3 s

    @property
    def speaking(self):
        return time.time() < self._until

    def stop_speaking(self):
        self._until = 0.0


class ParseTests(unittest.TestCase):
    def test_text_format(self):
        steps = parse_text("Hello there.\npause 3\n\nPress 1 to confirm.\n[1.5]\nGoodbye\n--- 4\n", default_pause=2)
        self.assertEqual(steps, [{"text": "Hello there.", "pause": 3.0}, {"text": "Press 1 to confirm.", "pause": 1.5}, {"text": "Goodbye", "pause": 4.0}])
        self.assertEqual(parse_text("wait 2\nOnly line\n(5s)")[0], {"text": "Only line", "pause": 5.0})
        self.assertEqual(parse_text(to_text(steps)), steps)                     # round trip
        c = clean_script({"name": " Demo ", "steps": [{"text": " a ", "pause": "x"}, {"text": "", "pause": 1}, {"text": "b", "pause": 9999}]})
        self.assertEqual((c["name"], c["steps"]), ("Demo", [{"text": "a", "pause": 2.0}, {"text": "b", "pause": 600.0}]))

    def test_store(self):
        d = tempfile.mkdtemp(); st = ScriptStore(os.path.join(d, "scripts.json"))
        self.assertEqual(st.load(), [])
        out = st.save([{"name": "A", "steps": [{"text": "x", "pause": 1}]}, {"id": "dup", "name": "B", "steps": [{"text": "y"}]}, {"id": "dup", "name": "C", "steps": [{"text": "z"}]}])
        self.assertEqual([s["name"] for s in st.load()], ["A", "B", "C"])
        self.assertEqual(len({s["id"] for s in out}), 3)
        shutil.rmtree(d, ignore_errors=True)


class RunnerTests(unittest.TestCase):
    def test_lines_pauses_stop_and_call_end(self):
        aud, events = FakeAudio(), []
        connected = {"on": True}
        r = ScriptRunner(aud, lambda: connected["on"], on_event=lambda k, t: events.append((k, t)))
        script = {"id": "s1", "name": "Demo", "steps": [{"text": "one", "pause": 0.5}, {"text": "two", "pause": 0.5}, {"text": "three", "pause": 0}]}
        r.run(script)
        time.sleep(0.1)
        self.assertTrue(r.running); self.assertEqual(r.status()["step"], 1)
        end = time.time() + 5
        while r.running and time.time() < end:
            time.sleep(0.05)
        self.assertEqual([t for t, _ in aud.said], ["one", "two", "three"])
        gaps = [aud.said[i + 1][1] - aud.said[i][1] for i in range(2)]
        for g in gaps:
            self.assertGreater(g, 0.75); self.assertLess(g, 1.6)                 # 0.3 s speech + 0.5 s pause (+ polling)
        self.assertEqual(r.status()["finished"], "done")
        self.assertIn(("said", "two"), events); self.assertTrue(any("finished" in t for k, t in events if k == "event"))
        # stop mid-way
        aud.said.clear()
        r.run({"id": "s2", "name": "Long", "steps": [{"text": "a", "pause": 5}, {"text": "b", "pause": 0}]})
        time.sleep(0.2); r.stop()
        self.assertEqual([t for t, _ in aud.said], ["a"]); self.assertEqual(r.status()["finished"], "stopped")
        # the call ends: the script ends with it
        aud.said.clear()
        r.run({"id": "s3", "name": "Ends", "steps": [{"text": "a", "pause": 0.3}, {"text": "b", "pause": 0}]})
        time.sleep(0.1); connected["on"] = False
        end = time.time() + 3
        while r.running and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(r.status()["finished"], "call ended"); self.assertEqual([t for t, _ in aud.said], ["a"])
        # refuses without a call / without TTS
        with self.assertRaises(RuntimeError):
            r.run(script)
        with self.assertRaises(RuntimeError):
            ScriptRunner(object(), lambda: True).run(script)


if __name__ == "__main__":
    unittest.main()

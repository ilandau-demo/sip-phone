"""Browser: the call screen keeps one block per call, folds a finished call into a
one-line summary, keeps only the last one, and never shows registration
refreshes, settings notices or availability alerts as transcript lines."""
import os, shutil, sys, tempfile, time, logging
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.WARNING)
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from sipphone import config_store, web
from sipphone.cli import load_config
from sipphone.phone import SIPPhone
from sipphone.audio import open_audio
from playwright.sync_api import sync_playwright

fails = []
def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini"); shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
sw = MockSwitch(); sw.start()
config_store.update(p, {"account": {"username": USER, "auth_username": AUTH, "password": PASS, "domain": REALM,
                                    "proxy": "127.0.0.1", "proxy_port": str(sw.port), "display_name": "Demo Desk"},
                        "local": {"bind_port": "0"}, "audio": {"backend": "null"}})
cfg, _, _ = load_config(p)
phone = SIPPhone(cfg, audio=open_audio(kind="null")); phone.start(); phone.register(wait=True)
import json as _j
_j.dump([{"t": time.time() - 60, "direction": "in", "number": "6165550199", "name": "Inbound", "outcome": "answered", "duration": 5, "reason": ""},
         {"t": time.time() - 120, "direction": "out", "number": "2142983342", "name": "Day Mode", "outcome": "answered", "duration": 9, "reason": ""}],
        open(f"{d}/h", "w"))
srv = web.serve(phone, phone.audio, port=0, open_browser=False, phrases_path=f"{d}/p", contacts_path=f"{d}/c",
                history_path=f"{d}/h", config_path=p)
st = srv.ui_state
base = f"http://127.0.0.1:{srv.server_address[1]}"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
for _ in range(12):                        # a long day of registration refreshes
    phone.on_registration(True, "expires 3600s")
st.add("system", "Account settings saved — registering again")

def call(i, lines):
    st.add("event", f"Incoming call from Day Mode Call Flow - WIRELESS CALLER {i} (214298334{i})")
    st.add("event", f"Connected to 214298334{i} (PCMU)")
    for k, t in lines:
        st.add(k, t)
    st.add("event", f"Call ended: hung up after 1:0{i}")

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 420, "height": 860})
    pg.goto(base + "/"); pg.wait_for_timeout(1500)
    t = pg.inner_text("#thread")
    check("no registration lines in the call screen", "Registered (" not in t)
    check("settings notice is not a transcript line", "Account settings saved" not in t)
    call(1, [("said", "Hello, this is an automated call. Please hold for a moment."), ("heard", "Sure, I can hold.")])
    pg.wait_for_timeout(800)
    check("finished call folds into one summary", pg.locator("#thread .callgrp.done").count() == 1 and not pg.eval_on_selector("#thread .callgrp.done", "e => e.open"))
    summ = pg.inner_text("#thread .callgrp.done summary")
    check(f"summary names the caller, duration and messages ({summ!r})", "WIRELESS CALLER 1" in summ and "1:01" in summ and "2 messages" in summ)
    check("the transcript is hidden until opened", not pg.is_visible("#thread .msg.said"))
    pg.click("#thread .callgrp.done summary"); pg.wait_for_timeout(200)
    check("clicking the summary shows that call's transcript", pg.is_visible("#thread .msg.said"))
    # a call in progress shows its lines open
    st.add("event", "Incoming call from Lab Bench (6165550101)"); st.add("event", "Connected to 6165550101 (PCMU)")
    st.add("said", "Second call line"); pg.wait_for_timeout(600)
    check("live call is open and shows its lines", pg.is_visible("text=Second call line") and pg.locator("#thread .callgrp.live").count() == 1)
    st.add("event", "Call ended: caller cancelled after 0:09"); pg.wait_for_timeout(600)
    check("only the last finished call is kept", pg.locator("#thread .callgrp").count() == 1 and "Lab Bench" in pg.inner_text("#thread .callgrp summary"))
    for i in range(3, 8):
        call(i, [("said", f"line {i}")])
    pg.wait_for_timeout(1000)
    h = pg.eval_on_selector("#thread", "e => e.scrollHeight")
    check(f"after several calls the screen stays short ({h}px)", pg.locator("#thread .callgrp").count() == 1 and h < 900)
    pg.screenshot(path=os.path.join(OUT, "thread_after_calls.png"))
    # reload: same result from the server's replay
    pg.reload(); pg.wait_for_timeout(1500)
    check("after a reload the screen is still one folded call", pg.locator("#thread .callgrp").count() == 1 and "Registered (" not in pg.inner_text("#thread"))
    # redial: Call with an empty field dials the last number called (outgoing)
    calls = []
    pg.route("**/api/call", lambda route: (calls.append(route.request.post_data_json), route.fulfill(status=200, body="{}", content_type="application/json")))
    pg.fill("#number", ""); pg.wait_for_timeout(200)
    check("empty field: button says Redial", pg.inner_text("#btn-call").strip() == "Redial" and "2142983342" in (pg.get_attribute("#btn-call", "title") or ""))
    pg.click("#btn-call"); pg.wait_for_timeout(400)
    check(f"Redial dials the last outgoing number ({calls})", calls and calls[-1].get("number") == "2142983342" and pg.input_value("#number") == "2142983342")
    pg.fill("#number", "5551234"); pg.wait_for_timeout(100)
    check("typing a number turns it back into Call", pg.inner_text("#btn-call").strip() == "Call")
    pg.click("#btn-call"); pg.wait_for_timeout(300)
    check("…and dials what was typed", calls[-1].get("number") == "5551234")
    pg.unroute("**/api/call")
    # Settings → Registration history
    pg.click("#btn-settings"); pg.wait_for_timeout(700)
    pg.click("#s-reglog summary"); pg.wait_for_timeout(600)
    items = pg.locator("#s-reglog-list li").count()
    check(f"registration history lists the refreshes ({items})", items >= 12 and "Registered (expires 3600s)" in pg.inner_text("#s-reglog-list"))
    check("current state shown", "Registered now" in pg.inner_text("#s-reglog-now"))
    pg.locator("#s-reglog").scroll_into_view_if_needed()
    pg.screenshot(path=os.path.join(OUT, "thread_reglog.png"))
    b.close()
srv.shutdown(); srv.server_close(); phone.stop(); sw.stop.set()
print("ALL THREAD CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

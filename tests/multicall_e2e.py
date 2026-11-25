"""Browser: several calls at once. Dial from any line while another is on a
call, up to three calls per line, one call with the audio and the rest on
hold, New call / Back to call, the strip of other calls, call waiting on the
same line, and what the dial pad and the lists do in each state."""
import json, os, shutil, sys, tempfile, time, logging, urllib.request
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.WARNING)
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from sipphone import config_store, web
from sipphone.cli import load_config
from sipphone.lines import Lines, line_configs
from sipphone.phone import CallState, SIPPhone
from sipphone.audio import open_audio
from playwright.sync_api import sync_playwright

fails = []
def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

def wait(fn, t=8):
    end = time.time() + t
    while time.time() < end:
        try:
            if fn(): return True
        except Exception:
            pass
        time.sleep(0.05)
    return False

d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini"); shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
sw1, sw2 = MockSwitch(), MockSwitch()
for s_ in (sw1, sw2): s_.start()
config_store.update(p, {"account": {"username": USER, "auth_username": AUTH, "password": PASS, "domain": REALM,
                                    "proxy": "127.0.0.1", "proxy_port": str(sw1.port), "display_name": "Front Desk",
                                    "label": "Front desk"},
                        "line2": {"enabled": "true", "label": "Sales demo", "username": USER, "auth_username": AUTH,
                                  "password": PASS, "domain": REALM, "proxy": "127.0.0.1", "proxy_port": str(sw2.port)},
                        "local": {"bind_port": "0"}, "audio": {"backend": "null"}})
cfg, _, _ = load_config(p)
p1 = SIPPhone(cfg, audio=open_audio(kind="null")); p1.start()
lines = Lines(p1, label="Front desk")
for n, (c, lbl) in line_configs(config_store.read(p), cfg).items():
    lines.set_line(n, c, lbl)
p1.register(wait=True)
srv = web.serve(lines, lines.audio, port=0, open_browser=False, phrases_path=f"{d}/p", contacts_path=f"{d}/c",
                history_path=f"{d}/h", config_path=p)
base = f"http://127.0.0.1:{srv.server_address[1]}"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
check("both lines register", wait(lambda: all(l["registered"] for l in lines.info())))
urllib.request.urlopen(urllib.request.Request(base + "/api/contacts", data=json.dumps(
    {"contacts": [{"name": "Dana Demo", "number": "5557000"}]}).encode(), headers={"Content-Type": "application/json"}))

def live():
    return lines._live()
def talking():
    return [c for c in live() if c.state == CallState.CONNECTED and not c.held]
def numbers(sel):
    return [c.remote_number for c in sel]

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 440, "height": 980})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(base + "/"); pg.wait_for_timeout(1500)
    state = lambda: pg.inner_text("#callstate")
    strip = lambda: pg.inner_text("#otherlines") if pg.is_visible("#otherlines") else ""
    picker = lambda: pg.eval_on_selector_all("#linepick option", "os => os.map(o => o.textContent)")
    pg.click("#tab-contacts"); pg.wait_for_timeout(200)
    dana = "#contacts .contact:has-text('Dana Demo') button.dial"

    # ---- one call on line 1 ----
    pg.fill("#number", "5551001"); pg.click("#btn-call")
    check("line 1 call connects", wait(lambda: "Connected" in state() and "FRONT DESK" in state().upper()))
    check("talking: no Call button, Hang up and New call instead",
          pg.is_hidden("#btn-call") and pg.is_visible("#btn-hang") and pg.inner_text("#btn-newcall") == "New call")
    check("talking: the pad sends tones", "touch tones" in pg.inner_text("#hint"))
    check("talking: a contact can't be dialled by accident", pg.is_disabled(dana))
    pg.click("#keys button[data-k='5']"); pg.wait_for_timeout(300)
    check("a key during the call is a tone, not a digit to dial", pg.inner_text("#thread").count("5") >= 1 and pg.is_hidden("#btn-call"))

    # ---- pick another line and dial: the user's case ----
    pg.select_option("#linepick", "2"); pg.wait_for_timeout(500)
    check("picking another line during a call opens the dialer", pg.is_visible("#btn-call") and pg.inner_text("#btn-call").strip() == "Call"
          and pg.input_value("#number") == "" and pg.inner_text("#btn-newcall") == "Back to call")
    check("…and says what will happen", "goes on hold" in pg.inner_text("#hint"))
    check("…with Hang up still there for the current call", pg.is_visible("#btn-hang") and "Connected" in state())
    pg.click("#keys button[data-k='5']"); pg.click("#keys button[data-k='5']"); pg.wait_for_timeout(150)
    check("now the keys type the number", pg.input_value("#number") == "55")
    check("…and contacts can be dialled", pg.is_enabled(dana))
    pg.screenshot(path=os.path.join(OUT, "multicall_dialmode.png"))
    pg.fill("#number", "5552001"); pg.click("#btn-call")
    check("line 2 dials while line 1 is on a call", wait(lambda: "SALES DEMO" in state().upper() and "Connected" in state()))
    check("line 1's call is on hold in the strip", wait(lambda: "Front desk" in strip() and "On hold" in strip()))
    check("one call has the audio", wait(lambda: numbers(talking()) == ["5552001"] and len(lines.router.foreground) == 1))
    check("the dialer closes again once the call is placed", pg.is_hidden("#btn-call") and pg.inner_text("#btn-newcall") == "New call")

    # ---- a second and third call on the same line ----
    pg.click("#btn-newcall"); pg.wait_for_timeout(200)
    check("New call opens the dialer", pg.is_visible("#btn-call") and pg.input_value("#number") == "")
    pg.click("#btn-newcall"); pg.wait_for_timeout(200)
    check("Back to call closes it", pg.is_hidden("#btn-call") and "touch tones" in pg.inner_text("#hint"))
    pg.locator("#number").press_sequentially("5552002"); pg.wait_for_timeout(200)
    check("typing a number also opens the dialer", pg.is_visible("#btn-call") and pg.input_value("#number") == "5552002")
    pg.keyboard.press("Enter")
    check("second call on line 2 connects", wait(lambda: numbers(talking()) == ["5552002"]))
    check("both earlier calls are held", wait(lambda: strip().count("On hold") == 2))
    check("picker counts each line's calls", wait(lambda: picker() == ["● Front desk (1)", "● Sales demo (2)"]))
    pg.click("#btn-newcall"); pg.wait_for_timeout(150)
    pg.click(dana)
    check("a contact dials the third call", wait(lambda: numbers(talking()) == ["5557000"] and strip().count("On hold") == 3))
    check("line 2 shows as full", wait(lambda: picker()[1] == "● Sales demo (3, full)"))
    pg.screenshot(path=os.path.join(OUT, "multicall_four_calls.png"))
    pg.click("#btn-newcall"); pg.fill("#number", "5552004"); pg.click("#btn-call")
    check("a fourth call on that line is refused with a reason", wait(lambda: "already has 3 calls" in pg.inner_text("#err")))
    check("…and nothing changed", numbers(talking()) == ["5557000"] and len(live()) == 4)

    # ---- inbound on a busy line: rings, answering holds the rest ----
    sw1.call_phone()
    check("a call rings on line 1 while talking on line 2", wait(lambda: "Ringing" in strip() and pg.is_visible("#otherlines .ol-row.ringing")))
    pg.click("#otherlines .ol-row.ringing button.ans")
    check("answering it puts the talking call on hold", wait(lambda: numbers(talking()) == ["5550001111"] and strip().count("On hold") == 4))
    check("main area shows the answered call on its line", "FRONT DESK" in state().upper() and "Connected" in state())
    check("still only one call with the audio", len(lines.router.foreground) == 1)

    # ---- hang up the call on screen: the others stay on hold ----
    pg.click("#btn-hang")
    check("Hang up ends only the call on screen", wait(lambda: len(live()) == 4 and not talking()))
    check("nobody talking: a held call is on screen with Resume", wait(lambda: "On hold" in state() and pg.inner_text("#btn-hold") == "Resume"))
    check("…and the dialer is open (Call next to Hang up)", pg.is_visible("#btn-call") and pg.is_visible("#btn-hang") and pg.is_hidden("#btn-newcall"))
    pg.click("#otherlines .ol-row:has-text('Front desk') button:has-text('Resume')")
    check("Resume in the strip brings line 1's call back", wait(lambda: numbers(talking()) == ["5551001"] and "FRONT DESK" in state().upper()))
    pg.click("#otherlines .ol-row:has-text('5552001') button.end")
    check("a held call can be ended from the strip", wait(lambda: "5552001" not in numbers(live())))
    check("line 2 has room again", wait(lambda: picker()[1] == "● Sales demo (2)"))
    groups = pg.eval_on_selector_all("#thread .callgrp summary b", "bs => bs.map(b => b.textContent)")
    held_note = "another call has the audio" in pg.inner_text("#thread")
    check(f"each call keeps its own block; held calls say why ({len(groups)} blocks)", len(groups) >= 2 and held_note)

    # ---- narrow side panel ----
    pg.set_viewport_size({"width": 360, "height": 900}); pg.wait_for_timeout(300)
    pg.screenshot(path=os.path.join(OUT, "multicall_narrow_tab.png"))
    pg.goto(base + "/?embed=1"); pg.wait_for_timeout(1500)                 # the extension's side panel
    fits = pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    row = pg.evaluate("(() => { const r = [...document.querySelectorAll('#incall button:not([hidden])')].map(b => b.getBoundingClientRect()); return r.every(x => Math.abs(x.top - r[0].top) < 2) && r.every(x => x.width > 60); })()")
    check("fits the 360 px side panel, four in-call buttons on one row", fits and row)
    check("the page picks the calls up again after a reload", "Connected" in state() and strip().count("On hold") == 2)
    strip_ok = pg.evaluate("""(() => [...document.querySelectorAll('#otherlines .ol-row')].every(r => {
        const box = r.getBoundingClientRect(), who = r.querySelector('.ol-who').getBoundingClientRect();
        return [...r.querySelectorAll('button')].every(b => { const x = b.getBoundingClientRect(); return x.right <= box.right + 1 && x.height < 34; })
               && who.width > 60; }))()""")
    check("strip rows keep the caller readable and the buttons inside the row", strip_ok)
    header_one_row = pg.evaluate("(() => { const r = [...document.querySelectorAll('header.top > *')].filter(e => e.offsetParent).map(e => e.getBoundingClientRect()); return r.every(x => Math.abs(x.top + x.height / 2 - (r[0].top + r[0].height / 2)) < 12); })()")
    check("header stays on one row with calls counted in the picker", header_one_row)
    pg.screenshot(path=os.path.join(OUT, "multicall_narrow.png"))
    check(f"no script errors ({errors})", not errors)
    b.close()
srv.shutdown(); srv.server_close(); lines.stop()
for s_ in (sw1, sw2): s_.stop.set()
print("ALL MULTICALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

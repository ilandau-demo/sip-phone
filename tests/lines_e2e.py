"""Browser: several SIP accounts — header account picker, a call ringing on a
second line while talking on the first, answering it holds the first, the
other-lines strip, Re-register with its answer, adding a line in Settings,
and "Updated to …" as a one-time toast instead of a banner."""
import os, shutil, sys, tempfile, time, logging
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.WARNING)
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from sipphone import config_store, web
from sipphone.cli import load_config
from sipphone.lines import Lines, line_configs
from sipphone.phone import SIPPhone
from sipphone.audio import open_audio
from playwright.sync_api import sync_playwright

fails = []
def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

def wait(fn, t=8):
    end = time.time() + t
    while time.time() < end:
        if fn(): return True
        time.sleep(0.05)
    return False

d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini"); shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
sw1, sw2, sw3 = MockSwitch(), MockSwitch(), MockSwitch()
for s_ in (sw1, sw2, sw3): s_.start()
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
                history_path=f"{d}/h", config_path=p, warnings=["Updated to Telvoro 2.6.0."])
base = f"http://127.0.0.1:{srv.server_address[1]}"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
check("both lines register", wait(lambda: all(l["registered"] for l in lines.info())))

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 440, "height": 900})
    pg.goto(base + "/"); pg.wait_for_timeout(1500)
    check("'Updated to' is not a lasting banner", pg.is_hidden("#warnbar") or "Updated to" not in pg.inner_text("#warnbar"))
    check("…it shows once as a toast", "Updated to Telvoro 2.6.0" in pg.inner_text("#toasts"))
    check("header has an account picker with both lines", pg.is_visible("#linepick") and
          pg.eval_on_selector_all("#linepick option", "os => os.map(o => o.textContent)") == ["● Front desk", "● Sales demo"])
    pg.select_option("#linepick", "2"); pg.wait_for_timeout(600)
    check("choosing Sales demo makes it the current line", lines.current == 2)
    # Re-register the current line
    pg.click("#reregister"); pg.wait_for_timeout(200)
    check("Re-register shows it's working", "Registering" in pg.inner_text("#reregister") or "registered" in pg.inner_text("#toasts"))
    check("…then reports the answer", wait(lambda: "Sales demo registered" in pg.inner_text("#toasts"), 6))
    # a call on line 1
    sw1.call_phone()
    pg.wait_for_selector("#btn-answer:not([hidden])", timeout=5000)
    pg.click("#btn-answer")
    check("line 1 call connected", wait(lambda: "Connected" in pg.inner_text("#callstate")))
    check("…labelled with its line", "FRONT DESK" in pg.inner_text("#callstate").upper())
    # line 2 rings meanwhile
    sw2.call_phone()
    pg.wait_for_selector("#otherlines .ol-row.ringing", timeout=5000)
    row = pg.inner_text("#otherlines")
    check(f"second line rings in the strip ({row!r})", "Sales demo" in row and "Ringing" in row)
    pg.screenshot(path=os.path.join(OUT, "lines_ringing.png"))
    pg.click("#otherlines .ol-row.ringing button.ans")
    check("answering line 2 puts line 1 on hold", wait(lambda: "On hold" in pg.inner_text("#otherlines") and "Front desk" in pg.inner_text("#otherlines")))
    check("main area now shows Sales demo connected", wait(lambda: "SALES DEMO" in pg.inner_text("#callstate").upper() and "Connected" in pg.inner_text("#callstate")))
    check("only one call has the audio", len(lines.router.foreground) == 1)
    pg.screenshot(path=os.path.join(OUT, "lines_two_calls.png"))
    pg.click("#otherlines button:has-text('Resume')")
    check("Resume swaps: line 1 back, line 2 held", wait(lambda: "FRONT DESK" in pg.inner_text("#callstate").upper() and "Sales demo" in pg.inner_text("#otherlines") and "On hold" in pg.inner_text("#otherlines")))
    pg.click("#otherlines button.end")
    check("hanging up the held line leaves line 1", wait(lambda: pg.is_hidden("#otherlines") and "Connected" in pg.inner_text("#callstate")))
    pg.click("#btn-hang")
    check("line 1 hung up", wait(lambda: "No call" in pg.inner_text("#callstate")))
    pg.wait_for_timeout(800)
    groups = pg.eval_on_selector_all("#thread .callgrp summary b", "bs => bs.map(b => b.textContent)")
    check(f"call screen keeps the last finished call, labelled with its line ({groups})", groups and groups[-1].startswith("Front desk · "))
    # Settings → add line 3
    pg.click("#btn-settings"); pg.wait_for_timeout(800)
    pg.locator(".s-line[data-line='3'] summary").click()
    pg.fill("[data-k='line3.label']", "Support demo")
    pg.fill("[data-k='line3.username']", USER); pg.fill("[data-k='line3.auth_username']", AUTH)
    pg.fill("[data-k='line3.password']", PASS); pg.fill("[data-k='line3.domain']", REALM)
    pg.fill("[data-k='line3.proxy']", "127.0.0.1"); pg.fill("[data-k='line3.proxy_port']", str(sw3.port))
    pg.click("#s-save")
    check("saving adds line 3 live", wait(lambda: 3 in lines.lines and lines.lines[3].phone.registered, 8))
    pg.wait_for_timeout(1200)
    ok3 = wait(lambda: pg.inner_text(".s-line[data-line='3'] .ln-sub") == "registered", 5)
    check(f"Settings shows line 3 registered ({pg.inner_text('.s-line[data-line=\'3\'] .ln-sub')!r})", ok3)
    pg.locator(".s-line[data-line='3']").scroll_into_view_if_needed()
    pg.screenshot(path=os.path.join(OUT, "lines_settings.png"))
    pg.locator("#s-reglog summary").click(); pg.wait_for_timeout(600)
    rl = pg.inner_text("#s-reglog-list")
    check("registration history names the lines", "Sales demo:" in rl and "Support demo:" in rl)
    check("password stored protected", "line3" in open(p).read() and PASS not in open(p).read().split("[line3]")[1].split("[")[0])
    pg.click("#s-close"); pg.wait_for_timeout(1500)
    check("picker now lists three accounts", pg.eval_on_selector_all("#linepick option", "os => os.length") == 3)
    check("header stays one line: no 'is registered' text when all lines are up", pg.inner_text("#regtext").strip() == "")
    pg.screenshot(path=os.path.join(OUT, "lines_header.png"))
    b.close()
srv.shutdown(); srv.server_close(); lines.stop()
for s_ in (sw1, sw2, sw3): s_.stop.set()
print("ALL LINES CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

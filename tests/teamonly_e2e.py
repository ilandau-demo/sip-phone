"""End-to-end: Team view without the desktop app.

Loads the unpacked extension with no native host registered, points it at the
mock Xsi server through the side panel's setup form, and checks that presence
arrives in the panel and on the badge, that pushed events update it live, and
that turning it off goes back to the install message.
"""
import json, os, sys, tempfile, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from tests.mock_xsi_events import MockXsi, USER, PW, call_xml
from tests.test_presence_engine import TEAM, ALICE, BOB, CAROL
from sipphone import extension_sync
from playwright.sync_api import sync_playwright

work = tempfile.mkdtemp()
EXT = os.path.join(work, "SIP Phone Extension")
extension_sync.sync(EXT)
EXT_ID = open(os.path.join(ROOT, ".extkey", "id.txt")).read().strip()
mock_port_holder = {}
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
HG = "hg_sales@nextiva.com"
mock = MockXsi(TEAM, directory=TEAM + [(HG, "Sales", "Hunt Group", "1100")], dnd={CAROL: True})
mock.calls[BOB] = [call_xml("c-bob", "Active", "Originator", "Acme Lab", "6165559999", time.time() - 40)]
fails = []
# headless Chromium can't show the permission prompt, so the mock's origin is a
# required host permission in this test copy (a real user clicks Allow once)
_m = json.load(open(os.path.join(EXT, "manifest.json"))); _m["host_permissions"].append(f"http://127.0.0.1:{mock.port}/*")
json.dump(_m, open(os.path.join(EXT, "manifest.json"), "w"), indent=2)

def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

with sync_playwright() as pw:
    profile = os.path.join(work, "profile")
    ctx = pw.chromium.launch_persistent_context(profile, headless=True, channel="chromium",
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
    swk = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    pump = ctx.pages[0] if ctx.pages else ctx.new_page()
    def wait(fn, t=10):
        end = time.time() + t
        while time.time() < end:
            if fn(): return True
            pump.wait_for_timeout(200)
        return False
    def ev(expr):
        for w in reversed(ctx.service_workers):
            if EXT_ID in w.url:
                try: return w.evaluate(expr)
                except Exception: continue
    badge = lambda: ev("chrome.action.getBadgeText({})")

    # the optional host permission is granted up front (a real user clicks Allow)
    pg = ctx.new_page(); pg.goto(f"chrome-extension://{EXT_ID}/sidepanel.html")
    pg.wait_for_timeout(1500)
    check("without the app the panel asks to install it", "Install" in pg.inner_text("#msg-title"))
    check("…and offers Team view without install", pg.is_visible("#msg-team"))
    pg.click("#msg-team")
    check("setup form opens", pg.is_visible("#setup"))
    pg.fill("#su-host", mock.base); pg.fill("#su-user", USER); pg.fill("#su-pass", PW)
    pg.check('#su-svc input[data-k="hunt_group"]')
    pg.click("#su-check")
    check("Check reports the login and lists", wait(lambda: "Login OK" in pg.inner_text("#su-msg")), )
    print("   ", pg.inner_text("#su-msg"))
    pg.click("#su-save")
    check("team view shows", wait(lambda: pg.is_visible("#team")))
    check("teammates arrive with status", wait(lambda: "With Acme Lab - 6165559999" in pg.inner_text("#team-list"), 15))
    check("Alice is Available (never unknown-as-idle)", "Available" in pg.inner_text("#team-list"))
    txt = pg.inner_text("#team-list")
    check("Carol is Do Not Disturb", "Do Not Disturb" in txt)
    check("hunt group listed as a service", "Sales Hunt Group" in txt and "x1100" in txt)
    check("footer says live", wait(lambda: "live" in pg.inner_text("#team-foot")))
    check("badge shows 1 on a call", wait(lambda: badge() == "1"))
    pg.screenshot(path=os.path.join(OUT, "teamonly_1.png"))
    # search-as-you-type, filters, details, a notification rule
    pg.fill("#team-q", "car"); pg.wait_for_timeout(300)
    check("search narrows to Carol", "Carol Chen" in pg.inner_text("#team-list") and "Bob Brown" not in pg.inner_text("#team-list"))
    check("match is highlighted", pg.locator("#team-list mark").count() == 1)
    pg.click("#team-qx"); pg.wait_for_timeout(300)
    pg.click("#team-chips button:has-text('On a call')"); pg.wait_for_timeout(300)
    check("filter chip shows only Bob", "Bob Brown" in pg.inner_text("#team-list") and "Alice" not in pg.inner_text("#team-list"))
    pg.click("#team-chips .reset"); pg.wait_for_timeout(300)
    pg.click("#team-list .mate .nm:has-text('Alice Adams')"); pg.wait_for_timeout(300)
    check("details dialog opens with fields", pg.is_visible("#pdlg") and "1001" in pg.inner_text("#pd-fields") and "Sales" in pg.inner_text("#pd-fields"))
    pg.check("#pd-watch-opts input[value=after-call]"); pg.click("#pdlg button[value=close]"); pg.wait_for_timeout(500)
    check("rule saved and bell lit", pg.locator("#team-list .mate .bell.on").count() == 1)
    check("rule in storage", wait(lambda: "alice1001@nextiva.com" in (ev("chrome.storage.local.get('teamWatch').then(v => Object.keys(v.teamWatch || {}))") or [])))
    mock.push(ALICE, "CallReceivedEvent", call_xml("c-al", "Alerting", "Terminator", "Dana", "6165551234"))
    check("pushed ringing shows within 2 s", wait(lambda: "Incoming · Dana" in pg.inner_text("#team-list"), 2.5))
    check("badge RINGs", wait(lambda: badge() == "RING"))
    mock.push(ALICE, "CallAnsweredEvent", call_xml("c-al", "Active", "Terminator", "Dana", "6165551234"))
    check("answered → on a call", wait(lambda: "From Dana" in pg.inner_text("#team-list"), 3))
    mock.push(ALICE, "CallReleasedEvent", call_xml("c-al", "Released", "Terminator"))
    check("ringing clears", wait(lambda: "Dana" not in pg.inner_text("#team-list")))
    check("after-call notification raised", wait(lambda: ev("new Promise(r => chrome.notifications.getAll(r)).then(all => Object.keys(all))") and
                                                 any(k.startswith("watch-alice") for k in ev("new Promise(r => chrome.notifications.getAll(r)).then(all => Object.keys(all))") or []), 5))
    check("one-shot rule consumed", wait(lambda: "alice1001@nextiva.com" not in (ev("chrome.storage.local.get('teamWatch').then(v => Object.keys(v.teamWatch || {}))") or [])))
    check("every event acknowledged", wait(lambda: len(mock.acks) >= 8))
    # the panel reopened later goes straight to the team view
    pg2 = ctx.new_page(); pg2.goto(f"chrome-extension://{EXT_ID}/sidepanel.html")
    check("reopened panel shows the team", wait(lambda: pg2.is_visible("#team") and "Bob Brown" in pg2.inner_text("#team-list")))
    pg2.close()
    # turn it off
    pg.click("#team-gear"); check("gear opens settings", pg.is_visible("#setup") and pg.is_visible("#su-off"))
    pg.click("#su-off")
    check("off: back to the install message", wait(lambda: pg.is_visible("#msg") and "Install" in pg.inner_text("#msg-title")))
    check("off: channel closed on the server", wait(lambda: not mock.channels and not mock.subs, 10))
    check("off: badge cleared", wait(lambda: badge() == "!"))
    ctx.close()
mock.stop()
print("ALL TEAM-ONLY CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

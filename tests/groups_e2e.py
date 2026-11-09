"""Browser: group filter chips on the Team tab, and renaming a group from Settings."""
import os, shutil, sys, tempfile, time, logging
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.WARNING)
from tests.mock_xsi_events import MockXsi, USER as XU, PW as XPW
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from tests.test_presence_engine import TEAM
from sipphone import config_store, web
from sipphone.cli import load_config, make_monitor
from sipphone.phone import SIPPhone
from sipphone.audio import open_audio
from playwright.sync_api import sync_playwright

fails = []
def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

HG = "hg_sales@nextiva.com"; HG2 = "hg_billing@nextiva.com"; CC = "cc_support@nextiva.com"
d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini"); shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
sw = MockSwitch(); sw.start()
mock = MockXsi(TEAM, directory=TEAM + [(HG, "hg_sales", "Hunt Group", "1100"), (HG2, "hg_billing", "Hunt Group", "1101"),
                                       (CC, "cc_support", "Call Center", "1200")])
from tests.test_presence_engine import ALICE, BOB, CAROL
mock.groups = {ALICE: "3149647G", BOB: "3149647G", CAROL: "5550001G"}
config_store.update(p, {"app": {"mode": "presence"},
                        "account": {"username": USER, "auth_username": AUTH, "password": PASS, "domain": REALM,
                                    "proxy": "127.0.0.1", "proxy_port": str(sw.port)},
                        "local": {"bind_port": "0"}, "audio": {"backend": "null"},
                        "presence": {"mode": "events", "xsi_user": XU, "xsi_password": XPW},
                        "admin": {"xsi_host": mock.base, "verify_tls": "false", "show_services": "hunt_group, call_center"}})
cfg, _, adm = load_config(p)
adm["cache_path"] = os.path.join(d, "directory-cache.json")
phone = SIPPhone(cfg, audio=open_audio(kind="null")); phone.presence_only = True; phone.start()
mon = make_monitor(adm, phone)
srv = web.serve(phone, phone.audio, port=0, open_browser=False, phrases_path=f"{d}/p", contacts_path=f"{d}/c",
                history_path=f"{d}/h", config_path=p, monitor=mon, started_mode="presence",
                build_monitor=lambda o: make_monitor(o, phone))
base = f"http://127.0.0.1:{srv.server_address[1]}"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 420, "height": 860})
    pg.goto(base + "/"); pg.wait_for_timeout(1200); pg.click("#tab-team")
    end = time.time() + 15
    while time.time() < end and "hg_sales" not in pg.inner_text("#team"): pg.wait_for_timeout(100)
    t = pg.inner_text("#team")
    check("groups listed under their kind", "hg_sales" in t and "cc_support" in t)
    chips = pg.inner_text("#team-chips")
    check("a chip per group kind with counts", "Hunt groups" in chips and "Call centers" in chips)
    pg.click("#team-chips button.grp:has-text('Hunt Group')"); pg.wait_for_timeout(300)
    t = pg.inner_text("#team")
    check("Hunt Group chip shows only hunt groups", "hg_sales" in t and "hg_billing" in t and "cc_support" not in t and "Alice" not in t)
    pg.fill("#team-q", "bill"); pg.wait_for_timeout(300)
    t = pg.inner_text("#team")
    check("search narrows within the filter", "hg_billing" in t and "hg_sales" not in t)
    pg.click("#team-chips .reset"); pg.wait_for_timeout(300)
    check("reset brings everyone back", "Alice" in pg.inner_text("#team") and "cc_support" in pg.inner_text("#team"))
    # Settings → rename
    pg.click("#btn-settings") if pg.locator("#btn-settings").count() else pg.click("[title='Settings']")
    pg.wait_for_timeout(800)
    opts = pg.eval_on_selector_all("#s-svn-pick option", "os => os.map(o => o.textContent)")
    check("Settings lists every group found", any("hg_sales" in o for o in opts) and any("cc_support" in o for o in opts))
    pg.select_option("#s-svn-pick", HG); pg.wait_for_timeout(200)
    check("name field appears with the directory name as placeholder", pg.is_visible("#s-svn-name") and "hg_sales" in pg.get_attribute("#s-svn-name", "placeholder"))
    pg.fill("#s-svn-name", "Sales Line"); pg.click("#s-svn-save"); pg.wait_for_timeout(600)
    check("saved message", "Saved" in pg.inner_text("#s-svn-msg"))
    opts = pg.eval_on_selector_all("#s-svn-pick option", "os => os.map(o => o.textContent)")
    check("dropdown shows new and old name", any("Sales Line (hg_sales" in o for o in opts))
    # people's group: 3149647G -> Arizona
    opts = pg.eval_on_selector_all("#s-org-pick option", "os => os.map(o => o.textContent)")
    check("Settings lists people's groups with member counts", any("3149647G · 2 people" in o for o in opts) and any("5550001G · 1 person" in o for o in opts))
    pg.select_option("#s-org-pick", "3149647G"); pg.wait_for_timeout(200)
    pg.fill("#s-org-name", "Arizona"); pg.press("#s-org-name", "Enter"); pg.wait_for_timeout(600)
    check("group name saved", "Arizona" in pg.inner_text("#s-org-msg"))
    pg.locator("#s-org").scroll_into_view_if_needed()
    pg.screenshot(path=os.path.join(OUT, "groups_settings.png"))
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    end = time.time() + 5
    while time.time() < end and "Sales Line" not in pg.inner_text("#team"): pg.wait_for_timeout(100)
    t = pg.inner_text("#team")
    check("Team tab shows the display name (pushed)", "Sales Line" in t and "hg_sales" not in t)
    pg.fill("#team-q", "sales line"); pg.wait_for_timeout(300)
    check("search finds the display name", "Sales Line" in pg.inner_text("#team"))
    pg.fill("#team-q", "arizona"); pg.wait_for_timeout(300)
    t = pg.inner_text("#team")
    check("search by group display name finds its people", "Alice" in t and "Bob" in t and "Carol" not in t)
    pg.fill("#team-q", ""); pg.wait_for_timeout(200)
    pg.screenshot(path=os.path.join(OUT, "groups_team.png"))
    pg.click("#team .teammate .nm2:has-text('Alice Adams')"); pg.wait_for_timeout(500)
    f = pg.inner_text("#pd-fields")
    check("details show Group: Arizona", "Arizona" in f and "3149647G" not in f)
    bg = pg.eval_on_selector("#pd-dot", "e => getComputedStyle(e).backgroundColor")
    grey = pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--ink-3')")
    check(f"details dot is coloured for Available ({bg})", bg not in ("rgba(0, 0, 0, 0)",) and "154" not in bg)
    pg.screenshot(path=os.path.join(OUT, "groups_details.png"))
    b.close()
check("name saved to group-names.json", "Sales Line" in open(os.path.join(d, "group-names.json")).read())
check("group name saved to org-group-names.json", "Arizona" in open(os.path.join(d, "org-group-names.json")).read())
srv.shutdown(); srv.server_close(); mon.stop(); phone.stop(); sw.stop.set(); mock.stop()
print("ALL GROUP CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

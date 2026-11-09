"""End-to-end: the phone page gets presence pushed (no polling) and paints a
teammate's change within a fraction of a second; with the stream blocked it
falls back to polling and still works."""
import os, shutil, sys, tempfile, time, logging
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
logging.basicConfig(level=logging.WARNING)
from tests.mock_xsi_events import MockXsi, USER as XU, PW as XPW, call_xml
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from tests.test_presence_engine import TEAM, ALICE, BOB
from sipphone import config_store, web
from sipphone.cli import load_config, make_monitor
from sipphone.phone import SIPPhone
from sipphone.audio import open_audio
from sipphone.presence import manager as pm
from playwright.sync_api import sync_playwright

pm.HEARTBEAT = 0.5
fails = []
def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond: fails.append(label)

d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini"); shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
sw = MockSwitch(); sw.start()
mock = MockXsi(TEAM)
config_store.update(p, {"app": {"mode": "presence"},
                        "account": {"username": USER, "auth_username": AUTH, "password": PASS, "domain": REALM,
                                    "proxy": "127.0.0.1", "proxy_port": str(sw.port), "display_name": "Front Desk"},
                        "local": {"bind_port": "0"}, "audio": {"backend": "null"},
                        "presence": {"mode": "events", "xsi_user": XU, "xsi_password": XPW},
                        "admin": {"xsi_host": mock.base, "verify_tls": "false"}})
cfg, _, adm = load_config(p)
phone = SIPPhone(cfg, audio=open_audio(kind="null")); phone.presence_only = True; phone.start()
mon = make_monitor(adm, phone)
srv = web.serve(phone, phone.audio, port=0, open_browser=False, phrases_path=f"{d}/p", contacts_path=f"{d}/c",
                history_path=f"{d}/h", config_path=p, monitor=mon, started_mode="presence",
                build_monitor=lambda o: make_monitor(o, phone))
base = f"http://127.0.0.1:{srv.server_address[1]}"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()

def paint_latency(pg, push, needle, timeout=3.0):
    t0 = time.time(); push()
    while time.time() - t0 < timeout:
        if needle in pg.inner_text("#team"): return time.time() - t0
        pg.wait_for_timeout(5)
    return None

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 420, "height": 760})
    reqs = []
    pg.on("request", lambda r: reqs.append(r.url))
    pg.goto(base + "/")
    pg.wait_for_timeout(1500)
    pg.click("#tab-team")
    end = time.time() + 15
    while time.time() < end and "live" not in pg.inner_text("#team"): pg.wait_for_timeout(100)
    foot = pg.inner_text("#team")
    check("team tab shows live (pushed, not polling)", "live" in foot and "(polling)" not in foot)
    check("page opened the stream", any(u.endswith("/api/stream?since=0") or "/api/stream" in u for u in reqs))
    reqs.clear(); pg.wait_for_timeout(2500)
    check("no /api/monitor or /api/state polling while streaming", not any("/api/monitor" in u or "/api/state?" in u for u in reqs))
    lat = []
    for i in range(5):
        cid = f"s{i}"
        l1 = paint_latency(pg, lambda: mock.push(ALICE, "CallReceivedEvent", call_xml(cid, "Alerting", "Terminator", f"Caller{i}", f"616555000{i}")), f"Caller{i}")
        l2 = paint_latency(pg, lambda: mock.push(ALICE, "CallReleasedEvent", call_xml(cid, "Released", "Terminator")), "Available") if l1 is not None else None
        if l1 is not None: lat.append(l1)
        if l2 is not None: lat.append(l2)
        pg.wait_for_timeout(150)
    lat_ms = [round(x * 1000) for x in lat]
    print("    push → painted (ms):", lat_ms)
    check("10 changes painted", len(lat) == 10)
    check("every change painted within 150 ms", lat and max(lat) < 0.15)
    pg.wait_for_timeout(500)
    check("footer reports the update time", "updates in" in pg.inner_text("#team"))
    pg.screenshot(path=os.path.join(OUT, "stream_1.png"))
    # fallback: block the stream, the page must keep working on polling
    pg2 = b.new_page(viewport={"width": 420, "height": 760})
    pg2.route("**/api/stream*", lambda route: route.abort())
    reqs2 = []; pg2.on("request", lambda r: reqs2.append(r.url))
    pg2.goto(base + "/"); pg2.wait_for_timeout(1500); pg2.click("#tab-team")
    end = time.time() + 15
    while time.time() < end and "polling" not in pg2.inner_text("#team"): pg2.wait_for_timeout(100)
    check("stream blocked → footer says polling", "(polling)" in pg2.inner_text("#team"))
    check("…and it polls /api/monitor", any("/api/monitor" in u for u in reqs2))
    l = paint_latency(pg2, lambda: mock.push(BOB, "CallReceivedEvent", call_xml("f1", "Alerting", "Terminator", "Fallback", "6165559911")), "Fallback", 4)
    check(f"change still shows on polling ({l and round(l*1000)} ms)", l is not None and l < 2.5)
    # the first page is unaffected
    check("streaming page still live", "live" in pg.inner_text("#team") and "(polling)" not in pg.inner_text("#team"))
    h = __import__("json").loads(pg.evaluate("fetch('/api/presence/health').then(r => r.text())"))
    print("    health.latency:", {k: h["latency"][k] for k in ("engine", "ack_rtt", "platform")}, "channels", h["channels"])
    b.close()
srv.shutdown(); srv.server_close(); mon.stop(); phone.stop(); sw.stop.set(); mock.stop()
print("ALL STREAM CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)

"""End-to-end: the Chrome extension + native host + phone + mock Nextiva switch.

Loads the unpacked extension into Chromium, registers the native host in the
profile, and checks: bridge connects, side panel embeds the phone, right-click
calling places a real SIP call, an incoming call turns the badge to RING and
raises an Answer/Decline notification, answering works, and quitting the app
is reflected in the panel.
"""
import json, os, stat, sys, tempfile, threading, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
work = tempfile.mkdtemp()
os.environ["HOME"] = work                      # the app saves extension zips to ~/Downloads
os.makedirs(os.path.join(work, "Downloads"))
from tests.test_flows import MockSwitch, USER, PASS, AUTH, REALM
from sipphone.phone import Config, SIPPhone, CallState
from sipphone.audio import open_audio
from sipphone import web
from playwright.sync_api import sync_playwright

from sipphone import extension_sync, __version__
EXT = os.path.join(tempfile.mkdtemp(), "SIP Phone Extension")
extension_sync.sync(EXT)                      # load it the way users do: from the synced folder
# simulate an install made by an older app version: the self-update check runs later
_m = json.load(open(os.path.join(EXT, "manifest.json"))); _m["version"] = "1.0.0"
json.dump(_m, open(os.path.join(EXT, "manifest.json"), "w"), indent=2)
json.dump({"via": "folder", "build": "old0000000000000", "version": "1.0.0"}, open(os.path.join(EXT, "install.json"), "w"))
EXT_ID = open(os.path.join(ROOT, ".extkey", "id.txt")).read().strip()
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()

# phone + UI on the real port the extension uses
sw = MockSwitch(); sw.start()
cfg = Config(username=USER, password=PASS, domain=REALM, auth_username=AUTH, proxy="127.0.0.1",
             proxy_port=sw.port, bind_ip="127.0.0.1", bind_port=0, display_name="Demo Desk",
             rtp_port_range=(34000, 35000))
phone = SIPPhone(cfg, audio=open_audio(kind="null")); phone.start()
assert phone.register(wait=True)
quit_flag = {"q": False}
cfg_path = os.path.join(work, "config.ini")
open(cfg_path, "w").write(f"[account]\nusername = {USER}\nauth_username = {AUTH}\npassword = {PASS}\n"
                          f"domain = {REALM}\nproxy = 127.0.0.1\nproxy_port = {sw.port}\ndisplay_name = Demo Desk\n"
                          f"[local]\nbind_port = 0\n[audio]\nbackend = null\n")
restarts = []
def on_quit():
    quit_flag["q"] = True
    srv.shutdown(); srv.server_close()
def on_restart():
    # stand-in for the real restart: go away for a moment, come back on the same port
    global srv
    restarts.append(1)
    srv.shutdown(); srv.server_close()
    time.sleep(2.5)
    srv = start_ui()
def start_ui():
    return web.serve(phone, phone.audio, port=8088, open_browser=False, on_quit=on_quit,
                     phrases_path=f"{work}/p.txt", contacts_path=f"{work}/c.json", history_path=f"{work}/h.json",
                     config_path=cfg_path, on_restart=lambda: threading.Thread(target=on_restart).start())
srv = start_ui()

# native host registered in the browser profile
profile = os.path.join(work, "profile")
os.makedirs(os.path.join(profile, "NativeMessagingHosts"))
wrapper = os.path.join(work, "host.sh")
cfg_home = os.path.join(work, "xdg")                       # the host's config dir (for update.lock)
open(wrapper, "w").write(f"#!/bin/sh\ncd {ROOT}\nexport XDG_CONFIG_HOME={cfg_home}\n"
                         f"exec {sys.executable} -m sipphone.native_host --no-autostart\n")
os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IEXEC)
json.dump({"name": "com.sipphone.host", "description": "SIP Phone", "path": wrapper, "type": "stdio",
           "allowed_origins": [f"chrome-extension://{EXT_ID}/"]},
          open(os.path.join(profile, "NativeMessagingHosts", "com.sipphone.host.json"), "w"))

def press(loc):
    # Headless Chromium doesn't route mouse input to the lower part of a
    # cross-origin iframe (the side panel embeds the phone page), so buttons
    # near the bottom are clicked from script. Real Chrome is unaffected.
    loc.wait_for(state="visible", timeout=10000)
    loc.evaluate("b => b.click()")

def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        raise SystemExit(1)

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(profile, headless=True, channel="chromium",
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
    # like a real user: Developer mode is on (needed for Load unpacked, and for
    # an unpacked extension to stay enabled when it reloads itself)
    _ep = ctx.new_page(); _ep.goto("chrome://extensions")
    _ep.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode: true})"); _ep.close()
    swk = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    check(f"extension loaded with fixed id {EXT_ID}", EXT_ID in swk.url)

    def badge():
        return swk.evaluate("chrome.action.getBadgeText({})")
    pump = ctx.pages[0] if ctx.pages else ctx.new_page()
    def wait(fn, t=10):
        end = time.time() + t
        while time.time() < end:
            if fn(): return True
            pump.wait_for_timeout(200)          # lets Playwright see service workers come and go
        return False

    def ev(expr):
        """Evaluate in whichever service worker is current (it restarts on reload)."""
        for w in reversed(ctx.service_workers):
            if EXT_ID in w.url:
                try:
                    return w.evaluate(expr)
                except Exception:
                    continue
        return None
    def cur():
        for w in reversed(ctx.service_workers):
            if EXT_ID in w.url:
                try:
                    w.evaluate("1"); return w
                except Exception:
                    pass
    REG = "chrome.storage.session.get('status').then(x => !!(x.status && x.status.registered))"

    # The folder was loaded with files from an older app (1.0.0). Once the bridge
    # reports the app's version, the extension reloads itself once to pick up
    # newer files -- and doesn't loop when the folder hasn't been updated.
    check("a stale copy reloads once, then settles (no loop)",
          wait(lambda: ev("chrome.storage.local.get('reloadedFor').then(x => x.reloadedFor)") == extension_sync.build_id(), 15)
          and wait(lambda: ev(REG), 15))
    time.sleep(2)
    check("still on the stale files and still connected", ev("chrome.runtime.getManifest().version") == "1.0.0" and ev(REG))
    # now the app updates the folder (what it does on every start) -> self-update
    extension_sync.sync(EXT)
    ev("chrome.storage.local.remove('reloadedFor')")
    ev("self.maybeSelfUpdate('%s', '%s')" % (__version__, extension_sync.build_id()))
    check(f"extension reloaded itself onto the new files ({__version__})",
          wait(lambda: ev("chrome.runtime.getManifest().version") == __version__, 15))
    check("bridge connected and reports 'registered'", wait(lambda: ev(REG), 15))
    swk = cur()
    check("toolbar title shows display name",
          "Demo Desk" in swk.evaluate("chrome.action.getTitle({})"))

    # side panel page embeds the phone
    pg = ctx.new_page(); pg.set_viewport_size({"width": 400, "height": 820})
    pg.goto(f"chrome-extension://{EXT_ID}/sidepanel.html")
    check("side panel shows the phone UI", wait(lambda: pg.is_visible("#ui")))
    fr = pg.frame_locator("#ui")
    fr.locator("#regtext").wait_for(timeout=10000)
    check("embedded UI says 'Demo Desk is registered'",
          wait(lambda: "Demo Desk is registered" in fr.locator("#regtext").inner_text()))
    pg.screenshot(path=os.path.join(OUT, "ext1_sidepanel.png"))

    # number extraction + right-click calling
    check("extracts number from messy text",
          swk.evaluate("self.extractNumber('Call our office at (616) 555-0199 ext 2')") == "6165550199")
    r = swk.evaluate("self.callNumber('Call our office at (616) 555-0199')")
    check(f"'Call with Telvoro' places the call ({r})", r.get("ok") and r.get("number") == "6165550199")
    check("badge shows ON while connected", wait(lambda: badge() == "ON"))
    check("mock switch received the INVITE", "INVITE" in sw.events)
    pg.screenshot(path=os.path.join(OUT, "ext2_oncall.png"))
    swk.evaluate("self.request('hangup')")
    check("badge clears after hangup", wait(lambda: badge() == ""))

    # incoming call: badge + notification + answer
    sw.call_phone()
    check("badge shows RING on incoming call", wait(lambda: badge() == "RING"))
    notes = swk.evaluate("chrome.notifications.getAll()")
    check(f"Answer/Decline notification raised ({list(notes)})", "incoming" in notes)
    pg.screenshot(path=os.path.join(OUT, "ext3_incoming.png"))
    check("answer from the notification path works", swk.evaluate("self.request('answer')").get("ok"))
    check("badge ON after answering", wait(lambda: badge() == "ON"))
    check("notification cleared after answering",
          wait(lambda: "incoming" not in swk.evaluate("chrome.notifications.getAll()")))
    sw.send_bye_to_phone()
    check("badge clears when caller hangs up", wait(lambda: badge() == ""))

    # settings inside the side panel
    fr.locator("#btn-settings").click()
    check("gear opens Settings in the side panel", wait(lambda: fr.locator("#sdlg").evaluate("d => d.open")))
    check("password is not sent to the page",
          fr.locator('[data-k="account.password"]').input_value() == "" and
          "saved" in fr.locator('[data-k="account.password"]').get_attribute("placeholder"))
    fr.locator('[data-k="account.display_name"]').fill("Sales Demo")
    press(fr.locator("#s-save"))
    check("saving says so", wait(lambda: fr.locator("#s-msg").inner_text().startswith("Saved"), 10))
    check("header shows the new display name",
          wait(lambda: "Sales Demo is registered" in fr.locator("#regtext").inner_text()))
    check("toolbar title follows", wait(lambda: "Sales Demo" in swk.evaluate("chrome.action.getTitle({})")))
    check("config.ini updated", "display_name = Sales Demo" in open(cfg_path).read())
    fr.locator('[data-k="account.password"]').fill("wrong-one")
    press(fr.locator("#s-save"))
    check("a wrong password re-registers and reports the failure",
          wait(lambda: "registration failed" in fr.locator("#s-msg").inner_text(), 20))
    fr.locator('[data-k="account.password"]').fill(PASS)
    press(fr.locator("#s-save"))
    check("the right password registers again without a restart",
          wait(lambda: "Registered as Sales Demo" in fr.locator("#s-msg").inner_text(), 20))
    pg.screenshot(path=os.path.join(OUT, "ext5_settings.png"))
    fr.locator('[data-k="local.bind_port"]').evaluate("e => e.closest('details').open = true")
    fr.locator('[data-k="local.bind_port"]').fill("5071")
    press(fr.locator("#s-save"))
    check("restart offered for a port change", wait(lambda: fr.locator("#s-restart-btn").is_visible()))
    press(fr.locator("#s-restart-btn"))
    check("panel shows Restarting…", wait(lambda: pg.is_visible("#msg") and "Restarting" in pg.inner_text("#msg-title"), 10))
    pg.screenshot(path=os.path.join(OUT, "ext6_restarting.png"))
    check("panel reconnects after the restart", wait(lambda: pg.is_visible("#ui") and restarts == [1], 30))
    fr = pg.frame_locator("#ui")
    check("phone UI is back", wait(lambda: "is registered" in fr.locator("#regtext").inner_text(), 15))

    # options page "Open settings" lands on the Settings sheet
    op = ctx.new_page()
    op.goto(f"chrome-extension://{EXT_ID}/options.html")
    check("options page has an Open settings button", op.is_visible("#open-settings"))
    fr.locator("#sdlg").evaluate("d => d.close()")
    op.evaluate("chrome.storage.session.set({openSettings: Date.now()})")
    check("open side panel jumps to Settings",
          wait(lambda: fr.locator("#sdlg").evaluate("d => d.open"), 10))
    fr.locator("#s-close").click()
    op.close()

    # Team-view-only status: badge shows the team, never "OFF"
    BUILD = extension_sync.build_id()
    swk.evaluate("self.onHostMessage({type:'status', up:true, version:'%s', extension_build:'%s', mode:'presence', registered:false, "
                 "name:'Front Desk', call:null, team:{active:2, total:5, ringing:[]}})" % (__version__, BUILD))
    check("presence mode badge shows teammates on calls", wait(lambda: badge() == "2"))
    check("presence mode title says Team view", "Team view" in swk.evaluate("chrome.action.getTitle({})"))
    swk.evaluate("self.onHostMessage({type:'status', up:true, version:'%s', extension_build:'%s', mode:'presence', registered:false, "
                 "name:'Front Desk', call:null, team:{active:0, total:5, ringing:['Carol Chen']}})" % (__version__, BUILD))
    check("presence mode badge RINGs for a teammate", wait(lambda: badge() == "RING"))
    swk.evaluate("self.request('status')")
    check("back to the real status", wait(lambda: badge() == ""))

    # An update in progress: the bridge steps aside, the extension waits for the
    # app's port instead of reconnecting, then comes back by itself.
    os.makedirs(os.path.join(cfg_home, "sipphone"), exist_ok=True)
    lock = os.path.join(cfg_home, "sipphone", "update.lock")
    json.dump({"version": "9.9.9", "started": time.time()}, open(lock, "w"))
    srv.shutdown(); srv.server_close()                       # the app quits for the update
    check("extension reports 'updating' and the badge shows it",
          wait(lambda: ev("self.hostStateNow()") == "updating" and badge() == "…", 20))
    check("side panel says Updating", wait(lambda: pg.is_visible("#msg") and "Updating" in pg.inner_text("#msg-title"), 10))
    time.sleep(4)
    check("no reconnect while the lock is there", ev("self.hostStateNow()") == "updating")
    os.remove(lock)
    srv = start_ui()                                          # the helper starts the new app
    check("extension reconnects once the app is back", wait(lambda: ev(REG), 30))
    fr = pg.frame_locator("#ui")
    check("side panel shows the phone again", wait(lambda: pg.is_visible("#ui") and "is registered" in fr.locator("#regtext").inner_text(), 20))

    # A copy installed by dropping the zip (Chrome unzips it into the profile):
    # with stale files it must NOT reload itself — it asks the app for a fresh
    # zip in Downloads and raises a notification instead.
    import io, zipfile
    zipext = os.path.join(work, "zip-install")
    with zipfile.ZipFile(io.BytesIO(extension_sync.zip_bytes())) as z:
        z.extractall(zipext)
    check("the droppable zip has manifest.json at its root", os.path.isfile(os.path.join(zipext, "manifest.json")))
    json.dump({"via": "zip", "build": "stale0000stale00", "version": "0.9.0"}, open(os.path.join(zipext, "install.json"), "w"))
    profile2 = os.path.join(work, "profile2")
    os.makedirs(os.path.join(profile2, "NativeMessagingHosts"))
    json.dump({"name": "com.sipphone.host", "description": "SIP Phone", "path": wrapper, "type": "stdio",
               "allowed_origins": [f"chrome-extension://{EXT_ID}/"]},
              open(os.path.join(profile2, "NativeMessagingHosts", "com.sipphone.host.json"), "w"))
    ctx2 = pw.chromium.launch_persistent_context(profile2, headless=True, channel="chromium",
        args=[f"--disable-extensions-except={zipext}", f"--load-extension={zipext}"])
    _ep2 = ctx2.new_page(); _ep2.goto("chrome://extensions")
    _ep2.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode: true})"); _ep2.close()
    def ev2(expr):
        for w in reversed(ctx2.service_workers):
            if EXT_ID in w.url:
                try:
                    return w.evaluate(expr)
                except Exception:
                    continue
        return None
    pump2 = ctx2.pages[0] if ctx2.pages else ctx2.new_page()
    def wait2(fn, t=15):
        end = time.time() + t
        while time.time() < end:
            if fn(): return True
            pump2.wait_for_timeout(200)
        return False
    check("zip-installed copy keeps the fixed id", wait2(lambda: ev2("chrome.runtime.id") == EXT_ID))
    check("zip-installed copy connects to the app", wait2(lambda: ev2(REG)))
    zp = os.path.join(work, "Downloads", extension_sync.zip_name())
    check("app saved a fresh zip to Downloads", wait2(lambda: os.path.isfile(zp), 20))
    check("fresh zip carries the current build",
          json.loads(zipfile.ZipFile(zp).read("install.json"))["build"] == extension_sync.build_id())
    check("notification tells the user to drop it", wait2(lambda: "ext-zip" in (ev2("chrome.notifications.getAll()") or {})))
    time.sleep(2)
    check("zip-installed copy did not reload itself", ev2("chrome.storage.local.get('reloadedFor').then(x => x.reloadedFor || '')") == "")
    check("offer made once per build", ev2("chrome.storage.local.get('zipOfferedFor').then(x => x.zipOfferedFor)") == extension_sync.build_id())
    ctx2.close()

    # quitting the app is reflected in the panel
    fr.locator("#quit").click()
    check("panel notices the app stopped",
          wait(lambda: pg.is_visible("#msg") and "stopped" in pg.inner_text("#msg-title"), 15))
    pg.screenshot(path=os.path.join(OUT, "ext4_stopped.png"))
    ctx.close()

phone.stop(); sw.stop.set()
print("ALL EXTENSION CHECKS PASSED")

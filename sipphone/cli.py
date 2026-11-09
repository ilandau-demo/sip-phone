"""Interactive command-line softphone.

    python -m sipphone --config config.ini
    python -m sipphone --config config.ini --call 5551234567
"""
from __future__ import annotations

import argparse
import json
import configparser
import logging
import os
import sys
import threading
import time

from . import __version__
from . import audio as audio_mod
from .phone import Call, CallState, Config, SIPPhone

HELP = """commands:
  call <number>     dial a number (or sip:user@host)
  answer | a        answer the ringing call
  reject            reject the ringing call (486 Busy)
  hangup | h        hang up / cancel
  dtmf <digits>     send DTMF during a call (0-9 * #)
  hold | resume     put the call on hold / take it back
  mute | unmute     microphone off / on (typed speech still goes through)
  transfer <num>    blind-transfer the call to a number or contact
  say <text>        speak text to the far end (TTS -> RTP)
  shh               stop speaking / flush the TTS queue
  status            registration + call status
  register          re-register now
  devices           list audio devices
  quit | q          unregister and exit
"""


def config_dir() -> str:
    """Per-user config folder: %APPDATA%\\sipphone on Windows, ~/.config/sipphone elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "sipphone")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "sipphone")


def find_config(explicit: str = "") -> str:
    """--config wins; else ./config.ini; else the per-user config folder."""
    candidates = [explicit] if explicit else ["config.ini", os.path.join(config_dir(), "config.ini")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return candidates[-1]


def init_config() -> str:
    """Create the per-user config from the bundled example and return its path."""
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "config.ini")
    if not os.path.exists(dst):
        import shutil
        shutil.copy(os.path.join(os.path.dirname(__file__), "config.example.ini"), dst)
    return dst


def find_piper_model(model: str, cfg_folder: str) -> str:
    """Resolve a Piper voice: explicit path, else look next to the config, cwd, then
    the per-user folder; a bare name like 'en_US-amy-medium' gets '.onnx' added."""
    name = model or "en_US-lessac-medium.onnx"
    if not name.endswith(".onnx"):
        name += ".onnx"
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    for folder in (cfg_folder, os.getcwd(), config_dir()):
        cand = os.path.join(folder, name)
        if os.path.isfile(cand) and os.path.isfile(cand + ".json"):
            return cand
    sys.exit(f"Piper voice not found: {name}\n"
             f"Put the .onnx and .onnx.json files in {cfg_folder}\n"
             f"(download with: python -m piper.download_voices {name[:-5]})")


def first_time_setup(voice: str = "en_US-lessac-medium") -> int:
    """Everything a new user needs, done from inside Python so the Microsoft Store
    build's AppData redirection can't put files in the wrong place."""
    import importlib.util
    import re
    import subprocess
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    cfg = os.path.join(d, "config.ini")
    created = not os.path.exists(cfg)
    init_config()
    print(f"config:  {cfg}" + ("  (new)" if created else "  (kept your existing one)"))

    have_piper = importlib.util.find_spec("piper") is not None
    have_whisper = importlib.util.find_spec("faster_whisper") is not None
    voice_ok = os.path.isfile(os.path.join(d, voice + ".onnx"))
    if have_piper and not voice_ok:
        print(f"voice:   downloading {voice} (~60 MB)…")
        r = subprocess.run([sys.executable, "-m", "piper.download_voices", voice], cwd=d,
                           capture_output=True, text=True)
        voice_ok = r.returncode == 0 and os.path.isfile(os.path.join(d, voice + ".onnx"))
        if voice_ok:
            print("voice:   ready")
        else:
            why = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
            print(f"voice:   download failed ({why[0][:120]})")
            print("         text-to-speech stays off; check the internet connection and run")
            print("         'python -m sipphone --setup' again later")
    elif voice_ok:
        print("voice:   ready")

    if created:  # switch on whatever got installed; never edit an existing config
        text = open(cfg, encoding="utf-8").read()
        if have_piper and voice_ok:
            text = re.sub(r"(\[tts\][^\[]*?\n)engine\s*=[^\n;]*", r"\1engine   = piper    ", text, count=1)
        if have_whisper:
            text = re.sub(r"(\[stt\][^\[]*?\n)engine\s*=[^\n;]*", r"\1engine = whisper   ", text, count=1)
        open(cfg, "w", encoding="utf-8").write(text)

    if sys.platform == "win32":
        try:
            from .app import install_shortcut
            print(f"shortcut: {install_shortcut()}")
        except Exception as e:
            print(f"shortcut: skipped ({e})")
    try:
        from .native_host import register_bridge
        r = register_bridge()
        print("browser bridge: registered" if r["ok"] else f"browser bridge: {r['note']}")
    except Exception as e:
        print(f"browser bridge: skipped ({e})")
    print(f"settings: {cfg}")
    print("\nNext: open 'Nextiva Companion App' from the Start menu (right-click it → Pin to Start).")
    print("Its Settings screen opens by itself the first time - enter your Nextiva")
    print("Generic SIP Phone details there. (Editing config.ini by hand still works.)")
    return 0


def check_admin(admin: dict, cfg_path: str) -> int:
    """One-shot diagnostic for the [admin] Xsi connection."""
    print(f"config file: {cfg_path}")
    if not admin["host"] or not admin["username"]:
        print("\n[admin] is not configured in this file — xsi_host and username are both required.")
        print("If you added them to a different config.ini, this isn't the one being loaded.")
        return 1
    from .xsi import XSIClient, XSIError
    client = XSIClient(admin["host"], admin["username"], admin["password"],
                       verify_tls=admin["verify_tls"], user_domain=admin.get("user_domain", "nextiva.com"))
    watch = admin["users"]
    seed = admin.get("directory_user", "")
    dtype = {"enterprise": "Enterprise", "group": "Group"}.get(
        admin.get("directory_type", "enterprise").lower(), "Enterprise")
    # If specific users are listed, test the real monitoring path (their /calls).
    # Otherwise enumerate the directory (seeded from directory_user if given).
    target = watch[0] if watch else None
    dir_who = seed or admin["username"]
    probe = f"/user/{client._uid(target)}/calls/HookStatus" if target else f"/user/{dir_who}/directories/{dtype}"
    print(f"Xsi request:  GET {client.base}{probe}")
    print(f"admin login:  {admin['username']}")
    if target:
        print(f"watching:     {', '.join(watch)}")
    print("contacting the server…\n")

    def explain(msg: str) -> None:
        if "cannot reach" in msg:
            print("The host didn't answer — DNS, firewall, or TLS. Check the hostname and")
            print("that this machine can reach it (your HookStatus URL proves it can from a browser).")
        elif "authentication" in msg:
            print("Reached the server, but it rejected the credentials (HTTP 401).")
            print("  → The admin username/password isn't valid for HTTP Basic auth on Xsi.")
            print("    Confirm with Nextiva which account + password to use for Xsi-Actions.")
        elif "forbidden" in msg:
            print("Credentials authenticated, but this account can't read that user (HTTP 403).")
            print("  → The admin login needs group-admin / supervisor scope on the Xsi interface.")
        elif "HTTP 404" in msg:
            if target:
                print(f"Authenticated, but '{target}' wasn't found (HTTP 404).")
                print("  → Check the userId spelling (it looks like ca#######@nextiva.com).")
            else:
                print(f"The {dtype} directory wasn't found for '{dir_who}' (HTTP 404).")
                print("  → The admin login has no directory of its own. Set directory_user to a")
                print("    REAL user (e.g. directory_user = jdoe1234567) and leave 'users' blank,")
                print("    or list the userIds you want in the 'users =' field.")

    try:
        if target:
            hook = client.hook_status(target)
        else:
            users = client.list_directory_users(seed, dtype)
    except XSIError as e:
        msg = str(e)
        print(f"RESULT: FAILED — {msg}\n")
        explain(msg)
        return 1

    if target:
        state = {"off": "on a call (off-hook)", "on": "idle (on-hook)"}.get(hook, "unknown")
        print(f"RESULT: OK — read {target}'s hook status: {state}.")
        print("\nThe admin can see other users' status — the Team view will work once this")
        print("[admin] section is in the config the app loads (run --check-admin with no")
        print("--config to see which file that is).")
    else:
        from .xsi import service_type, hidden_match
        hide = [h.strip().lower() for h in (admin.get("hide_user") or [])]
        hidden = [u for u in users if hidden_match(u, hide, admin.get("user_domain", ""))]
        filtered = [u for u in users if service_type(u.name, u.user_id) and u not in hidden]
        shown = [u for u in users if u not in hidden and u not in filtered]
        print(f"RESULT: OK — read the {dtype} directory, {len(users)} entr{'y' if len(users)==1 else 'ies'}.\n")
        print(f"The Team view would show these {len(shown)} user(s):")
        for u in shown[:40]:
            print(f"  - {u.name or u.user_id}  ({u.user_id}){'  x' + u.extension if u.extension else ''}")
        if len(shown) > 40:
            print(f"  … and {len(shown) - 40} more")
        if hidden:
            print(f"\nHidden by hide_user ({len(hidden)}): " + ", ".join(u.name or u.user_id for u in hidden))
        if filtered:
            print(f"Services, listed as speed dials if ticked ({len(filtered)}) — e.g. "
                  + ", ".join((u.name or u.user_id) for u in filtered[:4]) + ("…" if len(filtered) > 4 else ""))
    return 0


def prepare_update() -> int:
    """Installer step: mark an update in progress (so the extension bridge lets
    go and nothing restarts the app), ask the running app to quit, and wait."""
    import socket
    import subprocess
    import urllib.request
    from .updater import set_lock
    from .app import UI_PORT
    set_lock(reason="installer")
    # Let go of the browser bridge entirely while files are replaced (the app
    # registers it again when it starts). Also stops a 1.x extension from
    # relaunching the old bridge mid-install.
    try:
        from .native_host import unregister_bridge
        unregister_bridge()
    except Exception:
        pass

    def port_busy() -> bool:
        with socket.socket() as sk:
            sk.settimeout(0.3)
            return sk.connect_ex(("127.0.0.1", UI_PORT)) == 0

    if port_busy():
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{UI_PORT}/api/quit", data=b"{}",
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).read()
            print("asked the running Nextiva Companion App to quit")
        except Exception:
            pass
        for _ in range(60):
            if not port_busy():
                break
            time.sleep(0.5)
    if sys.platform == "win32":
        for _ in range(40):          # the bridge exits on its own once it sees the lock
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq sipphone-native-host.exe", "/NH"],
                                 capture_output=True, text=True).stdout
            if "sipphone-native-host" not in out.lower():
                break
            time.sleep(0.5)
        for exe in ("sipphone-app.exe", "sipphone-native-host.exe"):
            subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
    print("ready to update" + ("" if not port_busy() else " (the old app is still running; it will be stopped)"))
    return 0


def presence_mode(opts: dict) -> str:
    """'events' (user login), 'admin' (Admin XSI login) or '' (Team view off).
    An explicit [presence] mode wins; otherwise an [admin] login means admin."""
    mode = (opts.get("mode") or "").strip().lower()
    if mode == "blf":                       # pre-2.4 configs: the SIP BLF source is gone
        mode = "events" if opts.get("xsi_user") else ""
    if mode in ("admin", "events"):
        return mode
    if mode in ("off", "none"):
        return ""
    if opts.get("host") and opts.get("username"):
        return "admin"
    return ""


def make_monitor(admin_opts: dict, phone=None):
    """Build and start the Team-view source from the settings."""
    mode = presence_mode(admin_opts)
    if mode not in ("events", "admin"):
        return None
    from .authentication import from_options
    from .presence import PresenceManager
    creds = from_options(mode, admin_opts)
    if not creds.complete:
        raise RuntimeError("enter the user login and password for the Team view" if mode == "events"
                           else "enter the Admin XSI login and password for the Team view")
    monitor = PresenceManager(creds, cache_path=admin_opts.get("cache_path", ""), users=admin_opts["users"],
                              hide=admin_opts["hide_user"], show_services=admin_opts.get("show_services") or [],
                              concurrency=int(admin_opts.get("concurrency", 12) or 12),
                              registration_sweep=bool(admin_opts.get("registration_sweep", True)),
                              channels=int(admin_opts.get("channels", 0) or 0))
    monitor.start()
    return monitor


def diagnose(path: str) -> int:
    """Plain-words health check for 'not registered': stored passwords, name
    resolution, and one REGISTER attempt with the server's answer."""
    import socket
    from . import config_store
    from .authentication import credential_store
    print(f"config: {path}")
    from . import __version__
    from .app import UI_PORT, _running_version, port_owner
    print(f"this install: sipphone {__version__} on {sys.executable}")
    pid, name = port_owner(UI_PORT)
    if pid:
        running = _running_version()
        print(f"app port {UI_PORT}: held by {name or '?'} (pid {pid}) — "
              + (f"running version {running}" if running else "NOT ANSWERING (stuck copy; starting the app will stop it)"))
    else:
        print(f"app port {UI_PORT}: free (the app is not running)")
    cp = config_store.read(path)
    acct = cp["account"] if cp.has_section("account") else {}
    raw_pw = ""
    try:
        import configparser
        rawcp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#")); rawcp.read(path, encoding="utf-8")
        raw_pw = rawcp.get("account", "password", fallback="")
    except Exception:
        pass
    kind = "Windows-protected (DPAPI)" if raw_pw.startswith("dpapi:") else "obfuscated" if raw_pw.startswith("b64:") else "plain" if raw_pw else "EMPTY"
    pw = acct.get("password", "")
    print(f"SIP password on disk: {kind}; readable: {'yes' if pw else 'NO'}" + (f" — {credential_store.last_problem}" if credential_store.last_problem else ""))
    print(f"SIP username: {acct.get('username', '')!r}  auth name: {acct.get('auth_username', '') or '(same)'}  domain: {acct.get('domain', '')!r}")
    proxy = (acct.get("proxy") or acct.get("domain") or "").strip()
    port = acct.get("proxy_port", "5060")
    try:
        ip = socket.gethostbyname(proxy)
        print(f"server: {proxy}:{port} -> {ip}")
    except Exception as e:
        print(f"server: {proxy}:{port} -> DNS FAILED ({e})")
        return 1
    if not pw:
        print("RESULT: no usable password — open Settings > Phone line, enter the SIP password again, Save.")
        return 1
    loaded = load_config(path); cfg = loaded[0]
    from .audio import open_audio
    phone = SIPPhone(cfg, audio=open_audio(kind="null"))
    detail = {"text": ""}
    phone.on_registration = lambda ok_, d_: detail.update(text=d_)
    phone.start()
    ok = phone.register(wait=True, timeout=8)
    print(f"REGISTER: {'OK — registered' if ok else 'FAILED — ' + (detail['text'] or 'no answer (timeout)')}")
    phone.stop()
    if not ok:
        d = detail["text"].lower()
        if "auth" in d or "401" in d or "403" in d:
            print("Hint: the server rejected the credentials. Check SIP username vs. Authentication Name (they differ on most "
                  "BroadWorks devices), and that the password is the SIP/device password, not the portal login.")
        elif "timeout" in d or not d:
            print("Hint: no reply from the server — firewall, VPN, router SIP ALG, or wrong proxy port.")
    return 0 if ok else 1


def load_config(path: str) -> tuple[Config, dict]:
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    if not cp.read(path):
        sys.exit(f"config file not found: {path}\n"
                 f"Run `sipphone --init` to create one, then fill in your SIP device details.")
    from . import config_store
    config_store.reveal_secrets(cp)                 # passwords are stored protected
    acct = cp["account"] if cp.has_section("account") else cp["DEFAULT"]
    loc = cp["local"] if cp.has_section("local") else {}
    aud = cp["audio"] if cp.has_section("audio") else {}
    cfg = Config(
        username=acct.get("username", "").strip(),
        password=acct.get("password", ""),
        domain=acct.get("domain", "").strip(),
        auth_username=acct.get("auth_username", ""),
        display_name=acct.get("display_name", ""),
        proxy=acct.get("proxy", ""),
        proxy_port=acct.getint("proxy_port", 5060),
        bind_ip=loc.get("bind_ip", "0.0.0.0"),
        bind_port=int(loc.get("bind_port", 5060)),
        public_ip=loc.get("public_ip", ""),
        rtp_port_range=tuple(int(x) for x in loc.get("rtp_ports", "10000-20000").split("-")),
        register_expires=int(loc.get("register_expires", 3600)),
        keepalive_seconds=int(loc.get("keepalive_seconds", 25)),
        codecs=tuple(c.strip().upper() for c in loc.get("codecs", "PCMU,PCMA").split(",")),
    )
    tts = cp["tts"] if cp.has_section("tts") else {}
    stt = cp["stt"] if cp.has_section("stt") else {}
    adm = cp["admin"] if cp.has_section("admin") else {}
    pres = cp["presence"] if cp.has_section("presence") else {}
    audio_opts = {
        "stt_engine": stt.get("engine", ""),
        "stt_model": stt.get("model", ""),
        "kind": aud.get("backend", "auto"),
        "tts_engine": tts.get("engine", ""),
        "tts_mode": tts.get("mode", "duck"),
        "tts_gain": float(tts.get("gain", 0.9)),
        "tts_model": tts.get("model", ""),
        "tts_voice": tts.get("voice", ""),
        "greeting": tts.get("greeting", ""),
        "input_device": _dev(aud.get("input_device", "")),
        "output_device": _dev(aud.get("output_device", "")),
        "play": aud.get("play_file", "") or None,
        "record": aud.get("record_file", "") or None,
    }
    admin_opts = {
        "host": adm.get("xsi_host", "") or adm.get("host", ""),
        "username": adm.get("username", ""),
        "password": adm.get("password", ""),
        "users": [u.strip() for u in adm.get("users", "").split(",") if u.strip()],
        "directory_user": adm.get("directory_user", ""),
        "directory_type": adm.get("directory_type", "enterprise"),
        "user_domain": adm.get("user_domain", "nextiva.com"),
        "pickup_code": adm.get("pickup_code", "*97"),
        "exclude": ([k.strip() for k in adm["exclude"].split(",")] if adm.get("exclude", None) is not None else None),
        "hide_user": [h.strip() for h in adm.get("hide_user", "").split(",") if h.strip()],
        "users_only": (adm.get("users_only", "true").strip().lower() not in ("false", "no", "0")),
        "show_services": ([k.strip().lower() for k in adm["show_services"].split(",") if k.strip()]
                          if adm.get("show_services", None) is not None else None),
        "poll_seconds": float(adm.get("poll_seconds", 5) or 5),
        "concurrency": int(adm.get("concurrency", 12) or 12),
        "channels": int(pres.get("channels", adm.get("channels", 0)) or 0),      # 0 = automatic (1–4 by directory size)
        "registration_sweep": (adm.get("registration_sweep", "true").strip().lower() not in ("false", "no", "0")),
        "cache_path": os.path.join(os.path.dirname(os.path.abspath(path)), "directory-cache.json"),
        "verify_tls": (adm.get("verify_tls", "true").strip().lower() not in ("false", "no", "0")),
        # [presence]: the user's own XSI login
        "mode": pres.get("mode", "").strip(),
        "xsi_user": pres.get("xsi_user", "").strip(),
        "xsi_password": pres.get("xsi_password", ""),
    }
    app = cp["app"] if cp.has_section("app") else {}
    upd = cp["update"] if cp.has_section("update") else {}
    app_opts = {
        "mode": app_mode(app.get("mode", "")),
        "debug": (app.get("debug", "false").strip().lower() in ("true", "yes", "1", "on")),
        "update_source": upd.get("source", "").strip(),
        "update_auto": (upd.get("auto", "false").strip().lower() in ("true", "yes", "1", "on")),
    }
    out = _Loaded((cfg, audio_opts, admin_opts))
    out.app = app_opts
    return out


class _Loaded(tuple):
    """(cfg, audio_opts, admin_opts) plus `.app` = {mode, update_source, update_auto}."""
    app: dict = {}


def app_mode(value: str) -> str:
    """'phone' (default) or 'presence' (Team view only: no registration, no calls)."""
    v = (value or "").strip().lower()
    return "presence" if v in ("presence", "team", "team-only", "presence-only", "monitor") else "phone"


def _dev(v: str):
    if not v:
        return None
    return int(v) if v.isdigit() else v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sipphone", description="SIP 2.0 softphone")
    ap.add_argument("--config", "-c", default="", help="config file (default: ./config.ini, then the per-user one)")
    ap.add_argument("--init", action="store_true", help="create the per-user config.ini and open it for editing")
    ap.add_argument("--setup", action="store_true",
                    help="first-time setup: config, Piper voice, Start-menu shortcut, then open the config")
    ap.add_argument("--version", action="version", version=f"sipphone {__import__('sipphone').__version__}")
    ap.add_argument("--call", help="dial this number immediately after registering")
    ap.add_argument("--auto-answer", action="store_true", help="answer incoming calls automatically")
    ap.add_argument("--debug", action="store_true", help="log every SIP message")
    ap.add_argument("--devices", action="store_true", help="list audio devices and exit")
    ap.add_argument("--ui", action="store_true", help="open the browser UI (http://127.0.0.1:8088)")
    ap.add_argument("--ui-port", type=int, default=8088)
    ap.add_argument("--no-browser", action="store_true", help="with --ui: don't open a browser tab (e.g. launched by the Chrome extension)")
    ap.add_argument("--app", action="store_true", help="app mode: no terminal prompt; exit from the UI's Quit button")
    ap.add_argument("--install-shortcut", action="store_true", help="Windows: add 'Nextiva Companion App' to the Start menu")
    ap.add_argument("--stt", metavar="ENGINE", help="transcribe the caller: whisper | vosk")
    ap.add_argument("--tts", metavar="ENGINE", help="enable text-to-speech: piper | espeak | openai | elevenlabs")
    ap.add_argument("--native-host-info", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--extension", action="store_true",
                    help="set up the Chrome/Edge extension folder and open it with the extensions page")
    ap.add_argument("--startup", choices=["on", "off"], help="start Nextiva Companion App at sign-in (on/off)")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="write a setting into the per-user config.ini (repeatable), e.g. --set app.mode=presence")
    ap.add_argument("--register-bridge", action="store_true",
                    help="connect the Chrome/Edge extension to this app (done automatically when the app starts)")
    ap.add_argument("--unregister-bridge", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--prepare-update", action="store_true", help=argparse.SUPPRESS)   # installer
    ap.add_argument("--finish-update", action="store_true", help=argparse.SUPPRESS)    # installer
    ap.add_argument("--diagnose", action="store_true", help="check the stored passwords, DNS and a REGISTER round trip, then exit")
    ap.add_argument("--check-admin", action="store_true",
                    help="test the [admin] Xsi connection and print the exact result")
    args = ap.parse_args(argv)

    if not args.app:
        logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    if args.devices:
        print(audio_mod.list_devices())
        return 0
    if args.diagnose:
        # the app always uses the per-user config; so does the health check
        return diagnose(args.config or os.path.join(config_dir(), "config.ini"))
    if args.check_admin:
        args.config = find_config(args.config)
        _, _, admin_opts = load_config(args.config)
        return check_admin(admin_opts, args.config)
    if args.install_shortcut:
        from .app import install_shortcut
        try:
            print(f"created: {install_shortcut()}")
            print("Open Start, find 'Nextiva Companion App', right-click → Pin to Start.")
        except RuntimeError as e:
            sys.exit(str(e))
        return 0
    if args.extension:
        from .extension_sync import guide
        return guide()
    if args.set:
        from . import config_store
        path = args.config or os.path.join(config_dir(), "config.ini")
        if not os.path.isfile(path):
            path = init_config() if not args.config else path
        changes: dict = {}
        for item in args.set:
            if "=" not in item or "." not in item.split("=", 1)[0]:
                sys.exit(f"--set expects SECTION.KEY=VALUE, got {item!r}")
            key, value = item.split("=", 1)
            sec, k = key.split(".", 1)
            changes.setdefault(sec.strip(), {})[k.strip()] = value.strip()
        config_store.update(path, changes)
        print(f"updated {path}: " + ", ".join(f"{s_}.{k_}" for s_, kv in changes.items() for k_ in kv))
        return 0
    if args.startup:
        from .app import set_start_with_windows
        where = set_start_with_windows(args.startup == "on")
        print(f"start at sign-in: {args.startup} ({where})")
        return 0
    if args.register_bridge:
        from .native_host import register_bridge
        r = register_bridge()
        print(("bridge registered: " + ", ".join(r["where"]) + f" -> {r['path']}") if r["ok"] else f"bridge NOT registered: {r['note']}")
        return 0 if r["ok"] else 1
    if args.unregister_bridge:
        from .native_host import unregister_bridge
        unregister_bridge()
        print("bridge unregistered")
        return 0
    if args.prepare_update:
        return prepare_update()
    if args.finish_update:
        from .updater import clear_lock
        clear_lock()
        print("update lock cleared")
        return 0
    if args.native_host_info:
        from .native_host import host_exe_path, HOST_NAME, ALLOWED_EXTENSION_IDS, registry_virtualized
        print(json.dumps({"name": HOST_NAME, "path": host_exe_path(), "extension_ids": ALLOWED_EXTENSION_IDS,
                          "python_can_register": not registry_virtualized()}))
        return 0
    if args.setup:
        return first_time_setup()
    if args.init:
        path = init_config()
        print(f"config: {path}")
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                print("edit it with your Nextiva Device-tab values, then run: sipphone --ui")
        except OSError:
            pass
        return 0

    args.config = find_config(args.config)
    loaded = load_config(args.config)
    cfg, audio_opts, admin_opts = loaded
    mode = loaded.app["mode"]
    if loaded.app["debug"] and not args.debug:
        # [app] debug = true: full SIP trace into app.log (Settings > Advanced)
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("sipphone").setLevel(logging.DEBUG)
        for noisy in ("httpcore2", "httpx2", "httpcore", "httpx", "piper", "urllib3", "numba", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.INFO)
    if mode == "presence":
        # Team view only: no audio, no voice features, no fixed SIP port.
        audio_opts.update(kind="null", input_device=None, output_device=None)
        audio_opts["tts_engine"] = audio_opts["stt_engine"] = ""
        args.tts = args.stt = ""
        cfg.bind_port = 0
        cfg.register_expires = max(cfg.register_expires, 60)
    tts_engine = args.tts or audio_opts.pop("tts_engine")
    tts_kw = {k[4:]: audio_opts.pop(k) for k in ("tts_mode", "tts_gain", "tts_model", "tts_voice")}
    greeting = audio_opts.pop("greeting")
    stt_engine = args.stt or audio_opts.pop("stt_engine")
    stt_model = audio_opts.pop("stt_model")
    # With the UI up, a broken optional feature shouldn't stop the phone: it is
    # reported in the UI, and the Settings screen can fix it.
    warnings: list = []

    def problem(msg: str) -> None:
        if not args.ui:
            sys.exit(msg)
        print(f"WARNING: {msg}")
        logging.getLogger("sipphone").warning("%s", msg)
        warnings.append(msg)

    try:
        aud = audio_mod.open_audio(**audio_opts)
    except Exception as e:
        problem(f"Audio device problem ({e}); using no audio. Pick devices in Settings.")
        audio_opts.update(kind="null", input_device=None, output_device=None)
        aud = audio_mod.open_audio(**audio_opts)
    # The voice engine: providers live in voice.json (migrated once from the old
    # [tts]/[stt] sections). In phone mode the audio path always goes through
    # TTSAudio + the transcriber, so a provider enabled later works without a restart.
    from .voice import VoiceEngine
    voice = VoiceEngine(os.path.dirname(os.path.abspath(args.config)))
    try:
        if voice.migrate_from_config({"engine": tts_engine, "model": tts_kw["model"], "voice": tts_kw["voice"],
                                      "gain": tts_kw["gain"]}, {"engine": stt_engine, "model": stt_model}):
            logging.getLogger("sipphone.voice").info("voice providers migrated from config.ini")
    except Exception as e:
        problem(f"Voice engine settings could not be migrated: {e}")
    if args.tts and args.tts != "auto" and not voice.registry.get(args.tts):
        try:
            voice.registry.add({"whisper": "faster-whisper", "espeak": "native"}.get(args.tts, args.tts), pid=args.tts)
            voice.registry.set_defaults(tts=args.tts)
        except Exception as e:
            problem(f"--tts {args.tts}: {e}")
    if mode != "presence":
        from .tts import TTSAudio
        from .stt import ListeningAudio, Transcriber
        mic = None if aud.name == "null" else aud
        aud = TTSAudio(lambda text: voice.synthesize(text, "call"), mic=mic, mode=tts_kw["mode"], gain=1.0)
        aud.voice = voice
        tr = Transcriber(engine=voice.recognize, on_text=lambda t: print(f"*** heard: {t}"))
        tr.active = lambda: voice.stt_enabled
        aud = ListeningAudio(aud, tr)
        for pr in voice.registry.providers:
            pid = pr["id"]
            if pid in (voice.registry.defaults.get("tts"), voice.registry.defaults.get("stt")):
                inst = voice.registry.instance(pid)
                av = inst.availability() if inst else None
                if av is not None and not av.ok:
                    problem(f"{pr['name']}: {av.reason} {av.action}".strip())
    print(f"audio backend: {aud.name}")
    try:
        phone = SIPPhone(cfg, audio=aud)
        phone.presence_only = mode == "presence"
    except OSError as e:
        if not args.ui or not cfg.bind_port:
            raise
        problem(f"Local SIP port {cfg.bind_port} is busy ({e}); using a free port instead. "
                f"Change 'Local SIP port' in Settings if your provider needs a fixed one.")
        cfg.bind_port = 0
        phone = SIPPhone(cfg, audio=aud)
        phone.presence_only = mode == "presence"

    ringing = threading.Event()

    def on_incoming(call: Call) -> None:
        print(f"\n*** INCOMING CALL from {call.remote_display or ''} <{call.remote_number}> "
              f"— type 'answer' or 'reject'")
        ringing.set()
        if args.auto_answer:
            phone.answer(call.call_id)

    def on_state(call: Call) -> None:
        if call.state == CallState.CONNECTED:
            print(f"*** connected with {call.remote_number} ({call.rtp.codec if call.rtp else '?'})")
            if greeting and hasattr(aud, "say"):
                aud.say(greeting)
        elif call.state == CallState.ENDED:
            dur = f" after {int(time.time() - call.started_at)}s" if call.started_at else ""
            print(f"*** call ended: {call.end_reason}{dur}")

    def on_reg(ok: bool, detail: str) -> None:
        print(f"*** registration {'OK' if ok else 'FAILED'}: {detail}")

    phone.on_incoming_call = on_incoming
    phone.on_call_state = on_state
    phone.on_registration = on_reg

    monitor = None
    build_monitor = lambda opts: make_monitor(opts, phone)   # noqa: E731

    phone.start()
    if presence_mode(admin_opts):
        try:
            monitor = build_monitor(admin_opts)
            if presence_mode(admin_opts) == "events":
                print(f"team presence: Xsi events as {admin_opts['xsi_user']}")
            else:
                print(f"admin monitor: polling {admin_opts['host']} as {admin_opts['username']}")
        except Exception as e:
            print(f"team view disabled: {e}")
    quit_event = threading.Event()
    srv = None
    if args.ui:
        from . import web
        from .updater import UpdateManager, consume_result
        cfg_dir = os.path.dirname(os.path.abspath(args.config))
        result = consume_result()
        if result:
            if result.get("ok"):
                warnings.append(f"Updated to Nextiva Companion App {result.get('version')}.")
            else:
                warnings.append(f"The update to {result.get('version')} failed: {result.get('error') or 'see update.log'}. "
                                f"Still on {__version__}.")
        updater = UpdateManager(args.ui_port, on_quit=quit_event.set if args.app else None,
                                busy=lambda: any(c.state != CallState.ENDED for c in phone.calls.values()))

        def restart() -> None:
            # Start a fresh copy that waits for this one to let go of its ports,
            # then quit. Only offered in app mode (no terminal to restart from).
            from .app import spawn_detached
            spawn_detached(["--no-browser", "--wait-for-port"])
            quit_event.set()

        srv = web.serve(phone, aud, port=args.ui_port, on_quit=quit_event.set, monitor=monitor,
                  open_browser=not args.no_browser, voice=voice,
                  pickup_code=admin_opts["pickup_code"],
                  config_path=os.path.abspath(args.config), build_monitor=build_monitor,
                  on_restart=restart if args.app else None, warnings=warnings,
                  started_mode=mode, update_opts=loaded.app, updater=updater,
                  phrases_path=os.path.join(cfg_dir, "phrases.txt"),
                  contacts_path=os.path.join(cfg_dir, "contacts.json"),
                  history_path=os.path.join(cfg_dir, "history.json"))
    if mode == "presence":
        print("Team view only: the phone line is not registered and calls are off.")
    else:
        print(f"registering {cfg.username}@{cfg.domain} via {cfg.proxy}:{cfg.proxy_port} ...")
        if not phone.register(wait=True):
            print("registration failed — check username / auth name / password / domain")
            if not args.call:
                print("(you can still receive nothing; fix config and retry)")
        if args.call:
            phone.call(args.call)

    if args.app:
        try:
            quit_event.wait()
        except KeyboardInterrupt:
            pass
        phone.stop(); aud.stop()
        mon = srv.ui_state.monitor if srv is not None else monitor
        if mon: mon.stop()
        if srv is not None and srv.ui_state.updater is not None:
            srv.ui_state.updater.stop()
        if srv is not None:
            srv.shutdown(); srv.server_close()
        return 0

    print(HELP)
    try:
        while True:
            try:
                line = input("sip> ").strip()
            except EOFError:
                break
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            cmd = cmd.lower()
            try:
                if cmd in ("quit", "q", "exit"):
                    break
                elif cmd == "call" and arg:
                    phone.call(arg)
                elif cmd in ("answer", "a"):
                    phone.answer()
                elif cmd == "reject":
                    phone.reject()
                elif cmd in ("hangup", "h"):
                    phone.hangup()
                elif cmd == "dtmf" and arg:
                    phone.send_dtmf(arg)
                elif cmd == "hold":
                    phone.hold()
                elif cmd == "resume":
                    phone.resume()
                elif cmd in ("mute", "unmute"):
                    phone.mute(cmd == "mute")
                elif cmd == "transfer" and arg:
                    phone.transfer(arg)
                elif cmd == "say" and arg:
                    if not hasattr(aud, "say"):
                        print("TTS not enabled: start with --tts piper (or set [tts] engine in config)")
                    elif not phone.active_call or phone.active_call.state != CallState.CONNECTED:
                        print("no connected call to speak into")
                    else:
                        aud.say(arg)
                elif cmd == "shh":
                    if hasattr(aud, "stop_speaking"):
                        aud.stop_speaking()
                elif cmd == "register":
                    phone.register()
                elif cmd == "devices":
                    print(audio_mod.list_devices())
                elif cmd == "status":
                    c = phone.active_call
                    print(f"registered: {phone.registered}  (expires in "
                          f"{int(phone.register_expiry - time.time()) if phone.register_expiry else 0}s)")
                    print(f"call: {c.state.value + ' ' + c.remote_number if c else 'none'}")
                    if c and c.rtp:
                        print(f"rtp: {c.rtp.codec} tx={c.rtp.tx_packets} rx={c.rtp.rx_packets} "
                              f"remote={c.rtp.remote}")
                    if hasattr(aud, "queued_seconds"):
                        print(f"tts: {aud.queued_seconds():.1f}s queued")
                else:
                    print(HELP)
            except RuntimeError as e:
                print(f"error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        print("shutting down...")
        phone.stop()
        aud.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

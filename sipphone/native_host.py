"""Chrome / Edge native-messaging host for the SIP Phone extension.

Chrome starts this program (registered as "com.sipphone.host") when the
extension calls chrome.runtime.connectNative(). It:

  * starts the phone app if it isn't running (without opening a browser tab),
  * pushes a compact status message whenever something changes
    (registration, your call, team activity) so the extension can update its
    badge and raise notifications instantly — an open native port also keeps
    the extension's service worker alive,
  * runs commands from the extension: launch, call, answer, reject, hangup,
    quit, status.

Wire format (Chrome native messaging): each message is a 4-byte little-endian
length followed by UTF-8 JSON. Nothing else may be written to stdout.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Optional

HOST_NAME = "com.sipphone.host"
UI_PORT = int(os.environ.get("SIPPHONE_UI_PORT", "8088"))
BASE = f"http://127.0.0.1:{UI_PORT}"

log = logging.getLogger("sipphone.native")


# --------------------------------------------------------------------- framing
class Channel:
    def __init__(self, rx=None, tx=None) -> None:
        self.rx = rx or sys.stdin.buffer
        self.tx = tx or sys.stdout.buffer
        self._lock = threading.Lock()

    def read(self) -> Optional[dict]:
        head = self.rx.read(4)
        if len(head) < 4:
            return None                      # Chrome closed the port
        (n,) = struct.unpack("<I", head)
        body = self.rx.read(n)
        if len(body) < n:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            return {}

    def send(self, msg: dict) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self.tx.write(struct.pack("<I", len(data)) + data)
            self.tx.flush()


# ------------------------------------------------------------------- local app
def _http(path: str, body: Optional[dict] = None, timeout: float = 2.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def app_running() -> bool:
    try:
        _http("/api/version", timeout=0.8)
        return True
    except Exception:
        return False


def _updating() -> bool:
    try:
        from .updater import updating
        return updating() is not None
    except Exception:
        return False


def launch_app() -> bool:
    """Start the phone without a browser tab. Breaks away from Chrome's job
    object so the phone keeps running if Chrome closes."""
    if app_running():
        return True
    if _updating():
        return False                        # the update helper starts it when done
    from .app import relaunch_command
    cmd = relaunch_command(["--no-browser"])
    if os.environ.get("SIPPHONE_LAUNCH_CMD"):          # tests / custom setups
        cmd = json.loads(os.environ["SIPPHONE_LAUNCH_CMD"])
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
        try:
            subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | BREAKAWAY, **kw)
        except OSError:
            # Job doesn't allow breakaway: let Explorer start it instead.
            subprocess.Popen(["explorer.exe", cmd[0]], **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    for _ in range(60):
        if app_running():
            return True
        time.sleep(0.25)
    return False


def host_exe_path() -> str:
    """Where pip put sipphone-native-host(.exe) — what Chrome must launch."""
    from .app import _scripts_dir
    exe = "sipphone-native-host.exe" if sys.platform == "win32" else "sipphone-native-host"
    return os.path.join(_scripts_dir(), exe)


# ------------------------------------------------------------ registration
# Browsers find a native messaging host through a small JSON manifest: on
# Windows via a registry key that points at it, elsewhere via a per-browser
# folder. The app registers itself on every start, so no installer is needed
# for the bridge (Python 3.11+ from the Microsoft Store writes the real
# registry; its AppData is still virtualised, so the manifest lives outside it).
ALLOWED_EXTENSION_IDS = [
    "ekooaigkfmobhjmioleeahcanjnhhheh",      # the unpacked / zip build (fixed key)
    # add the Chrome Web Store id here once published
]


def allowed_extension_ids() -> list:
    """Built-in IDs plus any from [extension] ids in config.ini (the Chrome Web
    Store gives a published copy a different ID) and SIPPHONE_EXTENSION_IDS."""
    ids = list(ALLOWED_EXTENSION_IDS)
    extra = os.environ.get("SIPPHONE_EXTENSION_IDS", "")
    try:
        from .cli import find_config
        from . import config_store
        cp = config_store.read(find_config())
        if cp.has_section("extension"):
            extra += "," + cp.get("extension", "ids", fallback="") + "," + cp.get("extension", "store_id", fallback="")
    except Exception:
        pass
    for tok in extra.replace(";", ",").split(","):
        tok = tok.strip().lower()
        if len(tok) == 32 and tok.isalpha() and tok not in ids:
            ids.append(tok)
    return ids


WIN_KEYS = ["Software\\Google\\Chrome\\NativeMessagingHosts\\" + HOST_NAME,
            "Software\\Microsoft\\Edge\\NativeMessagingHosts\\" + HOST_NAME,
            "Software\\Chromium\\NativeMessagingHosts\\" + HOST_NAME]
UNIX_DIRS = {
    "linux": ["~/.config/google-chrome/NativeMessagingHosts", "~/.config/chromium/NativeMessagingHosts",
              "~/.config/microsoft-edge/NativeMessagingHosts"],
    "darwin": ["~/Library/Application Support/Google/Chrome/NativeMessagingHosts",
               "~/Library/Application Support/Chromium/NativeMessagingHosts",
               "~/Library/Application Support/Microsoft Edge/NativeMessagingHosts"],
}


def store_python() -> bool:
    return "PythonSoftwareFoundation" in sys.executable or "WindowsApps" in sys.executable


def registry_virtualized() -> bool:
    """Store builds older than 3.11 write to a private registry copy Chrome can't see."""
    return sys.platform == "win32" and store_python() and sys.version_info < (3, 11)


def manifest_path() -> str:
    if sys.platform == "win32":
        # NOT under AppData: the Store build of Python redirects that
        return os.path.join(os.path.expanduser("~"), ".sipphone", HOST_NAME + ".json")
    return os.path.join(os.path.expanduser(UNIX_DIRS.get(sys.platform, UNIX_DIRS["linux"])[0]), HOST_NAME + ".json")


def manifest_dict(exe: str = "") -> dict:
    return {"name": HOST_NAME, "description": "SIP Phone bridge", "path": exe or host_exe_path(),
            "type": "stdio", "allowed_origins": [f"chrome-extension://{i}/" for i in allowed_extension_ids()]}


def _write_json(path: str, data: dict) -> bool:
    text = json.dumps(data, indent=2) + "\n"
    try:
        if open(path, encoding="utf-8").read() == text:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def register_bridge(exe: str = "") -> dict:
    """Make the browser able to start this bridge. Idempotent. Returns
    {ok, path, where: [...], changed, note}."""
    exe = exe or host_exe_path()
    out = {"ok": True, "path": "", "where": [], "changed": False, "note": "", "exe": exe}
    if not os.path.isfile(exe):
        out.update(ok=False, note=f"bridge program not found at {exe} (reinstall the package)")
        return out
    data = manifest_dict(exe)
    if sys.platform == "win32":
        p = manifest_path()
        out["path"] = p
        out["changed"] = _write_json(p, data)
        if registry_virtualized():
            out.update(ok=False, note="This Python (Microsoft Store, older than 3.11) can't write the real "
                                      "registry; run Install.bat once, or install Python 3.11+.")
            return out
        import winreg
        for sub in WIN_KEYS:
            try:
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ) as k:
                    try:
                        cur, _ = winreg.QueryValueEx(k, "")
                    except OSError:
                        cur = None
                    if cur != p:
                        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, p)
                        out["changed"] = True
                out["where"].append("HKCU\\" + sub)
            except OSError as e:
                out.update(ok=False, note=f"registry write failed: {e}")
        return out
    dirs = [os.path.expanduser(d) for d in UNIX_DIRS.get(sys.platform, UNIX_DIRS["linux"])]
    for d in dirs:
        p = os.path.join(d, HOST_NAME + ".json")
        try:
            if _write_json(p, data):
                out["changed"] = True
            out["where"].append(p)
        except OSError as e:
            out["note"] = f"{d}: {e}"
    out["path"] = out["where"][0] if out["where"] else ""
    out["ok"] = bool(out["where"])
    return out


def unregister_bridge() -> None:
    """Forget the bridge (uninstall, or while an installer replaces the files)."""
    if sys.platform == "win32":
        if registry_virtualized():
            return
        import winreg
        for sub in WIN_KEYS:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass
        return
    for d in UNIX_DIRS.get(sys.platform, UNIX_DIRS["linux"]):
        try:
            os.remove(os.path.join(os.path.expanduser(d), HOST_NAME + ".json"))
        except OSError:
            pass


def bridge_status() -> dict:
    """Is the bridge registered and pointing at an existing program?"""
    exe = host_exe_path()
    st = {"registered": False, "exe_ok": os.path.isfile(exe), "exe": exe, "path": "", "allowed": allowed_extension_ids(),
          "can_register": not registry_virtualized()}
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_KEYS[0]) as k:
                p, _ = winreg.QueryValueEx(k, "")
        else:
            p = os.path.join(os.path.expanduser(UNIX_DIRS.get(sys.platform, UNIX_DIRS["linux"])[0]), HOST_NAME + ".json")
        m = json.load(open(p, encoding="utf-8"))
        st.update(path=p, registered=(m.get("name") == HOST_NAME and os.path.isfile(m.get("path", ""))))
    except (OSError, ValueError):
        pass
    return st


# ---------------------------------------------------------------- status pump
def build_status() -> dict:
    try:
        s = _http("/api/state?since=999999999")
    except Exception:
        return {"type": "status", "up": False, "updating": _updating()}
    st = {"type": "status", "up": True, "version": s.get("version"),
          "mode": s.get("mode", "phone"),
          "registered": s.get("registered"), "reg_detail": s.get("reg_detail"),
          "name": s.get("display_name") or s.get("account"), "call": None, "team": None,
          "update": (s.get("update") or {}).get("available"),
          "extension_build": s.get("extension_build", "")}
    c = s.get("call")
    if c:
        st["call"] = {"state": c.get("state"), "number": c.get("number"),
                      "name": c.get("name") or "", "elapsed": c.get("elapsed", "")}
    if s.get("has_monitor"):
        try:
            m = _http("/api/monitor")
            ringing = [u["name"] for u in m.get("users", []) if u.get("status") == "ringing"]
            st["team"] = {"active": m.get("active", 0), "total": m.get("total", 0),
                          "ringing": ringing, "error": m.get("last_error") or ""}
        except Exception:
            pass
    return st


def _key(st: dict) -> str:
    c = st.get("call") or {}
    t = st.get("team") or {}
    u = st.get("update") or {}
    return json.dumps([st.get("up"), st.get("updating"), st.get("mode"), st.get("registered"), st.get("name"),
                       c.get("state"), c.get("number"), c.get("name"), t.get("active"), t.get("ringing"),
                       t.get("error"), u.get("version"), st.get("extension_build")])


def pump(ch: Channel, stop: threading.Event, interval: float = 1.0) -> None:
    last = None
    beat = 0.0
    while not stop.is_set():
        st = build_status()
        if not st.get("up") and st.get("updating"):
            # An update is installing: pip must be able to replace this very
            # program, so tell the extension and get out of the way. The
            # extension waits for the app to come back and reconnects.
            ch.send(st)
            log.info("update in progress - bridge exiting")
            stop.wait(2.0)
            os._exit(0)
        k = _key(st)
        now = time.time()
        if k != last or now - beat > 20:        # on change, plus a heartbeat
            ch.send(st)
            last, beat = k, now
        stop.wait(interval)


# ------------------------------------------------------------------- commands
def handle(ch: Channel, msg: dict) -> None:
    cmd = (msg or {}).get("cmd")
    rid = msg.get("id") if isinstance(msg, dict) else None
    try:
        if cmd == "launch":
            ok = launch_app()
            ch.send({"type": "result", "id": rid, "ok": ok,
                     "error": "" if ok else ("SIP Phone is updating — it will be back in a moment" if _updating()
                                             else "The phone didn't start — open SIP Phone from the Start menu")})
        elif cmd == "call":
            number = "".join(c for c in str(msg.get("number", "")) if c.isdigit() or c in "+*#")
            if not number:
                raise RuntimeError("No phone number found in the selection")
            if not launch_app():
                raise RuntimeError("The phone isn't running")
            r = _http("/api/call", {"number": number})
            if r.get("error"):
                raise RuntimeError(r["error"])
            ch.send({"type": "result", "id": rid, "ok": True, "number": number})
        elif cmd == "extension-zip":
            # a zip-installed extension has stale files: save a fresh zip to Downloads
            r = _http("/api/extension/zip", {"to": "downloads"}, timeout=10)
            ch.send({"type": "result", "id": rid, "ok": not r.get("error"), "error": r.get("error", ""),
                     "path": r.get("path", ""), "name": r.get("name", ""), "folder": r.get("folder", "")})
        elif cmd in ("answer", "reject", "hangup", "quit"):
            r = _http(f"/api/{cmd}", {})
            ch.send({"type": "result", "id": rid, "ok": not r.get("error"), "error": r.get("error", "")})
        elif cmd == "status":
            ch.send(build_status())
        elif cmd == "ping":
            from . import __version__
            ch.send({"type": "pong", "id": rid, "host_version": __version__})
        else:
            ch.send({"type": "result", "id": rid, "ok": False, "error": f"unknown command {cmd!r}"})
    except Exception as e:
        err = str(e)
        if hasattr(e, "read"):                   # HTTPError carrying our JSON error
            try:
                err = json.loads(e.read()).get("error", err)  # type: ignore[attr-defined]
            except Exception:
                pass
        ch.send({"type": "result", "id": rid, "ok": False, "error": err})


def main() -> int:
    # Logs go to a file; stdout is reserved for the protocol.
    try:
        from .cli import config_dir
        d = config_dir()
        os.makedirs(d, exist_ok=True)
        logging.basicConfig(filename=os.path.join(d, "native-host.log"), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
    except Exception:
        pass
    ch = Channel()
    stop = threading.Event()
    if "--no-autostart" not in sys.argv and not _updating():
        threading.Thread(target=launch_app, daemon=True).start()
    threading.Thread(target=pump, args=(ch, stop), daemon=True).start()
    log.info("native host started (port %s)", UI_PORT)
    while True:
        msg = ch.read()
        if msg is None:
            break
        threading.Thread(target=handle, args=(ch, msg), daemon=True).start()
    stop.set()
    log.info("native host exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

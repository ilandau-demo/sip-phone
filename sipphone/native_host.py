"""Chrome / Edge native-messaging host for the Telvoro extension.

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
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Optional

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
# The two copies of the extension that exist. Both IDs are fixed for good: the
# zip/unpacked build is signed with the key in .extkey/, the store copy with
# the key Google made when the listing was created.
KNOWN_EXTENSIONS = {
    "ekooaigkfmobhjmioleeahcanjnhhheh": "zip / unpacked build",
    "kghndilnhefammdeabhnjlphlmeeomno": "Chrome Web Store",
}
ALLOWED_EXTENSION_IDS = list(KNOWN_EXTENSIONS)


def caller_extension() -> tuple:
    """(id, kind) of the extension that launched this bridge. Chrome passes the
    caller's origin (chrome-extension://<id>/) as the first argument."""
    for a in sys.argv[1:]:
        m = re.match(r"chrome-extension://([a-p]{32})/?$", a.strip().lower())
        if m:
            i = m.group(1)
            return i, KNOWN_EXTENSIONS.get(i, "custom build")
    return "", ""


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
    return {"name": HOST_NAME, "description": "Telvoro bridge", "path": exe or host_exe_path(),
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
def _status_from_state(s: dict) -> dict:
    st = {"type": "status", "up": True, "version": s.get("version"),
          "mode": s.get("mode", "phone"),
          "registered": s.get("registered"), "reg_detail": s.get("reg_detail"),
          "name": s.get("display_name") or s.get("account"), "call": None, "team": None,
          "update": (s.get("update") or {}).get("available"),
          "extension_build": s.get("extension_build", "")}
    c = s.get("call")
    # a call ringing on another line matters more than the one in progress:
    # the badge rings and the Answer/Decline notification appears for it
    ring = next((o for o in (s.get("other_calls") or []) if o.get("state") == "incoming"), None)
    if ring and not (c and c.get("state") == "incoming"):
        c = ring
    if c:
        st["call"] = {"state": c.get("state"), "number": c.get("number"),
                      "name": c.get("name") or "", "elapsed": c.get("elapsed", "")}
    return st


class StreamFollower(threading.Thread):
    """Keeps the app's pushed state (/api/stream) so the badge and notifications
    change the moment the app does, instead of on the next 1 s poll. If the
    stream is down the pump falls back to polling by itself."""

    BUSY = ("ringing", "dialing", "on-call", "on-hold")

    def __init__(self, wake: threading.Event) -> None:
        super().__init__(name="bridge-stream", daemon=True)
        self.wake = wake
        self.state: Optional[dict] = None
        self.team_users: Dict[str, dict] = {}
        self.team_meta: Optional[dict] = None
        self.last_seen = 0.0
        self.live = False
        self.lock = threading.Lock()

    def fresh(self, within: float = 8.0) -> bool:
        return self.live and (time.time() - self.last_seen) < within

    def monitor_view(self) -> Optional[dict]:
        """The pushed team in the shape /api/monitor returns."""
        with self.lock:
            if self.team_meta is None:
                return None
            if not self.team_meta.get("enabled", True):
                return {"enabled": False}
            users = list(self.team_users.values())
            m = dict(self.team_meta, users=users, total=len(users),
                     active=sum(1 for u in users if u.get("status") in self.BUSY))
            return m

    def _apply_team(self, m: dict) -> None:
        with self.lock:
            if m.get("enabled") is False:
                self.team_meta = {"enabled": False}; self.team_users = {}
                return
            if m.get("full") or not self.team_users:
                self.team_users = {u["user_id"]: u for u in m.get("users", [])}
            else:
                for u in m.get("users", []):
                    self.team_users[u["user_id"]] = u
                for uid in m.get("removed", []):
                    self.team_users.pop(uid, None)
            self.team_meta = dict(m.get("meta") or {}, enabled=True)

    def run(self) -> None:
        while True:
            try:
                req = urllib.request.Request(BASE + "/api/stream?since=999999999", headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    kind, data = "", ""
                    while True:
                        line = r.readline()
                        if not line:
                            break
                        self.last_seen = time.time()
                        line = line.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("event: "):
                            kind = line[7:]
                        elif line.startswith("data: "):
                            data = line[6:]
                        elif line == "":
                            if kind and data:
                                try:
                                    payload = json.loads(data)
                                except ValueError:
                                    payload = None
                                if payload is not None:
                                    if kind == "state":
                                        with self.lock:
                                            self.state = payload
                                    elif kind == "team":
                                        self._apply_team(payload)
                                    self.live = True
                                    self.wake.set()
                            kind, data = "", ""
            except Exception as e:
                if self.live:
                    log.info("app stream ended (%s); polling until it is back", e)
            self.live = False
            self.wake.set()
            time.sleep(1.0)


_follower: Optional[StreamFollower] = None


def build_status() -> dict:
    f = _follower
    if f is not None and f.fresh() and f.state is not None:
        with f.lock:
            s = dict(f.state)
        st = _status_from_state(s)
        if s.get("has_monitor"):
            m = f.monitor_view()
            if m is None or m.get("enabled") is False:
                try:
                    m = _http("/api/monitor")
                except Exception:
                    m = None
            if m:
                ringing = [u["name"] for u in m.get("users", []) if u.get("status") == "ringing"]
                st["team"] = {"active": m.get("active", 0), "total": m.get("total", 0),
                              "ringing": ringing, "error": m.get("last_error") or ""}
        return st
    try:
        s = _http("/api/state?since=999999999", timeout=5.0)
    except Exception as e:
        # A refused connection means the app is gone; anything else (a slow
        # answer, a transient server error) is reported so it can be debounced.
        refused = isinstance(e, (ConnectionRefusedError, urllib.error.URLError)) and \
            isinstance(getattr(e, "reason", e), ConnectionRefusedError)
        return {"type": "status", "up": False, "updating": _updating(), "_err": repr(e), "_refused": refused}
    st = _status_from_state(s)
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


def _report_caller() -> bool:
    """Tell the app which copy of the extension is talking to it (Settings
    shows "connected: Chrome Web Store copy"). Returns True once delivered."""
    ext_id, kind = caller_extension()
    try:
        _http("/api/extension/seen", {"id": ext_id, "kind": kind})
        return True
    except Exception:
        return False


def pump(ch: Channel, stop: threading.Event, interval: float = 1.0, follow: bool = True) -> None:
    global _follower
    wake = threading.Event()
    if follow and _follower is None:
        _follower = StreamFollower(wake)
        _follower.start()
    last = None
    beat = 0.0
    misses = 0
    reported = False
    while not stop.is_set():
        wake.clear()
        st = build_status()
        if not st.get("up") and not st.get("updating") and not st.get("_refused"):
            # One slow or failed poll must not flip the panel to "stopped":
            # only two in a row count (a refused connection counts at once).
            misses += 1
            if misses == 1:
                log.warning("status poll failed (%s); retrying before reporting the app down", st.get("_err"))
            if misses < 2:
                stop.wait(interval)
                continue
        elif st.get("up"):
            if misses:
                log.info("status poll recovered after %d miss(es)", misses)
            misses = 0
            if not reported:
                reported = _report_caller()
        else:
            misses = 0
        if not st.get("up") and last and json.loads(last)[0]:
            log.warning("app reported down: %s", st.get("_err", "connection refused"))
            reported = False
        st.pop("_err", None); st.pop("_refused", None)
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
        # a pushed change wakes us at once; otherwise poll on the interval
        wake.wait(interval) if (_follower is not None and _follower.fresh()) else stop.wait(interval)


# ------------------------------------------------------------------- commands
def handle(ch: Channel, msg: dict) -> None:
    cmd = (msg or {}).get("cmd")
    rid = msg.get("id") if isinstance(msg, dict) else None
    try:
        if cmd == "launch":
            ok = launch_app()
            ch.send({"type": "result", "id": rid, "ok": ok,
                     "error": "" if ok else ("Telvoro is updating — it will be back in a moment" if _updating()
                                             else "The phone didn't start — open Telvoro from the Start menu")})
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

"""In-app updates.

A new version reaches the app in one of two ways:

  * the user picks a sipphone-*.whl (or the whole Telvoro-x.y.z.zip) in
    Settings -> Updates, or
  * an *update source* — a folder / network share that holds the wheels, or an
    https:// address of a latest.json {"version": "...", "wheel": "...url..."} —
    which is checked at start, every 6 hours, and on demand. With
    [update] auto = true the app installs it by itself when no call is active.

Installing can't happen inside the running app on Windows: its own .exe
launchers are locked while it runs, and the extension bridge
(sipphone-native-host.exe) is locked for as long as Chrome keeps it open. So:

  1. the wheel is staged in <config>/updates/ and update.lock is written,
  2. a small helper script (plain Python, no sipphone imports) is started
     detached,
  3. the app quits.

The helper waits for the app and the bridge to go away, runs
`pip install --upgrade`, removes the lock and starts the app again. The native
host exits when it sees the lock (telling the extension "updating") and won't
autostart the app while it's there; the extension waits for the app to come
back, then reconnects and reloads itself if its files changed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Optional

from . import __version__

log = logging.getLogger("sipphone.updater")

LOCK_NAME = "update.lock"
RESULT_NAME = "update-result.json"
LOG_NAME = "update.log"
HELPER_NAME = "update_helper.py"
LOCK_MAX_AGE = 15 * 60          # a lock older than this is a crashed update
MAX_WHEEL = 80 * 1024 * 1024
WHEEL_RE = re.compile(r"^sipphone-(\d+(?:\.\d+)*(?:[a-z0-9.+-]*)?)-py3-none-any\.whl$", re.I)
CHECK_EVERY = 6 * 3600


# ----------------------------------------------------------------- versions
def version_tuple(v: str) -> tuple:
    parts = []
    for p in re.split(r"[.+-]", v or ""):
        parts.append(int(p) if p.isdigit() else -1)
    return tuple(parts)


def newer(a: str, b: str) -> bool:
    """True if version a is newer than b."""
    return version_tuple(a) > version_tuple(b)


def wheel_version(filename: str) -> str:
    m = WHEEL_RE.match(os.path.basename(filename))
    return m.group(1) if m else ""


# --------------------------------------------------------------------- lock
def _config_dir() -> str:
    from .cli import config_dir
    return config_dir()


def lock_path() -> str:
    return os.path.join(_config_dir(), LOCK_NAME)


def updating() -> Optional[dict]:
    """The lock's contents while an update is in progress, else None."""
    p = lock_path()
    try:
        age = time.time() - os.path.getmtime(p)
    except OSError:
        return None
    if age > LOCK_MAX_AGE:
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def set_lock(version: str = "", reason: str = "update") -> str:
    p = lock_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": version, "reason": reason, "started": time.time(), "pid": os.getpid()}, f)
    return p


def clear_lock() -> None:
    try:
        os.remove(lock_path())
    except OSError:
        pass


def consume_result() -> Optional[dict]:
    """The outcome the helper left behind (read once, then deleted)."""
    p = os.path.join(_config_dir(), RESULT_NAME)
    try:
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        os.remove(p)
        return r
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ staging
def validate_wheel(path: str) -> str:
    """Check that *path* is a sipphone wheel; return its version."""
    name = os.path.basename(path)
    ver = wheel_version(name)
    if not ver:
        raise ValueError("That isn't a Telvoro update file (expected sipphone-<version>-py3-none-any.whl)")
    if os.path.getsize(path) > MAX_WHEEL:
        raise ValueError("Update file is too large")
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            init = next((n for n in names if n == "sipphone/__init__.py"), None)
            if init is None or not any(n.endswith(".dist-info/METADATA") for n in names):
                raise ValueError("Update file is damaged or isn't Telvoro")
            m = re.search(rb'__version__\s*=\s*"([^"]+)"', z.read(init))
            if m and m.group(1).decode() != ver:
                raise ValueError(f"Update file name says {ver} but it contains {m.group(1).decode()}")
    except zipfile.BadZipFile:
        raise ValueError("Update file is damaged (not a valid wheel)")
    return ver


def stage(src: str, data: Optional[bytes] = None) -> tuple:
    """Put a wheel in <config>/updates/. *src* is a filename (data given) or a
    path. A Telvoro-x.y.z.zip bundle is accepted too: its wheel is used.
    Returns (staged_path, version)."""
    folder = os.path.join(_config_dir(), "updates")
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(src)
    if data is not None:
        if len(data) > MAX_WHEEL:
            raise ValueError("Update file is too large")
        incoming = os.path.join(folder, "incoming")
        os.makedirs(incoming, exist_ok=True)
        src = os.path.join(incoming, re.sub(r"[^A-Za-z0-9._-]", "_", name) or "upload")
        with open(src, "wb") as f:
            f.write(data)
    if name.lower().endswith(".zip") and not WHEEL_RE.match(name):
        with zipfile.ZipFile(src) as z:
            inner = [n for n in z.namelist() if WHEEL_RE.match(os.path.basename(n))]
            if not inner:
                raise ValueError("That zip doesn't contain a Telvoro update (sipphone-*.whl)")
            inner.sort(key=lambda n: version_tuple(wheel_version(os.path.basename(n))))
            target = os.path.join(folder, os.path.basename(inner[-1]))
            with z.open(inner[-1]) as r, open(target, "wb") as w:
                shutil.copyfileobj(r, w)
        if data is not None:
            os.remove(src)
        src = target
    ver = validate_wheel(src)
    dest = os.path.join(folder, os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copyfile(src, dest)
        if data is not None:
            os.remove(src)
    # keep the folder tidy: only the newest few
    old = sorted((f for f in os.listdir(folder) if WHEEL_RE.match(f)),
                 key=lambda f: version_tuple(wheel_version(f)))
    for f in old[:-3]:
        try:
            os.remove(os.path.join(folder, f))
        except OSError:
            pass
    return dest, ver


# ------------------------------------------------------------------ sources
def find_in_folder(folder: str) -> Optional[dict]:
    """Newest sipphone wheel in a folder (or UNC share)."""
    try:
        names = [n for n in os.listdir(folder) if WHEEL_RE.match(n)]
    except OSError as e:
        raise RuntimeError(f"can't read the update folder: {e}")
    if not names:
        return None
    names.sort(key=lambda n: version_tuple(wheel_version(n)))
    best = names[-1]
    notes = ""
    for cand in ("CHANGELOG.md", "whats-new.txt", "notes.txt"):
        p = os.path.join(folder, cand)
        if os.path.isfile(p):
            try:
                notes = open(p, encoding="utf-8", errors="replace").read(4000)
            except OSError:
                pass
            break
    return {"version": wheel_version(best), "path": os.path.join(folder, best), "notes": notes}


def fetch_latest(url: str, timeout: float = 15.0) -> Optional[dict]:
    """latest.json at an https:// address: {"version", "wheel" (url, may be relative), "notes"}."""
    req = urllib.request.Request(url, headers={"User-Agent": f"sipphone/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        info = json.load(r)
    ver = str(info.get("version", "")).strip()
    wheel = str(info.get("wheel", "")).strip()
    if not ver or not wheel:
        raise RuntimeError("latest.json needs \"version\" and \"wheel\"")
    return {"version": ver, "url": urllib.parse.urljoin(url, wheel), "notes": str(info.get("notes", ""))[:4000]}


def download(url: str, timeout: float = 60.0) -> tuple:
    name = os.path.basename(urllib.parse.urlparse(url).path)
    if not WHEEL_RE.match(name):
        raise RuntimeError(f"the update link doesn't point at a sipphone wheel ({name})")
    req = urllib.request.Request(url, headers={"User-Agent": f"sipphone/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_WHEEL + 1)
    if len(data) > MAX_WHEEL:
        raise RuntimeError("downloaded file is too large")
    return stage(name, data)


# ------------------------------------------------------------------- helper
HELPER = r'''# Telvoro update helper - started by the app, runs after it has quit.
import json, os, socket, subprocess, sys, time
a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
wheel, port, lock, logp = a["--wheel"], int(a["--port"]), a["--lock"], a["--log"]
app, result, version = a.get("--app", ""), a["--result"], a.get("--version", "")
py = sys.executable
log = open(logp, "a", encoding="utf-8", buffering=1)
def say(m):
    log.write(time.strftime("%H:%M:%S ") + m + "\n")
def port_busy():
    s = socket.socket(); s.settimeout(0.3)
    try: return s.connect_ex(("127.0.0.1", port)) == 0
    finally: s.close()
def running(exe):
    if sys.platform != "win32": return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/NH"], capture_output=True, text=True).stdout
        return exe.lower() in out.lower()
    except Exception: return False
def kill(exe):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
def finish(ok, error=""):
    with open(result, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "version": version, "error": error, "log": logp, "t": time.time()}, f)
    try: os.remove(lock)
    except OSError: pass
say("update to %s from %s" % (version, wheel))
for _ in range(240):                      # the app quits on its own; give it 60 s
    if not port_busy(): break
    time.sleep(0.25)
for _ in range(120):                      # Chrome lets the bridge go once it sees the lock
    if not running("sipphone-native-host.exe") and not running("sipphone-app.exe"): break
    time.sleep(0.25)
else:
    say("still running - stopping it"); kill("sipphone-native-host.exe"); kill("sipphone-app.exe"); time.sleep(1)
cmd = [py, "-m", "pip", "install", "--upgrade", "--no-warn-script-location", "--disable-pip-version-check", wheel]
if a.get("--same") == "1":
    # a rebuilt wheel with the version already installed: pip would call it
    # "already satisfied" and change nothing, so replace the package's files
    # (dependencies are already in place)
    cmd[4:4] = ["--force-reinstall", "--no-deps"]
ok, err = False, ""
for attempt in range(1, 5):
    say("pip attempt %d" % attempt)
    r = subprocess.run(cmd, capture_output=True, text=True)
    log.write(r.stdout + r.stderr)
    if r.returncode == 0:
        ok = True; break
    err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["pip failed"]
    err = err[0]
    kill("sipphone-native-host.exe"); kill("sipphone-app.exe"); time.sleep(3)
say("result: %s %s" % ("ok" if ok else "FAILED", err))
finish(ok, err)
cmd = [app, "--no-browser", "--after-update"] if app and os.path.isfile(app) else [py, "-m", "sipphone.app", "--no-browser", "--after-update"]
kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if sys.platform == "win32":
    try: subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x01000000, **kw)
    except OSError: subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, **kw)
else:
    subprocess.Popen(cmd, start_new_session=True, **kw)
say("started " + " ".join(cmd))
'''


def launch_helper(wheel: str, version: str, port: int) -> str:
    """Write the helper next to the config and start it detached. The caller
    then quits the app. Returns the helper's log path."""
    d = _config_dir()
    os.makedirs(d, exist_ok=True)
    helper = os.path.join(d, HELPER_NAME)
    with open(helper, "w", encoding="utf-8") as f:
        f.write(HELPER)
    from .app import pythonw
    logp = os.path.join(d, LOG_NAME)
    set_lock(version)
    # --app "": the helper relaunches through the interpreter (python -m
    # sipphone.app), never the exe launcher — see app.relaunch_command
    cmd = [pythonw(), helper, "--wheel", wheel, "--port", str(port), "--lock", lock_path(),
           "--log", logp, "--result", os.path.join(d, RESULT_NAME),
           "--app", os.environ.get("SIPPHONE_APP_EXE", ""),            # tests only
           "--version", version, "--same", "1" if version == __version__ else "0"]
    if os.environ.get("SIPPHONE_HELPER_CMD"):                 # tests
        cmd = json.loads(os.environ["SIPPHONE_HELPER_CMD"]) + cmd[2:]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
        try:
            subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | BREAKAWAY, **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    log.info("update helper started for %s (%s)", version, wheel)
    return logp


# ------------------------------------------------------------------ manager
class UpdateManager:
    """Checks the update source, stages files, and hands off to the helper."""

    def __init__(self, port: int, on_quit: Optional[Callable[[], None]] = None,
                 busy: Optional[Callable[[], bool]] = None, ui_state=None) -> None:
        self.port = port
        self.on_quit = on_quit                 # None = can't restart from here (terminal mode)
        self.busy = busy or (lambda: False)    # a call is active
        self.ui = ui_state
        self.source = ""
        self.auto = False
        self.available: Optional[dict] = None  # {"version", "path"|"url", "notes"}
        self.state = "idle"                    # idle | checking | downloading | installing | failed
        self.error = ""
        self.last_check = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- config ---------------------------------------------------------------
    def configure(self, opts: dict) -> None:
        self.source = (opts.get("update_source") or "").strip()
        self.auto = bool(opts.get("update_auto"))
        if self.source and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
            self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- status ----------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            av = dict(self.available) if self.available else None
        if av:
            av.pop("path", None); av.pop("url", None)
        return {"version": __version__, "available": av, "state": self.state, "error": self.error,
                "source": self.source, "auto": self.auto, "last_check": self.last_check,
                "last_error": self.last_error, "can_install": self.on_quit is not None,
                "updating": updating() is not None}

    # -- checking ----------------------------------------------------------------
    def check_now(self) -> dict:
        if not self.source:
            return {"ok": False, "message": "No update source is set."}
        self.state = "checking"
        try:
            if self.source.lower().startswith(("http://", "https://")):
                found = fetch_latest(self.source)
            else:
                found = find_in_folder(self.source)
            self.last_error = ""
            with self._lock:
                if found and newer(found["version"], __version__):
                    self.available = found
                    msg = f"Version {found['version']} is available."
                else:
                    self.available = None
                    msg = "You have the latest version." if not found or not newer(__version__, found["version"]) \
                        else f"You're on {__version__}; the source has {found['version']}."
            return {"ok": True, "message": msg, "available": self.status()["available"]}
        except Exception as e:
            self.last_error = str(e)
            log.warning("update check failed: %s", e)
            return {"ok": False, "message": f"Couldn't check for updates: {e}"}
        finally:
            self.last_check = time.time()
            if self.state == "checking":
                self.state = "idle"

    def _loop(self) -> None:
        self._wake.wait(5)                      # let the app finish starting
        while not self._stop.is_set():
            self._wake.clear()
            r = self.check_now()
            if r.get("ok") and self.available and self.auto and self.on_quit is not None:
                # give the banner a moment, then install when nothing is going on
                for _ in range(60):
                    if self._stop.wait(1):
                        return
                    if not self.busy():
                        break
                if not self.busy():
                    try:
                        self.install_available()
                        return
                    except Exception as e:
                        log.warning("automatic update failed: %s", e)
            self._wake.wait(CHECK_EVERY)

    # -- installing ----------------------------------------------------------------
    def install_available(self) -> dict:
        with self._lock:
            av = dict(self.available) if self.available else None
        if not av:
            raise RuntimeError("No update is available")
        if av.get("url"):
            self.state = "downloading"
            try:
                path, ver = download(av["url"])
            except Exception as e:
                self.state = "failed"; self.error = str(e)
                raise RuntimeError(f"Download failed: {e}")
        else:
            try:
                path, ver = stage(av["path"])
            except Exception as e:
                self.state = "failed"; self.error = str(e)
                raise RuntimeError(f"Couldn't read the update: {e}")
        return self.install_file(path)

    def install_file(self, path: str) -> dict:
        """Hand a staged wheel to the helper and quit. Raises on refusal."""
        if self.on_quit is None:
            raise RuntimeError("Updates install from the Telvoro desktop app (Start menu), not from a terminal session. "
                               f"Run:  python -m pip install --upgrade --force-reinstall --no-deps \"{path}\"")
        if self.busy():
            raise RuntimeError("A call is active — try again after it ends")
        if updating():
            raise RuntimeError("An update is already in progress")
        ver = validate_wheel(path)
        if not newer(ver, __version__) and ver != __version__:
            raise RuntimeError(f"{ver} is older than the installed {__version__}")
        self.state = "installing"
        self.error = ""
        launch_helper(path, ver, self.port)
        if self.ui is not None:
            self.ui.add("event", f"Installing Telvoro {ver} — back in a moment")
        threading.Timer(0.8, self.on_quit).start()
        return {"ok": True, "version": ver}

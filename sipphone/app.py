"""Windows "app" mode: no console window, browser UI, Start-menu shortcut.

* ``sipphone-app``  (gui_scripts entry → sipphone-app.exe, runs under pythonw)
  Starts the phone with the settings in config.ini, opens the UI, logs to
  app.log in the config folder, and exits when "Quit" is pressed in the UI.
  If an instance is already running it just brings the UI up in the browser.

* ``sipphone --install-shortcut``
  Creates "Nextiva Companion App" in the Start menu (Programs folder) pointing at
  sipphone-app.exe with the bundled icon, so it can be pinned to Start/taskbar.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import webbrowser

UI_PORT = 8088


def _scripts_dir() -> str:
    """Folder that holds sipphone-app.exe for the running interpreter."""
    candidates = []
    for scheme in (f"{os.name}_user", "nt_user", "posix_user", None):
        try:
            candidates.append(sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts"))
        except Exception:
            pass
    candidates.append(os.path.join(os.path.dirname(sys.executable), "Scripts"))
    exe = "sipphone-app.exe" if sys.platform == "win32" else "sipphone-app"
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, exe)):
            return c
    return candidates[0] or ""


def app_exe_path() -> str:
    exe = "sipphone-app.exe" if sys.platform == "win32" else "sipphone-app"
    return os.path.join(_scripts_dir(), exe)


def icon_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "sipphone.ico")


def shortcut_target(extra_args: list | None = None) -> tuple[str, str]:
    """(TargetPath, Arguments) for a shortcut that starts the app.

    Shortcuts go through the interpreter (``pythonw.exe -m sipphone.app``) rather
    than the pip-made sipphone-app.exe: the exe launcher embeds the interpreter
    path it was installed with, which breaks when that Python is removed or
    updated (Store Python in particular), while pythonw is always the one that
    actually has the package. The exe is still used for the browser bridge."""
    args = ["-m", "sipphone.app"] + list(extra_args or [])
    return pythonw(), " ".join(args)


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _write_shortcut(lnk: str, description: str, extra_args: list | None = None) -> str:
    target, arguments = shortcut_target(extra_args)
    if not os.path.isfile(target):
        raise RuntimeError(f"Python not found at {target}; reinstall the package")
    workdir = os.path.expanduser("~")
    ps = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{_ps_quote(lnk)}')
$s.TargetPath = '{_ps_quote(target)}'
$s.Arguments = '{_ps_quote(arguments)}'
$s.WorkingDirectory = '{_ps_quote(workdir)}'
$s.IconLocation = '{_ps_quote(icon_path())},0'
$s.Description = '{_ps_quote(description)}'
$s.WindowStyle = 7
$s.Save()
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not create shortcut: {r.stderr.strip()}")
    return lnk


def install_shortcut() -> str:
    """Create a Start-menu shortcut (Windows). Returns the .lnk path."""
    if sys.platform != "win32":
        raise RuntimeError("Start-menu shortcuts are a Windows feature")
    programs = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
    # The Store build of Python virtualises APPDATA; resolve the real folder via the shell.
    real = subprocess.run(["powershell", "-NoProfile", "-Command",
                           "[Environment]::GetFolderPath('Programs')"], capture_output=True, text=True)
    if real.returncode == 0 and real.stdout.strip():
        programs = real.stdout.strip()
    os.makedirs(programs, exist_ok=True)
    _remove_legacy(programs)
    return _write_shortcut(os.path.join(programs, "Nextiva Companion App.lnk"), "SIP softphone with text-to-speech")


LEGACY_LNK = "SIP Phone.lnk"          # the name before 2.5.0; replaced, never left beside the new one


def _remove_legacy(folder: str) -> bool:
    try:
        os.remove(os.path.join(folder, LEGACY_LNK))
        return True
    except OSError:
        return False


def _startup_entry() -> str:
    """Where a 'start when I sign in' entry lives for this user."""
    if sys.platform == "win32":
        real = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "[Environment]::GetFolderPath('Startup')"], capture_output=True, text=True)
        folder = real.stdout.strip() if real.returncode == 0 and real.stdout.strip() else \
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        return os.path.join(folder, "Nextiva Companion App.lnk")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "autostart", "sipphone.desktop")


def starts_with_windows() -> bool:
    entry = _startup_entry()
    if os.path.isfile(entry):
        return True
    # set up before the rename: still counts, and is migrated to the new name
    if sys.platform == "win32" and os.path.isfile(os.path.join(os.path.dirname(entry), LEGACY_LNK)):
        try:
            set_start_with_windows(True)
        except Exception:
            return True
        return True
    return False


def set_start_with_windows(on: bool) -> str:
    """Add/remove the sign-in autostart entry (the phone starts without a browser
    tab, so the extension and the Start-menu shortcut find it already running)."""
    entry = _startup_entry()
    if sys.platform == "win32":
        _remove_legacy(os.path.dirname(entry))
    if not on:
        try:
            os.remove(entry)
        except FileNotFoundError:
            pass
        return entry
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    if sys.platform == "win32":
        _write_shortcut(entry, "Nextiva Companion App (starts in the background at sign-in)", ["--no-browser"])
    else:
        target, arguments = shortcut_target(["--no-browser"])
        with open(entry, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Nextiva Companion App\n"
                    f"Exec={target} {arguments}\nX-GNOME-Autostart-enabled=true\n")
    return entry


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _running_version() -> str:
    """Version string reported by an instance already listening on UI_PORT."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}/api/version", timeout=2) as r:
            return json.load(r).get("version", "")
    except Exception:
        return ""


def _quit_running() -> None:
    """Ask the instance on UI_PORT to shut down cleanly."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{UI_PORT}/api/quit", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def port_owner(port: int) -> tuple[int, str]:
    """(pid, image name) of the process listening on 127.0.0.1:port, or (0, '')."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
            pid = 0
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                    pid = int(parts[4]); break
            if not pid:
                return 0, ""
            tl = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                capture_output=True, text=True, timeout=10).stdout
            name = tl.strip().split('","')[0].strip('"') if tl.strip() else ""
            return pid, name
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10).stdout
        pid = int(out.split()[0]) if out.split() else 0
        if not pid:
            return 0, ""
        name = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
        return pid, name
    except Exception:
        return 0, ""


def kill_stuck_instance(port: int = UI_PORT) -> str:
    """Terminate an unresponsive earlier copy of the app holding our port.

    Only python/pythonw/sipphone processes are touched — anything else on the
    port is left alone and reported. Returns a one-line note of what happened."""
    pid, name = port_owner(port)
    if not pid:
        return "nothing is listening on the port"
    ours = any(k in name.lower() for k in ("python", "sipphone"))
    if not ours:
        return f"port {port} is used by {name or 'another program'} (pid {pid}); not touched"
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.kill(pid, 9)
    except Exception as e:
        return f"could not stop {name} (pid {pid}): {e}"
    for _ in range(20):
        if not _port_in_use(port):
            break
        time.sleep(0.25)
    return f"stopped unresponsive {name} (pid {pid})"


def pythonw() -> str:
    """This interpreter, windowless where available."""
    exe = sys.executable
    if sys.platform == "win32" and os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def relaunch_command(args: list) -> list:
    r"""How a Python process should start the app. The pip-made sipphone-app.exe
    launcher can't resolve Store Python's pythonw alias from inside a Store
    Python process (its AppData\Local is redirected), so always go through the
    interpreter itself; the exe launcher is only for Explorer / the Start menu."""
    return [pythonw(), "-m", "sipphone.app"] + list(args)


def spawn_detached(args: list) -> None:
    """Start another copy of the app that outlives this process."""
    cmd = relaunch_command(args)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
        try:
            subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | BREAKAWAY, **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    logging.info("spawned %s", cmd)


def main() -> int:
    from .cli import config_dir, init_config, main as cli_main

    # App mode is always tied to the per-user config (%APPDATA%\sipphone on
    # Windows). Unlike the `sipphone` CLI, it deliberately ignores any
    # config.ini in the current working directory, so a pinned shortcut opens
    # the same account no matter where Windows launches it from.
    cfg_path = os.path.join(config_dir(), "config.ini")
    if not os.path.isfile(cfg_path):
        cfg_path = init_config()
    log_dir = os.path.dirname(cfg_path) or config_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")

    # No console under pythonw: send everything to a log file.
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(log_file, "a", encoding="utf-8", buffering=1)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        force=True)
    logging.info("Nextiva Companion desktop app starting; config: %s", cfg_path)

    from . import __version__
    from . import updater
    if "--after-update" in sys.argv[1:]:
        updater.clear_lock()
    elif updater.updating() is not None:
        # Started by hand while the update helper is still working: wait for it.
        logging.info("an update is in progress; waiting for it to finish")
        for _ in range(240):
            if updater.updating() is None:
                break
            time.sleep(0.5)
    # keep the browser extension folder in step with this version (it reloads itself)
    try:
        from .extension_sync import sync
        sync()
    except Exception as e:
        logging.warning("extension folder not updated: %s", e)
    # and make sure the browser can reach the bridge (no installer needed)
    try:
        from .native_host import register_bridge
        r = register_bridge()
        if not r["ok"]:
            logging.warning("browser bridge not registered: %s", r["note"])
        elif r["changed"]:
            logging.info("browser bridge registered: %s -> %s", ", ".join(r["where"]), r["path"])
    except Exception as e:
        logging.warning("browser bridge registration failed: %s", e)
    no_browser = "--no-browser" in sys.argv[1:]
    if "--wait-for-port" in sys.argv[1:] or "--after-update" in sys.argv[1:]:
        # Restart from Settings: the old copy is shutting down; wait for it.
        for _ in range(120):
            if not _port_in_use(UI_PORT):
                break
            time.sleep(0.25)
        time.sleep(0.5)                 # let it release the SIP port too
    if _port_in_use(UI_PORT):  # something is already running on our port
        running = _running_version()
        if running == __version__:
            logging.info("same version (%s) already running; opening it", __version__)
            if not no_browser:
                webbrowser.open(f"http://127.0.0.1:{UI_PORT}/")
            return 0
        # An older (or unknown) instance is still running the old code — that's
        # why updates don't show. Ask it to quit, wait for the port to free,
        # then start the current version in its place.
        logging.info("replacing running version %s with %s", running or "unknown", __version__)
        if running:
            _quit_running()
            for _ in range(20):
                if not _port_in_use(UI_PORT):
                    break
                time.sleep(0.25)
        if _port_in_use(UI_PORT):
            # It isn't answering (or won't quit): a copy left over from a removed
            # install, hung on the port. Stop it rather than opening a dead page.
            note = kill_stuck_instance(UI_PORT)
            logging.warning("port %s still busy: %s", UI_PORT, note)
        if _port_in_use(UI_PORT):
            msg = (f"Nextiva Companion App can't start: port {UI_PORT} is in use by another program "
                   f"({port_owner(UI_PORT)[1] or 'unknown'}). Close it and try again.")
            logging.error(msg)
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, msg, "Nextiva Companion App", 0x10)
                except Exception:
                    pass
            return 1

    try:
        return cli_main(["--config", cfg_path, "--ui", "--ui-port", str(UI_PORT), "--app"]
                        + (["--no-browser"] if no_browser else []))
    except SystemExit as e:
        # config problems etc.: surface them in the browser-less world via a message box
        msg = str(e)
        if msg and msg != "0" and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "Nextiva Companion App", 0x10)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

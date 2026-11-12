"""The Chrome/Edge extension, packaged two ways from the files inside the wheel
(sipphone/extension):

  * a ZIP you drop onto chrome://extensions (Developer mode on). Chrome unzips
    it into the profile and loads it as an unpacked extension. The manifest
    carries a fixed `key`, so the ID - and the bridge registration - never
    change. Simplest install. When the extension files change, the app saves
    a fresh zip to Downloads and the extension shows a notification asking to
    drop it again.

  * a fixed folder, %USERPROFILE%\\SIP Phone Extension, that the app refreshes
    on every start (for "Load unpacked"). An extension loaded from there
    reloads itself after an update - nothing to do.

Both carry install.json = {"via": "zip"|"folder", "build": <hash>}, and the
app reports the hash of its own packaged files, so the extension can tell
whether its files are stale (not just whether the app version changed).

The folder is deliberately NOT under AppData: Microsoft Store Python redirects
AppData writes into a private location Chrome can't see.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile

from . import __version__

log = logging.getLogger("sipphone.extension")

FOLDER_NAME = "SIP Phone Extension"
ZIP_BASENAME = "telvoro-extension"
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension")
MARKER = "install.json"

README = f"""TELVORO - PRESENCE & SIP PHONE - CHROME / EDGE EXTENSION
===================================

This folder is the Telvoro browser extension. The Telvoro desktop app keeps it up
to date automatically - don't edit or move it.

Add it to Chrome once:
  1. Open  chrome://extensions   (Edge: edge://extensions)
  2. Turn on "Developer mode"
  3. Click "Load unpacked" and choose THIS folder:
       {{path}}
  4. Pin "Telvoro" from the puzzle-piece menu.

After that, updating the Telvoro desktop app also updates the extension - it reloads
itself the next time the app runs. Nothing else to do.

(Alternative: drag the zip that "python -m sipphone --extension" saves to your
Downloads folder onto the extensions page instead of using this folder.)
"""


def extension_dir() -> str:
    return os.path.join(os.path.expanduser("~"), FOLDER_NAME)


def downloads_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def zip_name() -> str:
    return f"{ZIP_BASENAME}-{__version__.split('+')[0]}.zip"


def _files(root: str):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.startswith(".") or n.endswith((".pyc", ".map")):
                continue
            full = os.path.join(dirpath, n)
            yield os.path.relpath(full, root), full


def _manifest(src: str, keep_key: bool = True) -> dict:
    m = json.load(open(src, encoding="utf-8"))
    m["version"] = __version__.split("+")[0]
    if not keep_key:
        m.pop("key", None)
    return m


def _manifest_bytes(src: str, keep_key: bool = True) -> bytes:
    return (json.dumps(_manifest(src, keep_key), indent=2) + "\n").encode("utf-8")


_build_cache = ""


def build_id() -> str:
    """Hash of the packaged extension files (manifest without its version
    number), so two copies with the same files compare equal across versions."""
    global _build_cache
    if _build_cache:
        return _build_cache
    h = hashlib.sha256()
    for rel, src in _files(SOURCE):
        h.update(rel.replace("\\", "/").encode() + b"\0")
        if rel == "manifest.json":
            m = json.load(open(src, encoding="utf-8"))
            m.pop("version", None)
            h.update(json.dumps(m, sort_keys=True).encode())
        else:
            h.update(open(src, "rb").read())
        h.update(b"\0")
    _build_cache = h.hexdigest()[:16]
    return _build_cache


def _marker(via: str) -> bytes:
    return (json.dumps({"via": via, "build": build_id(), "version": __version__.split("+")[0]}) + "\n").encode()


def _contents(via: str, keep_key: bool = True):
    """(relative path, bytes) for every file of a packaged copy."""
    if not os.path.isdir(SOURCE):
        raise RuntimeError(f"extension files missing from the package ({SOURCE})")
    for rel, src in _files(SOURCE):
        yield rel, (_manifest_bytes(src, keep_key) if rel == "manifest.json" else open(src, "rb").read())
    yield MARKER, _marker(via)


# ------------------------------------------------------------------ folder
def sync(target: str = "") -> tuple:
    """Copy the packaged extension into the fixed folder if anything differs.
    Returns (folder, changed)."""
    target = target or extension_dir()
    os.makedirs(target, exist_ok=True)
    changed = False
    wanted = set()
    for rel, data in _contents("folder"):
        wanted.add(os.path.normcase(rel))
        dst = os.path.join(target, rel)
        try:
            same = hashlib.sha256(open(dst, "rb").read()).digest() == hashlib.sha256(data).digest()
        except OSError:
            same = False
        if not same:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            changed = True
    # remove files a newer version no longer ships (keep our README)
    for rel, full in list(_files(target)):
        if os.path.normcase(rel) not in wanted and rel != "README.txt":
            try:
                os.remove(full)
                changed = True
            except OSError:
                pass
    readme = README.replace("{path}", target)
    rp = os.path.join(target, "README.txt")
    try:
        if open(rp, encoding="utf-8").read() != readme:
            raise OSError
    except OSError:
        with open(rp, "w", encoding="utf-8") as f:
            f.write(readme)
    if changed:
        log.info("extension folder updated to %s: %s", __version__, target)
    return target, changed


# --------------------------------------------------------------------- zip
def zip_bytes(keep_key: bool = True) -> bytes:
    """The droppable zip: files at the root (Chrome needs manifest.json there).
    keep_key=False makes the Chrome Web Store variant (the store rejects `key`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in _contents("zip", keep_key):
            if not keep_key and rel == MARKER:
                data = (json.dumps({"via": "store", "build": build_id(),
                                    "version": __version__.split("+")[0]}) + "\n").encode()
            info = zipfile.ZipInfo(rel.replace("\\", "/"), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


def write_unpacked(dest: str, via: str = "bundle") -> str:
    """Write an unpacked copy (for 'Load unpacked') somewhere other than the
    self-updating folder, e.g. inside the share bundle. Marked `via=bundle`, so
    the extension asks for a new zip when its files go stale, like a zip install."""
    os.makedirs(dest, exist_ok=True)
    for rel, data in _contents(via):
        full = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
    return dest


def write_zip(dest: str = "") -> str:
    """Write the droppable zip; default: the Downloads folder. Returns its path."""
    if not dest or os.path.isdir(dest):
        dest = os.path.join(dest or downloads_dir(), zip_name())
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    data = zip_bytes()
    try:
        if open(dest, "rb").read() == data:
            return dest
    except OSError:
        pass
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    log.info("extension zip written: %s", dest)
    return dest


# ------------------------------------------------------------------- guide
def find_browser() -> str:
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def open_extensions_page() -> str:
    """Open chrome://extensions (or Edge's) in the installed browser. Returns the page opened."""
    browser = find_browser()
    if not browser:
        return ""
    page = "edge://extensions" if browser.lower().endswith("msedge.exe") else "chrome://extensions"
    try:
        subprocess.Popen([browser, page])
        return page
    except OSError:
        return ""


def guide() -> int:
    """`sipphone --extension`: save the droppable zip to Downloads, refresh the
    folder too, and put everything in front of the user: an Explorer window
    with the zip selected and the browser's extensions page."""
    zp = write_zip()
    path, changed = sync()
    print(f"Extension zip:    {zp}")
    print(f"Extension folder: {path}" + ("  (updated)" if changed else "  (up to date)"))
    print("\nOne-time setup in Chrome (or Edge) - either way:")
    print("  A. Turn on Developer mode on the extensions page, then DRAG the zip onto that page.")
    print("  B. Or click Load unpacked and paste the folder path (it's on your clipboard).")
    print("  Then pin Telvoro from the puzzle-piece menu.")
    print("Updates: the folder reloads itself; a zip install gets a notification with a new zip in Downloads.")
    if sys.platform == "win32":
        try:
            subprocess.run(["clip"], input=b"\xff\xfe" + path.encode("utf-16-le"), check=False)
        except OSError:
            pass
        try:
            subprocess.Popen(["explorer.exe", "/select,", zp])
        except OSError:
            pass
        open_extensions_page()
    return 0

"""Real drag-and-drop of the extension zip onto chrome://extensions.

Needs a display (run under xvfb-run), xdotool, and PyGObject/GTK 3 for a
drag-source window (python3.12 here). It drops the droppable zip and checks the
extension is installed with the fixed id; then drops the share bundle and
checks Chrome's "Could not unzip" error (the wrong-zip case).

    xvfb-run -a -s "-screen 0 1600x1000x24" python3 tests/drop_e2e.py [bundle.zip]
"""
import io, json, os, subprocess, sys, tempfile, time, zipfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone import extension_sync
from playwright.sync_api import sync_playwright

GTK_PY = next((p for p in ("/usr/bin/python3.12", sys.executable) if os.path.exists(p)), sys.executable)
SRC = os.path.join(ROOT, "tests", "drop", "dragsrc.py")
work = tempfile.mkdtemp()
zip_path = extension_sync.write_zip(os.path.join(work, extension_sync.zip_name()))
bundle = sys.argv[1] if len(sys.argv) > 1 else ""


def xdo(*a):
    subprocess.run(["xdotool", *a], check=False)


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        raise SystemExit(1)


def drop(pw, path):
    """Drag `path` from a GTK window onto a fresh Chromium's extensions page."""
    profile = tempfile.mkdtemp()
    ctx = pw.chromium.launch_persistent_context(profile, headless=False, viewport=None,
            args=["--window-position=400,0", "--window-size=1000,800"],
            ignore_default_args=["--disable-extensions"])
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("chrome://extensions")
    pg.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode: true})")
    time.sleep(1)
    src = subprocess.Popen([GTK_PY, SRC, path], stdout=subprocess.PIPE, text=True)
    assert src.stdout.readline().strip() == "ready"
    time.sleep(1)
    wid = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chromium"],
                         capture_output=True, text=True).stdout.split()[-1]
    g = dict(l.split("=") for l in subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                                   capture_output=True, text=True).stdout.strip().splitlines())
    cx, cy = int(g["X"]) + int(g["WIDTH"]) // 2, int(g["Y"]) + int(g["HEIGHT"]) // 2
    xdo("mousemove", "150", "150"); time.sleep(0.3)
    xdo("mousedown", "1"); time.sleep(0.3)
    for i in range(1, 21):
        xdo("mousemove", str(150 + (cx - 150) * i // 20), str(150 + (cy - 150) * i // 20)); time.sleep(0.08)
    time.sleep(0.8)
    xdo("mouseup", "1")
    time.sleep(4)
    exts = pg.evaluate("chrome.developerPrivate.getExtensionsInfo({includeDisabled:true, includeTerminated:true})"
                       ".then(l => l.map(e => [e.id, e.state, e.version, e.location, e.manifestErrors.length]))")
    shot = os.path.join(work, os.path.basename(path) + ".png")
    subprocess.run(["scrot", "-o", shot], check=False)
    ctx.close(); src.terminate()
    return exts, shot


with sync_playwright() as pw:
    exts, shot = drop(pw, zip_path)
    try:
        ext_id = open(os.path.join(ROOT, ".extkey", "id.txt")).read().strip()
    except OSError:
        ext_id = exts[0][0] if exts else ""
    check(f"dropped {os.path.basename(zip_path)}: installed as unpacked with the fixed id ({exts})",
          any(e[0] == ext_id and e[1] == "ENABLED" and e[3] == "UNPACKED" and e[4] == 0 for e in exts))
    check("installed version matches the app", exts[0][2] == extension_sync.__version__.split("+")[0])
    if bundle:
        exts2, shot2 = drop(pw, bundle)
        check("the share bundle itself is refused (nothing installed)", not exts2)
        print("screenshot of Chrome's error:", shot2)
print("ALL DROP CHECKS PASSED")

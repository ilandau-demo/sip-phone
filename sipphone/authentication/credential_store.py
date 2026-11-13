"""Secrets at rest.

Passwords in config.ini are stored protected: on Windows with DPAPI (bound to the
Windows user; the file is useless on another account or machine), elsewhere
base64-obfuscated with the config file chmod 600. The config layer calls
protect() when it writes a secret and reveal() when it reads one, so the rest of
the app only ever sees plain passwords in memory.

Format:  dpapi:<base64>   Windows DPAPI blob
         b64:<base64>     obfuscated (non-Windows)
         anything else    a plain value from an older config; rewritten protected on next save
"""
from __future__ import annotations

import base64
import logging
import os
import sys

log = logging.getLogger("sipphone.auth")

SECRET_KEYS = {("account", "password"), ("presence", "xsi_password"), ("admin", "password")} | {
    (f"line{n}", "password") for n in range(2, 9)}
_ENTROPY = b"sipphone-credential-store-1"


def is_protected(value: str) -> bool:
    return value.startswith(("dpapi:", "b64:"))


# ----------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    keep = []                                      # buffers must outlive the call

    def blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        keep.append(buf)
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    inp, ent, out = blob(data), blob(_ENTROPY), DATA_BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"DPAPI {'protect' if encrypt else 'unprotect'} failed ({ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


last_problem = ""          # set when a stored secret could not be read; shown in Settings


def protect(value: str) -> str:
    """Return the stored form of a secret (idempotent). The DPAPI form is only
    used if it reads back correctly on this machine right now; otherwise the
    secret is stored obfuscated rather than risk an unreadable password."""
    if not value or is_protected(value):
        return value
    raw = value.encode("utf-8")
    if sys.platform == "win32" and os.environ.get("SIPPHONE_NO_DPAPI") != "1":
        try:
            stored = "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
            if reveal(stored) == value:
                return stored
            log.warning("DPAPI round trip did not match; storing obfuscated instead")
        except Exception as e:                       # pragma: no cover - only on odd Windows setups
            log.warning("DPAPI unavailable (%s); storing obfuscated", e)
    return "b64:" + base64.b64encode(raw).decode("ascii")


def reveal(value: str) -> str:
    """The plain secret for a stored form (plain values pass through)."""
    if not value or not is_protected(value):
        return value
    kind, _, body = value.partition(":")
    try:
        raw = base64.b64decode(body.encode("ascii"))
    except Exception:
        log.warning("stored secret is not valid base64; treating as empty")
        return ""
    global last_problem
    if kind == "b64":
        return raw.decode("utf-8", errors="replace")
    if sys.platform != "win32":
        last_problem = "a Windows-protected password can only be read on the Windows account that saved it"
        log.warning(last_problem)
        return ""
    try:
        out = _dpapi(raw, False).decode("utf-8", errors="replace")
        last_problem = ""
        return out
    except Exception as e:
        last_problem = f"the stored password could not be read ({e}); enter it again in Settings"
        log.warning(last_problem)
        return ""


def restrict_file(path: str) -> None:
    """chmod 600 on POSIX (DPAPI covers Windows)."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

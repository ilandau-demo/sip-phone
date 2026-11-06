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

SECRET_KEYS = {("account", "password"), ("presence", "xsi_password"), ("admin", "password")}
_ENTROPY = b"sipphone-credential-store-1"


def is_protected(value: str) -> bool:
    return value.startswith(("dpapi:", "b64:"))


# ----------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp, ent, out = blob(data), blob(_ENTROPY), DATA_BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"DPAPI {'protect' if encrypt else 'unprotect'} failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(value: str) -> str:
    """Return the stored form of a secret (idempotent)."""
    if not value or is_protected(value):
        return value
    raw = value.encode("utf-8")
    if sys.platform == "win32" and os.environ.get("SIPPHONE_NO_DPAPI") != "1":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
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
    if kind == "b64":
        return raw.decode("utf-8", errors="replace")
    if sys.platform != "win32":
        log.warning("a DPAPI-protected secret can only be read on the Windows account that saved it")
        return ""
    try:
        return _dpapi(raw, False).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("could not read the stored password (%s); enter it again in Settings", e)
        return ""


def restrict_file(path: str) -> None:
    """chmod 600 on POSIX (DPAPI covers Windows)."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

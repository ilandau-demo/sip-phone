"""Read and update config.ini from the Settings screen.

Edits are made line by line so everything the user (or the template) wrote
stays put: comments, blank lines, key order, alignment. Only the value part
of a changed line is replaced; keys that don't exist yet are added at the end
of their section, and missing sections are appended.

Values are stored raw (no %-interpolation), which is how load_config reads
them, so passwords containing % work.
"""
from __future__ import annotations

import configparser
import os
import re
import tempfile
from typing import Dict, Optional

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY = re.compile(r"^(?P<lead>[A-Za-z0-9_.\-]+)(?P<sep>\s*[=:]\s*)(?P<rest>.*?)(?P<nl>\r?\n)?$")
_INLINE = re.compile(r"\s+[;#]")


def read(path: str) -> configparser.ConfigParser:
    """Parse *path*; stored secrets come back as plain values."""
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cp.read(path, encoding="utf-8")
    reveal_secrets(cp)
    return cp


def reveal_secrets(cp: configparser.ConfigParser) -> None:
    from .authentication.credential_store import SECRET_KEYS, reveal
    for sec, key in SECRET_KEYS:
        if cp.has_section(sec) and cp.has_option(sec, key):
            cp.set(sec, key, reveal(cp.get(sec, key)))


def check_value(v: str) -> Optional[str]:
    """Return a problem description if *v* can't be stored as an ini value."""
    if "\n" in v or "\r" in v:
        return "can't contain line breaks"
    if v != v.strip():
        return "can't start or end with a space"
    if _INLINE.search(v):
        return "can't contain a space followed by ; or # (it would be read as a comment)"
    return None


def _split_value(rest: str):
    """'value     ; comment' -> ('value', '     ; comment')."""
    m = _INLINE.search(rest)
    if not m:
        return rest.rstrip(), rest[len(rest.rstrip()):]
    return rest[:m.start()], rest[m.start():]


def update(path: str, changes: Dict[str, Dict[str, str]]) -> None:
    """Write {section: {key: value}} into *path*, preserving everything else.
    Secrets (passwords) are stored protected — see authentication.credential_store."""
    from .authentication.credential_store import SECRET_KEYS, protect, restrict_file
    for sec, kv in changes.items():
        for k, v in kv.items():
            prob = check_value(str(v))
            if prob:
                raise ValueError(f"[{sec}] {k} {prob}")
    changes = {sec: {k: (protect(str(v)) if (sec.lower(), k.lower()) in SECRET_KEYS else v) for k, v in kv.items()}
               for sec, kv in changes.items()}

    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    todo = {s.lower(): {k.lower(): str(v) for k, v in kv.items()} for s, kv in changes.items()}
    names = {s.lower(): s for s in changes}
    section = None
    last_key_line: Dict[str, int] = {}
    sec_header_line: Dict[str, int] = {}
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip().lower()
            sec_header_line.setdefault(section, i)
            continue
        if section is None or not line.strip() or line.lstrip()[:1] in (";", "#") or line[:1].isspace():
            continue
        km = _KEY.match(line)
        if not km:
            continue
        key = km.group("lead").lower()
        last_key_line[section] = i
        want = todo.get(section, {})
        if key not in want:
            continue
        new = want.pop(key)
        sep, rest = km.group("sep"), km.group("rest")
        eq = sep.rstrip()                          # e.g. "      ="
        if not rest.strip() or rest.lstrip()[:1] in (";", "#"):
            old, comment = "", rest                # empty value (maybe followed by a comment)
        else:
            old, comment = _split_value(rest)
        if comment.strip():
            # keep the comment in the same column when the new value fits
            col = (len(sep) - len(eq)) + len(old) + (len(comment) - len(comment.lstrip()))
            body = " " + new + " " * max(1, col - 1 - len(new)) + comment.lstrip()
        else:
            body = (" " + new) if new else ""
        lines[i] = f"{km.group('lead')}{eq}{body}{km.group('nl') or nl}"

    # keys not found: insert after the last key of their section (or after its header)
    inserts = []
    appended = []
    for sec, kv in todo.items():
        if not kv:
            continue
        new_lines = [f"{k} = {v}{nl}" for k, v in kv.items()]
        if sec in sec_header_line:
            at = last_key_line.get(sec, sec_header_line[sec]) + 1
            inserts.append((at, new_lines))
        else:
            appended.append([nl, f"[{names.get(sec, sec)}]{nl}"] + new_lines)
    for at, new_lines in sorted(inserts, key=lambda x: -x[0]):
        lines[at:at] = new_lines
    for block in appended:
        lines.extend(block)

    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        if os.path.exists(path):
            try:
                with open(path, "rb") as src, open(path + ".bak", "wb") as bak:
                    bak.write(src.read())
            except OSError:
                pass
        os.replace(tmp, path)
        restrict_file(path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

"""HTTP Digest authentication for SIP (RFC 3261 §22, RFC 2617, RFC 7616).

Supports MD5 / MD5-sess and SHA-256, qop=auth and qop=auth-int, which covers
BroadSoft-based platforms such as Nextiva (MD5, qop=auth).
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, Optional

_PARAM_RE = re.compile(r'(\w[\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')


def parse_challenge(header: str) -> Dict[str, str]:
    """Parse a `WWW-Authenticate: Digest realm="...", nonce="..."` value."""
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "digest":
        raise ValueError(f"unsupported auth scheme: {scheme}")
    out: Dict[str, str] = {}
    for m in _PARAM_RE.finditer(rest):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else m.group(3)
        out[key] = val.replace('\\"', '"')
    return out


def _hasher(algorithm: str):
    alg = (algorithm or "MD5").upper().replace("-SESS", "")
    if alg == "MD5":
        return hashlib.md5
    if alg == "SHA-256":
        return hashlib.sha256
    if alg == "SHA-512-256":
        return lambda b: hashlib.new("sha512_256", b)
    raise ValueError(f"unsupported digest algorithm: {algorithm}")


def build_authorization(
    challenge: Dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
    body: bytes = b"",
    nonce_count: int = 1,
    cnonce: Optional[str] = None,
) -> str:
    """Compute the Authorization header value for a challenge."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    opaque = challenge.get("opaque")
    algorithm = challenge.get("algorithm", "MD5")
    qop_offered = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    H = _hasher(algorithm)

    def h(s: str) -> str:
        return H(s.encode("utf-8")).hexdigest()

    cnonce = cnonce or os.urandom(8).hex()
    nc = f"{nonce_count:08x}"

    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")

    qop = None
    if "auth" in qop_offered:
        qop = "auth"
    elif "auth-int" in qop_offered:
        qop = "auth-int"

    if qop == "auth-int":
        ha2 = h(f"{method}:{uri}:{H(body).hexdigest()}")
    else:
        ha2 = h(f"{method}:{uri}")

    if qop:
        response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if qop:
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    if opaque is not None:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def verify(authorization: str, password: str, method: str, body: bytes = b"") -> bool:
    """Server-side check (used by the test registrar)."""
    p = parse_challenge(authorization)  # same grammar
    expected = build_authorization(
        {"realm": p.get("realm", ""), "nonce": p.get("nonce", ""),
         "qop": p.get("qop", ""), "algorithm": p.get("algorithm", "MD5"),
         "opaque": p.get("opaque")},
        p.get("username", ""), password, method, p.get("uri", ""), body,
        nonce_count=int(p.get("nc", "1"), 16), cnonce=p.get("cnonce"),
    )
    return parse_challenge(expected).get("response") == p.get("response")

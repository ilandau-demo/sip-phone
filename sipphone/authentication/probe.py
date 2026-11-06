"""Try a login and report, in plain words, what it can and cannot do."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Union

from .admin_credentials import AdminCredentials
from .user_credentials import UserCredentials

Credentials = Union[UserCredentials, AdminCredentials]


@dataclass
class AuthResult:
    ok: bool
    message: str
    people: int = 0
    services: int = 0
    notes: List[str] = field(default_factory=list)


def probe(creds: Credentials) -> AuthResult:
    """Authenticate and read the directory. Never guesses permissions beyond what
    the server says; monitoring rights are only known once subscriptions run."""
    from ..xsi import XSIClient, XSIError, service_type
    if not creds.complete:
        return AuthResult(False, f"Enter the {'admin' if creds.kind == 'admin' else 'user'} login and password.")
    client = XSIClient(creds.host, creds.login, creds.password, verify_tls=creds.verify_tls, user_domain=creds.user_domain)
    try:
        entries = client.list_directory_users(creds.directory_seed, getattr(creds, "directory_type", "Enterprise"))
    except XSIError as e:
        text = str(e)
        if "authentication" in text or "401" in text:
            return AuthResult(False, f"Login failed: the {creds.describe()} was not accepted. Check the login and password.")
        if "403" in text or "forbidden" in text.lower():
            return AuthResult(False, f"Login OK, but this login may not read the directory ({text}).")
        if "404" in text and creds.kind == "admin":
            return AuthResult(False, f"The directory user wasn't found ({text}). Enter a real user ID under Directory user.")
        return AuthResult(False, f"Could not reach the XSI server: {text}")
    people = [d for d in entries if not service_type(d.name, d.user_id)]
    svc = len(entries) - len(people)
    msg = f"Login OK. The directory lists {len(people)} people and {svc} services. " \
          "The phone system decides who this login may watch; anyone it refuses shows as Unknown."
    return AuthResult(True, msg, people=len(people), services=svc)

"""Authentication: exactly two ways in — a user's own login, or an Admin XSI login.

credential_store  protects secrets at rest (DPAPI on Windows)
user_credentials  the individual user's XSI login
admin_credentials the Admin XSI login (desktop app only; never sent to the browser)
"""
from .credential_store import protect, reveal, is_protected, SECRET_KEYS   # noqa: F401
from .user_credentials import UserCredentials                                # noqa: F401
from .admin_credentials import AdminCredentials                              # noqa: F401
from .probe import probe, AuthResult                                         # noqa: F401


def from_options(mode: str, opts: dict):
    """Credentials for a presence source from the merged [presence]/[admin] options
    (the admin_opts dict cli.load_config builds). mode: 'events' | 'admin'."""
    host = (opts.get("host") or "https://xsi.nextiva.com").strip()
    if not host.startswith(("https://", "http://")):
        host = "https://" + host
    common = dict(host=host, user_domain=opts.get("user_domain") or "nextiva.com",
                  verify_tls=bool(opts.get("verify_tls", True)))
    if mode == "admin":
        return AdminCredentials(opts.get("username", ""), opts.get("password", ""),
                                directory_user=opts.get("directory_user", "") or "",
                                directory_type={"group": "Group"}.get(str(opts.get("directory_type", "")).lower(), "Enterprise"),
                                **common)
    return UserCredentials(opts.get("xsi_user", ""), opts.get("xsi_password", ""), **common)

"""An Admin XSI login. Lives in the desktop app only: the browser extension never
receives it (Team Presence Only works with a user login)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdminCredentials:
    username: str
    password: str
    host: str = "https://xsi.nextiva.com"
    user_domain: str = "nextiva.com"
    verify_tls: bool = True
    directory_user: str = ""      # a real user whose directory lists everyone
    directory_type: str = "Enterprise"
    kind: str = "admin"

    @property
    def complete(self) -> bool:
        return bool(self.username.strip() and self.password)

    @property
    def login(self) -> str:
        return self.username.strip()

    @property
    def directory_seed(self) -> str:
        d = self.directory_user.strip()
        if d and "@" not in d and self.user_domain:
            d = f"{d}@{self.user_domain}"
        return d or self.login

    def describe(self) -> str:
        return f"Admin XSI login {self.login}"

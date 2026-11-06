"""An individual user's XSI login."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UserCredentials:
    user_id: str                  # jdoe1234567@example.com (domain added from user_domain if missing)
    password: str
    host: str = "https://xsi.nextiva.com"
    user_domain: str = "nextiva.com"
    verify_tls: bool = True
    kind: str = "user"

    @property
    def complete(self) -> bool:
        return bool(self.user_id.strip() and self.password)

    @property
    def login(self) -> str:
        u = self.user_id.strip()
        return u if "@" in u or not self.user_domain else f"{u}@{self.user_domain}"

    @property
    def directory_seed(self) -> str:
        """The user whose directory view we read: the user themself."""
        return self.login

    def describe(self) -> str:
        return f"user login {self.login}"

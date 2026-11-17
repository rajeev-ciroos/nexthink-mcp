from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .errors import ConfigError


@dataclass(frozen=True)
class Settings:
    base_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    mcp_auth_token: str | None = field(default=None, repr=False)

    @property
    def missing(self) -> list[str]:
        names = {
            "NEXTHINK_BASE_URL": self.base_url,
            "NEXTHINK_CLIENT_ID": self.client_id,
            "NEXTHINK_CLIENT_SECRET": self.client_secret,
        }
        return [k for k, v in names.items() if not v]

    def require(self) -> None:
        if self.missing:
            raise ConfigError(f"Missing configuration: set {', '.join(self.missing)}.")
        if urlparse(self.base_url).scheme not in ("http", "https") or not urlparse(self.base_url).netloc:
            raise ConfigError("NEXTHINK_BASE_URL must be an http(s) URL such as https://<instance>.api.<region>.nexthink.cloud")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            base_url=(env("NEXTHINK_BASE_URL") or "").rstrip("/") or None,
            client_id=env("NEXTHINK_CLIENT_ID") or None,
            client_secret=env("NEXTHINK_CLIENT_SECRET") or None,
            mcp_auth_token=env("NEXTHINK_MCP_AUTH_TOKEN") or None,
        )

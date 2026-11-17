from __future__ import annotations


class ConfigError(Exception):
    """Missing or invalid configuration."""


class NexthinkError(Exception):
    """An error response from Nexthink or its token endpoint."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @property
    def hint(self) -> str | None:
        if self.status == 401:
            return "Authentication failed: check NEXTHINK_CLIENT_ID and NEXTHINK_CLIENT_SECRET and that the API credential is enabled."
        if self.status == 403:
            return "Access denied: the API credential probably lacks permission to run NQL queries (unverified: see README)."
        if self.status == 429:
            return "Throttled by Nexthink; retry later or narrow the query."
        return None

    def __str__(self) -> str:
        parts = [f"Nexthink error {self.status} ({self.code}): {self.message}"]
        if self.hint:
            parts.append(self.hint)
        return " ".join(parts)

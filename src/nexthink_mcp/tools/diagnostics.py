from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..errors import ConfigError, NexthinkError
from .context import LATENCY, AppContext


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "Verify configuration, authentication and NQL access (gets a token, runs a 1-row query). Run this first if other "
        "tools fail. Never returns secrets. Read-only. Limits: 1 row. " + LATENCY
    ))
    async def check_connection() -> dict:
        s = ctx.settings
        result: dict = {"baseUrl": s.base_url, "clientId": s.client_id, "missingConfiguration": s.missing}
        if s.missing:
            result["status"] = "not_configured"
            return result
        try:
            result.update(await ctx.client.check())
            result["status"] = "ok"
        except (NexthinkError, ConfigError) as exc:
            result["status"] = "error"
            result["error"] = str(exc)
        return result

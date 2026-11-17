from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .context import LATENCY, AppContext, bounded


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "List devices, sorted by name, optionally filtered. Every device has a stable deviceId and its last logged-on userId. "
        "Filters are case-insensitive substring matches and are combined with AND: name_contains, site (site name), "
        "os_name (e.g. 'Windows 11'), user (user name). "
        "Limits: at most `limit` devices (default 50, max 200); `truncated` is true when more match. Fields come from the "
        "last inventory sync, so lastSeen can be hours behind. " + LATENCY
    ))
    async def list_devices(
        name_contains: str | None = None, site: str | None = None, os_name: str | None = None,
        user: str | None = None, limit: int = 50,
    ) -> dict:
        bounded("limit", limit, 1, 200)
        page = await ctx.client.list_devices(name_contains=name_contains, site=site, os_name=os_name, user=user, limit=limit)
        return {"count": len(page.items), "limit": limit, "truncated": page.truncated, "devices": page.items}

    @mcp.tool(description=(
        "Get one device by its stable deviceId (the `deviceId` from list_devices): inventory, installed applications, "
        "the most recent boots and a summary of crashes by application over the last 14 days. "
        "Returns found=false if the ID is unknown. Limits: 200 installed applications, 5 boots, top 20 crash rows; each list "
        "reports its own `truncated` flag. " + LATENCY
    ))
    async def get_device(device_id: str) -> dict:
        got = await ctx.client.get_device(device_id)
        if got is None:
            return {"found": False, "deviceId": device_id}
        return {
            "found": True,
            "device": got["device"],
            "installedApplications": {"count": len(got["applications"].items), "limit": 200, "truncated": got["applications"].truncated, "items": got["applications"].items},
            "recentBoots": got["boots"],
            "crashesLast14Days": {"limit": 20, "truncated": got["crashes"].truncated, "items": got["crashes"].items},
        }

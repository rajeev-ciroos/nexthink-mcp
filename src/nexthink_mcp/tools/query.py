from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .context import LATENCY, AppContext, bounded


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "Run a read-only NQL query (Nexthink Query Language) and return the rows. Use it for questions the other tools do "
        "not cover; prefer them when they fit. Tables the other tools use: devices, installations, execution.crashes, "
        "device_performance.boots. Add uid fields (e.g. uid, device.uid, user.uid) to the list/summarize so results carry "
        "stable device and user IDs. "
        "Limits: at most max_rows rows (default 100, max 500); a trailing '| limit N' above max_rows is lowered to max_rows. "
        "When more rows exist, `truncated` is true and only the first max_rows are returned. Synchronous call: for bigger "
        "extracts narrow the query instead. NQL cannot modify anything. " + LATENCY
    ))
    async def run_nql_query(query: str, max_rows: int = 100) -> dict:
        bounded("max_rows", max_rows, 1, 500)
        res = await ctx.client.run_query(query, max_rows)
        return {
            "executedQuery": res.executed_query, "executedAt": res.executed_at, "rowCount": len(res.rows),
            "maxRows": max_rows, "truncated": res.truncated, "rows": res.rows,
        }

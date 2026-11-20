from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .context import LATENCY, AppContext, bounded


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "Run one saved Nexthink query by its ID (for example '#ciroos_devices') and return the rows. Nexthink only runs "
        "saved queries that an administrator has created and enabled for API use, so this can reach only those; it cannot "
        "run free-form NQL. Use it for a saved query the other tools do not cover; prefer them when they fit. Pass "
        "`parameters` only if the saved query declares them. Read-only. "
        "Limits: at most max_rows rows (default 100, max 500) and never more than the synchronous API limit; when more "
        "rows exist `truncated` is true and only the first max_rows are returned. For big extracts use a query that "
        "summarises. " + LATENCY
    ))
    async def run_saved_query(query_id: str, parameters: dict[str, str | int | float | bool] | None = None, max_rows: int = 100) -> dict:
        bounded("max_rows", max_rows, 1, 500)
        res = await ctx.client.run_saved_query(query_id, parameters, max_rows)
        return {
            "savedQueryId": res.query_id, "executedAt": res.executed_at, "rowCount": len(res.rows),
            "maxRows": max_rows, "truncated": res.truncated, "rows": res.rows,
        }

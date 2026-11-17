"""Connect to the server like a real MCP client, call check_connection and summarize_experience.

  python scripts/smoke_client.py stdio
  python scripts/smoke_client.py http http://127.0.0.1:8000/mcp <NEXTHINK_MCP_AUTH_TOKEN>
"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


async def run(session: ClientSession) -> None:
    await session.initialize()
    print("tools:", [t.name for t in (await session.list_tools()).tools])
    res = await session.call_tool("check_connection", {})
    print("check_connection:", json.dumps(json.loads(res.content[0].text), indent=2))
    res = await session.call_tool("summarize_experience", {"group_by": "site", "days": 7})
    out = json.loads(res.content[0].text)
    print("summarize_experience highlights:", out.get("highlights"), "truncated:", out.get("truncated"))
    for g in out.get("groups", [])[:3]:
        print("  ", g["group"], "crashes:", g["crashes"], "boots:", g["boots"])


async def main() -> None:
    if sys.argv[1] == "stdio":
        params = StdioServerParameters(command=sys.executable, args=["-m", "nexthink_mcp.server"])
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await run(s)
    else:
        headers = {"Authorization": f"Bearer {sys.argv[3]}"} if len(sys.argv) > 3 else None
        async with streamablehttp_client(sys.argv[2], headers=headers) as (r, w, _), ClientSession(r, w) as s:
            await run(s)


asyncio.run(main())

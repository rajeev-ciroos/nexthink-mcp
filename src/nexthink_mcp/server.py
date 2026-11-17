from __future__ import annotations

import argparse
import hmac
import logging
import sys

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .client import NexthinkClient
from .config import Settings
from .tools import applications, devices, diagnostics, experience, query
from .tools.context import AppContext

INSTRUCTIONS = (
    "Read-only access to Nexthink digital employee experience data (no writes, no Remote Actions). "
    "list_devices / get_device give inventory with stable deviceId and userId; list_applications shows deployed versions; "
    "get_app_crashes explains crashes of one application; summarize_experience aggregates crashes and boot times by "
    "site, app version, OS build, network type or day to localise a problem; run_nql_query is the escape hatch. "
    "Data is near-real-time but a few minutes behind. Check `truncated` in every result. Run check_connection first if anything fails. "
    "All times are UTC."
)


def build_server(
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    transport: httpx.AsyncBaseTransport | None = None,
    ctx: AppContext | None = None,
) -> FastMCP:
    settings = settings or Settings.from_env()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    mcp = FastMCP(
        "nexthink-mcp",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        # Host-header allowlisting only makes sense on loopback; remote use is guarded by the bearer token.
        transport_security=None if loopback else TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    ctx = ctx or AppContext(settings, NexthinkClient(settings, transport=transport))
    for module in (diagnostics, query, devices, applications, experience):
        module.register(mcp, ctx)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):  # noqa: ARG001
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    return mcp


class BearerAuth:
    """Minimal ASGI middleware: require 'Authorization: Bearer <token>' on everything but /healthz."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexthink-mcp", description="Read-only Nexthink MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = Settings.from_env()
    if settings.missing:
        logging.warning("Not configured (%s); tools will report errors until set.", ", ".join(settings.missing))
    if args.transport == "http" and not settings.mcp_auth_token:
        sys.exit("Refusing to serve HTTP without NEXTHINK_MCP_AUTH_TOKEN: anyone who could reach the port could query your Nexthink data.")
    mcp = build_server(settings, args.host, args.port)
    if args.transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth, token=settings.mcp_auth_token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

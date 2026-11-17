"""A scripted fake Nexthink behind httpx.MockTransport: records requests, answers by query substring."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx

from nexthink_mcp.client import NexthinkClient
from nexthink_mcp.config import Settings
from nexthink_mcp.server import build_server

SETTINGS = Settings(base_url="https://nx.test", client_id="client-1", client_secret="s3cr3t-value", mcp_auth_token="tok")
RESULTS_URL = "https://files.test/results/abc?sig=xyz"


class FakeNexthink:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.rules: list[tuple[str, Callable[[str], list[dict]] | list[dict]]] = []
        self.token_calls = 0
        self.export_rows: dict[str, list[dict]] = {}
        self.status_script: list[dict] = [{"status": "SUBMITTED"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED", "resultsFileUrl": RESULTS_URL}]
        self.fail_next: list[int] = []  # statuses to answer with before behaving
        self.revoke_token_once = False

    def on(self, needle: str, rows):
        self.rules.append((needle, rows))
        return self

    def queries(self) -> list[str]:
        return [json.loads(r.content)["query"] for r in self.requests if r.url.path.endswith(("/execute", "/export"))]

    def _rows(self, query: str) -> list[dict]:
        for needle, rows in self.rules:
            if needle in query:
                return rows(query) if callable(rows) else rows
        return []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/api/v1/token":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.token_calls}", "token_type": "Bearer", "expires_in": 3600})
        if req.url.host == "files.test":
            return httpx.Response(200, json={"data": self.export_rows["abc"], "executionDateTime": "2026-11-17T12:00:00Z"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"}, json={"code": "BUSY", "message": "busy"})
        if self.revoke_token_once and req.headers["authorization"] == "Bearer tok-1":
            self.revoke_token_once = False
            return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "expired"})
        if path == "/api/v1/nql/execute":
            q = json.loads(req.content)["query"]
            rows = self._rows(q)
            return httpx.Response(200, json={"executedQuery": q, "rows": len(rows), "data": rows, "executionDateTime": "2026-11-17T12:00:00Z"})
        if path == "/api/v1/nql/export":
            self.export_rows["abc"] = self._rows(json.loads(req.content)["query"])
            return httpx.Response(200, json={"exportId": "abc"})
        if path == "/api/v1/nql/status/abc":
            step = self.status_script.pop(0) if len(self.status_script) > 1 else self.status_script[0]
            return httpx.Response(200, json=step)
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": path})


async def _instant(_):
    return None


def make(fake: FakeNexthink, settings: Settings = SETTINGS, **client_kw):
    client = NexthinkClient(settings, transport=httpx.MockTransport(fake), sleep=_instant, poll_interval_s=0, **client_kw)
    from nexthink_mcp.tools.context import AppContext

    return build_server(settings, ctx=AppContext(settings, client)), client


async def call(mcp, name, args=None):
    content = await mcp.call_tool(name, args or {})
    if isinstance(content, tuple):  # (content, structured) in newer SDKs
        content = content[0]
    return json.loads(content[0].text)


def run(coro):
    return asyncio.run(coro)


def device_row(uid="dev-1", name="CHI-LT-0001", **kw):
    row = {
        "uid": uid, "name": name, "last_logged_on_user.uid": "usr-1", "last_logged_on_user.name": "Ava Nguyen", "site.uid": "site-1",
        "site.name": "Chicago Office", "operating_system.name": "Windows 11 Enterprise", "operating_system.version": "10.0.22631.4602",
        "hardware.manufacturer": "Dell", "hardware.model": "Latitude 7440", "network.type": "wifi", "network.ip_subnet": "10.13.2.0/24",
        "last_seen": "2026-11-17T11:00:00Z",
    }
    return {**row, **kw}


def crash_event(t="2026-11-17T10:00:00Z", device="dev-1", user="usr-1", site="Chicago Office", net="wifi", app="Zoom Workplace", ver="6.2.0", os="10.0.22631.4602"):
    return {"time": t, "device.uid": device, "device.name": "n-" + device, "user.uid": user, "user.name": "u", "site.uid": "s", "site.name": site,
            "network.type": net, "operating_system.version": os, "application.name": app, "application.version": ver, "crash.type": "access_violation"}


def boot_event(t="2026-11-17T08:00:00Z", device="dev-1", user="usr-1", site="Chicago Office", net="wifi", secs=40.0, os="10.0.22631.4602"):
    return {"time": t, "device.uid": device, "device.name": "n-" + device, "user.uid": user, "user.name": "u", "site.uid": "s", "site.name": site,
            "network.type": net, "operating_system.version": os, "boot.duration_s": secs}

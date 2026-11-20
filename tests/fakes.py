"""A scripted fake Nexthink behind httpx.MockTransport: records requests, answers by saved-query ID. Like a real tenant,
it refuses requests without a queryId ("request.queryId: must not be blank")."""
from __future__ import annotations

import asyncio
import csv
import io
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
        self.rules: list[tuple[str, Callable[[dict], list[dict]] | list[dict]]] = []
        self.csv_results = False  # serve export results as CSV instead of JSON
        self.token_calls = 0
        self.export_rows: dict[str, list[dict]] = {}
        self.status_script: list[dict] = [{"status": "SUBMITTED"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED", "resultsFileUrl": RESULTS_URL}]
        self.fail_next: list[int] = []  # statuses to answer with before behaving
        self.revoke_token_once = False
        self.token_status = 200  # answer the token request with this status

    def on(self, needle: str, rows):
        self.rules.append((needle, rows))
        return self

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(("/execute", "/export"))]

    def queries(self) -> list[str]:
        """The saved-query IDs requested, in order."""
        return [b.get("queryId") for b in self.bodies()]

    def _rows(self, body: dict) -> list[dict]:
        for query_id, rows in self.rules:
            if query_id == body.get("queryId"):
                return rows(body) if callable(rows) else rows
        return []

    def _results(self, rows: list[dict]) -> httpx.Response:
        if not self.csv_results:
            return httpx.Response(200, json={"data": rows, "executionDateTime": "2026-11-17T12:00:00Z"})
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        return httpx.Response(200, text=out.getvalue(), headers={"content-type": "text/csv"})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/api/v1/token":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_request", "error_description": "token endpoint said no"})
            return httpx.Response(200, json={"access_token": f"tok-{self.token_calls}", "token_type": "Bearer", "expires_in": 3600})
        if req.url.host == "files.test":
            return self._results(self.export_rows["abc"])
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"}, json={"code": "BUSY", "message": "busy"})
        if self.revoke_token_once and req.headers["authorization"] == "Bearer tok-1":
            self.revoke_token_once = False
            return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "expired"})
        if path in ("/api/v1/nql/execute", "/api/v1/nql/export"):
            body = json.loads(req.content)
            if not body.get("queryId"):
                return httpx.Response(400, json={"code": "INVALID_REQUEST", "message": "request.queryId: must not be blank"})
            rows = self._rows(body)
            if path.endswith("/export"):
                self.export_rows["abc"] = rows
                return httpx.Response(200, json={"exportId": "abc"})
            return httpx.Response(200, json={"queryId": body["queryId"], "rows": len(rows), "data": rows, "executionDateTime": "2026-11-17T12:00:00Z"})
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

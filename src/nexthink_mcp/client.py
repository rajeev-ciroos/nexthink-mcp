"""The ONLY module that knows how Nexthink's API looks: URL paths, auth, request/response payloads, NQL table and
field names, row limits. Tools call the typed methods at the bottom and get normalised dicts (deviceId, userId, ...),
never Nexthink field names.

Everything marked ASSUMPTION is unverified against a real Nexthink instance; each one is listed in the README. The
simulator (nexthink-simulator) implements these same assumptions, so passing tests against it proves nothing about them.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .errors import NexthinkError

# ---- ASSUMPTIONS: endpoints ------------------------------------------------------------------------------------------
TOKEN_PATH = "/api/v1/token"  # client-credentials; client id/secret sent as HTTP Basic, grant_type in a form body
TOKEN_SCOPE: str | None = None  # ASSUMPTION: no scope parameter needed. Permissions are set on the API credential in Nexthink.
EXECUTE_PATH = "/api/v1/nql/execute"  # POST {"query": "<NQL>"} -> {"executedQuery","rows","data":[{...}],"executionDateTime"}
EXPORT_PATH = "/api/v1/nql/export"  # POST {"query": "<NQL>"} -> {"exportId": "..."}
STATUS_PATH = "/api/v1/nql/status/{export_id}"  # GET -> {"status": SUBMITTED|IN_PROGRESS|COMPLETED|ERROR, "resultsFileUrl", "errorDescription"}
STATUS_PENDING = ("SUBMITTED", "IN_PROGRESS")
STATUS_DONE, STATUS_FAILED = "COMPLETED", "ERROR"
# ASSUMPTION: resultsFileUrl is a pre-signed URL (often another host); the bearer token must NOT be sent to it.
RESULTS_URL_SENDS_BEARER = False
# ASSUMPTION: the results file is JSON shaped like the execute response ({"data": [...]}). A real export may be CSV.
RESULTS_ROWS_KEY = "data"

# ---- ASSUMPTIONS: limits and timing ----------------------------------------------------------------------------------
EXECUTE_ROW_LIMIT = 1000  # ASSUMPTION: rows the synchronous execute call accepts (the query must carry its own limit)
EXPORT_ROW_LIMIT = 100_000  # ASSUMPTION: rows an export can return
POLL_INTERVAL_S = 1.0
EXPORT_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 30.0
MAX_RETRIES = 3
MAX_RETRY_SLEEP_S = 30.0
RETRY_STATUSES = {429, 502, 503, 504}

# ---- ASSUMPTIONS: NQL schema (table and field names) -----------------------------------------------------------------
DEVICE_FIELDS = {
    "uid": "deviceId", "name": "deviceName", "last_logged_on_user.uid": "userId", "last_logged_on_user.name": "userName",
    "site.uid": "siteId", "site.name": "siteName", "operating_system.name": "os", "operating_system.version": "osVersion",
    "hardware.manufacturer": "manufacturer", "hardware.model": "model", "network.type": "networkType",
    "network.ip_subnet": "subnet", "last_seen": "lastSeen",
}
EVENT_REF_FIELDS = {
    "time": "time", "device.uid": "deviceId", "device.name": "deviceName", "user.uid": "userId", "user.name": "userName",
    "site.uid": "siteId", "site.name": "siteName", "network.type": "networkType", "operating_system.version": "osVersion",
}
CRASH_FIELDS = {**EVENT_REF_FIELDS, "application.name": "application", "application.version": "appVersion", "crash.type": "crashType"}
BOOT_FIELDS = {**EVENT_REF_FIELDS, "boot.duration_s": "bootSeconds"}
INSTALL_FIELDS = {
    "application.name": "application", "application.publisher": "publisher", "application.version": "appVersion",
    "installed_at": "installedAt",
}

_LIMIT_RE = re.compile(r"\|\s*limit\s+(\d+)\s*$", re.I)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def nql_str(value: str) -> str:
    """A double-quoted NQL string literal. Values that could break out of the literal are rejected, not escaped."""
    v = value.strip() if isinstance(value, str) else ""
    if not v or len(v) > 200 or '"' in v or "\\" in v or any(ord(c) < 32 for c in v):
        raise ValueError("filter values must be 1-200 characters without double quotes, backslashes or control characters")
    return f'"{v}"'


def _where(*conds: str | None) -> str:
    conds = [c for c in conds if c]
    return f"| where {' and '.join(conds)}\n" if conds else ""


def _rename(row: dict, mapping: dict[str, str]) -> dict:
    return {out: row.get(src) for src, out in mapping.items()}


def _with_limit(nql: str, max_rows: int) -> str:
    """Make the query return at most max_rows + 1 rows (the extra row is how truncation is detected)."""
    q = nql.strip()
    m = _LIMIT_RE.search(q)
    if m and int(m.group(1)) <= max_rows:
        return q
    if m:
        q = q[: m.start()].rstrip()
    return f"{q}\n| limit {max_rows + 1}"


def _error_from_response(resp: httpx.Response) -> NexthinkError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = body.get("code") or body.get("error") or "unknown"
    message = body.get("message") or body.get("error_description") or resp.reason_phrase or "request failed"
    return NexthinkError(resp.status_code, str(code), str(message).splitlines()[0])


@dataclass
class QueryResult:
    rows: list[dict]
    truncated: bool
    executed_query: str
    executed_at: str | None


@dataclass
class Page:
    items: list[dict]
    truncated: bool


class NexthinkClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_s: float = POLL_INTERVAL_S,
        export_timeout_s: float = EXPORT_TIMEOUT_S,
    ):
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self._poll = poll_interval_s
        self._export_timeout = export_timeout_s
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    # ---- transport -------------------------------------------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        self.settings.require()
        return httpx.AsyncClient(transport=self._transport, timeout=REQUEST_TIMEOUT_S, follow_redirects=False)

    async def _get_token(self, http: httpx.AsyncClient) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            data = {"grant_type": "client_credentials", **({"scope": TOKEN_SCOPE} if TOKEN_SCOPE else {})}
            resp = await self._request(
                http, "POST", self.settings.base_url + TOKEN_PATH, data=data,
                auth=(self.settings.client_id, self.settings.client_secret), bearer=False,
            )
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    async def _request(
        self, http: httpx.AsyncClient, method: str, url: str, *, json: Any = None, data: Any = None,
        auth: tuple[str, str] | None = None, bearer: bool = True,
    ) -> httpx.Response:
        attempt, refreshed = 0, False
        while True:
            headers = {"Accept": "application/json"}
            if bearer:
                headers["Authorization"] = f"Bearer {await self._get_token(http)}"
            resp = await http.request(method, url, json=json, data=data, auth=auth, headers=headers)
            if resp.status_code == 401 and bearer and not refreshed:
                self._token, refreshed = None, True
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                try:
                    delay = float(resp.headers["Retry-After"])
                except (KeyError, ValueError):
                    delay = float(2**attempt)
                await self._sleep(min(delay, MAX_RETRY_SLEEP_S))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise _error_from_response(resp)
            return resp

    # ---- NQL execution ---------------------------------------------------------------------------------------------
    async def _execute(self, nql: str, max_rows: int) -> QueryResult:
        if max_rows >= EXECUTE_ROW_LIMIT:
            raise ValueError(f"max_rows must be below {EXECUTE_ROW_LIMIT} for synchronous queries")
        query = _with_limit(nql, max_rows)
        async with self._http() as http:
            body = (await self._request(http, "POST", self.settings.base_url + EXECUTE_PATH, json={"query": query})).json()
        rows = body.get("data") or []
        return QueryResult(rows[:max_rows], len(rows) > max_rows, query, body.get("executionDateTime"))

    async def _export(self, nql: str, max_rows: int) -> QueryResult:
        if max_rows >= EXPORT_ROW_LIMIT:
            raise ValueError(f"max_rows must be below {EXPORT_ROW_LIMIT} for exports")
        query = _with_limit(nql, max_rows)
        async with self._http() as http:
            exported = (await self._request(http, "POST", self.settings.base_url + EXPORT_PATH, json={"query": query})).json()
            export_id = exported.get("exportId")
            if not export_id:
                raise NexthinkError(502, "EXPORT_NO_ID", "export response had no exportId")
            path = STATUS_PATH.format(export_id=quote(str(export_id), safe=""))
            deadline = time.monotonic() + self._export_timeout
            while True:
                status = (await self._request(http, "GET", self.settings.base_url + path)).json()
                state = status.get("status")
                if state == STATUS_DONE:
                    break
                if state == STATUS_FAILED:
                    raise NexthinkError(502, "EXPORT_FAILED", str(status.get("errorDescription") or "export failed"))
                if state not in STATUS_PENDING:
                    raise NexthinkError(502, "EXPORT_UNEXPECTED_STATUS", f"unexpected export status {state!r}")
                if time.monotonic() >= deadline:
                    raise NexthinkError(504, "EXPORT_TIMEOUT", f"export not finished after {self._export_timeout:.0f}s")
                await self._sleep(self._poll)
            url = status.get("resultsFileUrl") or ""
            if urlparse(url).scheme not in ("http", "https"):
                raise NexthinkError(502, "EXPORT_BAD_URL", "export finished without a usable resultsFileUrl")
            same_host = urlparse(url).netloc == urlparse(self.settings.base_url).netloc
            sends_bearer = RESULTS_URL_SENDS_BEARER and same_host
            body = (await self._request(http, "GET", url, bearer=sends_bearer)).json()
        rows = body.get(RESULTS_ROWS_KEY) or []
        return QueryResult(rows[:max_rows], len(rows) > max_rows, query, body.get("executionDateTime"))

    # ---- typed operations used by the tools ------------------------------------------------------------------------
    async def check(self) -> dict:
        res = await self._execute("devices\n| list uid\n| limit 1", 1)
        return {"queryOk": True, "executedAt": res.executed_at}

    async def run_query(self, nql: str, max_rows: int) -> QueryResult:
        if not nql or not nql.strip() or len(nql) > 5000:
            raise ValueError("query must be 1-5000 characters of NQL")
        return await self._execute(nql, max_rows)

    async def list_devices(
        self, *, name_contains: str | None, site: str | None, os_name: str | None, user: str | None, limit: int
    ) -> Page:
        where = _where(
            f"name contains {nql_str(name_contains)}" if name_contains else None,
            f"site.name contains {nql_str(site)}" if site else None,
            f"operating_system.name contains {nql_str(os_name)}" if os_name else None,
            f"last_logged_on_user.name contains {nql_str(user)}" if user else None,
        )
        res = await self._execute(f"devices\n{where}| list {', '.join(DEVICE_FIELDS)}\n| sort name asc", limit)
        return Page([_rename(r, DEVICE_FIELDS) for r in res.rows], res.truncated)

    async def get_device(self, device_id: str) -> dict | None:
        if not _SAFE_ID.match(device_id or ""):
            raise ValueError("device_id must be 1-64 characters from A-Z a-z 0-9 . _ : -")
        did = nql_str(device_id)
        device, apps, boots, crashes = await asyncio.gather(
            self._execute(f"devices\n| where uid == {did}\n| list {', '.join(DEVICE_FIELDS)}", 1),
            self._execute(f"installations\n| where device.uid == {did}\n| list {', '.join(INSTALL_FIELDS)}\n| sort application.name asc", 200),
            self._execute(
                f"device_performance.boots\n| during past 14d\n| where device.uid == {did}\n"
                "| list time, boot.duration_s, operating_system.version\n| sort time desc", 5),
            self._execute(
                f"execution.crashes\n| during past 14d\n| where device.uid == {did}\n"
                "| summarize crashes = count(), lastCrash = max(time) by application.name, application.version\n| sort crashes desc", 20),
        )
        if not device.rows:
            return None
        return {
            "device": _rename(device.rows[0], DEVICE_FIELDS),
            "applications": Page([_rename(r, INSTALL_FIELDS) for r in apps.rows], apps.truncated),
            "boots": [{"time": r["time"], "bootSeconds": r["boot.duration_s"], "osVersion": r["operating_system.version"]} for r in boots.rows],
            "crashes": Page(
                [{"application": r["application.name"], "appVersion": r["application.version"], "crashes": r["crashes"], "lastCrash": r["lastCrash"]} for r in crashes.rows],
                crashes.truncated,
            ),
        }

    async def list_applications(self, *, name_contains: str | None, limit: int) -> Page:
        where = _where(f"application.name contains {nql_str(name_contains)}" if name_contains else None)
        res = await self._execute(
            f"installations\n{where}| summarize devices = count() by application.name, application.publisher, application.version\n"
            "| sort devices desc", limit)
        items = [{"application": r["application.name"], "publisher": r["application.publisher"], "appVersion": r["application.version"], "devices": r["devices"]} for r in res.rows]
        return Page(items, res.truncated)

    async def app_crash_report(self, *, application: str, version: str | None, site: str | None, days: int, max_devices: int) -> dict:
        crash_filter = _where(
            f"application.name contains {nql_str(application)}",
            f"application.version == {nql_str(version)}" if version else None,
            f"site.name contains {nql_str(site)}" if site else None,
        )
        base = f"execution.crashes\n| during past {days}d\n{crash_filter}"
        agg = "| summarize crashes = count(), firstCrash = min(time), lastCrash = max(time) by "
        by_version, by_site, by_os, devices, installed = await asyncio.gather(
            self._execute(f"{base}{agg}application.name, application.version\n| sort crashes desc", 50),
            self._execute(f"{base}| summarize crashes = count() by site.uid, site.name\n| sort crashes desc", 50),
            self._execute(f"{base}| summarize crashes = count() by operating_system.version\n| sort crashes desc", 50),
            self._execute(
                f"{base}| summarize crashes = count(), lastCrash = max(time) by device.uid, device.name, user.uid, user.name, "
                "site.name, application.name, application.version\n| sort crashes desc", max_devices),
            self._execute(
                f"installations\n{_where(f'application.name contains {nql_str(application)}')}"
                "| summarize devices = count() by application.name, application.version\n| sort devices desc", 100),
        )
        return {
            "executedAt": by_version.executed_at,
            "byVersion": Page([
                {"application": r["application.name"], "appVersion": r["application.version"], "crashes": r["crashes"],
                 "firstCrash": r["firstCrash"], "lastCrash": r["lastCrash"]} for r in by_version.rows], by_version.truncated),
            "bySite": Page([{"siteId": r["site.uid"], "siteName": r["site.name"], "crashes": r["crashes"]} for r in by_site.rows], by_site.truncated),
            "byOsVersion": Page([{"osVersion": r["operating_system.version"], "crashes": r["crashes"]} for r in by_os.rows], by_os.truncated),
            "devices": Page([
                {"deviceId": r["device.uid"], "deviceName": r["device.name"], "userId": r["user.uid"], "userName": r["user.name"],
                 "siteName": r["site.name"], "application": r["application.name"], "appVersion": r["application.version"],
                 "crashes": r["crashes"], "lastCrash": r["lastCrash"]} for r in devices.rows], devices.truncated),
            "installed": [{"application": r["application.name"], "appVersion": r["application.version"], "devices": r["devices"]} for r in installed.rows],
        }

    async def experience_events(self, *, days: int, site: str | None, application: str | None, max_rows: int) -> dict:
        """Raw crash and boot events for server-side aggregation, fetched through the async export flow, newest first.
        The application filter only applies to crashes; boots are skipped when it is set."""
        site_cond = f"site.name contains {nql_str(site)}" if site else None
        app_cond = f"application.name contains {nql_str(application)}" if application else None
        crashes_q = (f"execution.crashes\n| during past {days}d\n{_where(site_cond, app_cond)}"
                     f"| list {', '.join(CRASH_FIELDS)}\n| sort time desc")
        boots_q = (f"device_performance.boots\n| during past {days}d\n{_where(site_cond)}"
                   f"| list {', '.join(BOOT_FIELDS)}\n| sort time desc")

        async def no_boots() -> QueryResult:
            return QueryResult([], False, "", None)

        crashes, boots = await asyncio.gather(
            self._export(crashes_q, max_rows), no_boots() if application else self._export(boots_q, max_rows))
        return {
            "executedAt": crashes.executed_at or boots.executed_at,
            "crashes": [_rename(r, CRASH_FIELDS) for r in crashes.rows],
            "boots": [_rename(r, BOOT_FIELDS) for r in boots.rows],
            "crashesTruncated": crashes.truncated,
            "bootsTruncated": boots.truncated,
            "bootsSkipped": bool(application),
        }

    async def device_counts_by_site(self, site: str | None) -> dict[str, int]:
        where = _where(f"site.name contains {nql_str(site)}" if site else None)
        res = await self._execute(f"devices\n{where}| summarize devices = count() by site.name\n| sort devices desc", 200)
        return {r["site.name"]: r["devices"] for r in res.rows}

    async def install_counts(self, application: str | None) -> dict[tuple[str, str], int]:
        where = _where(f"application.name contains {nql_str(application)}" if application else None)
        res = await self._execute(
            f"installations\n{where}| summarize devices = count() by application.name, application.version\n| sort devices desc", 500)
        return {(r["application.name"], r["application.version"]): r["devices"] for r in res.rows}

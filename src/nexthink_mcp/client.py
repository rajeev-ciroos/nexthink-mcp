"""The ONLY module that knows how Nexthink's API looks: URL paths, auth, request/response payloads, the saved queries
the tools depend on, NQL table and field names, row limits. Tools call the typed methods at the bottom and get
normalised dicts (deviceId, userId, ...), never Nexthink field names.

Nexthink's NQL API runs SAVED queries by ID ({"queryId": "#..."}); it does not accept NQL text. So the tools depend on
the small set of saved queries defined in SAVED_QUERIES below. An admin creates them once in Nexthink (scripts/
print_saved_queries.py prints them in a paste-ready form); filtering and aggregation then happen here, in Python.

Confirmed by the first real tenant (ExxonMobil): token endpoint and Basic auth, and that execute requires queryId.
Everything else marked ASSUMPTION is unverified against a real Nexthink instance and is listed in the README. The
simulator (nexthink-simulator) implements these same assumptions, so passing tests against it proves nothing about them.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .errors import NexthinkError

# ---- endpoints -------------------------------------------------------------------------------------------------------
TOKEN_PATH = "/api/v1/token"  # client-credentials; client id/secret sent as HTTP Basic, grant_type in a form body (works on ExxonMobil)
TOKEN_SCOPE: str | None = None  # ASSUMPTION: no scope parameter needed. Permissions are set on the API credential in Nexthink.
EXECUTE_PATH = "/api/v1/nql/execute"  # POST {"queryId": "#id", "parameters": {...}} -> {"rows","data":[{...}],"executionDateTime"}
EXPORT_PATH = "/api/v1/nql/export"  # ASSUMPTION: same body as execute -> {"exportId": "..."}
STATUS_PATH = "/api/v1/nql/status/{export_id}"  # ASSUMPTION: GET -> {"status": SUBMITTED|IN_PROGRESS|COMPLETED|ERROR, "resultsFileUrl", "errorDescription"}
STATUS_PENDING = ("SUBMITTED", "IN_PROGRESS")
STATUS_DONE, STATUS_FAILED = "COMPLETED", "ERROR"
# ASSUMPTION: resultsFileUrl is a pre-signed URL (often another host); the bearer token must NOT be sent to it.
RESULTS_URL_SENDS_BEARER = False
# ASSUMPTION: results are JSON shaped like the execute response ({"data": [...]}) OR CSV with one header row of the field
# names. Both are parsed; values from CSV stay strings except where a number is needed (see _num).
RESULTS_ROWS_KEY = "data"
PARAMETERS_KEY = "parameters"  # ASSUMPTION: saved-query parameters are sent as {"parameters": {"name": value}}

# ---- limits and timing -----------------------------------------------------------------------------------------------
EXECUTE_ROW_LIMIT = 1000  # ASSUMPTION: rows the synchronous execute call returns at most
EXPORT_ROW_LIMIT = 100_000  # ASSUMPTION: rows an export can return
SYNC_FETCH = EXECUTE_ROW_LIMIT - 1
FETCH_ALL = EXPORT_ROW_LIMIT - 1
EVENT_WINDOW_DAYS = 30  # the crashes and boots saved queries look back this far; tools never ask for more
POLL_INTERVAL_S = 1.0
EXPORT_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 30.0
MAX_RETRIES = 3
MAX_RETRY_SLEEP_S = 30.0
RETRY_STATUSES = {429, 502, 503, 504}

# ---- ASSUMPTIONS: NQL schema (table and field names the saved queries list, and the keys of the returned rows) -------
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


# ---- the saved queries -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SavedQuery:
    key: str  # what the code calls it; the env override is NEXTHINK_QUERY_ID_<KEY>
    default_id: str  # the ID an admin gives it in Nexthink unless they choose another and set the override
    mode: str  # "execute" (small results, synchronous) or "export" (large results, async)
    nql: str  # the definition to create in Nexthink
    purpose: str
    parameters: tuple[str, ...] = ()
    optional: bool = False  # only used when its ID is set explicitly (it needs a parameter)


def _cols(mapping: dict[str, str]) -> str:
    return ", ".join(mapping)


SAVED_QUERIES: dict[str, SavedQuery] = {q.key: q for q in [
    SavedQuery(
        "devices", "#ciroos_devices", "export",
        f"devices\n| list {_cols(DEVICE_FIELDS)}\n| sort name asc",
        "Inventory of every device: list_devices, get_device"),
    SavedQuery(
        "device_counts_by_site", "#ciroos_device_counts_by_site", "execute",
        "devices\n| summarize devices = count() by site.name\n| sort devices desc",
        "Devices per site: check_connection, summarize_experience (crash rates per 100 devices)"),
    SavedQuery(
        "app_installs", "#ciroos_app_installs", "export",
        "installations\n| summarize devices = count() by application.name, application.publisher, application.version\n"
        "| sort devices desc",
        "Devices per application version: list_applications, get_app_crashes, summarize_experience"),
    SavedQuery(
        "crashes", "#ciroos_crashes", "export",
        f"execution.crashes\n| during past {EVENT_WINDOW_DAYS}d\n| list {_cols(CRASH_FIELDS)}\n| sort time desc",
        f"Application crash events of the last {EVENT_WINDOW_DAYS} days: get_app_crashes, get_device, summarize_experience"),
    SavedQuery(
        "boots", "#ciroos_boots", "export",
        f"device_performance.boots\n| during past {EVENT_WINDOW_DAYS}d\n| list {_cols(BOOT_FIELDS)}\n| sort time desc",
        f"Boot events of the last {EVENT_WINDOW_DAYS} days: get_device, summarize_experience"),
    SavedQuery(
        "device_apps", "#ciroos_device_apps", "execute",
        f"installations\n| where device.uid == $device_id\n| list {_cols(INSTALL_FIELDS)}\n| sort application.name asc",
        "Applications installed on one device (get_device). Optional: needs a saved-query parameter, so it is off unless "
        "NEXTHINK_QUERY_ID_DEVICE_APPS is set",
        parameters=("device_id",), optional=True),
]}

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_QUERY_ID = re.compile(r"^#?[A-Za-z0-9._-]{1,100}$")
_PARAM_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _rename(row: dict, mapping: dict[str, str]) -> dict:
    return {out: row.get(src) for src, out in mapping.items()}


def _num(value: Any) -> Any:
    """CSV results arrive as strings; numbers are converted only where one is needed (counts, seconds)."""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return value


def _has(value: Any, needle: str | None) -> bool:
    """Case-insensitive substring match; an empty filter matches everything."""
    n = (needle or "").strip().lower()
    return not n or n in str(value or "").lower()


def _parse_time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _end(executed_at: str | None) -> datetime:
    return (_parse_time(executed_at) if executed_at else None) or datetime.now(timezone.utc)


def _recent(events: list[dict], end: datetime, days: int) -> list[dict]:
    cutoff = end - timedelta(days=days)
    return [e for e in events if (t := _parse_time(e["time"])) is None or t >= cutoff]


def _newest_first(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda e: str(e["time"] or ""), reverse=True)


def _group(events: list[dict], keys: tuple[str, ...], drop: tuple[str, ...] = ()) -> list[dict]:
    """Crash counts per distinct `keys`, biggest first, with first and last crash time unless named in `drop`."""
    acc: dict[tuple, dict] = {}
    for e in events:
        g = acc.setdefault(tuple(e[k] for k in keys), {"crashes": 0, "firstCrash": None, "lastCrash": None})
        g["crashes"] += 1
        t = e["time"]
        if t is not None:
            g["firstCrash"] = t if g["firstCrash"] is None or t < g["firstCrash"] else g["firstCrash"]
            g["lastCrash"] = t if g["lastCrash"] is None or t > g["lastCrash"] else g["lastCrash"]
    out = []
    for k, g in sorted(acc.items(), key=lambda kv: -kv[1]["crashes"]):
        item = dict(zip(keys, k))
        item.update({k: v for k, v in g.items() if k not in drop})
        out.append(item)
    return out


def _parse_rows(resp: httpx.Response) -> tuple[list[dict], str | None]:
    """Rows and execution time from a JSON or CSV body."""
    text = resp.text
    if "json" in resp.headers.get("content-type", "").lower() or text.lstrip().startswith(("{", "[")):
        body = resp.json()
        if isinstance(body, list):
            return body, None
        return list(body.get(RESULTS_ROWS_KEY) or []), body.get("executionDateTime")
    return [dict(r) for r in csv.DictReader(io.StringIO(text))], None


def _error_from_response(resp: httpx.Response, step: str) -> NexthinkError:
    """Keep what Nexthink said: code, message (or error list, or the raw text) and which call it was."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    code = message = None
    if isinstance(body, dict):
        code = body.get("code") or body.get("error") or body.get("title")
        message = body.get("message") or body.get("detail") or body.get("error_description")
        listed = body.get("errors") or body.get("violations")
        if not message and isinstance(listed, list):
            message = "; ".join(str(e.get("message") or e) if isinstance(e, dict) else str(e) for e in listed[:5])
    if not message:
        message = (resp.text or "").strip() or resp.reason_phrase or "request failed"
    return NexthinkError(resp.status_code, str(code or "unknown"), f"{step}: {str(message).splitlines()[0][:300]}")


@dataclass
class QueryResult:
    rows: list[dict]
    truncated: bool
    query_id: str
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

    # ---- saved-query IDs -------------------------------------------------------------------------------------------
    def query_id(self, key: str) -> str:
        return self.settings.query_ids.get(key) or SAVED_QUERIES[key].default_id

    def query_ids(self) -> dict[str, str]:
        return {k: self.query_id(k) for k, q in SAVED_QUERIES.items() if self.enabled(k)}

    def enabled(self, key: str) -> bool:
        return not SAVED_QUERIES[key].optional or key in self.settings.query_ids

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
                http, "POST", self.settings.base_url + TOKEN_PATH, step="token request", data=data,
                auth=(self.settings.client_id, self.settings.client_secret), bearer=False,
            )
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    async def _request(
        self, http: httpx.AsyncClient, method: str, url: str, *, step: str, json: Any = None, data: Any = None,
        auth: tuple[str, str] | None = None, bearer: bool = True,
    ) -> httpx.Response:
        attempt, refreshed = 0, False
        while True:
            headers = {"Accept": "application/json, text/csv"}
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
                raise _error_from_response(resp, step)
            return resp

    # ---- saved-query execution -------------------------------------------------------------------------------------
    @staticmethod
    def _payload(query_id: str, parameters: dict | None) -> dict:
        return {"queryId": query_id, **({PARAMETERS_KEY: parameters} if parameters else {})}

    async def _execute(self, query_id: str, parameters: dict | None, max_rows: int) -> QueryResult:
        if max_rows >= EXECUTE_ROW_LIMIT:
            raise ValueError(f"max_rows must be below {EXECUTE_ROW_LIMIT} for synchronous queries")
        async with self._http() as http:
            resp = await self._request(
                http, "POST", self.settings.base_url + EXECUTE_PATH, step=f"execute {query_id}",
                json=self._payload(query_id, parameters))
        rows, executed_at = _parse_rows(resp)
        return QueryResult(rows[:max_rows], len(rows) > max_rows or len(rows) >= EXECUTE_ROW_LIMIT, query_id, executed_at)

    async def _export(self, query_id: str, parameters: dict | None, max_rows: int) -> QueryResult:
        if max_rows >= EXPORT_ROW_LIMIT:
            raise ValueError(f"max_rows must be below {EXPORT_ROW_LIMIT} for exports")
        async with self._http() as http:
            exported = (await self._request(
                http, "POST", self.settings.base_url + EXPORT_PATH, step=f"export {query_id}",
                json=self._payload(query_id, parameters))).json()
            export_id = exported.get("exportId")
            if not export_id:
                raise NexthinkError(502, "EXPORT_NO_ID", f"export {query_id}: response had no exportId")
            path = STATUS_PATH.format(export_id=quote(str(export_id), safe=""))
            deadline = time.monotonic() + self._export_timeout
            while True:
                status = (await self._request(http, "GET", self.settings.base_url + path, step=f"export status {query_id}")).json()
                state = status.get("status")
                if state == STATUS_DONE:
                    break
                if state == STATUS_FAILED:
                    raise NexthinkError(502, "EXPORT_FAILED", f"export {query_id}: {status.get('errorDescription') or 'export failed'}")
                if state not in STATUS_PENDING:
                    raise NexthinkError(502, "EXPORT_UNEXPECTED_STATUS", f"export {query_id}: unexpected export status {state!r}")
                if time.monotonic() >= deadline:
                    raise NexthinkError(504, "EXPORT_TIMEOUT", f"export {query_id}: not finished after {self._export_timeout:.0f}s")
                await self._sleep(self._poll)
            url = status.get("resultsFileUrl") or ""
            if urlparse(url).scheme not in ("http", "https"):
                raise NexthinkError(502, "EXPORT_BAD_URL", f"export {query_id}: finished without a usable resultsFileUrl")
            same_host = urlparse(url).netloc == urlparse(self.settings.base_url).netloc
            sends_bearer = RESULTS_URL_SENDS_BEARER and same_host
            resp = await self._request(http, "GET", url, step=f"export download {query_id}", bearer=sends_bearer)
        rows, executed_at = _parse_rows(resp)
        return QueryResult(rows[:max_rows], len(rows) > max_rows or len(rows) >= EXPORT_ROW_LIMIT, query_id, executed_at)

    async def _saved(self, key: str, max_rows: int | None = None, parameters: dict | None = None) -> QueryResult:
        q = SAVED_QUERIES[key]
        if not self.enabled(key):
            raise ValueError(f"saved query {key!r} is not enabled: set NEXTHINK_QUERY_ID_{key.upper()}")
        if q.mode == "execute":
            return await self._execute(self.query_id(key), parameters, max_rows or SYNC_FETCH)
        return await self._export(self.query_id(key), parameters, max_rows or FETCH_ALL)

    # ---- typed operations used by the tools ------------------------------------------------------------------------
    async def check(self) -> dict:
        res = await self._saved("device_counts_by_site")
        return {"queryOk": True, "executedAt": res.executed_at, "sitesReturned": len(res.rows), "savedQueryIds": self.query_ids()}

    async def run_saved_query(self, query_id: str, parameters: dict | None, max_rows: int) -> QueryResult:
        if not _QUERY_ID.match(query_id or ""):
            raise ValueError("query_id must be 1-100 characters from A-Z a-z 0-9 . _ - with an optional leading #")
        parameters = parameters or {}
        if len(parameters) > 20 or not all(
            _PARAM_NAME.match(str(k)) and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200 for k, v in parameters.items()
        ):
            raise ValueError("parameters must be at most 20 name/value pairs; names A-Z a-z 0-9 . _ - and values text, number or boolean up to 200 characters")
        return await self._execute(query_id, parameters, max_rows)

    async def list_devices(
        self, *, name_contains: str | None, site: str | None, os_name: str | None, user: str | None, limit: int
    ) -> Page:
        res = await self._saved("devices")
        devices = [_rename(r, DEVICE_FIELDS) for r in res.rows]
        devices = [d for d in devices if _has(d["deviceName"], name_contains) and _has(d["siteName"], site)
                   and _has(d["os"], os_name) and _has(d["userName"], user)]
        devices.sort(key=lambda d: str(d["deviceName"] or "").lower())
        return Page(devices[:limit], len(devices) > limit or res.truncated)

    async def get_device(self, device_id: str) -> dict | None:
        if not _SAFE_ID.match(device_id or ""):
            raise ValueError("device_id must be 1-64 characters from A-Z a-z 0-9 . _ : -")
        found = next((r for r in (await self._saved("devices")).rows if str(r.get("uid")) == device_id), None)
        if found is None:
            return None
        jobs = [self._saved("crashes"), self._saved("boots")]
        if self.enabled("device_apps"):
            jobs.append(self._saved("device_apps", 200, {"device_id": device_id}))
        crashes, boots, *extra = await asyncio.gather(*jobs)
        apps = extra[0] if extra else None
        end = _end(crashes.executed_at or boots.executed_at)
        mine_crashes = _recent([e for e in (_rename(r, CRASH_FIELDS) for r in crashes.rows) if e["deviceId"] == device_id], end, 14)
        mine_boots = _newest_first(_recent([e for e in (_rename(r, BOOT_FIELDS) for r in boots.rows) if e["deviceId"] == device_id], end, 14))
        crash_rows = [
            {"application": g["application"], "appVersion": g["appVersion"], "crashes": g["crashes"], "lastCrash": g["lastCrash"]}
            for g in _group(mine_crashes, ("application", "appVersion"))
        ]
        return {
            "device": _rename(found, DEVICE_FIELDS),
            "applications": None if apps is None else Page([_rename(r, INSTALL_FIELDS) for r in apps.rows], apps.truncated),
            "boots": [{"time": e["time"], "bootSeconds": _num(e["bootSeconds"]), "osVersion": e["osVersion"]} for e in mine_boots[:5]],
            "crashes": Page(crash_rows[:20], len(crash_rows) > 20 or crashes.truncated),
        }

    async def _installs(self, application: str | None) -> tuple[list[dict], bool]:
        res = await self._saved("app_installs")
        rows = [
            {"application": r.get("application.name"), "publisher": r.get("application.publisher"),
             "appVersion": r.get("application.version"), "devices": _num(r.get("devices")) or 0}
            for r in res.rows
        ]
        return sorted((r for r in rows if _has(r["application"], application)), key=lambda r: -r["devices"]), res.truncated

    async def list_applications(self, *, name_contains: str | None, limit: int) -> Page:
        rows, truncated = await self._installs(name_contains)
        return Page(rows[:limit], len(rows) > limit or truncated)

    async def app_crash_report(self, *, application: str, version: str | None, site: str | None, days: int, max_devices: int) -> dict:
        res, (installs, _) = await asyncio.gather(self._saved("crashes"), self._installs(application))
        events = _recent([_rename(r, CRASH_FIELDS) for r in res.rows], _end(res.executed_at), days)
        events = [e for e in events if _has(e["application"], application)
                  and (not version or e["appVersion"] == version.strip()) and _has(e["siteName"], site)]

        def page(keys: tuple[str, ...], top: int, drop: tuple[str, ...] = ()) -> Page:
            groups = _group(events, keys, drop)
            return Page(groups[:top], len(groups) > top or res.truncated)

        both_times = ("firstCrash", "lastCrash")
        dev_keys = ("deviceId", "deviceName", "userId", "userName", "siteName", "application", "appVersion")
        installed: dict[tuple, int] = {}
        for r in installs:
            installed[(r["application"], r["appVersion"])] = installed.get((r["application"], r["appVersion"]), 0) + r["devices"]
        return {
            "executedAt": res.executed_at,
            "byVersion": page(("application", "appVersion"), 50),
            "bySite": page(("siteId", "siteName"), 50, both_times),
            "byOsVersion": page(("osVersion",), 50, both_times),
            "devices": page(dev_keys, max_devices, ("firstCrash",)),
            "installed": [{"application": a, "appVersion": v, "devices": n}
                          for (a, v), n in sorted(installed.items(), key=lambda kv: -kv[1])[:100]],
        }

    async def experience_events(self, *, days: int, site: str | None, application: str | None, max_rows: int) -> dict:
        """Crash and boot events for server-side aggregation, newest first, filtered here. The application filter only
        applies to crashes; boots are skipped when it is set."""
        async def no_boots() -> QueryResult:
            return QueryResult([], False, "", None)

        crashes_res, boots_res = await asyncio.gather(self._saved("crashes"), no_boots() if application else self._saved("boots"))
        end = _end(crashes_res.executed_at or boots_res.executed_at)
        crashes = _newest_first([
            e for e in _recent([_rename(r, CRASH_FIELDS) for r in crashes_res.rows], end, days)
            if _has(e["siteName"], site) and _has(e["application"], application)])
        boots = _newest_first([
            e for e in _recent([_rename(r, BOOT_FIELDS) for r in boots_res.rows], end, days) if _has(e["siteName"], site)])
        for e in boots:
            e["bootSeconds"] = _num(e["bootSeconds"])
        return {
            "executedAt": crashes_res.executed_at or boots_res.executed_at,
            "crashes": crashes[:max_rows],
            "boots": boots[:max_rows],
            "crashesTruncated": len(crashes) > max_rows or crashes_res.truncated,
            "bootsTruncated": len(boots) > max_rows or boots_res.truncated,
            "bootsSkipped": bool(application),
        }

    async def device_counts_by_site(self, site: str | None) -> dict[str, int]:
        res = await self._saved("device_counts_by_site")
        return {r["site.name"]: _num(r.get("devices")) or 0 for r in res.rows if _has(r.get("site.name"), site)}

    async def install_counts(self, application: str | None) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for r in (await self._installs(application))[0]:
            counts[(r["application"], r["appVersion"])] = counts.get((r["application"], r["appVersion"]), 0) + r["devices"]
        return counts

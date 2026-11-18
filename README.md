# nexthink-mcp

A **read-only** [MCP](https://modelcontextprotocol.io) server (official `mcp` SDK, FastMCP) that gives an AI agent such as
Ciroos access to **Nexthink** digital-employee-experience data: devices, installed applications, application crashes and
boot times, through the NQL API. Transports: stdio and Streamable HTTP with bearer-token auth.

There are **no write tools and no Remote Actions**. The server only ever sends NQL queries.

> **Status: built and tested against [`nexthink-simulator`](../nexthink-simulator), which implements the same
> assumptions. It has not been run against a real Nexthink instance.** The API details below were written from memory of
> Nexthink's public API shape, not verified. Read [Unverified assumptions](#unverified-assumptions) before pointing this at
> a real instance, and run `check_connection` first.

## Tools

| Tool | Use it for |
|---|---|
| `check_connection` | Verify config, token and a 1-row NQL query |
| `run_nql_query` | Escape hatch: any NQL query, up to 500 rows |
| `list_devices` | Find devices by name, site, OS or user; returns `deviceId` and `userId` |
| `get_device` | One device by `deviceId`: inventory, installed apps, recent boots, 14-day crash summary |
| `list_applications` | Installed apps with the versions in use and device counts |
| `get_app_crashes` | Crashes of one app by version, site and OS build, plus crashing devices and the install base |
| `summarize_experience` | Server-side aggregation of crashes and boot times by site, app, app version, OS version, network type or day |

All outputs carry stable IDs (`deviceId`, `userId`, `siteId`) so results from different tools can be joined. For
`run_nql_query` the IDs are there only if your query selects the `uid` fields.

### Data latency and limits

Every tool description says this too, so the agent sees it:

- **Latency:** Nexthink data is assumed to be near-real-time but some minutes behind; the newest ~15 minutes of events may
  be missing. Inventory fields (`lastSeen`, OS version) come from the last sync. All times are UTC.
- **Limits** (each result reports `truncated`; check it):
  - `run_nql_query`: `max_rows` default 100, max 500. A trailing `| limit N` above `max_rows` is lowered to it.
  - `list_devices`: default 50, max 200. `list_applications`: default 100 (app, version) pairs, max 500.
  - `get_device`: 200 installed apps, 5 boots, top 20 crash rows. `get_app_crashes`: top 50 per breakdown, `max_devices`
    default 25, max 200, `days` max 30.
  - `summarize_experience`: reads up to `max_rows` events per source (default 20,000, max 50,000) through the **export
    flow**, newest first. When more exist, `truncated` is true, `truncatedSources` says which, and the **oldest** events in the
    window are missing, so counts are lower bounds. At most 50 groups are returned.
- Boot metrics are not per application: with an `application` filter or `group_by` application they are skipped.
- Per-100-devices rates exist only where a denominator exists (site, application, application version).

## Setup

Python 3.10+. `mcp` is pinned below 2 because 2.x renamed `FastMCP`.

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

### Configuration (environment variables only)

| Variable | Required | Purpose |
|---|---|---|
| `NEXTHINK_BASE_URL` | yes | API base URL, e.g. `https://<instance>.api.<region>.nexthink.cloud` |
| `NEXTHINK_CLIENT_ID` | yes | API credential (client id) |
| `NEXTHINK_CLIENT_SECRET` | yes | API credential (secret). Sent only to `NEXTHINK_BASE_URL` as HTTP Basic, never logged or returned |
| `NEXTHINK_MCP_AUTH_TOKEN` | for HTTP | Bearer token clients must send. **HTTP refuses to start without it** |

`.env.example` lists them; `.env` is git-ignored. Nothing else is configurable by env: timeouts, retries and row limits are
constants at the top of `client.py`.

### Run

```bash
# stdio (Claude Desktop, MCP Inspector)
.venv/bin/nexthink-mcp

# Streamable HTTP at http://HOST:PORT/mcp  (health check: /healthz, no auth)
NEXTHINK_MCP_AUTH_TOKEN=... .venv/bin/nexthink-mcp --transport http --host 0.0.0.0 --port 8000

# talk to it like a real client
.venv/bin/python scripts/smoke_client.py http http://127.0.0.1:8000/mcp "$NEXTHINK_MCP_AUTH_TOKEN"
```

Put TLS in front (nginx, a load balancer) for anything beyond localhost: the bearer token travels in a header.

### OpenShift

The image runs under the default `restricted` SCC (random UID, read-only root filesystem, all capabilities dropped).
TLS ends at the OpenShift Route, so the pod needs no certificate. Manifests are in `openshift/`.

```bash
oc new-project nexthink-mcp
# build in-cluster from this repo (pushes to the internal registry as ImageStream nexthink-mcp)
oc new-build --name nexthink-mcp --strategy docker --binary=false \
  https://github.com/rajeev-ciroos/nexthink-mcp.git
# optional, if Docker Hub is blocked: --build-arg BASE_IMAGE=registry.access.redhat.com/ubi9/python-312
oc logs -f bc/nexthink-mcp

oc create secret generic nexthink-mcp \
  --from-literal=NEXTHINK_BASE_URL=https://<instance>.api.<region>.nexthink.cloud \
  --from-literal=NEXTHINK_CLIENT_ID=<client id> \
  --from-literal=NEXTHINK_CLIENT_SECRET=<client secret> \
  --from-literal=NEXTHINK_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"

oc apply -f openshift/
oc get route nexthink-mcp -o jsonpath='https://{.spec.host}/mcp{"\n"}'
```

Verified: the image builds on `python:3.13-slim` and on `ubi9/python-312`, and serves `/healthz` when run as UID
1000650000 with `--read-only --cap-drop ALL`. Not verified on a real OpenShift cluster (the Deployment, Route and
build commands were written to the OpenShift docs, not run). Egress through a corporate proxy: add `HTTPS_PROXY`
(and `NO_PROXY`) as extra keys in the Secret.

### With the simulator (docker compose)

Check out `nexthink-simulator` next to this repo, then:

```bash
cp .env.example .env     # set NEXTHINK_MCP_AUTH_TOKEN (openssl rand -hex 32); pick SCENARIO
docker compose up --build
```

The simulator listens on `127.0.0.1:8080` (ground truth for the scenario: `/_sim/state`), the MCP server on
`127.0.0.1:8000/mcp`. The compose file is the same in both repos. Not exercised here: the images were not built in the
environment this was written in, because no Docker daemon was running; the compose file was only validated with
`docker compose config`.

## Where the API assumptions live

`src/nexthink_mcp/client.py` is the only module that knows paths, payloads, auth, NQL table/field names and row limits.
It also owns every NQL query string; tools call typed methods and receive normalised dicts. If real Nexthink differs, fix
that file (and the simulator to match), not the tools.

## Unverified assumptions

None of these has been checked against a real Nexthink instance. The simulator mirrors all of them, so green tests prove
nothing about them. Each is a constant or a query in `client.py`.

**Authentication and scopes**
1. Token endpoint is `POST {base}/api/v1/token` with the client id/secret as HTTP Basic and a form body
   `grant_type=client_credentials`; the response has `access_token` and `expires_in`.
2. **No `scope` parameter** is sent (`TOKEN_SCOPE = None`). Which permission the API credential needs for NQL is unknown;
   a 403 probably means it is missing.
3. Token and NQL API share one base URL.

**NQL endpoints and payloads**
4. `POST {base}/api/v1/nql/execute` accepts **raw NQL text** as `{"query": "..."}`. Real Nexthink may only run saved queries
   by ID (`queryId`); if so every tool needs reworking.
5. The response is `{"data": [ {column: value} ], "executionDateTime": ...}` with keys named as written in the query.
6. Synchronous limit assumed **1,000 rows** (`EXECUTE_ROW_LIMIT`); queries always carry an explicit `limit`.
7. Errors are JSON `{"code","message"}` or `{"error","error_description"}`; 429/502/503/504 are retried honouring `Retry-After`.

**Async export**
8. `POST {base}/api/v1/nql/export` with `{"query": ...}` returns `{"exportId"}`;
   `GET {base}/api/v1/nql/status/{exportId}` returns `status` in `SUBMITTED | IN_PROGRESS | COMPLETED | ERROR` plus
   `resultsFileUrl` or `errorDescription`.
9. `resultsFileUrl` is a **pre-signed URL** (possibly another host): it is fetched **without** the bearer token
   (`RESULTS_URL_SENDS_BEARER = False`), and only `http(s)` URLs are followed.
10. The results file is **JSON** shaped like the execute response. A real export may be **CSV**, which would need a parser.
11. Export limit assumed 100,000 rows; polling every 1 s, timeout 120 s.

**NQL language and schema**
12. Syntax: `table | during past 7d | where f == "x" and g contains "y" | list a, b` and
    `... | summarize n = count(), m = max(time) by a, b | sort n desc | limit 50`; `==` is exact, `contains` is
    case-insensitive; string literals cannot contain quotes or backslashes (the client rejects such filter values).
13. Tables: `devices`, `installations` (one row per device and application), `execution.crashes`,
    `device_performance.boots`. Real Nexthink names and relations differ in places and event rows normally need joins.
14. Fields used (see `DEVICE_FIELDS`, `CRASH_FIELDS`, `BOOT_FIELDS`, `INSTALL_FIELDS`): `uid`, `name`,
    `last_logged_on_user.uid/.name`, `site.uid/.name`, `operating_system.name/.version`, `hardware.manufacturer/.model`,
    `network.type`, `network.ip_subnet`, `last_seen`, `application.name/.publisher/.version`, `installed_at`, `time`,
    `device.uid/.name`, `user.uid/.name`, `crash.type`, `boot.duration_s`. Event rows are assumed to carry the device's
    user, site, network type and OS version **as of the event**; real data may only offer current values.
15. A device's "user" is its last logged-on user.

**Data semantics**
16. Latency is minutes, and events are retained for at least the 30 days the tools allow.

## Not covered

Remote Actions, campaigns, alerts, web/connection/session events, and any write. Adding a table means adding fields to
`client.py` and a method for the tool.

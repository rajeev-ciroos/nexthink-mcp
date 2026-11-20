import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from nexthink_mcp.config import Settings
from nexthink_mcp.server import main
from tests.fakes import FakeNexthink, make


def test_http_requires_bearer_token_but_healthz_is_open():
    mcp, _ = make(FakeNexthink())
    from nexthink_mcp.server import BearerAuth

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth, token="tok")
    with TestClient(app) as c:
        assert c.get("/healthz").text == "ok"
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = c.post("/mcp", json={}, headers={"Authorization": "Bearer tok", "Accept": "application/json, text/event-stream"})
        assert r.status_code != 401


def test_settings_from_env_only(monkeypatch):
    for k in ("NEXTHINK_BASE_URL", "NEXTHINK_CLIENT_ID", "NEXTHINK_CLIENT_SECRET", "NEXTHINK_MCP_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert len(Settings.from_env().missing) == 3
    monkeypatch.setenv("NEXTHINK_BASE_URL", "https://nx.test/")
    monkeypatch.setenv("NEXTHINK_CLIENT_ID", "a")
    monkeypatch.setenv("NEXTHINK_CLIENT_SECRET", "b")
    s = Settings.from_env()
    assert s.base_url == "https://nx.test" and not s.missing and "hunter2" not in repr(s)


def test_query_id_overrides_come_from_env_only(monkeypatch):
    monkeypatch.setenv("NEXTHINK_QUERY_ID_DEVICES", " #acme_devices ")
    monkeypatch.setenv("NEXTHINK_QUERY_ID_DEVICE_APPS", "#acme_apps")
    monkeypatch.setenv("NEXTHINK_QUERY_ID_BOOTS", "")
    assert Settings.from_env().query_ids == {"devices": "#acme_devices", "device_apps": "#acme_apps"}


def test_http_transport_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("NEXTHINK_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["nexthink-mcp", "--transport", "http"])
    with pytest.raises(SystemExit, match="NEXTHINK_MCP_AUTH_TOKEN"):
        main()

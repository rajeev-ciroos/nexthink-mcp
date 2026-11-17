import json

import httpx
import pytest

from nexthink_mcp import client as C
from nexthink_mcp.client import NexthinkClient, nql_str
from nexthink_mcp.config import Settings
from nexthink_mcp.errors import ConfigError, NexthinkError
from tests.fakes import SETTINGS, FakeNexthink, boot_event, make, run


def client_for(fake, **kw):
    return make(fake, **kw)[1]


def test_token_uses_basic_auth_and_is_cached():
    fake = FakeNexthink().on("devices", [{"uid": "d"}])
    c = client_for(fake)
    run(c.run_query("devices | list uid", 10))
    run(c.run_query("devices | list uid", 10))
    assert fake.token_calls == 1
    tok = fake.requests[0]
    assert tok.url.path == "/api/v1/token" and tok.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in tok.content
    assert b"s3cr3t-value" not in tok.content  # the secret travels only in the Basic header
    assert fake.requests[1].headers["authorization"] == "Bearer tok-1"


def test_401_refreshes_token_once():
    fake = FakeNexthink().on("devices", [{"uid": "d"}])
    fake.revoke_token_once = True
    res = run(client_for(fake).run_query("devices | list uid", 10))
    assert res.rows == [{"uid": "d"}] and fake.token_calls == 2


def test_retries_on_429_then_succeeds_and_gives_up_after_max():
    fake = FakeNexthink().on("devices", [{"uid": "d"}])
    fake.fail_next = [429, 503]
    assert run(client_for(fake).run_query("devices | list uid", 10)).rows
    fake = FakeNexthink()
    fake.fail_next = [429] * 10
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake).run_query("devices | list uid", 10))
    assert e.value.status == 429 and "Throttled" in str(e.value)


def test_error_body_is_surfaced():
    fake = FakeNexthink()
    fake.fail_next = [400]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake).run_query("devices | list uid", 10))
    assert e.value.code == "BUSY" and "busy" in str(e.value)


def test_not_configured_and_bad_url():
    with pytest.raises(ConfigError, match="NEXTHINK_CLIENT_SECRET"):
        run(NexthinkClient(Settings(base_url="https://x", client_id="a")).run_query("devices | list uid", 1))
    with pytest.raises(ConfigError, match="http"):
        run(NexthinkClient(Settings(base_url="ftp://x", client_id="a", client_secret="b")).run_query("devices | list uid", 1))


@pytest.mark.parametrize("given,sent", [
    ("devices | list uid", "devices | list uid\n| limit 11"),
    ("devices | list uid | limit 5", "devices | list uid | limit 5"),
    ("devices | list uid | limit 10", "devices | list uid | limit 10"),
    ("devices | list uid | limit 500", "devices | list uid\n| limit 11"),
    ("devices | list uid | LIMIT 11", "devices | list uid\n| limit 11"),
])
def test_limit_handling(given, sent):
    fake = FakeNexthink()
    run(client_for(fake).run_query(given, 10))
    assert fake.queries() == [sent]


def test_truncation_is_detected_with_the_extra_row():
    fake = FakeNexthink().on("devices", [{"uid": str(i)} for i in range(11)])
    res = run(client_for(fake).run_query("devices | list uid", 10))
    assert res.truncated and len(res.rows) == 10
    fake = FakeNexthink().on("devices", [{"uid": str(i)} for i in range(10)])
    assert not run(client_for(fake).run_query("devices | list uid", 10)).truncated


def test_row_limit_guard_for_sync_calls():
    with pytest.raises(ValueError):
        run(client_for(FakeNexthink()).run_query("devices | list uid", C.EXECUTE_ROW_LIMIT))


@pytest.mark.parametrize("bad", ['x"', "a\\b", "line\nbreak", "", "  ", "x" * 201])
def test_nql_literals_cannot_break_out(bad):
    with pytest.raises(ValueError):
        nql_str(bad)
    if not bad:
        return  # an empty filter is "no filter" at the tool level
    with pytest.raises(ValueError):
        run(client_for(FakeNexthink()).list_devices(name_contains=bad, site=None, os_name=None, user=None, limit=5))


def test_filter_values_are_quoted_into_the_query():
    fake = FakeNexthink()
    run(client_for(fake).list_devices(name_contains=" CHI ", site="Chicago Office", os_name=None, user=None, limit=5))
    assert '| where name contains "CHI" and site.name contains "Chicago Office"\n' in fake.queries()[0]


def test_export_flow_polls_then_downloads_without_bearer():
    fake = FakeNexthink().on("device_performance.boots", [boot_event(), boot_event(device="dev-2")])
    res = run(client_for(fake)._export("device_performance.boots | list time", 100))
    assert len(res.rows) == 2 and not res.truncated
    paths = [r.url.path for r in fake.requests if r.url.host == "nx.test"]
    assert paths.count("/api/v1/nql/status/abc") == 3
    download = fake.requests[-1]
    assert download.url.host == "files.test" and "authorization" not in download.headers  # no token to the pre-signed host


def test_export_error_status_and_timeout_and_bad_url():
    fake = FakeNexthink()
    fake.status_script = [{"status": "ERROR", "errorDescription": "too big"}]
    with pytest.raises(NexthinkError, match="too big"):
        run(client_for(fake)._export("x | list a", 10))
    fake = FakeNexthink()
    fake.status_script = [{"status": "IN_PROGRESS"}]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake, export_timeout_s=0)._export("x | list a", 10))
    assert e.value.code == "EXPORT_TIMEOUT"
    fake = FakeNexthink()
    fake.status_script = [{"status": "COMPLETED", "resultsFileUrl": "file:///etc/passwd"}]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake)._export("x | list a", 10))
    assert e.value.code == "EXPORT_BAD_URL"
    fake = FakeNexthink()
    fake.status_script = [{"status": "WEIRD"}]
    with pytest.raises(NexthinkError, match="WEIRD"):
        run(client_for(fake)._export("x | list a", 10))


def test_export_truncation():
    fake = FakeNexthink().on("device_performance.boots", [boot_event() for _ in range(6)])
    res = run(client_for(fake)._export("device_performance.boots | list time", 5))
    assert res.truncated and len(res.rows) == 5

import httpx
import pytest

from nexthink_mcp import client as C
from nexthink_mcp.client import SAVED_QUERIES, NexthinkClient, _error_from_response
from nexthink_mcp.config import Settings
from nexthink_mcp.errors import ConfigError, NexthinkError
from tests.fakes import SETTINGS, FakeNexthink, boot_event, crash_event, device_row, make, run

DEVICES, COUNTS, INSTALLS = "#ciroos_devices", "#ciroos_device_counts_by_site", "#ciroos_app_installs"
CRASHES, BOOTS, APPS = "#ciroos_crashes", "#ciroos_boots", "#ciroos_device_apps"


def client_for(fake, **kw):
    return make(fake, **kw)[1]


def test_token_uses_basic_auth_and_is_cached():
    fake = FakeNexthink().on("#q", [{"uid": "d"}])
    c = client_for(fake)
    run(c.run_saved_query("#q", None, 10))
    run(c.run_saved_query("#q", None, 10))
    assert fake.token_calls == 1
    tok = fake.requests[0]
    assert tok.url.path == "/api/v1/token" and tok.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in tok.content
    assert b"s3cr3t-value" not in tok.content  # the secret travels only in the Basic header
    assert fake.requests[1].headers["authorization"] == "Bearer tok-1"


def test_execute_sends_a_query_id_and_parameters_never_nql_text():
    fake = FakeNexthink().on("#q", [{"uid": "d"}])
    run(client_for(fake).run_saved_query("#q", {"device_id": "dev-1", "n": 3}, 10))
    assert fake.bodies() == [{"queryId": "#q", "parameters": {"device_id": "dev-1", "n": 3}}]
    run(client_for(fake).run_saved_query("#q", None, 10))
    assert fake.bodies()[-1] == {"queryId": "#q"}  # no empty parameters object
    for b in fake.bodies():
        assert "query" not in b


def test_401_refreshes_token_once():
    fake = FakeNexthink().on("#q", [{"uid": "d"}])
    fake.revoke_token_once = True
    res = run(client_for(fake).run_saved_query("#q", None, 10))
    assert res.rows == [{"uid": "d"}] and fake.token_calls == 2


def test_retries_on_429_then_succeeds_and_gives_up_after_max():
    fake = FakeNexthink().on("#q", [{"uid": "d"}])
    fake.fail_next = [429, 503]
    assert run(client_for(fake).run_saved_query("#q", None, 10)).rows
    fake = FakeNexthink()
    fake.fail_next = [429] * 10
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake).run_saved_query("#q", None, 10))
    assert e.value.status == 429 and "Throttled" in str(e.value)


def test_error_names_the_step_and_keeps_the_body():
    fake = FakeNexthink()
    fake.fail_next = [400]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake).run_saved_query("#q", None, 10))
    assert e.value.code == "BUSY" and "busy" in str(e.value) and "execute #q" in str(e.value)


@pytest.mark.parametrize("status,body,code,text", [
    (400, {"code": "VALIDATION", "message": "request.queryId: must not be blank"}, "VALIDATION", "must not be blank"),
    (400, {"title": "Bad Request", "detail": "queryId is required"}, "Bad Request", "queryId is required"),
    (400, {"error": "invalid", "errors": [{"message": "a is bad"}, {"message": "b is bad"}]}, "invalid", "a is bad; b is bad"),
    (400, None, "unknown", "plain text failure"),
])
def test_error_body_shapes(status, body, code, text):
    resp = httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="plain text failure")
    err = _error_from_response(resp, "execute #q")
    assert err.code == code and text in err.message and err.message.startswith("execute #q: ")


def test_token_failure_is_labelled_as_the_token_step():
    fake = FakeNexthink()
    fake.token_status = 400
    with pytest.raises(NexthinkError, match="token request: token endpoint said no") as e:
        run(client_for(fake).run_saved_query("#q", None, 10))
    assert e.value.code == "invalid_request"


def test_the_fake_tenant_refuses_a_missing_query_id_like_the_real_one():
    fake = FakeNexthink()
    resp = httpx.Client(transport=httpx.MockTransport(fake)).post("https://nx.test/api/v1/nql/execute", json={"query": "devices | list uid"})
    assert resp.status_code == 400 and "queryId: must not be blank" in resp.text


def test_not_configured_and_bad_url():
    with pytest.raises(ConfigError, match="NEXTHINK_CLIENT_SECRET"):
        run(NexthinkClient(Settings(base_url="https://x", client_id="a")).run_saved_query("#q", None, 1))
    with pytest.raises(ConfigError, match="http"):
        run(NexthinkClient(Settings(base_url="ftp://x", client_id="a", client_secret="b")).run_saved_query("#q", None, 1))


def test_truncation_is_detected_with_the_extra_row():
    fake = FakeNexthink().on("#q", [{"uid": str(i)} for i in range(11)])
    res = run(client_for(fake).run_saved_query("#q", None, 10))
    assert res.truncated and len(res.rows) == 10
    fake = FakeNexthink().on("#q", [{"uid": str(i)} for i in range(10)])
    assert not run(client_for(fake).run_saved_query("#q", None, 10)).truncated
    fake = FakeNexthink().on("#q", [{"uid": str(i)} for i in range(C.EXECUTE_ROW_LIMIT)])  # a result at the API cap may be cut off
    assert run(client_for(fake).run_saved_query("#q", None, 500)).truncated


def test_row_limit_guard_for_sync_calls():
    with pytest.raises(ValueError):
        run(client_for(FakeNexthink())._execute("#q", None, C.EXECUTE_ROW_LIMIT))


@pytest.mark.parametrize("query_id", ["", "  ", "#a b", "#x'; drop", "#" + "a" * 101, "devices | list uid"])
def test_bad_query_ids_are_rejected_before_any_request(query_id):
    fake = FakeNexthink()
    with pytest.raises(ValueError, match="query_id"):
        run(client_for(fake).run_saved_query(query_id, None, 5))
    assert not fake.requests


@pytest.mark.parametrize("params", [{"a b": 1}, {"a": None}, {"a": [1]}, {"a": "x" * 201}, {str(i): 1 for i in range(21)}])
def test_bad_parameters_are_rejected_before_any_request(params):
    fake = FakeNexthink()
    with pytest.raises(ValueError, match="parameters"):
        run(client_for(fake).run_saved_query("#q", params, 5))
    assert not fake.requests


def test_filters_run_here_not_in_nexthink_and_use_the_right_saved_query():
    fake = FakeNexthink().on(DEVICES, [
        device_row("d1", "CHI-LT-0001"), device_row("d2", "CHI-LT-0002", **{"operating_system.name": "macOS"}),
        device_row("d3", "AUS-LT-0003", **{"site.name": "Austin Office"})])
    page = run(client_for(fake).list_devices(name_contains=" chi ", site="chicago", os_name="WINDOWS", user=None, limit=5))
    assert [d["deviceId"] for d in page.items] == ["d1"] and not page.truncated
    assert fake.bodies() == [{"queryId": DEVICES}]  # one request, no filter sent
    assert any(r.url.path == "/api/v1/nql/export" for r in fake.requests)  # devices come through the export flow


def test_list_devices_truncates_at_limit_and_sorts_by_name():
    fake = FakeNexthink().on(DEVICES, [device_row(f"d{i}", f"N-{9 - i}") for i in range(4)])
    page = run(client_for(fake).list_devices(name_contains=None, site=None, os_name=None, user=None, limit=2))
    assert [d["deviceName"] for d in page.items] == ["N-6", "N-7"] and page.truncated


def test_query_id_overrides_from_settings_and_optional_queries_are_off_by_default():
    fake = FakeNexthink().on("#acme_devices", [device_row()])
    settings = Settings(base_url="https://nx.test", client_id="a", client_secret="b", query_ids={"devices": "#acme_devices"})
    c = client_for(fake, settings=settings)
    assert run(c.list_devices(name_contains=None, site=None, os_name=None, user=None, limit=5)).items
    assert fake.queries() == ["#acme_devices"]
    assert "device_apps" not in c.query_ids() and c.query_ids()["devices"] == "#acme_devices"
    with pytest.raises(ValueError, match="NEXTHINK_QUERY_ID_DEVICE_APPS"):
        run(c._saved("device_apps", 10, {"device_id": "x"}))
    enabled = client_for(FakeNexthink(), settings=Settings(base_url="https://nx.test", client_id="a", client_secret="b",
                                                           query_ids={"device_apps": "#acme_apps"}))
    assert enabled.query_ids()["device_apps"] == "#acme_apps"


def test_export_flow_polls_then_downloads_without_bearer():
    fake = FakeNexthink().on(BOOTS, [boot_event(), boot_event(device="dev-2")])
    res = run(client_for(fake)._export(BOOTS, None, 100))
    assert len(res.rows) == 2 and not res.truncated
    paths = [r.url.path for r in fake.requests if r.url.host == "nx.test"]
    assert paths.count("/api/v1/nql/status/abc") == 3
    download = fake.requests[-1]
    assert download.url.host == "files.test" and "authorization" not in download.headers  # no token to the pre-signed host
    assert fake.bodies() == [{"queryId": BOOTS}]


def test_export_results_may_be_csv():
    fake = FakeNexthink().on(BOOTS, [boot_event(secs=41.5), boot_event(device="dev-2", secs=90.0)])
    fake.csv_results = True
    res = run(client_for(fake)._export(BOOTS, None, 100))
    assert [r["device.uid"] for r in res.rows] == ["dev-1", "dev-2"]
    assert res.rows[0]["boot.duration_s"] == "41.5"  # CSV values stay text...
    assert C._num(res.rows[0]["boot.duration_s"]) == 41.5 and C._num("12") == 12 and C._num("n/a") is None  # ...numbers are converted where needed


def test_csv_results_flow_through_to_the_typed_methods():
    fake = FakeNexthink().on(DEVICES, [device_row("d1", "CHI-LT-0001")]).on(INSTALLS, [
        {"application.name": "Zoom", "application.publisher": "Z", "application.version": "6.2.0", "devices": "40"},
        {"application.name": "Zoom", "application.publisher": "Z", "application.version": "6.1.5", "devices": "60"}])
    fake.csv_results = True
    c = client_for(fake)
    assert run(c.list_devices(name_contains=None, site=None, os_name=None, user=None, limit=5)).items[0]["deviceId"] == "d1"
    apps = run(c.list_applications(name_contains=None, limit=5))
    assert [(a["appVersion"], a["devices"]) for a in apps.items] == [("6.1.5", 60), ("6.2.0", 40)]


def test_export_error_status_and_timeout_and_bad_url():
    fake = FakeNexthink()
    fake.status_script = [{"status": "ERROR", "errorDescription": "too big"}]
    with pytest.raises(NexthinkError, match="too big"):
        run(client_for(fake)._export("#q", None, 10))
    fake = FakeNexthink()
    fake.status_script = [{"status": "IN_PROGRESS"}]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake, export_timeout_s=0)._export("#q", None, 10))
    assert e.value.code == "EXPORT_TIMEOUT"
    fake = FakeNexthink()
    fake.status_script = [{"status": "COMPLETED", "resultsFileUrl": "file:///etc/passwd"}]
    with pytest.raises(NexthinkError) as e:
        run(client_for(fake)._export("#q", None, 10))
    assert e.value.code == "EXPORT_BAD_URL"
    fake = FakeNexthink()
    fake.status_script = [{"status": "WEIRD"}]
    with pytest.raises(NexthinkError, match="WEIRD"):
        run(client_for(fake)._export("#q", None, 10))


def test_export_truncation():
    fake = FakeNexthink().on(BOOTS, [boot_event() for _ in range(6)])
    res = run(client_for(fake)._export(BOOTS, None, 5))
    assert res.truncated and len(res.rows) == 5


def test_every_saved_query_has_a_unique_hash_prefixed_id_and_only_reads():
    ids = [q.default_id for q in SAVED_QUERIES.values()]
    assert len(set(ids)) == len(ids) and all(i.startswith("#ciroos_") for i in ids)
    for q in SAVED_QUERIES.values():
        assert q.mode in ("execute", "export") and q.nql.split("\n")[0].split(" ")[0]
        assert all(f"${p}" in q.nql for p in q.parameters)

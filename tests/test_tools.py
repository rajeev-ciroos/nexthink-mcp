import pytest
from mcp.server.fastmcp.exceptions import ToolError

from nexthink_mcp.client import BOOT_FIELDS, _rename
from nexthink_mcp.config import Settings
from nexthink_mcp.tools.experience import aggregate
from tests.fakes import FakeNexthink, boot_event, call, crash_event, device_row, make, run

DEVICES, COUNTS, INSTALLS = "#ciroos_devices", "#ciroos_device_counts_by_site", "#ciroos_app_installs"
CRASHES, BOOTS, APPS = "#ciroos_crashes", "#ciroos_boots", "#ciroos_device_apps"


def install_row(app, ver, n, publisher="P"):
    return {"application.name": app, "application.publisher": publisher, "application.version": ver, "devices": n}


def test_tool_list_is_read_only_and_documents_latency_and_limits():
    mcp, _ = make(FakeNexthink())
    tools = {t.name: t for t in run(mcp.list_tools())}
    assert set(tools) == {"check_connection", "run_saved_query", "list_devices", "get_device", "list_applications", "get_app_crashes", "summarize_experience"}
    for name, t in tools.items():
        d = t.description.lower()
        assert "latency" in d, name
        assert "limit" in d or "max" in d, name
        assert not any(w in name for w in ("create", "update", "delete", "remote", "set_", "run_action")), name
    assert "truncated" in tools["summarize_experience"].description
    assert "free-form nql" in tools["run_saved_query"].description.lower()


def test_check_connection_ok_lists_query_ids_and_never_leaks_secret():
    fake = FakeNexthink().on(COUNTS, [{"site.name": "Chicago Office", "devices": 30}])
    mcp, _ = make(fake)
    out = run(call(mcp, "check_connection"))
    assert out["status"] == "ok" and out["sitesReturned"] == 1 and "s3cr3t" not in str(out)
    assert out["savedQueryIds"]["devices"] == DEVICES and "device_apps" not in out["savedQueryIds"]
    assert fake.queries() == [COUNTS]


def test_check_connection_reports_not_configured_and_auth_errors():
    mcp, _ = make(FakeNexthink(), settings=Settings(base_url="https://nx.test"))
    out = run(call(mcp, "check_connection"))
    assert out["status"] == "not_configured" and out["missingConfiguration"] == ["NEXTHINK_CLIENT_ID", "NEXTHINK_CLIENT_SECRET"]
    fake = FakeNexthink()
    fake.fail_next = [403]
    out = run(call(make(fake)[0], "check_connection"))
    assert out["status"] == "error" and "403" in out["error"] and f"execute {COUNTS}" in out["error"]


def test_check_connection_explains_a_saved_query_problem():
    fake = FakeNexthink()
    fake.fail_next = [400]
    out = run(call(make(fake)[0], "check_connection"))
    assert out["status"] == "error" and "execute #ciroos_device_counts_by_site" in out["error"]


def test_run_saved_query_truncation_parameters_and_validation():
    fake = FakeNexthink().on("#mine", [{"uid": str(i)} for i in range(4)])
    mcp, _ = make(fake)
    out = run(call(mcp, "run_saved_query", {"query_id": "#mine", "parameters": {"site": "Chicago"}, "max_rows": 3}))
    assert out["truncated"] and out["rowCount"] == 3 and out["maxRows"] == 3 and out["savedQueryId"] == "#mine"
    assert fake.bodies() == [{"queryId": "#mine", "parameters": {"site": "Chicago"}}]
    with pytest.raises(ToolError, match="max_rows"):
        run(call(mcp, "run_saved_query", {"query_id": "#mine", "max_rows": 501}))
    with pytest.raises(ToolError, match="query_id"):
        run(call(mcp, "run_saved_query", {"query_id": "devices | list uid"}))


def test_list_devices_has_stable_ids_and_truncated():
    fake = FakeNexthink().on(DEVICES, [device_row("dev-1"), device_row("dev-2", "CHI-LT-0002")])
    out = run(call(make(fake)[0], "list_devices", {"site": "Chicago", "limit": 1}))
    assert out["truncated"] and out["count"] == 1
    d = out["devices"][0]
    assert d["deviceId"] == "dev-1" and d["userId"] == "usr-1" and d["siteId"] == "site-1" and d["osVersion"] == "10.0.22631.4602"


def _device_fake():
    return (FakeNexthink().on(DEVICES, [device_row(), device_row("dev-2", "CHI-LT-0002")])
            .on(CRASHES, [crash_event(app="Slack", ver="4.40.0", t="2026-11-16T10:00:00Z"), crash_event(app="Slack", ver="4.40.0", t="2026-11-15T10:00:00Z"),
                          crash_event(app="Slack", ver="4.40.0", t="2026-10-01T10:00:00Z"), crash_event(device="dev-2", app="Zoom Workplace")])
            .on(BOOTS, [boot_event(secs=41.0), boot_event(device="dev-2", secs=99.0)]))


def test_get_device_found_and_not_found():
    mcp, _ = make(_device_fake())
    out = run(call(mcp, "get_device", {"device_id": "dev-1"}))
    assert out["found"] and out["device"]["deviceId"] == "dev-1" and out["device"]["userId"] == "usr-1"
    assert out["recentBoots"] == [{"time": "2026-11-17T08:00:00Z", "bootSeconds": 41.0, "osVersion": "10.0.22631.4602"}]
    crash = out["crashesLast14Days"]["items"]
    assert crash == [{"application": "Slack", "appVersion": "4.40.0", "crashes": 2, "lastCrash": "2026-11-16T10:00:00Z"}]  # dev-2 and the 7-week-old crash are excluded
    assert out["installedApplications"]["available"] is False
    assert run(call(make(FakeNexthink())[0], "get_device", {"device_id": "nope"})) == {"found": False, "deviceId": "nope"}
    with pytest.raises(ToolError):
        run(call(mcp, "get_device", {"device_id": 'x" | list uid'}))


def test_get_device_installed_applications_when_the_parameterised_query_is_enabled():
    fake = _device_fake().on(APPS, lambda body: [{"application.name": "Slack", "application.publisher": "S", "application.version": "4.40.0",
                                                  "installed_at": "2026-01-01T00:00:00Z"}] if body["parameters"] == {"device_id": "dev-1"} else [])
    settings = Settings(base_url="https://nx.test", client_id="client-1", client_secret="s3cr3t-value", query_ids={"device_apps": APPS})
    out = run(call(make(fake, settings=settings)[0], "get_device", {"device_id": "dev-1"}))
    apps = out["installedApplications"]
    assert apps["available"] and apps["items"][0]["application"] == "Slack"
    assert {"queryId": APPS, "parameters": {"device_id": "dev-1"}} in fake.bodies()


def test_list_applications_groups_versions():
    rows = [install_row("Zoom Workplace", "6.1.5", 60, "Zoom"), install_row("Zoom Workplace", "6.2.0", 40, "Zoom"), install_row("Slack", "4.40.0", 30, "S")]
    out = run(call(make(FakeNexthink().on(INSTALLS, rows))[0], "list_applications", {"name_contains": "o"}))
    assert [a["application"] for a in out["applications"]] == ["Zoom Workplace"]  # "Slack" has no "o"
    zoom = out["applications"][0]
    assert zoom["devices"] == 100 and len(zoom["versions"]) == 2 and not out["truncated"]
    out = run(call(make(FakeNexthink().on(INSTALLS, rows))[0], "list_applications", {"limit": 2}))
    assert out["truncated"] and out["versionRows"] == 2


def test_get_app_crashes_combines_breakdowns_and_install_base():
    events = ([crash_event(device=f"dev-{i % 3}", t="2026-11-16T10:00:00Z", ver="6.2.0") for i in range(30)]
              + [crash_event(device="dev-9", ver="6.1.5", site="Austin Office", os="10.0.19045.1"), crash_event(t="2026-11-01T10:00:00Z", ver="6.2.0")])
    fake = FakeNexthink().on(CRASHES, events).on(INSTALLS, [install_row("Zoom Workplace", "6.2.0", 10), install_row("Zoom Workplace", "6.1.5", 90), install_row("Slack", "4", 5)])
    mcp, _ = make(fake)
    out = run(call(mcp, "get_app_crashes", {"application": "zoom", "days": 3}))
    by_version = {r["appVersion"]: r for r in out["byVersion"]}
    assert by_version["6.2.0"]["crashes"] == 30 and by_version["6.2.0"]["crashesPerInstalledDevice"] == 3.0  # the 16-day-old event is outside `days`
    assert by_version["6.2.0"]["firstCrash"] == by_version["6.2.0"]["lastCrash"] == "2026-11-16T10:00:00Z"
    assert out["totalCrashes"] == 31 and [r["crashes"] for r in out["byVersion"]] == [30, 1]
    assert {s["siteName"] for s in out["bySite"]} == {"Chicago Office", "Austin Office"} and "firstCrash" not in out["bySite"][0]
    assert out["devices"][0]["crashes"] == 10 and out["devices"][0]["userId"] == "usr-1" and "firstCrash" not in out["devices"][0]
    assert not any(out["truncated"].values())
    only = run(call(make(fake)[0], "get_app_crashes", {"application": "zoom", "version": "6.1.5", "site": "austin", "days": 3, "max_devices": 1}))
    assert only["totalCrashes"] == 1 and [d["deviceId"] for d in only["devices"]] == ["dev-9"]
    with pytest.raises(ToolError, match="days"):
        run(call(make(FakeNexthink())[0], "get_app_crashes", {"application": "x", "days": 31}))


def test_summarize_experience_aggregates_on_the_server_and_reports_truncated():
    crashes = [crash_event(device=f"d{i % 3}", site="Chicago Office") for i in range(9)] + [crash_event(site="Austin Office", device="d9")]
    boots = [boot_event(device=f"d{i}", site="Chicago Office", secs=90.0) for i in range(12)] + [boot_event(site="Austin Office", secs=40.0)]
    fake = (FakeNexthink().on(CRASHES, crashes).on(BOOTS, boots)
            .on(COUNTS, [{"site.name": "Chicago Office", "devices": 30}, {"site.name": "Austin Office", "devices": 40}]))
    mcp, _ = make(fake)
    out = run(call(mcp, "summarize_experience", {"days": 7, "group_by": "site"}))
    assert out["truncated"] is False and out["window"]["end"] == "2026-11-17T12:00:00Z" and out["window"]["start"] == "2026-11-10T12:00:00Z"
    chi = out["groups"][0]
    assert chi["group"] == "Chicago Office" and chi["crashes"] == 9 and chi["affectedDevices"] == 3 and chi["devicePopulation"] == 30
    assert chi["crashesPer100Devices"] == 30.0 and chi["boots"]["avgSeconds"] == 90.0 and chi["boots"]["over60s"] == 12
    assert chi["sampleDeviceIds"] and chi["sampleUserIds"] == ["usr-1"]
    assert out["highlights"] == {"mostCrashes": "Chicago Office", "highestCrashRate": "Chicago Office", "slowestBoots": "Chicago Office"}
    assert out["totals"] == {"crashes": 10, "boots": 13, "devicesWithCrashes": 4}
    assert sorted(fake.queries()) == sorted([CRASHES, BOOTS, COUNTS])  # three saved queries, no NQL text

    site = run(call(make(fake)[0], "summarize_experience", {"site": "austin"}))  # filters apply here, to events and populations
    assert [g["group"] for g in site["groups"]] == ["Austin Office"] and site["totals"]["crashes"] == 1

    big = FakeNexthink().on(CRASHES, [crash_event() for _ in range(150)]).on(BOOTS, [boot_event()]).on(COUNTS, [])
    out = run(call(make(big)[0], "summarize_experience", {"max_rows": 100}))  # tool minimum is 100 rows
    assert out["truncated"] is True and out["truncatedSources"] == ["crashes"] and out["totals"]["crashes"] == 100
    assert any("lower bounds" in n for n in out["notes"])


def test_summarize_experience_days_window_is_applied_to_the_30_day_saved_queries():
    old = crash_event(t="2026-11-01T10:00:00Z")
    fake = FakeNexthink().on(CRASHES, [crash_event(), old]).on(BOOTS, [boot_event(), boot_event(t="2026-10-25T08:00:00Z")]).on(COUNTS, [])
    out = run(call(make(fake)[0], "summarize_experience", {"days": 7}))
    assert out["totals"]["crashes"] == 1 and out["totals"]["boots"] == 1
    out = run(call(make(fake)[0], "summarize_experience", {"days": 30}))
    assert out["totals"]["crashes"] == 2 and out["totals"]["boots"] == 2


def test_summarize_experience_application_grouping_and_filter():
    fake = (FakeNexthink().on(CRASHES, [crash_event(ver="6.2.0")] * 4 + [crash_event(ver="6.1.5")])
            .on(INSTALLS, [install_row("Zoom Workplace", "6.2.0", 8), install_row("Zoom Workplace", "6.1.5", 50)]))
    out = run(call(make(fake)[0], "summarize_experience", {"group_by": "application_version", "application": "zoom"}))
    top = out["groups"][0]
    assert top["group"] == "Zoom Workplace 6.2.0" and top["crashesPer100Devices"] == 50.0 and top["boots"] is None
    assert out["highlights"]["highestCrashRate"] == "Zoom Workplace 6.2.0"
    assert BOOTS not in fake.queries()  # boots skipped with an app filter
    assert any("skipped" in n for n in out["notes"])


def test_aggregate_day_and_percentiles():
    boots = [boot_event(t=f"2026-11-1{d}T08:00:00Z", secs=float(s)) for d, s in [(5, 10), (5, 20), (6, 30), (6, 40), (6, 100)]]
    groups = aggregate([], [_rename(b, BOOT_FIELDS) for b in boots], "day", None)
    assert [g["group"] for g in groups] == ["2026-11-15", "2026-11-16"]
    assert groups[1]["boots"] == {"count": 3, "avgSeconds": 56.7, "p50Seconds": 40.0, "p95Seconds": 100.0, "over60s": 1}
    assert groups[1]["sampleDeviceIds"] == ["dev-1"]

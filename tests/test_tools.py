import pytest
from mcp.server.fastmcp.exceptions import ToolError

from nexthink_mcp.client import BOOT_FIELDS, _rename
from nexthink_mcp.tools.experience import aggregate
from tests.fakes import FakeNexthink, boot_event, call, crash_event, device_row, make, run


def test_tool_list_is_read_only_and_documents_latency_and_limits():
    mcp, _ = make(FakeNexthink())
    tools = {t.name: t for t in run(mcp.list_tools())}
    assert set(tools) == {"check_connection", "run_nql_query", "list_devices", "get_device", "list_applications", "get_app_crashes", "summarize_experience"}
    for name, t in tools.items():
        d = t.description.lower()
        assert "latency" in d, name
        assert "limit" in d or "max" in d, name
        assert not any(w in name for w in ("create", "update", "delete", "remote", "set_", "run_action")), name
    assert "truncated" in tools["summarize_experience"].description


def test_check_connection_ok_and_never_leaks_secret():
    fake = FakeNexthink().on("devices", [{"uid": "d"}])
    mcp, _ = make(fake)
    out = run(call(mcp, "check_connection"))
    assert out["status"] == "ok" and "s3cr3t" not in str(out)


def test_check_connection_reports_not_configured_and_auth_errors():
    from nexthink_mcp.config import Settings

    mcp, _ = make(FakeNexthink(), settings=Settings(base_url="https://nx.test"))
    out = run(call(mcp, "check_connection"))
    assert out["status"] == "not_configured" and out["missingConfiguration"] == ["NEXTHINK_CLIENT_ID", "NEXTHINK_CLIENT_SECRET"]
    fake = FakeNexthink()
    fake.fail_next = [403]
    out = run(call(make(fake)[0], "check_connection"))
    assert out["status"] == "error" and "403" in out["error"]


def test_run_nql_query_truncation_and_validation():
    fake = FakeNexthink().on("devices", [{"uid": str(i)} for i in range(4)])
    mcp, _ = make(fake)
    out = run(call(mcp, "run_nql_query", {"query": "devices | list uid", "max_rows": 3}))
    assert out["truncated"] and out["rowCount"] == 3 and out["maxRows"] == 3
    with pytest.raises(ToolError, match="max_rows"):
        run(call(mcp, "run_nql_query", {"query": "devices | list uid", "max_rows": 501}))
    with pytest.raises(ToolError):
        run(call(mcp, "run_nql_query", {"query": "  "}))


def test_list_devices_has_stable_ids_and_truncated():
    fake = FakeNexthink().on("devices", [device_row("dev-1"), device_row("dev-2", "CHI-LT-0002")])
    out = run(call(make(fake)[0], "list_devices", {"site": "Chicago", "limit": 1}))
    assert out["truncated"] and out["count"] == 1
    d = out["devices"][0]
    assert d["deviceId"] == "dev-1" and d["userId"] == "usr-1" and d["siteId"] == "site-1" and d["osVersion"] == "10.0.22631.4602"


def test_get_device_found_and_not_found():
    fake = (FakeNexthink()
            .on("devices\n| where uid", [device_row()])
            .on("installations", [{"application.name": "Slack", "application.publisher": "S", "application.version": "4.40.0", "installed_at": "2026-01-01T00:00:00Z"}])
            .on("device_performance.boots", [{"time": "2026-11-17T08:00:00Z", "boot.duration_s": 41.0, "operating_system.version": "x"}])
            .on("execution.crashes", [{"application.name": "Slack", "application.version": "4.40.0", "crashes": 2, "lastCrash": "2026-11-16T10:00:00Z"}]))
    mcp, _ = make(fake)
    out = run(call(mcp, "get_device", {"device_id": "dev-1"}))
    assert out["found"] and out["device"]["deviceId"] == "dev-1" and out["device"]["userId"] == "usr-1"
    assert out["installedApplications"]["items"][0]["application"] == "Slack"
    assert out["recentBoots"][0]["bootSeconds"] == 41.0 and out["crashesLast14Days"]["items"][0]["crashes"] == 2
    assert run(call(make(FakeNexthink())[0], "get_device", {"device_id": "nope"})) == {"found": False, "deviceId": "nope"}
    with pytest.raises(ToolError):
        run(call(mcp, "get_device", {"device_id": 'x" | list uid'}))


def test_list_applications_groups_versions():
    rows = [
        {"application.name": "Zoom Workplace", "application.publisher": "Zoom", "application.version": "6.1.5", "devices": 60},
        {"application.name": "Zoom Workplace", "application.publisher": "Zoom", "application.version": "6.2.0", "devices": 40},
        {"application.name": "Slack", "application.publisher": "S", "application.version": "4.40.0", "devices": 30},
    ]
    out = run(call(make(FakeNexthink().on("installations", rows))[0], "list_applications", {"name_contains": "o"}))
    zoom = out["applications"][0]
    assert zoom["application"] == "Zoom Workplace" and zoom["devices"] == 100 and len(zoom["versions"]) == 2 and not out["truncated"]


def test_get_app_crashes_combines_breakdowns_and_install_base():
    fake = (FakeNexthink()
            .on("summarize crashes = count(), firstCrash", [{"application.name": "Zoom Workplace", "application.version": "6.2.0", "crashes": 30, "firstCrash": "a", "lastCrash": "b"}])
            .on("by site.uid", [{"site.uid": "s1", "site.name": "Chicago Office", "crashes": 30}])
            .on("by operating_system.version", [{"operating_system.version": "10.0.22631.4602", "crashes": 30}])
            .on("by device.uid", [{"device.uid": "dev-1", "device.name": "n", "user.uid": "usr-1", "user.name": "u", "site.name": "Chicago Office",
                                   "application.name": "Zoom Workplace", "application.version": "6.2.0", "crashes": 9, "lastCrash": "b"}])
            .on("installations", [{"application.name": "Zoom Workplace", "application.version": "6.2.0", "devices": 10}]))
    out = run(call(make(fake)[0], "get_app_crashes", {"application": "zoom", "version": "6.2.0", "days": 3}))
    assert out["byVersion"][0]["crashesPerInstalledDevice"] == 3.0 and out["totalCrashes"] == 30
    assert out["devices"][0]["deviceId"] == "dev-1" and out["devices"][0]["userId"] == "usr-1"
    assert not any(out["truncated"].values())
    q = [q for q in fake.queries() if "by device.uid" in q][0]
    assert 'application.name contains "zoom" and application.version == "6.2.0"' in q and "during past 3d" in q
    with pytest.raises(ToolError, match="days"):
        run(call(make(FakeNexthink())[0], "get_app_crashes", {"application": "x", "days": 31}))


def test_summarize_experience_aggregates_on_the_server_and_reports_truncated():
    crashes = [crash_event(device=f"d{i % 3}", site="Chicago Office") for i in range(9)] + [crash_event(site="Austin Office", device="d9")]
    boots = [boot_event(device=f"d{i}", site="Chicago Office", secs=90.0) for i in range(12)] + [boot_event(site="Austin Office", secs=40.0)]
    fake = (FakeNexthink().on("execution.crashes", crashes).on("device_performance.boots", boots)
            .on("devices", [{"site.name": "Chicago Office", "devices": 30}, {"site.name": "Austin Office", "devices": 40}]))
    mcp, _ = make(fake)
    out = run(call(mcp, "summarize_experience", {"days": 7, "group_by": "site"}))
    assert out["truncated"] is False and out["window"]["end"] == "2026-11-17T12:00:00Z" and out["window"]["start"] == "2026-11-10T12:00:00Z"
    chi = out["groups"][0]
    assert chi["group"] == "Chicago Office" and chi["crashes"] == 9 and chi["affectedDevices"] == 3 and chi["devicePopulation"] == 30
    assert chi["crashesPer100Devices"] == 30.0 and chi["boots"]["avgSeconds"] == 90.0 and chi["boots"]["over60s"] == 12
    assert chi["sampleDeviceIds"] and chi["sampleUserIds"] == ["usr-1"]
    assert out["highlights"] == {"mostCrashes": "Chicago Office", "highestCrashRate": "Chicago Office", "slowestBoots": "Chicago Office"}
    assert out["totals"] == {"crashes": 10, "boots": 13, "devicesWithCrashes": 4}
    # the export queries carry the cap + 1 and are sorted newest first
    assert all("| sort time desc\n| limit 20001" in q for q in fake.queries() if "crashes" in q or "boots" in q)

    out = run(call(make(fake)[0], "summarize_experience", {"max_rows": 100}))  # tool minimum is 100 rows
    assert out["truncated"] is False
    big = FakeNexthink().on("execution.crashes", [crash_event() for _ in range(150)]).on("device_performance.boots", [boot_event()]).on("devices", [])
    out = run(call(make(big)[0], "summarize_experience", {"max_rows": 100}))
    assert out["truncated"] is True and out["truncatedSources"] == ["crashes"] and out["totals"]["crashes"] == 100
    assert any("lower bounds" in n for n in out["notes"])


def test_summarize_experience_application_grouping_and_filter():
    fake = (FakeNexthink().on("execution.crashes", [crash_event(ver="6.2.0")] * 4 + [crash_event(ver="6.1.5")])
            .on("installations", [{"application.name": "Zoom Workplace", "application.version": "6.2.0", "devices": 8},
                                  {"application.name": "Zoom Workplace", "application.version": "6.1.5", "devices": 50}]))
    out = run(call(make(fake)[0], "summarize_experience", {"group_by": "application_version", "application": "zoom"}))
    top = out["groups"][0]
    assert top["group"] == "Zoom Workplace 6.2.0" and top["crashesPer100Devices"] == 50.0 and top["boots"] is None
    assert out["highlights"]["highestCrashRate"] == "Zoom Workplace 6.2.0"
    assert not any("boots" in q and "device_performance" in q for q in fake.queries())  # boots skipped with an app filter
    assert any("skipped" in n for n in out["notes"])


def test_aggregate_day_and_percentiles():
    boots = [boot_event(t=f"2026-11-1{d}T08:00:00Z", secs=float(s)) for d, s in [(5, 10), (5, 20), (6, 30), (6, 40), (6, 100)]]
    groups = aggregate([], [_rename(b, BOOT_FIELDS) for b in boots], "day", None)
    assert [g["group"] for g in groups] == ["2026-11-15", "2026-11-16"]
    assert groups[1]["boots"] == {"count": 3, "avgSeconds": 56.7, "p50Seconds": 40.0, "p95Seconds": 100.0, "over60s": 1}
    assert groups[1]["sampleDeviceIds"] == ["dev-1"]

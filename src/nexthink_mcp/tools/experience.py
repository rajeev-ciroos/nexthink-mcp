from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .context import LATENCY, AppContext, bounded

GroupBy = Literal["site", "application", "application_version", "os_version", "network_type", "day"]
SLOW_BOOT_S = 60
MAX_GROUPS = 50
MIN_BOOTS_FOR_HIGHLIGHT = 10


def _crash_key(e: dict, group_by: str) -> str | None:
    return {
        "site": e["siteName"], "application": e["application"], "application_version": f"{e['application']} {e['appVersion']}",
        "os_version": e["osVersion"], "network_type": e["networkType"], "day": (e["time"] or "")[:10],
    }[group_by]


def _boot_key(e: dict, group_by: str) -> str | None:
    if group_by in ("application", "application_version"):
        return None  # boots are not tied to an application
    return _crash_key({**e, "application": "", "appVersion": ""}, group_by)


def _pct(sorted_vals: list[float], p: float) -> float:
    return round(sorted_vals[max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)], 1)


def aggregate(crashes: list[dict], boots: list[dict], group_by: str, population: dict[str, int] | None) -> list[dict]:
    c_by: dict[str, list[dict]] = defaultdict(list)
    b_by: dict[str, list[dict]] = defaultdict(list)
    for e in crashes:
        if (k := _crash_key(e, group_by)):
            c_by[k].append(e)
    for e in boots:
        if (k := _boot_key(e, group_by)) and e["bootSeconds"] is not None:
            b_by[k].append(e)
    groups = []
    for key in set(c_by) | set(b_by):
        cs, bs = c_by.get(key, []), b_by.get(key, [])
        per_device = Counter(e["deviceId"] for e in cs)
        pop = (population or {}).get(key)
        g: dict = {
            "group": key, "crashes": len(cs), "affectedDevices": len(per_device),
            "affectedUsers": len({e["userId"] for e in cs}),
            "devicePopulation": pop, "crashesPer100Devices": round(100 * len(cs) / pop, 1) if pop else None,
            "boots": None,
        }
        if bs:
            secs = sorted(e["bootSeconds"] for e in bs)
            g["boots"] = {
                "count": len(secs), "avgSeconds": round(sum(secs) / len(secs), 1), "p50Seconds": _pct(secs, 50),
                "p95Seconds": _pct(secs, 95), f"over{SLOW_BOOT_S}s": sum(1 for s in secs if s > SLOW_BOOT_S),
            }
        if per_device:
            g["sampleDeviceIds"] = [d for d, _ in per_device.most_common(5)]
            g["sampleUserIds"] = list(dict.fromkeys(e["userId"] for e in cs if e["deviceId"] in g["sampleDeviceIds"]))[:5]
        elif bs:
            slowest = sorted(bs, key=lambda e: -e["bootSeconds"])[:5]
            g["sampleDeviceIds"] = list(dict.fromkeys(e["deviceId"] for e in slowest))
            g["sampleUserIds"] = list(dict.fromkeys(e["userId"] for e in slowest))
        groups.append(g)
    if group_by == "day":
        return sorted(groups, key=lambda g: g["group"])
    return sorted(groups, key=lambda g: (-g["crashes"], -((g["boots"] or {}).get("avgSeconds") or 0), g["group"]))


def _highlights(groups: list[dict]) -> dict:
    crashy = [g for g in groups if g["crashes"]]
    slow = [g for g in groups if g["boots"] and g["boots"]["count"] >= MIN_BOOTS_FOR_HIGHLIGHT]
    best = max(crashy, key=lambda g: (g["crashesPer100Devices"] or 0, g["crashes"]), default=None)
    return {
        "mostCrashes": max(crashy, key=lambda g: g["crashes"])["group"] if crashy else None,
        "highestCrashRate": best["group"] if best and best["crashesPer100Devices"] else None,
        "slowestBoots": max(slow, key=lambda g: g["boots"]["avgSeconds"])["group"] if slow else None,
    }


def _window(executed_at: str | None, days: int) -> dict:
    try:
        end = datetime.fromisoformat(executed_at.replace("Z", "+00:00")) if executed_at else datetime.now(timezone.utc)
    except ValueError:
        end = datetime.now(timezone.utc)
    f = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"start": f(end - timedelta(days=days)), "end": f(end), "days": days}


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "Summarise device experience (application crashes and boot times) over the last `days` (default 7, max 30), "
        "aggregated on the server and grouped by group_by: site, application, application_version, os_version, "
        "network_type or day. Per group: crashes, affected devices/users, device population and crashes per 100 devices "
        "(only for site/application/application_version), boot count, average/p50/p95 boot seconds and boots over 60 s, "
        "and sample deviceIds/userIds to drill into with get_device. `highlights` names the worst groups. Filters: site "
        "(substring) and application (substring; boots are then skipped because they are not per application). "
        "Typical use: find which site, app version or OS build explains a spike, then repeat with a filter and another group_by. "
        "Limits: reads at most max_rows events per source (default 20000, max 50000), newest first; if more exist "
        "`truncated` is true and the OLDEST events in the window are missing, so counts are lower bounds; narrow `days` or add a "
        "filter. At most 50 groups are returned (`groupsOmitted` counts the rest). "
        + LATENCY
    ))
    async def summarize_experience(
        days: int = 7, group_by: GroupBy = "site", site: str | None = None, application: str | None = None,
        max_rows: int = 20000,
    ) -> dict:
        bounded("days", days, 1, 30)
        bounded("max_rows", max_rows, 100, 50000)
        data = await ctx.client.experience_events(days=days, site=site, application=application, max_rows=max_rows)
        population = None
        if group_by == "site":
            population = await ctx.client.device_counts_by_site(site)
        elif group_by in ("application", "application_version"):
            installs = await ctx.client.install_counts(application)
            population = defaultdict(int)
            for (name, version), n in installs.items():
                population[name if group_by == "application" else f"{name} {version}"] += n
        groups = aggregate(data["crashes"], data["boots"], group_by, population)
        truncated_sources = [n for n, t in (("crashes", data["crashesTruncated"]), ("boots", data["bootsTruncated"])) if t]
        notes = []
        if truncated_sources:
            notes.append(f"{' and '.join(truncated_sources)} hit the {max_rows}-event cap: the oldest events in the window are missing and counts are lower bounds.")
        if data["bootsSkipped"]:
            notes.append("Boot metrics are skipped because an application filter is set.")
        if group_by in ("application", "application_version"):
            notes.append("Boot metrics are not available when grouping by application.")
        if group_by in ("os_version", "network_type", "day"):
            notes.append("Device population is not available for this grouping, so no per-100-devices rate is given.")
        return {
            "window": _window(data["executedAt"], days), "groupBy": group_by, "filters": {"site": site, "application": application},
            "truncated": bool(truncated_sources), "truncatedSources": truncated_sources, "maxRowsPerSource": max_rows,
            "totals": {
                "crashes": len(data["crashes"]), "boots": len(data["boots"]),
                "devicesWithCrashes": len({e["deviceId"] for e in data["crashes"]}),
            },
            "highlights": _highlights(groups), "groups": groups[:MAX_GROUPS], "groupsOmitted": max(0, len(groups) - MAX_GROUPS),
            "notes": notes,
        }

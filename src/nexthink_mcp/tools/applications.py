from __future__ import annotations

from collections import defaultdict

from mcp.server.fastmcp import FastMCP

from .context import LATENCY, AppContext, bounded


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool(description=(
        "List installed applications with the versions in use and how many devices run each version. name_contains is a "
        "case-insensitive substring filter. Use it to see which versions are deployed (e.g. mid-rollout). "
        "Limits: at most `limit` (application, version) pairs (default 100, max 500), biggest first; `truncated` is true "
        "when there are more, so small versions can be missing. Counts come from inventory, not live usage. " + LATENCY
    ))
    async def list_applications(name_contains: str | None = None, limit: int = 100) -> dict:
        bounded("limit", limit, 1, 500)
        page = await ctx.client.list_applications(name_contains=name_contains, limit=limit)
        apps: dict[str, dict] = {}
        versions: dict[str, list] = defaultdict(list)
        for r in page.items:
            apps.setdefault(r["application"], {"application": r["application"], "publisher": r["publisher"], "devices": 0})
            apps[r["application"]]["devices"] += r["devices"]
            versions[r["application"]].append({"version": r["appVersion"], "devices": r["devices"]})
        out = sorted(({**a, "versions": versions[n]} for n, a in apps.items()), key=lambda a: -a["devices"])
        return {"applicationCount": len(out), "versionRows": len(page.items), "limit": limit, "truncated": page.truncated, "applications": out}

    @mcp.tool(description=(
        "Crashes of one application (case-insensitive substring match on the name) over the last `days` (default 7, max 30), "
        "broken down by version, site and OS version, plus the crashing devices with their deviceId and userId and how many "
        "devices have each version installed (to judge rates). Optional filters: version (exact), site (substring). "
        "Use it to tell whether a crash spike follows an app version, a site or an OS build. "
        "Limits: breakdowns show the top 50 rows; `devices` shows the top max_devices (default 25, max 200) by crash count; "
        "each section has its own `truncated` flag. " + LATENCY
    ))
    async def get_app_crashes(
        application: str, version: str | None = None, site: str | None = None, days: int = 7, max_devices: int = 25,
    ) -> dict:
        bounded("days", days, 1, 30)
        bounded("max_devices", max_devices, 1, 200)
        r = await ctx.client.app_crash_report(application=application, version=version, site=site, days=days, max_devices=max_devices)
        installed = {(i["application"], i["appVersion"]): i["devices"] for i in r["installed"]}
        by_version = []
        for row in r["byVersion"].items:
            n = installed.get((row["application"], row["appVersion"]))
            by_version.append({**row, "devicesInstalled": n, "crashesPerInstalledDevice": round(row["crashes"] / n, 3) if n else None})
        sections = {"byVersion": r["byVersion"], "bySite": r["bySite"], "byOsVersion": r["byOsVersion"], "devices": r["devices"]}
        return {
            "application": application, "versionFilter": version, "siteFilter": site, "days": days, "asOf": r["executedAt"],
            "totalCrashes": sum(x["crashes"] for x in r["byVersion"].items),
            "totalCrashesIsLowerBound": r["byVersion"].truncated,
            "byVersion": by_version,
            "bySite": r["bySite"].items,
            "byOsVersion": r["byOsVersion"].items,
            "devices": r["devices"].items,
            "truncated": {k: v.truncated for k, v in sections.items()},
            "limits": {"breakdownRows": 50, "maxDevices": max_devices},
        }

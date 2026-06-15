from __future__ import annotations

import os
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from apt_index.published_state import PublishedState

JsonLoader = Callable[[Path, Any], Any]
JsonWriter = Callable[[Path, Any], None]
JsonPoster = Callable[[str, Any, dict[str, str] | None], Any]
TimestampFactory = Callable[[], str]
GraphqlTimeFormatter = Callable[[datetime], str]


def write_download_stats(
    path: Path,
    hostname: str | None,
    *,
    days: int,
    strict: bool,
    write_json: JsonWriter,
    empty_download_stats: Callable[[str, int], dict[str, Any]],
    resolve_cloudflare_zone_id: Callable[[str, str], str | None],
    fetch_download_stats: Callable[[str, str, str, int, dict[str, tuple[str, str]] | None], dict[str, Any]],
    state: PublishedState,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token or not hostname:
        if strict:
            raise RuntimeError("CLOUDFLARE_API_TOKEN and repository hostname are required to export download stats")
        logger.warning("Cloudflare credentials are missing; writing empty download stats")
        write_json(path, empty_download_stats("missing_cloudflare_credentials", days))
        return

    try:
        zone_id = resolve_cloudflare_zone_id(token, hostname)
        if not zone_id:
            raise RuntimeError(f"could not resolve Cloudflare zone for {hostname!r}")
        path_index = state.download_path_index()
        stats = fetch_download_stats(zone_id, token, hostname, days, path_index)
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        if strict:
            raise
        logger.warning("Cloudflare HTTP analytics query failed; writing empty download stats: {}", exc)
        stats = empty_download_stats("analytics_query_failed", days)
    write_json(path, stats)


def fetch_download_stats(
    zone_id: str,
    token: str,
    hostname: str,
    *,
    days: int,
    path_index: dict[str, tuple[str, str]] | None,
    max_days: int,
    cloudflare_graphql: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    now: Callable[[], datetime],
    now_iso: TimestampFactory,
    graphql_time: GraphqlTimeFormatter,
) -> dict[str, Any]:
    path_index = path_index or {}
    query_days = min(days, max_days)
    end = now().replace(microsecond=0)
    start = end - timedelta(days=query_days)
    seven_day_start = end - timedelta(days=min(7, query_days))
    package_counts: dict[tuple[str, str], int] = {}
    seven_day_counts: dict[tuple[str, str], int] = {}
    daily_rows: list[dict[str, Any]] = []

    for window_start, window_end in daily_time_windows(start, end):
        rows = fetch_download_path_rows(
            zone_id,
            token,
            hostname,
            window_start,
            window_end,
            cloudflare_graphql=cloudflare_graphql,
            graphql_time=graphql_time,
        )
        counts = aggregate_path_download_counts(rows, path_index)
        merge_download_counts(package_counts, counts)
        if window_start >= seven_day_start:
            merge_download_counts(seven_day_counts, counts)
        daily_rows.append(
            {
                "day": window_start.date().isoformat(),
                "downloads": sum(counts.values()),
            }
        )

    return format_download_stats(
        download_counts_to_rows(package_counts),
        download_counts_to_rows(seven_day_counts),
        daily_rows,
        hostname,
        query_days,
        now_iso,
    )


def daily_time_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=1), end)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


def fetch_download_path_rows(
    zone_id: str,
    token: str,
    hostname: str,
    start: datetime,
    end: datetime,
    *,
    cloudflare_graphql: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    graphql_time: GraphqlTimeFormatter,
) -> list[dict[str, Any]]:
    query = """
query AptIndexDownloadStats($zoneTag: string, $packageFilter: filter) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      packageRows: httpRequestsAdaptiveGroups(limit: 10000, filter: $packageFilter, orderBy: [count_DESC]) {
        count
        dimensions { clientRequestPath }
      }
    }
  }
}
""".strip()
    variables = {
        "zoneTag": zone_id,
        "packageFilter": http_download_filter(hostname, start, end, graphql_time),
    }
    payload = cloudflare_graphql(token, query, variables)
    zones = payload.get("data", {}).get("viewer", {}).get("zones", [])
    if not zones:
        raise RuntimeError(f"Cloudflare zone {zone_id!r} returned no HTTP analytics rows")
    return list(zones[0].get("packageRows", []))


def http_download_filter(
    hostname: str,
    start: datetime,
    end: datetime,
    graphql_time: GraphqlTimeFormatter,
) -> dict[str, Any]:
    return {
        "datetime_geq": graphql_time(start),
        "datetime_lt": graphql_time(end),
        "requestSource": "eyeball",
        "clientRequestHTTPHost": hostname,
        "clientRequestHTTPMethodName": "GET",
        "clientRequestPath_like": "/pool/%",
    }


def cloudflare_graphql(token: str, query: str, variables: dict[str, Any], *, post_json: JsonPoster) -> dict[str, Any]:
    payload = post_json(
        "https://api.cloudflare.com/client/v4/graphql",
        {"query": query, "variables": variables},
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"Cloudflare GraphQL query failed: {errors!r}")
    return payload


def aggregate_path_download_rows(
    rows: list[dict[str, Any]],
    path_index: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    return download_counts_to_rows(aggregate_path_download_counts(rows, path_index))


def aggregate_path_download_counts(
    rows: list[dict[str, Any]],
    path_index: dict[str, tuple[str, str]],
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        path = str(row.get("dimensions", {}).get("clientRequestPath") or "")
        package_identity = path_index.get(path)
        if not package_identity:
            continue
        counts[package_identity] = counts.get(package_identity, 0) + int(row.get("count") or 0)
    return counts


def merge_download_counts(target: dict[tuple[str, str], int], source: dict[tuple[str, str], int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def download_counts_to_rows(counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    return [
        {"entry_name": entry_name, "arch": arch, "downloads": downloads}
        for (entry_name, arch), downloads in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def parse_package_download_path(path: str) -> tuple[str, str, str] | None:
    parts = path.split("/")
    if len(parts) != 5 or parts[0] or parts[1] != "pool":
        return None
    return parts[2], parts[3], parts[4]


def format_download_stats(
    package_rows: list[dict[str, Any]],
    seven_day_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    hostname: str,
    days: int,
    now_iso: TimestampFactory,
) -> dict[str, Any]:
    seven_day_counts = {
        (str(row.get("entry_name") or ""), str(row.get("arch") or "")): int(row.get("downloads") or 0)
        for row in seven_day_rows
    }
    packages = []
    for row in package_rows:
        entry_name = str(row.get("entry_name") or "")
        arch = str(row.get("arch") or "")
        downloads = int(row.get("downloads") or 0)
        packages.append(
            {
                "entry_name": entry_name,
                "arch": arch,
                "downloads": downloads,
                "last_7_days": seven_day_counts.get((entry_name, arch), 0),
            }
        )

    daily = [
        {
            "date": normalize_day(row.get("day")),
            "downloads": int(row.get("downloads") or 0),
        }
        for row in daily_rows
    ]
    last_days = sum(row["downloads"] for row in packages)
    last_7_days = sum(row["last_7_days"] for row in packages)
    return {
        "version": 1,
        "generated_at": now_iso(),
        "source": "cloudflare_http_requests",
        "hostname": hostname,
        "window_days": days,
        "totals": {
            "downloads": last_days,
            "last_days": last_days,
            "last_7_days": last_7_days,
        },
        "packages": packages,
        "daily": daily,
    }


def normalize_day(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def empty_download_stats(reason: str, days: int, now_iso: TimestampFactory) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": now_iso(),
        "source": "none",
        "reason": reason,
        "window_days": days,
        "totals": {
            "downloads": 0,
            "last_days": 0,
            "last_7_days": 0,
        },
        "packages": [],
        "daily": [],
    }

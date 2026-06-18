from __future__ import annotations

import os
import urllib.error
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from apt_index.published_state import DownloadPathIndex, PackageIdentity, PublishedState
from apt_index.runtime import JsonFiles, SystemClock

JsonFetcher = Callable[[str, dict[str, str] | None], Any]
JsonPoster = Callable[[str, Any, dict[str, str] | None], Any]
TimestampFactory = Callable[[], str]
GraphqlTimeFormatter = Callable[[datetime], str]


class DownloadStatsTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    downloads: int
    last_days: int
    last_7_days: int


class DownloadStatsPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_name: str
    arch: str
    downloads: int
    last_7_days: int

    @property
    def identity(self) -> PackageIdentity:
        return PackageIdentity(entry_name=self.entry_name, arch=self.arch)


class DownloadStatsDay(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: str
    downloads: int


class PackageDownloadCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity: PackageIdentity
    downloads: int


class DownloadStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = 1
    generated_at: str
    source: Literal["cloudflare_http_requests", "none"]
    reason: str | None = None
    hostname: str | None = None
    window_days: int
    totals: DownloadStatsTotals
    packages: list[DownloadStatsPackage] = Field(default_factory=list)
    daily: list[DownloadStatsDay] = Field(default_factory=list)

    @classmethod
    def empty(cls, reason: str, *, days: int, clock: SystemClock) -> "DownloadStats":
        return empty_download_stats(reason, days, clock.now_iso)

    @classmethod
    def load_or_empty(cls, path: Path, *, json_files: JsonFiles, days: int, clock: SystemClock) -> "DownloadStats":
        payload = json_files.load(path, None)
        if not payload:
            return cls.empty("not_generated", days=days, clock=clock)
        return cls.model_validate(payload)

    @property
    def data(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def rows_by_identity(self) -> dict[PackageIdentity, DownloadStatsPackage]:
        return {
            package.identity: package
            for package in self.packages
        }

    def write(self, path: Path, json_files: JsonFiles) -> None:
        json_files.write(path, self.data)


class CloudflareHttpAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    token: str | None
    fetch_json: JsonFetcher
    post_json: JsonPoster
    max_days: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.token)

    def resolve_zone_id(self, hostname: str) -> str | None:
        if not self.token:
            return None
        configured_zone_id = os.environ.get("CLOUDFLARE_ZONE_ID")
        if configured_zone_id:
            return configured_zone_id

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        for zone_name in cloudflare_zone_name_candidates(hostname):
            query = urllib.parse.urlencode({"name": zone_name})
            payload = self.fetch_json(f"https://api.cloudflare.com/client/v4/zones?{query}", headers)
            if not payload.get("success"):
                raise RuntimeError(f"Cloudflare zone lookup failed: {payload.get('errors')!r}")
            for zone in payload.get("result", []):
                if zone.get("name") == zone_name and (hostname == zone_name or hostname.endswith("." + zone_name)):
                    zone_id = zone.get("id")
                    if zone_id:
                        return str(zone_id)
        return None

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("CLOUDFLARE_API_TOKEN is required to query Cloudflare HTTP analytics")
        payload = self.post_json(
            "https://api.cloudflare.com/client/v4/graphql",
            {"query": query, "variables": variables},
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(f"Cloudflare GraphQL query failed: {errors!r}")
        return payload

    def fetch_download_stats(
        self,
        hostname: str,
        *,
        days: int,
        path_index: DownloadPathIndex,
        clock: SystemClock,
    ) -> DownloadStats:
        zone_id = self.resolve_zone_id(hostname)
        if not zone_id:
            raise RuntimeError(f"could not resolve Cloudflare zone for {hostname!r}")

        def query_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            del token
            return self.graphql(query, variables)

        return fetch_download_stats(
            zone_id,
            self.token or "",
            hostname,
            days=days,
            path_index=path_index,
            max_days=self.max_days,
            cloudflare_graphql=query_graphql,
            now=clock.utc_now,
            now_iso=clock.now_iso,
            graphql_time=clock.graphql_time,
        )


class DownloadStatsPublisher(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    state: PublishedState
    analytics: CloudflareHttpAnalytics
    clock: SystemClock
    json_files: JsonFiles

    def write(self, path: Path, hostname: str | None, *, days: int, strict: bool) -> DownloadStats:
        if not self.analytics.has_credentials or not hostname:
            if strict:
                raise RuntimeError("CLOUDFLARE_API_TOKEN and repository hostname are required to export download stats")
            logger.warning("Cloudflare credentials are missing; writing empty download stats")
            stats = DownloadStats.empty("missing_cloudflare_credentials", days=days, clock=self.clock)
            stats.write(path, self.json_files)
            return stats

        try:
            stats = self.analytics.fetch_download_stats(
                hostname,
                days=days,
                path_index=self.state.download_paths(),
                clock=self.clock,
            )
        except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
            if strict:
                raise
            logger.warning("Cloudflare HTTP analytics query failed; writing empty download stats: {}", exc)
            stats = DownloadStats.empty("analytics_query_failed", days=days, clock=self.clock)
        stats.write(path, self.json_files)
        return stats


def cloudflare_zone_name_candidates(hostname: str) -> list[str]:
    labels = [label for label in hostname.lower().strip(".").split(".") if label]
    return [
        ".".join(labels[index:])
        for index in range(max(len(labels) - 1, 0))
        if len(labels[index:]) >= 2
    ]


def fetch_download_stats(
    zone_id: str,
    token: str,
    hostname: str,
    *,
    days: int,
    path_index: DownloadPathIndex | None,
    max_days: int,
    cloudflare_graphql: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    now: Callable[[], datetime],
    now_iso: TimestampFactory,
    graphql_time: GraphqlTimeFormatter,
) -> DownloadStats:
    path_index = path_index or DownloadPathIndex(paths={})
    query_days = min(days, max_days)
    end = now().replace(microsecond=0)
    start = end - timedelta(days=query_days)
    seven_day_start = end - timedelta(days=min(7, query_days))
    package_counts: dict[PackageIdentity, int] = {}
    seven_day_counts: dict[PackageIdentity, int] = {}
    daily_rows: list[DownloadStatsDay] = []

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
            DownloadStatsDay(
                date=window_start.date().isoformat(),
                downloads=sum(counts.values()),
            )
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


def aggregate_path_download_rows(
    rows: list[dict[str, Any]],
    path_index: DownloadPathIndex,
) -> list[PackageDownloadCount]:
    return download_counts_to_rows(aggregate_path_download_counts(rows, path_index))


def aggregate_path_download_counts(
    rows: list[dict[str, Any]],
    path_index: DownloadPathIndex,
) -> dict[PackageIdentity, int]:
    counts: dict[PackageIdentity, int] = {}
    for row in rows:
        path = str(row.get("dimensions", {}).get("clientRequestPath") or "")
        package_identity = path_index.identity_for(path)
        if not package_identity:
            continue
        counts[package_identity] = counts.get(package_identity, 0) + int(row.get("count") or 0)
    return counts


def merge_download_counts(target: dict[PackageIdentity, int], source: dict[PackageIdentity, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def download_counts_to_rows(counts: dict[PackageIdentity, int]) -> list[PackageDownloadCount]:
    return [
        PackageDownloadCount(identity=identity, downloads=downloads)
        for identity, downloads in sorted(counts.items(), key=download_count_sort_key)
    ]


def download_count_sort_key(item: tuple[PackageIdentity, int]) -> tuple[int, str, str]:
    identity, downloads = item
    return -downloads, identity.entry_name, identity.arch


def parse_package_download_path(path: str) -> tuple[str, str, str] | None:
    parts = path.split("/")
    if len(parts) != 5 or parts[0] or parts[1] != "pool":
        return None
    return parts[2], parts[3], parts[4]


def format_download_stats(
    package_rows: list[PackageDownloadCount],
    seven_day_rows: list[PackageDownloadCount],
    daily_rows: list[DownloadStatsDay],
    hostname: str,
    days: int,
    now_iso: TimestampFactory,
) -> DownloadStats:
    seven_day_counts = {
        row.identity: row.downloads
        for row in seven_day_rows
    }
    packages: list[DownloadStatsPackage] = []
    for row in package_rows:
        packages.append(
            DownloadStatsPackage(
                entry_name=row.identity.entry_name,
                arch=row.identity.arch,
                downloads=row.downloads,
                last_7_days=seven_day_counts.get(row.identity, 0),
            )
        )

    daily = [
        DownloadStatsDay(date=normalize_day(row.date), downloads=row.downloads)
        for row in daily_rows
    ]
    last_days = sum(row.downloads for row in packages)
    last_7_days = sum(row.last_7_days for row in packages)
    return DownloadStats(
        generated_at=now_iso(),
        source="cloudflare_http_requests",
        hostname=hostname,
        window_days=days,
        totals=DownloadStatsTotals(
            downloads=last_days,
            last_days=last_days,
            last_7_days=last_7_days,
        ),
        packages=packages,
        daily=daily,
    )


def normalize_day(value: Any) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def empty_download_stats(reason: str, days: int, now_iso: TimestampFactory) -> DownloadStats:
    return DownloadStats(
        generated_at=now_iso(),
        source="none",
        reason=reason,
        window_days=days,
        totals=DownloadStatsTotals(downloads=0, last_days=0, last_7_days=0),
    )

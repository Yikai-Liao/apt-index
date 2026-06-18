from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from apt_index.published_state import PublishedArtifact, PublishedState
from apt_index.download_stats import DownloadStats
from apt_index.runtime import JsonFiles, SystemClock


class HealthReports(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    track: dict[str, Any]
    artifact: dict[str, Any]
    track_path: Path | None = None
    artifact_path: Path | None = None
    track_source_exists: bool = False
    artifact_source_exists: bool = False

    @classmethod
    def load_or_not_generated(
        cls,
        state: PublishedState,
        *,
        track_health_path: Path,
        artifact_health_path: Path,
        json_files: JsonFiles,
        clock: SystemClock,
    ) -> "HealthReports":
        track_source_exists = track_health_path.exists()
        artifact_source_exists = artifact_health_path.exists()
        return cls(
            track=json_files.load(track_health_path, None) or not_generated_track_health(state, clock),
            artifact=json_files.load(artifact_health_path, None) or not_generated_artifact_health(state, clock),
            track_path=track_health_path,
            artifact_path=artifact_health_path,
            track_source_exists=track_source_exists,
            artifact_source_exists=artifact_source_exists,
        )

    def write_deploy_files(self, dist_dir: Path, json_files: JsonFiles) -> None:
        self._write_report(
            source_path=self.track_path,
            source_exists=self.track_source_exists,
            target=dist_dir / (self.track_path.name if self.track_path else "track_health.json"),
            fallback=self.track,
            json_files=json_files,
        )
        self._write_report(
            source_path=self.artifact_path,
            source_exists=self.artifact_source_exists,
            target=dist_dir / (self.artifact_path.name if self.artifact_path else "artifact_health.json"),
            fallback=self.artifact,
            json_files=json_files,
        )

    @staticmethod
    def _write_report(
        *,
        source_path: Path | None,
        source_exists: bool,
        target: Path,
        fallback: dict[str, Any],
        json_files: JsonFiles,
    ) -> None:
        if source_path and source_exists:
            shutil.copy2(source_path, target)
            return
        json_files.write(target, fallback)


class PublishedSiteData(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    state: PublishedState
    reports: HealthReports
    downloads: DownloadStats

    def to_json(self) -> dict[str, Any]:
        return format_site_data(self.state, self.reports, self.downloads).model_dump(mode="json")

    def write(self, output: Path, json_files: JsonFiles) -> None:
        json_files.write(output, self.to_json())


class SiteDataArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    arch: str
    version: str
    source: str
    update_policy: str
    size: int
    downloads: int
    downloads_last_7_days: int
    track_status: str
    artifact_status: str
    status_class: str


class SiteDataPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_name: str
    package_name: str
    description: str
    homepage: str
    artifacts: list[SiteDataArtifact] = Field(default_factory=list)


class SitePackageMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    homepage: str


class SiteDataSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_count: int
    row_count: int
    artifact_count: int
    total_size: int
    downloads_last_days: int
    downloads_last_7_days: int
    all_healthy: bool


class SiteData(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = 1
    generated_at: str | None
    window_days: int
    summary: SiteDataSummary
    packages: list[SiteDataPackage] = Field(default_factory=list)


def not_generated_track_health(state: PublishedState, clock: SystemClock) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": clock.now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "status": "not_checked",
                "architectures": {
                    artifact.configured_arch: {"status": "not_checked"}
                    for artifact in entry.artifacts
                },
            }
            for entry_name, entry in ((entry.entry_name, entry) for entry in state.entries)
        },
    }


def not_generated_artifact_health(state: PublishedState, clock: SystemClock) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": clock.now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "artifacts": {
                    artifact.configured_arch: not_checked_artifact_health(artifact)
                    for artifact in entry.artifacts
                }
            }
            for entry_name, entry in ((entry.entry_name, entry) for entry in state.entries)
        },
    }


def not_checked_artifact_health(artifact: PublishedArtifact) -> dict[str, Any]:
    health: dict[str, Any] = {"status": "not_checked", "check": "not_generated"}
    health["size"] = artifact.size
    health["sha256"] = artifact.sha256
    return health


def format_site_data(
    state: PublishedState,
    reports: HealthReports,
    download_stats: DownloadStats,
) -> SiteData:
    downloads_by_identity = download_stats.rows_by_identity
    packages: list[SiteDataPackage] = []
    artifact_count = 0
    total_size = 0
    downloads_last_days = 0
    downloads_last_7_days = 0

    for entry in state.entries_for_site():
        entry_name = entry.entry_name
        grouped_rows: dict[str, list[SiteDataArtifact]] = {}
        package_details: dict[str, SitePackageMetadata] = {}
        for artifact in entry.artifacts:
            package_name = artifact.package_name
            grouped_rows.setdefault(package_name, [])
            package_details.setdefault(
                package_name,
                SitePackageMetadata(description=artifact.description, homepage=artifact.homepage),
            )
            download_row = downloads_by_identity.get(artifact.download_identity)
            track_status = str(reports.track.get("packages", {}).get(entry_name, {}).get("architectures", {}).get(artifact.configured_arch, {}).get("status") or "unknown")
            artifact_status = str(reports.artifact.get("packages", {}).get(entry_name, {}).get("artifacts", {}).get(artifact.configured_arch, {}).get("status") or "unknown")
            downloads = download_row.downloads if download_row else 0
            artifact_downloads_last_7_days = download_row.last_7_days if download_row else 0
            grouped_rows[package_name].append(
                SiteDataArtifact(
                    arch=artifact.configured_arch,
                    version=artifact.version,
                    source=artifact.source,
                    update_policy=artifact.update_policy,
                    size=artifact.size,
                    downloads=downloads,
                    downloads_last_7_days=artifact_downloads_last_7_days,
                    track_status=track_status,
                    artifact_status=artifact_status,
                    status_class=status_class_for(track_status, artifact_status),
                )
            )
            artifact_count += 1
            total_size += artifact.size
            downloads_last_days += downloads
            downloads_last_7_days += artifact_downloads_last_7_days
        for package_name, artifacts in grouped_rows.items():
            metadata = package_details[package_name]
            packages.append(
                SiteDataPackage(
                    entry_name=entry_name,
                    package_name=package_name,
                    description=metadata.description,
                    homepage=metadata.homepage,
                    artifacts=sorted(artifacts, key=site_artifact_sort_key),
                )
            )

    packages = sorted(packages, key=site_package_sort_key)
    all_healthy = bool(packages) and all(
        artifact.status_class == "ok"
        for row in packages
        for artifact in row.artifacts
    )
    return SiteData(
        generated_at=state.generated_at,
        window_days=download_stats.window_days,
        summary=SiteDataSummary(
            entry_count=len(state.entries),
            row_count=len(packages),
            artifact_count=artifact_count,
            total_size=total_size,
            downloads_last_days=downloads_last_days,
            downloads_last_7_days=downloads_last_7_days,
            all_healthy=all_healthy,
        ),
        packages=packages,
    )


def site_artifact_sort_key(artifact: SiteDataArtifact) -> str:
    return artifact.arch


def site_package_sort_key(package: SiteDataPackage) -> tuple[str, str]:
    return package.package_name.casefold(), package.entry_name.casefold()


def status_class_for(track_status: str, artifact_status: str) -> str:
    if track_status == "ok" and artifact_status == "ok":
        return "ok"
    if track_status == "kept_previous" and artifact_status == "ok":
        return "warn"
    return "bad"

#!/usr/bin/env python3
from __future__ import annotations

import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from loguru import logger
from pydantic import BaseModel, ConfigDict

from apt_index import deb, deploy_tree, download_stats as download_stats_module, health, redirect, site_data as site_data_module, sources
from apt_index.config import AptIndexConfig, ConfigError, EntryConfig, ResolverKey, UpdatePolicy, load_configuration
from apt_index.paths import ARTIFACT_HEALTH_PATH, CACHE_DIR, DIST_DIR, LOCK_PATH, ROOT, TRACK_HEALTH_PATH
from apt_index.published_state import LockedArchitecture, LockedArtifact, LockedEntry, LockfileState, PublishedState
from apt_index.runtime import JsonFiles, SystemClock, UrlLibHttpClient

LEGACY_REDIRECT_RULES_PATHS = ("/redirect_rules.json",)
CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS = 7
USER_AGENT = "apt-index/0.1"
DEFAULT_JOBS = 4
app = typer.Typer(no_args_is_help=True)
JSON_FILES = JsonFiles()
CLOCK = SystemClock()
HTTP = UrlLibHttpClient(user_agent=USER_AGENT)


class ResolvedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry: LockedEntry
    full_checked_arches: set[str]
    architecture_health: dict[str, "ArchitectureHealth"]


class ArchitectureHealthStatus(str, Enum):
    OK = "ok"
    KEPT_PREVIOUS = "kept_previous"
    FAILED = "failed"


class PackageHealthStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ArchitectureHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ArchitectureHealthStatus
    source: ResolverKey
    update_policy: UpdatePolicy
    error: str | None = None

    def to_json(self) -> dict[str, str]:
        data = {
            "status": self.status.value,
            "source": self.source,
            "update_policy": self.update_policy,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@app.command("refresh")
def refresh_command(
    jobs: int | None = typer.Option(None, "--jobs", "-j", help="Maximum package refresh workers."),
    full_artifact_check: bool = typer.Option(False, "--full-artifact-check", help="Download and hash every locked artifact during health checks."),
) -> None:
    """Resolve upstream artifacts and write generated state."""
    refresh(jobs, full_artifact_check)


@app.command("build")
def build_command() -> None:
    """Build the deployable APT tree from the lockfile."""
    build()


@app.command("download-stats")
def download_stats_command(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output JSON path."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname to filter Cloudflare HTTP request analytics."),
    days: int = typer.Option(30, "--days", min=1, help="Number of days to publish in the public summary."),
    strict: bool = typer.Option(False, "--strict", help="Fail instead of writing empty stats when Cloudflare HTTP analytics cannot be queried."),
) -> None:
    """Write public download statistics from Cloudflare HTTP request analytics."""
    config = load_config()
    if hostname is None:
        hostname = urllib.parse.urlparse(config.repository.base_url).hostname
    download_stats_module.DownloadStatsPublisher(
        state=load_published_state(config.component),
        analytics=download_stats_module.CloudflareHttpAnalytics(
            token=os.environ.get("CLOUDFLARE_API_TOKEN"),
            fetch_json=HTTP.fetch_json,
            post_json=HTTP.post_json,
            max_days=CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS,
        ),
        clock=CLOCK,
        json_files=JSON_FILES,
    ).write(output or DIST_DIR / deploy_tree.DOWNLOAD_STATS_FILENAME, hostname, days=days, strict=strict)


@app.command("site-data")
def site_data_command(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output JSON path."),
    download_stats: Path | None = typer.Option(None, "--download-stats", help="Download stats JSON path."),
) -> None:
    """Write the published site data consumed by the static homepage."""
    config = load_config()
    state = load_published_state(config.component)
    reports = site_data_module.HealthReports.load_or_not_generated(
        state,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        json_files=JSON_FILES,
        clock=CLOCK,
    )
    downloads = download_stats_module.DownloadStats.load_or_empty(
        download_stats or DIST_DIR / deploy_tree.DOWNLOAD_STATS_FILENAME,
        json_files=JSON_FILES,
        days=30,
        clock=CLOCK,
    )
    site_data_module.PublishedSiteData(
        state=state,
        reports=reports,
        downloads=downloads,
    ).write(output or DIST_DIR / deploy_tree.SITE_DATA_FILENAME, JSON_FILES)


@app.command("plan-redirect-purge")
def plan_redirect_purge_command(
    output: Path = typer.Option(Path("redirect-purge-urls.txt"), "--output", "-o", help="Output file for changed package download URLs."),
    snapshot: Path | None = typer.Option(None, "--snapshot", help="New local redirect snapshot path."),
    base_url: str | None = typer.Option(None, "--base-url", help="Published repository base URL."),
    strict: bool = typer.Option(False, "--strict", help="Fail when the previous deployed snapshot cannot be fetched."),
) -> None:
    """Plan which cached package redirects should be purged after deployment."""
    config = load_config()
    redirect.plan_redirect_purge(
        output,
        snapshot or DIST_DIR / deploy_tree.REDIRECT_RULES_DIRNAME / deploy_tree.REDIRECT_SNAPSHOT_FILENAME,
        base_url or config.repository.base_url,
        redirect_rules_dirname=deploy_tree.REDIRECT_RULES_DIRNAME,
        redirect_snapshot_filename=deploy_tree.REDIRECT_SNAPSHOT_FILENAME,
        legacy_redirect_rules_paths=LEGACY_REDIRECT_RULES_PATHS,
        fetch_previous_redirect_snapshot=fetch_previous_redirect_snapshot,
        strict=strict,
    )


@app.command("purge-redirect-cache")
def purge_redirect_cache_command(
    urls: Path = typer.Option(Path("redirect-purge-urls.txt"), "--urls", help="File containing package download URLs to purge."),
    strict: bool = typer.Option(False, "--strict", help="Fail when Cloudflare cache purge cannot be completed."),
) -> None:
    """Purge changed package redirect responses from Cloudflare cache."""
    redirect.purge_redirect_cache(
        urls,
        redirect_rules_dirname=deploy_tree.REDIRECT_RULES_DIRNAME,
        resolve_cloudflare_zone_id=resolve_cloudflare_zone_id,
        purge_cloudflare_urls=purge_cloudflare_urls,
        purge_cloudflare_prefixes=purge_cloudflare_prefixes,
        strict=strict,
    )


@app.command("all")
def all_command(
    jobs: int | None = typer.Option(None, "--jobs", "-j", help="Maximum package refresh workers."),
    full_artifact_check: bool = typer.Option(False, "--full-artifact-check", help="Download and hash every locked artifact during health checks."),
) -> None:
    """Refresh state and build the deployable APT tree."""
    refresh(jobs, full_artifact_check)
    build()


def resolve_cloudflare_zone_id(token: str, hostname: str) -> str | None:
    return redirect.resolve_cloudflare_zone_id(token, hostname, fetch_json=HTTP.fetch_json)


def fetch_previous_redirect_snapshot(base_url: str, strict: bool) -> dict[str, str]:
    return redirect.fetch_previous_redirect_snapshot(
        base_url,
        redirect_rules_dirname=deploy_tree.REDIRECT_RULES_DIRNAME,
        redirect_snapshot_filename=deploy_tree.REDIRECT_SNAPSHOT_FILENAME,
        fetch_bytes=HTTP.fetch_bytes,
        strict=strict,
    )


def purge_cloudflare_urls(zone_id: str, token: str, batch: list[str]) -> None:
    redirect.purge_cloudflare_urls(zone_id, token, batch, post_json=HTTP.post_json)


def purge_cloudflare_prefixes(zone_id: str, token: str, prefixes: list[str]) -> None:
    redirect.purge_cloudflare_prefixes(zone_id, token, prefixes, post_json=HTTP.post_json)


def refresh(jobs: int | None = None, full_artifact_check: bool = False) -> None:
    try:
        config = load_configuration(ROOT)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    previous_lock = LockfileState.model_validate(JSON_FILES.load(LOCK_PATH, {"version": 2, "generated_at": None, "packages": {}}))
    previous_packages = previous_lock.packages
    locked_packages: dict[str, LockedEntry] = {}
    full_checked_artifacts: set[tuple[str, str]] = set()
    track_health: dict[str, Any] = {"version": 2, "generated_at": CLOCK.now_iso(), "packages": {}}
    package_entries = list(config.packages.items())
    candidate_resolver = sources.build_candidate_resolver(
        fetch_json=HTTP.fetch_json,
        fetch_text=HTTP.fetch_text,
        root=ROOT,
    )
    max_workers = worker_count(len(package_entries), jobs)

    logger.info("refreshing {} package entries with {} workers", len(package_entries), max_workers)
    resolved_entries: dict[str, ResolvedEntry] = {}
    errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                resolve_entry,
                entry_name,
                entry,
                previous_packages.get(entry_name),
                candidate_resolver=candidate_resolver,
            ): entry_name
            for entry_name, entry in package_entries
        }
        for future in as_completed(futures):
            entry_name = futures[future]
            try:
                resolved_entries[entry_name] = future.result()
            except Exception as exc:
                errors[entry_name] = exc

    for entry_name, _ in package_entries:
        if entry_name in resolved_entries:
            resolved = resolved_entries[entry_name]
            if resolved.entry.architectures:
                locked_packages[entry_name] = resolved.entry
            full_checked_artifacts.update((entry_name, arch) for arch in resolved.full_checked_arches)
            track_health["packages"][entry_name] = {
                "status": package_health_status(resolved.architecture_health).value,
                "architectures": {
                    arch: arch_health.to_json()
                    for arch, arch_health in resolved.architecture_health.items()
                },
            }
            continue
        if entry_name in errors:
            exc = errors[entry_name]
            if entry_name in previous_packages:
                locked_packages[entry_name] = previous_packages[entry_name]
                status = ArchitectureHealthStatus.KEPT_PREVIOUS.value
            else:
                status = ArchitectureHealthStatus.FAILED.value
            track_health["packages"][entry_name] = {"status": status, "error": str(exc), "architectures": {}}
            logger.warning("{} refresh {}: {}", entry_name, status, exc)

    generated_at = previous_lock.generated_at
    if previous_lock.version != 2 or generated_at is None or locked_packages != previous_packages:
        generated_at = CLOCK.now_iso()
    lock = LockfileState(version=2, generated_at=generated_at, packages=locked_packages).model_dump(mode="json")
    JSON_FILES.write(LOCK_PATH, lock)
    state = PublishedState.from_lock(lock, component=config.component)
    artifact_health = health.check_artifacts(
        state,
        max_workers,
        full_artifact_check=full_artifact_check,
        full_checked_artifacts=full_checked_artifacts,
        now_iso=CLOCK.now_iso,
        worker_count=worker_count,
        cache_dir=CACHE_DIR,
        user_agent=USER_AGENT,
    )
    JSON_FILES.write(TRACK_HEALTH_PATH, track_health)
    JSON_FILES.write(ARTIFACT_HEALTH_PATH, artifact_health)

    failed_architectures = [
        f"{name}:{arch}"
        for name, health in track_health["packages"].items()
        for arch, arch_health in health.get("architectures", {}).items()
        if arch_health["status"] == "failed"
    ]
    if failed_architectures:
        raise SystemExit(f"failed to resolve entry architectures: {', '.join(failed_architectures)}")


def build() -> None:
    config = load_config()
    deploy_tree.DeployableAptTree(
        config=deploy_tree.DeployConfig.model_validate(config.to_runtime_dict()),
        state=load_published_state(config.component),
        paths=deploy_tree.DeployPaths(dist_dir=DIST_DIR),
        clock=CLOCK,
        json_files=JSON_FILES,
    ).build()


def resolve_entry(
    entry_name: str,
    entry: EntryConfig,
    previous_entry: LockedEntry | None = None,
    *,
    candidate_resolver: sources.CandidateResolver,
) -> ResolvedEntry:
    logger.info("resolving {}", entry_name)
    architectures: dict[str, LockedArchitecture] = {}
    architecture_health: dict[str, ArchitectureHealth] = {}
    full_checked_arches: set[str] = set()
    for arch, architecture in entry.architectures.items():
        source_name = architecture.source.type
        update_policy = architecture.update_policy
        try:
            candidate = candidate_resolver(architecture)
            previous_architecture = previous_architecture_entry(previous_entry, arch, source_name, update_policy)
            previous_artifact = previous_architecture.artifact if previous_architecture else None
            if previous_artifact and previous_artifact.matches_candidate(candidate):
                logger.info("reusing locked artifact {}:{} {}", entry_name, arch, candidate.upstream_version)
                architectures[arch] = previous_architecture
                architecture_health[arch] = ArchitectureHealth(
                    status=ArchitectureHealthStatus.OK,
                    source=source_name,
                    update_policy=update_policy,
                )
                continue

            deb_path = deb.download(
                candidate.url,
                cache_dir=CACHE_DIR,
                user_agent=USER_AGENT,
                expected_hash=candidate.expected_hash,
                hash_algorithm=candidate.hash_algorithm,
            )
            metadata = deb.inspect_deb(deb_path)
            control = metadata["control"]
            package_arch = control.get("Architecture")
            if package_arch not in {arch, "all"}:
                raise RuntimeError(f"{entry_name}:{arch} resolved package architecture {package_arch!r}")
            artifact = LockedArtifact(
                url=candidate.url,
                upstream_version=candidate.upstream_version,
                asset_name=candidate.asset_name,
                filename=deb.safe_deb_filename(control, candidate.asset_name),
                control=control,
                size=metadata["size"],
                md5=metadata["md5"],
                sha1=metadata["sha1"],
                sha256=metadata["sha256"],
                sha512=metadata["sha512"],
            )
            architectures[arch] = locked_architecture(source_name, update_policy, artifact)
            architecture_health[arch] = ArchitectureHealth(
                status=ArchitectureHealthStatus.OK,
                source=source_name,
                update_policy=update_policy,
            )
            full_checked_arches.add(arch)
        except Exception as exc:
            previous_architecture = previous_architecture_entry(previous_entry, arch, source_name, update_policy)
            if previous_architecture:
                architectures[arch] = previous_architecture
                status = ArchitectureHealthStatus.KEPT_PREVIOUS
            else:
                status = ArchitectureHealthStatus.FAILED
            architecture_health[arch] = ArchitectureHealth(
                status=status,
                source=source_name,
                update_policy=update_policy,
                error=str(exc),
            )
            logger.warning("{}:{} refresh {}: {}", entry_name, arch, status.value, exc)

    return ResolvedEntry(
        entry=LockedEntry(homepage=entry.homepage, architectures=architectures),
        full_checked_arches=full_checked_arches,
        architecture_health=architecture_health,
    )


def locked_architecture(source_name: str, update_policy: str, artifact: LockedArtifact) -> LockedArchitecture:
    return LockedArchitecture(
        source=source_name,
        update_policy=update_policy,
        resolved_at=CLOCK.now_iso(),
        artifact=artifact,
    )


def previous_architecture_entry(
    previous_entry: LockedEntry | None,
    arch: str,
    source_name: str,
    update_policy: str,
) -> LockedArchitecture | None:
    if not previous_entry:
        return None
    previous_architecture = previous_entry.architectures.get(arch)
    if not previous_architecture:
        return None
    if (
        previous_architecture.source == source_name
        and previous_architecture.update_policy == update_policy
    ):
        return previous_architecture
    return None


def package_health_status(architecture_health: dict[str, ArchitectureHealth]) -> PackageHealthStatus:
    statuses = [health.status for health in architecture_health.values()]
    if not statuses:
        return PackageHealthStatus.FAILED
    if all(status is ArchitectureHealthStatus.OK for status in statuses):
        return PackageHealthStatus.OK
    if all(status is ArchitectureHealthStatus.FAILED for status in statuses):
        return PackageHealthStatus.FAILED
    return PackageHealthStatus.PARTIAL


def load_config() -> AptIndexConfig:
    try:
        return load_configuration(ROOT)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def load_published_state(component: str) -> PublishedState:
    lock = JSON_FILES.load(LOCK_PATH, None)
    if not lock:
        raise SystemExit("apt-index.lock.json is missing; run refresh first")
    return PublishedState.from_lock(lock, component=component)


def worker_count(total: int, requested: int | None) -> int:
    if requested is None:
        env_value = os.environ.get("APT_INDEX_JOBS")
        requested = int(env_value) if env_value else DEFAULT_JOBS
    if requested < 1:
        raise typer.BadParameter("jobs must be at least 1")
    return max(1, min(total or 1, requested))

if __name__ == "__main__":
    app()

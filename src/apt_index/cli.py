#!/usr/bin/env python3
from __future__ import annotations

import base64
import email.utils
import gzip
import html as html_module
import hashlib
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from apt_index import deb
from apt_index import download_stats as download_stats_module
from apt_index import health
from apt_index.config import ConfigError, EntryConfig, load_configuration
from apt_index import publish
from apt_index import redirect
from apt_index import sources
from apt_index.paths import (
    ARTIFACT_HEALTH_PATH,
    CACHE_DIR,
    DIST_DIR,
    ENV_PATH,
    GNUPG_DIR,
    LOCK_PATH,
    ROOT,
    STATIC_DIR,
    TRACK_HEALTH_PATH,
    WORKER_SCRIPT_PATH,
)
from apt_index import site_data as site_data_module

DOWNLOAD_STATS_FILENAME = "download_stats.json"
SITE_DATA_FILENAME = "site-data.json"
REDIRECT_RULES_DIRNAME = "redirect-rules"
REDIRECT_SNAPSHOT_FILENAME = "snapshot.json.zst"
SITE_DATA_TTL_SECONDS = 60 * 5
STATIC_REDIRECT_RULES_EDGE_TTL_SECONDS = 60 * 60 * 24 * 365
STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY = "public, max-age=0, must-revalidate"
STATIC_REDIRECT_RULES_CDN_TTL_POLICY = (
    f"public, max-age={STATIC_REDIRECT_RULES_EDGE_TTL_SECONDS}, stale-while-revalidate=86400, stale-if-error=604800"
)
SITE_DATA_BROWSER_TTL_POLICY = f"public, max-age={SITE_DATA_TTL_SECONDS}, must-revalidate"
SITE_DATA_CDN_TTL_POLICY = f"public, max-age={SITE_DATA_TTL_SECONDS}"
LEGACY_REDIRECT_RULES_PATHS = ("/redirect_rules.json",)
CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS = 7
USER_AGENT = "apt-index/0.1"
DEFAULT_JOBS = 4
STATIC_ASSET_FILENAMES = ("logo.webp",)
SIGNING_PRIVATE_KEY_ENV = "APT_INDEX_GPG_PRIVATE_KEY"
SIGNING_PRIVATE_KEY_B64_ENV = "APT_INDEX_GPG_PRIVATE_KEY_B64"
SIGNING_PASSPHRASE_ENV = "APT_INDEX_GPG_PASSPHRASE"
DOTENV_LOADED = False
app = typer.Typer(no_args_is_help=True)


@dataclass(frozen=True)
class ResolvedEntry:
    entry: dict[str, Any]
    full_checked_arches: set[str]
    architecture_health: dict[str, Any]


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
    if hostname is None:
        hostname = urllib.parse.urlparse(load_config()["repository"]["base_url"]).hostname
    download_stats_module.write_download_stats(
        output or DIST_DIR / DOWNLOAD_STATS_FILENAME,
        hostname,
        days=days,
        strict=strict,
        load_json=load_json,
        load_config=load_config,
        write_json=write_json,
        empty_download_stats=lambda reason, stats_days: download_stats_module.empty_download_stats(reason, stats_days, now_iso),
        resolve_cloudflare_zone_id=lambda token, host: redirect.resolve_cloudflare_zone_id(token, host, fetch_json=fetch_json),
        fetch_download_stats=lambda zone_id, token, stats_hostname, stats_days, path_index: download_stats_module.fetch_download_stats(
            zone_id,
            token,
            stats_hostname,
            days=stats_days,
            path_index=path_index,
            max_days=CLOUDFLARE_HTTP_ANALYTICS_MAX_DAYS,
            cloudflare_graphql=lambda graphql_token, query, variables: download_stats_module.cloudflare_graphql(
                graphql_token,
                query,
                variables,
                post_json=post_json,
            ),
            now=lambda: datetime.now(timezone.utc),
            now_iso=now_iso,
            graphql_time=graphql_time,
        ),
        lock_path=LOCK_PATH,
        package_virtual_path=package_virtual_path,
    )


@app.command("site-data")
def site_data_command(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output JSON path."),
    download_stats: Path | None = typer.Option(None, "--download-stats", help="Download stats JSON path."),
) -> None:
    """Write the published site data consumed by the static homepage."""
    site_data_module.write_site_data(
        output or DIST_DIR / SITE_DATA_FILENAME,
        download_stats or DIST_DIR / DOWNLOAD_STATS_FILENAME,
        lock_path=LOCK_PATH,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        load_json=load_json,
        write_json=write_json,
        empty_download_stats=lambda reason: download_stats_module.empty_download_stats(reason, 30, now_iso),
        now_iso=now_iso,
    )


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
        snapshot or DIST_DIR / REDIRECT_RULES_DIRNAME / REDIRECT_SNAPSHOT_FILENAME,
        base_url or config["repository"]["base_url"],
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        redirect_snapshot_filename=REDIRECT_SNAPSHOT_FILENAME,
        legacy_redirect_rules_paths=LEGACY_REDIRECT_RULES_PATHS,
        fetch_previous_redirect_snapshot=lambda snapshot_base_url, snapshot_strict: redirect.fetch_previous_redirect_snapshot(
            snapshot_base_url,
            redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
            redirect_snapshot_filename=REDIRECT_SNAPSHOT_FILENAME,
            fetch_bytes=fetch_bytes,
            strict=snapshot_strict,
        ),
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
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        resolve_cloudflare_zone_id=lambda token, host: redirect.resolve_cloudflare_zone_id(token, host, fetch_json=fetch_json),
        purge_cloudflare_urls=lambda zone_id, token, batch: redirect.purge_cloudflare_urls(zone_id, token, batch, post_json=post_json),
        purge_cloudflare_prefixes=lambda zone_id, token, prefixes: redirect.purge_cloudflare_prefixes(zone_id, token, prefixes, post_json=post_json),
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


def refresh(jobs: int | None = None, full_artifact_check: bool = False) -> None:
    try:
        config = load_configuration(ROOT)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    previous_lock = load_json(LOCK_PATH, {"version": 2, "generated_at": None, "packages": {}})
    previous_packages = previous_lock.get("packages", {})
    locked_packages: dict[str, Any] = {}
    full_checked_artifacts: set[tuple[str, str]] = set()
    track_health: dict[str, Any] = {"version": 2, "generated_at": now_iso(), "packages": {}}
    package_entries = list(config.packages.items())
    candidate_resolver = sources.build_candidate_resolver(
        fetch_json=fetch_json,
        fetch_text=fetch_text,
        root=ROOT,
    )
    max_workers = worker_count(len(package_entries), jobs)

    logger.info("refreshing {} package entries with {} workers", len(package_entries), max_workers)
    resolved_entries: dict[str, Any] = {}
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
            if resolved.entry["architectures"]:
                locked_packages[entry_name] = resolved.entry
            full_checked_artifacts.update((entry_name, arch) for arch in resolved.full_checked_arches)
            track_health["packages"][entry_name] = {
                "status": package_health_status(resolved.architecture_health),
                "architectures": resolved.architecture_health,
            }
            continue
        if entry_name in errors:
            exc = errors[entry_name]
            if entry_name in previous_packages:
                locked_packages[entry_name] = previous_packages[entry_name]
                status = "kept_previous"
            else:
                status = "failed"
            track_health["packages"][entry_name] = {"status": status, "error": str(exc), "architectures": {}}
            logger.warning("{} refresh {}: {}", entry_name, status, exc)

    generated_at = previous_lock.get("generated_at")
    if previous_lock.get("version") != 2 or generated_at is None or locked_packages != previous_packages:
        generated_at = now_iso()
    lock = {"version": 2, "generated_at": generated_at, "packages": locked_packages}
    write_json(LOCK_PATH, lock)
    artifact_health = health.check_artifacts(
        lock,
        max_workers,
        full_artifact_check=full_artifact_check,
        full_checked_artifacts=full_checked_artifacts,
        now_iso=now_iso,
        worker_count=worker_count,
        cache_dir=CACHE_DIR,
        user_agent=USER_AGENT,
    )
    write_json(TRACK_HEALTH_PATH, track_health)
    write_json(ARTIFACT_HEALTH_PATH, artifact_health)

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
    lock = load_json(LOCK_PATH, None)
    if not lock:
        raise SystemExit("apt-index.lock.json is missing; run refresh first")

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    suite = config["suite"]
    component = config["component"]
    archs = lock_architectures(lock)

    for arch in archs:
        package_stanzas: list[str] = []
        for entry_name, entry in lock["packages"].items():
            artifact = entry.get("architectures", {}).get(arch, {}).get("artifact")
            if not artifact:
                continue
            virtual_path = package_virtual_path(component, entry_name, artifact["filename"])
            stanza = dict(artifact["control"])
            stanza["Filename"] = virtual_path
            stanza["Size"] = str(artifact["size"])
            stanza["MD5sum"] = artifact["md5"]
            stanza["SHA1"] = artifact["sha1"]
            stanza["SHA256"] = artifact["sha256"]
            package_stanzas.append(deb.format_control(stanza))

        packages_dir = DIST_DIR / "dists" / suite / component / f"binary-{arch}"
        packages_dir.mkdir(parents=True, exist_ok=True)
        packages_text = "\n".join(package_stanzas)
        (packages_dir / "Packages").write_text(packages_text, encoding="utf-8")
        with gzip.open(packages_dir / "Packages.gz", "wb", compresslevel=9) as f:
            f.write(packages_text.encode("utf-8"))

    redirect.write_redirect_rules(
        lock,
        component,
        dist_dir=DIST_DIR,
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        redirect_snapshot_filename=REDIRECT_SNAPSHOT_FILENAME,
        write_json=write_json,
        package_virtual_path=package_virtual_path,
    )
    write_json(DIST_DIR / DOWNLOAD_STATS_FILENAME, download_stats_module.empty_download_stats("not_generated", 30, now_iso))
    (DIST_DIR / "key.asc").write_text(ensure_signing_key(config), encoding="utf-8")
    site_data_module.copy_state_files(
        lock,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        dist_dir=DIST_DIR,
        write_json=write_json,
        now_iso=now_iso,
    )
    site_data_module.write_site_data(
        DIST_DIR / SITE_DATA_FILENAME,
        DIST_DIR / DOWNLOAD_STATS_FILENAME,
        lock_path=LOCK_PATH,
        track_health_path=TRACK_HEALTH_PATH,
        artifact_health_path=ARTIFACT_HEALTH_PATH,
        load_json=load_json,
        write_json=write_json,
        empty_download_stats=lambda reason: download_stats_module.empty_download_stats(reason, 30, now_iso),
        now_iso=now_iso,
    )
    publish.write_headers(
        DIST_DIR / "_headers",
        site_data_filename=SITE_DATA_FILENAME,
        site_data_browser_ttl_policy=SITE_DATA_BROWSER_TTL_POLICY,
        site_data_cdn_ttl_policy=SITE_DATA_CDN_TTL_POLICY,
        redirect_rules_dirname=REDIRECT_RULES_DIRNAME,
        static_redirect_rules_browser_ttl_policy=STATIC_REDIRECT_RULES_BROWSER_TTL_POLICY,
        static_redirect_rules_cdn_ttl_policy=STATIC_REDIRECT_RULES_CDN_TTL_POLICY,
    )
    publish.write_worker(DIST_DIR / "_worker.js", WORKER_SCRIPT_PATH)
    publish.write_routes(DIST_DIR / "_routes.json")
    write_index_page(config, lock)
    write_release(config, archs)
    sign_release(config)
    logger.info("built deployable tree at {}", DIST_DIR)


def resolve_entry(
    entry_name: str,
    entry: EntryConfig,
    previous_entry: dict[str, Any] | None = None,
    *,
    candidate_resolver: sources.CandidateResolver,
) -> ResolvedEntry:
    logger.info("resolving {}", entry_name)
    architectures: dict[str, Any] = {}
    architecture_health: dict[str, Any] = {}
    full_checked_arches: set[str] = set()
    for arch, architecture in entry.architectures.items():
        source_name = architecture.source.type
        update_policy = architecture.update_policy
        try:
            candidate = candidate_resolver(architecture)
            previous_architecture = previous_architecture_entry(previous_entry, arch, source_name, update_policy)
            previous_artifact = (previous_architecture or {}).get("artifact")
            if previous_artifact and artifact_matches_candidate(previous_artifact, candidate):
                logger.info("reusing locked artifact {}:{} {}", entry_name, arch, candidate.upstream_version)
                architectures[arch] = previous_architecture
                architecture_health[arch] = {
                    "status": "ok",
                    "source": source_name,
                    "update_policy": update_policy,
                }
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
            artifact = {
                "url": candidate.url,
                "upstream_version": candidate.upstream_version,
                "asset_name": candidate.asset_name,
                "filename": deb.safe_deb_filename(control, candidate.asset_name),
                "control": control,
                "size": metadata["size"],
                "md5": metadata["md5"],
                "sha1": metadata["sha1"],
                "sha256": metadata["sha256"],
                "sha512": metadata["sha512"],
            }
            architectures[arch] = locked_architecture(source_name, update_policy, artifact)
            architecture_health[arch] = {
                "status": "ok",
                "source": source_name,
                "update_policy": update_policy,
            }
            full_checked_arches.add(arch)
        except Exception as exc:
            previous_architecture = previous_architecture_entry(previous_entry, arch, source_name, update_policy)
            if previous_architecture:
                architectures[arch] = previous_architecture
                status = "kept_previous"
            else:
                status = "failed"
            architecture_health[arch] = {
                "status": status,
                "source": source_name,
                "update_policy": update_policy,
                "error": str(exc),
            }
            logger.warning("{}:{} refresh {}: {}", entry_name, arch, status, exc)

    return ResolvedEntry(
        {
            "homepage": entry.homepage,
            "architectures": architectures,
        },
        full_checked_arches,
        architecture_health,
    )


def locked_architecture(source_name: str, update_policy: str, artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source_name,
        "update_policy": update_policy,
        "resolved_at": now_iso(),
        "artifact": artifact,
    }


def previous_architecture_entry(
    previous_entry: dict[str, Any] | None,
    arch: str,
    source_name: str,
    update_policy: str,
) -> dict[str, Any] | None:
    if not previous_entry:
        return None
    if "architectures" in previous_entry:
        previous_architecture = previous_entry["architectures"].get(arch)
        if not previous_architecture:
            return None
        if (
            previous_architecture.get("source") == source_name
            and previous_architecture.get("update_policy") == update_policy
        ):
            return previous_architecture
        return None

    artifact = previous_entry.get("artifacts", {}).get(arch)
    if not artifact:
        return None
    return locked_architecture(source_name, update_policy, artifact)


def package_health_status(architecture_health: dict[str, Any]) -> str:
    statuses = [health["status"] for health in architecture_health.values()]
    if not statuses:
        return "failed"
    if all(status == "ok" for status in statuses):
        return "ok"
    if all(status == "failed" for status in statuses):
        return "failed"
    return "partial"


def lock_architectures(lock: dict[str, Any]) -> list[str]:
    return sorted(
        {
            arch
            for entry in lock["packages"].values()
            for arch, architecture in entry.get("architectures", {}).items()
            if architecture.get("artifact")
        }
    )


def package_virtual_path(component: str, entry_name: str, filename: str) -> str:
    return f"pool/{component}/{entry_name}/{filename}"


def artifact_matches_candidate(artifact: dict[str, Any], candidate: sources.ArtifactCandidate) -> bool:
    if not (
        artifact.get("url") == candidate.url
        and artifact.get("upstream_version") == candidate.upstream_version
        and artifact.get("asset_name") == candidate.asset_name
    ):
        return False
    if candidate.expected_hash is None:
        return True
    return artifact.get(candidate.hash_algorithm) == candidate.expected_hash


def write_release(config: dict[str, Any], archs: list[str]) -> None:
    suite = config["suite"]
    component = config["component"]
    release_path = DIST_DIR / "dists" / suite / "Release"
    files = []
    for path in sorted((DIST_DIR / "dists" / suite).rglob("*")):
        if path.is_file() and path.name != "Release":
            rel = path.relative_to(DIST_DIR / "dists" / suite).as_posix()
            data = path.read_bytes()
            files.append((rel, len(data), hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest()))

    sections = {
        "MD5Sum": [(md5, size, rel) for rel, size, md5, _, _ in files],
        "SHA1": [(sha1, size, rel) for rel, size, _, sha1, _ in files],
        "SHA256": [(sha256, size, rel) for rel, size, _, _, sha256 in files],
    }
    repo = config["repository"]
    lines = [
        f"Origin: {repo['origin']}",
        f"Label: {repo['label']}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)}",
        f"Architectures: {' '.join(archs)}",
        f"Components: {component}",
        f"Description: {repo['description']}",
    ]
    for name, values in sections.items():
        lines.append(f"{name}:")
        lines.extend(f" {digest} {size:16d} {rel}" for digest, size, rel in values)
    release_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_dotenv() -> None:
    global DOTENV_LOADED
    if DOTENV_LOADED:
        return
    DOTENV_LOADED = True
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        os.environ[name] = value


def has_secret_key(key_name: str, env: dict[str, str]) -> bool:
    list_result = subprocess.run(["gpg", "--list-secret-keys", key_name], env=env, text=True, capture_output=True)
    return list_result.returncode == 0


def import_signing_key_from_env(env: dict[str, str]) -> None:
    key_material = os.environ.get(SIGNING_PRIVATE_KEY_ENV)
    key_material_b64 = os.environ.get(SIGNING_PRIVATE_KEY_B64_ENV)
    if not key_material and key_material_b64:
        key_material = base64.b64decode(key_material_b64).decode("utf-8")
    if not key_material:
        raise RuntimeError(
            "missing signing private key; set "
            f"{SIGNING_PRIVATE_KEY_B64_ENV} or {SIGNING_PRIVATE_KEY_ENV} before running apt-index build"
        )

    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            os.environ.get(SIGNING_PASSPHRASE_ENV, ""),
            "--import",
        ],
        input=key_material,
        env=env,
        text=True,
        check=True,
    )


def ensure_signing_key(config: dict[str, Any]) -> str:
    load_dotenv()
    GNUPG_DIR.mkdir(mode=0o700, exist_ok=True)
    key_name = config["signing"]["key_name"]
    env = gpg_env()
    if not has_secret_key(key_name, env):
        import_signing_key_from_env(env)
    if not has_secret_key(key_name, env):
        raise RuntimeError(f"imported signing key does not contain secret key for {key_name!r}")
    result = subprocess.run(["gpg", "--armor", "--export", key_name], env=env, check=True, text=True, capture_output=True)
    return result.stdout


def sign_release(config: dict[str, Any]) -> None:
    ensure_signing_key(config)
    key_name = config["signing"]["key_name"]
    release_path = DIST_DIR / "dists" / config["suite"] / "Release"
    inrelease_path = release_path.parent / "InRelease"
    release_gpg_path = release_path.parent / "Release.gpg"
    env = gpg_env()
    passphrase = os.environ.get(SIGNING_PASSPHRASE_ENV, "")
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            passphrase,
            "--local-user",
            key_name,
            "--clearsign",
            "--digest-algo",
            "SHA256",
            "--output",
            str(inrelease_path),
            str(release_path),
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            passphrase,
            "--local-user",
            key_name,
            "--detach-sign",
            "--armor",
            "--digest-algo",
            "SHA256",
            "--output",
            str(release_gpg_path),
            str(release_path),
        ],
        env=env,
        check=True,
    )


def write_index_page(config: dict[str, Any], lock: dict[str, Any]) -> None:
    base_url = config["repository"]["base_url"]
    suite = html_module.escape(config["suite"])
    component = html_module.escape(config["component"])
    package_index_links = "\n                ".join(
        f'<a href="/dists/{suite}/{component}/binary-{html_module.escape(arch)}/Packages">Packages {html_module.escape(arch)}</a>'
        for arch in lock_architectures(lock)
    )
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = (
        template
        .replace("__BASE_URL__", base_url)
        .replace("__SUITE__", config["suite"])
        .replace("__COMPONENT__", config["component"])
        .replace("__PACKAGE_INDEX_LINKS__", package_index_links)
    )
    (DIST_DIR / "index.html").write_text(html, encoding="utf-8")
    for filename in STATIC_ASSET_FILENAMES:
        shutil.copy2(STATIC_DIR / filename, DIST_DIR / filename)


def load_config() -> dict[str, Any]:
    try:
        return load_configuration(ROOT).to_runtime_dict()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return json.loads(fetch_text(url, headers))


def fetch_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc


def fetch_text(url: str, headers: dict[str, str] | None = None) -> str:
    return fetch_bytes(url, headers).decode("utf-8")


def post_json(url: str, payload: Any, headers: dict[str, str] | None = None) -> Any:
    return json.loads(post_text(url, json.dumps(payload), headers))


def post_text(url: str, body: str, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: HTTP {exc.code}: {response_body}") from exc


def gpg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(GNUPG_DIR)
    return env


def worker_count(total: int, requested: int | None) -> int:
    if requested is None:
        env_value = os.environ.get("APT_INDEX_JOBS")
        requested = int(env_value) if env_value else DEFAULT_JOBS
    if requested < 1:
        raise typer.BadParameter("jobs must be at least 1")
    return max(1, min(total or 1, requested))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def graphql_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    app()

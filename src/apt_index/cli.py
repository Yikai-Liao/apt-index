#!/usr/bin/env python3
from __future__ import annotations

import base64
import email.utils
import fnmatch
import gzip
import html as html_module
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from apt_index.config import ConfigError, load_configuration

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "apt-index.toml").exists():
    ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "apt-index.lock.json"
TRACK_HEALTH_PATH = ROOT / "track_health.json"
ARTIFACT_HEALTH_PATH = ROOT / "artifact_health.json"
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / ".apt-index-cache"
DIST_DIR = ROOT / "dist"
DOWNLOAD_STATS_FILENAME = "download_stats.json"
REDIRECT_RULES_DIRNAME = "redirect-rules"
REDIRECT_SNAPSHOT_FILENAME = "snapshot.json.zst"
REDIRECT_EDGE_TTL_SECONDS = 60 * 60 * 24 * 30
REDIRECT_BROWSER_TTL_SECONDS = 60 * 5
LEGACY_REDIRECT_RULES_PATHS = ("/redirect_rules.json",)
GNUPG_DIR = ROOT / ".apt-index-gnupg"
ENV_PATH = ROOT / ".env"
USER_AGENT = "apt-index/0.1"
DEFAULT_JOBS = 4
SIGNING_PRIVATE_KEY_ENV = "APT_INDEX_GPG_PRIVATE_KEY"
SIGNING_PRIVATE_KEY_B64_ENV = "APT_INDEX_GPG_PRIVATE_KEY_B64"
SIGNING_PASSPHRASE_ENV = "APT_INDEX_GPG_PASSPHRASE"
DOTENV_LOADED = False
app = typer.Typer(no_args_is_help=True)


@dataclass(frozen=True)
class ArtifactCandidate:
    url: str
    upstream_version: str
    asset_name: str
    expected_hash: str | None = None
    hash_algorithm: str = "sha256"


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
    write_download_stats(output or DIST_DIR / DOWNLOAD_STATS_FILENAME, hostname, days, strict)


@app.command("plan-redirect-purge")
def plan_redirect_purge_command(
    output: Path = typer.Option(Path("redirect-purge-urls.txt"), "--output", "-o", help="Output file for changed package download URLs."),
    snapshot: Path | None = typer.Option(None, "--snapshot", help="New local redirect snapshot path."),
    base_url: str | None = typer.Option(None, "--base-url", help="Published repository base URL."),
    strict: bool = typer.Option(False, "--strict", help="Fail when the previous deployed snapshot cannot be fetched."),
) -> None:
    """Plan which cached package redirects should be purged after deployment."""
    config = load_config()
    plan_redirect_purge(
        output,
        snapshot or DIST_DIR / REDIRECT_RULES_DIRNAME / REDIRECT_SNAPSHOT_FILENAME,
        base_url or config["repository"]["base_url"],
        strict,
    )


@app.command("purge-redirect-cache")
def purge_redirect_cache_command(
    urls: Path = typer.Option(Path("redirect-purge-urls.txt"), "--urls", help="File containing package download URLs to purge."),
    strict: bool = typer.Option(False, "--strict", help="Fail when Cloudflare cache purge cannot be completed."),
) -> None:
    """Purge changed package redirect responses from Cloudflare cache."""
    purge_redirect_cache(urls, strict)


@app.command("all")
def all_command(
    jobs: int | None = typer.Option(None, "--jobs", "-j", help="Maximum package refresh workers."),
    full_artifact_check: bool = typer.Option(False, "--full-artifact-check", help="Download and hash every locked artifact during health checks."),
) -> None:
    """Refresh state and build the deployable APT tree."""
    refresh(jobs, full_artifact_check)
    build()


def refresh(jobs: int | None = None, full_artifact_check: bool = False) -> None:
    config = load_config()
    lock = load_json(LOCK_PATH, {"version": 2, "generated_at": None, "packages": {}})
    previous_packages = lock.get("packages", {})
    locked_packages: dict[str, Any] = {}
    full_checked_artifacts: set[tuple[str, str]] = set()
    track_health: dict[str, Any] = {"version": 2, "generated_at": now_iso(), "packages": {}}
    package_entries = list(config["packages"].items())
    max_workers = worker_count(len(package_entries), jobs)

    logger.info("refreshing {} package entries with {} workers", len(package_entries), max_workers)
    resolved_entries: dict[str, Any] = {}
    errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(resolve_entry, entry_name, entry, previous_packages.get(entry_name)): entry_name
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

    lock = {"version": 2, "generated_at": now_iso(), "packages": locked_packages}
    write_json(LOCK_PATH, lock)
    artifact_health = check_artifacts(lock, max_workers, full_artifact_check, full_checked_artifacts)
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
            package_stanzas.append(format_control(stanza))

        packages_dir = DIST_DIR / "dists" / suite / component / f"binary-{arch}"
        packages_dir.mkdir(parents=True, exist_ok=True)
        packages_text = "\n".join(package_stanzas)
        (packages_dir / "Packages").write_text(packages_text, encoding="utf-8")
        with gzip.open(packages_dir / "Packages.gz", "wb", compresslevel=9) as f:
            f.write(packages_text.encode("utf-8"))

    write_redirect_rules(lock, component)
    write_json(DIST_DIR / DOWNLOAD_STATS_FILENAME, empty_download_stats("not_generated"))
    (DIST_DIR / "key.asc").write_text(ensure_signing_key(config), encoding="utf-8")
    copy_state_files(lock)
    write_worker(DIST_DIR / "_worker.js")
    write_routes(DIST_DIR / "_routes.json")
    write_index_page(config, lock)
    write_release(config, archs)
    sign_release(config)
    logger.info("built deployable tree at {}", DIST_DIR)


def resolve_entry(entry_name: str, entry: dict[str, Any], previous_entry: dict[str, Any] | None = None) -> ResolvedEntry:
    logger.info("resolving {}", entry_name)
    architectures: dict[str, Any] = {}
    architecture_health: dict[str, Any] = {}
    full_checked_arches: set[str] = set()
    for arch, architecture in entry["architectures"].items():
        source_name = architecture["source"]["type"]
        update_policy = architecture["update_policy"]
        try:
            candidate = resolve_candidate(architecture)
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

            deb_path = download(candidate.url, candidate.expected_hash, candidate.hash_algorithm)
            metadata = inspect_deb(deb_path)
            control = metadata["control"]
            package_arch = control.get("Architecture")
            if package_arch not in {arch, "all"}:
                raise RuntimeError(f"{entry_name}:{arch} resolved package architecture {package_arch!r}")
            artifact = {
                "url": candidate.url,
                "upstream_version": candidate.upstream_version,
                "asset_name": candidate.asset_name,
                "filename": safe_deb_filename(control, candidate.asset_name),
                "control": control,
                "size": metadata["size"],
                "md5": metadata["md5"],
                "sha1": metadata["sha1"],
                "sha256": metadata["sha256"],
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
            "homepage": entry.get("homepage"),
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


def redirect_maps(lock: dict[str, Any], component: str) -> tuple[dict[str, str], dict[tuple[str, str], dict[str, str]]]:
    redirects: dict[str, str] = {}
    shards: dict[tuple[str, str], dict[str, str]] = {}
    for entry_name, entry in lock["packages"].items():
        shard: dict[str, str] = {}
        for architecture in entry.get("architectures", {}).values():
            artifact = architecture.get("artifact")
            if not artifact:
                continue
            filename = artifact["filename"]
            target = artifact["url"]
            virtual_path = "/" + package_virtual_path(component, entry_name, filename)
            existing_target = redirects.get(virtual_path)
            if existing_target and existing_target != target:
                raise RuntimeError(f"conflicting redirect target for {virtual_path}")
            existing_filename_target = shard.get(filename)
            if existing_filename_target and existing_filename_target != target:
                raise RuntimeError(f"conflicting redirect target for {entry_name}/{filename}")
            redirects[virtual_path] = target
            shard[filename] = target
        if shard:
            shards[(component, entry_name)] = dict(sorted(shard.items()))
    return dict(sorted(redirects.items())), shards


def write_redirect_rules(lock: dict[str, Any], component: str) -> dict[str, str]:
    redirects, shards = redirect_maps(lock, component)
    redirect_dir = DIST_DIR / REDIRECT_RULES_DIRNAME
    for (shard_component, entry_name), shard in shards.items():
        shard_path = redirect_dir / shard_component / f"{entry_name}.json"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(shard_path, shard)
    write_redirect_snapshot(redirect_dir / REDIRECT_SNAPSHOT_FILENAME, redirects)
    return redirects


def write_redirect_snapshot(path: Path, redirects: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "redirects": redirects}, indent=2, sort_keys=True) + "\n"
    path.write_bytes(zstd_compress(payload.encode("utf-8")))


def read_redirect_snapshot(path: Path) -> dict[str, str]:
    payload = json.loads(zstd_decompress(path.read_bytes()).decode("utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("redirects"), dict):
        raise RuntimeError(f"{path}: unsupported redirect snapshot format")
    return {str(key): str(value) for key, value in payload["redirects"].items()}


def zstd_compress(data: bytes) -> bytes:
    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError("zstd is required to write redirect snapshots")
    result = subprocess.run([zstd, "-q", "-19", "-c"], input=data, check=True, capture_output=True)
    return result.stdout


def zstd_decompress(data: bytes) -> bytes:
    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError("zstd is required to read redirect snapshots")
    result = subprocess.run([zstd, "-d", "-q", "-c"], input=data, check=True, capture_output=True)
    return result.stdout


def plan_redirect_purge(output: Path, snapshot: Path, base_url: str, strict: bool = False) -> list[str]:
    base_url = base_url.rstrip("/")
    new_redirects = read_redirect_snapshot(snapshot)
    old_redirects = fetch_previous_redirect_snapshot(base_url, strict)
    purge_paths = {
        path
        for path, old_target in old_redirects.items()
        if new_redirects.get(path) != old_target
    }
    purge_paths.update(LEGACY_REDIRECT_RULES_PATHS)
    urls = [base_url + path for path in sorted(purge_paths)]
    output.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    logger.info("planned {} redirect cache purge URLs at {}", len(urls), output)
    return urls


def fetch_previous_redirect_snapshot(base_url: str, strict: bool = False) -> dict[str, str]:
    snapshot_url = f"{base_url.rstrip('/')}/{REDIRECT_RULES_DIRNAME}/{REDIRECT_SNAPSHOT_FILENAME}"
    cache_bust = os.environ.get("GITHUB_RUN_ID") or str(int(datetime.now(timezone.utc).timestamp()))
    separator = "&" if "?" in snapshot_url else "?"
    snapshot_url = f"{snapshot_url}{separator}run={cache_bust}"
    try:
        data = fetch_bytes(snapshot_url, {"Cache-Control": "no-cache"})
        payload = json.loads(zstd_decompress(data).decode("utf-8"))
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        if strict:
            raise
        logger.warning("previous redirect snapshot unavailable; treating as first deploy: {}", exc)
        return {}
    if payload.get("version") != 1 or not isinstance(payload.get("redirects"), dict):
        raise RuntimeError(f"{snapshot_url}: unsupported redirect snapshot format")
    return {str(key): str(value) for key, value in payload["redirects"].items()}


def purge_redirect_cache(urls_path: Path, strict: bool = False) -> None:
    urls = [line.strip() for line in urls_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not urls:
        logger.info("no redirect cache URLs to purge")
        return
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    hostname = urllib.parse.urlparse(urls[0]).hostname
    if not token or not hostname:
        message = "CLOUDFLARE_API_TOKEN and purge URL hostname are required to purge redirect cache"
        if strict:
            raise RuntimeError(message)
        logger.warning("{}; skipping {} redirect cache purge URLs", message, len(urls))
        return
    try:
        zone_id = resolve_cloudflare_zone_id(token, hostname)
        if not zone_id:
            raise RuntimeError(f"could not resolve Cloudflare zone for {hostname!r}")
        for batch in batched(urls, 30):
            purge_cloudflare_urls(zone_id, token, batch)
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        if strict:
            raise
        logger.warning("Cloudflare redirect cache purge failed; skipping {} URLs: {}", len(urls), exc)
        return
    logger.info("purged {} redirect cache URLs", len(urls))


def resolve_cloudflare_zone_id(token: str, hostname: str) -> str | None:
    configured_zone_id = os.environ.get("CLOUDFLARE_ZONE_ID")
    if configured_zone_id:
        return configured_zone_id

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    for zone_name in cloudflare_zone_name_candidates(hostname):
        query = urllib.parse.urlencode({"name": zone_name})
        payload = fetch_json(f"https://api.cloudflare.com/client/v4/zones?{query}", headers)
        if not payload.get("success"):
            raise RuntimeError(f"Cloudflare zone lookup failed: {payload.get('errors')!r}")
        for zone in payload.get("result", []):
            if zone.get("name") == zone_name and (hostname == zone_name or hostname.endswith("." + zone_name)):
                zone_id = zone.get("id")
                if zone_id:
                    return str(zone_id)
    return None


def cloudflare_zone_name_candidates(hostname: str) -> list[str]:
    labels = [label for label in hostname.lower().strip(".").split(".") if label]
    return [
        ".".join(labels[index:])
        for index in range(max(len(labels) - 1, 0))
        if len(labels[index:]) >= 2
    ]


def purge_cloudflare_urls(zone_id: str, token: str, urls: list[str]) -> None:
    payload = {"files": urls}
    response = post_json(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache",
        payload,
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    if not response.get("success"):
        raise RuntimeError(f"Cloudflare cache purge failed: {response!r}")


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def artifact_matches_candidate(artifact: dict[str, Any], candidate: ArtifactCandidate) -> bool:
    return (
        artifact.get("url") == candidate.url
        and artifact.get("upstream_version") == candidate.upstream_version
        and artifact.get("asset_name") == candidate.asset_name
    )


def resolve_candidate(architecture: dict[str, Any]) -> ArtifactCandidate:
    source_config = architecture["source"]
    source = source_config["type"]
    if source == "github":
        release = github_release(source_config, architecture["update_policy"])
        pattern = source_config["asset_pattern"]
        for asset in release.get("assets", []):
            name = asset["name"]
            if fnmatch.fnmatch(name, pattern):
                return ArtifactCandidate(asset["browser_download_url"], release["tag_name"], name)
        raise RuntimeError(f"no GitHub asset matched {pattern!r}")
    if source == "aur":
        srcinfo = fetch_text(f"https://aur.archlinux.org/cgit/aur.git/plain/.SRCINFO?h={source_config['package']}")
        fields = parse_srcinfo(srcinfo)
        source_key, source_index, source_value = select_aur_source(fields, source_config["asset_pattern"])
        checksum_algorithm, checksum = aur_checksum_for(fields, source_key, source_index)
        asset_name, artifact_url = split_aur_source(source_value)
        return ArtifactCandidate(artifact_url, first_srcinfo_value(fields, "pkgver", "unknown"), asset_name, checksum, checksum_algorithm)
    if source == "url":
        url = source_config["url"]
        return ArtifactCandidate(url, "fixed", Path(url).name)
    raise RuntimeError(f"unsupported source resolver {source!r}")


def github_release(source_config: dict[str, Any], update_policy: str) -> dict[str, Any]:
    repo = source_config["repo"]
    if update_policy == "fixed":
        path = f"repos/{repo}/releases/tags/{source_config['release_tag']}"
    else:
        path = f"repos/{repo}/releases/latest"

    headers = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return fetch_json(f"https://api.github.com/{path}", headers=headers)

    gh = shutil.which("gh")
    if gh:
        result = subprocess.run([gh, "api", path], cwd=ROOT, check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    return fetch_json(f"https://api.github.com/{path}", headers=headers)


def parse_srcinfo(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields.setdefault(key.strip(), []).append(value.strip())
    return fields


def first_srcinfo_value(fields: dict[str, list[str]], key: str, default: str = "") -> str:
    values = fields.get(key)
    return values[0] if values else default


def select_aur_source(fields: dict[str, list[str]], asset_pattern: str) -> tuple[str, int, str]:
    for key, values in fields.items():
        if key != "source" and not key.startswith("source_"):
            continue
        for index, value in enumerate(values):
            asset_name, url = split_aur_source(value)
            if aur_source_matches(asset_pattern, value, asset_name, url):
                return key, index, value
    raise RuntimeError(f"no AUR source matched {asset_pattern!r}")


def aur_source_matches(pattern: str, raw_value: str, asset_name: str, url: str) -> bool:
    return any(
        fnmatch.fnmatch(value, pattern)
        for value in (asset_name, url, raw_value)
    )


def aur_checksum_for(
    fields: dict[str, list[str]],
    source_key: str,
    source_index: int,
) -> tuple[str, str | None]:
    suffix = source_key.removeprefix("source")
    checksum_source_keys = [f"{algorithm}sums{suffix}" for algorithm in ("sha256", "sha512")]
    for checksum_key in checksum_source_keys:
        values = fields.get(checksum_key, [])
        if source_index < len(values) and values[source_index] != "SKIP":
            return checksum_key.split("sums", 1)[0], values[source_index]
    return "sha256", None


def split_aur_source(value: str) -> tuple[str, str]:
    if "::" in value:
        asset_name, url = value.split("::", 1)
        return asset_name, url
    return Path(value).name, value


def download(url: str, expected_hash: str | None = None, hash_algorithm: str = "sha256") -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"{cache_key}.deb"
    if not path.exists():
        logger.info("downloading {}", url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        tmp_path: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(dir=CACHE_DIR, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    shutil.copyfileobj(response, tmp, length=1024 * 1024)
            if tmp_path:
                tmp_path.replace(path)
        except Exception:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
            raise
    if expected_hash and file_hash(path, hash_algorithm) != expected_hash:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"{hash_algorithm} mismatch for {url}")
    return path


def inspect_deb(path: Path) -> dict[str, Any]:
    members = read_ar(path)
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    if not control_name:
        raise RuntimeError(f"{path.name} has no control.tar member")
    control_bytes = extract_control_tar(members[control_name], control_name)
    control = read_control_file(control_bytes, control_name)
    data = path.read_bytes()
    return {
        "control": control,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_ar(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise RuntimeError(f"{path.name} is not a Debian ar archive")
    offset = 8
    members: dict[str, bytes] = {}
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[:16].decode("utf-8").strip().rstrip("/")
        size = int(header[48:58].decode("utf-8").strip())
        start = offset + 60
        end = start + size
        members[name] = data[start:end]
        offset = end + (size % 2)
    return members


def extract_control_tar(data: bytes, name: str) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".zst"):
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError("zstd is required to read control.tar.zst")
        result = subprocess.run([zstd, "-d", "-q", "-c"], input=data, check=True, capture_output=True)
        return result.stdout
    if name.endswith(".tar"):
        return data
    raise RuntimeError(f"unsupported control archive format {name}")


def read_control_file(tar_bytes: bytes, member_name: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / member_name
        tar_path.write_bytes(tar_bytes)
        with tarfile.open(tar_path) as archive:
            control_member = next((m for m in archive.getmembers() if m.name in {"control", "./control"}), None)
            if not control_member:
                raise RuntimeError("control file is missing")
            extracted = archive.extractfile(control_member)
            if not extracted:
                raise RuntimeError("control file could not be read")
            return parse_control(extracted.read().decode("utf-8", errors="replace"))


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line[0].isspace() and current_key:
            fields[current_key] += "\n" + line
            continue
        key, value = line.split(":", 1)
        current_key = key
        fields[key] = value.strip()
    return fields


def format_control(fields: dict[str, str]) -> str:
    preferred = [
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Depends",
        "Recommends",
        "Suggests",
        "Section",
        "Priority",
        "Homepage",
        "Description",
        "Filename",
        "Size",
        "MD5sum",
        "SHA1",
        "SHA256",
    ]
    keys = preferred + sorted(k for k in fields if k not in preferred)
    return "\n".join(f"{key}: {fields[key]}" for key in keys if fields.get(key)) + "\n"


def safe_deb_filename(control: dict[str, str], asset_name: str) -> str:
    package = control["Package"].replace("/", "_")
    version = control["Version"].replace(":", "%3a").replace("/", "_")
    arch = control["Architecture"]
    if asset_name.endswith(".deb") and all(part in asset_name for part in [arch]):
        return asset_name
    return f"{package}_{version}_{arch}.deb"


def check_artifacts(
    lock: dict[str, Any],
    jobs: int,
    full_artifact_check: bool = False,
    full_checked_artifacts: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    health = {"version": 2, "generated_at": now_iso(), "packages": {}}
    full_checked_artifacts = full_checked_artifacts or set()
    artifact_entries = [
        (entry_name, arch, artifact)
        for entry_name, entry in lock["packages"].items()
        for arch, architecture in entry.get("architectures", {}).items()
        if (artifact := architecture.get("artifact"))
    ]
    max_workers = worker_count(len(artifact_entries), jobs)
    checked: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for entry_name, arch, artifact in artifact_entries:
            key = (entry_name, arch)
            if key in full_checked_artifacts:
                checked[key] = full_artifact_health(artifact)
                continue
            check = check_artifact if full_artifact_check else check_artifact_light
            futures[executor.submit(check, artifact)] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                checked[key] = future.result()
            except Exception as exc:
                checked[key] = {"status": "failed", "error": str(exc)}
    for entry_name, entry in lock["packages"].items():
        health["packages"][entry_name] = {
            "artifacts": {
                arch: checked[(entry_name, arch)]
                for arch, architecture in entry.get("architectures", {}).items()
                if architecture.get("artifact")
            }
        }
    return health


def check_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    path = download(artifact["url"], artifact["sha256"])
    size = path.stat().st_size
    sha256 = file_hash(path, "sha256")
    if size != artifact["size"]:
        raise RuntimeError(f"size mismatch for {artifact['url']}: expected {artifact['size']}, got {size}")
    if sha256 != artifact["sha256"]:
        raise RuntimeError(f"sha256 mismatch for {artifact['url']}")
    return {
        "status": "ok",
        "check": "full",
        "size": size,
        "sha256": sha256,
    }


def full_artifact_health(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "check": "full",
        "size": artifact["size"],
        "sha256": artifact["sha256"],
    }


def check_artifact_light(artifact: dict[str, Any]) -> dict[str, Any]:
    try:
        size = fetch_artifact_size(artifact["url"], "HEAD")
        check = "head"
    except urllib.error.HTTPError:
        size = fetch_artifact_size(artifact["url"], "GET", {"Range": "bytes=0-0"})
        check = "range"

    if size is not None and size != artifact["size"]:
        raise RuntimeError(f"size mismatch for {artifact['url']}: expected {artifact['size']}, got {size}")

    result: dict[str, Any] = {"status": "ok", "check": check}
    if size is not None:
        result["size"] = size
    return result


def fetch_artifact_size(url: str, method: str, headers: dict[str, str] | None = None) -> int | None:
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response_size(response)


def response_size(response: Any) -> int | None:
    content_range = response.getheader("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1]
        if total.isdigit():
            return int(total)
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if status == 206:
        return None
    content_length = response.getheader("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


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


def write_worker(path: Path) -> None:
    worker = """export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\\/pool\\/([^/]+)\\/([^/]+)\\/([^/]+)$/);
    if (!match) {
      return new Response("package redirect not found", { status: 404 });
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", {
        status: 405,
        headers: { "Allow": "GET, HEAD" },
      });
    }

    const cacheUrl = new URL(url);
    cacheUrl.search = "";
    const cacheKey = new Request(cacheUrl.toString(), { method: "GET" });
    const cache = caches.default;
    let cached = await cache.match(cacheKey);
    if (cached) {
      cached = new Response(cached.body, cached);
      cached.headers.set("X-Apt-Index-Redirect-Cache", "HIT");
      return cached;
    }

    const [, component, entryName, filename] = match;
    const rulesUrl = new URL(`/redirect-rules/${component}/${entryName}.json`, url);
    const rulesResponse = await env.ASSETS.fetch(rulesUrl.toString());
    if (!rulesResponse.ok) {
      return new Response("package redirect not found", { status: 404 });
    }

    const rules = await rulesResponse.json();
    const target = rules[filename];
    if (!target) {
      return new Response("package redirect not found", { status: 404 });
    }

    const redirectResponse = new Response(null, {
      status: 302,
      headers: {
        "Location": target,
        "Cache-Control": "public, max-age=__REDIRECT_BROWSER_TTL_SECONDS__, s-maxage=__REDIRECT_EDGE_TTL_SECONDS__",
        "Cloudflare-CDN-Cache-Control": "public, max-age=__REDIRECT_EDGE_TTL_SECONDS__",
        "X-Apt-Index-Redirect-Cache": "MISS",
      },
    });

    if (request.method === "GET") {
      ctx.waitUntil(cache.put(cacheKey, redirectResponse.clone()).catch((error) => {
        console.warn("redirect cache put failed", error);
      }));
    }

    return redirectResponse;
  },
};
"""
    worker = worker.replace("__REDIRECT_BROWSER_TTL_SECONDS__", str(REDIRECT_BROWSER_TTL_SECONDS))
    worker = worker.replace("__REDIRECT_EDGE_TTL_SECONDS__", str(REDIRECT_EDGE_TTL_SECONDS))
    path.write_text(worker, encoding="utf-8")


def write_routes(path: Path) -> None:
    routes = {"version": 1, "include": ["/pool/*"], "exclude": []}
    path.write_text(json.dumps(routes, indent=2) + "\n", encoding="utf-8")


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


def copy_state_files(lock: dict[str, Any]) -> None:
    shutil.copy2(LOCK_PATH, DIST_DIR / LOCK_PATH.name)
    copy_or_write_health_report(TRACK_HEALTH_PATH, DIST_DIR / TRACK_HEALTH_PATH.name, lambda: not_generated_track_health(lock))
    copy_or_write_health_report(ARTIFACT_HEALTH_PATH, DIST_DIR / ARTIFACT_HEALTH_PATH.name, lambda: not_generated_artifact_health(lock))


def copy_or_write_health_report(source: Path, target: Path, fallback_factory: Any) -> None:
    if source.exists():
        shutil.copy2(source, target)
        return
    logger.warning("{} is missing; writing not_generated report to {}", source.name, target)
    write_json(target, fallback_factory())


def not_generated_track_health(lock: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "status": "not_checked",
                "architectures": {
                    arch: {"status": "not_checked"}
                    for arch, architecture in entry.get("architectures", {}).items()
                    if architecture.get("artifact")
                },
            }
            for entry_name, entry in lock.get("packages", {}).items()
        },
    }


def not_generated_artifact_health(lock: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 2,
        "generated_at": now_iso(),
        "status": "not_generated",
        "packages": {
            entry_name: {
                "artifacts": {
                    arch: not_checked_artifact_health(architecture["artifact"])
                    for arch, architecture in entry.get("architectures", {}).items()
                    if architecture.get("artifact")
                }
            }
            for entry_name, entry in lock.get("packages", {}).items()
        },
    }


def not_checked_artifact_health(artifact: dict[str, Any]) -> dict[str, Any]:
    health: dict[str, Any] = {"status": "not_checked", "check": "not_generated"}
    for key in ("size", "sha256"):
        if key in artifact:
            health[key] = artifact[key]
    return health


def write_download_stats(path: Path, hostname: str | None, days: int = 30, strict: bool = False) -> None:
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
        stats = fetch_download_stats(zone_id, token, hostname, days)
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        if strict:
            raise
        logger.warning("Cloudflare HTTP analytics query failed; writing empty download stats: {}", exc)
        stats = empty_download_stats("analytics_query_failed", days)
    write_json(path, stats)


def fetch_download_stats(zone_id: str, token: str, hostname: str, days: int = 30) -> dict[str, Any]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    seven_day_start = end - timedelta(days=7)
    package_counts: dict[tuple[str, str], int] = {}
    seven_day_counts: dict[tuple[str, str], int] = {}
    daily_rows: list[dict[str, Any]] = []

    for window_start, window_end in daily_time_windows(start, end):
        rows = fetch_download_path_rows(zone_id, token, hostname, window_start, window_end)
        counts = aggregate_path_download_counts(rows)
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
        days,
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
        "packageFilter": http_download_filter(hostname, start, end),
    }
    payload = cloudflare_graphql(token, query, variables)
    zones = payload.get("data", {}).get("viewer", {}).get("zones", [])
    if not zones:
        raise RuntimeError(f"Cloudflare zone {zone_id!r} returned no HTTP analytics rows")
    return list(zones[0].get("packageRows", []))


def http_download_filter(hostname: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "datetime_geq": graphql_time(start),
        "datetime_lt": graphql_time(end),
        "requestSource": "eyeball",
        "clientRequestHTTPHost": hostname,
        "clientRequestHTTPMethodName": "GET",
        "clientRequestPath_like": "/pool/%",
    }


def cloudflare_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
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


def aggregate_path_download_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return download_counts_to_rows(aggregate_path_download_counts(rows))


def aggregate_path_download_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        path = str(row.get("dimensions", {}).get("clientRequestPath") or "")
        parsed = parse_package_download_path(path)
        if not parsed:
            continue
        _component, entry_name, filename = parsed
        key = (entry_name, package_architecture(filename))
        counts[key] = counts.get(key, 0) + int(row.get("count") or 0)
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


def package_architecture(filename: str) -> str:
    match = re.search(r"_([^_]+)\.deb$", filename)
    return match.group(1) if match else ""


def format_download_stats(
    package_rows: list[dict[str, Any]],
    seven_day_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    hostname: str,
    days: int,
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


def empty_download_stats(reason: str, days: int = 30) -> dict[str, Any]:
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


def file_hash(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from apt_index.published_state import PublishedState
from apt_index import zstd

JsonFetcher = Callable[[str, dict[str, str] | None], Any]
JsonPoster = Callable[[str, Any, dict[str, str] | None], Any]
BytesFetcher = Callable[[str, dict[str, str] | None], bytes]
JsonWriter = Callable[[Path, Any], None]


def redirect_maps(
    state: PublishedState,
) -> tuple[dict[str, str], dict[tuple[str, str], dict[str, str]]]:
    return state.redirect_snapshot(), state.redirect_shards()


def write_redirect_rules(
    state: PublishedState,
    *,
    dist_dir: Path,
    redirect_rules_dirname: str,
    redirect_snapshot_filename: str,
    write_json: JsonWriter,
) -> dict[str, str]:
    redirects, shards = redirect_maps(state)
    redirect_dir = dist_dir / redirect_rules_dirname
    for (shard_component, entry_name), shard in shards.items():
        shard_path = redirect_dir / shard_component / f"{entry_name}.json"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(shard_path, shard)
    write_redirect_snapshot(redirect_dir / redirect_snapshot_filename, redirects)
    return redirects


def write_redirect_snapshot(path: Path, redirects: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "redirects": redirects}, indent=2, sort_keys=True) + "\n"
    path.write_bytes(zstd.compress(payload.encode("utf-8")))


def read_redirect_snapshot(path: Path) -> dict[str, str]:
    payload = json.loads(zstd.decompress(path.read_bytes()).decode("utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("redirects"), dict):
        raise RuntimeError(f"{path}: unsupported redirect snapshot format")
    return {str(key): str(value) for key, value in payload["redirects"].items()}


def plan_redirect_purge(
    output: Path,
    snapshot: Path,
    base_url: str,
    *,
    redirect_rules_dirname: str,
    redirect_snapshot_filename: str,
    legacy_redirect_rules_paths: tuple[str, ...],
    fetch_previous_redirect_snapshot: Callable[[str, bool], dict[str, str]],
    strict: bool = False,
) -> list[str]:
    base_url = base_url.rstrip("/")
    new_redirects = read_redirect_snapshot(snapshot)
    old_redirects = fetch_previous_redirect_snapshot(base_url, strict)
    removed_or_changed_paths = {
        path for path, old_target in old_redirects.items() if new_redirects.get(path) != old_target
    }
    added_or_changed_paths = {
        path for path, new_target in new_redirects.items() if old_redirects.get(path) != new_target
    }
    purge_paths = removed_or_changed_paths | added_or_changed_paths
    shard_paths = {
        shard_path
        for path in purge_paths
        if (shard_path := redirect_shard_path_for_virtual_path(path, redirect_rules_dirname)) is not None
    }
    if purge_paths:
        shard_paths.add(f"/{redirect_rules_dirname}/{redirect_snapshot_filename}")
    purge_paths.update(shard_paths)
    purge_paths.update(legacy_redirect_rules_paths)
    urls = [base_url + path for path in sorted(purge_paths)]
    output.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    logger.info("planned {} redirect cache purge URLs at {}", len(urls), output)
    return urls


def redirect_shard_path_for_virtual_path(path: str, redirect_rules_dirname: str) -> str | None:
    match = re.fullmatch(r"/pool/([^/]+)/([^/]+)/[^/]+", path)
    if not match:
        return None
    component, entry_name = match.groups()
    return f"/{redirect_rules_dirname}/{component}/{entry_name}.json"


def fetch_previous_redirect_snapshot(
    base_url: str,
    *,
    redirect_rules_dirname: str,
    redirect_snapshot_filename: str,
    fetch_bytes: BytesFetcher,
    strict: bool = False,
) -> dict[str, str]:
    snapshot_url = f"{base_url.rstrip('/')}/{redirect_rules_dirname}/{redirect_snapshot_filename}"
    cache_bust = os.environ.get("GITHUB_RUN_ID") or str(int(datetime.now(timezone.utc).timestamp()))
    separator = "&" if "?" in snapshot_url else "?"
    snapshot_url = f"{snapshot_url}{separator}run={cache_bust}"
    try:
        data = fetch_bytes(snapshot_url, {"Cache-Control": "no-cache"})
        payload = decode_redirect_snapshot_payload(data)
    except (RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        if strict:
            raise
        logger.warning("previous redirect snapshot unavailable; treating as first deploy: {}", exc)
        return {}
    if payload.get("version") != 1 or not isinstance(payload.get("redirects"), dict):
        raise RuntimeError(f"{snapshot_url}: unsupported redirect snapshot format")
    return {str(key): str(value) for key, value in payload["redirects"].items()}


def decode_redirect_snapshot_payload(data: bytes) -> dict[str, Any]:
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json.loads(zstd.decompress(data).decode("utf-8"))


def purge_redirect_cache(
    urls_path: Path,
    *,
    redirect_rules_dirname: str,
    resolve_cloudflare_zone_id: Callable[[str, str], str | None],
    purge_cloudflare_urls: Callable[[str, str, list[str]], None],
    purge_cloudflare_prefixes: Callable[[str, str, list[str]], None],
    strict: bool = False,
) -> None:
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
        purge_cloudflare_prefixes(zone_id, token, [f"{hostname}/{redirect_rules_dirname}"])
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        if strict:
            raise
        logger.warning("Cloudflare redirect cache purge failed; skipping {} URLs: {}", len(urls), exc)
        return
    logger.info("purged {} redirect cache URLs and redirect-rules prefix", len(urls))


def resolve_cloudflare_zone_id(
    token: str,
    hostname: str,
    *,
    fetch_json: JsonFetcher,
) -> str | None:
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


def purge_cloudflare_urls(zone_id: str, token: str, urls: list[str], *, post_json: JsonPoster) -> None:
    purge_cloudflare_cache_payload(zone_id, token, {"files": urls}, post_json=post_json)


def purge_cloudflare_prefixes(zone_id: str, token: str, prefixes: list[str], *, post_json: JsonPoster) -> None:
    purge_cloudflare_cache_payload(zone_id, token, {"prefixes": prefixes}, post_json=post_json)


def purge_cloudflare_cache_payload(zone_id: str, token: str, payload: dict[str, Any], *, post_json: JsonPoster) -> None:
    response = post_json(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache",
        payload,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if not response.get("success"):
        raise RuntimeError(f"Cloudflare cache purge failed: {response.get('errors')!r}")


def batched(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from apt_index import deb


def check_artifacts(
    lock: dict[str, Any],
    jobs: int,
    *,
    full_artifact_check: bool,
    full_checked_artifacts: set[tuple[str, str]] | None,
    now_iso: Callable[[], str],
    worker_count: Callable[[int, int | None], int],
    cache_dir: Path,
    user_agent: str,
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
            futures[
                executor.submit(
                    check,
                    artifact,
                    cache_dir=cache_dir,
                    user_agent=user_agent,
                )
            ] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                checked[key] = future.result()
            except Exception as exc:
                checked[key] = {"status": "failed", "error": str(exc)}

    for entry_name, entry in lock["packages"].items():
        artifacts: dict[str, Any] = {}
        for arch, architecture in entry.get("architectures", {}).items():
            if not architecture.get("artifact"):
                continue
            artifacts[arch] = checked[(entry_name, arch)]
        health["packages"][entry_name] = {"artifacts": artifacts}
    return health


def check_artifact(
    artifact: dict[str, Any],
    *,
    cache_dir: Path,
    user_agent: str,
) -> dict[str, Any]:
    path = deb.download(
        artifact["url"],
        cache_dir=cache_dir,
        user_agent=user_agent,
        expected_hash=artifact["sha256"],
    )
    size = path.stat().st_size
    sha256 = deb.file_hash(path, "sha256")
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


def check_artifact_light(artifact: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    try:
        size = fetch_artifact_size(artifact["url"], "HEAD", user_agent=user_agent)
        check = "head"
    except urllib.error.HTTPError:
        size = fetch_artifact_size(
            artifact["url"],
            "GET",
            headers={"Range": "bytes=0-0"},
            user_agent=user_agent,
        )
        check = "range"
    if size is not None and size != artifact["size"]:
        raise RuntimeError(f"size mismatch for {artifact['url']}: expected {artifact['size']}, got {size}")
    result: dict[str, Any] = {"status": "ok", "check": check}
    if size is not None:
        result["size"] = size
    return result


def fetch_artifact_size(
    url: str,
    method: str,
    *,
    user_agent: str,
    headers: dict[str, str] | None = None,
) -> int | None:
    request_headers = {"User-Agent": user_agent}
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

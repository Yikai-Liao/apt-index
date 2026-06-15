from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JsonFetcher = Callable[[str, dict[str, str] | None], Any]
TextFetcher = Callable[[str, dict[str, str] | None], str]


@dataclass(frozen=True)
class ArtifactCandidate:
    url: str
    upstream_version: str
    asset_name: str
    expected_hash: str | None = None
    hash_algorithm: str = "sha256"


def resolve_candidate(
    architecture: dict[str, Any],
    *,
    fetch_json: JsonFetcher,
    fetch_text: TextFetcher,
    root: Path,
) -> ArtifactCandidate:
    source_config = architecture["source"]
    source = source_config["type"]
    if source == "github":
        release = github_release(source_config, architecture["update_policy"], fetch_json=fetch_json, root=root)
        pattern = source_config["asset_pattern"]
        for asset in release.get("assets", []):
            name = asset["name"]
            if fnmatch.fnmatch(name, pattern):
                return ArtifactCandidate(asset["browser_download_url"], release["tag_name"], name)
        raise RuntimeError(f"no GitHub asset matched {pattern!r}")
    if source == "aur":
        srcinfo = fetch_text(f"https://aur.archlinux.org/cgit/aur.git/plain/.SRCINFO?h={source_config['package']}", None)
        fields = parse_srcinfo(srcinfo)
        source_key, source_index, source_value = select_aur_source(fields, source_config["asset_pattern"])
        checksum_algorithm, checksum = aur_checksum_for(fields, source_key, source_index)
        asset_name, artifact_url = split_aur_source(source_value)
        return ArtifactCandidate(artifact_url, first_srcinfo_value(fields, "pkgver", "unknown"), asset_name, checksum, checksum_algorithm)
    if source == "sourceforge":
        files = sourceforge_files(
            source_config["project"],
            source_config["path"],
            fetch_text=fetch_text,
        )
        matched = [file for file in files if sourceforge_asset_matches(file["name"], source_config["asset_regex"])]
        if not matched:
            raise RuntimeError(f"no SourceForge asset matched {source_config['asset_regex']!r}")
        if len(matched) > 1:
            matched_names = ", ".join(file["name"] for file in matched)
            raise RuntimeError(f"multiple SourceForge assets matched {source_config['asset_regex']!r}: {matched_names}")
        file = matched[0]
        hash_algorithm = "sha1" if file.get("sha1") else "md5"
        expected_hash = file.get(hash_algorithm)
        return ArtifactCandidate(file["download_url"], file["name"], file["name"], expected_hash, hash_algorithm)
    if source == "url":
        url = source_config["url"]
        return ArtifactCandidate(url, "fixed", Path(url).name)
    raise RuntimeError(f"unsupported source resolver {source!r}")


def github_release(
    source_config: dict[str, Any],
    update_policy: str,
    *,
    fetch_json: JsonFetcher,
    root: Path,
) -> dict[str, Any]:
    repo = source_config["repo"]
    if update_policy == "fixed":
        path = f"repos/{repo}/releases/tags/{source_config['release_tag']}"
    else:
        path = f"repos/{repo}/releases/latest"
    headers = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return fetch_json(f"https://api.github.com/{path}", headers)

    gh = shutil.which("gh")
    if gh:
        result = subprocess.run([gh, "api", path], cwd=root, check=True, text=True, capture_output=True)
        return json.loads(result.stdout)
    return fetch_json(f"https://api.github.com/{path}", headers)


def parse_srcinfo(srcinfo: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for raw_line in srcinfo.splitlines():
        stripped = raw_line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
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


def aur_checksum_for(fields: dict[str, list[str]], source_key: str, source_index: int) -> tuple[str, str | None]:
    suffix = source_key.removeprefix("source")
    checksum_source_keys = [f"{algorithm}sums{suffix}" for algorithm in ("sha256", "sha512")]
    for checksum_key in checksum_source_keys:
        values = fields.get(checksum_key, [])
        if source_index < len(values):
            checksum = values[source_index]
            if checksum != "SKIP":
                return checksum_key.split("sums", 1)[0], checksum
    return "sha256", None


def split_aur_source(value: str) -> tuple[str, str]:
    if "::" in value:
        asset_name, url = value.split("::", 1)
        return asset_name, url
    return Path(value).name, value


def sourceforge_files(
    project: str,
    path: str,
    *,
    fetch_text: TextFetcher,
) -> list[dict[str, Any]]:
    url = f"https://sourceforge.net/projects/{project}/files/{path.strip('/')}/"
    html = fetch_text(url, None)
    match = re.search(r"net\.sf\.files\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not match:
        raise RuntimeError(f"could not parse SourceForge file listing for {project}/{path}")
    payload = json.loads(match.group(1))
    files = []
    for value in payload.values():
        if not isinstance(value, dict) or not value.get("downloadable"):
            continue
        name = str(value.get("name", ""))
        download_url = str(value.get("download_url", ""))
        if not name or not download_url:
            continue
        files.append(
            {
                "name": name,
                "download_url": download_url,
                "md5": str(value.get("md5", "")),
                "sha1": str(value.get("sha1", "")),
            }
        )
    return files


def sourceforge_asset_matches(name: str, asset_regex: str) -> bool:
    return re.fullmatch(asset_regex, name) is not None

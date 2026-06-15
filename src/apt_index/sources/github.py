from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from apt_index.config import EntryArchitecture, GithubArchSource, UpdatePolicy

from . import ArtifactCandidate, JsonFetcher, ResolvedAsset, SourceResolver


@dataclass(frozen=True)
class GithubRelease:
    tag_name: str
    assets: list[ResolvedAsset]

    @classmethod
    def from_payload(cls, payload: object) -> GithubRelease:
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected GitHub release payload")
        tag_name = str(payload.get("tag_name", ""))
        if not tag_name:
            raise RuntimeError("GitHub release payload is missing tag_name")
        assets = [asset for asset in (cls.parse_asset(value) for value in payload.get("assets", [])) if asset is not None]
        return cls(tag_name=tag_name, assets=assets)

    @staticmethod
    def parse_asset(payload: object) -> ResolvedAsset | None:
        if not isinstance(payload, dict):
            return None
        name = str(payload.get("name", ""))
        download_url = str(payload.get("browser_download_url", ""))
        if not name or not download_url:
            return None
        return ResolvedAsset(name=name, download_url=download_url)


class GithubResolver(SourceResolver):
    key = "github"

    def resolve_candidate(self, architecture: EntryArchitecture) -> ArtifactCandidate:
        source = architecture.source
        if not isinstance(source, GithubArchSource):
            raise RuntimeError(f"{self.key} resolver requires github source")
        release = self.load_release(source, architecture.update_policy)
        for asset in release.assets:
            if fnmatch.fnmatch(asset.name, source.asset_pattern):
                return asset.as_candidate(release.tag_name)
        raise RuntimeError(f"no GitHub asset matched {source.asset_pattern!r}")

    def load_release(
        self,
        source: GithubArchSource,
        update_policy: UpdatePolicy,
    ) -> GithubRelease:
        path = self.release_path(source, update_policy)
        headers = {}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return GithubRelease.from_payload(self.context.fetch_json(f"https://api.github.com/{path}", headers))

        gh = shutil.which("gh")
        if gh:
            result = subprocess.run([gh, "api", path], cwd=self.context.root, check=True, text=True, capture_output=True)
            return GithubRelease.from_payload(json.loads(result.stdout))
        return GithubRelease.from_payload(self.context.fetch_json(f"https://api.github.com/{path}", headers))

    @staticmethod
    def release_path(source: GithubArchSource, update_policy: UpdatePolicy) -> str:
        if update_policy == "fixed":
            return f"repos/{source.repo}/releases/tags/{source.release_tag}"
        return f"repos/{source.repo}/releases/latest"

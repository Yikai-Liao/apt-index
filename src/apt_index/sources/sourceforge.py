from __future__ import annotations

import json
import re

from apt_index.config import EntryArchitecture, SourceforgeArchSource

from . import ArtifactCandidate, ResolvedAsset, SourceResolver


class SourceforgeResolver(SourceResolver):
    key = "sourceforge"

    def resolve_candidate(self, architecture: EntryArchitecture) -> ArtifactCandidate:
        source = architecture.source
        if not isinstance(source, SourceforgeArchSource):
            raise RuntimeError(f"{self.key} resolver requires sourceforge source")
        assets = self.load_assets(source)
        matched = [asset for asset in assets if self.asset_matches(asset.name, source.asset_regex)]
        if not matched:
            raise RuntimeError(f"no SourceForge asset matched {source.asset_regex!r}")
        if len(matched) > 1:
            matched_names = ", ".join(asset.name for asset in matched)
            raise RuntimeError(f"multiple SourceForge assets matched {source.asset_regex!r}: {matched_names}")
        asset = matched[0]
        return asset.as_candidate(asset.name)

    def load_assets(self, source: SourceforgeArchSource) -> list[ResolvedAsset]:
        url = f"https://sourceforge.net/projects/{source.project}/files/{source.path.strip('/')}/"
        html = self.context.fetch_text(url, None)
        match = re.search(r"net\.sf\.files\s*=\s*(\{.*?\});", html, re.DOTALL)
        if not match:
            raise RuntimeError(f"could not parse SourceForge file listing for {source.project}/{source.path}")
        payload = json.loads(match.group(1))
        assets: list[ResolvedAsset] = []
        for value in payload.values():
            asset = self.asset_from_payload(value)
            if asset is not None:
                assets.append(asset)
        return assets

    @staticmethod
    def asset_matches(name: str, asset_regex: str) -> bool:
        return re.fullmatch(asset_regex, name) is not None

    @staticmethod
    def asset_from_payload(payload: object) -> ResolvedAsset | None:
        if not isinstance(payload, dict) or not payload.get("downloadable"):
            return None
        name = str(payload.get("name", ""))
        download_url = str(payload.get("download_url", ""))
        if not name or not download_url:
            return None
        sha1 = str(payload.get("sha1", ""))
        md5 = str(payload.get("md5", ""))
        expected_hash = sha1 or md5
        hash_algorithm = "sha1" if sha1 else "md5"
        return ResolvedAsset(
            name=name,
            download_url=download_url,
            expected_hash=expected_hash,
            hash_algorithm=hash_algorithm,
        )

from __future__ import annotations

from apt_index.config import EntryArchitecture, UrlArchSource

from . import ArtifactCandidate, SourceResolver


class UrlResolver(SourceResolver):
    key = "url"

    def resolve_candidate(self, architecture: EntryArchitecture) -> ArtifactCandidate:
        source = architecture.source
        if not isinstance(source, UrlArchSource):
            raise RuntimeError(f"{self.key} resolver requires url source")
        return ArtifactCandidate(
            url=source.url,
            upstream_version="fixed",
            asset_name=source.url.rsplit("/", 1)[-1],
        )

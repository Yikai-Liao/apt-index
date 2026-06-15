from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apt_index.config import ActiveSource, EntryArchitecture, ResolverKey, UpdatePolicy

JsonFetcher = Callable[[str, dict[str, str] | None], Any]
TextFetcher = Callable[[str, dict[str, str] | None], str]


@dataclass(frozen=True)
class ArtifactCandidate:
    url: str
    upstream_version: str
    asset_name: str
    expected_hash: str | None = None
    hash_algorithm: str = "sha256"


@dataclass(frozen=True)
class ResolvedAsset:
    name: str
    download_url: str
    expected_hash: str | None = None
    hash_algorithm: str = "sha256"

    def as_candidate(self, upstream_version: str) -> ArtifactCandidate:
        return ArtifactCandidate(
            self.download_url,
            upstream_version,
            self.name,
            self.expected_hash,
            self.hash_algorithm,
        )


@dataclass(frozen=True)
class ResolverContext:
    fetch_json: JsonFetcher
    fetch_text: TextFetcher
    root: Path


class SourceResolver(ABC):
    key: ResolverKey

    def __init__(self, context: ResolverContext) -> None:
        self.context = context

    @abstractmethod
    def resolve_candidate(self, architecture: EntryArchitecture) -> ArtifactCandidate:
        raise NotImplementedError


CandidateResolver = Callable[[EntryArchitecture], ArtifactCandidate]


from .aur import AurResolver  # noqa: E402
from .github import GithubResolver  # noqa: E402
from .sourceforge import SourceforgeResolver  # noqa: E402
from .url import UrlResolver  # noqa: E402


def build_candidate_resolver(
    *,
    fetch_json: JsonFetcher,
    fetch_text: TextFetcher,
    root: Path,
) -> CandidateResolver:
    context = ResolverContext(
        fetch_json=fetch_json,
        fetch_text=fetch_text,
        root=root,
    )
    resolvers: dict[ResolverKey, SourceResolver] = {
        GithubResolver.key: GithubResolver(context),
        AurResolver.key: AurResolver(context),
        SourceforgeResolver.key: SourceforgeResolver(context),
        UrlResolver.key: UrlResolver(context),
    }

    def resolve(architecture: EntryArchitecture) -> ArtifactCandidate:
        resolver = resolvers.get(architecture.source.type)
        if resolver is None:
            return unsupported_resolver(architecture.source, architecture.update_policy)
        return resolver.resolve_candidate(architecture)

    return resolve


def unsupported_resolver(source: ActiveSource, update_policy: UpdatePolicy) -> ArtifactCandidate:
    del update_policy
    raise RuntimeError(f"unsupported source resolver {source.type!r}")


__all__ = [
    "ArtifactCandidate",
    "CandidateResolver",
    "JsonFetcher",
    "ResolvedAsset",
    "ResolverContext",
    "SourceResolver",
    "TextFetcher",
    "build_candidate_resolver",
]

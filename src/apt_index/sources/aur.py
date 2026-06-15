from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from apt_index.config import AurArchSource, EntryArchitecture

from . import ArtifactCandidate, ResolvedAsset, SourceResolver


@dataclass(frozen=True)
class Srcinfo:
    fields: dict[str, list[str]]

    @classmethod
    def parse(cls, srcinfo: str) -> Srcinfo:
        fields: dict[str, list[str]] = {}
        for raw_line in srcinfo.splitlines():
            stripped = raw_line.strip()
            if not stripped or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            fields.setdefault(key.strip(), []).append(value.strip())
        return cls(fields=fields)

    def first_value(self, key: str, default: str = "") -> str:
        values = self.fields.get(key)
        return values[0] if values else default

    def select_asset(self, asset_pattern: str) -> tuple[str, int, ResolvedAsset]:
        for key, values in self.fields.items():
            if key != "source" and not key.startswith("source_"):
                continue
            for index, value in enumerate(values):
                asset = self.source_asset(value)
                if self.source_matches(asset_pattern, value, asset.name, asset.download_url):
                    return key, index, asset
        raise RuntimeError(f"no AUR source matched {asset_pattern!r}")

    def checksum_for(self, source_key: str, source_index: int) -> tuple[str, str | None]:
        suffix = source_key.removeprefix("source")
        checksum_source_keys = [f"{algorithm}sums{suffix}" for algorithm in ("sha256", "sha512")]
        for checksum_key in checksum_source_keys:
            values = self.fields.get(checksum_key, [])
            if source_index < len(values):
                checksum = values[source_index]
                if checksum != "SKIP":
                    return checksum_key.split("sums", 1)[0], checksum
        return "sha256", None

    @staticmethod
    def source_matches(pattern: str, raw_value: str, asset_name: str, url: str) -> bool:
        return any(fnmatch.fnmatch(value, pattern) for value in (asset_name, url, raw_value))

    @staticmethod
    def source_asset(value: str) -> ResolvedAsset:
        if "::" in value:
            name, download_url = value.split("::", 1)
            return ResolvedAsset(name=name, download_url=download_url)
        return ResolvedAsset(name=Path(value).name, download_url=value)


class AurResolver(SourceResolver):
    key = "aur"

    def resolve_candidate(self, architecture: EntryArchitecture) -> ArtifactCandidate:
        source = architecture.source
        if not isinstance(source, AurArchSource):
            raise RuntimeError(f"{self.key} resolver requires aur source")
        srcinfo = Srcinfo.parse(
            self.context.fetch_text(f"https://aur.archlinux.org/cgit/aur.git/plain/.SRCINFO?h={source.package}", None)
        )
        source_key, source_index, asset = srcinfo.select_asset(source.asset_pattern)
        hash_algorithm, expected_hash = srcinfo.checksum_for(source_key, source_index)
        return ResolvedAsset(
            name=asset.name,
            download_url=asset.download_url,
            expected_hash=expected_hash,
            hash_algorithm=hash_algorithm,
        ).as_candidate(srcinfo.first_value("pkgver", "unknown"))

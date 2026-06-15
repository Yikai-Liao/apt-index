from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _package_virtual_path(component: str, entry_name: str, filename: str) -> str:
    return f"pool/{component}/{entry_name}/{filename}"


class PublishedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_name: str
    entry_homepage: str | None = None
    configured_arch: str
    package_arch: str
    source: str
    update_policy: str
    url: str
    filename: str
    virtual_path: str
    control: dict[str, Any]
    size: int
    md5: str
    sha1: str
    sha256: str
    sha512: str

    def package_stanza(self) -> dict[str, Any]:
        stanza = dict(self.control)
        stanza["Filename"] = self.virtual_path
        stanza["Size"] = str(self.size)
        stanza["MD5sum"] = self.md5
        stanza["SHA1"] = self.sha1
        stanza["SHA256"] = self.sha256
        return stanza

    def download_path(self) -> str:
        return "/" + self.virtual_path

    def download_identity(self) -> tuple[str, str]:
        return self.entry_name, self.package_arch

    def package_name(self) -> str:
        return str(self.control.get("Package") or self.entry_name)

    def version(self) -> str:
        return str(self.control.get("Version") or "")

    def homepage(self) -> str:
        return str(self.entry_homepage or self.control.get("Homepage") or self.url or "#")


class LockedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    upstream_version: str
    asset_name: str
    filename: str
    control: dict[str, Any]
    size: int
    md5: str
    sha1: str
    sha256: str
    sha512: str

    def matches_candidate(self, candidate: Any) -> bool:
        if not (
            self.url == candidate.url
            and self.upstream_version == candidate.upstream_version
            and self.asset_name == candidate.asset_name
        ):
            return False
        if candidate.expected_hash is None:
            return True
        return getattr(self, candidate.hash_algorithm) == candidate.expected_hash


class LockedArchitecture(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    update_policy: str
    resolved_at: str
    artifact: LockedArtifact


class LockedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    homepage: str | None = None
    architectures: dict[str, LockedArchitecture]


class LockfileState(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = 2
    generated_at: str | None = None
    packages: dict[str, LockedEntry]


class PublishedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_name: str
    homepage: str | None = None
    artifacts: list[PublishedArtifact]


class PublishedState(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    generated_at: str | None = None
    entries: list[PublishedEntry]

    @classmethod
    def from_lock(cls, lock: dict[str, Any], *, component: str) -> PublishedState:
        locked = LockfileState.model_validate(lock)
        entries: list[PublishedEntry] = []
        for entry_name, entry in sorted(locked.packages.items()):
            artifacts: list[PublishedArtifact] = []
            for configured_arch, architecture in sorted(entry.architectures.items()):
                artifact = architecture.artifact
                control = dict(artifact.control)
                filename = artifact.filename
                artifacts.append(
                    PublishedArtifact(
                        entry_name=entry_name,
                        entry_homepage=entry.homepage,
                        configured_arch=configured_arch,
                        package_arch=str(control.get("Architecture") or configured_arch),
                        source=architecture.source,
                        update_policy=architecture.update_policy,
                        url=artifact.url,
                        filename=filename,
                        virtual_path=_package_virtual_path(component, entry_name, filename),
                        control=control,
                        size=artifact.size,
                        md5=artifact.md5,
                        sha1=artifact.sha1,
                        sha256=artifact.sha256,
                        sha512=artifact.sha512,
                    )
                )
            entries.append(
                PublishedEntry(
                    entry_name=entry_name,
                    homepage=entry.homepage,
                    artifacts=artifacts,
                )
            )
        return cls(component=component, generated_at=locked.generated_at, entries=entries)

    def architectures(self) -> list[str]:
        return sorted({artifact.configured_arch for artifact in self.artifacts()})

    def artifacts(self) -> list[PublishedArtifact]:
        return [artifact for entry in self.entries for artifact in entry.artifacts]

    def artifacts_for_arch(self, arch: str) -> list[PublishedArtifact]:
        return [artifact for artifact in self.artifacts() if artifact.configured_arch == arch]

    def download_path_index(self) -> dict[str, tuple[str, str]]:
        return {
            artifact.download_path(): artifact.download_identity()
            for artifact in self.artifacts()
        }

    def redirect_snapshot(self) -> dict[str, str]:
        redirects: dict[str, str] = {}
        for artifact in self.artifacts():
            virtual_path = artifact.download_path()
            existing_target = redirects.get(virtual_path)
            if existing_target and existing_target != artifact.url:
                raise RuntimeError(f"conflicting redirect target for {virtual_path}")
            redirects[virtual_path] = artifact.url
        return dict(sorted(redirects.items()))

    def redirect_shards(self) -> dict[tuple[str, str], dict[str, str]]:
        shards: dict[tuple[str, str], dict[str, str]] = {}
        for entry in self.entries:
            shard: dict[str, str] = {}
            for artifact in entry.artifacts:
                existing_target = shard.get(artifact.filename)
                if existing_target and existing_target != artifact.url:
                    raise RuntimeError(f"conflicting redirect target for {artifact.entry_name}/{artifact.filename}")
                shard[artifact.filename] = artifact.url
            if shard:
                shards[(self.component, entry.entry_name)] = dict(sorted(shard.items()))
        return shards

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict


def _package_virtual_path(component: str, entry_name: str, filename: str) -> str:
    return f"pool/{component}/{entry_name}/{filename}"


class PackageIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_name: str
    arch: str


class RedirectShardKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    entry_name: str

    @property
    def relative_path(self) -> str:
        return f"{self.component}/{self.entry_name}.json"


class RedirectRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    target: str
    shard: RedirectShardKey
    filename: str


class RedirectRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    rules: tuple[RedirectRule, ...]

    @property
    def snapshot(self) -> dict[str, str]:
        redirects: dict[str, str] = {}
        for rule in self.rules:
            existing_target = redirects.get(rule.path)
            if existing_target and existing_target != rule.target:
                raise RuntimeError(f"conflicting redirect target for {rule.path}")
            redirects[rule.path] = rule.target
        return dict(sorted(redirects.items()))

    @property
    def shards(self) -> dict[RedirectShardKey, dict[str, str]]:
        shards: dict[RedirectShardKey, dict[str, str]] = {}
        for rule in self.rules:
            shard = shards.setdefault(rule.shard, {})
            existing_target = shard.get(rule.filename)
            if existing_target and existing_target != rule.target:
                raise RuntimeError(f"conflicting redirect target for {rule.shard.entry_name}/{rule.filename}")
            shard[rule.filename] = rule.target
        return {
            key: dict(sorted(shard.items()))
            for key, shard in sorted(shards.items(), key=redirect_shard_item_sort_key)
        }


class DownloadPathIndex(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: dict[str, PackageIdentity]

    def identity_for(self, path: str) -> PackageIdentity | None:
        return self.paths.get(path)


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

    def apt_package_stanza(self) -> dict[str, Any]:
        stanza = dict(self.control)
        stanza["Filename"] = self.virtual_path
        stanza["Size"] = str(self.size)
        stanza["MD5sum"] = self.md5
        stanza["SHA1"] = self.sha1
        stanza["SHA256"] = self.sha256
        return stanza

    @property
    def download_path(self) -> str:
        return "/" + self.virtual_path

    @property
    def download_identity(self) -> PackageIdentity:
        return PackageIdentity(entry_name=self.entry_name, arch=self.package_arch)

    @property
    def redirect_target(self) -> str:
        return self.url

    @property
    def package_name(self) -> str:
        return str(self.control.get("Package") or self.entry_name)

    @property
    def version(self) -> str:
        return str(self.control.get("Version") or "")

    @property
    def homepage(self) -> str:
        return str(self.entry_homepage or self.control.get("Homepage") or self.url or "#")

    @property
    def description(self) -> str:
        value = self.control.get("Description")
        return str(value or "").splitlines()[0].strip() if value else ""


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
    def from_lock(cls, lock: Mapping[str, Any], *, component: str) -> PublishedState:
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

    @property
    def architectures(self) -> list[str]:
        return sorted({artifact.configured_arch for artifact in self.artifacts})

    @property
    def artifacts(self) -> list[PublishedArtifact]:
        return [artifact for entry in self.entries for artifact in entry.artifacts]

    def artifacts_for_arch(self, arch: str) -> list[PublishedArtifact]:
        return [artifact for artifact in self.artifacts if artifact.configured_arch == arch]

    def artifacts_by_architecture(self) -> dict[str, list[PublishedArtifact]]:
        return {
            arch: self.artifacts_for_arch(arch)
            for arch in self.architectures
        }

    def download_paths(self) -> DownloadPathIndex:
        return DownloadPathIndex(
            paths={
                artifact.download_path: artifact.download_identity
                for artifact in self.artifacts
            }
        )

    def redirects(self) -> RedirectRules:
        rules: list[RedirectRule] = []
        for entry in self.entries:
            shard = RedirectShardKey(component=self.component, entry_name=entry.entry_name)
            for artifact in entry.artifacts:
                rules.append(
                    RedirectRule(
                        path=artifact.download_path,
                        target=artifact.redirect_target,
                        shard=shard,
                        filename=artifact.filename,
                    )
                )
        return RedirectRules(rules=tuple(sorted(rules, key=redirect_rule_sort_key)))

    def entries_for_site(self) -> list[PublishedEntry]:
        return self.entries


def redirect_shard_item_sort_key(item: tuple[RedirectShardKey, dict[str, str]]) -> tuple[str, str]:
    key, _ = item
    return key.component, key.entry_name


def redirect_rule_sort_key(rule: RedirectRule) -> tuple[str, str]:
    return rule.path, rule.target

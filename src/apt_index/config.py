from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Arch = Literal["amd64", "arm64"]
ResolverKey = Literal["url", "github", "aur", "sourceforge", "script"]
UpdatePolicy = Literal["fixed", "track"]
T = TypeVar("T")

ENTRY_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


class ConfigError(ValueError):
    pass


class RepositoryMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str
    label: str
    description: str
    base_url: str


class SigningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_name: str


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str
    component: str
    repository: RepositoryMetadata
    signing: SigningConfig


class ArchitecturePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ResolverKey
    update_policy: UpdatePolicy


class UrlArchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["url"] = "url"
    url: str


class GithubArchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["github"] = "github"
    repo: str
    asset_pattern: str
    release_tag: str | None = None


class AurArchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["aur"] = "aur"
    package: str
    asset_pattern: str


class SourceforgeArchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sourceforge"] = "sourceforge"
    project: str
    path: str
    asset_regex: str


class ScriptArchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["script"] = "script"
    command: str


ActiveSource = Annotated[
    UrlArchSource | GithubArchSource | AurArchSource | SourceforgeArchSource | ScriptArchSource,
    Field(discriminator="type"),
]


class EntryArchitecture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    update_policy: UpdatePolicy
    source: ActiveSource


class EntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    homepage: str
    architectures: dict[Arch, EntryArchitecture]


class AptIndexConfig(RepositoryConfig):
    packages: dict[str, EntryConfig]

    def to_runtime_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class RawUrlSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: dict[Arch, str] = Field(default_factory=dict)


class RawGithubSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    asset_patterns: dict[Arch, str] = Field(default_factory=dict)
    release_tag: str | None = None
    release_tags: dict[Arch, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_release_tag_syntax(self) -> Self:
        if self.release_tag is not None and self.release_tags:
            raise ValueError("use release_tag or release_tags, not both")
        return self


class RawAurSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str
    asset_patterns: dict[Arch, str] = Field(default_factory=dict)


class RawSourceforgeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str
    path: str
    asset_regexes: dict[Arch, str] = Field(default_factory=dict)


class RawScriptSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str


class RawSourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: RawUrlSource | None = None
    github: RawGithubSource | None = None
    aur: RawAurSource | None = None
    sourceforge: RawSourceforgeSource | None = None
    script: RawScriptSource | None = None

    def get(self, key: ResolverKey) -> object | None:
        return getattr(self, key)


class RawEntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    homepage: str
    architectures: list[Arch] | dict[Arch, ArchitecturePlan]
    source: ResolverKey | None = None
    update_policy: UpdatePolicy | None = None
    sources: RawSourcesConfig

    def normalize(self) -> EntryConfig:
        plans = normalize_architecture_plans(
            self.architectures,
            self.source,
            self.update_policy,
        )
        if not plans:
            raise ValueError("at least one architecture is required")

        return EntryConfig(
            homepage=self.homepage,
            architectures={
                arch: EntryArchitecture(
                    update_policy=plan.update_policy,
                    source=normalize_arch_source(self.sources, arch, plan),
                )
                for arch, plan in plans.items()
            },
        )


SOURCE_CAPABILITIES: Mapping[ResolverKey, frozenset[UpdatePolicy]] = {
    "url": frozenset({"fixed"}),
    "github": frozenset({"fixed", "track"}),
    "aur": frozenset({"track"}),
    "sourceforge": frozenset({"fixed", "track"}),
    "script": frozenset({"track"}),
}


def load_configuration(root: Path) -> AptIndexConfig:
    repository_config_path = root / "apt-index.toml"
    old_config_path = root / "packages.toml"
    if old_config_path.exists():
        raise ConfigError("packages.toml is not a valid configuration entry point; use apt-index.toml and packages/")
    if not repository_config_path.exists():
        raise ConfigError("apt-index.toml is missing")

    repository_config = validate_model(
        RepositoryConfig,
        read_toml(repository_config_path),
        repository_config_path,
    )
    return AptIndexConfig(
        **repository_config.model_dump(),
        packages=load_entries(root / "packages"),
    )


def load_entries(packages_dir: Path) -> dict[str, EntryConfig]:
    if not packages_dir.exists():
        return {}
    if not packages_dir.is_dir():
        raise ConfigError("packages must be a directory")

    entry_files = discover_entry_files(packages_dir)
    entries: dict[str, EntryConfig] = {}
    for name, path in entry_files.items():
        raw_entry = validate_model(RawEntryConfig, read_toml(path), path)
        try:
            entries[name] = raw_entry.normalize()
        except ValueError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    return entries


def discover_entry_files(packages_dir: Path) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    for path in sorted(packages_dir.rglob("*.toml")):
        relative = path.relative_to(packages_dir)
        parts = relative.parts
        if len(parts) == 1:
            name = path.stem
        elif len(parts) == 2 and parts[1] == "index.toml":
            name = parts[0]
        else:
            raise ConfigError(f"nested entry path is not allowed: {path}")

        if not ENTRY_NAME_RE.fullmatch(name):
            raise ConfigError(f"invalid software entry name {name!r}")
        existing = entries.get(name)
        if existing is not None:
            raise ConfigError(f"duplicate software entry {name!r}: {existing} and {path}")
        entries[name] = path
    return entries


def normalize_architecture_plans(
    raw_architectures: list[Arch] | dict[Arch, ArchitecturePlan],
    shorthand_source: ResolverKey | None,
    shorthand_update_policy: UpdatePolicy | None,
) -> dict[Arch, ArchitecturePlan]:
    if isinstance(raw_architectures, list):
        if shorthand_source is None or shorthand_update_policy is None:
            raise ValueError("architecture shorthand requires source and update_policy")
        if len(set(raw_architectures)) != len(raw_architectures):
            raise ValueError("architecture shorthand contains duplicate entries")
        return {
            arch: ArchitecturePlan(
                source=shorthand_source,
                update_policy=shorthand_update_policy,
            )
            for arch in raw_architectures
        }

    if shorthand_source is not None or shorthand_update_policy is not None:
        raise ValueError("source/update_policy shorthand cannot be mixed with explicit architectures")

    return raw_architectures


def normalize_arch_source(
    sources: RawSourcesConfig,
    arch: Arch,
    plan: ArchitecturePlan,
) -> ActiveSource:
    source = sources.get(plan.source)
    if source is None:
        raise ValueError(f"{arch}: missing source {plan.source!r}")

    supported_policies = SOURCE_CAPABILITIES[plan.source]
    if plan.update_policy not in supported_policies:
        allowed = ", ".join(sorted(supported_policies))
        raise ValueError(f"{arch}: {plan.source} supports {allowed}, not {plan.update_policy}")

    return SOURCE_NORMALIZERS[plan.source](source, arch, plan.update_policy)


def normalize_url_source(
    source: object,
    arch: Arch,
    update_policy: UpdatePolicy,
) -> UrlArchSource:
    raw = expect_source(source, RawUrlSource)
    return UrlArchSource(url=required_arch_value(raw.urls, arch, "URL artifact"))


def normalize_github_source(
    source: object,
    arch: Arch,
    update_policy: UpdatePolicy,
) -> GithubArchSource:
    raw = expect_source(source, RawGithubSource)
    release_tag = None
    if update_policy == "fixed":
        release_tag = raw.release_tags.get(arch) or raw.release_tag
        if release_tag is None:
            raise ValueError(f"{arch}: fixed GitHub source requires a release tag")

    return GithubArchSource(
        repo=raw.repo,
        asset_pattern=required_arch_value(raw.asset_patterns, arch, "GitHub asset pattern"),
        release_tag=release_tag,
    )


def normalize_aur_source(
    source: object,
    arch: Arch,
    update_policy: UpdatePolicy,
) -> AurArchSource:
    raw = expect_source(source, RawAurSource)
    return AurArchSource(
        package=raw.package,
        asset_pattern=required_arch_value(
            raw.asset_patterns,
            arch,
            "AUR asset pattern",
        ),
    )


def normalize_script_source(
    source: object,
    arch: Arch,
    update_policy: UpdatePolicy,
) -> ScriptArchSource:
    raw = expect_source(source, RawScriptSource)
    return ScriptArchSource(command=raw.command)


def normalize_sourceforge_source(
    source: object,
    arch: Arch,
    update_policy: UpdatePolicy,
) -> SourceforgeArchSource:
    raw = expect_source(source, RawSourceforgeSource)
    asset_regex = required_arch_value(raw.asset_regexes, arch, "SourceForge asset regex")
    try:
        re.compile(asset_regex)
    except re.error as exc:
        raise ValueError(f"{arch}: invalid SourceForge asset regex {asset_regex!r}: {exc}") from exc
    return SourceforgeArchSource(
        project=raw.project,
        path=raw.path.strip("/"),
        asset_regex=asset_regex,
    )


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def required_arch_value(values: Mapping[Arch, str], arch: Arch, label: str) -> str:
    try:
        return values[arch]
    except KeyError as exc:
        raise ValueError(f"{arch}: missing {label}") from exc


def expect_source(source: object, model: type[T]) -> T:
    if not isinstance(source, model):
        raise TypeError(f"expected {model.__name__}")
    return cast(T, source)


def validate_model(model: type[T], data: dict[str, Any], path: Path) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


SourceNormalizer = Callable[[object, Arch, UpdatePolicy], ActiveSource]

SOURCE_NORMALIZERS: Mapping[ResolverKey, SourceNormalizer] = {
    "url": normalize_url_source,
    "github": normalize_github_source,
    "aur": normalize_aur_source,
    "sourceforge": normalize_sourceforge_source,
    "script": normalize_script_source,
}

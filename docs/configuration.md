# Configuration Model

This document describes the active split configuration model used by Apt Index today.

`apt-index.toml` plus `packages/` is the only supported configuration layout. The old single-file `packages.toml` entry point is rejected by the loader.

## Files

Repository configuration lives in `apt-index.toml`. It contains only repository-level settings such as the suite, component, repository metadata, and signing configuration. It does not define software entries.

Software entries live under `packages/` in one of two layouts:

```text
packages/<entry-name>.toml
packages/<entry-name>/index.toml
```

An entry must choose one layout. If both `packages/wechat.toml` and `packages/wechat/index.toml` exist, configuration loading fails. Entry names are flat; nested names such as `packages/tencent/wechat/index.toml` are not valid entry names.

Entry names use only lowercase letters, digits, hyphens, and underscores, and must start and end with a letter or digit:

```text
^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$
```

## Entry Schema

A software entry has:

- `homepage`, which describes the software entry itself.
- an architecture plan, which states which architectures the entry publishes, which source resolver each architecture uses, and which update policy each architecture follows.
- `sources`, which contains resolver-specific source options.

Source-specific fields are only valid under `sources.<resolver>`. Old flat fields such as `source`, `repo`, `asset_patterns`, `aur_package`, and `aur_architectures` are not valid as resolver configuration fields in the new schema.

The schema currently recognizes `url`, `github`, `aur`, `sourceforge`, and `script` source keys.

A software entry may keep source options that no architecture currently selects. Those unselected source options are valid raw configuration, but normalization drops them; refresh and build code only receive the active source selected by each architecture.

## Shorthand Entry

Use the shorthand when every architecture uses the same source resolver and update policy.

```toml
# packages/bat.toml
homepage = "https://github.com/sharkdp/bat"
architectures = ["amd64", "arm64"]
source = "github"
update_policy = "track"

[sources.github]
repo = "sharkdp/bat"

[sources.github.asset_patterns]
amd64 = "bat_*_amd64.deb"
arm64 = "bat_*_arm64.deb"
```

The loader normalizes this to the same internal architecture plan as:

```toml
[architectures]
amd64 = { source = "github", update_policy = "track" }
arm64 = { source = "github", update_policy = "track" }
```

For a fixed GitHub release where all architectures use the same release tag, keep `release_tag` scalar:

```toml
# packages/example-fixed.toml
homepage = "https://github.com/example/app"
architectures = ["amd64", "arm64"]
source = "github"
update_policy = "fixed"

[sources.github]
repo = "example/app"
release_tag = "v1.2.3"

[sources.github.asset_patterns]
amd64 = "app_*_amd64.deb"
arm64 = "app_*_arm64.deb"
```

## Explicit Entry

Use the explicit architecture plan when different architectures use different source resolvers, different update policies, or different fixed release tags.

```toml
# packages/example-mixed/index.toml
homepage = "https://example.test/app"

[architectures]
amd64 = { source = "aur", update_policy = "track" }
arm64 = { source = "github", update_policy = "fixed" }

[sources.aur]
package = "example-app-bin"

[sources.aur.asset_patterns]
amd64 = "example-app_*_amd64.deb"

[sources.github]
repo = "example/app"

[sources.github.asset_patterns]
arm64 = "app_*_arm64.deb"

[sources.github.release_tags]
arm64 = "v1.2.3"
```

## SourceForge Entry

Use a SourceForge source when the upstream publishes downloadable `.deb` artifacts under a files directory rather than GitHub Releases or AUR metadata.

```toml
# packages/deadbeef.toml
homepage = "https://deadbeef.sourceforge.io/"
architectures = ["amd64", "arm64"]
source = "sourceforge"
update_policy = "track"

[sources.sourceforge]
project = "deadbeef"
path = "Builds/master/linux"

[sources.sourceforge.asset_regexes]
amd64 = "deadbeef-static_.+_amd64\\.deb"
arm64 = "deadbeef-static_.+_arm64\\.deb"
```

The explicit plan and shorthand are mutually exclusive. If `architectures` is a list, top-level `source` and `update_policy` are required. If `architectures` is a map, top-level `source` and `update_policy` are forbidden.

## Validation Rules

- Each software entry must declare at least one architecture.
- Every architecture plan must reference a configured source resolver under `sources`.
- The update policy must be compatible with the selected source resolver for that architecture.
- Unselected source options are allowed and are omitted from the normalized entry model.
- `url` supports `fixed`.
- `github` supports `fixed` and `track`.
- `aur` supports `track`.
- `sourceforge` supports `fixed` and `track`.
- `script` is accepted by the config schema and normalized model, but refresh currently raises `unsupported source resolver "script"` because the runtime resolver is not implemented yet.
- Source identity fields are validated when a source option exists. Per-architecture resolver fields are validated only for architectures that select that source.
- Extra fields are forbidden at every model layer.

## Implemented Model Shape

The loader in [`src/apt_index/config.py`](../src/apt_index/config.py) uses two model layers:

- a raw model that accepts TOML syntax, including shorthand fields.
- a normalized architecture-centric model that refresh and build code use exclusively.

The normalized model does not preserve source-level shorthand. For example, raw GitHub TOML may contain `release_tag` or `release_tags`, but the normalized per-architecture GitHub source contains only the release tag selected for that architecture.

```python
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Literal, Self, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


Arch = Literal["amd64", "arm64"]
ResolverKey = Literal["url", "github", "aur", "sourceforge", "script"]
UpdatePolicy = Literal["fixed", "track"]
T = TypeVar("T")


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

def normalize_architecture_plans(
    raw_architectures: list[Arch] | dict[Arch, ArchitecturePlan],
    shorthand_source: ResolverKey | None,
    shorthand_update_policy: UpdatePolicy | None,
) -> dict[Arch, ArchitecturePlan]:
    if isinstance(raw_architectures, list):
        if shorthand_source is None or shorthand_update_policy is None:
            raise ValueError("architecture shorthand requires source and update_policy")
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
    return SourceforgeArchSource(
        project=raw.project,
        path=raw.path,
        asset_regex=required_arch_value(raw.asset_regexes, arch, "SourceForge asset regex"),
    )


def required_arch_value(values: Mapping[Arch, str], arch: Arch, label: str) -> str:
    try:
        return values[arch]
    except KeyError as exc:
        raise ValueError(f"{arch}: missing {label}") from exc


def expect_source(source: object, model: type[T]) -> T:
    if not isinstance(source, model):
        raise TypeError(f"expected {model.__name__}")
    return cast(T, source)


SourceNormalizer = Callable[[object, Arch, UpdatePolicy], ActiveSource]

SOURCE_NORMALIZERS: Mapping[ResolverKey, SourceNormalizer] = {
    "url": normalize_url_source,
    "github": normalize_github_source,
    "aur": normalize_aur_source,
    "sourceforge": normalize_sourceforge_source,
    "script": normalize_script_source,
}
```

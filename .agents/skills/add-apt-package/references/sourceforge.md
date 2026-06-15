# SourceForge package entries

Use this path when the upstream publishes `.deb` downloads from a SourceForge files directory instead of GitHub Releases or AUR metadata.

## When SourceForge is the right resolver

Choose `sourceforge` when:

- the project's downloadable artifacts live under `https://sourceforge.net/projects/<project>/files/...`
- the Debian artifacts can be identified by filename
- SourceForge's page metadata is sufficient to select one file per architecture

## Patterns in this repo

Read `packages/deadbeef.toml` before editing.

Typical shorthand entry:

```toml
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

## How to derive the config

1. Identify the SourceForge project slug.
2. Identify the files subdirectory path below `files/`.
3. Write a regex that fully matches exactly one downloadable `.deb` per architecture.

The regex is a full match, not a substring search. Write it narrowly enough that a second matching artifact would be surprising.

Good examples:

- `deadbeef-static_.+_amd64\\.deb`
- `myapp_[0-9.]+_arm64\\.deb`

Bad examples:

- `.*amd64.*`
- `.+\\.deb`

## Update policy

- Use `track` when the project directory always contains the current release you want to follow.
- Use `fixed` only when you have a stable directory snapshot or a user explicitly wants a pinned artifact set.

## Validation target

After editing, prove that `sources.resolve_candidate()` picks exactly one downloadable artifact per architecture.

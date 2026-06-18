# Apt Index

This context describes a personal APT package index for installing third-party Debian packages without redistributing package files.

## Language

**Personal APT index**:
An APT-compatible repository owned by one person for installing selected third-party packages on Debian and Ubuntu systems.
_Avoid_: deb mirror, package mirror

**Upstream package file**:
The original `.deb` file published by a vendor, GitHub release, or other upstream source.
_Avoid_: mirrored deb, stored deb

**Redirected package download**:
A package download where this project serves APT metadata but redirects the `.deb` request to the upstream package file URL.
_Avoid_: package hosting, package mirroring

**Upstream package name**:
The installable APT package name declared inside the upstream `.deb` control metadata, not the `.deb` filename or the configured software entry name.
_Avoid_: file name, release asset name, alias name

**Software entry**:
A configured third-party software source that this project tracks and indexes. A software entry may resolve to an upstream package name that differs from the entry name.
_Avoid_: package name, install name

**Software entry name**:
The stable lowercase identifier for a software entry. It uses only letters, digits, hyphens, and underscores, and starts and ends with a letter or digit.
_Avoid_: package name, path name

**Repository configuration**:
The repository-level settings that describe the personal APT index as a whole, separate from individual software entries.
_Avoid_: package configuration, package list

**Shared suite**:
A single APT suite intended to be used across supported Debian and Ubuntu releases when upstream packages are not distribution-specific.
_Avoid_: distro suite, codename-specific repository

**Entry architecture**:
A package architecture that a software entry publishes in the personal APT index.
_Avoid_: required architecture, optional architecture

**Entry architecture plan**:
The per-architecture plan that selects an active source resolver and update policy for each entry architecture.
_Avoid_: global source, global update policy

**Architecture artifact**:
The upstream package file resolved for one package architecture of a software entry.
_Avoid_: universal deb

**Published package state**:
The read-only in-memory view of the current published artifacts derived from the index lockfile and used to generate publish-time outputs.
_Avoid_: build cache, deploy manifest

**Published artifact**:
One artifact record inside the published package state, including its software entry identity, configured architecture, upstream package metadata, and virtual package path.
_Avoid_: raw lockfile dict, deploy file

**Repository metadata signature**:
A signature over APT repository metadata that lets clients verify the package index before trusting package URLs and hashes.
_Avoid_: trusted repository flag, unsigned source

**Index lockfile**:
A generated state file that records the resolved upstream package artifacts used by the current APT index.
_Avoid_: source configuration, cache file, log file

**Refresh commit**:
An automated commit produced by the daily refresh workflow when tracked software entries resolve to newer upstream artifacts.
_Avoid_: update pull request, manual review gate

**Health report**:
A generated diagnostic file that records track refresh health and artifact health for software entries without being the source of published APT metadata.
_Avoid_: lockfile, log file

**Track health report**:
A generated diagnostic file that records track refresh health for software entries.
_Avoid_: artifact health report

**Artifact health report**:
A generated diagnostic file that records artifact health for resolved package files.
_Avoid_: track health report

**Track refresh health**:
The result of checking and resolving an updated upstream version for a tracked entry architecture during a refresh.
_Avoid_: artifact health, package health

**Artifact health**:
The result of checking whether a resolved upstream package file is downloadable and still matches the recorded package metadata and hashes.
_Avoid_: track refresh health

**Update policy**:
The policy that decides whether an entry architecture stays fixed to a configured upstream version or tracks newer upstream versions during refresh.
_Avoid_: APT pinning

**Source resolver**:
The mechanism that resolves a software entry and architecture to an upstream package file URL and version candidate.
_Avoid_: update mode, package source

**Source option**:
One configured resolver option for a software entry. A software entry may keep multiple source options, and each entry architecture selects one of them during a refresh.
_Avoid_: source resolver, fallback source

**Active source resolver**:
The source option selected for one entry architecture during a refresh.
_Avoid_: enabled source, package source

**URL source resolver**:
A source resolver that uses configured upstream `.deb` artifact URLs.
_Avoid_: single URL resolver, generic download

**GitHub source resolver**:
A source resolver that uses GitHub Releases to discover upstream `.deb` artifacts.
_Avoid_: GitHub tag parser

**AUR source resolver**:
A source resolver that uses AUR package metadata to discover upstream `.deb` artifacts for Debian or Ubuntu installation.
_Avoid_: AUR package build, PKGBUILD adaptation

**Script source resolver**:
A future source resolver that lets project-owned code discover upstream `.deb` artifacts for sources not covered by URL, GitHub, or AUR.
_Avoid_: package generator, metadata writer

**AUR metadata**:
The static `.SRCINFO` data read from an AUR package repository to discover upstream `.deb` artifacts.
_Avoid_: PKGBUILD execution

**Redirect rules**:
Generated static data that maps redirected package download requests to upstream package file URLs. Redirect rules are organized by APT component and software entry rather than as one global table.
_Avoid_: Worker routing code, global redirect table

**Download statistics**:
Public aggregate counts of redirected package download `GET` requests derived from Cloudflare HTTP request analytics for package download paths.
_Avoid_: Worker event counter, resolver health

**Deployable APT tree**:
The generated static APT repository directory uploaded to Cloudflare Pages.
_Avoid_: source configuration, committed state

**Resolver capability**:
The set of update policies a source resolver supports for a software entry.
_Avoid_: mode type

**Release tag**:
The GitHub Release tag used as the fixed upstream version identity for a GitHub release source resolver.
_Avoid_: package version, asset URL

**Latest release**:
The GitHub Release selected by GitHub's latest-release semantics for a tracked GitHub release source resolver.
_Avoid_: newest tag, newest asset

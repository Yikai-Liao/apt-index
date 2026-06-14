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

**Shared suite**:
A single APT suite intended to be used across supported Debian and Ubuntu releases when upstream packages are not distribution-specific.
_Avoid_: distro suite, codename-specific repository

**Required architecture**:
A package architecture that must resolve for a software entry during index generation.
_Avoid_: supported architecture

**Optional architecture**:
A package architecture that may resolve for a software entry; absence does not fail index generation.
_Avoid_: best-effort architecture

**Architecture artifact**:
The upstream package file resolved for one package architecture of a software entry.
_Avoid_: universal deb

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
The result of checking and resolving an updated upstream version for a tracked software entry during a refresh.
_Avoid_: artifact health, package health

**Artifact health**:
The result of checking whether a resolved upstream package file is downloadable and still matches the recorded package metadata and hashes.
_Avoid_: track refresh health

**Update policy**:
The policy that decides whether a software entry stays fixed to a configured upstream version or tracks newer upstream versions during refresh.
_Avoid_: APT pinning

**Source resolver**:
The mechanism that resolves a software entry and architecture to an upstream package file URL and version candidate.
_Avoid_: update mode, package source

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
A generated static data file that maps repository package download paths to upstream package file URLs.
_Avoid_: Worker routing code

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

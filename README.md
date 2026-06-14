# Apt Index

Apt Index is a personal APT repository for installing selected third-party Debian packages on Debian and Ubuntu systems without mirroring or redistributing the upstream `.deb` files.

The repository publishes standard APT metadata, while package downloads are served through generated redirect rules that point clients to the original upstream package files.

## Goals

- Provide a normal `apt install <package>` workflow for packages that are not available in the official Debian or Ubuntu repositories.
- Avoid storing or redistributing upstream `.deb` files.
- Keep package selection in a single TOML configuration file.
- Support both fixed-version packages and rolling tracked packages.
- Refresh tracked packages automatically from GitHub Actions.
- Target a shared APT suite for Debian 12-13 and Ubuntu 22.04-26.04 where upstream packages are not distribution-specific.
- Require signed APT repository metadata from the first release.

## Non-Goals

- This is not a Debian package mirror.
- This does not rewrite upstream package names, dependencies, maintainer scripts, or package metadata.
- This does not execute AUR `PKGBUILD` scripts.
- This does not repackage upstream software to create aliases such as `apt install feishu` when the upstream Debian package name is different.
- This does not commit the generated `dist/` APT tree to the repository.

## Architecture

```text
packages.toml
    |
    v
Python refresh/build tools
    |
    +--> apt-index.lock.json
    +--> track_health.json
    +--> artifact_health.json
    |
    v
Generated deployable APT tree
    |
    +--> dists/stable/main/binary-amd64/Packages.gz
    +--> dists/stable/main/binary-arm64/Packages.gz
    +--> dists/stable/Release
    +--> dists/stable/InRelease
    +--> redirect_rules.json
    |
    v
Cloudflare Pages + Worker
```

Cloudflare Pages serves the static APT metadata and generated redirect data. The Worker handles virtual package download paths and redirects them to the original upstream `.deb` URLs.

## Package Identity

The installable package name comes from the upstream `.deb` control metadata, not from:

- the configured package entry name
- the `.deb` filename
- the GitHub release asset name
- the AUR package name

For example, an upstream file named `Feishu-linux_x64-7.66.10.deb` may declare:

```text
Package: bytedance-feishu-stable
Version: 7.66.10-0
Architecture: amd64
```

In that case the APT package name is `bytedance-feishu-stable`.

## Configuration Model

The main configuration is a single TOML file. Each package entry has:

- an `update_policy`
- a `source.type`
- source-specific fields
- architecture-specific artifact selection

Supported source resolver keys:

| Source | Purpose | Update policies |
| --- | --- | --- |
| `url` | Use configured upstream `.deb` URLs | `fixed` |
| `github` | Resolve `.deb` assets from GitHub Releases | `fixed`, `track` |
| `aur` | Read AUR `.SRCINFO` to discover upstream `.deb` URLs | `track` |
| `script` | Reserved for future custom resolvers | `track` |

Example:

```toml
suite = "stable"
component = "main"
required_architectures = ["amd64"]
optional_architectures = ["arm64"]

[packages.dust]
update_policy = "track"

[packages.dust.source]
type = "github"
repo = "bootandy/dust"
asset_patterns.amd64 = "*.deb"
asset_patterns.arm64 = "*.deb"

[packages.feishu]
update_policy = "track"

[packages.feishu.source]
type = "aur"
package = "feishu-bin"

[packages.some-vendor-app]
update_policy = "fixed"

[packages.some-vendor-app.source]
type = "url"
urls.amd64 = "https://example.com/vendor-app_1.2.3_amd64.deb"
urls.arm64 = "https://example.com/vendor-app_1.2.3_arm64.deb"
```

## Update Policies

`fixed` means the package is held at a configured upstream version or artifact.

`track` means the daily refresh workflow resolves the latest upstream artifact and updates the lockfile when the package changes.

For GitHub sources:

- `fixed` pins a GitHub release tag.
- `track` follows GitHub's latest-release semantics.

For AUR sources:

- the resolver reads static `.SRCINFO`
- the resolver uses AUR only to discover upstream `.deb` URLs and checksums
- `PKGBUILD` is never executed
- AUR package metadata such as `provides`, `conflicts`, install scripts, and file modifications is not inherited

## Architectures

The first release targets:

- `amd64` as a required architecture
- `arm64` as an optional architecture

If a package cannot resolve an `amd64` artifact, that package cannot be newly published. If `arm64` is unavailable, the refresh can continue without blocking the `amd64` package.

## Generated State

The repository keeps generated state files that are useful for review, automation, and diagnostics.

`apt-index.lock.json` records the resolved artifacts currently used by the published APT index. It includes upstream URLs, package control metadata, sizes, hashes, and architecture information.

`track_health.json` records whether tracked package refresh checks succeeded.

`artifact_health.json` records whether resolved upstream package files are still downloadable and still match the recorded hashes and sizes.

The deployable `dist/` tree is generated in CI and uploaded to Cloudflare Pages, but it is not committed.

## Refresh Workflow

The daily GitHub Actions workflow:

1. Reads `packages.toml`.
2. Resolves tracked package updates.
3. Keeps the previous lock entry for a package if that package's track refresh fails.
4. Continues refreshing unrelated packages.
5. Checks artifact health for both fixed and tracked packages.
6. Commits changed generated state files directly to the default branch.
7. Builds and signs the deployable APT tree.
8. Uploads the generated tree to Cloudflare Pages.

This repository intentionally uses a rolling self-managed model. Successful refreshes are committed directly instead of opening pull requests.

## APT Metadata

APT metadata is generated from the lockfile and extracted upstream `.deb` control metadata.

The project does not use `reprepro`, `dpkg-scanpackages`, or `apt-ftparchive packages` as the primary package index generator because those tools are built around local `.deb` package trees. Apt Index publishes virtual package paths that redirect to upstream artifacts.

## Security Model

APT repository metadata is signed from the first release. Clients should use `signed-by` when adding the repository and should not use `trusted=yes`.

The repository controls:

- package metadata
- package hashes
- package sizes
- virtual download paths
- redirect targets

The upstream source controls the actual `.deb` contents. Artifact health checks detect when an upstream artifact disappears or no longer matches the recorded lockfile data.

## Deployment Model

Cloudflare Pages hosts the generated static APT tree.

Cloudflare Worker code handles redirected package download paths and reads generated static redirect rules. Redirect rules are data, not Worker code, so daily package updates do not require redeploying the Worker bundle.

## Implementation Notes

- Refresh/build tools are written in Python.
- Worker code can use TypeScript.
- `dist/` is a deploy artifact, not source-controlled state.
- The shared suite is expected to be `stable main` unless the configuration says otherwise.

## Design Records

Project decisions are documented in [`docs/adr`](./docs/adr). Domain language is documented in [`CONTEXT.md`](./CONTEXT.md).

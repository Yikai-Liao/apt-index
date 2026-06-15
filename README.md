# Apt Index

Apt Index is a personal APT repository for installing selected third-party Debian packages on Debian and Ubuntu systems without mirroring or redistributing the upstream `.deb` files.

The repository publishes standard APT metadata, while package downloads are served through generated redirect rules that point clients to the original upstream package files.

## Usage

The repository is published at:

```text
https://deb.lyk-ai.com
```

```sh
curl -fsSL https://deb.lyk-ai.com/key.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/lyk-ai-apt.gpg

echo "deb [signed-by=/usr/share/keyrings/lyk-ai-apt.gpg] https://deb.lyk-ai.com stable main" \
  | sudo tee /etc/apt/sources.list.d/lyk-ai.list

sudo apt update
sudo apt install <package-name>
```

Package selection is maintained in [`packages.toml`](./packages.toml). The resolved upstream artifacts and installable Debian package names are recorded in [`apt-index.lock.json`](./apt-index.lock.json).

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
    +--> download_stats.json
    +--> _routes.json
    +--> _worker.js
    |
    v
Cloudflare Pages + Worker
```

Cloudflare Pages serves the static APT metadata, signing key, generated redirect data, and public download statistics. `_routes.json` routes only `/pool/*` package download paths to the Worker; `/dists/*` and `/key.asc` are served directly as static files. The Worker reads `redirect_rules.json` and redirects virtual package download paths to the original upstream `.deb` URLs. If the `DOWNLOADS` Workers Analytics Engine binding is configured, the Worker also records successful package redirect requests.

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
- a `source` resolver key
- resolver-specific fields
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

[packages.bat]
update_policy = "track"
source = "github"
repo = "sharkdp/bat"
asset_patterns.amd64 = "bat_*_amd64.deb"
asset_patterns.arm64 = "bat_*_arm64.deb"
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

The current release targets:

- `amd64` as a required architecture
- `arm64` as an optional architecture where the upstream source publishes a compatible `.deb`

If a package cannot resolve an `amd64` artifact, that package cannot be newly published. Optional architectures are resolved only for package entries that declare an architecture-specific artifact selector, so packages without upstream arm64 `.deb` assets can stay published for `amd64`.

## Generated State

The repository keeps generated state files that are useful for review, automation, and diagnostics.

`apt-index.lock.json` records the resolved artifacts currently used by the published APT index. It includes upstream URLs, package control metadata, sizes, hashes, and architecture information.

`track_health.json` records whether tracked package refresh checks succeeded.

`artifact_health.json` records whether resolved upstream package files are still reachable. Daily refreshes use a lightweight `HEAD` or range request for unchanged artifacts and compare the remote size when available. New artifacts and explicit full checks download the package and verify the recorded hash and size.

`download_stats.json` records public, aggregated download request counts exported from Workers Analytics Engine. It is a deploy artifact, not committed generated state.

The deployable `dist/` tree is generated in CI and uploaded to Cloudflare Pages, but it is not committed.

## Refresh Workflow

The daily GitHub Actions workflow:

1. Reads `packages.toml`.
2. Resolves tracked package updates with limited parallel workers, reusing lockfile metadata when the upstream artifact URL, version, and asset name are unchanged.
3. Keeps the previous lock entry for a package if that package's track refresh fails.
4. Continues refreshing unrelated packages.
5. Checks artifact health for both fixed and tracked packages. Daily checks are lightweight for unchanged artifacts; `apt-index refresh --full-artifact-check` downloads and hashes every locked artifact.
6. Commits changed generated state files directly to the default branch.
7. Builds and signs the deployable APT tree.
8. Exports public download statistics to `dist/download_stats.json`.
9. Uploads the generated tree to Cloudflare Pages.

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

Download request statistics use Workers Analytics Engine:

- Configure an Analytics Engine binding named `DOWNLOADS` on the Cloudflare Pages project.
- Use the dataset name `apt_index_downloads`.
- The Worker records `GET` and `HEAD` redirect requests; the public JSON and page counts only aggregate `GET` requests.
- Enable Workers Analytics Engine for the Cloudflare account before deploying the binding.
- The GitHub Actions `CLOUDFLARE_API_TOKEN` must include `Account > Account Analytics > Read` so it can query the Analytics Engine SQL API.

## Implementation Notes

- Refresh/build tools are written in Python with `uv`, `typer`, and `loguru`.
- `apt-index refresh` and `apt-index all` accept `--jobs`/`-j`; the default is 4 workers, or `APT_INDEX_JOBS` when set.
- `apt-index refresh --full-artifact-check` and `apt-index all --full-artifact-check` force a full artifact download and hash verification for every locked artifact.
- The current Worker is generated JavaScript in the deployable APT tree.
- `dist/` is a deploy artifact, not source-controlled state.
- The shared suite is expected to be `stable main` unless the configuration says otherwise.
- Local verification is available with `docker/apt-index-test.Dockerfile`.

## Design Records

Project decisions are documented in [`docs/adr`](./docs/adr). Domain language is documented in [`CONTEXT.md`](./CONTEXT.md).

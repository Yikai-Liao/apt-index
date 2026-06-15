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

The repository reads repository settings from [`apt-index.toml`](./apt-index.toml) and software entries from [`packages/`](./packages/); see [`docs/configuration.md`](./docs/configuration.md).

The resolved upstream artifacts and installable Debian package names are recorded in [`apt-index.lock.json`](./apt-index.lock.json).

## Goals

- Provide a normal `apt install <package>` workflow for packages that are not available in the official Debian or Ubuntu repositories.
- Avoid storing or redistributing upstream `.deb` files.
- Keep repository-level configuration separate from per-software-entry configuration.
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
apt-index.toml + packages/
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
    +--> redirect-rules/main/<entry>.json
    +--> redirect-rules/snapshot.json.zst
    +--> download_stats.json
    +--> _routes.json
    +--> _worker.js
    |
    v
Cloudflare Pages + Worker
```

`track_health.json` and `artifact_health.json` are generated diagnostics. They are published with the generated `dist/` tree, but they are ignored by Git and are not source-controlled state.

Cloudflare Pages serves the static APT metadata, signing key, generated redirect data, and public download statistics. `_routes.json` routes only `/pool/*` package download paths to the Worker; `/dists/*` and `/key.asc` are served directly as static files. The Worker reads the per-entry redirect shard for the requested virtual package path and returns a cacheable `302` redirect to the original upstream `.deb` URL.

Static redirect shards and `redirect-rules/snapshot.json.zst` are published with `_headers` rules that keep browser caching conservative but give Cloudflare's edge a long TTL. Package download redirects themselves are cached as Worker-generated `302` responses in the Cache API, and missing package paths get a short-lived cached `404` so repeated probes do not reread the shard on every miss.

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

The split configuration model, including shorthand and explicit entry examples plus the Pydantic validation prototype, is documented in [`docs/configuration.md`](./docs/configuration.md).

Each software entry has:

- an entry homepage
- an architecture plan
- source options under `sources`

Supported source resolver keys:

| Source | Purpose | Update policies |
| --- | --- | --- |
| `url` | Use configured upstream `.deb` URLs | `fixed` |
| `github` | Resolve `.deb` assets from GitHub Releases | `fixed`, `track` |
| `aur` | Read AUR `.SRCINFO` to discover upstream `.deb` URLs | `track` |
| `script` | Reserved for future custom resolvers | `track` |

Example:

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

## Update Policies

`fixed` means the package is held at a configured upstream version or artifact.

`track` means the daily refresh workflow resolves the latest upstream artifact and updates the lockfile when the package changes.

For GitHub sources:

- `fixed` pins a GitHub release tag.
- `track` follows GitHub's latest-release semantics.

For AUR sources:

- the resolver reads static `.SRCINFO`
- the resolver uses AUR only to discover upstream `.deb` URLs and checksums
- every enabled architecture must have an `asset_patterns.<arch>` glob that matches the `.deb` source asset name or URL
- `PKGBUILD` is never executed
- AUR package metadata such as `provides`, `conflicts`, install scripts, and file modifications is not inherited

## Architectures

Each software entry declares the entry architectures it publishes. Refresh failures are recorded per entry architecture: one architecture can update while another keeps its previous architecture artifact or fails without blocking unrelated architectures.

## Generated State

The repository keeps generated state files that are useful for review, automation, and diagnostics.

`apt-index.lock.json` records the resolved artifacts currently used by the published APT index. It includes upstream URLs, package control metadata, sizes, hashes, and architecture information.

`track_health.json` records whether tracked package refresh checks succeeded.

`artifact_health.json` records whether resolved upstream package files are still reachable. Daily refreshes use a lightweight `HEAD` or range request for unchanged artifacts and compare the remote size when available. New artifacts and explicit full checks download the package and verify the recorded hash and size.

`download_stats.json` records public, aggregated `GET /pool/*` request counts exported from Cloudflare HTTP request analytics. It is a deploy artifact, not committed generated state.

The deployable `dist/` tree is generated in CI and uploaded to Cloudflare Pages, but it is not committed.

## Refresh Workflow

The daily GitHub Actions workflow:

1. Reads `apt-index.toml` and `packages/`.
2. Resolves tracked entry architecture updates with limited parallel workers, reusing lockfile metadata when the upstream artifact URL, version, and asset name are unchanged.
3. Keeps the previous architecture artifact if that entry architecture's track refresh fails.
4. Continues refreshing unrelated packages.
5. Checks artifact health for both fixed and tracked packages. Daily checks are lightweight for unchanged artifacts; `apt-index refresh --full-artifact-check` downloads and hashes every locked artifact.
6. Commits changed generated state files directly to the default branch.
7. Builds and signs the deployable APT tree, including per-entry redirect shards and `redirect-rules/snapshot.json.zst`.
8. Compares the new redirect snapshot with the previously deployed Cloudflare snapshot and plans which cached package URLs and redirect-rule assets need purging.
9. Exports public download statistics to `dist/download_stats.json`.
10. Uploads the generated tree to Cloudflare Pages.
11. Purges cached package redirect URLs whose redirect target changed, newly appeared package URLs that may have cached `404` misses, affected redirect shards, and the redirect snapshot asset.

This repository intentionally uses a rolling self-managed model. Successful refreshes are committed directly instead of opening pull requests.

## APT Metadata

APT metadata is generated from the lockfile and extracted upstream `.deb` control metadata.

The project does not use `reprepro`, `dpkg-scanpackages`, or `apt-ftparchive packages` as the primary package index generator because those tools are built around local `.deb` package trees. Apt Index publishes virtual package paths that redirect to upstream artifacts.

## Security Model

APT repository metadata is signed from the first release. Clients should use `signed-by` when adding the repository and should not use `trusted=yes`.

Signing uses a long-lived GPG key named `Apt Index <apt-index@lyk-ai.com>`. The private key is not committed. Local builds load it from `.env`; GitHub Actions loads the same key from repository secrets. If the key is not already present in `.apt-index-gnupg/` and no private-key environment variable is provided, `apt-index build` fails instead of generating a new key.

To create the signing key once:

```sh
gpg --quick-generate-key "Apt Index <apt-index@lyk-ai.com>" rsa3072 sign 0
```

To export it for `.env` and GitHub Actions:

```sh
gpg --armor --export-secret-keys "Apt Index <apt-index@lyk-ai.com>" | base64 | tr -d '\n'
```

Set the output as `APT_INDEX_GPG_PRIVATE_KEY_B64`. If the key has a passphrase, also set `APT_INDEX_GPG_PASSPHRASE`. Locally, copy `.env.example` to `.env` and fill those values. In GitHub, create repository secrets with the same names.

The repository controls:

- package metadata
- package hashes
- package sizes
- virtual download paths
- redirect targets

The upstream source controls the actual `.deb` contents. Artifact health checks detect when an upstream artifact disappears or no longer matches the recorded lockfile data.

## Deployment Model

Cloudflare Pages hosts the generated static APT tree.

Cloudflare Worker code handles redirected package download paths and reads generated static redirect shards. Redirect rules are data, not Worker code, so daily package updates do not require changing the Worker logic. The Worker stores generated `302` redirect responses in Cloudflare's Cache API with a long shared TTL; the publish workflow purges only cached package download URLs whose redirect target changed.

Download request statistics use Cloudflare HTTP request analytics:

- The public JSON and page counts aggregate only `GET /pool/*` requests.
- `HEAD` requests are excluded because they can represent probes, checks, or cache validation rather than a package download.
- The GitHub Actions `CLOUDFLARE_API_TOKEN` must be able to list zones and query GraphQL HTTP request analytics. If it also has zone cache purge permission, the workflow actively purges changed package redirect URLs; otherwise the purge step logs a warning and leaves existing cached redirects to expire.
- `CLOUDFLARE_ZONE_ID` is optional; when it is absent, the tooling resolves the zone from the repository hostname.

## Implementation Notes

- Refresh/build tools are written in Python with `uv`, `typer`, and `loguru`.
- `apt-index refresh` and `apt-index all` accept `--jobs`/`-j`; the default is 4 workers, or `APT_INDEX_JOBS` when set.
- `apt-index refresh --full-artifact-check` and `apt-index all --full-artifact-check` force a full artifact download and hash verification for every locked artifact.
- The current Worker is generated JavaScript in the deployable APT tree.
- `dist/` is a deploy artifact, not source-controlled state.
- The shared suite is expected to be `stable main` unless the configuration says otherwise.
- Local verification is available with `docker/apt-index-test.Dockerfile`.

## Design Records

Project decisions are documented in [`docs/adr`](./docs/adr). Domain language is documented in [`CONTEXT.md`](./CONTEXT.md). The target configuration model is documented in [`docs/configuration.md`](./docs/configuration.md), with the implementation plan in [`docs/configuration-migration-plan.md`](./docs/configuration-migration-plan.md).

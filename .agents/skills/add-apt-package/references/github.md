# GitHub package entries

Use this path when the upstream publishes Debian artifacts as GitHub Release assets.

## When GitHub is the right resolver

Choose `github` when:

- the upstream release assets include `.deb` files directly
- asset names are stable enough to capture with an arch-specific glob
- the release identity is a GitHub tag

Prefer GitHub over AUR if the official GitHub Releases page already exposes the exact `.deb` artifacts you need.

## Patterns in this repo

Read one or two existing GitHub examples such as:

- `packages/bat.toml`
- `packages/fastfetch.toml`
- `packages/ripgrep.toml`

Typical shorthand entry:

```toml
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

## Decision rules

- Use `update_policy = "track"` when the user wants the package to follow the latest upstream release.
- Use `update_policy = "fixed"` only when the user gives a specific release/tag to hold.
- For `track`, start from the latest stable release endpoint. Do not enumerate full release history unless `latest` is insufficient to answer a concrete question.
- For fixed GitHub entries:
  - if all architectures use the same tag, set scalar `release_tag`
  - if architectures need different tags, use the explicit architecture plan plus `release_tags.<arch>`

## Fast inspection

Prefer the bundled helper script:

```bash
uv run python .agents/skills/add-apt-package/scripts/github_latest_assets.py <owner/repo>
```

It prints the latest release tag, prerelease/draft flags, and asset names. By default it filters to `.deb` assets only.

If you need the raw API directly, use `repos/<repo>/releases/latest` before any paginated release listing.

## How to derive `asset_patterns`

1. Inspect the release asset names.
2. Keep the glob as narrow as possible while still surviving future patch releases.
3. Match the actual `.deb` asset name, not the marketing product name.
4. Include the architecture token exactly as released upstream.

Good examples:

- `bat_*_amd64.deb`
- `fastfetch-linux-amd64.deb`

Bad examples:

- `*.deb`
- `*linux*.deb`

## Common traps

- If GitHub already publishes the exact `.deb`, prefer `github` over an indirect source like AUR.
- If upstream has a newer prerelease but `track` should follow stable, do not expand into full release-history exploration just to mention the prerelease. The runtime resolver uses the latest stable release endpoint.

## Validation target

After editing, prove that `sources.resolve_candidate()` returns the intended GitHub asset for every architecture.

# AUR package entries

Use this path when AUR `.SRCINFO` is the cleanest way to discover upstream `.deb` URLs and checksums.

## When AUR is the right resolver

Choose `aur` when:

- the package is already modeled in AUR as a binary package
- `.SRCINFO` exposes direct upstream `.deb` URLs
- the repository should track those upstream artifacts over time

Do not use AUR to inherit package naming, dependencies, install scripts, or PKGBUILD behavior. This repository only uses `.SRCINFO` as a discovery layer.
If the official upstream already publishes the exact `.deb` cleanly, prefer that direct source over AUR.

## Patterns in this repo

Read one or two existing AUR examples such as:

- `packages/wechat.toml`
- `packages/qq.toml`
- `packages/vscode.toml`
- `packages/google-chrome.toml`

Typical shorthand entry:

```toml
homepage = "https://meeting.tencent.com/download/"
architectures = ["amd64", "arm64"]
source = "aur"
update_policy = "track"

[sources.aur]
package = "wemeet-bin"

[sources.aur.asset_patterns]
amd64 = "wemeet-*-x86_64.deb"
arm64 = "wemeet-*-aarch64.deb"
```

## How to inspect AUR

Prefer `.SRCINFO` directly:

```sh
curl -fsSL "https://aur.archlinux.org/cgit/aur.git/plain/.SRCINFO?h=<package>"
```

Do this even if the HTML package page is blocked by bot protection. The runtime resolver also reads `.SRCINFO`, so this is the source of truth that matters.

## Matching rules

The resolver checks the configured pattern against:

- the asset name
- the URL
- the raw `source` or `source_<arch>` value

That means a pattern may target the renamed asset (`foo_amd64.deb`) or a distinctive URL fragment, but prefer matching the asset name when possible because it is easier to review.

## Architecture mapping

Watch for AUR naming differences:

- repo architecture `amd64` often maps to upstream/AUR `x86_64`
- repo architecture `arm64` often maps to upstream/AUR `aarch64` or `arm64`

Do not normalize those strings in the pattern. Match what upstream actually publishes.

## Common traps

- Some AUR packages mix `.deb`, AppImage, or other artifact types across architectures. Do not force the AUR resolver in that case unless AUR is the only clean source of the requested `.deb`.
- `obsidian`-style packages are a good example: one architecture may expose a `.deb` while another only exposes AppImage. In that case, either narrow the architecture set or switch to a cleaner direct upstream source.

## Validation target

After editing, prove that `sources.build_candidate_resolver()` returns:

- the expected `.deb` asset for each declared architecture
- the upstream version from `.SRCINFO`
- the checksum algorithm and presence of checksum data

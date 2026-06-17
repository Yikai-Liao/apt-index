# Direct URL package entries

Use this path when the upstream provides a fixed `.deb` URL and there is no better structured resolver.

## When URL is the right resolver

Choose `url` when:

- the user wants a pinned package version
- the upstream exposes a direct `.deb` URL per architecture
- GitHub Releases, AUR, and SourceForge do not fit cleanly

Do not use `url` for rolling packages that should update automatically. `url` only supports `fixed`.

## Shape

Use the explicit architecture map if different architectures point at different fixed URLs.

```toml
homepage = "https://example.test/downloads"

[architectures]
amd64 = { source = "url", update_policy = "fixed" }
arm64 = { source = "url", update_policy = "fixed" }

[sources.url.urls]
amd64 = "https://example.test/myapp_1.2.3_amd64.deb"
arm64 = "https://example.test/myapp_1.2.3_arm64.deb"
```

If only one architecture is supported, declare only that architecture.

## Validation target

After editing, prove that `sources.build_candidate_resolver()` returns the configured URL and a fixed upstream version marker for each declared architecture.

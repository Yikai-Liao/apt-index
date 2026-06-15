---
name: add-apt-package
description: Add or update Apt Index package entries for upstream Debian packages. Use this skill whenever the user asks to add a new package to this repository, wire up a new upstream `.deb` source, convert GitHub/AUR/SourceForge download details into `packages/*.toml`, or verify that a package entry resolves correctly for `amd64` and `arm64`.
---

# Add Apt Index Package

This repository keeps human-authored package definitions under `packages/`. When the user wants a new package added, make the smallest possible config change and prove that the entry resolves.

## Outcome

Produce or update exactly the package entry needed for the request, then verify:

1. the config loader accepts the entry
2. each declared architecture resolves to the intended upstream artifact

Do not run a full repository refresh or build unless the user explicitly asks for that. On `main`, the normal change is the package entry file only.

## First pass

1. Read `README.md` and `docs/configuration.md`.
2. Inspect 1-3 nearby examples in `packages/` that match the source type.
3. Decide which source resolver fits:
   - GitHub Releases: read `references/github.md`
   - AUR `.SRCINFO`: read `references/aur.md`
   - SourceForge files page: read `references/sourceforge.md`
   - Fixed direct URL: read `references/url.md`
4. If the upstream does not clearly fit one of those, stop and explain the mismatch instead of inventing a config shape.

## Editing rules

- Prefer `packages/<entry>.toml`.
- Match the repository's existing style exactly.
- Use the shorthand form when every architecture shares the same source and update policy.
- Keep the entry name flat and lowercase.
- Touch only the new entry unless the user asked for a rename or migration.
- Do not add speculative abstractions, helper scripts, or doc rewrites.
- `script` exists in the schema but is not implemented at runtime. Do not use it for new entries.

## Entry checklist

Every new entry should answer these questions before you edit:

- What should the entry filename be?
- What is the software homepage?
- Which architectures are actually available: `amd64`, `arm64`, or both?
- Which resolver should be used?
- Is the package `track` or `fixed`?
- What artifact glob or regex should match each architecture?

If one of these is unclear and you cannot derive it from the upstream metadata or nearby examples, say so explicitly.

## Validation

Always run both checks after editing.

### 1. Config loads

Use `load_configuration()` and print the normalized entry for the package you added.

### 2. Resolver works

Use `sources.resolve_candidate(...)` for each declared architecture and print:

- architecture
- resolved asset name
- resolved URL
- upstream version
- checksum algorithm if present

Prefer a short `uv run python - <<'PY' ... PY` snippet over a full refresh.

## Response shape

Report:

- which file you added or changed
- which resolver was chosen and why
- validation result for each architecture
- anything you deliberately did not do, such as skipping `refresh`/`build`

## Reference map

- `references/github.md`: choosing `track` vs `fixed`, finding release tags, matching asset globs
- `references/aur.md`: using `.SRCINFO`, matching `source_<arch>` entries, dealing with blocked AUR HTML
- `references/sourceforge.md`: selecting the correct files directory and full-match regexes
- `references/url.md`: fixed direct URLs when no richer resolver fits

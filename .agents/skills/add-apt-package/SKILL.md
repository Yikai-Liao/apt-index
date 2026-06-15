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

## Fast path

Default to the shortest path that can produce a correct entry.

1. Extract the decision-critical facts from the user request first:
   - requested upstream or homepage
   - requested architectures, if any
   - whether the user already constrained the source type
2. If the user already gave a concrete upstream and architecture scope, do not re-research alternative sources unless the stated source is unusable.
3. If architecture support is ambiguous and one architecture is clearly missing or messy upstream, prefer the narrower valid entry over speculative multi-arch support.
4. Do not use CodeGraph for this skill. This task is config editing plus upstream metadata lookup, not code-structure exploration.
5. Treat the validation APIs as known:
   - `load_configuration(root)`
   - `sources.resolve_candidate(...)`
   Do not reread source files just to rediscover these entry points unless your validation snippet fails.
6. If the user already pasted exact asset names, use them to derive the glob directly instead of rediscovering the same pattern from search results.

## First pass

Read only the minimum context needed:

1. Inspect one nearby example in `packages/` that matches the resolver you expect to use.
2. Read a second example only if the first one does not answer the shape you need.
3. Read `docs/configuration.md` only when the schema detail is unclear from the example or the edit touches an uncommon shape.
4. Read `README.md` only when the user asks for repository workflow beyond adding the package entry itself.
5. Decide which source resolver fits:
   - GitHub Releases: read `references/github.md`
   - AUR `.SRCINFO`: read `references/aur.md`
   - SourceForge files page: read `references/sourceforge.md`
   - Fixed direct URL: read `references/url.md`
6. Read exactly one resolver reference after you pick the likely resolver. Do not read multiple resolver references "just in case".
7. If the upstream does not clearly fit one of those, stop and explain the mismatch instead of inventing a config shape.

## Research guardrails

- Prefer deterministic metadata endpoints over search:
  - GitHub: repository releases or API
  - AUR: `.SRCINFO` raw endpoint
  - SourceForge: exact files directory page
  - Direct URL: the artifact URL itself
- Do not use a search engine to find raw metadata URLs that can be constructed directly.
- For GitHub, prefer `gh api` or the direct releases endpoints before HTML search results when the repository is already known.
- For AUR, prefer the raw `.SRCINFO` endpoint directly. Do not open AUR search results first.
- If the request mentions multiple possible sources, prefer the cleanest upstream that directly publishes the target `.deb`.
- If an AUR package mixes `.deb`, AppImage, or other artifacts across architectures, do not force the AUR resolver unless AUR is the only place the requested `.deb` exists.
- If one architecture has the desired `.deb` and another does not, declare only the architecture that can be resolved cleanly unless the user explicitly wants a fallback strategy discussed.
- If the upstream repo clearly publishes multiple different products or release streams, stop as soon as you can name the ambiguity and ask the user to choose. Do not continue broad web exploration looking for a default product.

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
- Can a narrower architecture set avoid a messy or non-existent upstream artifact?
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

Prefer one short `uv run python - <<'PY' ... PY` snippet that does both checks in one run:

- load configuration
- print the normalized entry
- resolve each declared architecture
- print the candidate summary for each one

Do not write a helper script, run a full refresh, or split validation into multiple exploratory commands unless the combined snippet fails for a concrete reason.
If network validation stalls or fails repeatedly, stop after the direct check and report the blocker instead of broadening the investigation.
Do not do a separate source-code-reading phase before validation. The point of validation is to exercise the real APIs directly.

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

## Case notes

- `obsidian`-style mixed-source packages are a trap: AUR may expose different artifact types per architecture while the upstream GitHub releases page has the exact `.deb` you need. In that case, prefer the direct upstream resolver and keep the architecture set minimal.

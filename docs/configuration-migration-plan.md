# Configuration Migration Record

The split-configuration migration has already landed.

Use [`configuration.md`](./configuration.md) for the current source of truth. This file is kept only as a short historical record of what changed and which invariants came out of that migration.

## Completed Outcome

The repository no longer uses a single `packages.toml` file.

- Repository settings live in `apt-index.toml`.
- Software entries live in `packages/`.
- The loader normalizes raw TOML into an architecture-centric runtime model before refresh or build logic runs.
- `apt-index.lock.json` stores source, update policy, and artifact data per architecture.
- `track_health.json` reports refresh status per architecture.
- Published APT metadata is derived from the lockfile, not from repository-level architecture lists.

## Current Invariants

- `packages.toml` is rejected as an active configuration entry point.
- Valid entry layouts are `packages/<entry-name>.toml` and `packages/<entry-name>/index.toml`.
- Entry names are flat and must match `^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$`.
- Shorthand entries use a top-level `architectures = [...]` list plus top-level `source` and `update_policy`.
- Explicit entries use `[architectures]` maps and must not also set shorthand `source` or `update_policy`.
- Architectures may select different resolvers and different update policies within the same entry.
- Unselected source options are allowed in raw TOML but are dropped from the normalized runtime model.

## Historical Notes Worth Keeping

The migration mattered because it separated repository concerns from per-entry concerns and let refresh/build operate on a single normalized shape instead of carrying TOML shorthand rules through the whole toolchain.

The most important resulting ADRs are:

- [`docs/adr/0017-separate-repository-configuration-from-software-entries.md`](./adr/0017-separate-repository-configuration-from-software-entries.md)
- [`docs/adr/0018-normalize-entry-configuration-by-architecture.md`](./adr/0018-normalize-entry-configuration-by-architecture.md)

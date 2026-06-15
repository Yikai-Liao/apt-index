# Configuration Migration Plan

This plan moves Apt Index from the current single `packages.toml` configuration to the split configuration model documented in [`configuration.md`](./configuration.md).

## Scope

The migration is an implementation change, not a compatibility layer. After the migration:

- `apt-index.toml` is the only root repository configuration file.
- `packages.toml` is not a valid configuration entry point.
- software entries live under `packages/`.
- refresh and build code consume normalized per-architecture entry configuration only.
- raw TOML shorthand is expanded before resolver logic runs.

## Implementation Steps

1. Add Pydantic as a runtime dependency.
2. Add raw configuration models for `apt-index.toml` and `packages/`.
3. Add normalized models where each software entry contains `architectures[arch]`, and each architecture contains its selected update policy and active source resolver.
4. Implement config loading from:

   ```text
   apt-index.toml
   packages/<entry-name>.toml
   packages/<entry-name>/index.toml
   ```

5. Fail fast when:

   - `packages.toml` exists as an active configuration source.
   - an entry name does not match the allowed pattern.
   - a nested entry path is used.
   - both `packages/<entry-name>.toml` and `packages/<entry-name>/index.toml` exist.
   - explicit architecture plans are mixed with shorthand `source` / `update_policy`.
   - an architecture references a missing source option.
   - an architecture selects a source whose capability does not support its update policy.
   - a selected source is missing required per-architecture resolver fields.

6. Allow unselected source options in raw entry TOML, but drop them during normalization.
7. Update resolver inputs so GitHub, AUR, URL, and future script resolution read the normalized per-architecture active source only.
8. Update refresh so architecture failures are recorded per architecture:

   - a failed architecture with a previous locked artifact keeps that artifact.
   - a failed architecture without a previous locked artifact is reported as failed.
   - other architectures in the same software entry can still refresh and update.

9. Update the lockfile schema to record source and update policy per architecture.
10. Keep build and artifact health code tolerant of the previous lockfile schema only if needed for a one-time transition; new refresh output should use the new schema.
11. Migrate the repository configuration:

   - move repository-level fields to `apt-index.toml`.
   - move each current `[packages.<entry>]` table to `packages/<entry>.toml`.
   - remove global `required_architectures` and `optional_architectures`.
   - declare each entry's supported architectures in that entry.

12. Update README examples after the implementation lands so they describe the active configuration, not the migration target.

## Lockfile Target

The lockfile should stop representing source and update policy as entry-level fields. The target shape is architecture-centric:

```json
{
  "version": 2,
  "generated_at": "2026-06-15T00:00:00+00:00",
  "packages": {
    "example": {
      "homepage": "https://example.test/app",
      "architectures": {
        "amd64": {
          "source": "aur",
          "update_policy": "track",
          "resolved_at": "2026-06-15T00:00:00+00:00",
          "artifact": {
            "url": "https://example.test/app-amd64.deb",
            "upstream_version": "1.2.3",
            "asset_name": "app-amd64.deb",
            "filename": "app_1.2.3_amd64.deb",
            "control": {
              "Package": "app",
              "Version": "1.2.3",
              "Architecture": "amd64"
            },
            "size": 123,
            "md5": "md5",
            "sha1": "sha1",
            "sha256": "sha256"
          }
        }
      }
    }
  }
}
```

Build code should derive published architectures from the lockfile contents, not from repository-level required/optional architecture lists.

## Health Report Target

Track refresh health should also become architecture-centric:

```json
{
  "version": 2,
  "generated_at": "2026-06-15T00:00:00+00:00",
  "packages": {
    "example": {
      "status": "partial",
      "architectures": {
        "amd64": {
          "status": "ok",
          "source": "aur",
          "update_policy": "track"
        },
        "arm64": {
          "status": "kept_previous",
          "source": "github",
          "update_policy": "fixed",
          "error": "no GitHub asset matched 'app_*_arm64.deb'"
        }
      }
    }
  }
}
```

Artifact health can stay artifact-centric, but it should iterate the new lockfile shape.

## Test Matrix

Configuration loading tests:

- shorthand entry expands to a normalized per-architecture plan.
- explicit mixed-source entry normalizes each architecture to its own active source.
- explicit mixed-policy entry supports `track` for one architecture and `fixed` for another.
- unselected source options are accepted and omitted from the normalized model.
- selected source options validate required per-architecture fields.
- `release_tag` and `release_tags` are accepted only in raw GitHub config; normalized GitHub source has only the selected per-architecture `release_tag`.
- `release_tag` and `release_tags` cannot both be set in the same raw GitHub source.
- source/update policy incompatibility fails validation.
- top-level old flat resolver fields fail validation.
- missing `apt-index.toml` fails.
- active `packages.toml` fails.
- invalid entry names fail.
- nested entry paths fail.
- duplicate `packages/<entry>.toml` and `packages/<entry>/index.toml` fail.

Refresh and lockfile tests:

- unchanged architecture reuses its previous artifact.
- changed architecture downloads and records a new artifact.
- one architecture can update while another keeps the previous artifact after a resolver failure.
- an architecture failure without a previous artifact is recorded as failed.
- new lockfile entries store source and update policy per architecture.
- build derives architecture output directories from locked artifacts.
- artifact health iterates the new lockfile shape.

Documentation tests or checks:

- README links to the target configuration model and migration plan.
- `docs/configuration.md` includes both shorthand and explicit TOML examples.
- `docs/configuration.md` includes the raw-to-normalized Pydantic prototype.

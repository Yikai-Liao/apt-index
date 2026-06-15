# Separate repository configuration from software entries

The root configuration file is named `apt-index.toml` and contains repository-level configuration only; software entries live under `packages/`. This avoids letting a single `packages.toml` grow into both a repository configuration file and an unbounded software entry list.

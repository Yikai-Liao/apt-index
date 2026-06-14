# Keep lockfile and health report separate

The refresh workflow keeps the published package state in a lockfile and writes track refresh and artifact diagnostics to separate health reports. A track refresh failure for one entry should not prevent unrelated package updates from being published, but diagnostics must not become the source of APT metadata or overwrite the last known resolved artifacts. Health report changes are committed even when the lockfile is unchanged, so repository state reflects current refresh and artifact failures without changing the published package index.

# Store resolved artifacts in a lockfile

The refresh workflow writes resolved package artifacts to a generated lockfile instead of deriving all state from the source configuration on every run. Tracked entries need prior resolved versions to detect updates, produce reviewable diffs, and generate the Worker redirect table without running source resolvers at request time.

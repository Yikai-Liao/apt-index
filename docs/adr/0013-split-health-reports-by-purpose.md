# Split health reports by purpose

The refresh workflow writes `track_health.json` for tracked update checks and `artifact_health.json` for resolved artifact download and checksum checks. Splitting the files keeps update discovery failures separate from published artifact failures and makes each report easier to consume automatically.

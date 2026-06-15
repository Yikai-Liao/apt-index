# Commit tracked refreshes directly

Tracked package refreshes publish automatically instead of opening pull requests. Generated publish state such as `apt-index.lock.json` is committed directly to the dedicated `apt-index-state` branch rather than the default source branch, so the repository stays rolling and self-managed without mixing refresh churn into `main`.

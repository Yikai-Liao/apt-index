# Commit tracked refreshes directly

Tracked package refreshes commit updated generated state, such as the lockfile and health reports, directly to the default branch instead of opening pull requests. The repository is intentionally rolling and self-managed, so successful daily refreshes should publish automatically; failed refreshes should stop the affected entry rather than wait for a review queue.

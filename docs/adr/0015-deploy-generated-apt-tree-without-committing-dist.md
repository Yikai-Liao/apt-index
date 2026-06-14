# Deploy generated APT tree without committing dist

The deployable APT tree is generated in GitHub Actions and uploaded to Cloudflare Pages, but `dist/` is not committed to the repository. The repository commits human-authored configuration plus generated state such as the lockfile and health reports; signed APT metadata and static redirect rules are deploy artifacts derived from that state rather than source-controlled state.

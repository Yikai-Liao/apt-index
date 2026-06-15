# Publish redirect rules as static data

Redirect rules are generated as static data rather than embedded in the Worker bundle. This lets the daily refresh workflow update package redirects with repository data and Pages assets without redeploying Worker code for every package version change.

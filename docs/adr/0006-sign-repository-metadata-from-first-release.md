# Sign repository metadata from the first release

The repository signs its APT metadata from the first release instead of relying on `trusted=yes` or disabled client verification. The project does not host upstream `.deb` files, but it does control the package index, download locations, sizes, and hashes, so unsigned metadata would make the redirect model unsafe.

Signing uses a long-lived GPG key. GitHub Actions imports the private key from `APT_INDEX_GPG_PRIVATE_KEY_B64`, with optional `APT_INDEX_GPG_PASSPHRASE`, before producing `InRelease` and `Release.gpg`. Local builds read the same variables from `.env`. The build must fail when no signing private key is available; it must not generate an ephemeral key in CI, because that would rotate `key.asc` and break clients that already trust the previous repository key.

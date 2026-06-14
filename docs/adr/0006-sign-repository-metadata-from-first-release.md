# Sign repository metadata from the first release

The repository signs its APT metadata from the first release instead of relying on `trusted=yes` or disabled client verification. The project does not host upstream `.deb` files, but it does control the package index, download locations, sizes, and hashes, so unsigned metadata would make the redirect model unsafe.

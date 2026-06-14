# Generate Packages from the lockfile

APT `Packages` files are generated directly from the lockfile and extracted upstream `.deb` control metadata instead of using `reprepro`, `dpkg-scanpackages`, or `apt-ftparchive packages` as the primary generator. Those tools are built around local `.deb` trees or repository pools, while this project publishes virtual package paths that redirect to upstream artifacts.

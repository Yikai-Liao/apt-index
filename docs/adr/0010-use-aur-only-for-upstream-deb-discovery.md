# Use AUR only for upstream deb discovery

The AUR source resolver uses static AUR `.SRCINFO` metadata to find upstream `.deb` artifact URLs and checksums, but it does not execute PKGBUILD scripts or inherit AUR package names, dependency adaptations, install scripts, `provides`, or file modifications. This project targets Debian and Ubuntu directly, so the resolved upstream `.deb` control metadata remains the source of APT package identity.

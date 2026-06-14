# Use upstream package metadata

The first release indexes the package name, version, architecture, and dependency fields declared inside each upstream `.deb` without rewriting them. This avoids repackaging third-party software and keeps the repository a personal APT index rather than a package transformation system, even when a configured software entry name differs from the actual `apt install` name.

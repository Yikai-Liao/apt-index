# Use a shared suite first

The first release exposes one shared APT suite, such as `stable main`, for Debian and Ubuntu systems instead of generating separate suites for each distribution codename. Most targeted upstream `.deb` packages are not distribution-specific, so per-distro suites would add configuration and CI complexity before there is evidence that package compatibility needs to diverge.

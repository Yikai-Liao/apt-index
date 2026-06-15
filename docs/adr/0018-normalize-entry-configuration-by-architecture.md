# Normalize entry configuration by architecture

Software entry configuration is normalized into per-architecture plans before refresh and build code runs. Raw TOML may use shorthand for common cases, and a software entry may keep multiple source options, but the runtime model contains only each architecture's selected source resolver and update policy so mixed-source and mixed-policy entries do not leak configuration sugar into resolver logic.

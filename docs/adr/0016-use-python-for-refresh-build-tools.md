# Use Python for refresh build tools

The GitHub Actions refresh and APT tree build tools are written in Python, while Worker code can remain TypeScript when edge routing code is needed. Python is the simpler fit for this build pipeline because it has built-in TOML parsing, archive handling, hashing, compression, and straightforward file generation, avoiding a Node dependency stack for code that does not run in the Worker runtime.

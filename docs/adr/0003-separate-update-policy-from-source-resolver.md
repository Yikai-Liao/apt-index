# Separate update policy from source resolver

Package configuration separates `update_policy` from the source resolver because version movement and upstream discovery are different decisions. A GitHub resolver can support both fixed and tracked releases, while a URL resolver is fixed-only and AUR or script resolvers are track-only; encoding that compatibility keeps the daily refresh workflow explicit.

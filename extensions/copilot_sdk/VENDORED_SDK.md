# Vendored Copilot SDK

This extension vendors the Python Copilot SDK source under `extensions/copilot_sdk/_vendor/copilot/`.

Why this exists:
- the required SDK behavior is ahead of the installable PyPI release path
- the newer published wheels bundle a platform-specific CLI that does not fit the Termux/runtime model used here
- this repo needs a stable, reproducible Python-side SDK surface while continuing to use the external `copilot` CLI executable

Upstream source:
- repository: `https://github.com/github/copilot-sdk`
- vendored commit: `396e8b3c04175dcf2fd1c7c34950c3fc0a5395e8`

Scope:
- only the Copilot extension imports this vendored package
- platform-agnostic core files do not import it directly
- runtime still uses the external `copilot` CLI path configured by the extension

# Agent Index (Templates)

This directory holds optional **template agents** that can be installed into the
user cache at runtime.

Runtime SSOT:

- `$XDG_CACHE_HOME` (if set), otherwise `~/.cache`
- `${AGENT_CACHE_HOME}/agent_messaging/agent_index/<agent_id>/manifest.json`

The loader will copy any agent directories found here into the cache if they are
missing there. Each agent directory should contain a `manifest.json` and any
profile shellspecs under `profiles/`.

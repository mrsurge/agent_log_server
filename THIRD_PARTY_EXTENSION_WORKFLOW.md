# Third-Party Extension Workflow

This document is the contract for installing, updating, and iterating on third-party extensions in the site-package deployment of `agent_log_server`.

## Purpose

- keep builtin repo extensions separate from user-installed extensions
- make third-party extension installs predictable and reversible
- let agents iterate quickly with `install`, `update`, `smoke`, repeat
- keep `server.py` and the shared frontend generic

## Canonical Paths

### Builtin extension root

- `extensions/`

This is the repo-owned builtin extension root.

### User-installed extension root

- `~/.local/share/app_server/extensions/`

This is the normal live install target for third-party extensions in a site-package deployment.

### Runtime and cache state

- `~/.cache/app_server/`

Examples:

- `~/.cache/app_server/app_server_config.json`
- `~/.cache/app_server/conversations/`
- `~/.cache/app_server/codex_app_server_schema/`
- `~/.cache/app_server/debug_raw.jsonl`

Installed extension code does **not** belong under the cache root.

## Source vs Installed Target

There are two distinct locations for an extension during development:

### Contributor source

A non-live source location used for development, review, and patching.

Examples:

- a repo submodule
- a checked-out git repo
- a local source directory

### Installed target

The live extension directory that the app actually loads from:

- `~/.local/share/app_server/extensions/<folder>/`

Do **not** run third-party extensions directly from the contributor source path.

## Registry Contract

Each extension root has its own `extensions.json`.

For third-party/user-installed extensions, the live registry is:

- `~/.local/share/app_server/extensions/extensions.json`

Each entry should include:

- `id`
- `name`
- `type`
- `path`
- `enabled`

Registry writes must be idempotent:

- if the extension already exists, update its entry in place
- if it does not exist, append it
- do not create duplicate entries for the same `id`

## Installer Contract

The installer should be generic and should not live in `server.py`.

### Required operations

- `install_from_path(source_path, extension_id=None)`
- `install_from_git(repo_url, ref=None, extension_id=None)`
- `update_from_path(extension_id, source_path)`
- `update_from_git(extension_id, repo_url=None, ref=None)`
- `remove_extension(extension_id)`

### Install flow

1. stage source into a temp/work area
2. validate required files:
   - `manifest.json`
   - `client.py`
3. copy or sync into:
   - `~/.local/share/app_server/extensions/<folder>/`
4. upsert the registry entry in:
   - `~/.local/share/app_server/extensions/extensions.json`
5. optionally run dependency install/check hooks
6. restart or reload extension discovery
7. smoke test

### Update flow

1. fetch from the recorded source (`path` or `git`)
2. stage into a temp/work area
3. validate required files
4. atomically replace the installed target
5. preserve the registry entry unless the extension identity is invalid
6. smoke test again

### Remove flow

1. remove the registry entry
2. remove the installed target under the user extension root
3. do **not** delete conversations or cache state automatically

## Metadata To Persist

If an installer tracks origin metadata, store enough to support future updates:

- source type: `path` or `git`
- source path or repo URL
- optional git ref
- installed extension `id`
- installed folder name

This can live in installer-managed state or registry metadata, but it must remain machine-readable.

## Validation Loop

The intended iteration loop is:

1. patch contributor source
2. `update_from_path(...)` or `update_from_git(...)`
3. restart or hot-reload extension discovery
4. run a targeted smoke test
5. repeat

This workflow applies both to extension development and extension-framework development.

## KB vs Agent Log

Use the KB tools for durable repo-scoped workflow/contract knowledge.

Use the agent log for:

- coordination
- progress updates
- handoffs
- transient decisions

The KB is the better place for stable workflow contracts because it is repo-scoped and cross-platform.

## Current Product Direction

Legacy builtin `codex` is now a compatibility path, not the preferred primary agent path.

Directionally:

- extension-backed agents should be favored over legacy builtin `codex`
- the settings modal should not present legacy `codex` as the primary/default-looking option
- compatibility can remain, but preference should move toward the extension-backed implementations

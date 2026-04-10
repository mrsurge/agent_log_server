# Third-Party Extension Workflow

This document is the contract for installing, updating, and iterating on third-party extensions in the site-package deployment of `agent_log_server`.

## Terminology

- `extension root`
  - one installable extension package directory, such as `~/.local/share/app_server/extensions/gemini-acp/`
- `extensions root`
  - a directory that contains many extension package directories plus a shared `extensions.json`

Examples:

- builtin extensions root: `extensions/`
- user-installed extensions root: `~/.local/share/app_server/extensions/`
- shared registry for the user-installed extensions root: `~/.local/share/app_server/extensions/extensions.json`

## Purpose

- keep builtin repo extensions separate from user-installed extensions
- make third-party extension installs predictable and reversible
- let agents iterate quickly with `install`, `update`, `smoke`, repeat
- keep `server.py` and the shared frontend generic

## Canonical Paths

### Builtin extensions root

- `extensions/`

This is the repo-owned builtin extensions root.

### User-installed extensions root

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

Each extensions root has its own shared `extensions.json`.

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

- `validate(source_type, ...)`
- `install_from_path(source_path, extension_id=None)`
- `install_from_zip(zip_path, extension_id=None)`
- `install_from_git(repo_url, ref=None, extension_id=None)`
- `update_from_path(extension_id, source_path)`
- `update_from_zip(extension_id, zip_path)`
- `update_from_git(extension_id, repo_url=None, ref=None)`
- `remove_extension(extension_id)`
- `reload_extensions()`

### Install flow

1. stage source into a temp/work area
2. validate required files:
   - `manifest.json`
   - `client.py`
3. validate path-bearing manifest references that must stay inside the extension root:
   - `agent.shellspec`
   - `ui.requestCards[*].module`
4. copy or sync into:
   - `~/.local/share/app_server/extensions/<folder>/`
5. upsert the registry entry in:
   - `~/.local/share/app_server/extensions/extensions.json`
6. optionally run dependency install/check hooks
7. restart or reload extension discovery
8. smoke test

### Source types

The current installer/validator contract is source-type driven:

- `path`
- `zip`
- `git`

For `zip`, the accepted layouts are:

- archive root is the extension root
- archive contains a single enclosing directory that is the extension root

For `git`, the installer stages a clone, optionally checks out a ref, and records the resolved commit SHA.

If `.gitmodules` is present, git installs now materialize submodules before validation:

- local filesystem repos first try to overlay already-materialized local submodule working trees
- remaining missing submodules fall back to recursive git submodule materialization
- if recursive materialization fails, the git install/update fails

### Update flow

1. fetch from the recorded source (`path`, `zip`, or `git`)
2. stage into a temp/work area
3. validate required files
4. atomically replace the installed target
5. preserve the registry entry unless the extension identity is invalid
6. smoke test again

### Remove flow

1. remove the registry entry
2. remove the installed target under the user-installed extensions root
3. do **not** delete conversations or cache state automatically

## Metadata To Persist

If an installer tracks origin metadata, store enough to support future updates:

- source type: `path` or `git`
- source path, archive path, or repo URL
- optional git ref
- resolved git commit SHA when available
- installed extension `id`
- installed folder name

This must remain machine-readable.

Current authority split:

- top-level registry fields (`id`, `name`, `type`, `path`, `enabled`) drive live loader/runtime identity
- `install_source` is authoritative for later update-source resolution
- `installer_meta` is authoritative for installer semantics/history such as:
  - identity authority
  - path authority
  - current install snapshot
  - previous replaced snapshot

## Validation Loop

The intended iteration loop is:

1. patch contributor source
2. `update_from_path(...)` or `update_from_git(...)`
3. restart or hot-reload extension discovery
4. run a targeted smoke test
5. repeat

This workflow applies both to extension development and extension-framework development.

## Manifest schema version compatibility

The contract now has a manifest/package `schema_version`.

Current runtime behavior is intentionally compatibility-friendly:

- if `schema_version` is present, it must be supported by the host
- if `schema_version` is missing, validation reports a warning and assumes schema version `1`

This keeps older prototype extensions installable while the explicit manifest contract is rolling out.

If `compat` is absent, the current default is:

- no extra compatibility gate beyond supported `schema_version`

If `compat` is present, the current canonical keys are:

- `app_server_manifest_min`
- `app_server_manifest_max`

## Install folder authority

Current installer behavior is:

- `manifest.id` is authoritative identity
- first install resolves the live installed folder from sanitized `manifest.id`
- once installed, registry `path` becomes authoritative for later update/remove operations
- source folder names are not authoritative

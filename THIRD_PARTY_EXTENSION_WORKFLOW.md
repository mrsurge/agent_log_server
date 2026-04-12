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

## Operator CLI

The repo now also exposes a thin operator-facing CLI through the existing `codex-agent` entrypoint.

This CLI is **local/direct** by default. It uses the installer helpers in-process, so a running `agent_log_server` instance is **not required** to validate, install, update, or remove an extension package.

If a long-running server process is already up, it still needs its own reload path to see newly installed files in-memory. The package install itself is local filesystem + registry work; the running server is only relevant for live runtime refresh.

Examples:

- `codex-agent extension validate --path /path/to/ext`
- `codex-agent extension validate --zip /path/to/ext.zip`
- `codex-agent extension validate --git /path/or/repo/url --ref main`
- `codex-agent extension install --path /path/to/ext`
- `codex-agent extension update my-ext --git /path/or/repo/url --ref main`
- `codex-agent extension remove my-ext`
- `codex-agent extension reload`

Source selection is explicit:

- `--path`
- `--zip`
- `--git`

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

This is the authoritative patch/review/commit surface for third-party extension work. If the contributor source is a repo, it should normally carry its own `README.md` describing the extension's purpose, architecture, and update flow.

### Installed target

The live extension directory that the app actually loads from:

- `~/.local/share/app_server/extensions/<folder>/`

Do **not** run third-party extensions directly from the contributor source path.
Do **not** treat the installed target as the place to make source edits and then expect later installs/updates to reflect those manual patches.

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

1. reproduce/test the installed extension behavior
2. investigate in the contributor source and runtime/log surfaces
3. patch contributor source
4. run repo-side validation
5. run a small non-server smoke if the change affects transport/session timing or transcript shaping
6. ensure the contributor-source manifest version is correct for the revision being shipped
7. commit and push the contributor source if the install/update flow depends on git
8. `install_from_git(...)`, `update_from_git(...)`, `update_from_path(...)`, or the equivalent operator CLI command
9. restart or hot-reload extension discovery if needed
10. retest the installed extension
11. repeat

This workflow applies both to extension development and extension-framework development.

## Manifest schema version compatibility

The contract now has a manifest/package `schema_version`.

Current runtime behavior is intentionally compatibility-friendly:

- if `schema_version` is present, it must be supported by the host
- if `schema_version` is missing, validation reports a warning and assumes schema version `1`

This keeps older prototype extensions installable while the explicit manifest contract is rolling out.

## Manifest package version

`manifest.version` is required for third-party packages.

- it must be a non-empty string in the extension's own `manifest.json`
- if a legacy manifest is being versioned for the first time, start at `0.1.0`
- installer/runtime metadata must not be treated as a substitute for a missing manifest version

The contributor source manifest is the source of truth for the package version you are shipping.

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

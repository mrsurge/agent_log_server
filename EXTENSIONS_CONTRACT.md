# Extensions Contract

Status: draft

This is a draft contract for installable third-party extensions.

It is intentionally separate from runtime/backend protocol contracts.

## Scope

This contract defines:

- installable extension package shape
- manifest fields and validation rules
- archive install expectations
- git install expectations
- registry behavior
- compatibility/version semantics

This contract does **not** define:

- backend protocol message schemas
- runtime-generated protocol bundles
- extension-specific transport semantics

## Terminology

- `extension root`
  - one installable extension package directory
- `extensions root`
  - a directory containing many extension package directories plus one shared `extensions.json`

Examples:

- builtin extensions root: `extensions/`
- user-installed extensions root: `~/.local/share/app_server/extensions/`
- one extension root inside that user-installed extensions root:
  - `~/.local/share/app_server/extensions/gemini-acp/`

## Canonical locations

- builtin extensions root: `extensions/`
- installed third-party extensions root: `~/.local/share/app_server/extensions/`
- installed third-party shared registry for that extensions root: `~/.local/share/app_server/extensions/extensions.json`
- runtime/cache state lives under `~/.cache/app_server/`

## Package unit

An installable extension package corresponds to one extension root directory.

Installed shape:

```text
<extension-root>/
  manifest.json
  client.py
  router.py                # optional
  settings_schema.json     # optional
  dependencies.py          # optional
  splash_schema.json       # optional
  ui/
  shellspec/
  ...
```

## Manifest contract

### Required fields

- `schema_version`
- `id`
- `name`
- `version`
- `type`

### Strongly expected fields

- `description`
- `enabled`
- `capabilities`
- `ui`

### Current runtime-facing fields already in active use

- `agent`
- `capabilities`
- `dependencies`
- `model`
- `ui`
- `ui.requestCards`
- `ui.toolRenderPolicy`
- `ui.semanticShellRibbon`

## Field semantics

### `schema_version`

The version of **this manifest/install contract**.

This is for installer/validator compatibility only.

It must remain distinct from:

- extension package `version`
- backend/runtime protocol versions
- runtime-generated schema versions

Current compatibility behavior:

- packages should provide `schema_version`
- the current installer treats a missing `schema_version` as a warning and assumes schema version `1`
- unsupported explicit `schema_version` values are rejected

### `version`

The extension package version.

Intended use:

- update comparisons
- install reporting
- rollback/upgrade logic

Recommended format:

- semantic version string, e.g. `0.1.0`, `1.0.0`

This field is required for both builtin and user-installed extension manifests.
If you are versioning an older manifest for the first time, start at `0.1.0`.

### `id`

Stable extension identity used by:

- registry entries
- loader identity
- API routes
- conversation settings agent selection

### `type`

Handler family / shared implementation type.

Multiple extension IDs may still share one handler type.

## Compatibility block

Canonical shape:

```json
{
  "schema_version": 1,
  "id": "gemini-acp",
  "name": "Gemini ACP",
  "version": "0.1.0",
  "type": "gemini_acp",
  "compat": {
    "app_server_manifest_min": 1,
    "app_server_manifest_max": 1
  }
}
```

Compatibility semantics:

- `compat` is optional
- if `compat` is absent, installer behavior defaults to `schema_version`-only compatibility
- if `compat` is present, it must be an object
- currently supported keys are:
  - `app_server_manifest_min`
  - `app_server_manifest_max`
- unknown keys are tolerated but surfaced as validation warnings

## Validation rules

An installable package is valid only if all of the following pass.

### Manifest validation

- `manifest.json` exists
- manifest parses as a JSON object
- supported `schema_version`
- required fields are present and non-empty strings where applicable
- `version` is present and parseable under the chosen version format
- runtime discovery must not treat registry/install metadata as a fallback for a missing manifest `version`

### File layout validation

- `client.py` exists
- optional referenced files must resolve if declared
- all referenced paths must stay inside the extension root

Examples:

- `agent.shellspec` targets a file under the extension root
- `ui.requestCards[*].module` resolves inside the extension root
- any auxiliary assets referenced by the manifest stay local to the package

### Identity validation

- package `id` must match the install request if one is provided
- duplicate extension IDs should be rejected by installer policy unless explicit override is allowed
- duplicate builtin IDs/folders should be rejected by default

### Compatibility validation

- manifest `schema_version` must be supported by the host
- if `compat` is absent, no extra compatibility gate is applied beyond supported `schema_version`
- if `compat` is present, the declared manifest min/max compatibility block must pass

## Install folder resolution

Authority is intentionally split:

- `manifest.id`
  - authoritative extension identity
- registry `path`
  - authoritative installed folder for an already-installed extension
- source folder name
  - not authoritative for install/update identity

Current install/update behavior:

- first install resolves the installed folder from sanitized `manifest.id`
- once installed, registry `path` becomes authoritative for future update/remove operations
- updates do not rename the installed folder just because the contributor source folder name differs
- updates also do not rename the installed folder if the source archive encloses the extension in a differently named directory
- explicit folder override is not part of the current installer contract

## Registry contract

Each extensions root has its own shared `extensions.json`.

Registry entries should include:

- `id`
- `name`
- `type`
- `path`
- `enabled`

Installer-managed metadata may also include:

- installed `version`
- source type (`path`, `zip`, `git`)
- source path or archive path
- source repo URL / ref
- source commit SHA
- install timestamp

Authoritative split:

- top-level registry fields are authoritative for live loader/runtime identity:
  - `id`
  - `name`
  - `type`
  - `path`
  - `enabled`
- `install_source` is authoritative for future installer update-source resolution
- `installer_meta` is authoritative for installer semantics/history such as:
  - identity authority (`manifest.id`)
  - path authority (`manifest.id` on first install, `registry.path` thereafter)
  - current install snapshot
  - previous install snapshot for rollback-oriented bookkeeping

Registry writes must be idempotent:

- update matching `id` in place
- append if missing
- never duplicate the same `id`

## Archive / zip contract

### Accepted layouts

Draft proposal:

1. archive root is the extension root
2. archive contains a single enclosing directory that is the extension root

Anything more ambiguous should fail validation.

### Archive safety rules

- no absolute paths
- no `..` traversal
- no symlink escapes
- no writes outside the temp staging dir or final install target

## Git source contract

### Supported source metadata

Git-backed installs should be able to record:

- repository URL
- requested ref
- resolved commit SHA

### Git staging rules

- clone into a temp staging directory first
- validate from the staged checkout, not directly from the live clone target
- normalize either:
  - repo root as extension root
  - one explicit package subdirectory, if that is later allowed by policy

### Git submodule materialization

If `.gitmodules` is present, the installer now materializes submodule content before validation:

- for local filesystem repos, it first overlays any already-materialized local submodule working trees into the staged clone
- if submodules are still missing after that, it falls back to recursive git submodule materialization
- if recursive materialization fails, git install/update fails

This keeps local worktree-driven extension development viable while still giving non-local repos a clear recursive submodule rule

### Git safety / determinism rules

- install result should capture the resolved commit SHA when available
- updates should be able to compare the newly resolved commit against the installed source metadata
- installer policy should define whether shallow clone is acceptable by default

## Install semantics

Installer should:

1. stage package into a temp dir
2. normalize layout
3. validate manifest and file layout
4. install into `~/.local/share/app_server/extensions/<folder>/`
5. upsert registry entry
6. reload extension discovery
7. run dependency checks and warm-up

For git sources, staging means a temp checkout rather than archive extraction.

## Update semantics

Updater should:

- validate before replacing live files
- preserve identity unless explicitly performing a rename/migration flow
- compare installed version vs incoming version
- compare git source metadata when the source type is `git`
- support rollback if reload or warm-up fails

Current metadata authority for updater/rollback bookkeeping:

- `install_source` is the primary machine-readable source of truth for future update source resolution
- `installer_meta.current` captures the active installed snapshot
- `installer_meta.previous` captures the replaced snapshot when an update succeeds

## Removal semantics

Removal should:

- delete registry entry
- delete live installed extension directory
- not automatically purge conversations/cache/runtime artifacts

## Explicit non-goal

Do not treat runtime protocol/schema versions as the package install contract.

For example:

- Codex runtime schema generation/versioning is a backend runtime concern
- package `schema_version` is an installer/manifest compatibility concern

Those are related only indirectly and should remain separate.

## Related docs

- `THIRD_PARTY_EXTENSION_WORKFLOW.md`
- `worktrees/notes/THIRD_PARTY_EXTENSION_INSTALL_PLAN.md`

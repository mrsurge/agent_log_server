# Sidebar Project RPC Plan

Status: implemented in `mrselect6` / `file_editor_cm6` as of the sidebar project RPC slice. This file remains valid as the contract reference; the notes below now distinguish the intended contract from the implemented files.

## Goal

Add a typed `/sidebar_ipc` JSON-RPC project-control suite for Code TE2 so sidebar clients can ask the backend about known projects, open known projects, and create projects from a target path without routing through Explorer or host frontend shortcuts.

## Ownership Rules

- `/sidebar_ipc` only talks to the sidebar IPC backend.
- Sidebar IPC does not call Explorer RPC, editor RPC, or host UI functions directly.
- Frontend state updates use that frontend's existing transport. For the main-page host, backend-originated sidebar notifications still arrive on `/sidebar_ipc` and the host consumes them through its sidebar IPC connection.
- Sidebar IPC backend delegates project lookup, creation, and switching to backend project logic that already owns those behaviors, or to new shared hooks extracted from existing backend route logic.
- Project membership is backend-owned and comes from Code TE2 history plus sidecar state, not from sidebar client state.

## Existing Project Source

- The `Files -> Projects...` modal calls `GET /api/app/file_editor_cm6/debug/projects`.
- That endpoint is built from `HistoryStore.list_projects()` and enriched with `ProjectSidecar.get_sidecar_path(...)` metadata.
- A known project root for this contract means:
  - exact match after history-style logical path normalization, `abspath(expanduser(path))`
  - entry is present in `HistoryStore.list_projects()`
  - the project sidecar file exists
- Lookup must not call `ProjectSidecar.load_or_create(...)`, because lookup should not create sidecars as a side effect.

## RPC Methods

### Current project root

No separate `sidebar.project.current` method is needed for the current contract. Use the existing sidebar-owned method `sidebar.cwd.get` to answer "which project is TE2 currently in?"

Current implementation detail:

- `sidebar.cwd.get` returns `{ cwd, reason, ts }`.
- The backend source is `HistoryStore.get_active_project() || get_project_root()`.
- This is enough when the sidebar client needs the active project path/root.
- If the client needs project metadata or sidecar-known status for that path, call `sidebar.project.lookup` with the returned `cwd`.

### `sidebar.project.lookup`

Params:

```json
{ "path": "/absolute/or/~/project/path" }
```

Result:

```json
{
  "ok": true,
  "known": true,
  "reason": null,
  "project": {
    "path": "/logical/history/path",
    "label": "project",
    "opened_at": "timestamp",
    "is_active": false,
    "directory_exists": true,
    "sidecar": {
      "exists": true,
      "path": "/home/.../.cache/cm6_editor/projects/<sha>.json"
    }
  }
}
```

For misses, return `ok: true`, `known: false`, and a stable `reason` such as `not_in_history`, `sidecar_missing`, `path_missing`, or `invalid_path`.

### `sidebar.project.open`

Params:

```json
{ "path": "/known/project/root" }
```

Behavior:

- Runs the same lookup rule.
- Refuses paths that are not known with an existing sidecar.
- Delegates to shared backend project-open logic.
- Emits `sidebar.project.opened` after successful switch so host clients can refresh over their existing sidebar IPC frontend transport.

### `sidebar.project.create`

Params:

```json
{
  "path": "/desired/project/root",
  "adoptExisting": true,
  "open": true
}
```

Behavior:

- If the target path does not exist, create the project directory through shared backend project-create logic.
- If the target path exists as a directory and `adoptExisting` is true, register/open it through shared backend project-open logic.
- Current backend default: `adoptExisting` defaults to `false`, and `open` defaults to `true`.
- If the target path exists as a file, reject it.
- If `open` is true, emit `sidebar.project.opened` after successful switch.

## Server Notification

### `sidebar.project.opened`

Params:

```json
{
  "path": "/logical/history/path",
  "resolved_path": "/resolved/project/root",
  "state": {},
  "source": "sidebar_ipc_rpc",
  "ts": 1778220000000
}
```

Host/main-page clients should consume this via their existing `/sidebar_ipc` connection and run the same project-open resync path already used after Explorer project-open notifications.

## Implementation Shape

1. Extract shared project lookup/open/create helpers from existing backend route logic or add a focused backend hook module that reuses the same dependencies.
2. Keep HTTP `/project/open` and `/project/create` behavior intact by making those routes call the shared helpers.
3. Add sidebar RPC constants and allowlist entries in the Python and TypeScript sidebar RPC contracts.
4. Add dispatch cases in the sidebar IPC backend only.
5. Add main-page sidebar notification handling for `sidebar.project.opened` so the host frontend updates through its sidebar IPC frontend transport.
6. Update Code TE2 sidebar IPC contract/schema docs.
7. Rebuild served frontend assets, sync version surfaces once, and validate with targeted local checks.

## Implemented Files

- Shared project service: `app/apps/file_editor_cm6/main_page/backend/project_service.py`
- Sidebar backend hook: `app/apps/file_editor_cm6/host/project_backend.py`
- Sidebar IPC dispatch: `app/apps/file_editor_cm6/ui_ipc/sidebar_ws.py`
- Backend sidebar contract constants: `app/apps/file_editor_cm6/ui_ipc/sidebar_rpc_contract.py`
- Frontend sidebar contract constants: `app/apps/file_editor_cm6/src/sidebar_ipc/rpc_contract.ts`
- Host frontend sidebar notification handling: `app/apps/file_editor_cm6/main_page/frontend/connections/ui-ipc.ts`
- Host project-open resync hook: `app/apps/file_editor_cm6/main.ts`
- Existing HTTP project routes now reuse the shared service: `app/apps/file_editor_cm6/main_page/backend/project_routes.py`
- Code TE2 contract docs: `docs/apps/code_cm6/SIDEBAR_IPC_RPC_CONTRACT.md` and `docs/apps/code_cm6/SIDEBAR_IPC_RPC_SCHEMA.md`

## Implementation Notes

- `sidebar.project.open` requires the path to be a known history project with an existing sidecar.
- `sidebar.project.create` may adopt an existing directory only when `adoptExisting` is true.
- `sidebar.project.opened` is emitted only after an actual project switch result that includes `resolved_path`.
- The host/main-page client observes `sidebar.project.opened` over its existing `/sidebar_ipc` connection and calls its own project-open resync path. Sidebar IPC does not call host UI functions directly.

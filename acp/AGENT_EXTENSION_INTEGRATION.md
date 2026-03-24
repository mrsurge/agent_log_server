# Agent Extension Integration

> Historical note: this file lives under `acp/` for now because that folder predates the current extension system. The content here is no longer ACP-specific.

This document describes the current pluggable agent-extension architecture in `agent_log_server`, the hook surface exposed by the backend, and the two real reference implementations that exist today:

- `copilot-sdk` — the more complete, production-style example
- `codex-ext-testing` — the Codex app-server extension example; legacy `codex` is a separate built-in compatibility path in `server.py`, not a normal registered extension

The goal is to explain how to build a new agent extension without hardcoding backend-specific logic into `server.py`, `static/codex_agent.js`, or `static/modals/settings_schema.js`.

## Core invariants

These rules matter more than any individual implementation detail:

1. **Platform-agnostic core files**
   - `agent_log_server/server.py`
   - `agent_log_server/static/codex_agent.js`
   - `agent_log_server/static/modals/settings_schema.js`

   These files must not gain extension-specific imports or hardcoded protocol branches beyond explicit compatibility exceptions that already exist.

2. **All extension-specific behavior lives under `extensions/`**
   - backend/session logic in `extensions/<folder>/client.py`
   - live-event translation in `extensions/<folder>/router.py`
   - metadata in `extensions/<folder>/manifest.json`
   - schema in `extensions/<folder>/settings_schema.json` or a dynamic `get_settings_schema()` hook

3. **`conversation_id` is local, `thread_id`/`session_id` is remote**
   - `conversation_id` identifies our local UI/transcript container
   - `thread_id` or `session_id` is only the backend binding handle

4. **Local `transcript.jsonl` is the source of truth**
   - replay/hydration reads our transcript
   - vendor-native history is only used for bind/import when an extension explicitly supports it

5. **Live event output and transcript output must match**
   Any router that emits frontend events must write equivalent transcript entries for replay parity. Missing transcript fields become replay bugs.

6. **Internal-only debug data must be explicitly tagged**
   - use `internal: true` on both the live event and the transcript entry
   - normal frontend live-play, replay, and conversation-preview paths must ignore internal-tagged records
   - `/api/appserver/transcript/range` hides internal-tagged rows by default; opt in with `include_internal=true` when you need to inspect them directly

## Terms used in this repo

- **new session from port-in**
  - import an external rollout/session into a fresh local conversation
  - bind the remote session/thread id
  - materialize flat transcript entries into local `transcript.jsonl`

- **existing conversation hydration**
  - replay an already-local `transcript.jsonl`
  - uses platform-agnostic frontend/server replay helpers
  - does not require backend-specific transport ownership

## High-level architecture

```text
Frontend (platform-agnostic UI)
    │
    │ Socket.IO + generic HTTP routes
    ▼
agent_log_server/server.py
    │
    ├─ generic conversation/transcript/config plumbing
    ├─ generic extension endpoints
    ├─ direct handler.handle_message(...) dispatch for non-legacy agents
    └─ ext_loader.route_event(...) for extension-owned live routing
            │
            ▼
extensions/__init__.py (ext_loader)
            │
            ├─ registry/discovery
            ├─ generic wrapper methods
            └─ dynamic handler loading
                    │
                    ▼
          extensions/<folder>/client.py
                    │
                    ├─ backend/session lifecycle
                    ├─ request shaping
                    ├─ approval/interrupt support
                    └─ optional router delegation
                            │
                            ▼
                 extensions/<folder>/router.py
                            │
                            ▼
                    actual backend runtime
```

Extensions own backend transport/session logic so the shared frontend can stay unified and agent-agnostic.

## Current filesystem layout

Each extension is self-contained in its own folder. There are now two extension roots:

- builtin repo root: `extensions/`
- user-installed root: `~/.local/share/app_server/extensions/`

Each root has its own `extensions.json`.

| Path | Purpose |
|------|---------|
| `extensions/extensions.json` | Builtin extension registry and ordering |
| `extensions/<folder>/manifest.json` | Builtin extension metadata and capabilities |
| `extensions/<folder>/client.py` | Builtin extension client module |
| `extensions/<folder>/router.py` | Builtin optional event translation layer |
| `extensions/<folder>/settings_schema.json` | Builtin optional static settings schema |
| `~/.local/share/app_server/extensions/extensions.json` | User-installed extension registry and ordering |
| `~/.local/share/app_server/extensions/<folder>/manifest.json` | User-installed extension metadata and capabilities |
| `~/.local/share/app_server/extensions/<folder>/client.py` | User-installed extension client module |
| `~/.local/share/app_server/extensions/<folder>/router.py` | User-installed optional event translation layer |
| `~/.local/share/app_server/extensions/<folder>/settings_schema.json` | User-installed optional static settings schema |
| `extensions/codex_ext_testing/runtime_protocol.py` | Codex extension-specific runtime schema/cache helper |
| `agent_log_server/server.py` | Generic backend entrypoint |
| `agent_log_server/static/modals/settings_schema.js` | Generic schema-driven settings renderer |

The older flat `extensions/<type>_client.py` / `extensions/<type>_router.py` pattern is no longer the model to follow.

## Extension registry and loader

### Registry

The loader merges registries from both roots:

- builtin: `extensions/extensions.json`
- user-installed: `~/.local/share/app_server/extensions/extensions.json`

At startup, the server ensures the user root exists and seeds its `extensions.json` with an empty registry when missing.

```json
{
  "version": "1.0",
  "extensions": [
    {
      "id": "copilot-sdk",
      "name": "GitHub Copilot",
      "type": "copilot_sdk",
      "path": "copilot_sdk",
      "enabled": true
    },
    {
      "id": "codex-ext-testing",
      "name": "Codex Extension Testing",
      "type": "codex_ext_testing",
      "path": "codex_ext_testing",
      "enabled": true
    }
  ]
}
```

### Load flow

On startup, `ext_loader.load_extensions(...)`:

1. ensures `~/.local/share/app_server/extensions/` exists
2. ensures `~/.local/share/app_server/extensions/extensions.json` exists
3. reads each root's `extensions.json` (or scans that root for manifests if the file is absent)
4. merges discovered extensions by `id`
5. imports builtin extensions as `extensions.<folder>.client`
6. imports user-installed extensions from file-backed synthetic package namespaces so relative imports like `.router` and `.dependencies` still work
7. calls the handler init function using this convention:
   - `init_<type>_manager(...)`
   - fallback: `init_<folder>_manager(...)`
   - fallback: any `init_*_manager(...)`
   - fallback: `init_manager(...)`
8. injects shared server callbacks:
   - `broadcast_fn`
   - `transcript_fn`
   - `meta_fns`
   - `fws_getter`
   - `server_root`
   - `extensions_dir`

For each extension, `extensions_dir` is the root that owns that extension:

- builtin extension → repo `extensions/`
- user-installed extension → `~/.local/share/app_server/extensions/`

This is what keeps `server.py` from importing extension modules directly while still allowing non-builtin roots.

## Generic hook surface

The extension system is intentionally small. Implement only the hooks your backend actually supports.

| Hook | Called from | Purpose |
|------|-------------|---------|
| `init_<type>_manager(...)` | `ext_loader.load_extensions()` | One-time handler setup and callback injection |
| `warm_up_all_extensions(timeout)` | `ext_loader.warm_up_extensions()` | Optional warm-up/readiness pass |
| `is_extension_ready(extension_id)` / `wait_extension_ready(...)` | readiness checks | Optional per-extension readiness |
| `handle_message(conversation_id, text, agent_type, settings)` | send-message path in `server.py` | Send user input through the backend |
| `get_settings_schema(extension_id)` | `GET /api/extensions/{id}/settings_schema` | Return a dynamic schema when runtime-generated |
| `get_runtime_options(extension_id, conversation_id=None, settings=None)` | `GET /api/appserver/runtime_options` | Expose shared runtime quick controls/current values such as plan or collaboration mode |
| `list_models()` | `GET /api/extensions/{id}/models` | Populate schema-driven model selectors |
| `list_sessions(cwd=None)` | `GET /api/extensions/{id}/sessions` | Populate `session_picker` browse flows |
| `resume_session_with_history(...)` | session bind/resume endpoint | Bind a backend session/thread to a local conversation and make live backend state ready |
| `hydrate_transcript(...)` | session bind/resume endpoint | Return flat transcript entries for a new session from port-in |
| `route_event(...)` | live app-server notification routing | Translate backend live events into UI + transcript output |
| `resolve_approval(request_id, decision)` | approval response handlers | Complete a pending approval request |
| `validate_pending_approval(...)` | approval validation | Confirm a persisted approval is still valid |
| `abort_session(conversation_id)` | interrupt flow | Abort the active turn/session |
| `get_raw_buffer(limit)` | debug/raw endpoint | Inspect extension debug state |

### Important note about `handle_message`

Most extension hooks are wrapped by `ext_loader`, but the send path still directly fetches the handler and calls `handle_message(...)` from `server.py`.

That is still generic because `server.py` does not import a concrete extension module; it asks `ext_loader` for the active handler and calls the shared method name.

## Server integration points

These are the generic places where extensions plug into the backend:

### Send path

For non-legacy agents:

```text
send_message / api_appserver_message
    └─ load meta.settings.agent
       └─ ext_loader.get_handler(agent).handle_message(...)
```

If `agent == "codex"`, the legacy built-in send path still handles the request for compatibility.

### Generic extension endpoints

All extension endpoints are extension-id based:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/extensions` | list extensions |
| `GET /api/extensions/{id}` | get registry info |
| `GET /api/extensions/{id}/settings_schema` | load dynamic or static schema |
| `GET /api/extensions/{id}/models` | list models |
| `GET /api/extensions/{id}/sessions` | list resumable sessions |
| `POST /api/extensions/{id}/sessions/resume` | bind/resume + optional transcript hydration |
| `GET /api/extensions/{id}/debug/raw` | handler-specific debug buffer |

### Live routing

`server.py` now delegates extension-owned live routing through:

```python
await ext_loader.route_event(
    extension_id,
    label=label,
    payload=payload,
    conversation_id=...,
    thread_id=...,
    turn_id=...,
    request_id=...,
)
```

For the built-in `codex` agent, legacy server-side handling still remains as a fallback for non-collab events. `codex-ext-testing` uses the extension-owned route directly.

### Approval and interrupt plumbing

- approvals: `ext_loader.resolve_approval(...)`
- approval validation: `ext_loader.validate_pending_approval(...)`
- interrupts: `ext_loader.interrupt_session(...)`

## Settings schema model

The settings modal is shared UI plus extension-defined UI.

### Shared core settings

These are not owned by individual extensions:

- agent picker
- label
- alias
- command output lines
- markdown/xterm/diff toggles
- TE2 MCP integration checkbox

### Extension-owned settings

Extensions contribute backend-specific fields through one of two mechanisms:

1. **Static schema** — `extensions/<folder>/settings_schema.json`
2. **Dynamic schema hook** — `get_settings_schema(extension_id)`

Supported field patterns already used in the repo:

| Pattern | Used by | Purpose |
|---------|---------|---------|
| `dynamic_source` | Copilot model selector, dynamic model loading | Populate select options at render time |
| `session_picker` | Copilot + Codex port-in flows | Browse and bind existing backend sessions |
| `textarea` | developer instructions | Multi-line prompt/config input |
| `json` | MCP server config | Structured config input |
| runtime-generated schema | Codex app-server | Build UI fields directly from backend protocol schema |

Special case: legacy `codex` still returns `{useBuiltin: true}` from `/api/extensions/codex/settings_schema`.

## Reference implementation 1: Copilot SDK

`copilot-sdk` is the best example of a fleshed-out extension.

### Files

| File | Purpose |
|------|---------|
| `extensions/copilot_sdk/manifest.json` | extension metadata/capabilities |
| `extensions/copilot_sdk/settings_schema.json` | static schema for session picker, model, policies, MCP, developer instructions |
| `extensions/copilot_sdk/client.py` | client lifecycle, session init/resume/send, approvals, transcript hydration |
| `extensions/copilot_sdk/router.py` | SDK `SessionEvent` → internal event/transcript translation |

### Process model

Copilot SDK does **not** use `framework_shells` for the agent process. The Python SDK manages its own backend subprocess internally through `CopilotClient(...)`.

That makes it a good example of an extension whose backend transport is fully hidden behind a Python client library.

### Hook usage

- **`handle_message(...)`**
  - ensures a session exists
  - resumes or re-resumes when needed
  - sends text with `session.send(...)`

- **`list_models()`**
  - proxies `client.list_models()`
  - normalizes SDK objects into JSON-safe payloads for the schema UI

- **`list_sessions(cwd)`**
  - proxies `client.list_sessions()`
  - sorts results by CWD / git-root relevance for the picker

- **`resume_session_with_history(...)`**
  - binds `thread_id`
  - resumes the SDK session into in-memory state

- **`hydrate_transcript(...)`**
  - calls `session.get_messages()`
  - converts history into our flat transcript format

- **approval hooks**
  - `on_permission_request` handles approval prompts
  - `validate_pending_approval(...)` guards persisted approval state

- **policy hook**
  - `SessionHooks(on_pre_tool_use=...)` enforces sandbox and web policy before tool execution

- **router**
  - `session.on(...)` streams SDK events into `CopilotEventRouter.route_event(...)`
  - router translates deltas, reasoning, tool calls, subagents, usage, and approvals
  - live `_emit()` and transcript `_record()` stay in parity
  - optional `debug_trace` settings emit structured `debug_trace` records on the internal-only lane for Copilot provenance debugging without polluting normal replay

### Why this is the best "full" example

It demonstrates almost the entire generic surface:

- static schema
- model/session listing
- session bind + transcript hydrate
- send flow
- approvals
- tool policy hooks
- rich event routing

## Reference implementation 2: Codex app-server extension (experimental / MVP)

`extensions/codex_ext_testing` is the runtime-schema-driven example.

### Registered extension IDs

The registered extension ID is:

- `codex-ext-testing`

Legacy `codex` is not loaded from the extension registry. It remains a built-in compatibility path in `server.py` with its own server-owned transport/orchestration.

`codex-ext-testing`:

- exercises the generic extension architecture end to end
- uses runtime-generated settings schema, session picker, send/receive hooks, and bind/import hooks
- owns its own framework-shell-backed app-server transport (`app-server:codex-extension`)
- is the active proving ground for the schema-driven, extension-owned transport approach

### Runtime protocol architecture

This extension does **not** depend on a committed schema artifact anymore.

At runtime it:

1. runs `codex --version`
2. computes a versioned cache directory:
   - `~/.cache/app_server/codex_app_server_schema/<version>/`
3. runs:
   - `codex app-server generate-json-schema --out <cache_dir>`
4. loads the generated bundle
5. builds in-memory registries from:
   - `ClientRequest`
   - `ServerNotification`
   - `EventMsg`

That logic lives in `extensions/codex_ext_testing/runtime_protocol.py`.

### What the runtime schema is used for

- **dynamic settings schema**
  - `get_settings_schema(extension_id)` returns UI fields derived from the generated protocol bundle
  - includes generic field types such as:
    - `session_picker`
    - dynamic `model`
    - `reasoning_effort`
    - `summary` (`auto`, `concise`, `detailed`, `none`)
    - `approvalPolicy`
    - `sandboxPolicy`
    - `developer_instructions`

- **request shaping**
  - `build_request_params(...)` uses the runtime request registry to build params for:
    - `thread/start`
    - `thread/resume`
    - `turn/start`
    - `turn/interrupt`
  - the same helper also keeps turn-only settings in the right lane:
    - thread-level settings participate in `thread/start`, `thread/resume`, and the thread runtime signature
    - turn-level settings such as reasoning effort and reasoning summary are persisted through the generic schema/meta flow but only emitted on `turn/start`

- **runtime signature tracking**
  - thread-level runtime settings are hashed from the protocol-shaped payload, not from a handwritten config subset

- **event routing**
  - the router checks runtime-known notification and event types before translating them

### Hook usage

- **`handle_message(...)`**
  - ensures the app-server shell is ready
  - starts a thread when no `thread_id` exists yet
  - resumes an existing thread before send only when the transport lost that thread or thread-level runtime settings changed
  - sends the user message with `turn/start`
  - applies turn-only settings such as reasoning effort and reasoning summary on `turn/start`
  - persists the thread runtime signature for the thread-level settings only

- **`list_models()`**
  - reuses the app-server `model/list` data
  - normalizes `id` / `name` for schema UI use

- **`list_sessions(cwd)`**
  - uses `thread/list` through the extension-owned transport
  - sorts results by CWD relevance for the picker

- **`resume_session_with_history(...)`**
  - binds `thread_id`
  - resumes the selected Codex thread through the extension-owned transport

- **`hydrate_transcript(...)`**
  - supports the **new session from port-in** flow
  - reuses the in-house rollout import helper to return flat transcript entries
  - leaves local transcript writing/replay orchestration to the generic server/frontend path

- **`route_event(...)`**
  - currently handles the MVP receive slice:
    - `thread/started`
    - `turn/started`
    - `turn/completed`
    - user messages
    - assistant deltas/finalization
    - collab subagent lifecycle events
  - routed results may also include a generic `meta_patch` object; `server.py` applies it to `meta.json` so extensions can persist conversation-local live state without adding extension-specific backend code

### framework_shells interaction

Unlike the Copilot SDK extension, Codex app-server does use `framework_shells`.

- `codex-ext-testing` starts/adopts a dedicated shell labeled `app-server:codex-extension`
- that shell uses the observed shellspec entry `app_server_observed`
- `agent_log_server.rpc_stdio_mirror` preserves the real stdout pipe for the transport parser while mirroring RPC stdin/stdout traffic to stderr for framework-shell observability
- the handler also hardens startup by restarting locally when an adopted shell lacks a live stdin pipe

### Current state of the Codex extension

What works now:

- runtime schema generation from the installed binary
- versioned schema cache
- dynamic schema-driven settings
- runtime-schema-backed reasoning summary setting persisted through the shared settings/meta flow
- session picker / new session from port-in
- request building from runtime schema
- extension-owned app-server transport
- real two-way message flow
- interrupt support
- stderr RPC observability via the wrapper shell
- extension-owned MVP live routing

What is intentionally still incomplete:

- full tool call render coverage
- full approval plumbing through the extension-owned path

Existing conversation hydration is already handled through the local transcript replay path and is not an extension-specific transport concern.

Those are the next major slices after basic two-way communication.

## How to build a new agent extension

Use the Copilot SDK and Codex app-server implementations as the two reference patterns.

Legacy `codex` in `server.py` is the hard-coded compatibility template, but new extensions do **not** get their own special server/frontend branches. A real extension must plug into the same generic `server.py` / `ext_loader` hook surface as `copilot-sdk` and `codex-ext-testing`, so it also works when loaded from non-builtin extension roots.

### 1. Add a registry entry

For a site-package install, the normal user-modifiable extension root is:

- `~/.local/share/app_server/extensions/`

So for a new third-party extension, create or update:

- `~/.local/share/app_server/extensions/extensions.json`

with an entry containing:

- `id`
- `name`
- `type`
- `path`
- `enabled`

### 2. Create an extension folder

Minimum recommended structure for a user-installed extension:

```text
~/.local/share/app_server/extensions/<folder>/
  manifest.json
  client.py
  router.py              # optional but recommended for streaming backends
  settings_schema.json   # optional if schema is dynamic
```

Builtin repo extensions under `extensions/<folder>/` still follow the same layout, but that is the builtin root, not the normal target for user-installed additions.

### 3. Implement the manager init hook

Your client module should expose one of the init function names recognized by `ext_loader`, preferably:

```python
def init_<type>_manager(extensions_dir, server_root, fws_getter, broadcast_fn, transcript_fn, meta_fns): ...
```

Use this to store callbacks and initialize any shared backend state.

`extensions_dir` will be the root that owns the extension:

- builtin repo extension → `extensions/`
- user-installed extension → `~/.local/share/app_server/extensions/`

### 4. Decide whether schema is static or dynamic

- choose **static `settings_schema.json`** when the backend config surface is stable
- choose **`get_settings_schema()`** when the backend protocol is runtime-generated or version-dependent
- if the extension participates in shared footer/runtime quick controls, expose that through schema metadata (`runtime_option`) instead of hardcoding frontend behavior
- for plan/collaboration mode, expose a normal enumerated schema field (for example `mode`) and set the capability in the manifest; do not hard-code labels in shared UI code

### 5. Decide who owns backend transport/session lifecycle

- choose the **Copilot-style** pattern when a Python client library already hides transport/session details
- choose the **Codex-style** pattern when you need an extension-owned framework-shell or JSON-RPC transport
- keep transport/session ownership inside the extension; do not leak raw backend protocol logic into the shared frontend

### 6. Implement the send/session hooks

At minimum, a usable extension usually needs:

- `handle_message(...)`
- `list_models()`

If you support shared runtime quick controls (for example approval policy, sandbox policy, or plan/collaboration mode), also implement:

- `get_runtime_options(...)`

If you support bind/resume flows, also implement:

- `list_sessions(...)`
- `resume_session_with_history(...)`
- `hydrate_transcript(...)`

For bind/import flows, keep the responsibilities split:

- `resume_session_with_history(...)`
  - bind the remote thread/session id to the local conversation
  - make the live backend state ready
- `hydrate_transcript(...)`
  - return flat transcript entries for a **new session from port-in**
- existing conversation hydration
  - replay the already-local transcript through the generic platform-agnostic helpers

### 7. Add a router if the backend streams events

If your backend emits live updates, put protocol translation in `router.py` and surface it through `route_event(...)`.

Do **not** teach `server.py` about your backend event names directly.

If your extension exposes plan/collaboration mode, the router should translate live mode changes into the shared `mode` event/transcript shape, and any live plan updates into the shared `plan_state` shape. Replay must see the same fields the live UI sees.

### 8. Preserve transcript parity

If the live UI sees a field, replay should also see it later. That means router output must always have a transcript equivalent whenever replay depends on it.

### 9. Validate the extension end to end

Recommended checklist:

- extension appears in `/api/extensions`
- settings schema loads correctly
- model list loads correctly
- first message creates or resumes backend state
- session picker works if implemented
- new session from port-in works if implemented
- transcript replay still works locally
- approvals/interrupts work if the backend supports them
- stderr/log observability works if the extension owns a custom transport wrapper
- live output and replay output match

## Practical guidance on choosing a pattern

Use the **Copilot SDK pattern** when:

- a Python library already abstracts backend transport
- session history can be queried directly
- backend events are rich and callback-driven

Use the **Codex app-server pattern** when:

- the backend exposes a machine-readable runtime schema
- request and event shapes change with backend version
- you want protocol-driven settings/request generation rather than hand-maintained constants
- you need extension-owned transport/runtime control while keeping the frontend unified

## Current project direction

The extension architecture is now the intended long-term path.

- `copilot-sdk` is the mature example
- `codex-ext-testing` proves the runtime-schema-driven, extension-owned transport model
- the next major Codex extension slices are:
  - tool call rendering
  - approval flow integration

## Rename note

This document replaces the old `ACP_INTEGRATION.md` name because the content now covers the generic extension system, not ACP specifically.

The `acp/` directory name is still historical and can be cleaned up later if we want a wider documentation rename pass.

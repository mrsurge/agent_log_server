# Agent Extension Integration

This document describes the current pluggable agent-extension architecture in `agent_log_server`, the hook surface exposed by the backend, and the real reference implementations that exist today:

- `copilot-sdk` — the more complete, production-style example
- `codex-ext` — the stable Codex app-server extension example
- `codex-ext-exp` — the experimental Codex fork for dynamic developer-instruction / pending-context work
- `codex-ext-testing` — an optional compatibility registry alias that resolves to `codex-ext` and is disabled by default in the builtin registry

The goal is to explain how to build a new agent extension without hardcoding backend-specific logic into `server.py`, `static/codex_agent.ts`, or `static/modals/settings_schema.js`.

## Core invariants

These rules matter more than any individual implementation detail:

1. **Platform-agnostic core files**
   - `agent_log_server/server.py`
   - `agent_log_server/static/codex_agent.ts`
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

5. **Existing harness conversation reload is transcript-first and lazy**
   - reloading/selecting a conversation that already exists on our harness replays the local `transcript.jsonl`
   - if a remote `thread_id` / `session_id` is already bound, the extension stays cold until the first new send
   - that first send may fail against a cold backend; the extension then resumes/reattaches and retries the buffered message
   - resume-time history / hydration noise must be suppressed until the backend ack because the local transcript is already present

6. **Live event output and transcript output must match**
   Any router that emits frontend events must write equivalent transcript entries for replay parity. Missing transcript fields become replay bugs.

7. **Internal-only debug data must be explicitly tagged**
   - use `internal: true` on both the live event and the transcript entry
   - normal frontend live-play, replay, and conversation-preview paths must ignore internal-tagged records
   - `/api/appserver/transcript/range` hides internal-tagged rows by default; opt in with `include_internal=true` when you need to inspect them directly

8. **Runtime UI/backend contracts are Socket.IO-only**
   - generic HTTP endpoints still exist for data loading, session browsing, debug, and admin flows
   - do not add HTTP fallback paths for runtime UI/backend behavior unless the user explicitly approves that fallback

## Terms used in this repo

- **new session from port-in**
  - import an external rollout/session into a fresh local conversation
  - bind the remote session/thread id
  - materialize flat transcript entries into local `transcript.jsonl`

- **existing conversation hydration**
  - replay an already-local `transcript.jsonl`
  - uses platform-agnostic frontend/server replay helpers
  - does not require backend-specific transport ownership
  - if a remote thread/session is already bound, keep the backend cold until the first new send
  - do not import vendor-native history during this ordinary reload/select flow

See also:

- `AGENTS.md` — short repo-wide invariant/guardrail
- `CODEX_APP_SERVER_EXTENSION.md` — architecture/reference implementation manual

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
| `extensions/codex_ext/runtime_protocol.py` | Stable Codex runtime schema/cache helper |
| `extensions/codex_ext_exp/runtime_protocol.py` | Experimental Codex runtime schema/cache helper |
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
      "id": "codex-ext",
      "name": "Codex Extension",
      "type": "codex_ext",
      "path": "codex_ext",
      "enabled": true
    },
    {
      "id": "codex-ext-testing",
      "name": "Codex Extension Testing (compat shim → codex-ext)",
      "type": "codex_ext",
      "path": "codex_ext",
      "enabled": false
    },
    {
      "id": "codex-ext-exp",
      "name": "Codex Extension (Experimental)",
      "type": "codex_ext_exp",
      "path": "codex_ext_exp",
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

### Availability and dependency gating

The merged extension registry stores more than install metadata. Loader/runtime state also tracks:

- `enabled`
- `dependency_status`
- `dependency_ok`
- `dependency_message`
- `active`
- `source_kind`
- `source_root`
- `install_source`
- `installer_meta`
- `version`
- `schema_version`

`active` is computed from the live runtime state:

```text
active = enabled && manifest_ok && dependency_ok
```

Practical consequences:

- only active extensions are handler-loadable and agent-selectable
- disabled or dependency-unmet extensions still appear in extension-management surfaces
- splash/settings UI surfaces `dependency_message` so the operator can see why an extension is unavailable
- dependency install/check hooks can move an extension from inactive to active without adding extension-specific code to the shared frontend

### Operator/admin package surfaces

The install/update/remove lifecycle is available through both the local operator CLI and the generic HTTP admin surface.

CLI:

- `codex-agent extension validate ...`
- `codex-agent extension install ...`
- `codex-agent extension update <id> ...`
- `codex-agent extension remove <id>`
- `codex-agent extension reload [extension_ids...]`

HTTP:

- `POST /api/extensions/validate`
- `POST /api/extensions/install`
- `POST /api/extensions/{extension_id}/update`
- `DELETE /api/extensions/{extension_id}`
- `POST /api/extensions/reload`

Both paths use the same loader/installer helpers. Package install/update is local filesystem + registry work first; reload, dependency install, and readiness are separate follow-up operations.

Important import note for cross-root extensions:

- relative imports are still fine for modules inside the same extension package, for example `.router` or `.dependencies`
- shared repo helper modules are different: user-installed extensions are loaded under synthetic package names, not under the builtin `extensions.<folder>` package tree
- when you need a shared helper that lives in the builtin top-level `extensions` package, use an absolute import such as:
  - `from extensions.tool_card_contracts import build_tool_card_request, build_tool_card_response`
- do not rely on parent-relative imports like `from ..tool_card_contracts import ...` for cross-root extensions

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
| `resume_session_with_history(...)` | session bind/import endpoint | Bind a backend session/thread to a local conversation; ordinary existing-conversation reload still stays cold until first-send lazy resume |
| `hydrate_transcript(...)` | session bind/import endpoint | Return flat transcript entries for a **new local conversation from port-in/import** |
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

Conversation-scoped runtime sends are transport-neutral at this backend seam.
During rollout, the frontend may reach it through either:

- legacy `/appserver` `send_message`
- `/rpc/conversations` `conversation.send`

For non-legacy agents:

```text
send_message | conversation.send
    └─ api_appserver_message / conversations RPC adapter
       └─ load meta.settings.agent
          └─ ext_loader.get_handler(agent).handle_message(...)
```

The frontend stays `conversation_id`-only on the normal send path; the backend still owns
thread/session lifecycle and extension dispatch.

If `agent == "codex"`, the compatibility lane returns the explicit legacy-disabled result.
Supported Codex traffic should use `codex-ext`, `codex-ext-exp`, or compatibility aliases
that resolve to those extensions.

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

For conversation-scoped runtime traffic, migrated event families are mirrored onto both live
lanes during rollout:

- legacy `/appserver` `appserver_event`
- `/rpc/conversations` `rpc.notify`

When the splash RPC toggle is enabled, the frontend suppresses duplicate legacy
`appserver_event` handling after the conversations RPC lane is connected.

For the built-in `codex` agent, the legacy compatibility surface remains disabled rather than
acting as a real provider fallback. `codex-ext` and `codex-ext-exp` use the extension-owned
route directly, and `codex-ext-testing` simply resolves to the `codex-ext` handler.

### Approval and interrupt plumbing

- approvals: `ext_loader.resolve_approval(...)`
- approval validation: `ext_loader.validate_pending_approval(...)`
- interrupts: `ext_loader.interrupt_session(...)`
- compaction: `ext_loader.compact_session(...)`

During rollout, the generic runtime controls can arrive through either transport:

- legacy `/appserver` shim calls: `interrupt`, `compact`
- `/rpc/conversations`: `conversation.interrupt`, `conversation.compact`

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
| `section` | shared schema modal | Read-only heading/description block |
| `info` | live provider/account/quota panels | Read-only information row |
| `cache: "none"` | live dynamic settings schemas | Refetch schema on modal open instead of using the frontend schema cache |
| runtime-generated schema | Codex app-server | Build UI fields directly from backend protocol schema |

Extension-owned settings are SSOT. The shared modal renderer must not depend on hidden builtin-Codex fields for extension save/hydrate behavior; the extension schema payload is the source of truth.

Dynamic schemas may also wrap a static template. Current examples:

- `codex-ext` / `codex-ext-exp` prepend live account + rate-limit information blocks
- `copilot-sdk` prepends live quota information blocks in front of a base schema template

Dynamic-source fields are resolved over the shared Socket.IO contract. If a TE2 relay/proxy sits in front of the appserver, it must explicitly forward the matching events (for example `get_extension_models` or `get_runtime_options`) instead of adding HTTP fallback logic.

There is no live builtin-Codex settings-schema lane. Extension-owned settings payloads are SSOT; legacy builtin-Codex compatibility endpoints, where they still exist, return explicit disabled/no-op results rather than an alternate settings flow.

## Reference implementation 1: Copilot SDK

`copilot-sdk` is the best example of a fleshed-out extension.

### Files

| File | Purpose |
|------|---------|
| `extensions/copilot_sdk/manifest.json` | extension metadata/capabilities |
| `extensions/copilot_sdk/settings_schema.json` | base schema template for session picker, model, policies, MCP, developer instructions |
| `extensions/copilot_sdk/client.py` | client lifecycle, session init/resume/send, approvals, transcript hydration, and dynamic settings-schema augmentation for live usage info |
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
  - for port-in/import, resumes the SDK session into in-memory state
  - does **not** redefine the ordinary existing-harness-conversation reload contract

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

## Reference implementation 2: Codex app-server extensions

`extensions/codex_ext` is the stable runtime-schema-driven example. `extensions/codex_ext_exp` is the experimental fork that layers dynamic developer-instruction / pending-context work on top of the same architecture. `codex-ext-testing` in the registry is only a compatibility alias that resolves to `codex-ext`, and it is disabled by default in the builtin registry.

### Registered extension IDs

The currently registered Codex extension IDs are:

- `codex-ext` — stable extension-owned Codex path
- `codex-ext-testing` — optional compatibility shim → `codex-ext` (disabled by default)
- `codex-ext-exp` — experimental fork with its own shellspec and transport label

Legacy builtin-Codex routes may still exist as compatibility shims, but they are not a live runtime path. They return explicit disabled/no-op results that direct callers to `codex-ext` or `codex-ext-exp` through the generic extension surface.

`codex-ext`:

- exercises the generic extension architecture end to end
- uses runtime-generated settings schema, session picker, send/receive hooks, and bind/import hooks
- owns its own framework-shell-backed app-server transport (`app-server:codex-extension`)
- is the active proving ground for the schema-driven, extension-owned transport approach

`codex-ext-exp`:

- reuses the same generic extension architecture and runtime-schema approach
- keeps a separate shellspec/transport label so it can point at a patched `codex-app-server` binary
- is where dynamic developer-instruction / pending-context behavior is being explored without destabilizing the stable path

### Patched app-server build behind `codex-ext-exp`

`codex-ext-exp` is backed by a local custom `codex-rs` checkout rather than the stock `codex app-server` on `PATH`.

- local repo checkout: `~/downloads/codex/codex-rs`
- upstream base tag: `rust-v0.117.0-alpha.5`
- patch branch: `patch/dynamic-developer-instructions`
- binary path: `~/downloads/codex/codex-rs/target/debug/codex-app-server`
- why it exists: the patch adds mid-thread / per-turn `developer_instructions` override support so pending-context and repo-memory updates can take effect on the next turn without restarting the thread
- Termux build recipe:
  - `pkg install libzstd`
  - `cd ~/downloads/codex/codex-rs`
  - `ZSTD_SYS_USE_PKG_CONFIG=1 CC=cc cargo build -p codex-app-server`
- source-of-truth patch note in the Codex repo:
  - `~/downloads/codex/codex-rs/docs/patched_app_server.md`

### Runtime protocol architecture

These extensions do **not** depend on a committed schema artifact anymore.

At runtime it:

1. runs `codex --version`
2. computes a versioned cache directory:
   - `~/.cache/app_server/codex_app_server_schema/<version>/`
3. runs:
   - `codex app-server generate-json-schema --out <cache_dir>`
4. loads the generated bundle
5. builds in-memory registries from:
   - `ClientRequest` request params
   - response definitions in the schema bundle
   - `ServerRequest`
   - `ServerNotification`
   - `EventMsg`

The stable runtime protocol/cache logic lives in `extensions/codex_ext/runtime_protocol.py`. The experimental fork mirrors the same pattern in `extensions/codex_ext_exp/runtime_protocol.py`.

Response registries are keyed by lowercase method names. Manual overrides should only exist where the schema naming and method naming are genuinely semantically different; they should not be used as a lazy substitute for building the registry from the schema bundle.

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

- **response decoding**
  - JSON-RPC results are validated/decoded through the runtime response registry
  - response lookups are keyed by lowercase method names
  - the schema bundle / TS bindings define the semantics and typing for those responses

- **event routing**
  - the router checks runtime-known notification and event types before translating them
  - the experimental fork reuses the same runtime-shaped request/event model while layering in its dynamic developer-instruction flow

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
  - for port-in/import, resumes the selected Codex thread through the extension-owned transport
  - does **not** redefine the ordinary existing-harness-conversation reload contract

- **`hydrate_transcript(...)`**
  - supports the **session from port-in** flow
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

- **`compact_session(...)`**
  - powers the generic frontend `compact` flow via `ext_loader.compact_session(...)`
  - resumes the thread first when transport state requires it, then sends `thread/compact/start`

### framework_shells interaction

Unlike the Copilot SDK extension, Codex app-server does use `framework_shells`.

- `codex-ext` starts/adopts a dedicated shell labeled `app-server:codex-extension` and uses `shellspec/app_server.yaml#app_server_observed`
- `codex-ext-exp` starts/adopts a dedicated shell labeled `app-server:codex-experimental` and uses `extensions/codex_ext_exp/shellspec/app_server_exp.yaml#app_server_exp_observed`
- both paths use observed shellspec entries (`app_server_observed` / `app_server_exp_observed`) for framework-shell observability
- both observed shellspecs now run the real app-server binary directly and rely on framework-shells pipe stdout logging/subscriptions for observability instead of stderr mirroring
- pipe-backed consumers must treat framework-shells as the owner of stdout consumption:
  - reads go through `subscribe_output()` / `subscribe_output_bytes()`
  - stdin writes go through `write_to_pipe()`
  - direct `state.process.stdout.read(...)` loops are invalid because they race framework-shells' own tee/log readers
- both handlers harden startup by restarting locally when an adopted shell lacks a live stdin pipe
- `codex-ext-exp` keeps a distinct transport label/shellspec so it can point at the patched `codex-app-server` binary without process adoption conflicts; see `~/downloads/codex/codex-rs/docs/patched_app_server.md` for the patched build details

### Current state of the Codex extensions

What works now:

- runtime schema generation from the installed binary
- versioned schema cache
- dynamic schema-driven settings
- live provider information blocks in the schema-driven settings modal
- runtime-schema-backed reasoning summary setting persisted through the shared settings/meta flow
- session picker / new session from port-in
- request building from runtime schema
- response decoding from the runtime schema registry
- extension-owned app-server transport
- real two-way message flow
- interrupt support
- compaction support through the generic `compact` flow
- stderr RPC observability via the wrapper shell
- extension-owned MVP live routing
- `codex-ext-exp` adds dynamic developer-instruction / pending-context experimentation on a patched app-server binary

What is intentionally still evolving:

- richer tool-card/render parity
- continued approval-path parity across the stable and experimental forks

Existing conversation hydration is already handled through the local transcript replay path and is not an extension-specific transport concern.

## How to build a new agent extension

Use the Copilot SDK and Codex app-server implementations as the two reference patterns.

Do **not** copy the disabled builtin-Codex compatibility shims. A real extension must plug into the same generic `server.py` / `ext_loader` hook surface as `copilot-sdk`, `codex-ext`, and `codex-ext-exp`, so it also works when loaded from non-builtin extension roots.

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

`manifest.json` must include a non-empty `version` string for both builtin and
user-installed extensions. If you are adding versioning to an older manifest
for the first time, start at `0.1.0`.

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
- dynamic schema hooks may also wrap a static template so they can prepend live read-only `section` / `info` fields
- use `cache: "none"` when the schema includes live provider/account/quota data that must refresh on each modal open
- if the extension participates in shared footer/runtime quick controls, expose that through schema metadata (`runtime_option`) 
- for plan/collaboration mode, expose a normal enumerated schema field (for example `mode`) and set the capability in the manifest

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
  - for **new local conversation from port-in/import**, it may also prepare live backend state if that import flow requires it
  - for **ordinary reload/select of an existing harness conversation**, it must **not** eagerly load/attach the backend session
- `hydrate_transcript(...)`
  - return flat transcript entries for a **new session from port-in**
- existing harness conversation hydration/reload
  - replay the already-local transcript through the generic platform-agnostic helpers
  - leave the backend cold until the first new send
  - use the buffered-send lazy resume/retry path when that first send hits a cold backend
  - suppress resume/load history noise until the backend ack so it does not duplicate the already-local transcript

Some backends only expose a history-bearing `load_session()` path and do not advertise a true `session/resume` capability. In that case, `load_session()` may act as the synthetic reattach ack for the retry path, but suppression still has to stay active until replay updates reach a real quiet period. Do not treat RPC completion by itself as proof that replay delivery is finished.

This lifecycle split is part of the repo contract, not an extension-specific preference.

See also:

- `AGENTS.md`
- `CODEX_APP_SERVER_EXTENSION.md`

### 7. Add a router if the backend streams events

If your backend emits live updates, put protocol translation in `router.py` and surface it through `route_event(...)`.

Do **not** teach `server.py` about your backend event names directly.

If your extension exposes plan/collaboration mode, the router should translate live mode changes into the shared `mode` event/transcript shape, and any live plan updates into the shared `plan_state` shape. Replay must see the same fields the live UI sees.

### 8. Preserve transcript parity

If the live UI sees a field, replay should also see it later. That means router output must always have a transcript equivalent whenever replay depends on it.

See `TRANSCRIPT_CARD_CONTRACTS.md` for the shared generic card shapes and replay-parity rules.

### 9. Validate the extension end to end

Recommended checklist:

- extension appears in `/api/extensions`
- settings schema loads correctly
- if schema is dynamic/live, repeated opens refetch and show fresh data when the schema declares `cache: "none"`
- model list loads correctly
- first message creates or resumes backend state
- session picker works if implemented
- new session from port-in works if implemented
- transcript replay still works locally
- with the splash RPC toggle enabled, replay/send/interrupt/compact and migrated live
  notifications work over `/rpc/conversations` without duplicate rows
- with the splash RPC toggle disabled, the legacy `/appserver` compatibility lane still works
- ordinary existing-conversation reload with a bound session stays transcript-first and cold, and the first-send retry path does not absorb delayed replay updates into the live turn
- approvals/interrupts work if the backend supports them
- stderr/log observability works if the extension owns a custom transport wrapper
- live output and replay output match

## Practical guidance on choosing a pattern

Use the **Copilot SDK pattern** when:

- a Python library already abstracts backend transport
- backend events are rich and callback-driven

Use the **Codex app-server pattern** when:

- the backend exposes a machine-readable runtime schema
- request and event shapes change with backend version
- you want protocol-driven settings/request generation rather than hand-maintained constants
- you need extension-owned transport/runtime control while keeping the frontend unified

## Current project direction

The extension architecture is now the intended long-term path.

- `copilot-sdk` is the mature example
- `codex-ext` is the live runtime-schema-driven, extension-owned Codex path
- `codex-ext-exp` is the experimental fork for dynamic context injection and patched app-server work
- `codex-ext-testing` remains a compatibility alias to `codex-ext`
- the next major Codex slices continue to be tool/render parity and approval-path parity

## Rename note

This document replaces the old `ACP_INTEGRATION.md` name because the content now covers the generic extension system, not ACP specifically.

The `acp/` directory name is still historical and can be cleaned up later if we want a wider documentation rename pass.

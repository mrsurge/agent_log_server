# Copilot SDK Extension — Pluggable Integration Plan

## Philosophy
`codex_agent.js` is **platform-agnostic**. The existing codex logic is a working reference — not to be patched with SDK-specific code. New extensions plug in via:
1. **`extensions/__init__.py`** — loader routes by type
2. **`settings_schema.json`** — schema-driven UI fields
3. **`settings_schema.js`** — generic schema renderer (handles all field types including `session_picker`)
4. **Server.py extension endpoints** — HTTP routes for extension-specific APIs
5. **`conversation_update` + `meta.json`** — generic `thread_id` binding (same field codex uses)

## Architecture Mapping: Codex → Copilot SDK

| Concept | Codex | Copilot SDK |
|---------|-------|-------------|
| Backend process | `codex-app-server` binary via FWS | `CopilotClient` (SDK manages subprocess) |
| Session identity | `thread_id` from `thread/started` | `session.session_id` from `create_session()` |
| Session storage | `meta.json["thread_id"]` | `meta.json["thread_id"]` (same field!) |
| Resume trigger | `thread/resume` JSON-RPC on first message | `client.resume_session(thread_id)` on first message |
| Session listing | Rollout files in `~/.codex/sessions/` | `client.list_sessions()` |
| Message send | `turn/start` JSON-RPC | `session.send(MessageOptions)` |
| Event stream | stdout JSON-RPC notifications | `session.on(handler)` → `SessionEvent` |
| Transcript | Our `transcript.jsonl` (SSOT) | Our `transcript.jsonl` (SSOT) — same! |
| Transcript hydration on resume | Parse our JSONL via `replayTranscript()` | Parse our JSONL via `replayTranscript()` — same! |
| Delete conversation | Remove local dir only, rollout persists | Remove local dir only, SDK session persists |
| Approval | JSON-RPC response to `requestApproval` | `PermissionRequestResult` via async handler |
| Model list | `model/list` JSON-RPC | `client.list_models()` |
| Settings injection | Backend intercepts RPC, injects from SSOT | `SessionConfig`/`ResumeSessionConfig` params |

## Key Invariants (MUST match codex behavior)

1. **`conversation_id` ≠ `thread_id`** — our ID vs SDK's ID, stored in `meta.json["thread_id"]`
2. **No eager SDK resume on conversation select** — just load meta + `replayTranscript()` from JSONL
3. **Lazy resume on first message** — `handle_message()` checks `meta["thread_id"]`, calls `resume_session()` if needed
4. **Delete = local only** — never call `client.delete_session()`
5. **Our JSONL is source of truth** — SDK's `get_messages()` is NOT used for transcript
6. **`codex_agent.js` has ZERO SDK-specific code** — all extension UI via schema system

## Conversation Lifecycle (Copilot SDK)

### A. New Conversation (no session picked)
```
Frontend                    server.py                     copilot_sdk_client.py
   │                              │                              │
   │ conversation_create          │                              │
   │─────────────────────────────>│ Generate UUID, create dir    │
   │                              │ Write meta.json (thread_id=null)
   │ 200 {meta}                   │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │ conversation_update          │                              │
   │ {settings, thread_id: undef} │                              │
   │─────────────────────────────>│ Save settings to meta.json   │
   │                              │                              │
   │ send_message {text}          │                              │
   │─────────────────────────────>│ handle_message()             │
   │                              │──────────────────────────────>│
   │                              │  No thread_id → init_session()│
   │                              │  SDK generates session_id     │
   │                              │  Store as meta["thread_id"]   │
   │                              │  session.send(text)           │
   │                              │                              │
   │ WS: events stream            │  session.on(handler) fires   │
   │<─────────────────────────────│<──────────────────────────────│
   │                              │  Router writes transcript.jsonl
```

### B. New Conversation (session picked from Browse)
```
Frontend                    server.py                     copilot_sdk_client.py
   │                              │                              │
   │ [settings_schema.js renders  │                              │
   │  session_picker field]       │                              │
   │ [user clicks Browse]         │                              │
   │ [schema calls get_sessions]  │                              │
   │─────────────────────────────>│ list_sessions()              │
   │                              │──────────────────────────────>│
   │ {sessions: [...]}            │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │ [user picks session, populates│                             │
   │  input field, hits Save]     │                              │
   │                              │                              │
   │ conversation_update          │                              │
   │ {settings, thread_id: "sdk-session-id"}                     │
   │─────────────────────────────>│ Save thread_id to meta.json  │
   │                              │ (line 4495-4498 already does this!)
   │                              │                              │
   │ [first message]              │                              │
   │ send_message {text}          │ handle_message()             │
   │─────────────────────────────>│──────────────────────────────>│
   │                              │  Has thread_id → resume_session()
   │                              │  session.send(text)          │
   │                              │                              │
```

### C. Switching to Existing Conversation
```
Frontend                    server.py
   │                              │
   │ conversation_select {id}     │
   │─────────────────────────────>│ Load meta.json
   │                              │ NO eager SDK resume!
   │ 200 OK                       │
   │<─────────────────────────────│
   │                              │
   │ GET /transcript/range        │
   │─────────────────────────────>│ Read transcript.jsonl (OUR data)
   │ [transcript entries]         │
   │<─────────────────────────────│
   │                              │
   │ [renders via replayTranscript()]
   │                              │
   │ send_message {text}          │  ← only NOW does SDK resume happen
   │─────────────────────────────>│ handle_message() → resume_session()
```

### D. Delete Conversation
```
Frontend                    server.py                     copilot_sdk_client.py
   │                              │                              │
   │ conversation_delete {id}     │                              │
   │─────────────────────────────>│ Remove meta.json, transcript │
   │                              │ destroy_session() (in-memory only)
   │                              │──────────────────────────────>│
   │                              │  Pop from _sessions, _routers│
   │                              │  SDK session LEFT ALIVE       │
   │ 200 OK                       │                              │
   │<─────────────────────────────│                              │
```

## What Lives Where

### `extensions/copilot_sdk_client.py` (backend handler)
- `init_session()` — create SDK session, store session_id as `meta["thread_id"]`
- `resume_session()` — read `meta["thread_id"]`, call `client.resume_session()`
- `handle_message()` — lazy init/resume, then `session.send()`
- `list_sessions()` — `client.list_sessions()`, return JSON-safe dicts
- `list_models()` — `client.list_models()`, convert to JSON-safe dicts
- `destroy_session()` — in-memory cleanup only
- `delete_session()` — calls `destroy_session()` (no SDK delete!)
- `resolve_approval()` — resolve pending Future

### `extensions/copilot_sdk_router.py` (event translator)
- `route_event()` — maps `SessionEvent` → internal format
- Calls `broadcast_fn()` (→ frontend WS) and `transcript_fn()` (→ JSONL)
- Handles: message deltas, reasoning, tool start/complete/progress, turn lifecycle

### `extensions/copilot_sdk/settings_schema.json` (UI schema)
- `cwd` — path field
- `session` — `session_picker` field (only visible when no thread_id)
- `model` — select with `dynamic_source` (fetches from backend)
- `reasoning_effort` — select

### `static/modals/settings_schema.js` (generic schema renderer)
- `session_picker` field type → renders input + Browse button
- Hides when `conversationMeta.thread_id` exists
- Browse button calls `openSessionPicker()` (exposed via helpers)
- **Session picker overlay, list rendering, fetch** → ALL MUST LIVE HERE, not in codex_agent.js

### `codex_agent.js` (ZERO SDK code)
- `conversation_update` already passes `thread_id` from payload → backend stores it
- `replayTranscript()` already works for any extension (reads our JSONL)
- `handleEvent()` already routes any `appserver_event` by type
- `respondApproval()` — needs to be generic (currently hardcodes codex RPC format)

### `server.py`
- Extension endpoints: `/api/extensions/copilot-sdk/models`, `/sessions`, `/sessions/resume`
- SIO handlers mirror HTTP endpoints
- `conversation_update` already handles `thread_id` binding (line 4495)
- `conversation_select` does NOT resume SDK (lazy on first message)

## Current Status

### Working ✅
- [x] `copilot_sdk_client.py` — init, resume, handle_message, lazy resume logic
- [x] `copilot_sdk_router.py` — event translation, broadcast, transcript write
- [x] Extension loader wiring (`__init__.py`, `extensions.json`)
- [x] Settings schema (manifest, schema JSON)
- [x] Model listing (JSON-safe serialization fixed)
- [x] Approval flow (Future-based permission handler)
- [x] `delete_session()` — local-only, no SDK delete
- [x] No eager resume on conversation select
- [x] `conversation_id` ≠ `thread_id` mapping

### Broken / TODO 🔧
- [ ] **Session picker in wrong place** — 90 lines of SDK code polluting `codex_agent.js`, needs to move to `settings_schema.js`
- [ ] **Approval routing** — `respondApproval()` in codex_agent.js has SDK-specific branch, needs to be generic
- [ ] **SIO migration** — reverted; frontend is back on HTTP POST (backend supports both, no breakage)
- [ ] **Thread hydration on resume** — verify router events flow to transcript correctly when SDK session resumes
- [ ] **ACP cleanup** — delete old `acp_client.py`, `acp_router.py`, `acp/` directory
| `tool.execution_start` | `shell_begin` | `data.content` has tool info |
| `tool.execution_complete` | `shell_end` | Exit code, output |
| `tool.execution_progress` | `shell_delta` | Streaming tool output |
| `session.error` | `error` | |
| `assistant.usage` | `token_usage` | Token counts |
| `assistant.intent` | `activity` | Intent text for ribbon |
| `session.start` | Session metadata | |
| `session.resume` | Session resumed | |

## Todos

### 1. copilot-sdk-client — Create `extensions/copilot_sdk_client.py`
New handler module (replaces `acp_client.py`). Responsibilities:
- Global `CopilotClient` singleton (like `_shared_shells`)
- `init_copilot_manager()` — initialize with server callbacks
- `warm_up_extension()` — start CopilotClient, call `client.start()`, `client.ping()`
- `init_session()` — `client.create_session(config)` with streaming, permission handler, hooks
- `handle_message()` — `session.send({"prompt": text})` and rely on event handler
- `resume_session()` — `client.resume_session(session_id)` on reconnect
- Session tracking: `conversation_id → CopilotSession` mapping
- `list_models()` — expose `client.list_models()` to server
- Debug raw buffer (keep existing pattern)

### 2. copilot-sdk-router — Create `extensions/copilot_sdk_router.py`
New event router (replaces `acp_router.py`). Responsibilities:
- `CopilotEventRouter` class with same interface as `ACPEventRouter`
- `route_event(event: SessionEvent)` — translate SDK events to internal format
- Block-based ID tracking for interleaved reasoning/message (same pattern)
- Turn counter, sequence numbers, transcript recording
- Permission request → approval_request broadcast (future: user approval UI)

### 3. ext-loader-update — Update `extensions/__init__.py`
- Add `"copilot_sdk"` type in `_load_handler_for_type()`
- Import and init `copilot_sdk_client` module
- Keep existing interface (no changes to function signatures)

### 4. extensions-json — Update `extensions/extensions.json`
- Replace `gemini-acp` entry with `copilot-sdk` entry
- Type: `"copilot_sdk"`, id: `"copilot-sdk"`

### 5. copilot-manifest — Create `extensions/copilot_sdk/manifest.json`
- Extension metadata (command, capabilities, models)
- `eagerSessionInit: true` (start client on settings save)

### 6. copilot-settings-schema — Create `extensions/copilot_sdk/settings_schema.json`
- CWD field (path type with browse)
- Model selector (populated dynamically from `list_models()`)
- Reasoning effort selector
- Approval policy selector

### 7. server-model-list — Add model list endpoint for Copilot SDK
- `GET /api/extensions/copilot-sdk/models` → calls `client.list_models()`
- Returns `ModelInfo` list for dynamic dropdown population

### 8. server-debug-raw — Update debug endpoint
- `/api/extensions/debug/raw` currently imports from `acp_client`
- Update to route to `copilot_sdk_client.get_raw_buffer()` instead

### 9. server-session-resume — Wire session resume in conversation switch
- On conversation switch where `meta.settings.agent == "copilot-sdk"`:
  - If session exists in SDK client, just foreground it
  - If not, call `client.resume_session(session_id)` using stored session_id from meta

### 10. cleanup-acp — Remove ACP files (after copilot_sdk is working)
- Delete `extensions/acp_client.py`
- Delete `extensions/acp_router.py`
- Delete `extensions/acp/` directory
- Remove `acp` type from `__init__.py`
- Remove `import acp` dependency

## Phase 2: Migrate All Frontend↔Backend Communication to Socket.IO

### Problem
Despite Socket.IO being the intended architecture, all frontend→backend calls use HTTP POST/GET (`postJson()` / `fetch()`). The `/appserver` Socket.IO namespace is broadcast-only (server→client events). The raw WebSocket layer (`_appserver_ws_clients_ui`) is dead weight — nothing on the frontend consumes it.

### Architecture
- **Single namespace**: `/appserver` for both events and control
- **Server→Client**: `socketio_server.emit("appserver_event", ...)` (existing, keep as-is)
- **Client→Server**: `socket.emit("event_name", data, ackCallback)` — Socket.IO ack for request/response
- **HTTP endpoints remain** as internal/debug API, not used by frontend
- **Fallback**: `sioCall()` helper on frontend falls back to HTTP if socket disconnected

### Todos

#### Phase 2a: Infrastructure
- [ ] `sio-frontend-helper` — Create `sioCall(event, data)` Promise wrapper on frontend
- [ ] `sio-server-registry` — Create server-side SIO handler registration pattern

#### Phase 2b: Real-time paths (highest value)
- [ ] `sio-send-message` — `sendUserMessage()` → SIO `"send_message"`
- [ ] `sio-shell-exec` — `sendShellCommand()` → SIO `"shell_exec"`
- [ ] `sio-rpc` — Codex RPC calls → SIO `"rpc"`
- [ ] `sio-interrupt` — `interrupt()` → SIO `"interrupt"`

#### Phase 2c: Conversation CRUD
- [ ] `sio-convo-get` — GET conversation → SIO `"conversation_get"`
- [ ] `sio-convo-create` — POST conversation → SIO `"conversation_create"`
- [ ] `sio-convo-list` — GET conversations → SIO `"conversations_list"`
- [ ] `sio-convo-select` — POST select → SIO `"conversation_select"`
- [ ] `sio-convo-delete` — DELETE conversation → SIO `"conversation_delete"`
- [ ] `sio-convo-draft` — POST draft → SIO `"conversation_draft"`
- [ ] `sio-convo-bind-rollout` — POST bind → SIO `"conversation_bind_rollout"`

#### Phase 2d: Settings & Data
- [ ] `sio-view` — POST view → SIO `"set_view"`
- [ ] `sio-models` — GET models → SIO `"get_models"`
- [ ] `sio-rollouts` — GET rollouts → SIO `"get_rollouts"`
- [ ] `sio-rollout-preview` — GET rollout preview → SIO `"get_rollout_preview"`
- [ ] `sio-extensions` — GET extensions → SIO `"get_extensions"`
- [ ] `sio-sessions` — GET copilot sessions → SIO `"get_sessions"`
- [ ] `sio-session-resume` — POST session resume → SIO `"session_resume"`
- [ ] `sio-status` — GET status → SIO `"get_status"`
- [ ] `sio-start-stop` — POST start/stop → SIO `"app_start"` / `"app_stop"`
- [ ] `sio-compact` — POST compact → SIO `"compact"`
- [ ] `sio-approval-record` — POST approval record → SIO `"approval_record"`
- [ ] `sio-convo-meta` — GET conversation meta by ID → SIO `"conversation_meta"`
- [ ] `sio-transcript` — GET transcript → SIO `"get_transcript"`

#### Phase 2e: Cleanup
- [ ] `sio-strip-raw-ws` — Remove `_appserver_ws_clients_ui` raw WS broadcast layer
- [ ] `sio-approval-flow` — Wire approval request/response through SIO (cherry on top)

### Notes
- Socket.IO ack pattern: server handler `return`s the value → client receives in callback
- `sioCall()` wraps this as `new Promise((resolve) => socket.emit(evt, data, resolve))`
- HTTP fallback in `sioCall()` uses existing `postJson()`/`fetch()` paths
- Migration is incremental: swap one call at a time, test, repeat
- Raw WS removal is last step after all calls confirmed working on SIO

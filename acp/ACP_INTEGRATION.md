# Copilot SDK Extension

> GitHub Copilot (via Python SDK) as a pluggable agent extension — replaces the deprecated ACP/Gemini integration

## Overview

The Copilot SDK extension connects the agent_log_server to GitHub Copilot using the official `copilot` Python package (PyPI). It replaces the previous ACP/Gemini integration, which lacked session resume, required ~60s cold start, and used a bespoke JSON-RPC stdin/stdout protocol.

**Key advantages over ACP:**
- **Session resume** — SDK sessions persist on disk; conversations survive server restarts
- **No process management** — SDK manages its own `copilot --headless --stdio` subprocess (`auto_start=True`)
- **Async-native** — all SDK methods are `async` coroutines, no thread bridge needed
- **Multi-model** — access to all Copilot models (Claude, GPT, Gemini, etc.) via `list_models()`
- **Schema-driven UI** — frontend is entirely data-driven from JSON manifests

**Key principle:** Frontend is a dumb renderer. All conversation logic, event routing, and state management happens in the backend.

## Architecture

```
Frontend (codex_agent.js — platform-agnostic, ZERO SDK code)
    │ Socket.IO (/appserver namespace)
    ▼
┌─────────────────────────────────────────────┐
│           server.py (platform-agnostic)      │
│  _broadcast_appserver_ui()                   │
│  _append_transcript_entry()                  │
│  _write_transcript_entries()                 │
│                                              │
│  /api/appserver/message                      │
│       │                                      │
│       ├── agent == "codex" ──► Codex path    │
│       │                                      │
│       └── agent in extensions ──► ext_loader │
│                                              │
│  extensions/__init__.py (ext_loader)         │
│       │ ALL extension calls go through here  │
│       │ NEVER direct handler imports         │
│       ▼                                      │
│  ext_loader.handle_message()                 │
│  ext_loader.hydrate_transcript()             │
│  ext_loader.list_models()                    │
│  ext_loader.list_sessions()                  │
│  ext_loader.resolve_approval()               │
│                   │                          │
└───────────────────┼──────────────────────────┘
                    │
                    ▼
            extensions/copilot_sdk_client.py
                    │ session mgmt, handle_message, hydrate_transcript
                    ▼
            extensions/copilot_sdk_router.py
                    │ live event translation (deltas, tool calls, etc.)
                    ▼
            copilot Python SDK (CopilotClient)
                    │ internal subprocess
                    ▼
            copilot --headless --stdio
```

**INVARIANT:** `server.py` uses ONLY `ext_loader` methods. No `from extensions.copilot_sdk_client import X`. See `AGENTS.md` for the full invariant.

## File Structure

| File | Purpose |
|------|---------|
| `extensions/__init__.py` | Generic extension loader — reads `extensions.json`, loads handlers by type |
| `extensions/copilot_sdk_client.py` | Session management, `handle_message()`, resume, list models/sessions |
| `extensions/copilot_sdk_router.py` | SDK event → internal format translation |
| `extensions/extensions.json` | Extension registry |
| `extensions/copilot_sdk/manifest.json` | Extension metadata (`eagerSessionInit`, capabilities) |
| `extensions/copilot_sdk/settings_schema.json` | Settings UI schema (CWD, model dropdown, reasoning effort) |
| `static/modals/settings_schema.js` | Dynamic settings field renderer (supports `dynamic_source`) |

## Extension Loading

### Registry (`extensions/extensions.json`)

```json
{
  "version": "1.0",
  "extensions": [
    {
      "id": "copilot-sdk",
      "name": "GitHub Copilot (SDK)",
      "type": "copilot_sdk",
      "path": "copilot_sdk",
      "enabled": true
    }
  ]
}
```

### Loading Flow

1. On server startup, `_init_extensions()` calls `ext_loader.load_extensions()`
2. Loader reads `extensions.json` and loads handler modules by type
3. Type `"copilot_sdk"` → loads `extensions/copilot_sdk_client.py`
4. Handler is initialized with callbacks to server infrastructure:
   - `broadcast_fn` — `_broadcast_appserver_ui()`
   - `transcript_fn` — `_append_transcript_entry()`
   - `meta_fns` — `{load: _load_conversation_meta, save: _save_conversation_meta}`

### Adding New Agent Types

To add a new agent:
1. Add entry to `extensions/extensions.json`
2. Create manifest at `extensions/<type>/manifest.json`
3. Create settings schema at `extensions/<type>/settings_schema.json`
4. Create handler module at `extensions/<type>_client.py`
5. Add type loader in `extensions/__init__.py`

**server.py never mentions specific agents** — fully pluggable.

## Message Flow

### 1. User Sends Message

```
POST /api/appserver/message {conversation_id, text}
    │
    ▼
Check meta.settings.agent
    │
    ├── "codex" ──► existing Codex flow
    │
    └── "copilot-sdk" ──► ext_loader.get_handler("copilot_sdk").handle_message(...)
```

### 2. Session Lifecycle

```python
# Server startup - initialize SDK client:
warm_up_extension("copilot-sdk")
    │
    ├── Create CopilotClient(auto_start=True, auto_restart=True)
    │   └── SDK spawns `copilot --headless --stdio` internally
    │
    ├── await client.start()
    │
    └── Mark extension as ready

# Settings save - create session eagerly:
init_session(conversation_id, "copilot-sdk", cwd, model)
    │
    ├── await client.create_session(SessionConfig(...))
    │   └── Returns CopilotSession object
    │
    ├── Create CopilotEventRouter (stores broadcast/transcript callbacks)
    │
    ├── session.on() to register event callback
    │   └── SDK dispatches events via call_soon_threadsafe
    │
    └── Store session_id as thread_id in conversation meta

# First message (session already exists):
handle_message(conversation_id, text, agent_type, settings)
    │
    ├── Ensure session exists (create if missing)
    │
    └── await session.send(text)
        └── SDK streams events back through registered callback
```

### 3. Event Routing (Callback Model)

Unlike ACP (which required a reader loop on stdout), the Copilot SDK uses a callback model:

```python
# Register event handler (sync call, but dispatched on event loop)
unsub = session.on(lambda event: asyncio.get_running_loop().create_task(
    router.route_event(event)
))

# SDK reader thread → call_soon_threadsafe → our handler → event loop task
```

The SDK's internal `JsonRpcClient` reads from subprocess stdout in a daemon thread and dispatches notifications via `loop.call_soon_threadsafe()`, so our handler runs on the main event loop.

## Event Translation Table

| SDK Event Type | Internal Event Type | Frontend Handler |
|----------------|---------------------|------------------|
| `MESSAGE_DELTA` | `assistant_delta` | `appendAssistantDelta()` |
| `MESSAGE_COMPLETE` | `assistant_finalize` | `finalizeAssistant()` |
| `THINKING_DELTA` | `reasoning_delta` | `appendReasoningDelta()` |
| `THINKING_COMPLETE` | `reasoning_finalize` | `finalizeReasoning()` |
| `TOOL_CALL_START` | `shell_begin` | `renderShellBegin()` |
| `TOOL_CALL_DELTA` | `shell_delta` | `renderShellDelta()` |
| `TOOL_CALL_COMPLETE` | `shell_end` | `renderShellEnd()` |
| `TURN_COMPLETE` | `turn_completed` | `completeTurn()` |
| `TOKEN_USAGE` | `token_usage` | `updateTokens()` |
| `PLAN` | `plan` | `renderPlanCard()` |
| `PERMISSION_REQUEST` | `approval_request` | `showApprovalUI()` |

## Event IDs and Ordering

All events include fields for proper frontend rendering and ordering:

### ID Fields

Each event type has a unique `id` field for DOM element accumulation:

| Event Type | ID Pattern | Purpose |
|------------|------------|---------|
| `message` (user) | `user_{turn}` | User message block |
| `assistant_delta` | `msg_{turn}_{block}` | Assistant message accumulation |
| `assistant_finalize` | `msg_{turn}_{block}` | Matches delta for finalization |
| `reasoning_delta` | `reasoning_{turn}_{block}` | Reasoning block accumulation |
| `shell_begin/delta/end` | `tool_{turn}_{block}` | Tool call block |
| `plan` | `plan_turn_{turn}` | Plan card |
| `approval_request` | has `request_id` | Approval UI |

### Block-Based IDs for Interleaved Events

Copilot interleaves reasoning and message events within a turn. The frontend creates one DOM row per unique `id`. To preserve order, we use block-based IDs that increment each time the event type switches:

| Event sequence | ID | Result |
|----------------|-----|--------|
| thinking chunk | `reasoning_1_1` | New row |
| thinking chunk | `reasoning_1_1` | Appends to same row |
| message chunk | `msg_1_2` | New row (below reasoning) |
| tool call | `tool_1_3` | New row (below message) |
| message chunk | `msg_1_4` | New row (below tool) |

Format: `{type}_{turn}_{block}` where block increments on each type switch.

**Critical fix:** `_handle_tool_start()` must set `self._last_block_type = "tool"` — otherwise post-tool message deltas reuse the pre-tool block ID, causing DOM updates in the wrong row.

### Ordering Fields

Every event includes:

| Field | Purpose |
|-------|---------|
| `seq` | Global sequence number (monotonically increasing) |
| `turn_id` | Groups events within a turn (`turn_1`, `turn_2`, etc.) |

```json
{
  "type": "assistant_delta",
  "conversation_id": "abc123",
  "id": "msg_1_2",
  "delta": "Hello",
  "turn_id": "turn_1",
  "seq": 42
}
```

### Transcript Entries

Transcript entries mirror the ID structure for replay:

```jsonl
{"role": "user", "id": "user_1", "text": "Hello", "turn_id": "turn_1", "seq": 1, "timestamp": "..."}
{"role": "reasoning", "id": "reasoning_1_1", "text": "Thinking...", "turn_id": "turn_1", "seq": 5, "timestamp": "..."}
{"role": "assistant", "id": "msg_1_2", "text": "Hi!", "turn_id": "turn_1", "seq": 10, "timestamp": "..."}
{"role": "command", "id": "tool_1_3", "command": "ls", "output": "...", "turn_id": "turn_1", "seq": 15, "timestamp": "..."}
{"role": "status", "status": "success", "turn_id": "turn_1", "seq": 20, "timestamp": "..."}
```

## Session Resume

### How It Works

The Copilot SDK persists sessions on disk via the CLI's internal state. This gives us free session resume:

```
Server Restart
    │
    ├── CopilotClient.start() → reconnects to CLI subprocess
    │
    ├── User opens conversation with saved thread_id
    │
    ├── client.resume_session(session_id) → reconnects to persisted session
    │
    └── (Resume is LAZY — happens on first message, not on conversation select)
```

### Transcript Hydration (bind-rollout Pattern)

When binding an existing SDK session to a new conversation via the session picker, the server follows the same pattern as Codex rollout binding:

```
1. ext_loader.resume_session_with_history()  → Bind session (meta + SDK resume)
2. ext_loader.hydrate_transcript()           → Build flat entries from get_messages()
3. _write_transcript_entries(convo_id, items) → Write to transcript.jsonl
4. Return response → Frontend calls replayTranscript()
```

**Critical:** Steps 1-3 are **synchronous** (awaited inline) — the HTTP response MUST NOT return until transcript is written. Otherwise the frontend's `replayTranscript()` finds an empty transcript (this was a real bug caused by `asyncio.create_task()` fire-and-forget).

The `hydrate_transcript()` handler in `copilot_sdk_client.py` calls `session.get_messages()` and converts `SessionEvent` objects into the standard transcript format:

```python
# Flat entries matching _rollout_preview_entries() output:
{"role": "user", "text": "...", "ts": "ISO"}
{"role": "assistant", "text": "...", "ts": "ISO"}
{"role": "reasoning", "text": "...", "ts": "ISO"}
{"role": "command", "command": "tool_name", "output": "...", "exit_code": 0, "ts": "ISO"}
```

### Two Resume Paths

1. **Lazy resume on first message** — when user sends a message to a conversation with `thread_id`:
   ```python
   # In handle_message():
   if meta.thread_id and no active session:
       await resume_session(conversation_id)  # reconnects to SDK session
       await session.send(text)
   ```

2. **Manual via session picker** — "Browse" button in settings:
   ```
   Settings Modal → [Browse] → Session Picker Overlay
       │
       ├── GET /api/extensions/{ext_id}/sessions?cwd=<cwd>
       │   └── Returns sessions sorted by CWD relevance
       │
       ├── User clicks a session, hits Save
       │
       └── conversation_update handler:
           ├── ext_loader.resume_session_with_history()
           ├── ext_loader.hydrate_transcript()  → List[Dict]
           ├── _write_transcript_entries(items)  → transcript.jsonl
           └── Frontend replayTranscript() renders history
   ```

### CWD-Based Session Sorting

`list_sessions()` sorts results by relevance when `cwd` is provided:

| Priority | Condition |
|----------|-----------|
| 0 (highest) | Session CWD exactly matches requested CWD |
| 1 | Session is in the same git repository |
| 2 | Session CWD is a parent/child of requested CWD |
| 9 (lowest) | No CWD match |

Within each group, sessions are sorted by `modifiedTime` descending (newest first).

### Session ↔ Conversation Binding

```
conversation_id (our system) ←──thread_id──→ session_id (SDK)
```

- `init_session()` stores SDK session_id as `thread_id` in conversation metadata
- `resume_session()` looks up `thread_id` to reconnect
- If session_id ≠ conversation_id, the in-memory maps are remapped on resume

## SDK Client Lifecycle

### Singleton Pattern

```python
_client: Optional[CopilotClient] = None  # One client per server
_sessions: Dict[str, CopilotSession] = {}  # conversation_id → session
_routers: Dict[str, CopilotEventRouter] = {}  # conversation_id → router

async def _ensure_client() -> CopilotClient:
    async with _get_client_lock():
        if _client is None:
            _client = CopilotClient(auto_start=True, auto_restart=True)
            await _client.start()
        return _client
```

### Why No framework_shells?

The Copilot SDK manages its own CLI subprocess internally:
- `auto_start=True` — starts `copilot --headless --stdio` on first use
- `auto_restart=True` — restarts if the process dies
- Internal `JsonRpcClient` handles stdin/stdout JSON-RPC framing

No shellspec YAML, no pipe management, no reader loop needed.

### Permission Handling

The SDK supports permission callbacks for tool approval:

```python
def _permission_handler(request) -> dict:
    # Auto-approve (configurable)
    return {"kind": "approved", "rules": []}

session_config = {
    "session_id": conversation_id,
    "model": "claude-sonnet-4-20250514",
    "streaming": True,
    "working_directory": cwd,
    "on_permission_request": _permission_handler,
}
```

## API Endpoints

### Extension Endpoints (Generic Routes)

All extension endpoints now use `{extension_id}` path parameters — no hardcoded SDK paths:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /api/extensions` | GET | List loaded extensions |
| `GET /api/extensions/{id}` | GET | Get extension details |
| `GET /api/extensions/{id}/settings_schema` | GET | Get settings UI schema |
| `GET /api/extensions/{id}/models` | GET | List available models |
| `GET /api/extensions/{id}/sessions` | GET | List sessions (with `?cwd=` sorting) |
| `POST /api/extensions/{id}/sessions/resume` | POST | Resume+hydrate a session |
| `GET /api/extensions/{id}/debug/raw?limit=50` | GET | Debug event buffer |

### Model List

```json
{
  "models": [
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
    {"id": "gpt-5.1-codex", "name": "GPT-5.1-Codex"},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"}
  ]
}
```

The model dropdown in settings uses `dynamic_source` to fetch this list at runtime:
```json
{
  "id": "model",
  "type": "select",
  "label": "Model",
  "dynamic_source": "/api/extensions/copilot-sdk/models"
}
```

## Settings Schema

Each extension defines a `settings_schema.json` that controls which fields appear in the Settings modal.

### Schema Format

```json
{
  "version": "1",
  "description": "Settings for GitHub Copilot SDK extension",
  "fields": [
    {
      "id": "cwd",
      "type": "path",
      "label": "Working Directory",
      "placeholder": "~/project",
      "required": true,
      "browse": true
    },
    {
      "id": "model",
      "type": "select",
      "label": "Model",
      "dynamic_source": "/api/extensions/copilot-sdk/models",
      "options": [{"value": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"}]
    },
    {
      "id": "reasoning_effort",
      "type": "select",
      "label": "Reasoning Effort",
      "options": [
        {"value": "low", "label": "Low"},
        {"value": "medium", "label": "Medium"},
        {"value": "high", "label": "High"}
      ],
      "default": "medium"
    }
  ]
}
```

### Field Types

| Type | Description | Extra Options |
|------|-------------|---------------|
| `text` | Simple text input | `placeholder` |
| `path` | Path input with browse button | `placeholder`, `browse: true` |
| `select` | Dropdown selector | `options`, `dynamic_source` |
| `session_picker` | Session selector with Browse overlay | `source` (URL), `resume_endpoint` |
| `checkbox` | Boolean toggle | — |
| `number` | Numeric input | `min`, `max`, `placeholder` |

The `session_picker` field is hidden when the conversation already has a `thread_id` (i.e., session already bound). The Browse button fetches sessions from the `source` URL and renders a picker overlay. All picker logic lives in `settings_schema.js` — NOT in `codex_agent.js`.

### `dynamic_source`

When a `select` field has `dynamic_source`, the frontend fetches the URL at render time and populates the dropdown. Supports response formats:
- `{"models": [...]}` — each entry has `id` and optional `name`
- `{"options": [...]}` — each entry has `value` and `label`

### Settings Modal Layout

```
┌─────────────────────────────────────┐
│ Conversation Settings           ✕  │
├─────────────────────────────────────┤
│ Agent:        [copilot-sdk ▾]       │  ← Always shown
│ CWD:          [~/project] [Browse]  │  ← Always shown
├─────────────────────────────────────┤
│ ┌─ settings-codex-fields ─────────┐ │
│ │ (hidden when agent ≠ codex)     │ │
│ └─────────────────────────────────┘ │
│ ┌─ Session: [(new session)] [Resume]│ │  ← copilot-sdk only
│ └─────────────────────────────────┘ │
│ ┌─ settings-extension-fields ─────┐ │
│ │ Model:  [claude-sonnet-4 ▾]     │ │  ← schema-driven
│ │ Effort: [medium ▾]              │ │
│ └─────────────────────────────────┘ │
├─────────────────────────────────────┤
│ Conversation Label: [...]           │
│ Command Output Lines: [20]          │
│ ☑ Render Markdown                   │
│ ☑ Use xterm.js                      │
├─────────────────────────────────────┤
│              [Cancel] [Save]        │
└─────────────────────────────────────┘
```

## Threading Model

Understanding the event dispatch chain is critical for debugging:

```
SDK internal reader thread (daemon)
    │ reads subprocess stdout
    ▼
JsonRpcClient._read_loop()
    │ parses JSON-RPC
    ▼
loop.call_soon_threadsafe(notification_handler, method, params)
    │ crosses thread boundary
    ▼
CopilotSession._dispatch_event(event)
    │ runs ON main event loop
    ▼
our session.on() callback
    │ creates asyncio.Task
    ▼
CopilotEventRouter.route_event(event)
    │ translates to internal format
    ▼
broadcast_fn(conversation_id, event_dict)
    │ sends to WebSocket
    ▼
Frontend renders
```

**Important:** `session.on()` is the ONE sync method in the SDK. The callback itself runs on the event loop (scheduled by `call_soon_threadsafe`), so `asyncio.get_running_loop()` works inside it.

## Frontend Transport: Socket.IO Migration

All frontend↔backend communication now uses Socket.IO (namespace `/appserver`). HTTP POST/GET calls have been replaced with `sioCall()`, a client-side helper that emits with ack callbacks.

### `sioCall(event, data, options)`

Promise wrapper for Socket.IO emit with ack:
- Timeout: 10s default
- HTTP fallback: `options.fallbackUrl` for graceful degradation
- Server error format: `{"__error": "message"}` via `_sio_error()` helper

### Events Migrated to SIO

| Category | Events |
|----------|--------|
| Real-time | `send_message`, `send_shell_command`, `send_rpc`, `approval_response`, `interrupt_turn` |
| Conversation CRUD | `conversation_create`, `conversation_get`, `conversation_update`, `conversation_delete`, `conversation_select`, `conversation_draft`, `conversation_bind_rollout` |
| Data | `get_transcript`, `get_transcript_range`, `get_status`, `get_models`, `get_rollouts`, `get_rollout_preview`, `get_extensions`, `approval_record`, `conversations_list` |
| App control | `app_start`, `app_stop` |
| Extension | `get_extension_models`, `get_sessions`, `session_resume` |

### What Remains on HTTP

- PTY/MCP subsystem (`postJson()`)
- Static assets
- Filesystem operations
- TE2 integration (`postTe2OpenRequest`)

### Raw WebSocket Removal

The `/ws/appserver` raw WebSocket endpoint has been removed. Only remaining WebSocket: `/ws/pty/{conversation_id}` for raw terminal data. The raw event buffer is retained for the debug endpoint (`/api/appserver/debug/raw`).

## Interrupt Support

SDK sessions can be interrupted via the existing interrupt endpoint:

```
POST /api/appserver/interrupt {conversation_id}
    │
    ├── agent == "codex" → existing Codex interrupt
    └── agent in extensions → ext_loader.interrupt_session(ext_id, convo_id)
                                  └── handler.abort_session(convo_id)
                                        └── session.abort()
```

Added `interrupt_session()` pass-through in `extensions/__init__.py`.

## Intent → Reasoning Ribbon

SDK `INTENT` events are mapped to `type: "thought"` (not `type: "activity"`), which triggers both `setActivity()` (left status) and `setReasoningRibbon()` (right ribbon) on the frontend. This matches codex behavior where `**thought headers**` in reasoning are parsed to the ribbon.

## Diff Path Resolution

The SDK router extracts file paths for diff events from tool call arguments when `data.path` is empty (which is common for SDK tool completions):

```python
file_path = data.path or ""
if not file_path:
    args = tool_call.get("arguments") or {}
    if isinstance(args, dict):
        file_path = args.get("path") or args.get("file_path") or ""
```

This ensures diff cards have proper `data-path` attributes for TE2 "jump to file and line" functionality. The frontend's `toProjectRelativePath()` converts absolute paths to project-relative paths for the TE2 editor integration.

## Agent-Aware Initialization

`ensureInitialized()` in the frontend is agent-aware:
- **Codex**: Full binary start + JSON-RPC handshake (`initialize`/`initialized`)
- **Non-codex extensions**: Just `await waitForWs()` (near-instant, since SDK manages its own process)

This eliminates the ~2s delay on first message for non-codex conversations.

## Settings Save Safety

### Empty Value Filtering

Schema values are filtered before sending to the server to prevent the server's merge loop from stripping existing settings:

```javascript
// Filter out empty/null values — don't let blanks strip existing settings
const schemaVals = Object.fromEntries(
  Object.entries(schemaRaw).filter(([_, v]) => v !== '' && v != null)
);
```

### Session Picker Exclusion

`getSchemaValues()` skips `session_picker` fields entirely — they're one-time bindings, not persistent settings that should be re-sent on every save.

### Schema Input Guard

`renderSchemaFields()` has an `if (input)` guard before storing in `currentSchemaValues` — prevents `undefined` entries when `session_picker` is hidden (has thread). `getSchemaValues()` also null-guards each entry.

## In-Memory State vs Disk State

The SDK client maintains in-memory state that is NOT persisted to disk:

| State | Storage | Survives Page Refresh | Survives Server Restart |
|-------|---------|----------------------|------------------------|
| `_sessions` dict | Python memory | ✅ (server still running) | ❌ |
| `_routers` dict | Python memory | ✅ | ❌ |
| `_pending_approvals` | Python memory | ✅ | ❌ |
| `meta.json` (thread_id, settings) | Disk | ✅ | ✅ |
| `transcript.jsonl` | Disk | ✅ | ✅ |

**Critical implication:** Manual edits to `meta.json` (e.g., correcting a thread_id) require a server restart to take effect if the conversation already has an active session in `_sessions`.

## Policy Settings

Three configurable policies in `settings_schema.json`:

| Setting | Options | Effect |
|---------|---------|--------|
| `approval_policy` | `auto-approve`, `suggest`, `always-ask` | Controls tool approval behavior |
| `sandbox_policy` | `cwd-only`, `allow-all-paths`, `ask` | File access scope |
| `web_policy` | `deny`, `allow`, `ask` | Network access |

Enforced via `on_permission_request` and `pre_tool_use` hooks in the SDK session config.

## Known Issues

1. **Ping warning** — `coroutine 'CopilotClient.ping' was never awaited` appears on startup. Cosmetic; doesn't affect functionality. Likely a uvloop/SDK interaction edge case.

2. **Session context** — SDK v0.1.22's `SessionMetadata` doesn't include `context` (cwd/gitRoot). CWD-based sorting falls back gracefully when context is unavailable.

3. **Lock initialization** — `asyncio.Lock()` must NOT be created at module import time (before uvloop takes over). Use lazy init via `_get_client_lock()`.

4. **Delta events in hydration** — `hydrate_transcript()` only processes completed events (`ASSISTANT_MESSAGE`, `USER_MESSAGE`, `TOOL_EXECUTION_COMPLETE`). Delta events are skipped since we only need final content for transcript entries.

5. **Large transcript payloads** — Conversations with 1MB+ command outputs can exceed the default 1MB WebSocket frame limit when proxied through TE2's `proxy_shell.py`. The upstream `websockets.connect()` call needs `max_size=16*1024*1024` (or similar) to handle these payloads.

## Migration from ACP

The Copilot SDK extension is a drop-in replacement:

| ACP (deprecated) | Copilot SDK |
|-------------------|-------------|
| `acp_client.py` | `copilot_sdk_client.py` |
| `acp_router.py` | `copilot_sdk_router.py` |
| `acp/gemini/manifest.json` | `copilot_sdk/manifest.json` |
| `shellspec/gemini_acp.yaml` | Not needed (SDK manages process) |
| `gemini --experimental-acp` | `copilot --headless --stdio` (managed by SDK) |
| No session resume | Full session resume via `resume_session()` |
| 60s cold start | Near-instant (SDK auto-starts) |
| One shared process, multiplexed sessions | One client, independent sessions |
| JSON-RPC over stdin/stdout (manual) | Python SDK with async API |
| framework_shells for process lifecycle | SDK manages its own subprocess |

ACP files (`acp_client.py`, `acp_router.py`, `acp/` directory) are retained but deprecated. They can be removed once the Copilot SDK extension is verified stable.

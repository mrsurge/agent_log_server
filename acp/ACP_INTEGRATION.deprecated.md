# ACP Integration

> Gemini CLI (ACP) as a pluggable agent extension

## Overview

The ACP integration allows Gemini CLI (and future ACP-compatible agents) to work with the existing agent_log_server infrastructure. The extension is a **protocol adapter** - it speaks ACP to Gemini, but speaks our internal event format to everything else.

**Key principle:** Frontend is a dumb renderer. All conversation logic, event routing, and state management happens in the backend.

## Architecture

```
Frontend (unchanged)
    │ WebSocket (/ws/appserver)
    ▼
┌─────────────────────────────────────────────┐
│           server.py                          │
│  _broadcast_appserver_ui()                   │
│  _append_transcript_entry()                  │
│                                              │
│  /api/appserver/message                      │
│       │                                      │
│       ├── agent == "codex" ──► Codex Router  │
│       │                                      │
│       └── agent in extensions ──► ext_loader │
│                   │                          │
│                   ▼                          │
│           extensions/acp_client.py           │
│                   │                          │
└───────────────────┼──────────────────────────┘
                    │
                    ▼
            extensions/acp_router.py
                    │ ACP JSON-RPC
                    ▼
            gemini --experimental-acp
```

## File Structure

| File | Purpose |
|------|---------|
| `extensions/__init__.py` | Generic extension loader - reads `extensions.json`, loads handlers by type |
| `extensions/acp_client.py` | ACP session management, shell lifecycle, `handle_message()` entry point |
| `extensions/acp_router.py` | ACP event → internal format translation |
| `extensions/extensions.json` | Extension registry |
| `extensions/acp/gemini/manifest.json` | Gemini extension metadata |
| `extensions/acp/gemini/settings_schema.json` | Settings UI schema for Gemini |
| `shellspec/gemini_acp.yaml` | Shell spec for Gemini process (framework_shells) |
| `static/modals/settings_schema.js` | Dynamic settings field renderer |

## Extension Loading

### Registry (`extensions/extensions.json`)

```json
{
  "version": "1.0",
  "extensions": [
    {
      "id": "gemini-acp",
      "name": "Gemini CLI (ACP)",
      "type": "acp",
      "path": "acp/gemini",
      "enabled": true
    }
  ]
}
```

### Loading Flow

1. On server startup, `_init_extensions()` calls `ext_loader.load_extensions()`
2. Loader reads `extensions.json` and loads handler modules by type
3. Type `"acp"` → loads `extensions/acp_client.py`
4. Handler is initialized with callbacks to server infrastructure:
   - `fws_getter` - get framework_shells manager
   - `broadcast_fn` - `_broadcast_appserver_ui()`
   - `transcript_fn` - `_append_transcript_entry()`
   - `meta_fns` - `{load: _load_conversation_meta, save: _save_conversation_meta}`

### Adding New Agent Types

To add a new agent (e.g., Claude CLI):
1. Add entry to `extensions/extensions.json`
2. Create manifest at `extensions/acp/claude/manifest.json`
3. Create shell spec at `shellspec/claude_acp.yaml`
4. (If new type) Add handler loader in `extensions/__init__.py`

**server.py never mentions specific agents** - fully pluggable.

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
    └── other ──► ext_loader.get_handler(agent_type).handle_message(...)
```

### 2. ACP Session Lifecycle

```python
# Server startup - warm up the extension:
warm_up_extension("gemini-acp")
    │
    ├── Start shell via framework_shells (ONE process)
    │   └── gemini --experimental-acp
    │
    └── Send "initialize" request, wait for response
        └── Mark extension as ready

# Settings save - create session eagerly:
init_session(conversation_id, "gemini-acp", cwd)
    │
    ├── Get shared shell (or promote warmup shell)
    │
    ├── Create ACPSession object for this conversation
    │
    ├── Create ACPEventRouter (stores broadcast/transcript callbacks)
    │
    ├── Start reader task (_acp_reader_loop)
    │
    └── Send "session/new" request with {cwd: "..."}
        └── Capture sessionId → store as thread_id in meta.json

# First message (session already exists):
handle_message(conversation_id, text, agent_type, settings)
    │
    └── Send "session/prompt" with user text
```

### 3. Event Routing (Reader Task)

```python
# _acp_reader_loop continuously reads Gemini stdout:
while True:
    line = await stdout.readline()
    message = parse_acp_line(line)  # JSON-RPC
    await router.route_event(message)
```

## Event Translation Table

| ACP Event | Internal Event Type | Frontend Handler |
|-----------|---------------------|------------------|
| `agent_message_chunk` | `assistant_delta` | `appendAssistantDelta()` |
| `agent_thought_chunk` | `reasoning_delta` | `appendReasoningDelta()` |
| `tool_call` (start) | `shell_begin` | `renderShellBegin()` |
| `tool_call_update` (in_progress) | `shell_delta` | `renderShellDelta()` |
| `tool_call_update` (completed) | `shell_end` | `renderShellEnd()` |
| `plan` | `plan` | `renderPlanCard()` |
| `session/prompt` response | `assistant_finalize` + `turn_completed` | `finalizeAssistant()` |
| (on prompt send) | `message` (role=user) + `turn_started` + `activity` | `addMessage()` |

## Event IDs and Ordering

All events include fields for proper frontend rendering and ordering:

### ID Fields

Each event type has a unique `id` field for DOM element accumulation:

| Event Type | ID Pattern | Purpose |
|------------|------------|---------|
| `message` (user) | `user_{turn}` | User message block |
| `assistant_delta` | `msg_{turn}` | Assistant message accumulation |
| `assistant_finalize` | `msg_{turn}` | Matches delta for finalization |
| `reasoning_delta` | `reasoning_{turn}` | Reasoning block accumulation |
| `shell_begin/delta/end` | `{tool_call_id}` | Tool call block (from ACP) |
| `plan` | `plan_turn_{turn}` | Plan card |
| `approval_request` | has `request_id` + `tool_call_id` | Approval UI |

**Why distinct IDs matter:** The frontend uses `id` to find or create DOM elements. If `assistant_delta` and `reasoning_delta` shared the same ID, deltas would mix into the wrong element.

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

### Block-Based IDs for Interleaved Events

Gemini interleaves reasoning and message events within a turn. The frontend creates one DOM row per unique `id`. To preserve order, we use block-based IDs that increment each time the event type switches:

| Event sequence | ID | Result |
|----------------|-----|--------|
| reasoning chunk | `reasoning_1_1` | New row |
| reasoning chunk | `reasoning_1_1` | Appends to same row |
| message chunk | `msg_1_2` | New row (below reasoning) |
| reasoning chunk | `reasoning_1_3` | New row (below message) |
| message chunk | `msg_1_4` | New row (below reasoning) |

Format: `{type}_{turn}_{block}` where block increments on each type switch.

### Transcript Entries

Transcript entries mirror the ID structure for replay:

```jsonl
{"role": "user", "id": "user_1", "text": "Hello", "turn_id": "turn_1", "seq": 1, "timestamp": "..."}
{"role": "reasoning", "id": "reasoning_1_1", "text": "Thinking...", "turn_id": "turn_1", "seq": 5, "timestamp": "..."}
{"role": "assistant", "id": "msg_1_2", "text": "Hi!", "turn_id": "turn_1", "seq": 10, "timestamp": "..."}
{"role": "command", "id": "tc_abc123", "command": "ls", "output": "...", "turn_id": "turn_1", "seq": 15, "timestamp": "..."}
{"role": "status", "status": "success", "turn_id": "turn_1", "seq": 20, "timestamp": "..."}
```

## ACP Protocol Details

### Initialization

```json
// Client → Agent
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
  "protocolVersion": 1,
  "clientCapabilities": {"fs": {"readTextFile": true, "writeTextFile": true}, "terminal": true},
  "clientInfo": {"name": "agent-log-server", "version": "1.0.0"}
}}

// Agent → Client
{"jsonrpc": "2.0", "id": 1, "result": {
  "protocolVersion": 1,
  "agentCapabilities": {"loadSession": false, ...},
  "agentInfo": {"name": "gemini-cli", ...}
}}
```

### Session Creation

```json
// Client → Agent
{"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {
  "cwd": "/absolute/path/to/project",
  "mcpServers": []
}}

// Agent → Client
{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "uuid-here"}}
```

### Prompt

```json
// Client → Agent
{"jsonrpc": "2.0", "id": 12345, "method": "session/prompt", "params": {
  "sessionId": "uuid-here",
  "prompt": [{"type": "text", "text": "User message here"}]
}}

// Agent → Client (streaming notifications)
{"jsonrpc": "2.0", "method": "session/update", "params": {
  "sessionId": "uuid-here",
  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}}
}}

// Agent → Client (completion)
{"jsonrpc": "2.0", "id": 12345, "result": {"stopReason": "end_turn"}}
```

## Debug Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/extensions` | List loaded extensions |
| `GET /api/extensions/{id}` | Get extension details |
| `GET /api/extensions/{id}/settings_schema` | Get settings UI schema |
| `GET /api/extensions/debug/raw?limit=50` | Raw ACP message buffer (in/out) |

Debug buffer shows last 200 messages with direction, conversation ID (first 8 chars), and raw JSON-RPC data.

## Settings Schema

Each extension defines a `settings_schema.json` that controls which fields appear in the Settings modal. This allows extension-specific settings without hardcoding in the frontend.

### Schema Format

```json
{
  "version": "1",
  "description": "Settings schema for Gemini CLI extension",
  "fields": [
    {
      "id": "cwd",
      "type": "path",
      "label": "Working Directory",
      "placeholder": "~/project",
      "default": ".",
      "required": true,
      "browse": true
    }
  ]
}
```

### Field Types

| Type | Description | Extra Options |
|------|-------------|---------------|
| `text` | Simple text input | `placeholder` |
| `path` | Path input with browse button | `placeholder`, `browse: true` |
| `select` | Dropdown selector | `options: [{value, label}]` |
| `checkbox` | Boolean toggle | — |
| `number` | Numeric input | `min`, `max`, `placeholder` |

### Modal Layout

```
┌─────────────────────────────────────┐
│ Conversation Settings           ✕  │
├─────────────────────────────────────┤
│ Agent:        [codex ▾]             │  ← Always shown
│ CWD:          [~/project] [Browse]  │  ← Always shown
├─────────────────────────────────────┤
│ ┌─ settings-codex-fields ─────────┐ │
│ │ Approval Policy: [on-failure]   │ │  ← Codex only
│ │ Sandbox Policy:  [workspaceWrite│ │
│ │ Model:           [gpt-5.1-codex]│ │
│ │ Effort:          [medium]       │ │
│ │ Summary:         [concise]      │ │
│ │ Rollout:         [Pick]         │ │
│ └─────────────────────────────────┘ │
│ ┌─ settings-extension-fields ─────┐ │
│ │ (schema-driven fields here)     │ │  ← Non-codex
│ └─────────────────────────────────┘ │
├─────────────────────────────────────┤
│ Conversation Label: [...]           │  ← Always shown
│ Command Output Lines: [20]          │
│ ☑ Render Markdown                   │
│ ☑ Use xterm.js                      │
│ ☐ Syntax highlighting               │
│ ☐ Semantic shell ribbon             │
├─────────────────────────────────────┤
│              [Cancel] [Save]        │
└─────────────────────────────────────┘
```

### Implementation

1. When agent dropdown changes, `onAgentChange(agentId)` is called
2. If `agentId !== 'codex'`, fetch `/api/extensions/{agentId}/settings_schema`
3. Hide `#settings-codex-fields`, render schema fields into `#settings-extension-fields`
4. On save, `getSchemaValues()` merges extension field values into settings

## Current Limitations

### Session Resume (Not Supported)

Gemini CLI reports `loadSession: false` - it does not support resuming sessions. When:
- Server restarts
- Shell dies
- User returns after session expires

...context is lost. We create a fresh `session/new`.

**Future option:** Re-inject conversation history as context in first prompt.

### Streaming Granularity

Gemini sends `agent_message_chunk` in large blocks, not token-by-token. Streaming infrastructure works, but visual effect is less smooth than Codex.

## Known Issues (TODO)

1. ~~**Session startup on conversation switch** - Buggy, needs investigation~~ **FIXED** - Eager warm-up + shared shell
2. ~~**Multiple shells spawning** - Each conversation was starting a new Gemini process~~ **FIXED** - Shared shell architecture
3. **Conversation hydration** - When switching to existing Gemini conversation, need to handle session state
4. ~~**Tool call approval flow** - Not yet implemented (auto-approves)~~ **FIXED** - Correct ACP response format: `{"result":{"outcome":{"outcome":"selected","optionId":"proceed_once"}}}`
5. ~~**File operations** - `fs/read_text_file`, `fs/write_text_file` stubs exist but not wired~~ **FIXED** - Handlers in router with shell_begin/end events
6. **Terminal operations** - `terminal/*` methods not implemented
7. **Error handling** - Need better recovery on shell crash
8. **Cleanup** - Shell cleanup on conversation delete
9. ~~**CWD not passed correctly** - Session was created with wrong CWD~~ **FIXED** - Eager init on settings save
10. **Interleaved event ordering** - Gemini interleaves reasoning/message/tool events within a turn. Current fix uses block-based IDs (`reasoning_1_1`, `msg_1_2`, `reasoning_1_3`) so each block gets its own row. Still seeing bunching issues - needs further investigation.

## Client Method Handlers

The ACP router handles incoming requests from the agent:

### `fs/read_text_file`

Agent requests file content:
```json
{"jsonrpc":"2.0","id":0,"method":"fs/read_text_file","params":{"path":"/path/to/file.cpp","sessionId":"..."}}
```

We respond with:
```json
{"jsonrpc":"2.0","id":0,"result":{"content":"file content here..."}}
```

### `fs/write_text_file`

Agent requests to write file:
```json
{"jsonrpc":"2.0","id":1,"method":"fs/write_text_file","params":{"path":"/path/to/file.cpp","content":"new content","sessionId":"..."}}
```

We respond with:
```json
{"jsonrpc":"2.0","id":1,"result":{}}
```

### `session/request_permission`

Agent requests permission for a tool call (e.g., file edit):
```json
{"jsonrpc":"2.0","id":1,"method":"session/request_permission","params":{
  "sessionId":"...",
  "options":[
    {"optionId":"proceed_always","name":"Allow All Edits","kind":"allow_always"},
    {"optionId":"proceed_once","name":"Allow","kind":"allow_once"},
    {"optionId":"cancel","name":"Reject","kind":"reject_once"}
  ],
  "toolCall":{"toolCallId":"...","title":"main.cpp: ...","kind":"edit","content":[...]}
}}
```

We respond with the user's decision per ACP spec (currently auto-approves):
```json
{"jsonrpc":"2.0","id":1,"result":{"outcome":{"outcome":"selected","optionId":"proceed_once"}}}
```

**Note:** The result must be `{"outcome": {"outcome": "selected", "optionId": "..."}}`. For cancellation, use `{"outcome": {"outcome": "cancelled"}}`.

### Unknown Methods

For unhandled methods, we return a JSON-RPC error:
```json
{"jsonrpc":"2.0","id":99,"error":{"code":-32601,"message":"Method not found: some/method"}}
```

## Eager Warm-up & Shared Shell Architecture

Gemini CLI is a Node.js application that takes up to 60 seconds to load. To avoid blocking the first message:

1. **Warm-up on server start**: Start the Gemini process and send `initialize`
2. **Shared shell**: ONE process per extension, multiplexed across all conversations
3. **Eager session init**: Send `session/new` when settings are saved (before first message)

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Gemini Process                         │
│                   (ONE per extension)                    │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  Session A   │  │  Session B   │  │  Session C   │   │
│  │  (convo-1)   │  │  (convo-2)   │  │  (convo-3)   │   │
│  │  cwd: ~/p1   │  │  cwd: ~/p2   │  │  cwd: ~/p3   │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Warm-up Flow (Server Start)

```
Server Startup (_lifespan)
    │
    ├── ext_loader.warm_up_extensions(timeout=60)
    │       │
    │       └── acp_client.warm_up_extension("gemini-acp")
    │               │
    │               ├── Start shell via framework_shells
    │               │
    │               ├── Send "initialize" request
    │               │
    │               ├── Wait for response (up to 60s)
    │               │
    │               └── Mark as ready (NO session/new yet)
    │
    └── Print "Extension gemini-acp: ready"
```

### Eager Session Init (Settings Save)

Extensions with `eagerSessionInit: true` in their manifest trigger session creation when settings are saved:

```json
// extensions/acp/gemini/manifest.json
{
  "agent": {
    "command": "gemini",
    "args": ["--experimental-acp"],
    "eagerSessionInit": true
  }
}
```

```
Settings Modal [Save]
    │
    ├── POST /api/appserver/conversation {settings: {agent: "gemini-acp", cwd: "~/project"}}
    │
    ├── Check: ext_loader.requires_eager_session_init("gemini-acp") → true
    │
    └── Fire background task: ext_loader.init_session(convo_id, "gemini-acp", cwd)
            │
            ├── Get shared shell (promote warmup shell if needed)
            │
            ├── Create new ACPSession object for this conversation
            │
            ├── Start reader task for this conversation
            │
            ├── Send "session/new" with correct CWD
            │
            └── Wait for sessionId → store in meta.json, set status="active"
```

### First Message Flow

By the time the user sends their first message, the session is already ready:

```python
# In handle_message():
if not _manager.has_session(conversation_id):
    # Use init_session (shared shell, creates new ACP session)
    await init_session(conversation_id, agent_type, cwd)

# Session is ready - send prompt immediately
await _send_prompt(conversation_id, text, fws_mgr)
```

### Shared Shell Tracking

```python
# Global state in acp_client.py:
_shared_shells: Dict[str, str] = {}  # extension_id -> shell_id

# When first conversation needs a session:
shell_id = _shared_shells.get(extension_id)
if not shell_id:
    # Promote warmup shell to shared
    warmup_session = _manager.get_session("__warmup__gemini-acp")
    shell_id = warmup_session.shell_id
    _shared_shells[extension_id] = shell_id

# Create new session object using shared shell
session = ACPSession(
    conversation_id=conversation_id,
    shell_id=shell_id,  # Same shell for all conversations
    ...
)
```

### Multiple Conversations

Each conversation gets its own:
- `ACPSession` object (Python-side tracking)
- `session_id` (ACP-side, returned by `session/new`)
- Reader task (routes events to correct conversation)
- Router instance (broadcasts to correct WebSocket)

All share the same:
- Shell (Gemini process)
- `shell_id` (framework_shells identifier)

## Framework Shells Integration

ACP uses [framework_shells](https://github.com/mrsurge/framework-shells) for process management.

### Why framework_shells?

- **Multiple backends**: PTY (interactive terminals), Pipes (stdin/stdout streams), Dtach (persistent)
- **Runtime isolation**: Shells namespaced by repo fingerprint + secret
- **Unified API**: Same `spawn`/`terminate`/`list` patterns for all backends

### Backend Selection

| Use Case | Backend | Why |
|----------|---------|-----|
| ACP agents (Gemini, etc.) | `pipe` | Need raw stdin/stdout for JSON-RPC |
| Agent's execution terminal | `dtach` | Persistent, survives restarts, attachable |
| Interactive one-off shells | `pty` | Full terminal emulation |

### ACP Process Lifecycle

```python
from framework_shells import get_manager
from framework_shells.orchestrator import Orchestrator

mgr = await get_manager()

# Spawn Gemini with pipe backend
record = await Orchestrator(mgr).start_from_ref(
    "shellspec/gemini_acp.yaml#gemini_acp",
    base_dir=project_root,
    ctx={"CWD": cwd, "CONVERSATION_ID": conversation_id},
    label=f"acp:{conversation_id}",
)

# Get pipe handles for JSON-RPC
pipe_state = mgr.get_pipe_state(record.id)
stdin = pipe_state.stdin   # asyncio.StreamWriter
stdout = pipe_state.stdout # asyncio.StreamReader

# Write JSON-RPC request
stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize",...}\n')
await stdin.drain()

# Read JSON-RPC response
line = await stdout.readline()
response = json.loads(line)
```

### Shell Spec (`shellspec/gemini_acp.yaml`)

```yaml
version: "1"
shells:
  gemini_acp:
    backend: pipe
    cwd: ${CWD}
    subgroups: ["acp", "gemini"]
    command:
      - gemini
      - --experimental-acp
    labels:
      app: "gemini-acp"
      conversation_id: ${CONVERSATION_ID}
```

- `backend: pipe` - Returns `PipeState` with stdin/stdout handles (not PTY)
- `subgroups` - For dashboard grouping/filtering
- `labels` - Metadata for finding shells by conversation

### Two Separate Shell Systems

Note: There are **two** shell systems in play:

1. **ACP shell** (this integration) - The agent process itself (e.g., `gemini --experimental-acp`)
   - Managed by `acp_client.py`
   - Uses `pipe` backend for JSON-RPC
   - **ONE per extension** (shared across all conversations)
   - Multiple ACP sessions within the same process

2. **Agent PTY shell** (`shell_manager.py`) - The agent's execution environment
   - Managed by separate shell_manager service (port 12361)
   - Uses `dtach` backend for persistence
   - Where the agent runs commands (ls, git, etc.)
   - One per conversation

The ACP agent may request terminal operations via `terminal/*` methods - these would execute in the Agent PTY shell, not the ACP pipe.

# Codex Agent Server - Deep Dive Technical Documentation

## Overview

The Codex Agent Server is a FastAPI + `fastcore.xml` Python server that acts as a **bridge and UI layer** between the OpenAI Codex CLI (`codex-app-server` binary) and a web-based frontend. It provides a rich conversational interface for interacting with AI coding agents.

### Core Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Web Browser (Frontend)                      │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                    codex_agent.js                               ││
│  │  - Dumb renderer (displays what backend tells it)               ││
│  │  - Socket.IO client for live updates and runtime actions        ││
│  │  - HTTP endpoints for conversation data, debug, and admin flows ││
│  └─────────────────────────────────────────────────────────────────┘│
└──────────────────────────────┬──────────────────────────────────────┘
                               │ Socket.IO live runtime + HTTP data/debug endpoints
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Python Server (server.py)                        │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │  FastAPI + SocketIO                                             ││
│  │  - Translates codex events → frontend-friendly format           ││
│  │  - Manages conversation state (SSOT sidecar)                    ││
│  │  - Stores internal transcript (richer than rollout)             ││
│  │  - Handles approvals, settings, conversation switching          ││
│  └─────────────────────────────────────────────────────────────────┘│
└──────────────────────────────┬──────────────────────────────────────┘
                               │ stdin/stdout (JSON-RPC)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    codex-app-server (Rust binary)                   │
│  - Manages conversations with OpenAI API                            │
│  - Executes tools (shell commands, file edits)                      │
│  - Emits events via stdout (JSON-RPC notifications)                 │
│  - Receives commands via stdin (JSON-RPC requests)                  │
│  - Writes rollout logs to ~/.codex/sessions/                        │
│  - Handles multiplexing (multiple conversations)                    │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Concepts

### 1. SSOT Conversation Configuration Sidecar

The **Single Source of Truth (SSOT)** sidecar is a JSON file that stores the current conversation's configuration. Located at `conversations/<id>/meta.json`.

```json
{
  "conversation_id": "uuid",
  "thread_id": "codex-thread-id",
  "created_at": "ISO timestamp",
  "pending_approvals": {},
  "settings": {
    "agent": "codex",
    "cwd": "/working/directory",
    "approvalPolicy": "on-request",
    "sandboxPolicy": "workspace-write",
    "model": "gpt-5-codex",
    "effort": "medium",
    "label": "user-friendly name"
  },
  "status": "draft|active"
}
```

### 2. Draft vs Active Conversations

- **Draft**: A conversation that hasn't received a `thread_id` from codex yet. Fresh conversations start as drafts.
- **Active**: Has a `thread_id` (either from first turn response, or loaded from a rollout).

When loading a rollout, the `thread_id` is extracted and set immediately, so rollout-loaded conversations are never drafts.

### 3. Internal Transcript vs Rollout

**Rollout** (`~/.codex/sessions/.../rollout-*.jsonl`):
- Raw log from codex-app-server
- Very noisy (multiple events per action)
- Used for session recovery

**Internal Transcript** (`conversations/<id>/transcript.jsonl`):
- Curated by our server
- Cleaner, richer format
- Stores: user messages, agent messages, reasoning, diffs, commands, approvals

## Server Startup Flow

```
1. Python server starts (uvicorn)
   │
2. @app.on_event("startup")
   │  - Calls start_appserver_process()
   │
3. start_appserver_process()
   │  - Spawns: codex-app-server --json-rpc
   │  - Stores process handle in global APP_SERVER_PROC
   │  - Starts stdout reader task (_appserver_reader)
   │
4. _appserver_reader() [async background task]
   │  - Continuously reads lines from stdout
   │  - Parses JSON-RPC messages
   │  - Routes to appropriate handlers
   │  - Emits SocketIO events to frontend
   │
5. Server ready on port 12359
```

## Message Flows

### A. User Sends a Message

```
Frontend                    Python Server                 codex-app-server
   │                              │                              │
   │ SIO/HTTP: send_message       │                              │
   │ {conversation_id, text}      │                              │
   │─────────────────────────────>│                              │
   │                              │                              │
   │                              │ Load meta.json               │
   │                              │ Resolve thread/start/resume  │
   │                              │ Send turn/start              │
   │                              │─────────────────────────────>│
   │                              │                              │
   │                              │ stdout: turn/started         │
   │                              │<─────────────────────────────│
   │                              │                              │
   │ WS: turn/started             │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │                              │ stdout: item/started         │
   │                              │ (reasoning, message, etc)    │
   │                              │<─────────────────────────────│
   │                              │                              │
   │ WS: codex_event (deltas)     │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │                              │ stdout: turn/completed       │
   │                              │<─────────────────────────────│
   │                              │                              │
   │ WS: turn/completed           │                              │
   │<─────────────────────────────│                              │
```

The frontend only knows `conversation_id` on the normal send path. Thread lifecycle is negotiated by the Python runtime.

### B. File Change Approval Flow

```
codex-app-server              Python Server                    Frontend
      │                              │                              │
      │ item/fileChange/             │                              │
      │ requestApproval              │                              │
      │ {id: 0, itemId: "call_xxx",  │                              │
      │  changes: [...]}             │                              │
      │─────────────────────────────>│                              │
      │                              │                              │
      │                              │ Persist pending approval     │
      │                              │ into meta.json               │
      │                              │                              │
      │                              │ WS: appserver_event          │
      │                              │ {type: "approval", ...}      │
      │                              │─────────────────────────────>│
      │                              │                              │
      │                              │      [User clicks Accept]    │
      │                              │                              │
      │                              │ SIO/HTTP: approval_response  │
      │                              │ {request_id: 0,              │
      │                              │  decision: "accept"}         │
      │                              │<─────────────────────────────│
      │                              │                              │
      │ stdin: JSON-RPC response     │                              │
      │ {id: 0, result:              │                              │
      │  {decision: "accept"}}       │                              │
      │<─────────────────────────────│                              │
      │                              │                              │
      │                              │ Remove pending approval      │
      │                              │ from meta.json               │
      │                              │                              │
      │ [applies patch]              │                              │
      │                              │                              │
      │ item/completed               │                              │
      │ {type: "fileChange",         │                              │
      │  status: "completed"}        │                              │
      │─────────────────────────────>│                              │
      │                              │                              │
      │                              │ POST /approval_record        │
      │                              │ (records to transcript)      │
      │                              │<─────────────────────────────│
      │                              │                              │
      │                              │ WS: item/completed           │
      │                              │─────────────────────────────>│
```

The exact live approval card payload is also persisted in `meta.json.pending_approvals[request_id].render_event` so replay uses the same data the live UI saw.

### C. Conversation Switching

```
Frontend                    Python Server                 codex-app-server
   │                              │                              │
   │ POST /conversations/select   │                              │
   │ {id: "new-convo-id"}         │                              │
   │─────────────────────────────>│                              │
   │                              │                              │
   │                              │ Load meta.json               │
   │                              │ from conversations/<id>/     │
   │                              │                              │
   │                              │ If has rollout_path:         │
   │                              │   Parse rollout, extract     │
   │                              │   thread_id                  │
   │                              │ Validate pending approvals   │
   │                              │ for replay                   │
   │                              │                              │
   │ 200 OK                       │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │ GET /conversation            │                              │
   │─────────────────────────────>│                              │
   │                              │                              │
   │ {meta + rollout_entries}     │                              │
   │<─────────────────────────────│                              │
   │                              │                              │
   │ GET /transcript/range        │                              │
   │─────────────────────────────>│                              │
   │                              │                              │
   │ [transcript entries]         │                              │
   │<─────────────────────────────│                              │
```

### D. Creating a New Conversation

```
Frontend                    Python Server
   │                              │
   │ POST /conversations          │
   │ {label: "My Convo",          │
   │  model: "gpt-5.2-codex",     │
   │  ...}                        │
   │─────────────────────────────>│
   │                              │
   │                              │ Generate UUID
   │                              │ Create conversations/<id>/
   │                              │ Write meta.json
   │                              │ Create empty transcript.jsonl
   │                              │
   │ {id: "new-uuid"}             │
   │<─────────────────────────────│
   │                              │
   │ POST /conversations/select   │
   │ {id: "new-uuid"}             │
   │─────────────────────────────>│
   │                              │
   │                              │ [switches active conversation]
```

## Event Types from codex-app-server

### Notifications (method field)

| Method | Description | Key Data |
|--------|-------------|----------|
| `turn/started` | Turn begins | `threadId`, `turnId` |
| `turn/completed` | Turn ends | `status`, `error` |
| `item/started` | Item begins | `type` (userMessage, reasoning, agentMessage, commandExecution, fileChange) |
| `item/completed` | Item ends | Full item data |
| `item/agentMessage/delta` | Streaming text | `delta` (text chunk) |
| `item/reasoning/summaryTextDelta` | Reasoning stream | `delta` |
| `item/fileChange/requestApproval` | Needs approval | `itemId`, `reason`, `grantRoot` (diff comes from prior `fileChange` item state) |
| `thread/started` | New thread created | `thread` object with `id` |
| `thread/tokenUsage/updated` | Token counts | Usage stats |
| `account/rateLimits/updated` | Rate limit info | Percentages, reset times |

### Codex-specific Events (codex/event/*)

| Event Type | Description |
|------------|-------------|
| `task_started` | Agent begins processing |
| `task_complete` | Agent finished |
| `agent_message_delta` | Text streaming |
| `agent_reasoning_delta` | Thinking streaming |
| `exec_command_begin` | Shell command starting |
| `exec_command_end` | Shell command finished |
| `apply_patch_approval_request` | Legacy approval-context mirror (non-actionable without the matching JSON-RPC request id) |

## Frontend Components

### codex_agent.js

Main JavaScript file handling all frontend logic:

**Key Functions:**
- `initSocketIO()` - Establishes WebSocket connection
- `sendUserMessage(text)` - Sends a generic conversation-scoped message
- `respondApproval(requestId, decision)` - Handles approval buttons
- `renderDiff(diffText)` - Parses and renders unified diffs
- `renderTranscriptEntries(entries)` - Renders transcript items
- `replayTranscript()` - Loads and displays conversation history
- `fetchConversation()` - Gets current conversation state
- `createConversation(meta)` - Creates new conversation
- `saveSettings(settings)` - Saves conversation settings

**State Management:**
- `transcriptEl` - DOM element for transcript display
- `activityRibbon` - Shows current activity
- `statusDot` - Server connection status
- `pinned` - Whether auto-scroll is enabled

### UI Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ Header: Model selector, Context %, Markdown toggle, Settings    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Transcript Area (scrollable)                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ [User Message Card]                                       │  │
│  │ [Reasoning Card] - collapsible                            │  │
│  │ [Agent Message Card] - markdown rendered                  │  │
│  │ [Plan Card] - with step checkboxes                        │  │
│  │ [Command Card] - with output, duration                    │  │
│  │ [Shell Card] - user !command output                       │  │
│  │ [Diff Card] - syntax highlighted                          │  │
│  │ [Approval Card] - Accept/Decline buttons                  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│ [Status Ribbon] spinner + activity text + status dot ●          │
├─────────────────────────────────────────────────────────────────┤
│ Input Area: contenteditable + @mention + Send/Interrupt         │
│ - @ triggers Tribute.js file picker                             │
│ - ! prefix sends direct shell command                           │
│ - Mobile: Enter=newline, button=send                            │
│ - Desktop: Enter=send, Shift+Enter=newline                      │
└─────────────────────────────────────────────────────────────────┘

[Drawer] - Slide-out panel showing full transcript
```

## Data Storage

### Directory Structure

```
agent_log_server/
├── agent_log_server/
│   ├── server.py             # Main server
│   ├── static/
│   │   ├── codex_agent.js    # Frontend logic
│   │   └── codex_agent.css   # Styles
│   ├── templates/
│   │   └── template.html     # Legacy agent log page
│   └── shellspec/
├── conversations/           # Conversation data
│   └── <uuid>/
│       ├── meta.json
│       └── transcript.jsonl
└── my-schema/              # JSON schemas for validation
```

### Transcript Entry Format

```jsonl
{"role": "user", "content": "message text", "timestamp": "ISO"}
{"role": "assistant", "content": "response text", "timestamp": "ISO"}
{"role": "reasoning", "content": "thinking...", "timestamp": "ISO"}
{"role": "diff", "diff": "unified diff text", "path": "file.py", "timestamp": "ISO"}
{"role": "command", "command": "ls -la", "output": "...", "duration_ms": 52, "exit_code": 0, "timestamp": "ISO"}
{"role": "approval", "status": "accepted|declined", "diff": "...", "path": "...", "timestamp": "ISO"}
{"role": "plan", "steps": [{"step": "Step description", "status": "completed|in_progress|pending"}], "turn_id": "...", "timestamp": "ISO"}
{"role": "error", "text": "error message", "event": "codex/event/error", "timestamp": "ISO"}
```

## Configuration

### Approval Modes

| Mode | Behavior |
|------|----------|
| `never` | Auto-approve everything (YOLO mode) |
| `on-request` | Ask when the runtime explicitly requires approval |
| `untrusted` | Auto-approve trusted actions, ask for untrusted ones |
| `on-failure` | Older compatibility value; do not advertise it in new UI flows |

Approval and sandbox option lists should come from the backend runtime contract, not from frontend hardcoded arrays.

### Reasoning Effort

Controls how much "thinking" the model does:
- `low` - Fast, less thorough
- `medium` - Balanced
- `high` - Slower, more thorough

## Error Handling

### Common Issues

1. **Socket disconnects immediately**
   - Check ping/pong intervals
   - Ensure no conflicting transports

2. **Approval hangs**
   - Verify JSON-RPC response format: `{"jsonrpc": "2.0", "id": <same-id>, "result": {"decision": "accept"}}`
   - Check stdin write is flushed

3. **Rollout corrupted**
   - Failed approvals can leave rollout in bad state
   - codex-app-server may panic on reload
   - Solution: Start fresh conversation

4. **Draft conversation won't send**
   - Check server status isn't showing wrong value
   - Ensure WebSocket is connected

## API Endpoints

### Conversation Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/conversations` | GET | List all conversations |
| `/api/appserver/conversations` | POST | Create new conversation |
| `/api/appserver/conversations/select` | POST | Switch active conversation |
| `/api/appserver/conversation` | GET | Get current conversation |
| `/api/appserver/conversation` | POST | Update conversation meta |

### Transcript

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/transcript/range` | GET | Get transcript entries (paginated) |
| `/api/appserver/transcript/append` | POST | Add entry to transcript |
| `/api/appserver/approval_record` | POST | Record approval decision |

### RPC & Server

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/rpc` | POST | Send JSON-RPC to codex |
| `/api/appserver/status` | GET | Server status |
| `/api/appserver/start` | POST | Start codex-app-server |
| `/api/appserver/stop` | POST | Stop codex-app-server |

### Debug

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/debug/raw` | GET | Get legacy appserver raw event buffer (last N items) |
| `/api/appserver/debug/state` | GET | Get legacy appserver debug state |
| `/api/appserver/debug/toggle` | POST | Toggle legacy appserver debug mode (`{"enabled": true}`) |
| `/api/extensions/{extension_id}/debug/raw` | GET | Get extension-owned debug buffer |

Legacy appserver debug mode can mirror raw appserver events to `~/.cache/agent_log_server/debug_raw.jsonl`. Extension-owned transports expose their own debug buffers through `GET /api/extensions/{extension_id}/debug/raw`, and extensions may also persist internal `debug_raw` transcript rows when a debug workflow needs durable per-conversation forensic evidence.

## WebSocket Events

### Server → Client

| Event | Data | Description |
|-------|------|-------------|
| `appserver_event` | `{type, ...}` | Generic live event stream |
| `server_status` | `{status}` | Server state change |
| `appserver_event` approval payload | `{type: "approval", request_id, payload}` | Needs user approval |
| `plan` | `{steps: [{step, status}]}` | Completed plan from turn |
| `error` | `{message}` | Error from codex-app-server |
| `warning` | `{message}` | Warning from codex-app-server |

### Client → Server

Live/runtime UI actions use Socket.IO. HTTP endpoints remain for conversation data loading, admin, and debug surfaces; they are not the runtime fallback contract for shared UI/backend behavior.

## Diff Rendering

Diffs are parsed and rendered with syntax highlighting:

```
┌─────────────────────────────────────────────────────────────┐
│ main.cpp                                         [filepath] │
├─────────────────────────────────────────────────────────────┤
│ Lines 6-8 → 6-8                            [hunk header]    │
│  6│ 6   std::cout << "Number Guessing Game\n";              │
│  7│  -  std::cout << "1 to 100.\n";         [deletion]      │
│   │ 7+  std::cout << "1 to 50.\n";          [addition]      │
│  8│ 8   std::cout << "Type a guess...\n";                   │
└─────────────────────────────────────────────────────────────┘
```

- Red background: Deletions
- Green background: Additions  
- Gray: Context lines
- Left accent border: Purple (normal), Red (declined approval)

## Command Output Rendering

```
┌─────────────────────────────────────────────────────────────┐
│ Command                                                     │
├─────────────────────────────────────────────────────────────┤
│ /bin/sh -lc "cat file.txt"                    [command]     │
├─────────────────────────────────────────────────────────────┤
│ file contents here...                         [output]      │
│ (black background, white text)                              │
├─────────────────────────────────────────────────────────────┤
│ Duration: 52ms                                [footer]      │
└─────────────────────────────────────────────────────────────┘
```

Output is truncated to `command_output_lines` setting (default: 20 lines).

## Recent Changes & Fixes

### Diff Handling (Implemented)

**Problem:** codex-app-server emits ~5 diffs per file change:
- 2 short diffs (from `item/started` and `item/completed` fileChange)
- 2 long diffs (from `turn_diff` / `turn/diff/updated`)
- 1 approval diff (from `apply_patch_approval_request`)

**Solution:**
- Only render `turn_diff` events (long diffs with context)
- Skip short diffs from `item/fileChange` events
- Ignore `apply_patch_approval_request` for live approval cards; it mirrors diff context but not the actionable request id
- Deduplicate by content hash (`seenDiffs` Set)
- Extract filename from `--- a/filename` header or `path` field
- Show relative paths, strip git prefixes

**Multi-file diff blobs (Frontend fix):**
- Some `turn_diff` payloads arrive as a *single unified diff text blob* that contains multiple `diff --git a/... b/...` sections, but only a single top-level `path` field.
- The frontend now treats `diff --git ...` lines as file boundaries and renders a file header row for each file when multiple sections exist.
- Clickable diff lines (`data-path`) and per-line syntax highlighting now track the active file as the diff renderer walks the unified diff text (so multi-file diffs deeplink correctly).

**Future plan (B2: structural diff SSOT, without extra transcript noise):**
- Keep the UX contract: **one canonical diff card per turn** (avoid reintroducing the short/long/approval diff spam).
- Upgrade the transcript diff entry schema from “one big `text` + one `path`” to a structured form:
  - `{"role":"diff","changes":[{"path":"…","diff":"…"}, ...], ...}`
  - Optionally keep `text` for backwards compatibility, but prefer rendering from `changes` when present.
- This preserves a single transcript diff event per turn while making per-file path attribution lossless and eliminating the need for the frontend to infer file boundaries from raw unified diff headers.

### Approval Flow (Implemented)

**Problem:** Approval would hang after clicking Accept.

**Solution:**
- Fixed JSON-RPC response format to match request ID
- Treat `item/*/requestApproval` as the canonical actionable approval event and ignore the legacy `codex/event/*_approval_request` wrappers for live cards
- Remove approval card from DOM after decision
- Record approval status to transcript: `{"role": "approval", "status": "accepted|declined", ...}`
- Declined approvals show with red left border accent

### Reasoning Streaming (Implemented)

**Problem:** Reasoning was rendering in multiple separate boxes and not being stored correctly.

**Solution:** 
- Made reasoning work exactly like messaging:
  - Stream deltas to frontend via `appendToActivityRibbon()`
  - Store complete reasoning text to transcript on `item/completed`
  - Single reasoning card per reasoning block
  - Replay from transcript shows complete reasoning text

### Command Output (Implemented)

- Edge-to-edge rendering (no card wrapper)
- Command header: gray background
- Output area: black background, white text
- Duration footer: gray
- Purple left border accent
- Configurable truncation (`command_output_lines` setting, default: 20)
- Output stored in transcript for replay

### Server Status (Fixed)

**Problem:** Status pill showed wrong values ("draft", "pinned" instead of "running"/"idle")

**Solution:** Separated concerns - server status only shows codex-app-server state, not conversation state.

### Conversation Switching (Fixed)

**Problem:** Creating new conversation would show old transcript.

**Solution:** Clear transcript state and DOM before loading new conversation data.

### Activity Ribbon Scroll (Fixed)

**Problem:** On page load with drawer open, activity ribbon appeared at top instead of bottom.

**Solution:** Start drawer closed, wait for transcript render, then open and scroll.

## Framework Shells (FWS) Integration

The server uses **Framework Shells** (`framework_shells` package) as an orchestration layer for managing the codex-app-server process.

### What is Framework Shells?

Framework Shells is a Python library that provides:
- **Declarative shell/process management** via YAML specs
- **Process lifecycle management** (start, stop, restart, health checks)
- **stdin/stdout streaming** with async support
- **Multiplexed shell sessions** with unique IDs
- **Web UI endpoints** for shell management (logs, status)

### How It's Used

```python
# Imports
from framework_shells import get_manager as get_framework_shell_manager
from framework_shells.api import fws_ui
from framework_shells.orchestrator import Orchestrator

# Router included for web UI endpoints
app.include_router(fws_ui.router, dependencies=[Depends(lambda: _ensure_framework_shells_secret())])
```

### Shell Management Flow

1. The extension manager configures the runtime protocol and extension-owned transport.
2. The transport adopts or starts an **observed** framework-shell entry:
   - stable Codex: `shellspec/app_server.yaml#app_server_observed`
   - experimental Codex: `extensions/codex_ext_exp/shellspec/app_server_exp.yaml#app_server_exp_observed`
3. The observed shellspec runs the real app-server binary directly:
   - stable label: `app-server:codex-extension`
   - experimental label: `app-server:codex-experimental`
4. `framework_shells` owns pipe stdout consumption/logging:
   - transports read via `subscribe_output()` / `subscribe_output_bytes()`
   - stdin writes go through `write_to_pipe()`
   - direct `state.process.stdout.read(...)` loops are invalid because they race the framework-shell tee/log readers
5. Stop/reset paths clear transport-local state and terminate the observed shell through the manager when needed.

This is the current extension-owned Codex contract; the old legacy builtin shellspec path is no longer the live extension path.

### FWS Web UI Endpoints

The `fws_ui.router` provides endpoints for shell management:
- `/fws/shells` - List all shells
- `/fws/shell/{id}` - Get shell details
- `/fws/shell/{id}/logs` - Stream shell logs
- `/fws/logs` - WebSocket for live log streaming

### Why FWS?

Using Framework Shells instead of raw `subprocess`:
1. **Structured process management** - Specs define behavior declaratively
2. **Persistence** - Shell IDs survive server restarts
3. **Multiplexing** - Can manage multiple shell instances
4. **Async-first** - Native asyncio support for stdin/stdout
5. **Health monitoring** - Built-in status checks
6. **Web UI** - Free logging/management endpoints

### Configuration Storage

FWS stores its data in `~/.cache/framework_shells/`:
```
~/.cache/framework_shells/
├── runtimes/
│   └── <fingerprint>/
│       ├── secret          # Auth secret
│       └── shells/         # Shell state
```

The `fingerprint` is derived from the repository/working directory path, ensuring isolation between different projects.

### Plan Transcript (Implemented)

**Problem:** Plan updates from `turn/plan/updated` were being rendered in a sticky overlay that was removed. Plans weren't being persisted.

**Solution:**
- Accumulate plan steps in turn state during `turn/plan/updated` events
- On `turn/completed`, write completed plan to transcript as `{"role": "plan", "steps": [...], ...}`
- Emit `plan` event to frontend for live display
- Frontend renders plan as a card with checkboxes showing step status (pending ☐, in_progress ◐, completed ☑)
- Plan cards persist in transcript for replay

### SSOT Settings Injection (Implemented)

**Problem:** When resuming a thread, the frontend wasn't sending model/settings from the SSOT, causing codex-app-server to fall back to defaults from `config.toml`.

**Solution:**
- Backend intercepts `thread/resume`, `thread/start`, and `turn/start` RPC methods
- Injects settings from the conversation's SSOT meta before forwarding to codex-app-server
- Frontend no longer responsible for sending settings - only validates conversation ID matches (guards against stale tabs/multi-device conflicts)
- Clean separation: backend owns SSOT and enforces settings, frontend only guards against conflicts

**Per-Method Parameter Support:**

Different RPC methods accept different parameters (per codex-app-server schema):

| Setting | `thread/resume` | `thread/start` | `turn/start` |
|---------|-----------------|----------------|--------------|
| `model` | ✓ | ✓ | ✓ |
| `cwd` | ✓ | ✓ | ✓ |
| `approvalPolicy` | ✓ | ✓ | ✓ |
| `sandbox`/`sandboxPolicy` | ✓ | ✓ | ✓ |
| `reasoningEffort`/`effort` | ✗ | ✓ (as `reasoningEffort`) | ✓ (as `effort`) |
| `summary` | ✗ | ✗ | ✓ |

**Key Behavior:**
- Changing `model` mid-conversation: Takes effect on next `thread/resume` or `turn/start`
- Changing `effort` (reasoning level) mid-conversation: Takes effect on next `turn/start` only (not on resume)
- Changing `summary` (reasoning summary mode) mid-conversation: Takes effect on next `turn/start` only (not on resume)
- In `codex-ext`, `summary` is surfaced in the runtime-generated settings schema and persisted in SSOT/meta with enum values `auto`, `concise`, `detailed`, and `none`
- Settings are read fresh from SSOT on each RPC, enabling mid-conversation changes
- Multi-device/tab support: Backend always uses current SSOT, frontend validates conversation ID before sending

### Backend-Owned Send/Resume Strategy (Current Behavior)

**Problem:** the frontend should not decide whether a thread is or is not loaded in app-server memory.

**Solution:**
- frontend sends a generic `send_message` against `conversation_id`
- backend checks `meta.json.thread_id`
- if there is no thread yet, backend performs `thread/start` and then `turn/start`
- if there is a thread and the transport has not resumed it yet, or thread-level runtime settings changed, backend performs `thread/resume` and then `turn/start`
- if there is a thread already ready with the same thread-level settings, backend sends `turn/start` directly
- turn-only settings such as `effort` and `summary` ride on `turn/start` and do not force a thread resume by themselves

**Behavior Summary:**
- frontend stays `conversation_id`-only
- backend owns thread lifecycle
- backend also owns the split between thread-level settings and turn-only overrides
- the same ownership model matches extension backends that use session IDs instead of thread IDs

### Generic Runtime Options (Implemented)

Approval and sandbox dropdowns in the shared frontend are driven by a backend runtime-options contract.

- frontend asks generically for runtime options
- backend resolves the active agent from `meta.json.settings.agent`
- legacy Codex answers internally
- extensions answer through generic hooks in `extensions/__init__.py`

This keeps shared frontend files platform-agnostic while still reflecting runtime-supported values such as `on-request`.

### Schema-Driven Extension Information Blocks (Implemented)

Extension settings now stay entirely on the schema-driven extension path.

- `static/modals/settings_schema.js` renders generic field shapes only; it does **not** own extension-specific save/hydrate logic
- extensions may still start from a static template, but `get_settings_schema(extension_id)` can prepend live dynamic fields before the modal renders
- shared non-persisted field types now include:
  - `section` — heading/description blocks
  - `info` — read-only information rows
- dynamic schemas can set `cache: "none"` when they surface live provider/account/quota/rate-limit data so the modal refetches fresh values on open
- extension settings must not depend on hidden builtin Codex modal inputs; the extension schema payload is the source of truth for extension-owned save/hydrate behavior
- `codex-ext` / `codex-ext-exp` now inject live account + rate-limit info, and `copilot-sdk` injects live quota info, through these schema-owned information blocks

### Dynamic Model/Effort Dropdowns (Implemented)

**Problem:** Different models support different reasoning effort levels (e.g., `gpt-5.1-codex-mini` supports `low/medium/high` but not `xhigh`). Users could select invalid combinations.

**Solution:**
- Backend fetches model list from codex-app-server via `model/list` RPC, caches for 5 minutes
- Each model includes `supportedReasoningEfforts` array with valid options
- Frontend stores full model list with metadata
- When user selects a model, effort dropdown is dynamically updated to only show supported options
- If current effort is not supported by new model, it's automatically reset to the model's default

**Model List Response Structure:**
```typescript
{
  data: [{
    id: "gpt-5.1-codex-mini",
    displayName: "gpt-5.1-codex-mini",
    supportedReasoningEfforts: [
      {reasoningEffort: "medium", description: "..."},
      {reasoningEffort: "high", description: "..."}
    ],
    defaultReasoningEffort: "medium",
    isDefault: false
  }, ...]
}
```

### Streaming Markdown Rendering (Implemented)

**Problem:** Agent responses with markdown (code blocks, bold, lists) weren't being rendered properly during streaming or in transcript replay.

**Solution:**
- Integrated `streaming-markdown` (smd) library for live markdown parsing during token deltas
- Markdown parser is created per message and receives deltas incrementally via `smd.parser_write()`
- On message complete, `smd.parser_end()` finalizes rendering
- finalized assistant cards and replay/static assistant cards are rendered through the markdown-it event renderer instead of keeping the live streaming parser around
- User toggle in conversation header and settings modal to enable/disable markdown
- Setting persisted in conversation SSOT as `markdown: true|false`
- Citations like `'citeturn1file0L11-L26'` are stripped from rendered output
- markdown file links are intercepted and routed through the TE2 open flow, including `#L123`, `#L123C4`, and `path:line[:column]` location syntax
- external `http/https` links are intercepted and opened through the backend `xdg-open` path instead of raw iframe/browser navigation

**Key Code:**
```javascript
const renderer = smd.default_renderer(container);
const parser = smd.parser(renderer);
// On each delta:
smd.parser_write(parser, cleanDelta);
// On complete:
smd.parser_end(parser);
```

### File Mentions with Tribute.js (Implemented)

**Problem:** The original `@` mention system was clunky - cursor jumped to end on insert, `@` at start of text didn't work, tokens were hard to edit/delete.

**Solution:**
- Replaced custom implementation with Tribute.js library for autocomplete
- `@` trigger shows file picker dropdown with fuzzy search
- Files and directories are fetched from backend via `/api/appserver/mention`
- Uses `ripgrep --files` for fast file listing, respects `.gitignore`
- Selected files inserted as non-editable tokens (spans with `contenteditable="false"`)
- Directories and files separated in dropdown with visual separator
- Files shown as 📄 icon, directories as 📁 icon
- Paths are relative to conversation CWD

**API Endpoint:**
```
POST /api/appserver/mention
{
  "query": "search term",
  "limit": 30
}

Response:
{
  "files": [
    {"path": "src/main.py", "type": "file"},
    {"path": "src/utils/", "type": "directory"}
  ]
}
```

### Direct Shell Command Execution (Implemented)

**Problem:** Users wanted to run shell commands directly without going through the agent.

**Solution:**
- `!` prefix in input triggers direct shell execution (e.g., `!ls -la`)
- Commands sent to backend `/api/appserver/shell/exec` endpoint
- Backend executes via codex-app-server's shell interface
- Output rendered in same style as agent command cards
- Results persisted to transcript with role `shell_input` and `shell_output`
- Exit code determines success/error styling

**Transcript Format:**
```jsonl
{"role": "shell_input", "command": "ls -la", "timestamp": "ISO"}
{"role": "shell_output", "command": "ls -la", "stdout": "...", "stderr": "...", "exit_code": 0, "timestamp": "ISO"}
```

### Status Ribbon Refactor (Implemented)

**Problem:** Activity ribbon took up too much space and didn't show command success/failure status.

**Solution:**
- Refactored ribbon to tile above user input area (6px height)
- Added status dot indicator (right-justified):
  - 🟢 Green: Last command/response succeeded
  - 🔴 Red: Last command/response failed/errored
  - 🟡 Yellow: Warning
  - ⚪ Gray: Idle/neutral
- Spinner scaled down to fit compact ribbon
- Status persisted to transcript as `{"role": "status", "status": "success|error", ...}`
- Replays restore status dot state from transcript

### Mobile Keyboard Handling (Implemented)

**Problem:** On mobile, pressing Enter key in the input would send the message, making it impossible to type multi-line messages.

**Solution:**
- User agent detection for mobile devices (Android, iOS, etc.)
- On mobile: Enter key inserts newline, only Send button submits
- On desktop: Enter sends, Shift+Enter inserts newline
- Detection uses standard mobile UA patterns

### Context Window Display (Fixed)

**Problem:** Context window pill showed wrong percentage (was using `active_context` which is tiny due to caching).

**Solution:**
- Parse `thread/tokenUsage/updated` events properly
- Calculate percentage: `(totalTokens / modelContextWindow) * 100` - matches CLI behavior
- Display as "used" percentage in UI (e.g., 56% means 56% of context window consumed)
- Store raw values in transcript for replay: `{"role": "token_usage", "total": N, "input_tokens": N, "cached_input_tokens": N, "active_context": N, "context_window": M, ...}`
- Note: `active_context = inputTokens - cachedInputTokens` is also recorded but not used for display percentage

### Word Wrap Styling (Implemented)

Added CSS for proper word wrapping in agent messages, user messages, and reasoning:
```css
.message-body {
  overflow-wrap: break-word;
  word-wrap: break-word;
  word-break: break-word;
}
```

## API Endpoints (Updated)

### Shell Execution

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/shell/exec` | POST | Execute shell command directly |

**Request:**
```json
{"command": "ls -la"}
```

**Response:**
```json
{"stdout": "...", "stderr": "", "exitCode": 0}
```

### File Mentions

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/appserver/mention` | POST | Get file/directory list for mention autocomplete |

**Request:**
```json
{"query": "src", "limit": 30}
```

**Response:**
```json
{"files": [{"path": "src/main.py", "type": "file"}, ...]}
```

## Transcript Entry Types (Updated)

```jsonl
{"role": "shell_input", "command": "ls", "timestamp": "ISO"}
{"role": "shell_output", "command": "ls", "stdout": "...", "stderr": "", "exit_code": 0, "timestamp": "ISO"}
{"role": "status", "status": "success|error|warning", "timestamp": "ISO"}
{"role": "token_usage", "total": 145243, "input_tokens": 145013, "cached_input_tokens": 144896, "active_context": 117, "context_window": 258400, "timestamp": "ISO"}
```

## Future Considerations

1. **Session recovery** - Better handling of corrupted rollouts
2. **Multi-agent** - Agent log server for inter-agent communication
3. **Consecutive reasoning merge** - Merge multiple consecutive reasoning blocks into one display card
4. **Warp-like shell interface** - Full shell mode with streaming output, using codex-app-server's PTY management
5. **Screen model colors** - Current pyte screen model is text-only; adding ANSI color preservation requires new delta schema with cell attributes

## Pluggable Extension System

The server supports pluggable agent extensions alongside the built-in Codex flow. Extensions plug in via `extensions/__init__.py` (the "ext_loader") without modifying `server.py`, `codex_agent.js`, or `settings_schema.js` with extension-specific logic.

Shared live/runtime UI contracts are Socket.IO-based. Generic HTTP routes still exist for conversation loading, debug, and admin flows, but they are not the fallback path for runtime extension behavior.

### INVARIANT: Platform-Agnostic Core Files

**`server.py`, `codex_agent.js`, and `settings_schema.js` must contain ZERO extension-specific code.** See `AGENTS.md` for the full invariant. All extension interaction in server.py goes through `ext_loader`:

```python
import extensions as ext_loader
result = await ext_loader.list_models(extension_id)  # CORRECT
# from extensions.copilot_sdk_client import list_models  # WRONG — will be reverted
```

### Generic Extension API Routes

All extension HTTP endpoints use `{extension_id}` path parameters:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /api/extensions/{id}/models` | GET | List available models |
| `GET /api/extensions/{id}/sessions` | GET | List sessions (with `?cwd=` sorting) |
| `POST /api/extensions/{id}/sessions/resume` | POST | Resume+hydrate a session into a conversation |
| `GET /api/extensions/{id}/debug/raw` | GET | Debug event buffer |
| `GET /api/extensions/{id}/settings_schema` | GET | Get settings UI schema |

### Generic Extension SIO Handlers

| Event | Data | Purpose |
|-------|------|---------|
| `get_sessions` | `{extension_id, cwd}` | List sessions |
| `session_resume` | `{extension_id, session_id, conversation_id}` | Bind/import session for a new local conversation |
| `approval_response` | `{request_id, decision}` | Resolve approval (reads agent from meta) |
| `get_extension_models` | `{extension_id}` | List models |

### New session from port-in vs existing conversation hydration

There are two different flows that are easy to conflate:

- **new session from port-in**
  - bind an external rollout/session into a fresh local conversation
  - import flat transcript entries before the response returns
- **existing conversation hydration**
  - replay an already-local `transcript.jsonl`
  - uses the platform-agnostic replay helpers
  - if a remote thread/session is already bound, keep the backend cold until the first new send
  - the first new send uses the backend-specific lazy resume/retry path
  - suppress resume/load history noise until the backend ack so it does not duplicate the already-local transcript

Reference patterns:

```
Legacy Codex port-in:
  1. _rollout_preview_entries(path)                         → List[{role, text, ts}]
  2. _write_transcript_entries(items)                       → writes transcript.jsonl

Copilot SDK port-in:
  1. ext_loader.resume_session_with_history(ext_id, ...)    → bind + resume
  2. ext_loader.hydrate_transcript(ext_id, session_id, ...) → List[{role, text, ts}]  (SDK history)
  3. _write_transcript_entries(items)                       → writes transcript.jsonl

codex-ext port-in (also used by the `codex-ext-testing` compat shim):
  1. ext_loader.resume_session_with_history(ext_id, ...)    → bind + resume
  2. ext_loader.hydrate_transcript(ext_id, session_id, ...) → List[{role, text, ts}]  (internal rollout import helper)
  3. _write_transcript_entries(items)                       → writes transcript.jsonl

Existing harness conversation reload with bound backend session/thread:
  1. replay the already-local transcript.jsonl              → frontend conversation cards
  2. leave the backend session/thread cold
  3. first new send hits the normal backend send path
  4. extension resumes/reattaches on cold-backend failure
  5. retry the buffered message
  6. suppress resume/load history noise until backend ack

Gemini ACP-style fallback when the backend does not advertise a true `session/resume` capability:
  1. replay the already-local transcript.jsonl              → frontend conversation cards
  2. leave the bound backend session cold
  3. first new send fails against the cold session
  4. extension calls `load_session()` as the synthetic reattach ack
  5. keep replay suppression active until delayed history notifications reach a quiet period
  6. only then enter prompt-capture mode and retry the buffered message
```

**Critical:** Port-in hydration MUST complete before the HTTP response returns. The frontend transitions to conversation view and calls `replayTranscript()` immediately — if transcript is not written yet, the view is empty.

**Also critical:** for load-only backends, waiting for the `load_session()` RPC to return is not enough. The replay update stream may still be draining after the RPC result is delivered, so the extension needs a real quiet-period barrier before it switches into live prompt capture.

See also:

- `AGENT_EXTENSION_INTEGRATION.md` — canonical extension hook/lifecycle contract
- `AGENTS.md` — short repo-wide invariant/guardrail

### ext_loader Pass-Through Methods

`extensions/__init__.py` exposes generic methods that route to the active handler:

| Method | Signature | Purpose |
|--------|-----------|---------|
| `list_models(ext_id)` | `async → List` | Get available models |
| `get_settings_schema(ext_id)` | `async → Dict` | Get dynamic or static settings schema |
| `list_sessions(ext_id, cwd)` | `async → List` | Get resumable sessions |
| `resume_session_with_history(ext_id, session_id, convo_id, ...)` | `async → Dict` | Bind/import session to conversation; ordinary existing-conversation reload remains cold until first-send lazy resume |
| `hydrate_transcript(ext_id, session_id, convo_id, ...)` | `async → List[Dict]` | Build flat transcript entries for a new session from port-in/import |
| `get_runtime_options(ext_id, conversation_id, settings)` | `async → Dict` | Return generic approval/sandbox descriptors |
| `resolve_approval(ext_id, request_id, decision)` | `sync → None` | Resolve pending approval |
| `compact_session(ext_id, convo_id)` | `async → Dict` | Trigger extension-owned compaction |
| `shutdown_extension(ext_id)` | `async → None` | Graceful shutdown |
| `get_raw_buffer(ext_id, limit)` | `sync → List` | Debug buffer |

All follow the same pattern: `get_handler(ext_id) → hasattr(handler, method) → handler.method(...)`.

### codex-ext / codex-ext-exp today

`codex-ext` is the stable extension-owned Codex path. `codex-ext-testing` remains a compatibility registry alias that resolves to the same `extensions/codex_ext` implementation, and `codex-ext-exp` is the experimental fork in `extensions/codex_ext_exp`.

- settings modal fields are generated dynamically from the installed Codex app-server JSON schema
- extension-owned settings are fully schema-driven and do **not** depend on hidden builtin Codex modal fields for save or hydrate
- shared settings modals now support read-only `section` / `info` fields plus `cache: "none"` so extensions can prepend live provider information without polluting persisted settings
- session browse uses the generic `session_picker` flow
- bind/import uses the same generic `resume_session_with_history(...)` + `hydrate_transcript(...)` contract as Copilot, but the Codex implementation reuses internal rollout import helpers
- stable live send/resume/interrupt/compact uses an extension-owned framework-shell transport rooted at `shellspec/app_server.yaml#app_server_observed` with transport label `app-server:codex-extension`
- the stable observed shell now runs `codex app-server` directly; framework-shells owns stdout observability for the pipe shell
- `codex-ext-exp` keeps a separate shellspec/transport label (`shellspec/app_server_exp.yaml#app_server_exp_observed`, `app-server:codex-experimental`) so it can point at the patched `codex-app-server` binary without adoption conflicts
- both observed shellspecs now run the real app-server binary directly and rely on framework-shells pipe stdout subscriptions/logging instead of mirroring RPC traffic to stderr
- runtime response schemas are keyed by lowercase method names, and the request/response/notification/event registries are all derived from the generated schema bundle; manual overrides should only remain for true semantic naming mismatches, not lazy registry gaps

#### Patched `codex-ext-exp` app-server build

The experimental Codex path uses a local custom `codex-rs` checkout instead of the stock `codex app-server` binary on `PATH`.

- local repo checkout: `~/downloads/codex/codex-rs`
- upstream base tag: `rust-v0.117.0-alpha.5`
- patch branch: `patch/dynamic-developer-instructions`
- patched binary path: `~/downloads/codex/codex-rs/target/debug/codex-app-server`
- purpose: add mid-thread / per-turn `developer_instructions` override support so dynamic developer-instruction and pending-context updates can land on the next turn without starting a new thread
- Termux build recipe:
  - `pkg install libzstd`
  - `cd ~/downloads/codex/codex-rs`
  - `ZSTD_SYS_USE_PKG_CONFIG=1 CC=cc cargo build -p codex-app-server`
- canonical patch note in the Codex repo:
  - `~/downloads/codex/codex-rs/docs/patched_app_server.md`

Ongoing extension-owned work includes richer tool-card parity, approval-path parity, and continuing the experimental dynamic-context path in `codex-ext-exp`.

### Adding a New Extension

1. Create `extensions/<name>/client.py` and optional helpers such as `router.py`, `transport.py`, or runtime-schema helpers
2. Create `extensions/<name>/manifest.json` and either `settings_schema.json` or a dynamic `get_settings_schema()` hook
3. Add an entry to `extensions/extensions.json`
4. Keep backend transport/session ownership inside the extension so the frontend can stay unified
5. **Do NOT touch** `server.py`, `codex_agent.js`, or `settings_schema.js` with extension-specific protocol logic

## MCP Agent PTY Integration

The server includes an MCP (Model Context Protocol) server for PTY management, enabling agent-to-agent communication and TUI control.

### Screen Model (pyte)

A pyte-based terminal screen model provides clean rendered output for agents:

- **Raw bytes** → `output.raw` (lossless, via `subscribe_output_bytes`)
- **Screen deltas** → `screen.jsonl` (rate-limited, clean text rows)
- **Snapshots** → `screen.snapshot.json` (full screen state)

**MCP Tools:**
| Tool | Description |
|------|-------------|
| `pty_read_raw` | Read lossless raw bytes (base64 encoded) |
| `pty_read_screen` | Get current screen state (40 rows, cursor, title) |
| `pty_read_screen_deltas` | Read incremental screen changes |
| `pty_screen_status` | Get screen metadata without row content |

### Data Flow

```
PTY (bash + rcfile)
    │
    ├─► subscribe_output() ──► text ──► _on_chunk ──► spool + block parsing
    │                                                  (markers filtered here)
    │
    └─► subscribe_output_bytes() ──► bytes ──► _run_bytes
                                                  │
                                                  ├─► output.raw (lossless)
                                                  ├─► raw_events.jsonl
                                                  ├─► pyte screen model
                                                  └─► screen.jsonl (deltas)
```

**Note:** Shell markers (`__FWS_BLOCK_BEGIN__`, etc.) are in raw output because they're printed by the rcfile. The `agent_block_delta` events are already filtered (clean). Screen model currently includes markers; filtering planned.

### `ask_user` identity contract

The current MCP `ask_user` contract is split into two identities on purpose:

- canonical `requestId` / approval rendezvous key = the conversation's own `conversationId`
- router-owned `card_id` = the stable DOM/replay identity for each rendered ask-user card

Important live-routing detail:

- Codex/Codex-exp bind live `agent-pty-blocks.ask_user` cards from the `item/started` notification carrying `item.type == "mcpToolCall"`
- they do **not** rely on a separate emitted `item/tool/call` request in the tested runtime path

This lets multiple `ask_user` cards coexist in one conversation while keeping the actual approval submission keyed to the conversation-scoped IPC/MCP rendezvous id.

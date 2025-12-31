# Codex App Server Extension

This document is a technical deep dive into the **Codex App Server Extension** side of the project. It is intentionally separate from the **Agent Log Server** concerns, even though both live in the same repo. Think of them as opposite sides of the same coin:

- **Agent Log Server** = persistent “message board” for humans/agents.
- **Codex App Server Extension** = live JSON‑RPC bridge + UI contract for Codex app‑server.

Source of truth for this doc is the repo code (`server.py`, `static/codex_agent.js`, `static/codex_agent.css`) and the local `codex-app-server_README.md` + `fws_README.md`.

---

## 1) High‑Level Architecture

**Codex app‑server** speaks **JSON‑RPC 2.0 over stdio** (JSONL) but omits the `jsonrpc` header. This extension:

1. **Spawns** `codex app-server` via **Framework Shells** using the **pipe backend**.
2. **Reads stdout** line‑by‑line (JSONL) from the pipe.
3. **Parses, routes, and sanitizes** server events into UI‑friendly events.
4. **Writes JSON‑RPC requests** to the same pipe (stdin).
5. **Persists** the conversation transcript in `~/.cache/app_server/transcripts/<thread_id>.jsonl`.
6. **Replays** transcript entries on UI refresh.

The UI (codex agent tab) is **dumb** by design: it only receives sanitized events and never needs to parse raw JSON‑RPC. This keeps frontend logic simple and stable.

---

## 2) Framework Shells Role (Process Orchestration)

The extension uses **Framework Shells** (FWS) as the process manager. Key points:

- **Backend:** `pipe` (stdin/stdout)
- **Shellspec:** `shellspec/app_server.yaml`
- **Lifecycle:** the shell is started/stopped by the server, not the UI
- **Isolation:** shells are namespaced by repo fingerprint + secret (see `fws_README.md`)

Shellspec (current):
```
version: "1"
shells:
  app_server:
    backend: pipe
    cwd: ${CWD}
    subgroups: ["app_server", "codex"]
    command:
      - ${APP_SERVER_COMMAND}
    labels:
      app: "codex-app-server"
```

### Why pipe backend?
Pipe gives direct access to stdin/stdout with **JSONL streaming**, which matches codex app‑server’s protocol. PTY/dtach aren’t required for JSON‑RPC and add extra complexity.

### Secret + runtime ID
Framework Shells needs `FRAMEWORK_SHELLS_SECRET` to calculate the runtime ID. If missing, the manager raises:
```
RuntimeError: FRAMEWORK_SHELLS_SECRET is required
```
Make sure the secret is set or stored so the same runtime can be recovered across restarts.

---

## 3) App‑Server Process Lifecycle

**Server endpoints (FastAPI)**

- `POST /api/appserver/start`
  - Starts or reuses the app‑server shell.
  - Spawns reader task for stdout.
- `POST /api/appserver/stop`
  - Terminates the shell.
- `GET /api/appserver/status`
  - Returns `running` + shell details.
- `POST /api/appserver/rpc`
  - Sends arbitrary JSON‑RPC payload to stdin.
- `POST /api/appserver/initialize`
  - Sends `initialize` + `initialized`.

**Process start flow**
1. `_get_or_start_appserver_shell()` loads config for `cwd` and `app_server_command`.
2. Framework Shells `Orchestrator` starts the pipe backend shell.
3. `_ensure_appserver_reader()` attaches to stdout and begins the JSONL read loop.

**Process shutdown**
- Calls Framework Shells terminate APIs.
- Should also be tied into app shutdown hooks to avoid orphaned shells.

---

## 4) Transport & Message Parsing

### Raw JSONL stdout
Each line is either:

1. **A label line** (e.g., `turn/started`)
2. **A JSON object** line
3. **A combined label + JSON** line (prefix + JSON)
4. **A JSON‑RPC response** with `id`

The reader keeps a `pending_label` so it can associate label lines with the following JSON payload.

### JSON‑RPC Request/Response
Requests are sent **without** the `jsonrpc` header (per `codex-app-server_README.md`).
Responses are recognized as `{id: ..., result: ...}` or `{id: ..., error: ...}`.

**UI events emitted for responses:**
- `rpc_response`
- `rpc_error`

These are used to resolve client‑side pending requests.

---

## 5) Message Router (Core Logic)

The router lives in `_route_appserver_event()` and is responsible for:

- **Filtering noise** (startup, rateLimits, item started/completed that aren’t user‑facing)
- **Routing deltas** (assistant and reasoning)
- **Finalizing** assistant + reasoning items
- **Capturing diffs & approvals**
- **Maintaining state** per thread + turn + item

### Turn/Item State
State is tracked per `(thread_id, turn_id)`:
```
msg_source, reason_source
assistant_id, reasoning_id
assistant_started, reasoning_started
assistant_buffer, reasoning_buffer
diff_hashes, diff_seen
```
This allows the router to:
1) decide which event stream to trust (`item/*` vs `codex/event/*`)
2) de‑dupe diffs and deltas
3) assemble streaming output into a single UI row

---

## 6) Single‑Conversation Mode (Current Behavior)

This repo is intentionally **single‑conversation only** for now:

- Once `thread_id` is set, it is **never overwritten**.
- The UI only ever calls `thread/resume` for that pinned `thread_id`.
- `/api/appserver/transcript` falls back to the most recent transcript file and **pins** it.

This prevents new conversation IDs from being created while testing and keeps the UI stable.

---

## 7) Transcript SSOT (Single Source of Truth)

Transcript files are stored under:
```
~/.cache/app_server/transcripts/<thread_id>.jsonl
```

Each entry has:
```
{
  "ts": "<iso>",
  "role": "user" | "assistant" | "reasoning" | "diff",
  "text": "...",
  "item_id": "...",
  "event": "item/completed" | "turn_diff" | ...
}
```

### What is recorded
- **User messages** (`item/started` userMessage)
- **Assistant messages** (finalized)
- **Reasoning summaries** (finalized)
- **Diffs** (turn‑level unified diff)

### What is NOT recorded
- Streaming deltas (those are UI‑only)
- Low‑level status noise

### Replay
UI calls `/api/appserver/transcript` and replays into the timeline:
- `reasoning` → appended as a reasoning block
- `diff` → rendered as diff row
- `user` / `assistant` → standard message rows

---

## 8) Diff Handling (De‑dupe + Identity)

Diffs arrive from multiple event types (e.g. `turn/diff/updated`, `codex/event/turn_diff`, fileChange items). The router:

1. Extracts the unified diff text.
2. Builds a **signature hash** using:
   - file header lines (`+++`, `---`)
   - hunk headers (`@@`)
   - full diff body
3. Uses the hash to **de‑duplicate** identical diffs.
4. Emits an event with `diff_id = thread:turn:hash`.

This means:
- Same diff repeated → ignored.
- Same file re‑edited → new diff row.
- Multiple diffs per turn → separate rows.

Diffs are rendered with dark background + green insertions / red deletions.

---

## 9) Approvals (Commands + File Changes)

The app-server sends **server-initiated JSON-RPC requests** when approval is needed.

### Command approvals
Sequence (simplified):
1. `item/started` with `commandExecution`
2. `item/commandExecution/requestApproval`
3. Client replies `{ decision: "accept" | "decline" }`
4. `item/completed` with final status and output

### File change approvals
Sequence (simplified):
1. `item/started` with `fileChange`
2. `item/fileChange/requestApproval`
3. Client replies `{ decision: "accept" | "decline" }`
4. `item/completed` with final status

The extension renders approvals inline in the timeline and issues the response via
`/api/appserver/rpc` to the app-server.

---

## 10) UI Contract (Codex Agent Tab)

The UI only consumes **sanitized events** from the backend via `/ws/appserver`:

Event types emitted:
- `activity` (updates the pinned activity line)
- `message` (user or assistant message)
- `assistant_delta` (streaming assistant tokens)
- `assistant_finalize` (final assistant message)
- `reasoning_delta` (streaming reasoning summary)
- `diff` (unified diff)
- `approval` (command or diff approval)
- `token_count` (counters)
- `rpc_response` / `rpc_error`

The activity line is pinned to the bottom of the timeline and stays visible.
Auto-scroll can be released via a "Pinned/Free" toggle.

### UI guarantees
- Raw JSON-RPC never reaches the browser.
- Deltas are assembled into single rows (assistant + reasoning).
- Diff rows are edge-to-edge and de-duplicated.
- Transcript replay hydrates the timeline on page refresh.

---

## 11) Configuration & REST Knobs

Configuration is stored at:
```
~/.cache/app_server/app_server_config.json
```

Fields:
- `cwd`: project root for app-server
- `thread_id`: pinned conversation id (single-conversation mode)
- `app_server_command`: optional override for command
- `shell_id`: last known shell id

REST endpoints:
- `GET /api/appserver/config`
- `POST /api/appserver/config`
- `POST /api/appserver/cwd`
- `GET /api/appserver/transcript`
- `POST /api/appserver/start`
- `POST /api/appserver/stop`
- `POST /api/appserver/rpc`
- `POST /api/appserver/initialize`

These are intentionally kept even though WebSocket is the main transport, because
they enable automated smoke tests and external tooling.

---

## 12) Failure Modes & Observability

### Common failure cases
- **Missing `FRAMEWORK_SHELLS_SECRET`**: FWS manager errors before shell spawn.
- **App-server stdout not connected**: no events stream to UI.
- **Thread ID mismatch**: transcript replay returns empty if configured ID has no file.

### Observability
- Raw stdout is available via `/ws/appserver?mode=raw` for debugging.
- Sanitized events are on `/ws/appserver`.
- FWS logs are stored under `~/.cache/framework_shells/.../logs`.

---

## 13) Relationship to Agent Log Server

This extension intentionally does **not** replace the agent log:

- Agent log is a **shared message board**.
- App server extension is **live streaming + SSOT transcript** for Codex app-server.

They are separate, but the same UI can embed both (e.g., agent log in one tab,
codex app-server in another).

---

## 14) Next Steps (Future Work)

- Multi-conversation support (thread list + resume UI)
- App-server config editor UI
- Unified diff rendering enhancements (file headers -> readable labels)
- Streaming tool output rendering (command stdout/stderr panels)

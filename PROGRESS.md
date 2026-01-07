## Progress

### Done
- Added MCP server `mcp_agent_pty_server.py` (stdio) with tools: `pty.exec`, `pty.exec_interactive`, `pty.send`, `pty.wait_for`, `pty.status`, `pty.read_spool`, `pty.end_session`, `pty.ctrl_c`, `pty.ctrl_d`, `pty.enter`, `blocks.since`, `blocks.get`, `blocks.read`, `blocks.search`.
- Added pyte screen model with lossless raw byte capture, screen deltas/snapshots, and Sprint 3 MCP tools: `pty_read_raw`, `pty_read_screen`, `pty_read_screen_deltas`, `pty_screen_status`.
- Added golden test doc for screen rendering: `SCREEN_MODEL_GOLDEN_TEST.md`.
- Agent PTY is per conversation (dtach-backed) with deterministic block markers; writes `agent_pty/blocks.jsonl`, `agent_pty/blocks/*.out`, `agent_pty/events.jsonl`, `agent_pty/output.spool`.
- Server tails `agent_pty/events.jsonl` and fans out `agent_block_*` events to the frontend via WebSocket.
- Transcript replay: agent PTY blocks now render as xterm-backed transcript cards (prompt + output).
- Frontend renders `agent_block_begin/agent_block_delta/agent_block_end` as terminal-style cards.
- Added xterm.js scaffolding + a Terminal/Chat toggle; in terminal mode, Enter submits on desktop and mobile.
- Added prompt sentinel `__FWS_PROMPT__` emitted by shell rcfile after each command (enables `wait_for(match_type="prompt")`).
- Added cursor-based `wait_for` with `match_cursor`, `match_span`, `next_cursor` semantics.
- Added output spool (`output.spool`) for deterministic cursor-based awaiting.
- Added mode state machine: `idle` → `block_running` / `interactive` → `idle`.
- Persisted transcript offset to disk (`.transcript_offset`) to survive server restarts.

### Known Issues (Current)

#### Cursor Semantics Bug
- `next_cursor` is end-of-scanned-chunk, which can skip matches that were in the same scanned data.
- Chaining works with `match_span.end`, but NOT with `next_cursor`.
- **Fix needed**: Return `resume_cursor = match_span.end` (or rename to clarify intent).
- Timeout path returns `next_cursor=0` due to `read_spool(0, 0)` bug.

#### UI Rendering Issues
- Agent PTY blocks may pile up at bottom of transcript on replay (due to duplicate writes before offset persistence fix).
- xterm.js vs text box toggle not fully wired in settings.
- Live rendering works, replay may show duplicates for old transcripts.

#### Usability Issues
- Cursor management is error-prone for agents (next_cursor vs match_span.end).
- Interactive flows require 4-6 tool calls for simple prompts.
- No atomic `expect+send` primitive.

### In Progress
- Fix cursor semantics (`resume_cursor` instead of ambiguous `next_cursor`).
- De-clunk the API with higher-level primitives (`pty.expect_send`, `pty.wait_prompt`).
- Add `client_id` parameter for multi-client isolation.

### Next
- Add `pty.expect_send(expect, send)` atomic helper.
- Add `pty.exec_interactive_expect(command, steps:[{expect, send}...])` for scripted flows.
- Add `client_id` to all PTY tools for per-thread, per-client isolation.
- Update storage paths to `threads/<thread_id>/clients/<client_id>/`.
- Create `docs/MCP_PTY_USAGE.md` and AGENTS.md snippet.

## Direction (High Level)
- Goal: build a Warp-like "blocks" terminal model, not a shared terminal; each conversation gets two stateful PTYs (agent-owned + user-owned).
- UI: terminal I/O lives inside the transcript as block cards (xterm-backed now; later potentially ephemeral), with a small `>_` toggle to switch the composer into command-entry mode.
- Capability: agent gets a superior, queryable PTY data model (blocks, search, replay); user gets an interactive PTY whose blocks can be optionally exposed for "show and tell" (read-only to agent initially).
- MCP tools should work for ANY MCP client, not just the codex-agent UI (requires explicit `thread_id` + `client_id` parameters).

## Architecture: Event Router

The server has two event routing paths:

### 1. Codex App Server Events (main router)
```
codex-app-server stdout → _appserver_reader → _route_appserver_event
                                                      │
                                  ┌───────────────────┴───────────────────┐
                                  │                                       │
                         [Frontend Events]                      [Transcript SSOT]
                         via _broadcast_appserver_ui()          via _append_transcript_entry()
                         - Streaming deltas                     - Complete items for replay
                         - Activity indicators                  - User messages, assistant msgs
                         - Approvals                            - Commands, diffs, plans
```

### 2. Agent PTY Events (file-based tailing)
```
mcp_agent_pty_server.py writes → agent_pty/events.jsonl
                                        │
            ┌───────────────────────────┴───────────────────────────┐
            │                                                       │
   _ensure_agent_pty_event_tailer()                    _tail_agent_pty_events_to_transcript()
   - Tails events.jsonl                                - Tails events.jsonl (separate offset)
   - Broadcasts to WebSocket (live UI)                 - Writes to transcript.jsonl (replay)
   - Uses _agent_pty_ws_offsets (in-memory)            - Uses _agent_pty_transcript_offsets
                                                       - Offset persisted to .transcript_offset
```

**Key insight**: The two tailers use SEPARATE offset tracking. The WebSocket tailer is for live UI updates. The transcript tailer is for replay persistence. Both read from the same `events.jsonl` but track progress independently.

**Server restart behavior**: 
- WebSocket offsets reset to 0 (re-broadcast is harmless, UI handles duplicates via `agentBlockRows` map).
- Transcript offset is persisted to disk, so no duplicate writes on restart.

## Key Files
- `server.py`: main FastAPI app + websocket fanout; manages per-conversation state; tails `agent_pty/events.jsonl` into `transcript.jsonl`; exposes `/api/mcp/agent-pty/*` endpoints and the current in-process bridge for `agent-pty/exec`.
- `mcp_agent_pty_server.py`: MCP (FastMCP) stdio server that owns the agent PTY + block store; spawns a dtach-backed bash per conversation; emits `agent_block_*` events and spools outputs into per-block `.out` files + `output.spool` for cursor-based awaiting.
- `shellspec/mcp_agent_pty.yaml`: framework-shells spec used to run/monitor the MCP agent PTY server as a managed service.
- `static/codex_agent.js`: frontend logic for chat/terminal toggle, composer "terminal mode" behavior, live websocket event handling, and transcript replay rendering; creates xterm instances inside transcript cards for agent blocks; uses `agentBlockRows` map to prevent duplicate rendering.
- `static/codex_agent.css`: transcript "terminal illusion" styling (no gaps, square corners) and terminal card styling (including xterm background overrides).
- `templates/*`: HTML templates used by the server-rendered UI (including script/style includes; xterm is currently pulled via CDN from `server.py`).

## MCP PTY Data Model

### Storage Layout
```
~/.cache/app_server/conversations/<conversation_id>/
├── transcript.jsonl           # SSOT for replay (all roles)
├── conversation_meta.json     # Settings, thread_id, etc.
└── agent_pty/
    ├── events.jsonl           # Raw agent_block_* events
    ├── blocks.jsonl           # Block index (metadata)
    ├── output.spool           # Normalized output for cursor-based wait_for
    ├── .transcript_offset     # Persisted offset for transcript tailer
    └── blocks/
        └── <seq>_<ts>.out     # Per-block raw output
```

### Block Events
- `agent_block_begin`: `{type, conversation_id, block: {block_id, seq, ts_begin, cwd, cmd, status, output_path}}`
- `agent_block_delta`: `{type, conversation_id, block_id, delta}`
- `agent_block_end`: `{type, conversation_id, block: {..., ts_end, exit_code, status}}`

### Prompt Sentinel
Shell emits after each command:
```
__FWS_PROMPT__ ts=<ms> cwd_b64=<base64>
```
Used by `wait_for(match_type="prompt")` to detect when shell is ready.

### Cursor Model
- Output spool is append-only, normalized to `\n`.
- Cursor = byte offset into spool.
- `wait_for` returns: `{match_cursor, match_span: {start, end}, next_cursor}`.
- **Bug**: Agent should use `match_span.end` (not `next_cursor`) as `from_cursor` for next wait.

## Proposed API Improvements

### 1. Fix Cursor Semantics
Return `resume_cursor` instead of ambiguous `next_cursor`:
- `resume_cursor = match_span.end` when matched
- `resume_cursor = spool_size` on timeout

### 2. Add Atomic Helper
```
pty.expect_send(thread_id, client_id, expect, send, timeout_ms, from_cursor)
→ {ok, matched, match_text, match_span, resume_cursor}
```

### 3. Add Multi-Step Helper
```
pty.exec_interactive_expect(thread_id, client_id, command, steps:[{expect, send}...])
→ {ok, session_id, block_id, exit_reason, resume_cursor}
```

### 4. Add Client Isolation
All PTY tools accept `thread_id` + `client_id` for multi-client isolation.
Storage: `threads/<thread_id>/clients/<client_id>/...`

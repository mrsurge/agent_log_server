# Superior Data Model: Invariants, Contracts, and Fixes

This document defines the **non-negotiable invariants**, the **required fixes**, and the **acceptance criteria** for the agent PTY "blocks + spool + cursor" model. The goal is a **deterministic, awaitable, and queryable** terminal data model that is strictly superior to one-shot exec tooling and robust enough for long-lived interactive sessions.

This is a *spec*, not a proposal. Implementations should conform to these rules exactly, and tests should validate them.

---

## 1) Scope and Goals

### 1.1 Scope
- **Applies to**: agent-owned per-conversation PTY subsystem (blocks, spool, cursors, wait_for, exec_interactive, prompt sentinel, block store).
- **Does not cover**: UI rendering details, non-PTY exec tools, or external process orchestration beyond the PTY manager.

### 1.2 Goals
- Deterministic, resumable output consumption.
- Safe and controllable interactive sessions (installers, REPLs, TUIs).
- A stable, queryable, structured transcript (blocks + search + slice).
- No implicit fallback behaviors that mask errors or produce ambiguous results.

---

## 2) Definitions

### 2.1 Block
A block is a unit of terminal execution with a start and end marker.

**Types**
- **Block Mode**: created by `exec_block` (BEGIN/END markers from shell wrapper).
- **Interactive Session Block**: created by `exec_interactive`, runs until prompt sentinel or explicit end.

**Required fields**
- `block_id`: unique per block.
- `cmd`: original command submitted.
- `cwd`: working directory at start.
- `ts_begin`, `ts_end`: timestamps.
- `status`: `running | completed | failed | cancelled | interactive`.
- `exit_code`: integer or null (if unknown).
- `output_path`: path to raw output file.

### 2.2 Spool
The spool is an append-only stream of terminal output for the conversation.

**Properties**
- Append-only; never truncated during active conversation.
- Normalized line endings (`\n` only).
- Cursor is **byte offset** into the exact spool bytes.

### 2.3 Cursor
A cursor is a byte offset into the spool, used to resume deterministic reads.

**Key cursor name**
- **`resume_cursor`**: the *only* cursor returned by wait/read operations, and the *only* cursor that should be used for subsequent calls.

### 2.4 Prompt Sentinel
An unambiguous output line emitted by the shell whenever it is ready for a new command.

**Format**
- `__FWS_PROMPT__ ts=<ms> cwd_b64=<base64> exit=<rc?>`

**Notes**
- `exit=<rc>` is strongly recommended for interactive block exit codes.
- The sentinel is considered authoritative for "back to prompt" detection.

### 2.5 Mode
Runtime state of the PTY:
- `idle`: safe to start a new block.
- `block_running`: non-interactive block in progress.
- `interactive`: interactive session in progress.

---

## 3) Non-Negotiable Invariants

These invariants are hard constraints. Any behavior that violates them must be considered a bug.

### 3.1 Single Canonical Cursor
- **Invariant**: All output-waiting or output-reading APIs **return and accept only** `resume_cursor`.
- **Rationale**: Having multiple cursor fields (e.g., `next_cursor`) creates ambiguity and mis-resumption bugs.
- **Example**: `wait_for(...)->{resume_cursor}` is the only cursor used for next wait.

### 3.2 Monotonic Cursor Movement
- **Invariant**: `resume_cursor` must be **monotonic non-decreasing** for any single conversation.
- **Rationale**: Guarantees resumability and prevents re-reading earlier output.

### 3.3 Deterministic `wait_for`
- **Invariant**: `wait_for` must never skip matches that exist in the scanned spool range.
- **Rationale**: Skipped matches cause deadlocks and invisible output.
- **Implication**: `resume_cursor` must be **`match_span.end`** on match.

### 3.4 Prompt Sentinel Authority
- **Invariant**: The prompt sentinel is the authoritative signal that the shell is ready for a new command.
- **Rationale**: Avoids guessing based on output content or timing.
- **Requirement**: `wait_for(match_type="prompt")` is deterministic.

### 3.5 Exactly-One Block Lifecycle
- **Invariant**: For any block, emit exactly one `BEGIN`, zero or more `DELTA`, and exactly one `END`.
- **Rationale**: Transcript integrity and replay reliability.

### 3.6 No Mixed Modes
- **Invariant**: `exec_block` MUST be rejected while `mode=interactive`.
- **Invariant**: While `mode=block_running`, any new exec MUST be rejected (or queued, but one mode must be chosen and enforced).
- **Rationale**: Prevents interleaving output and corrupting interactive sessions.

### 3.7 No Fallbacks
- **Invariant**: Responses MUST NOT include deprecated fields (no `next_cursor`, no silent compatibility fields).
- **Rationale**: Eliminates ambiguity and enforces correctness at the client boundary.

### 3.8 Idempotent Prompt Finalization
- **Invariant**: When a prompt sentinel is observed, the session **must** transition to `idle` exactly once.
- **Rationale**: Prevents stuck interactive state and redundant END events.

### 3.9 Output Spool Integrity
- **Invariant**: The spool is append-only and represents the canonical byte stream for cursors.
- **Rationale**: Enables deterministic scanning and replay.

---

## 4) Required Fixes (Mapped to Invariants)

Each fix below is required to satisfy one or more invariants.

### 4.1 `exec_interactive` must return `resume_cursor`
- **Issue**: returning `cursor=0` causes re-matching old output.
- **Fix**: after ensuring spool initialization, return `resume_cursor = self._spool_size`.
- **Invariants**: 3.1, 3.2, 3.3.

### 4.2 `wait_for` returns only `resume_cursor`
- **Issue**: legacy `next_cursor` is ambiguous and can skip matches.
- **Fix**: remove `next_cursor` from `wait_for` responses; return `resume_cursor` only.
- **Invariants**: 3.1, 3.3, 3.7.

### 4.3 `wait_for` timeout returns `resume_cursor = spool_size`
- **Issue**: `read_spool(0, 0)` returns `0`, breaking resume logic.
- **Fix**: use internal spool size directly.
- **Invariants**: 3.2, 3.3.

### 4.4 Prompt match triggers session finalization
- **Issue**: `wait_for(prompt)` can match before `_handle_prompt` flips mode.
- **Fix**: when `match_type="prompt"` matches, call the same finalization logic used by `_handle_prompt` (guarded so it is idempotent).
- **Invariants**: 3.4, 3.8.

### 4.5 Prompt sentinel should include exit code
- **Issue**: interactive blocks lack reliable `exit_code`.
- **Fix**: emit `exit=<rc>` in sentinel line and parse it; set `exit_code` on block end.
- **Invariants**: 3.5, 3.8.

### 4.6 Add atomic `expect_send`
- **Issue**: multi-step interactive flows currently require manual wait/send with risk of interleaving.
- **Fix**: provide `pty.expect_send(expect, send, from_cursor, timeout)` under a per-PTY lock.
- **Invariants**: 3.6.

### 4.7 Add `wait_prompt`
- **Issue**: callers must hand-roll prompt detection and mode checks.
- **Fix**: provide `pty.wait_prompt(from_cursor, timeout)` that returns only after prompt sentinel AND mode flips to `idle`.
- **Invariants**: 3.4, 3.8.

---

## 5) API Contract (No Fallbacks)

### 5.1 `pty.exec_block`
**Request**
- `conversation_id`, `cmd`, `cwd?`

**Response**
- `{ ok, block_id, seq, ts }`

**Errors**
- if `mode=interactive` or `mode=block_running`: `{ ok: false, error: "busy" }`

### 5.2 `pty.exec_interactive`
**Request**
- `conversation_id`, `cmd`, `cwd?`

**Response**
- `{ ok, session_id, block_id, ts_begin, resume_cursor }`

### 5.3 `pty.wait_for`
**Request**
- `conversation_id`, `match`, `match_type`, `from_cursor`, `timeout_ms`

**Response on match**
- `{ ok: true, matched: true, match_text, match_cursor, match_span: {start, end}, resume_cursor, extra? }`

**Response on timeout**
- `{ ok: false, matched: false, error: "timeout", resume_cursor }`

**No other cursor fields permitted.**

### 5.4 `pty.read_spool`
**Request**
- `conversation_id`, `from_cursor`, `max_bytes`

**Response**
- `{ ok, data, cursor, resume_cursor }`

### 5.5 `pty.status`
**Response**
- `{ ok, mode, active_session_id?, active_block_id?, shell_id?, resume_cursor }`

### 5.6 `pty.send`
**Request**
- `conversation_id`, `data`

**Response**
- `{ ok }`

---

## 6) Concurrency and Locking

### 6.1 Per-PTY Lock
All operations that can alter PTY state (exec, send, expect_send, end_session) must acquire a per-PTY lock to prevent interleaving.

### 6.2 Atomic Expect+Send
`expect_send` must be atomic:
- The wait completes.
- The send is immediately issued before any other writes.
- This prevents race conditions when multiple agents or tasks operate.

### 6.3 Re-entrancy Rules
- If `mode=interactive`, **no block exec** is permitted.
- If `mode=block_running`, reject new execs.
- If needed, expose a `queue` mode explicitly, but it must be opt-in and clearly documented.

---

## 7) Failure Recovery

### 7.1 End Session
`pty.end_session` should:
- attempt graceful exit (Ctrl+C) first.
- transition session to `idle` if prompt sentinel appears.
- support a forced reset if the PTY wedges (optional but recommended).

### 7.2 Reset
If a reset is required:
- terminate the PTY and spawn a new shell.
- emit a "session reset" event to the transcript so the agent can explain.
- do **not** silently drop history; the old spool and blocks remain on disk.

---

## 8) Testing and Acceptance Criteria

Each test below must pass for the model to be considered stable.

### 8.1 Cursor Semantics (Block Mode)
Steps:
1. `exec_block("printf \"hello\\nworld\\n\"")`
2. `wait_for("hello", from_cursor=0)`
3. `wait_for("world", from_cursor=resume_cursor)`

Pass if:
- `wait_for` response contains `resume_cursor` and **no `next_cursor`**.
- `world` is found with the second wait.
- timeout returns `resume_cursor == spool_size`.

### 8.2 Interactive Correct Guess
Steps:
1. `exec_interactive("./scratch/guess/guess")`
2. `wait_for("Guess a number", from_cursor=resume_cursor)`
3. `send("7\\r")`
4. `wait_for("Correct!", from_cursor=resume_cursor)`
5. `wait_prompt(from_cursor=resume_cursor)`

Pass if:
- prompt sentinel is observed.
- session ends (`mode=idle`) deterministically.
- block is completed in `blocks.jsonl`.

### 8.3 Interactive Error Path
Steps:
1. `exec_interactive("./scratch/guess/guess")`
2. `wait_for("Guess a number", from_cursor=resume_cursor)`
3. `send("11\\r")`
4. `wait_for("Out of range", from_cursor=resume_cursor)`
5. `wait_prompt(from_cursor=resume_cursor)`

Pass if:
- output includes error line.
- block is completed and `exit_code` is set (if exit is exposed).

### 8.4 Safety Gate
Steps:
1. `exec_interactive("./scratch/guess/guess")`
2. call `exec_block("echo SHOULD_FAIL")`

Pass if:
- `exec_block` is rejected with a clear "interactive mode" error.

### 8.5 Prompt Sentinel Consistency
Steps:
1. Execute multiple commands in a row.
2. Verify prompt sentinel emitted after each command.

Pass if:
- prompt sentinel is always present and parseable.
- `cwd` and `ts` fields parse without errors.

---

## 9) Open Questions / Future Extensions

These are optional, but should not compromise the invariants above.

1. **Multi-client isolation**: per `(thread_id, client_id)` spools and block stores.
2. **TUI snapshots**: optional snapshot capture for alternate screen programs.
3. **Structured input logs**: record agent `send` events as input lines in the transcript.
4. **Backpressure**: configurable spool chunk sizes to avoid memory spikes.
5. **Session recovery**: ability to reattach to dtach-backed interactive sessions after restart.

---

## 10) Summary

The "superior data model" depends on *clarity* and *determinism*. The system must expose **one canonical cursor**, **one authoritative prompt sentinel**, and **one disciplined block lifecycle**, with **zero legacy fallbacks**. If these invariants hold, the model is strictly better than one-shot exec tools: it enables safe interactive control, reproducible reads, and a queryable transcript without context flooding.

---

## 11) Mapping Appendix (Implementation Touchpoints)

This appendix maps the spec to concrete locations in the codebase so changes can be made precisely and audited quickly.

### 11.1 Core Files
- `mcp_agent_pty_server.py`: PTY state machine, spool, waiters, MCP tools, prompt parsing.
- `server.py`: HTTP bridge endpoints, websocket streaming, conversation config and lifecycle.
- `shellspec/mcp_agent_pty.yaml`: service definition for the MCP PTY server.

### 11.2 Invariants → Functions/Sections

**Single Canonical Cursor (3.1)**
- `mcp_agent_pty_server.py`
  - `ConversationState.wait_for(...)`
  - `ConversationState.read_spool(...)`
  - MCP tool wrappers:
    - `pty.wait_for`
    - `pty.read_spool`
    - `pty.exec_interactive` (return shape)

**Monotonic Cursor (3.2)**
- `mcp_agent_pty_server.py`
  - `ConversationState._append_spool(...)` (cursor growth)
  - `ConversationState.read_spool(...)` (cursor arithmetic)
  - `ConversationState.wait_for(...)` (resume_cursor derivation)

**Deterministic `wait_for` (3.3)**
- `mcp_agent_pty_server.py`
  - `ConversationState.wait_for(...)`
  - `ConversationState._check_waiters(...)` (match_span + resume_cursor)

**Prompt Sentinel Authority (3.4)**
- `mcp_agent_pty_server.py`
  - Prompt emission helpers (`_write_rcfile` / prompt hooks)
  - `_handle_prompt(...)`
  - `wait_for(match_type="prompt")`

**Exactly-One Block Lifecycle (3.5)**
- `mcp_agent_pty_server.py`
  - `_handle_begin(...)`
  - `_handle_end(...)`
  - `_handle_prompt(...)` (interactive end)
  - `_append_event(...)` (agent_block_* events)

**No Mixed Modes (3.6)**
- `mcp_agent_pty_server.py`
  - MCP tools: `pty.exec`, `pty.exec_interactive`, `pty.send`
  - `ConversationState._mode` transitions

**No Fallbacks (3.7)**
- `mcp_agent_pty_server.py`
  - MCP tool response shapes (remove `next_cursor` from `wait_for`)
  - Docstrings for tool return types

**Idempotent Prompt Finalization (3.8)**
- `mcp_agent_pty_server.py`
  - `_handle_prompt(...)`
  - `wait_for(match_type="prompt")` finalization hook

**Spool Integrity (3.9)**
- `mcp_agent_pty_server.py`
  - `_init_spool(...)`
  - `_append_spool(...)`
  - `read_spool(...)`

### 11.3 API Contract → MCP Tools

**`pty.exec_block`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.exec")`
- Implementation: `ConversationState.exec(...)`

**`pty.exec_interactive`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.exec_interactive")`
- Implementation: `ConversationState.exec_interactive(...)`

**`pty.wait_for`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.wait_for")`
- Implementation: `ConversationState.wait_for(...)`

**`pty.read_spool`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.read_spool")`
- Implementation: `ConversationState.read_spool(...)`

**`pty.send`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.send")`
- Implementation: `ConversationState.send_stdin(...)`

**`pty.status`**
- File: `mcp_agent_pty_server.py`
- Tool: `@mcp.tool(name="pty.status")`
- Implementation: `ConversationState.get_status(...)`

**Blocks API**
- File: `mcp_agent_pty_server.py`
- Tools: `blocks.since`, `blocks.get`, `blocks.read`, `blocks.search`
- Storage: `~/.cache/app_server/conversations/<id>/agent_pty/blocks.jsonl`

### 11.4 HTTP Bridge → MCP

These are optional convenience endpoints; they should not change MCP semantics.

- `server.py`
  - `/api/mcp/agent-pty/start` → `_get_or_start_mcp_shell()`
  - `/api/mcp/agent-pty/stop` → `_stop_mcp_shell()`
  - `/api/mcp/agent-pty/status` → reads `mcp_shell_id`
  - `/api/mcp/agent-pty/exec` → calls `mcp_agent_pty_server.pty_exec(...)`

### 11.5 Prompt Sentinel Implementation

- `mcp_agent_pty_server.py`
  - RC file writer: `_write_rcfile(...)`
  - Prompt hook: `PROMPT_COMMAND="__fws_*"` functions
  - Sentinel constant: `_MARKER_PROMPT = "__FWS_PROMPT__"`
  - Prompt handling: `_handle_prompt(...)`

### 11.6 Storage Layout (Authoritative)

- Root: `~/.cache/app_server/conversations/<conversation_id>/agent_pty/`
  - `events.jsonl` (agent_block_* stream)
  - `blocks.jsonl` (block metadata)
  - `output.spool` (canonical byte stream)
  - `blocks/<block_id>.out` (raw block output)
  - `.transcript_offset` (consumer offset)

### 11.7 Suggested Implementation Checklist

When applying fixes, follow this order:
1. Remove `next_cursor` from `wait_for` responses; add `resume_cursor`.
2. Fix timeout path to use `self._spool_size`.
3. Update `exec_interactive` return to include `resume_cursor`.
4. Add prompt-triggered finalization for `wait_for(match_type="prompt")`.
5. Add exit code into prompt sentinel and parse it.
6. Add `wait_prompt` + `expect_send` (if in scope).

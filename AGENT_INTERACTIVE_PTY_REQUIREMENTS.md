# Agent Interactive PTY: Requirements (Midterm)

This document specifies what we need to make the **agent-owned per-conversation PTY** truly interactive and *awaitable* while preserving the **block model** (Warp-like “command blocks”).

Focus: agent workflows that involve unknown/unpredictable prompts (debugging, installers, REPLs, TUIs) where the agent must:
1) run something, 2) wait for a condition in output, 3) send input, 4) continue.

## Current State (Today)

- Agent PTY exists per conversation (`dtach` via `framework_shells`).
- Commands are executed with a wrapper that produces one block per submitted command.
- Output is streamed as `agent_block_delta` and stored in:
  - `~/.cache/app_server/conversations/<id>/agent_pty/events.jsonl`
  - `~/.cache/app_server/conversations/<id>/agent_pty/blocks.jsonl`
  - `~/.cache/app_server/conversations/<id>/agent_pty/blocks/<seq>_<ts>.out`

Limitation: interactive foreground programs cannot be safely driven via “wrapped exec” alone.

## Goal

Provide a **stateful, awaitable PTY interface** for the agent that is superior to one-shot “run a command and get stdout” tools:
- deterministic “wait until X appears”
- robust prompt detection
- safe raw input injection (without corrupting the running program)
- transcriptable blocks suitable for replay/search

## Core Concept: Sessions + Awaitables

Interactive behavior needs an explicit notion of “we are inside an interactive session” and a primitive to await output conditions without sleeps.

### Session Types

1) **Block session** (default)
- Created by `exec_block(...)`
- Assumes the command will complete and return to prompt
- Produces exactly one block

2) **Interactive session**
- Created by `exec_interactive(...)`
- Output streams until the agent ends the session or the program exits back to prompt
- Input uses `send(...)` (raw bytes)

## Minimum API Surface (MCP + HTTP)

### 1) Start a normal command block

`pty.exec_block(conversation_id, command, cwd?) -> { ok, block_id, seq, ts_begin }`

Requirements:
- Exactly one block per invocation (compound commands stay one block).
- Must not require sleeps/timeouts to decide “done”.

### 2) Start an interactive session

`pty.exec_interactive(conversation_id, command, cwd?) -> { ok, session_id, block_id, ts_begin }`

Semantics:
- Launches the command and immediately switches the PTY into “interactive mode”.
- Begins a block (a “session block”) whose deltas contain the entire interactive transcript until exit.
- The session ends when:
  - the program returns to a known prompt sentinel, or
  - the agent explicitly ends/cancels it.

### 3) Send raw bytes (stdin)

`pty.send(conversation_id, session_id?, data) -> { ok }`

Requirements:
- Writes bytes directly to the PTY (no wrappers, no markers).
- Supports:
  - text
  - newline (choose canonical: `\r` is typical in terminals)
  - control chars (Ctrl+C = `\x03`, Ctrl+D = `\x04`, Esc = `\x1b`)
  - escape sequences for arrow/function keys (optional midterm, but don’t block on it)

Convenience:
- `pty.ctrl_c(...)`, `pty.ctrl_d(...)`, `pty.enter(...)`

### 4) Await output condition (no sleeps)

`pty.wait_for(conversation_id, session_id?, match, *, from_cursor?, timeout_ms=30000, max_bytes?) -> { ok, matched, match_text?, cursor }`

Where:
- `match` supports:
  - substring
  - regex (flagged explicitly)
  - special tokens: `PROMPT` (prompt sentinel), `EOF` (session ended)
- `from_cursor` is a monotonic cursor into the output stream/spool so waits are deterministic and resumable.

Implementation options:
- **A.** Wait on the live subscriber stream (best for responsiveness).
- **B.** Wait by tailing the spooled output file (best for determinism, works if stream missed).
- Midterm can combine: subscribe first; if reconnect, backfill from spool using cursor.

### 5) Query session status

`pty.status(conversation_id) -> { ok, mode, active_session_id?, active_block_id?, shell_id?, pid? }`

Modes:
- `idle` (safe to exec)
- `block_running` (block in progress)
- `interactive` (agent must use `send` + `wait_for`)

### 6) End / cancel

`pty.end_session(conversation_id, session_id) -> { ok }`

Rules:
- If interactive program is running:
  - try graceful end by sending `exit\r` only if we know it’s a shell/REPL
  - otherwise prefer `Ctrl+C`, then optionally “hard reset” as last resort

Optional:
- `pty.reset(conversation_id)` to kill and respawn the PTY if it gets wedged.

## Prompt Sentinel (Required)

Interactive correctness depends on knowing when we’re “back at a prompt”.

Requirement:
- The PTY must emit an unambiguous marker whenever the shell is ready for a new command.

Example:
- On prompt display, print a line like:
  - `__FWS_PROMPT__ ts=<ms> cwd_b64=<...>`

Then:
- `wait_for(PROMPT)` becomes reliable.
- We can end interactive sessions deterministically.

## Block Semantics

### For `exec_block`
- Block is bounded by BEGIN/END markers emitted by the wrapper.

### For `exec_interactive`
- Create a **single “session block”**:
  - `block.cmd` is the launch command
  - `agent_block_delta` contains raw terminal output until prompt sentinel observed
  - `agent_block_end` emitted when prompt sentinel observed or session canceled

We do *not* attempt to store per-keystroke input as separate transcript lines midterm.

## Cursor Model for Awaitables

We need a cursor the agent can store and reuse:
- Cursor is a byte offset into a canonical output stream.

Recommendation:
- Normalize output to `\n` for storage.
- Expose cursor in terms of bytes in the normalized stream (or raw bytes + explicit encoding).

The agent workflow then becomes:
1) `cursor = last_cursor`
2) `wait_for(match, from_cursor=cursor) -> {cursor: next}`
3) `send(...)`
4) repeat

## Safety Rules (Agent Side)

To avoid corrupting interactive programs:
- While `mode === interactive`, forbid `exec_block` (return a clear error).
- While `mode === block_running`, either:
  - queue subsequent `exec_block` calls, or
  - reject with “busy” (simpler midterm).

## Example Flow: Interactive Number Guessing

1) `exec_interactive("./guess") -> session_id, block_id`
2) `wait_for("Guess a number", from_cursor=0)`
3) `send("7\r")`
4) `wait_for("Correct!", from_cursor=<prev>)`
5) `wait_for(PROMPT, from_cursor=<prev>)` → session ends / block ends

## Implementation Checklist (Midterm)

1) Add raw `pty.send` (MCP tool + HTTP endpoint).
2) Add `pty.wait_for` with cursor support.
3) Add prompt sentinel emission (`__FWS_PROMPT__`) from the shell rcfile.
4) Add mode/session tracking in the agent PTY server:
   - active session id
   - active block id
   - transitions on BEGIN/END + PROMPT
5) UI: optional “agent interactive” affordances later; not required for the core backend capability.


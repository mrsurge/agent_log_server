Here’s the high-level conceptual draft that stitches together **xterm.js + MCP + framework-shells (fws) + your Codex Agent Server extension** into a “stateful PTY + block history + agent-safe access” system.

---

## 0) The goal

* The **user** gets a normal, stateful terminal session (one PTY per conversation/thread).
* Every command becomes a **Block** (aka “card”) with:

  * `cmd / cwd / exit_code / timestamps`
  * **first-order telemetry** (`output_lines`, `output_bytes`, `was_clipped_for_ui`, etc.)
  * full output stored safely (not shoved into agent context)
* The **agent** reads blocks via **MCP tool calls** and can:

  * pull just `head/tail/ranges/search` of output
  * follow live output with a cursor
  * run its own commands in the same PTY (or a separate agent PTY) without truncation disasters.

---

## 1) The pieces and the boundaries

### A) `framework-shells` (fws): the PTY substrate

Responsibilities:

* Spawn the PTY shell: `spawn_shell_pty(["bash","-l","-i"], cwd=...)`
* Provide I/O primitives:

  * `subscribe_output(shell_id)` → async queue of bytes/chunks
  * `write_to_pty(shell_id, data)`
  * `resize_pty(shell_id, cols, rows)`
* Provide runtime storage namespace (repo fingerprint + runtime secret) and log paths.

**Boundary:** fws is purely “process + stream.” It should not care about “blocks” or agents.

---

### B) Block Assembler (new): turns raw PTY into Blocks

This sits *on top of* fws output subscription and is your “Warp layer.”

Responsibilities:

* Parse the PTY stream
* Detect **block boundaries**
* Spool output to disk per block
* Emit block events to the UI
* Expose block store to MCP tools

**Boundary:** Block Assembler is the *only* component that interprets terminal output as structured data.

---

### C) xterm.js UI: cards + live terminal

Responsibilities:

* Render “Block cards” for commands
* For the current/interactive block, attach a live xterm instance
* For completed blocks, freeze into a static transcript view
* Never truncate the “truth”: truncation is UI-only; the block store keeps full output.

**Boundary:** UI renders what the backend says; it should not be the system of record.

---

### D) MCP Server (new): agent-facing API

Responsibilities:

* Expose “blocks” and “pty” as tool calls:

  * `blocks.since(cursor)`, `blocks.get(block_id)`, `blocks.read(range)`, `blocks.search(query)`
  * `pty.exec(...)` (agent-run command) and/or `pty.stream.since(cursor)`
* Enforce policy: rate limits, allow/deny rules, “agent can’t steal stdin,” etc.

**Boundary:** MCP is the only agent entry point to terminal history + execution.

---

### E) Codex Agent Server extension (your FastAPI/SocketIO bridge)

You already have:

* SSOT sidecar + internal transcript
* codex-app-server JSON-RPC bridge (stdin/stdout)
* frontend websocket event fanout

New responsibilities with this design:

* Treat the PTY + block system as a first-class “tool surface”
* During/after a user turn, allow the codex-driven agent to call MCP tools to inspect the terminal session safely
* When the agent wants to run terminal commands, route them through MCP → PTY blocks (instead of “dumb exec with truncated stdout”).

**Boundary:** Codex server coordinates conversation + approvals and delegates terminal reality to MCP/fws.

---

## 2) How Blocks are extracted from a stateful PTY

You need a deterministic way to know when a command starts/ends inside an interactive shell.

### Block boundary strategy: shell integration markers

When you spawn the shell via fws, inject a tiny hook file (bash/zsh) that prints **BEGIN/END markers** into the PTY stream:

* `FWS_BLOCK_BEGIN … (cmd, cwd, timestamp, seq)`
* `FWS_BLOCK_END … (exit_code, timestamp, seq)`

Then the Block Assembler does:

1. **subscribe_output(shell_id)** from fws

2. Feed every chunk into:

   * the UI stream (live xterm)
   * the parser (block boundary detection)
   * the spooling writer (raw output capture)

3. On `BLOCK_BEGIN`:

   * create `block_id`
   * open `blocks/<block_id>.out` spool file
   * reset counters: `output_bytes = 0`, `output_lines = 0`

4. For every subsequent output chunk:

   * append to spool file
   * increment counters (bytes, newlines)
   * stream to UI for the active card

5. On `BLOCK_END`:

   * finalize metadata (exit code, duration, total lines/bytes)
   * write one JSON record to `blocks.jsonl`
   * emit “block completed” event to UI + transcript

That’s how you get **first-order telemetry** without extra commands.

---

## 3) How blocks are served to the agent after the user’s turn

Define “user’s turn” as: **a block completes with `actor=user`**.

When that happens:

1. Block Assembler finalizes and persists the block:

   * `blocks.jsonl` (metadata)
   * `blocks/<id>.out` (full output)
   * optional `blocks/<id>.preview` (head/tail cached)

2. Codex Agent Server records a clean transcript item:

   * `type: "pty_block_completed"`
   * `block_id`, `cmd`, `cwd`, `exit_code`, `output_lines`, `output_bytes`, `preview_head/tail`

3. The agent then learns about it in a controlled way:

   * Either you include the **block summary** in the conversation context as the “last terminal event”
   * Or the agent uses MCP tools immediately:

     * `blocks.since(cursor)` → sees summaries
     * only pulls slices as needed:

       * `blocks.read(block_id, from_line, to_line)`
       * `blocks.search(block_id, "Traceback")`

Crucial: the agent **does not** automatically ingest the entire `3000 lines`. It sees:

* “output_lines: 3000” and a small preview,
  then decides the minimal next read.

---

## 4) How the agent utilizes the PTY (without messing with the user)

Two clean modes:

### Mode 1: Read + advise (agent never runs terminal commands)

* Agent reads blocks + snapshots
* Agent suggests commands to the user (copy/paste or “approve to run”)

Tools used:

* `blocks.since`, `blocks.get`, `blocks.search`, `pty.snapshot`, `pty.stream.since`

### Mode 2: Agent-run commands in the same PTY (serialized, safe)

* Agent can request command execution through MCP:

  * `pty.exec({shell_id, cmd, cwd?})`

Implementation detail:

* `pty.exec` does **not** “type alongside the user.”
* It waits until the session is “idle” (no active block), then sends the command, and the block system captures output.

Agent loop becomes:

1. `pty.exec(...)` → returns `{pending_block_id}` quickly
2. Agent polls:

   * `blocks.get(block_id)` until `status=completed`
   * or follows live:

     * `pty.stream.since(cursor)` for incremental chunks
3. If output is huge, agent reads only slices from the block output store.

This gives Warp-like capability without agent competing for stdin.

---

## 5) UI “cards” behavior (xterm.js + blocks)

### Non-interactive block card

* Render streamed output as text (or a lightweight terminal renderer)
* When completed, freeze and stop streaming

### Interactive block card (“sticks”)

If the shell enters alt-screen / heavy cursor control:

* Attach an **ephemeral xterm.js instance** to that card
* Keep it live until `BLOCK_END`
* On completion:

  * snapshot the buffer into stored output (viewport or serialize dump)
  * dispose xterm instance
  * card becomes static

The block store remains the authoritative record.

---

## 6) The resulting system invariants

* **One PTY per thread** gives the user state “for free” (cwd/env/venv/session continuity).
* **Blocks are authoritative**: command + output becomes structured data + telemetry.
* **Agents never drown**: they see counts/previews first, and only pull slices.
* **Live output is toolable**: cursor-based streaming + snapshots.
* **Codex layer stays clean**: it coordinates conversation/approvals/transcript; MCP owns terminal truth.

That’s the full architecture you’ve been converging on: terminal output becomes a queryable dataset, the UI becomes card-driven, and the agent gets a safe, structured interface to both history and live execution.

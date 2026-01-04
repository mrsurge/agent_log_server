## Progress

### Done
- Added MCP server `mcp_agent_pty_server.py` (stdio) with tools: `pty.exec`, `blocks.since`, `blocks.get`, `blocks.read`, `blocks.search`.
- Agent PTY is per conversation (dtach-backed) with deterministic block markers; writes `agent_pty/blocks.jsonl`, `agent_pty/blocks/*.out`, `agent_pty/events.jsonl`.
- Server tails `agent_pty/events.jsonl` and fans out `agent_block_*` events to the frontend.
- Frontend renders `agent_block_begin/agent_block_delta/agent_block_end` as terminal-style cards.
- Added xterm.js scaffolding + a Terminal/Chat toggle; in terminal mode, Enter submits on desktop and mobile; output mirrors existing one-off shell exec streaming.

### In Progress
- Long-running user PTY (dtach-backed) + block capture + wiring terminal-mode input to it (replacing one-off shell exec in terminal mode).

### Next
- Add per-conversation user PTY spawn/attach lifecycle in `server.py`.
- Implement user block markers + block spool store (same schema as agent blocks).
- Render user blocks/cards and allow agent to read user blocks (read-only at first).

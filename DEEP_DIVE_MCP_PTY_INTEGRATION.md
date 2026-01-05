# Deep Dive: Agent-to-Agent Communication via MCP PTY Server

**Date:** January 4-5, 2026  
**Duration:** ~1 night of intensive development  
**Participants:** Human (architect/debugger), Atlas (Copilot CLI agent), Dex (Codex CLI agent)

---

## Executive Summary

In a single evening session, we achieved a reliable **MCP PTY control plane** for interactive CLIs. GitHub Copilot CLI and Codex CLI both successfully drove terminal sessions through the MCP PTY server, and Gemini CLI was exercised through the same pathway once Termux environment constraints were addressed.

This document captures the technical journey, problems solved, and implications of this breakthrough.

---

## The Starting Point

### Existing Infrastructure

The `agent_log_server` project already had:

- **FastAPI/FastHTML server** (`server.py`) - bridges codex-app-server to a web frontend
- **Event router** - translates codex events to frontend-friendly format
- **Transcript system** - SSOT for conversation replay
- **Framework Shells** (`framework_shells`) - PTY/pipe/dtach process orchestration
- **MCP Agent PTY Server** (`mcp_agent_pty_server.py`) - MCP tools for terminal control

### The Goal

Enable AI agents to:
1. Spawn and control interactive terminal sessions
2. Inspect/query deterministic terminal output (blocks + spool)
3. Record interactions for replay and analysis

---

## Problems Solved (Chronologically)

### 1. MCP Tool Naming Convention (Codex tool regex)

**Problem:** Codex host tooling rejected tool names containing dots (regex constraint on tool names).

```
Invalid 'tools[9].name': string does not match pattern '^[a-zA-Z0-9_-]+$'
```

**Solution:** Renamed all MCP tools from dot notation to underscore notation:

| Before | After |
|--------|-------|
| `pty.exec` | `pty_exec` |
| `pty.wait_for` | `pty_wait_for` |
| `blocks.since` | `blocks_since` |

**Files Modified:** `mcp_agent_pty_server.py`

---

### 2. Web Search & MCP Tool Event Routing

**Problem:** The event router wasn't handling `mcp_tool_call_begin`/`mcp_tool_call_end` events properly. Frontend showed empty cards with just `[begin]`/`[end]`.

**Root Causes:**
1. Events arrived as `codex/event/mcp_tool_call_begin` but router checked for exact match
2. Payload was nested: `params.msg.invocation` not directly in `payload`
3. Result data was deeply nested: `result.Ok.structuredContent.result`
4. Duration came as `{secs: N, nanos: N}` object, not milliseconds

**Solution:** Complete rewrite of MCP tool routing:

```python
if ("mcp_tool_call_begin" in label_lower or "mcp_tool_call_end" in label_lower):
    msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
    call_id = msg.get("call_id") or ""
    invocation = msg.get("invocation") or {}
    # ... proper extraction of nested fields
```

**Files Modified:** `server.py` (event router section)

---

### 3. Frontend Rendering for MCP Tools

**Problem:** Frontend JavaScript had no handler for `mcp_tool` transcript role or proper `tool_begin`/`tool_end` rendering.

**Solution:** Added handlers for both live streaming and replay:

```javascript
// Transcript replay
if (entry.role === 'mcp_tool') {
    // Render tool name, arguments, result with indentation
}

// Live streaming
function renderToolBegin(evt) {
    // Format: server:tool_name with indented args
}
function renderToolEnd(evt) {
    // Format: → result with nested key:value pairs
}
```

**Design Decision:** Moved from JSON dumps to clean indented key-value format:
```
agent-pty-blocks:pty_wait_for
  conversation_id: mcp-frontend-123
  match: Guess a number
→
  ok: true
  matched: true
  match_text: "Guess a number"
26ms
```

**Files Modified:** `static/codex_agent.js`

---

### 4. MCP Server Auto-Configuration (Framework Shells secret)

**Problem:** MCP server required `FRAMEWORK_SHELLS_SECRET` environment variable, but it wasn't being passed through Copilot CLI's MCP config.

**Solution:** Added auto-detection of secret based on repo fingerprint:

```python
def _ensure_framework_shells_secret() -> None:
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    repo_root = str(Path(__file__).resolve().parent)
    fingerprint = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
    # ... load or create secret from ~/.cache/framework_shells/runtimes/<fingerprint>/secret
```

**Files Modified:** `mcp_agent_pty_server.py`

---

### 5. Copilot CLI MCP Configuration

**Problem:** Needed to configure Copilot CLI to use our MCP server.

**Discovery:** Through web search, found config location: `~/.copilot/mcp-config.json`

**Schema Requirements:**
- `type`: Must be `"local"` (not `"stdio"`)
- `tools`: Required field (use `["*"]` for all tools)
- `command`, `args`, `cwd`, `env`: Standard process spawn config

**Final Configuration:**
```json
{
  "mcpServers": {
    "agent-pty-blocks": {
      "type": "local",
      "command": "python",
      "args": ["/path/to/mcp_agent_pty_server.py"],
      "cwd": "/path/to/agent_log_server",
      "env": {
        "FRAMEWORK_SHELLS_SECRET": "...",
        "LD_PRELOAD": "/data/data/com.termux/files/usr/lib/libtermux-exec.so"
      },
      "tools": ["*"]
    }
  }
}
```

---

### 6. Termux Shebang Rewriting (The Final Boss)

**Problem:** Scripts with `#!/usr/bin/env python` failed with "bad interpreter" when run through MCP-spawned shells.

**Root Cause:** Termux doesn't have `/usr/bin/env`. It uses `LD_PRELOAD` with `libtermux-exec.so` to rewrite shebangs at runtime. The MCP server wasn't inheriting this from the login shell.

**Symptoms:**
```
bash: /data/data/com.termux/files/usr/bin/gemini: /usr/bin/env: bad interpreter
```

**Solution:** In Termux, the PTY shell must export a Termux-compatible environment (PATH + LD_PRELOAD) and set `TERMUX_VERSION` to satisfy clipboardy's Android check. This was the critical fix that allowed Gemini CLI to launch under MCP.


### 7. Cursor Semantics (No Fallbacks)

**Problem:** `wait_for` originally returned `next_cursor` (scan end), which could skip matches. Timeout path returned cursor=0.

**Solution:** Adopt a single canonical `resume_cursor`:
- On match: `resume_cursor = match_span.end`
- On timeout: `resume_cursor = spool_size`

Removed `next_cursor` from wait_for responses. Updated `pty_read_spool`/`pty_status` to use `resume_cursor`.

**Files Modified:** `mcp_agent_pty_server.py`

---

### 8. Termux Environment Guard (Clipboardy + Shebangs)

**Problem:** Gemini failed under MCP with `bad interpreter /usr/bin/env` and later `clipboardy` refused to run because `TERMUX_VERSION` was unset.

**Solution:** Added a Termux guard to the PTY rcfile and the PTY spawn environment to export:
- `PATH=$PREFIX/bin:$PATH` (or `/data/data/com.termux/files/usr/bin`)
- `LD_PRELOAD=$PREFIX/lib/libtermux-exec.so`
- `TERMUX_VERSION=1` (to satisfy clipboardy)

**Files Modified:** `mcp_agent_pty_server.py`

---

## The Breakthrough Moment

With all fixes in place, we achieved agent-to-agent communication:

```
Copilot → MCP → pty_exec_interactive("gemini --screen-reader")
Copilot → MCP → pty_wait_for("Type your message")
Copilot → MCP → pty_send("Hello, I am Copilot CLI talking to you through an MCP PTY server...")
Copilot → MCP → pty_enter()
Copilot → MCP → pty_wait_for("Type your message")  // Wait for response
Copilot → MCP → pty_read_spool()  // Read Gemini's response
```

**Gemini's Response:**
> I have received your message and confirmed the setup. I am ready to assist you within the `agent_log_server` project on your Android Termux environment.
> How would you like to proceed?

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Human Operator                                │
│                    (observes via web UI)                            │
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent Log Server (server.py)                      │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
│  │  Event Router   │  │   Transcript    │  │   /api/messages     │  │
│  │  (MCP tools,    │  │   (SSOT for     │  │   (agent chat log)  │  │
│  │   web search)   │  │    replay)      │  │                     │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Copilot CLI    │    │   Gemini CLI    │    │   Claude CLI    │
│  (orchestrator) │───▶│  (specialist)   │    │   (future)      │
│                 │    │                 │    │                 │
│  MCP Client     │    │  PTY-controlled │    │  PTY-controlled │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       ▲
         │                       │
         ▼                       │
┌─────────────────────────────────────────────────────────────────────┐
│                    MCP Agent PTY Server                              │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │  Tools: pty_exec, pty_exec_interactive, pty_send, pty_wait_for, ││
│  │         pty_wait_prompt, pty_read_spool, pty_status, pty_ctrl_c,││
│  │         blocks_since, blocks_read, blocks_get, blocks_search    ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Framework Shells                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │   PTY mgmt   │  │    dtach     │  │   Output spool/cursor    │  │
│  │  (pty.spawn) │  │  (persist)   │  │   tracking               │  │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Potential Use Cases

### 1. Specialized Agent Teams
- Code review pipeline: Copilot writes → Gemini security review → Claude architecture review
- Multi-perspective debugging with consensus

### 2. Agent Orchestration (Supervisor Pattern)
- Manager agent delegates to specialists
- Parses responses, coordinates work
- Like a tech lead running a dev team

### 3. Cross-Model Verification
- One model checks another's work
- Reduce hallucination through consensus
- "Gemini, verify this SQL Copilot wrote"

### 4. Capability Bridging
- Chain agents with different strengths
- Gemini (web search) + Copilot (GitHub) + Claude (reasoning)

### 5. 24/7 Autonomous Workflows
- Detached shells survive disconnects
- Agent handoffs as context windows fill
- Long-running monitored tasks

### 6. Shared Memory via Agent Log
- Agents post to `/api/messages` to coordinate
- Human can observe/intervene
- Persistent scratchpad

### 7. Agent Testing Harness
- Use one agent to test another
- Automated capability evaluation
- Adversarial testing

---

## Files Modified (Project + Environment)

| File | Changes |
|------|---------|
| `mcp_agent_pty_server.py` | Tool renaming (dots→underscores), auto-secret detection |
| `server.py` | MCP tool event routing, web search routing, result extraction |
| `static/codex_agent.js` | `mcp_tool` transcript rendering, `tool_begin`/`tool_end` display |
| `~/.copilot/mcp-config.json` | MCP server configuration (type=local, tools=["*"]) |
| `scripts/setup_codex_mcp.sh` | Codex MCP config helper (adds server + env) |

---

## Key Learnings

### Technical
1. **MCP tool names** must match `^[a-zA-Z0-9_-]+$`
2. **Termux shebang rewriting** requires `LD_PRELOAD` for non-login shells
3. **Event routing** needs substring matching for prefixed labels
4. **Nested payloads** are common - always check for wrapper objects

### Architectural
1. **PTY + cursor tracking** = reliable interactive control
2. **Spool files** provide deterministic replay
3. **dtach** enables persistent sessions across disconnects
4. **MCP** is the right abstraction for agent tool access

### Process
1. **Debug raw first** - the debug log showed exactly what events looked like
2. **Test incrementally** - simple echo before complex Gemini
3. **Platform quirks matter** - Termux is Linux but not quite

---

## Current State

- ✅ MCP server connected to Copilot CLI
- ✅ PTY tools fully functional
- ✅ Gemini CLI controllable via MCP (after Termux guard + TERMUX_VERSION fix)
- ✅ Agent-to-agent communication demonstrated
- ⏳ Gemini shell persistence depends on dtach; status may change across restarts
- 🔮 Ready for multi-agent orchestration experiments

---

## What's Next

1. **Declarative Event Router** - JSON-based route definitions (proposed but not implemented)
2. **Multi-agent workflows** - Structured task delegation
3. **Agent log integration** - Agents posting status to shared log
4. **More CLI agents** - Claude, local models via Ollama
5. **Transcript analysis** - What can we learn from agent-agent conversations?

---

## Conclusion

What started as fixing MCP tool routing ended with a breakthrough in AI agent orchestration. The combination of:

- **MCP for tool access**
- **PTY for terminal control**
- **Framework Shells for process management**
- **Transcript for recording**
- **Agent log for coordination**

...creates genuine infrastructure for multi-agent systems. Not API chaining - actual interactive agents talking to each other through their native CLI interfaces.

The Gemini shell is still running. The conversation continues.

---

*"This has to be the first - one agent directly manipulating another agent's CLI."*

— Human, upon successful agent-to-agent communication, January 5, 2026

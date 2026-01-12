# Meta Envelope Proposal: User Command Context Injection

**Date:** 2026-01-12  
**Author:** Atlas (Copilot CLI)  
**Status:** Draft / Pending Answers

---

## Overview

When a user runs a command via the agent PTY (from `/codex-agent` UI), we want that command's context to become first-class agent context in the transcript—without cluttering the user-facing display.

**Solution:** Buffer command context in `meta.json`, prepend a sentinel-wrapped envelope to the next user message on `turn/start`, then strip the envelope before it hits the internal transcript and frontend rendering.

---

## Architecture

### Message Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER RUNS COMMAND                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  agent PTY executes command → block completes → context buffered in meta    │
│                                                                             │
│  meta.json: { "pending_cmd_context": { v, type, cmd, preview, mcp, ... } }  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         USER SENDS NEXT MESSAGE                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  api_appserver_rpc intercepts turn/start                                    │
│  → reads pending_cmd_context from meta                                      │
│  → prepends envelope to user message text                                   │
│  → clears pending_cmd_context from meta                                     │
│  → forwards to codex-app-server                                             │
│                                                                             │
│  Outgoing message: "\x1e{...json...}\x1f<user message>"                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         codex-app-server                                    │
│  → receives full message (envelope + user text)                             │
│  → agent sees full context including envelope                               │
│  → echoes back item/started with type: "usermessage"                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  _route_appserver_event receives item/started                               │
│  → _extract_item_text strips envelope via _strip_meta_envelope()            │
│  → clean text written to transcript via _append_transcript_entry()          │
│  → clean text broadcast to frontend via events.append()                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Sentinel Design

**Sentinels:**
- **Start:** `\x1eCODEX_META ` (ASCII Record Separator + prefix + space)
- **End:** `\x1f` (ASCII Unit Separator)

**Format:**
```
\x1eCODEX_META <single-line JSON>\x1f<user message text>
```

**Why these sentinels?**
- `\x1e` (Record Separator) is non-printable ASCII, extremely unlikely in user text
- `CODEX_META ` prefix provides additional false-positive guard
- Together they ensure we never accidentally strip legitimate user text
- Simple to detect: check if text starts with `\x1eCODEX_META `

---

## Envelope Schema

The envelope contains an array of commands accumulated since the last user message.

```json
{
  "v": 1,
  "type": "user_cmd_context",
  "conversation_id": "abc123",
  "shell_id": "shell_xyz",
  "total_commands_run": 3,
  "kept": 2,
  "dropped": 1,
  "commands": [
    {
      "cmd": "ls -la",
      "exit_code": 0,
      "cwd": "/home/user",
      "block_id": "block_001",
      "ts": 1736654400000,
      "preview": {
        "lines": ["total 48", "drwxr-xr-x  5 user user 4096 Jan 12 04:00 ."],
        "truncated": false
      }
    },
    {
      "cmd": "pwd",
      "exit_code": 0,
      "cwd": "/home/user",
      "block_id": "block_002",
      "ts": 1736654410000,
      "preview": {
        "lines": ["/home/user"],
        "truncated": false
      }
    }
  ],
  "mcp": ["pty_read_screen", "pty_read_scrollback"]
}
```

**Top-level fields:**
| Field | Type | Description |
|-------|------|-------------|
| `v` | `int` | Schema version (1) |
| `type` | `string` | Always `"user_cmd_context"` |
| `conversation_id` | `string` | Conversation ID for MCP calls |
| `shell_id` | `string` | Shell ID for MCP calls |
| `total_commands_run` | `int` | Total commands run since last message (including dropped) |
| `kept` | `int` | Number of commands included in envelope |
| `dropped` | `int` | Number of oldest commands dropped due to cap |
| `commands` | `array` | Array of command entries (up to N=10) |
| `mcp` | `string[]` | Suggested MCP tool names for full context |

**Per-command fields:**
| Field | Type | Description |
|-------|------|-------------|
| `cmd` | `string` | Command that was executed |
| `exit_code` | `int\|null` | Command exit code |
| `cwd` | `string` | Working directory |
| `block_id` | `string` | Block ID for referencing artifacts |
| `ts` | `int` | Timestamp (ms since epoch) |
| `preview` | `object` | Bounded tail preview |
| `preview.lines` | `string[]` | Last N lines of output (10-20) |
| `preview.truncated` | `bool` | True if output was truncated |

**Bounding rules:**
- Max commands: 10 (oldest dropped first)
- Per-command preview: up to 20 lines
- Per-command preview bytes: up to 3KB
- If `dropped > 0`, agent knows some history was lost

---

## Implementation

### 1. Buffer pending command context (accumulating)

**Location:** `_tail_agent_pty_events_to_transcript()` in `server.py`, on `agent_block_end` events.

```python
# Constants
_CMD_BUFFER_MAX_ENTRIES = 10
_CMD_PREVIEW_MAX_LINES = 20
_CMD_PREVIEW_MAX_BYTES = 3000


def _get_shell_id(conversation_id: str) -> Optional[str]:
    """Read shell_id from persisted file."""
    path = _conversation_dir(conversation_id) / "agent_pty" / "shell_id.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


async def _build_cmd_preview(conversation_id: str) -> dict:
    """Build bounded tail preview from scrollback/screen snapshot.
    
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    lines: List[str] = []
    truncated = False
    
    def _read_snapshots() -> List[str]:
        """Sync helper to read snapshot files."""
        result = []
        
        # Try scrollback snapshot first (more complete history)
        scrollback_path = _conversation_dir(conversation_id) / "agent_pty" / "scrollback.snapshot.json"
        if scrollback_path.exists():
            try:
                data = json.loads(scrollback_path.read_text(encoding="utf-8"))
                result = data.get("lines", [])[-_CMD_PREVIEW_MAX_LINES:]
                if result:
                    return result
            except Exception:
                pass
        
        # Fallback to screen snapshot
        screen_path = _conversation_dir(conversation_id) / "agent_pty" / "screen.snapshot.json"
        if screen_path.exists():
            try:
                data = json.loads(screen_path.read_text(encoding="utf-8"))
                rows = data.get("rows", [])
                # Filter empty rows from bottom
                while rows and not rows[-1].strip():
                    rows.pop()
                result = rows[-_CMD_PREVIEW_MAX_LINES:]
            except Exception:
                pass
        
        return result
    
    lines = await asyncio.to_thread(_read_snapshots)
    
    # Apply byte cap
    total_bytes = 0
    capped_lines = []
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8"))
        if total_bytes + line_bytes > _CMD_PREVIEW_MAX_BYTES:
            truncated = True
            break
        capped_lines.insert(0, line)
        total_bytes += line_bytes
    
    if len(capped_lines) < len(lines):
        truncated = True
    
    return {"lines": capped_lines, "truncated": truncated}


async def _buffer_cmd_context(conversation_id: str, block: dict) -> None:
    """Append command context to buffer for next user message.
    
    Called on agent_block_end events. Accumulates up to N commands.
    """
    cmd = block.get("cmd", "")
    exit_code = block.get("exit_code")
    cwd = block.get("cwd", "")
    block_id = block.get("block_id", "")
    ts = block.get("ts_end") or block.get("ts_begin") or int(datetime.now(timezone.utc).timestamp() * 1000)
    
    shell_id = _get_shell_id(conversation_id)
    preview = await _build_cmd_preview(conversation_id)
    
    entry = {
        "cmd": cmd,
        "exit_code": exit_code,
        "cwd": cwd,
        "block_id": block_id,
        "ts": ts,
        "preview": preview,
    }
    
    meta = _load_conversation_meta(conversation_id)
    buffer = meta.get("pending_cmd_buffer", {})
    
    # Initialize or update buffer
    if "commands" not in buffer:
        buffer = {
            "v": 1,
            "shell_id": shell_id,
            "conversation_id": conversation_id,
            "commands": [],
            "total_commands_run": 0,
        }
    
    buffer["total_commands_run"] = buffer.get("total_commands_run", 0) + 1
    buffer["commands"].append(entry)
    
    # Cap at max entries (drop oldest)
    if len(buffer["commands"]) > _CMD_BUFFER_MAX_ENTRIES:
        buffer["commands"] = buffer["commands"][-_CMD_BUFFER_MAX_ENTRIES:]
    
    # Update shell_id if changed
    if shell_id:
        buffer["shell_id"] = shell_id
    
    meta["pending_cmd_buffer"] = buffer
    _save_conversation_meta(conversation_id, meta)
```

### 2. Hook into event processing

**Location:** `_tail_agent_pty_events_to_transcript()` in `server.py`

Add call to `_buffer_cmd_context()` when processing `agent_block_end`:

```python
async def _tail_agent_pty_events_to_transcript(conversation_id: str, *, max_lines_per_tick: int = 50) -> None:
    # ... existing code ...
    
    for line in lines:
        try:
            evt = json.loads(line.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        etype = evt.get("type")
        if etype not in {"agent_block_begin", "agent_block_delta", "agent_block_end"}:
            continue
        
        # NEW: Buffer command context on block end (async)
        if etype == "agent_block_end":
            block = evt.get("block")
            if isinstance(block, dict):
                await _buffer_cmd_context(conversation_id, block)
        
        # ... rest of existing transcript writing ...
```

### 3. Inject envelope on outgoing turn/start

**Location:** `api_appserver_rpc()` in `server.py`

```python
# Sentinel constants
_META_ENVELOPE_START = "\x1eCODEX_META "  # RS + prefix for false-positive guard
_META_ENVELOPE_END = "\x1f"               # US


def _build_envelope_from_buffer(buffer: dict) -> str:
    """Build envelope JSON from command buffer.
    
    Returns the envelope payload (without sentinels).
    """
    total = buffer.get("total_commands_run", len(buffer.get("commands", [])))
    kept = len(buffer.get("commands", []))
    dropped = total - kept
    
    envelope = {
        "v": 1,
        "type": "user_cmd_context",
        "conversation_id": buffer.get("conversation_id"),
        "shell_id": buffer.get("shell_id"),
        "total_commands_run": total,
        "kept": kept,
        "dropped": dropped,
        "commands": buffer.get("commands", []),
        "mcp": ["pty_read_screen", "pty_read_scrollback"],
    }
    return json.dumps(envelope, ensure_ascii=False)


@app.post("/api/appserver/rpc")
async def api_appserver_rpc(payload: Dict[str, Any] = Body(...)):
    # ... existing code ...
    
    method = payload.get("method", "")
    
    # ... existing SSOT injection for settings ...
    
    # NEW: Inject pending command context envelope on turn/start
    if method == "turn/start" and convo_id:
        meta = _load_conversation_meta(convo_id)
        buffer = meta.pop("pending_cmd_buffer", None)
        if buffer and buffer.get("commands"):
            _save_conversation_meta(convo_id, meta)  # Clear buffer
            
            # Build envelope
            envelope_json = _build_envelope_from_buffer(buffer)
            envelope = _META_ENVELOPE_START + envelope_json + _META_ENVELOPE_END
            
            # Prepend envelope to first text input item
            # Frontend sends: params.input = [{ type: 'text', text: '...' }]
            params = payload.get("params", {})
            input_items = params.get("input", [])
            if input_items and isinstance(input_items[0], dict):
                if input_items[0].get("type") == "text":
                    original_text = input_items[0].get("text", "")
                    input_items[0]["text"] = envelope + original_text
            
            payload["params"] = params
    
    # ... rest of existing code ...
    await _write_appserver(payload)
    return {"ok": True}
```

### 4. Strip envelope on incoming user message

**Location:** `_extract_item_text()` in `server.py`

```python
# Must match injection constants
_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"


def _strip_meta_envelope(text: str) -> str:
    """Strip leading meta envelope (\x1eCODEX_META ...\x1f) from text if present.
    
    The envelope is prepended to user messages to provide command context
    to agents. It must be stripped before writing to transcript or
    displaying in frontend.
    """
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1:]
    return text


def _extract_item_text(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract text from user/assistant message items.
    
    This is the SINGLE choke point for sanitizing user messages before
    they reach transcript or frontend. All envelope stripping happens here.
    """
    raw_type = str(item.get("type") or "")
    item_type = raw_type.lower()
    
    if item_type in {"usermessage", "user_message"}:
        text_parts: List[str] = []
        content = item.get("content") or []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        if not text_parts and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
        if not text_parts and isinstance(item.get("message"), str):
            text_parts.append(item["message"])
        
        text = "\n".join(text_parts).strip()
        text = _strip_meta_envelope(text)  # <-- Strip envelope (choke point)
        
        if text:
            return {"role": "user", "text": text}
    
    if item_type in {"agentmessage", "assistantmessage", "assistant"}:
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("message") if isinstance(item.get("message"), str) else None
        if isinstance(text, str) and text.strip():
            return {"role": "assistant", "text": text.strip()}
    
    return None
```

### 5. Preview builder helper

(Already included in section 1 above as `_build_cmd_preview()`)

---

## Files Modified

- `server.py`

---

## Answers to Open Questions (Confirmed by Dex)

### 1. Where to capture command context?

**Answer:** Hook into `_tail_agent_pty_events_to_transcript()` in `server.py`.

When processing `agent_block_end` events, append command context to a buffer in `meta.json`:
- `evt['block']` contains: `cmd`, `exit_code`, `cwd`, `conversation_id`, `block_id`
- `shell_id`: read from `conversations/<id>/agent_pty/shell_id.txt`
- `preview`: read from snapshots (see below)

This is the terminalMode path (`/api/mcp/agent-pty/exec`) that `/codex-agent` uses.

### 2. Preview source

**Answer:** Read from `screen.snapshot.json` and/or `scrollback.snapshot.json`.

These are written at prompt/block-end by the MCP server and should be fresh by the time we process the event. Located at:
- `conversations/<id>/agent_pty/screen.snapshot.json`
- `conversations/<id>/agent_pty/scrollback.snapshot.json`

### 3. Multiple commands before message

**Answer:** Accumulate commands in a buffer array (option C).

**Bounding rules:**
- Keep last N commands (e.g., N=10)
- Per-command preview: 10-30 lines
- Total envelope size capped at 2-4 KB
- If overflow, include summary header:
  - `shell_id`
  - `total_commands_run` (including dropped)
  - `kept=N, dropped=M`
  - Per-command `truncated` flags

Buffer is flushed only when user sends next message (`turn/start`).

---

## Validation

After implementation:

- [ ] User runs command via terminalMode → `agent_block_end` triggers buffering
- [ ] Command context buffered in `meta.json` under `pending_cmd_buffer`
- [ ] Multiple commands accumulate in buffer (up to 10)
- [ ] User sends message → envelope prepended with all buffered commands
- [ ] Agent receives full message with envelope (can parse context array)
- [ ] Buffer cleared from `meta.json` after flush
- [ ] Transcript shows clean message (no envelope)
- [ ] Frontend displays clean message (no envelope)
- [ ] Preview is bounded (20 lines, 3KB max per command)
- [ ] Overflow tracked: `total_commands_run`, `kept`, `dropped`

---

## Future Considerations

- **`user_kill` type:** When user sends Ctrl+C or kills a command
- **Rich preview:** Include exit code styling, timing info
- **Opt-out:** Setting to disable envelope injection if needed

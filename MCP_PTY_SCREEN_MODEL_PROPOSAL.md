# MCP PTY Screen Model Proposal (pyte fan‑out)

## Purpose

Add a **screen‑state channel** alongside the existing raw/output spool so TUI/interactive apps (e.g., Gemini) can be reasoned about reliably. This avoids “duplicate lines” caused by cursor movement and redraws and gives agents a clear, up‑to‑date view of **what is actually visible**.

---

## Summary (One‑Page)

**Current**:  
We store a normalized line spool (`output.spool`) and block output files. For TUIs, the raw byte stream includes cursor movement, redraws, and OSC, which cannot be line‑deduped.

**Proposed**:  
Split PTY output into:

1) **Raw stream log** (lossless bytes)  
2) **Screen model** (pyte) → emitted as `screen_delta` events and a snapshot file

Agents can query:
- raw stream (for debugging or replay),
- deterministic line spool (for regex waiting),
- **screen state** (for TUIs).

---

## Goals

1. Give agents an accurate **“what’s on screen now”** view.
2. Keep raw bytes **lossless** for replay/debug.
3. Preserve current `wait_for` behavior and block model.
4. Provide minimal, fast **delta events** for UI and tools.

---

## Core Design

### Output streams

| Stream | Purpose | Format |
|---|---|---|
| `output.raw` | Lossless PTY bytes | Raw bytes (no normalization) |
| `output.spool` | Deterministic text (current) | Normalized LF (`\n`) |
| `screen.*` | Rendered screen state | JSON deltas + snapshot |

### Screen model

Use **pyte** to parse terminal control sequences and maintain:
- screen grid (rows × cols)
- scrollback (optional buffer)
- cursor row/col
- title (OSC)
- alt‑screen state

---

## Data Flow

### Existing pipeline (simplified)

```
PTY chunk -> output.spool (normalized)
        -> blocks/*.out (raw-ish per block)
        -> wait_for / blocks read
```

### Proposed pipeline

```
PTY chunk -> output.raw (lossless bytes)
        -> output.spool (normalized LF; used by wait_for)
        -> pyte screen model
            -> screen_delta event
            -> screen.snapshot (latest)
```

### Where to hook

In `mcp_agent_pty_server.py`, in the chunk handler:

```
_on_chunk(chunk):
  append_raw(chunk)
  append_spool(normalize(chunk))
  screen.feed(chunk)         # pyte
  emit screen_delta if rows changed
  existing block delta logic
```

---

## Storage Layout (per conversation)

```
~/.cache/app_server/conversations/<id>/agent_pty/
├── output.raw             # new: lossless bytes
├── output.spool           # existing normalized text
├── screen.jsonl           # new: screen delta stream
├── screen.snapshot.json   # new: latest full screen
├── blocks.jsonl
└── blocks/*.out
```

Notes:
- `output.raw` is append‑only bytes.
- `screen.jsonl` stores only deltas (changed rows + cursor + title).
- `screen.snapshot.json` is a full state snapshot for quick reads.

---

## Event Schema

### `screen_delta` (emitted on change)

```json
{
  "type": "screen_delta",
  "conversation_id": "abc",
  "rows": [
    {"row": 12, "text": "⠙ Waiting for auth..."},
    {"row": 13, "text": "Gemini - agent_log_server"}
  ],
  "cursor": {"row": 14, "col": 0},
  "title": "Gemini - agent_log_server",
  "alt_screen": false,
  "ts": 1767572000
}
```

### `screen_snapshot` (on demand)

```json
{
  "rows": [
    "line 0 ...",
    "line 1 ...",
    "...",
    "line N ..."
  ],
  "cursor": {"row": 14, "col": 0},
  "title": "Gemini - agent_log_server",
  "alt_screen": false,
  "cols": 120,
  "rows_count": 40,
  "ts": 1767572000
}
```

---

## MCP Tools (Proposed)

### 1) `pty_read_raw`
Read the raw byte stream (lossless).
```json
{ "conversation_id": "...", "from_offset": 0, "max_bytes": 65536 }
```

### 2) `pty_read_screen`
Return the latest full screen snapshot.
```json
{ "conversation_id": "..." }
```

### 3) `pty_read_screen_deltas`
Read deltas from `screen.jsonl` using a cursor.
```json
{ "conversation_id": "...", "cursor": 0, "limit": 50 }
```

### 4) `pty_screen_status`
Convenience tool:
```json
{
  "conversation_id": "...",
  "cursor": 12345,
  "title": "...",
  "alt_screen": false,
  "rows": 40,
  "cols": 120
}
```

---

## Screen Model Details (pyte)

### Parser
- `pyte.Stream` + `pyte.Screen(cols, rows)`
- Feed every raw chunk into `stream.feed(chunk)`

### Dirty rows
pyte exposes `screen.dirty`:
- Track rows that changed since last flush.
- Emit only those rows as `screen_delta`.

### Scrollback
Use `pyte.DiffScreen` or store scrollback manually.
- Optional: keep a fixed scrollback buffer for agents.

### OSC Title
Capture title changes (OSC sequences) and include in `screen_delta`.

### Alternate screen
Track `alt_screen` state if using full‑screen TUIs.

---

## Why this works for agents

Raw logs are for replay.  
**Screen state is what the agent should read** for TUI control.  
This eliminates duplicate “waiting…” lines and gives the agent a stable prompt target.

---

## Performance & Backpressure

- Emit `screen_delta` only for changed rows.
- Optional rate‑limit (e.g., max 20 deltas/sec) to avoid spinner floods.
- Snapshot updates can be throttled (e.g., every 250ms or on prompt).

---

## Compatibility

- `output.spool` remains unchanged for `wait_for`.
- Existing block model remains unchanged.
- New screen tools are additive.

---

## Implementation Checklist

1. Add `output.raw` writer in `ConversationState`.
2. Integrate pyte screen model in `_on_chunk`.
3. Track `screen.dirty` rows and emit `screen_delta` events.
4. Persist `screen.jsonl` and `screen.snapshot.json`.
5. Add new MCP tools: `pty_read_raw`, `pty_read_screen`, `pty_read_screen_deltas`, `pty_screen_status`.
6. Add UI hooks (optional): display screen deltas for TUI cards.

---

## Open Questions

- Do we expose scrollback separately or bundle into `screen_snapshot`?
- How do we handle alternative screen vs main screen in transcripts?
- Should prompt sentinel force a full snapshot?


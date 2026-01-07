# Pyte Screen Model Implementation Sprints

**Date:** 2026-01-05  
**Author:** vectorArc (Copilot CLI)  
**Reviewers:** Dex (Codex CLI)  
**Goal:** Add pyte-based screen model fan-out to MCP PTY server for TUI support

---

## Review Notes (from Dex)

The following corrections have been incorporated based on code review:

1. **`output.raw` encoding** - Chunks arrive as `str`; re-encoding with `errors="replace"` loses bytes. Use `surrogateescape` for lossless round-trip, or rename to `output.text` and document limitation.
2. **pyte row extraction** - Use `screen.display[row]` (returns string) instead of `line.values()` which doesn't preserve column order.
3. **pyte attributes** - `screen.title` and `in_alternate_screen` aren't always present. Use `getattr()` guards; detect alt-screen via `pyte.modes.ALTBUF`.
4. **Dirty-row handling** - Don't clear `screen.dirty` before emitting deltas. Buffer dirty rows and flush on prompt/session end.
5. **Cursor semantics** - Define byte offset cursor for `screen.jsonl` (like `blocks.since`).
6. **`pty_read_raw` response** - Must be base64; JSON text is unsafe for arbitrary bytes.
7. **Screen size** - Document plan for resize events + future `pty_resize` tool.
8. **Prompt sentinel** - Decide whether `__FWS_PROMPT__` should be hidden from screen snapshots.
9. **Concurrency** - Use dedicated `_screen_lock` (not `_spool_lock`) to avoid `wait_for` latency.

1. **Alt-screen fix**: `pyte.modes.DECSCNM` is reverse-video, not alt-screen. Use `pyte.modes.ALTBUF` instead.

2. **Raw bytes now available**: Dex implemented **lossless raw bytes subscription** in `framework_shells` v0.0.4:
   - `subscribe_output_bytes()` / `unsubscribe_output_bytes()` 
   - Raw bytes fan-out in `_pty_reader`
   - This means we can get true lossless bytes from the PTY backend!

---

## Overview

The current `mcp_agent_pty_server.py` stores PTY output in:
- `output.spool` - normalized LF text (used by `wait_for`)
- `blocks/*.out` - per-block raw output
- `events.jsonl` - block lifecycle events

**Problem:** TUIs use cursor movement, redraws, spinners. The spool logs every redraw as a "new line" → massive duplication. Agents can't reason about "what's on screen now."

**Solution:** Add pyte as a parallel consumer that maintains a virtual terminal screen, emitting:
- `screen.jsonl` - screen delta events (changed rows)
- `screen.snapshot.json` - latest full screen state
- `output.raw` - lossless byte stream (new)

---

## Sprint 1: Raw Byte Stream + Pyte Integration (Foundation)

**Effort:** ~2-3 hours  
**Risk:** Low (additive changes only)

### Goals
1. Add `output.raw` writer (lossless bytes, no normalization)
2. Integrate pyte `Screen` + `Stream` into `ConversationState`
3. Basic dirty-row tracking

### Patch Proposal

```python
# === ADDITIONS TO TOP OF FILE ===

import pyte  # Add to imports
import pyte.modes

# === ADDITIONS TO ConversationState.__init__ ===

        # Screen model (pyte)
        self._screen: Optional[pyte.Screen] = None
        self._stream: Optional[pyte.Stream] = None
        self._screen_cols: int = 120
        self._screen_rows: int = 40
        self._pending_dirty_rows: set = set()  # Buffered dirty rows (don't clear pyte.dirty early)
        
        # Raw byte stream (truly lossless via framework_shells subscribe_output_bytes)
        self._raw_path: Optional[Path] = None
        self._raw_size: int = 0
        self._bytes_queue: Optional[asyncio.Queue] = None
        self._bytes_reader_task: Optional[asyncio.Task] = None
        
        # Dedicated lock for screen operations (avoid blocking wait_for)
        self._screen_lock = asyncio.Lock()

# === NEW METHODS IN ConversationState ===

    async def _init_raw(self) -> None:
        """Initialize raw byte stream file."""
        if self._raw_path is None:
            self._raw_path = _agent_pty_root(self.conversation_id) / "output.raw"
            self._raw_path.parent.mkdir(parents=True, exist_ok=True)
            if self._raw_path.exists():
                self._raw_size = self._raw_path.stat().st_size
            else:
                self._raw_path.write_bytes(b"")
                self._raw_size = 0

    async def _append_raw(self, data: bytes) -> int:
        """Append raw bytes (lossless), return new size."""
        async with self._screen_lock:  # Use dedicated lock
            await self._init_raw()
            await asyncio.to_thread(self._append_bytes, self._raw_path, data)
            self._raw_size += len(data)
            return self._raw_size

    async def _ensure_bytes_reader(self, mgr) -> None:
        """Subscribe to raw bytes stream from PTY (truly lossless)."""
        if self._bytes_reader_task and not self._bytes_reader_task.done():
            return
        
        # Use new framework_shells subscribe_output_bytes for lossless raw bytes
        self._bytes_queue = await mgr.subscribe_output_bytes(self.shell_id)
        
        async def _run_bytes() -> None:
            while True:
                chunk_bytes: bytes = await self._bytes_queue.get()
                # Append raw bytes directly (no encoding needed - truly lossless)
                await self._append_raw(chunk_bytes)
                # Feed to pyte (decode for pyte, but raw already saved)
                try:
                    chunk_str = chunk_bytes.decode("utf-8", errors="replace")
                    self._feed_screen(chunk_str)
                    await self._emit_screen_delta()
                except Exception:
                    pass  # pyte may choke; raw bytes already saved
        
        self._bytes_reader_task = asyncio.create_task(
            _run_bytes(), 
            name=f"agent-pty-bytes-reader:{self.conversation_id}"
        )

    def _init_screen(self) -> None:
        """Initialize pyte screen model."""
        if self._screen is None:
            self._screen = pyte.Screen(self._screen_cols, self._screen_rows)
            self._stream = pyte.Stream(self._screen)

    def _feed_screen(self, data: str) -> set:
        """Feed data to pyte, return set of dirty row indices."""
        self._init_screen()
        # NOTE: Don't clear screen.dirty here - we buffer in _pending_dirty_rows
        # and clear only after emitting deltas
        try:
            self._stream.feed(data)
        except Exception:
            pass  # pyte may choke on malformed sequences
        dirty = set(self._screen.dirty)
        self._pending_dirty_rows.update(dirty)
        return dirty

    def _get_screen_row(self, row: int) -> str:
        """Get text content of a screen row (0-indexed)."""
        if self._screen is None:
            return ""
        # Use screen.display[row] for correct column-ordered string
        return self._screen.display[row].rstrip()

    def _is_alt_screen(self) -> bool:
        """Check if terminal is in alternate screen mode."""
        if self._screen is None:
            return False
        # Check via ALTBUF mode flag (not DECSCNM which is reverse-video)
        try:
            return pyte.modes.ALTBUF in self._screen.mode
        except AttributeError:
            # Fallback for older pyte versions
            return getattr(self._screen, 'in_alternate_screen', False)

    def _get_screen_snapshot(self) -> dict:
        """Get full screen state as dict."""
        self._init_screen()
        rows = []
        for i in range(self._screen_rows):
            rows.append(self._get_screen_row(i))
        return {
            "rows": rows,
            "cursor": {"row": self._screen.cursor.y, "col": self._screen.cursor.x},
            "title": getattr(self._screen, 'title', '') or "",
            "alt_screen": self._is_alt_screen(),
            "cols": self._screen_cols,
            "rows_count": self._screen_rows,
            "ts": _now_ms(),
        }

# === MODIFY _on_chunk METHOD ===

    # NOTE: Raw bytes are now handled by _ensure_bytes_reader (separate task)
    # _on_chunk still receives text for existing spool/block logic
    
    async def _on_chunk(self, chunk: str) -> None:
        # Always notify raw chunk callbacks first (for xterm.js streaming)
        await self._notify_raw_chunk(chunk)
        
        # NOTE: Raw bytes already saved by _bytes_reader_task (truly lossless)
        # NOTE: pyte already fed by _bytes_reader_task
        
        # Append to spool for cursor-based wait_for (existing)
        await self._append_spool(chunk)
        
        # ... rest of existing method unchanged ...

# === MODIFY _ensure_reader METHOD ===

    async def _ensure_reader(self, mgr) -> None:
        if self._reader_task and not self._reader_task.done():
            return
        q = await mgr.subscribe_output(self.shell_id)
        
        # NEW: Also start bytes reader for lossless raw stream + pyte
        await self._ensure_bytes_reader(mgr)

        async def _run() -> None:
            while True:
                chunk = await q.get()
                await self._on_chunk(chunk)

        self._reader_task = asyncio.create_task(_run(), name=f"agent-pty-reader:{self.conversation_id}")
```

**Key changes from original:**
- Use `subscribe_output_bytes()` for truly lossless raw bytes (no re-encoding)
- Separate `_bytes_reader_task` handles raw bytes → `output.raw` + pyte feed
- Existing `_on_chunk` (text) still handles spool + block logic
- pyte fed with decoded bytes (errors="replace" is fine since raw already saved)
- Alt-screen uses `pyte.modes.ALTBUF` (not DECSCNM)

### Files Modified
- `mcp_agent_pty_server.py`

### Validation
- Run existing PTY tools - should work unchanged
- Check `output.raw` is created and grows
- Check pyte doesn't crash on escape sequences

---

## Sprint 2: Screen Delta Events + Persistence

**Effort:** ~2 hours  
**Depends on:** Sprint 1

### Goals
1. Emit `screen_delta` events to `screen.jsonl`
2. Persist `screen.snapshot.json` (throttled)
3. Rate-limit delta emission (max 10/sec for spinner flood protection)

### Patch Proposal

```python
# === NEW HELPER FUNCTIONS ===

def _screen_events_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "screen.jsonl"

def _screen_snapshot_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "screen.snapshot.json"

# === ADDITIONS TO ConversationState.__init__ ===

        # Screen delta rate limiting
        self._last_screen_delta_ts: float = 0.0
        self._screen_delta_min_interval: float = 0.1  # 100ms = max 10/sec
        # _pending_dirty_rows already defined in Sprint 1
        
        # Snapshot throttling
        self._last_snapshot_ts: float = 0.0
        self._snapshot_interval: float = 0.25  # 250ms

# === NEW METHODS IN ConversationState ===

    async def _emit_screen_delta(self) -> None:
        """Emit screen delta event (rate-limited). Flushes _pending_dirty_rows."""
        now = time.time()
        
        # Rate limit (skip if too soon, unless force-flushing)
        if now - self._last_screen_delta_ts < self._screen_delta_min_interval:
            return
        
        if not self._pending_dirty_rows:
            return
        
        async with self._screen_lock:
            # Build delta event from buffered dirty rows
            rows_data = []
            for row_idx in sorted(self._pending_dirty_rows):
                if 0 <= row_idx < self._screen_rows:
                    text = self._get_screen_row(row_idx)
                    # Optionally filter out prompt sentinel from visible output
                    # if "__FWS_PROMPT__" in text:
                    #     text = text.replace("__FWS_PROMPT__", "").strip()
                    rows_data.append({
                        "row": row_idx,
                        "text": text,
                    })
            
            event = {
                "type": "screen_delta",
                "conversation_id": self.conversation_id,
                "rows": rows_data,
                "cursor": {"row": self._screen.cursor.y, "col": self._screen.cursor.x},
                "title": getattr(self._screen, 'title', '') or "",
                "alt_screen": self._is_alt_screen(),
                "ts": _now_ms(),
            }
            
            # Write to screen.jsonl
            path = _screen_events_path(self.conversation_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, ensure_ascii=False)
            await asyncio.to_thread(self._append_line, path, line)
            
            # Clear pending dirty rows and pyte's dirty set
            self._pending_dirty_rows.clear()
            if self._screen:
                self._screen.dirty.clear()
            self._last_screen_delta_ts = now
        
        # Maybe update snapshot
        await self._maybe_update_snapshot()

    async def _maybe_update_snapshot(self) -> None:
        """Update snapshot file if enough time has passed."""
        now = time.time()
        if now - self._last_snapshot_ts < self._snapshot_interval:
            return
        
        async with self._screen_lock:
            snapshot = self._get_screen_snapshot()
            path = _screen_snapshot_path(self.conversation_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            self._last_snapshot_ts = now

    async def _flush_screen_state(self) -> None:
        """Force flush any pending screen state (call on session end)."""
        # Force emit regardless of rate limit
        self._last_screen_delta_ts = 0
        await self._emit_screen_delta()
        # Force snapshot update
        self._last_snapshot_ts = 0
        await self._maybe_update_snapshot()
```

**Key changes from original:**
- `_emit_screen_delta()` now takes no args; reads from `_pending_dirty_rows`
- Clear `screen.dirty` only AFTER emitting delta (not before feeding)
- Use `_screen_lock` for all screen operations
- Use `getattr()` for `screen.title`
- Added commented-out prompt sentinel filtering (design decision pending)

### Files Modified
- `mcp_agent_pty_server.py`

### Validation
- Start interactive session with TUI (e.g., `htop`, `vim`)
- Check `screen.jsonl` has delta events
- Check `screen.snapshot.json` updates periodically
- Verify spinner floods don't create 1000s of events

---

## Sprint 3: New MCP Tools for Screen Access

**Effort:** ~1.5 hours  
**Depends on:** Sprint 2

### Goals
1. Add `pty_read_raw` tool
2. Add `pty_read_screen` tool (snapshot)
3. Add `pty_read_screen_deltas` tool
4. Add `pty_screen_status` tool

### Patch Proposal

```python
# === NEW MCP TOOLS ===

@mcp.tool(name="pty_read_raw", description="Read lossless raw PTY output bytes at an offset (base64 encoded).")
async def pty_read_raw(
    conversation_id: str,
    from_offset: int = 0,
    max_bytes: int = 65536,
) -> Dict[str, Any]:
    """
    Read raw PTY output (lossless bytes, includes all escape sequences).
    
    Returns base64-encoded data since raw bytes may contain invalid UTF-8.
    Use this for debugging or replay. For TUI state, use pty_read_screen instead.
    """
    state = _state(conversation_id)
    try:
        await state._init_raw()
        from_offset = max(0, int(from_offset))
        max_bytes = max(1, min(int(max_bytes), 1024 * 1024))
        
        if from_offset >= state._raw_size:
            return {"ok": True, "data_b64": "", "offset": from_offset, "resume_offset": state._raw_size, "raw_size": state._raw_size}
        
        data = await asyncio.to_thread(
            ConversationState._read_bytes, state._raw_path, from_offset, max_bytes
        )
        # Return as base64 (primary) - safe for JSON transport
        data_b64 = base64.b64encode(data).decode("ascii")
        # Also provide lossy UTF-8 for convenience (clearly marked as lossy)
        return {
            "ok": True,
            "data_b64": data_b64,
            "data_utf8_lossy": data.decode("utf-8", errors="replace"),
            "offset": from_offset,
            "resume_offset": from_offset + len(data),
            "raw_size": state._raw_size,
            "bytes_returned": len(data),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_read_screen", description="Get the current terminal screen state (what the user sees).")
async def pty_read_screen(conversation_id: str) -> Dict[str, Any]:
    """
    Get the current rendered screen state.
    
    Returns the terminal screen as an array of row strings, plus cursor position,
    title, and alt-screen state. This is what an agent should read for TUI control.
    
    Note: Screen dimensions are fixed at 120x40. Resize support planned for future.
    """
    state = _state(conversation_id)
    try:
        async with state._screen_lock:
            state._init_screen()
            snapshot = state._get_screen_snapshot()
        return {"ok": True, **snapshot}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_read_screen_deltas", description="Read screen delta events from a byte cursor position.")
async def pty_read_screen_deltas(
    conversation_id: str,
    cursor: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    Read screen delta events (incremental row changes) from screen.jsonl.
    
    Cursor is a byte offset into screen.jsonl (like blocks.since).
    Use resume_cursor from response as cursor for next call.
    
    Each delta contains: rows (changed), cursor position, title, alt_screen, ts.
    """
    path = _screen_events_path(conversation_id)
    if not path.exists():
        return {"ok": True, "cursor": 0, "resume_cursor": 0, "deltas": [], "file_size": 0}
    
    cursor = max(0, int(cursor))
    limit = max(1, min(int(limit), 200))
    
    try:
        data = await asyncio.to_thread(path.read_bytes)
        file_size = len(data)
        if cursor > file_size:
            cursor = file_size
        tail = data[cursor:]
        lines = tail.splitlines()[:limit]
        
        deltas = []
        for raw in lines:
            try:
                deltas.append(json.loads(raw))
            except Exception:
                continue
        
        # Calculate resume_cursor: cursor + bytes consumed (including newlines)
        consumed = sum(len(line) + 1 for line in lines)  # +1 for each \n
        resume_cursor = cursor + consumed
        if resume_cursor > file_size:
            resume_cursor = file_size
        
        return {"ok": True, "cursor": cursor, "resume_cursor": resume_cursor, "deltas": deltas, "file_size": file_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_screen_status", description="Get screen model status (dimensions, cursor, title, alt-screen).")
async def pty_screen_status(conversation_id: str) -> Dict[str, Any]:
    """
    Get screen model metadata without full row content.
    
    Useful for checking dimensions, cursor position, title, and alt-screen state
    without transferring all row data.
    
    Note: Screen dimensions are fixed at 120x40. Future: pty_resize tool.
    """
    state = _state(conversation_id)
    try:
        async with state._screen_lock:
            state._init_screen()
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "cursor": {"row": state._screen.cursor.y, "col": state._screen.cursor.x},
                "title": getattr(state._screen, 'title', '') or "",
                "alt_screen": state._is_alt_screen(),
                "cols": state._screen_cols,
                "rows": state._screen_rows,
                "raw_size": state._raw_size,
                "spool_size": state._spool_size,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

**Key changes from original:**
- `pty_read_raw`: Returns `data_b64` as primary (safe for JSON), `data_utf8_lossy` clearly marked as lossy
- `pty_read_screen_deltas`: Cursor is byte offset; `resume_cursor` calculated correctly with newlines
- Added `file_size` to delta response for cursor validation
- Use `_screen_lock` for thread safety
- Added notes about fixed screen size and future resize support
- Use `getattr()` for `screen.title`

### Files Modified
- `mcp_agent_pty_server.py`

### Validation
- Call `pty_read_screen` during TUI - should show clean rows
- Call `pty_read_raw` - should have escape sequences
- Call `pty_read_screen_deltas` - should have incremental events

---

## Sprint 3.1: Cursor Semantics Alignment (Quick Win)

**Effort:** ~0.5 hours  
**Depends on:** Sprint 3

### Goal
Standardize cursor semantics across spool and screen delta readers so agents can chain reads safely without skipping matches.

### Changes
1. **Spool waiters:** return `resume_cursor = match_span.end` (not end-of-scan), and use that in docs/examples.
2. **Screen deltas:** keep cursor as byte offset; always return `resume_cursor` based on bytes consumed (not a line count heuristic).
3. **Docs:** replace any lingering references to `next_cursor` in examples with `resume_cursor`.

### Validation
- Confirm chained `wait_for` calls do not skip matches when using `resume_cursor`.
- Confirm `pty_read_screen_deltas` resumes cleanly when following `resume_cursor`.

---

## Sprint 4: Scrollback Buffer + Session Lifecycle

**Effort:** ~2 hours  
**Depends on:** Sprint 3

### Goals
1. Add scrollback buffer (lines that scrolled off top)
2. Flush screen state on session end
3. Reset screen on new session
4. Handle alternate screen transitions

### Patch Proposal

```python
# === MODIFY ConversationState ===

# In __init__:
        # Scrollback
        self._scrollback_limit: int = 1000

# === NEW/MODIFIED METHODS ===

    def _init_screen(self) -> None:
        """Initialize pyte screen model with history tracking."""
        if self._screen is None:
            self._screen = pyte.HistoryScreen(
                self._screen_cols, 
                self._screen_rows,
                history=self._scrollback_limit,
            )
            self._stream = pyte.Stream(self._screen)
            # Enable line feed mode for proper scrollback
            self._screen.set_mode(pyte.modes.LNM)

    def _get_scrollback(self, limit: int = 100) -> list:
        """Get scrollback lines (lines that scrolled off top)."""
        if self._screen is None or not hasattr(self._screen, 'history'):
            return []
        history = list(self._screen.history.top)[-limit:]
        result = []
        for line in history:
            # Use display-style extraction for correct column order
            text = "".join(char.data for col, char in sorted(line.items())).rstrip()
            result.append(text)
        return result

    def _get_screen_snapshot(self) -> dict:
        """Get full screen state as dict (with scrollback)."""
        self._init_screen()
        rows = []
        for i in range(self._screen_rows):
            rows.append(self._get_screen_row(i))
        
        scrollback = self._get_scrollback(100)
        scrollback_total = len(list(self._screen.history.top)) if hasattr(self._screen, 'history') else 0
        
        return {
            "rows": rows,
            "scrollback": scrollback,
            "scrollback_total": scrollback_total,
            "cursor": {"row": self._screen.cursor.y, "col": self._screen.cursor.x},
            "title": getattr(self._screen, 'title', '') or "",
            "alt_screen": self._is_alt_screen(),
            "cols": self._screen_cols,
            "rows_count": self._screen_rows,
            "ts": _now_ms(),
        }

    def _reset_screen(self) -> None:
        """Reset screen model (for new session)."""
        self._screen = None
        self._stream = None
        self._pending_dirty_rows.clear()
        self._last_screen_delta_ts = 0
        self._last_snapshot_ts = 0

# === MODIFY end_session ===

    async def end_session(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """End an interactive session."""
        if self._mode != "interactive":
            return {"ok": False, "error": "No interactive session active"}
        if session_id and session_id != self._interactive_session_id:
            return {"ok": False, "error": "Session ID mismatch"}
        
        # Try graceful exit with Ctrl+C
        await self.send_stdin("\x03")
        
        # NEW: Flush screen state (force emit pending deltas + snapshot)
        await self._flush_screen_state()
        
        # Mark session as ended
        if self._active:
            self._active.status = "completed"
            self._active.ts_end = _now_ms()
            await self._append_block_index(self._active)
            await self._append_event({
                "type": "agent_block_end",
                "conversation_id": self.conversation_id,
                "block": self._active.__dict__
            })
            self._active = None
        
        self._mode = "idle"
        self._interactive_session_id = None
        return {"ok": True}
```

**Key changes from original:**
- Removed redundant `self._scrollback` list (pyte.HistoryScreen manages it)
- Use `hasattr()` guard for `screen.history`
- Scrollback extraction uses `sorted(line.items())` for correct column order
- Use `getattr()` for `screen.title`
- `_flush_screen_state()` forces both delta and snapshot update

### Files Modified
- `mcp_agent_pty_server.py`

### Validation
- Run command with lots of output → check scrollback captured
- End session → check `screen.snapshot.json` is final state
- Start new session → check screen reset properly

---

## Sprint 5: UI Integration + WebSocket Fanout

**Effort:** ~2-3 hours  
**Depends on:** Sprint 4

### Goals
1. Tail `screen.jsonl` in `server.py` (like `events.jsonl`)
2. Broadcast `screen_delta` events to WebSocket
3. Add screen display option in frontend (optional)

### Patch Proposal (server.py sketch)

```python
# === IN server.py ===

# Similar to existing agent_pty event tailing, add screen event tailing

async def _tail_screen_events(conversation_id: str) -> None:
    """Tail screen.jsonl and broadcast to WebSocket."""
    path = _agent_pty_root(conversation_id) / "screen.jsonl"
    offset = 0
    
    while True:
        if not path.exists():
            await asyncio.sleep(0.5)
            continue
        
        try:
            data = path.read_bytes()
            if len(data) > offset:
                new_data = data[offset:]
                for line in new_data.splitlines():
                    try:
                        event = json.loads(line)
                        await _broadcast_ws({
                            "type": "screen_delta",
                            "conversation_id": conversation_id,
                            **event,
                        })
                    except Exception:
                        pass
                offset = len(data)
        except Exception:
            pass
        
        await asyncio.sleep(0.1)  # 100ms poll
```

### Files Modified
- `server.py`
- `static/codex_agent.js` (optional: render screen cards)

### Validation
- Open web UI
- Start TUI session
- Check WebSocket receives `screen_delta` events
- (Optional) Render screen state in UI

---

## Sprint 6: Documentation + Polish

**Effort:** ~1 hour  
**Depends on:** Sprint 5

### Goals
1. Update `PROGRESS.md` with new capabilities
2. Update `MCP_PTY_SCREEN_MODEL_PROPOSAL.md` with implementation notes
3. Add usage examples to `AGENTS.md`
4. Handle edge cases (resize, encoding errors)

### Tasks
- Document new tools in AGENTS.md
- Add example agent workflow for TUI control
- Note: screen size is fixed at 120x40 (add resize support later if needed)
- Handle pyte exceptions gracefully

---

## Dependency Graph

```
Sprint 1 (Foundation)
    │
    ▼
Sprint 2 (Delta Events)
    │
    ▼
Sprint 3 (MCP Tools)
    │
    ▼
Sprint 4 (Scrollback)
    │
    ▼
Sprint 5 (UI Integration) ──── Optional
    │
    ▼
Sprint 6 (Docs)
```

---

## Requirements

Add to `requirements.txt`:
```
pyte>=0.8.0
```

---

## Open Questions

1. **Screen dimensions**: Fixed 120x40 or detect from PTY? 
   - **Decision**: Start fixed 120x40. Add `pty_resize(cols, rows)` tool in future sprint.

2. **Scrollback in deltas**: Include scrollback changes in delta events? 
   - **Decision**: No - scrollback only in snapshot. Deltas are for visible screen only.

3. **Alt-screen**: Separate storage for alt-screen content? 
   - **Decision**: No - just track `alt_screen` boolean flag. Both screens use same storage.

4. **Encoding**: Raw is bytes, screen is text. Handle encoding errors? 
   - **Decision**: Use `subscribe_output_bytes()` for truly lossless raw bytes. Pyte gets decoded string (errors="replace" is fine since raw already saved).

5. **Prompt sentinel visibility**: Should `__FWS_PROMPT__` be hidden from screen snapshots?
   - **Decision**: TBD. Leave visible for now; can filter in `_get_screen_row()` if needed.

6. **Concurrency**: Separate lock for screen vs spool?
   - **Decision**: Yes. Use `_screen_lock` for screen operations to avoid blocking `wait_for`.

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| pyte crashes on malformed sequences | Wrap `stream.feed()` in try/except |
| Delta floods from spinners | Rate limiting (10/sec max) + flush on session end |
| Large scrollback memory | Limit to 1000 lines via HistoryScreen |
| Concurrent access | Dedicated `_screen_lock` for screen, existing `_spool_lock` for spool |
| Backward compatibility | All changes additive, existing tools unchanged |
| `output.raw` not truly lossless | Use `subscribe_output_bytes()` - truly lossless now |
| Row extraction wrong order | Use `screen.display[row]` not `line.values()` |
| Alt-screen detection wrong | Use `pyte.modes.ALTBUF` not `DECSCNM` |
| Missing pyte attributes | Use `getattr()` guards throughout |

---

## Success Criteria

After all sprints:
- [ ] Agent can call `pty_read_screen` and get clean row text (no escape sequences)
- [ ] Spinner loops produce ~10 deltas/sec max, not 100s
- [ ] Raw bytes preserved **truly losslessly** in `output.raw` (via `subscribe_output_bytes`)
- [ ] Existing `wait_for` and spool tools work unchanged
- [ ] TUI state visible in screen snapshot
- [ ] Scrollback captured for commands with lots of output
- [ ] Final screen state flushed on session end
- [ ] `screen.jsonl` cursor semantics match `blocks.since` (byte offset)

---

## Two-Surface Contract (User vs Agent)

### User Surface Contract (UI-facing)
**Goal:** Look and feel like a normal terminal, even though it is block-based.

**Surfaces:**
1. **Prompt area (idle)**  
   - A command input form (not a live PTY).
   - Shows cwd/exit code as part of prompt metadata.
2. **Transcript cards (one per command)**  
   - **Header:** command text (decoded `cmd_b64` from `__FWS_BLOCK_BEGIN__`).
   - **Body (running):** live xterm.js stream for the *current* block only.
   - **Body (finished):** frozen snapshot of the command output (no live stream).
3. **Prompt area (running/TUI)**  
   - While a command runs, the prompt area becomes a live input surface (keystrokes to PTY).
   - This is *separate* from the transcript card body.

**State transitions:**
- `BLOCK_BEGIN` -> create new card, start live stream into that card, switch prompt to TUI input mode.
- `BLOCK_END` + `PROMPT` -> freeze card output (snapshot), detach live stream, restore prompt form.

**Invariants:**
- Only one live xterm.js stream at a time.
- Transcript cards never receive raw PTY bytes after the block ends.
- Prompt area is the only live input surface while a command runs.

---

### Agent Surface Contract (Structured truth)
**Goal:** Deterministic, inspectable context that never depends on UI rendering.

**Primary object: Block Context**
Each command produces a block with:
- `cmd` (string, decoded)
- `cwd`
- `ts_begin`, `ts_end`, `duration_ms`
- `exit_code`
- `output.raw` reference (lossless bytes)
- `screen.snapshot.json` (rendered view at prompt)
- `screen.jsonl` deltas (optional for replay)

**Agent read rules:**
- For "what happened": use `cmd`, `exit_code`, and `screen.snapshot.json`.
- For "what exactly was emitted": use `output.raw` (lossless bytes).
- For incremental TUI control: use `pty_read_screen` and `pty_read_screen_deltas`.

**Invariants:**
- Agent reads block data, not UI streams.
- `pty_read_screen` returns the last known rendered screen even if in-memory state is gone.
- Raw bytes are preserved losslessly and can rehydrate the screen.

---

## Wiring Plan (Bridging the Two Surfaces)

1. **Block lifecycle is the bridge.**  
   - User surface uses `BLOCK_BEGIN/END` to create cards + freeze snapshots.
   - Agent surface uses the same markers to define block context boundaries.

2. **Snapshot timing.**  
   - On prompt sentinel, flush screen state and persist `screen.snapshot.json`.
   - That snapshot becomes the frozen card body and the agent’s rendered context.

3. **Stream routing.**  
   - Live PTY stream -> card body only while block is running.
   - Post-block -> no more PTY stream to that card; freeze snapshot instead.

4. **Separation guarantee.**  
   - User surface can change (layout/UX) without affecting agent semantics.
   - Agent surface remains stable, testable, and structured.

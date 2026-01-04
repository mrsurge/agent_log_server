#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from framework_shells import get_manager as get_framework_shell_manager
from mcp.server.fastmcp import FastMCP


_DEFAULT_CONVERSATION_DIR = Path(os.path.expanduser("~/.cache/app_server/conversations"))


def _conversation_dir() -> Path:
    raw = os.environ.get("AGENT_LOG_SERVER_CONVERSATION_DIR")
    if raw:
        return Path(os.path.expanduser(raw))
    return _DEFAULT_CONVERSATION_DIR


def _b64decode(s: str) -> str:
    try:
        return base64.b64decode(s.encode("ascii"), validate=False).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _agent_pty_root(conversation_id: str) -> Path:
    safe = "".join(ch for ch in conversation_id if ch.isalnum() or ch in ("-", "_"))
    return _conversation_dir() / safe / "agent_pty"


def _blocks_dir(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "blocks"


def _blocks_index_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "blocks.jsonl"


def _blocks_events_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "events.jsonl"


def _rcfile_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "bashrc_agent_pty.sh"


_MARKER_BEGIN = "__FWS_BLOCK_BEGIN__"
_MARKER_END = "__FWS_BLOCK_END__"


def _write_rcfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Marker format:
    #   __FWS_BLOCK_BEGIN__ seq=<n> ts=<ms> cwd_b64=<...> cmd_b64=<...>
    #   __FWS_BLOCK_END__ seq=<n> ts=<ms> exit=<code>
    #   __FWS_PROMPT__ ts=<ms> cwd_b64=<...>
    #
    # We rely on base64 + tr to avoid quoting issues.
    content = r"""
__FWS_SEQ=0
__FWS_LAST_SEQ=""
__FWS_IN_MARKER=0
__FWS_READY=0
__FWS_MANUAL="${__FWS_MANUAL:-0}"

__fws_b64() { printf %s "$1" | base64 | tr -d '\n'; }
__fws_now_ms() {
  date +%s%3N 2>/dev/null && return 0
  python - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

__fws_emit_begin() {
  local cmd="$1"
  local cwd="$2"
  local ts="$3"
  local seq="$4"
  local cwd_b64="$(__fws_b64 "$cwd")"
  local cmd_b64="$(__fws_b64 "$cmd")"
  printf '\n__FWS_BLOCK_BEGIN__ seq=%s ts=%s cwd_b64=%s cmd_b64=%s\n' "$seq" "$ts" "$cwd_b64" "$cmd_b64"
}

__fws_emit_end() {
  local exit_code="$1"
  local ts="$2"
  local seq="$3"
  printf '\n__FWS_BLOCK_END__ seq=%s ts=%s exit=%s\n' "$seq" "$ts" "$exit_code"
}

__fws_emit_prompt() {
  local ts="$(__fws_now_ms)"
  local cwd="$(pwd -P 2>/dev/null || pwd)"
  local cwd_b64="$(__fws_b64 "$cwd")"
  printf '\n__FWS_PROMPT__ ts=%s cwd_b64=%s\n' "$ts" "$cwd_b64"
}

__fws_should_ignore_cmd() {
  local cmd="$1"
  case "$cmd" in
    PS1=*|PROMPT_COMMAND=*|__FWS_READY=*|__FWS_SEQ=*|__FWS_LAST_SEQ=*|__FWS_IN_MARKER=*|trap*|shopt*|set\ +o*|set\ -o*)
      return 0
      ;;
    *__FWS_BLOCK_BEGIN__*|*__FWS_BLOCK_END__*|*__FWS_PROMPT__*) return 0 ;;
    __fws_*|__FWS_*) return 0 ;;
  esac
  return 1
}

if [ "${__FWS_MANUAL}" = "1" ]; then
  # In manual mode, Python wraps each submitted command in a single BEGIN/END marker pair.
  # This prevents compound commands (e.g. `echo hi && pwd`) from being split into multiple blocks.
  __FWS_READY=1
  
  # Emit prompt sentinel after each command (PROMPT_COMMAND runs before prompt display)
  __fws_manual_precmd() {
    __fws_emit_prompt
  }
  PROMPT_COMMAND="__fws_manual_precmd"
else
  trap '__fws_preexec' DEBUG
  __fws_preexec() {
    if [ "${__FWS_IN_MARKER}" = "1" ]; then return 0; fi
    # Only start emitting once the shell has reached its first prompt.
    if [ "${__FWS_READY}" != "1" ]; then return 0; fi
    # Only in interactive shells.
    case "$-" in *i*) ;; *) return 0 ;; esac
    local cmd="${BASH_COMMAND}"
    if __fws_should_ignore_cmd "$cmd"; then return 0; fi
    __FWS_IN_MARKER=1
    __FWS_SEQ=$((__FWS_SEQ + 1))
    __FWS_LAST_SEQ="$__FWS_SEQ"
    local ts="$(__fws_now_ms)"
    local cwd="$(pwd -P 2>/dev/null || pwd)"
    __fws_emit_begin "$cmd" "$cwd" "$ts" "$__FWS_SEQ"
    __FWS_IN_MARKER=0
  }

  __fws_precmd() {
    if [ "${__FWS_IN_MARKER}" = "1" ]; then return 0; fi
    if [ "${__FWS_READY}" != "1" ]; then
      __FWS_READY=1
      __fws_emit_prompt
      return 0
    fi
    if [ -z "${__FWS_LAST_SEQ}" ]; then
      __fws_emit_prompt
      return 0
    fi
    __FWS_IN_MARKER=1
    local exit_code="$?"
    local ts="$(__fws_now_ms)"
    __fws_emit_end "$exit_code" "$ts" "$__FWS_LAST_SEQ"
    __FWS_LAST_SEQ=""
    __FWS_IN_MARKER=0
    __fws_emit_prompt
  }

  PROMPT_COMMAND="__fws_precmd"
fi

PS1="agent-pty> "
"""
    path.write_text(content.lstrip(), encoding="utf-8")


@dataclass
class BlockInfo:
    block_id: str
    conversation_id: str
    seq: int
    ts_begin: int
    cwd: str
    cmd: str
    status: str
    exit_code: Optional[int] = None
    ts_end: Optional[int] = None
    output_path: Optional[str] = None


def _output_spool_path(conversation_id: str) -> Path:
    """Canonical output spool for wait_for cursor operations."""
    return _agent_pty_root(conversation_id) / "output.spool"


_MARKER_PROMPT = "__FWS_PROMPT__"


class ConversationState:
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.lock = asyncio.Lock()
        self.shell_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer = ""
        self._active: Optional[BlockInfo] = None
        self._begin_waiter: Optional[asyncio.Future] = None
        self._raw_chunk_callbacks: list = []  # List of async callbacks for raw chunks
        # Mode: 'idle', 'block_running', 'interactive'
        self._mode: str = "idle"
        self._interactive_session_id: Optional[str] = None
        # Output spool for cursor-based wait_for
        self._spool_lock = asyncio.Lock()
        self._spool_path: Optional[Path] = None
        self._spool_size: int = 0
        # Waiters for wait_for - list of (condition_fn, future, from_cursor)
        self._waiters: list = []

    @property
    def mode(self) -> str:
        return self._mode

    def add_raw_chunk_callback(self, callback) -> None:
        """Add a callback to receive raw PTY chunks (for WebSocket streaming)."""
        if callback not in self._raw_chunk_callbacks:
            self._raw_chunk_callbacks.append(callback)

    def remove_raw_chunk_callback(self, callback) -> None:
        """Remove a raw chunk callback."""
        if callback in self._raw_chunk_callbacks:
            self._raw_chunk_callbacks.remove(callback)

    async def _notify_raw_chunk(self, chunk: str) -> None:
        """Notify all raw chunk callbacks."""
        for cb in self._raw_chunk_callbacks:
            try:
                await cb(chunk)
            except Exception:
                pass

    async def _init_spool(self) -> None:
        """Initialize or open the output spool file."""
        if self._spool_path is None:
            self._spool_path = _output_spool_path(self.conversation_id)
            self._spool_path.parent.mkdir(parents=True, exist_ok=True)
            if self._spool_path.exists():
                self._spool_size = self._spool_path.stat().st_size
            else:
                self._spool_path.write_bytes(b"")
                self._spool_size = 0

    async def _append_spool(self, data: str) -> int:
        """Append to spool, return new size (cursor position)."""
        async with self._spool_lock:
            await self._init_spool()
            # Normalize to \n for storage
            normalized = data.replace("\r\n", "\n").replace("\r", "\n")
            encoded = normalized.encode("utf-8", errors="replace")
            await asyncio.to_thread(self._append_bytes, self._spool_path, encoded)
            self._spool_size += len(encoded)
            return self._spool_size

    @staticmethod
    def _append_bytes(path: Path, data: bytes) -> None:
        with path.open("ab") as f:
            f.write(data)

    async def read_spool(self, from_cursor: int = 0, max_bytes: int = 65536) -> tuple:
        """Read spool from cursor, returns (data, next_cursor)."""
        async with self._spool_lock:
            await self._init_spool()
            from_cursor = max(0, int(from_cursor))
            if from_cursor >= self._spool_size:
                return ("", self._spool_size)
            data = await asyncio.to_thread(self._read_bytes, self._spool_path, from_cursor, max_bytes)
            return (data.decode("utf-8", errors="replace"), from_cursor + len(data))

    @staticmethod
    def _read_bytes(path: Path, offset: int, max_bytes: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read(max_bytes)

    async def _check_waiters(self, new_data: str) -> None:
        """Check if any waiters match the new data."""
        if not self._waiters:
            return
        # Read recent spool data for matching
        resolved = []
        for i, (match_fn, future, from_cursor) in enumerate(self._waiters):
            if future.done():
                resolved.append(i)
                continue
            try:
                data, data_end_cursor = await self.read_spool(from_cursor, 1024 * 1024)  # 1MB max scan
                result = match_fn(data)
                if result is not None:
                    match_cursor = from_cursor + result["match_index"]
                    match_end_cursor = from_cursor + result["match_end"]
                    response = {
                        "matched": True,
                        "match_text": result["match_text"],
                        "match_cursor": match_cursor,
                        "match_span": {"start": match_cursor, "end": match_end_cursor},
                        "next_cursor": data_end_cursor,
                    }
                    if result.get("extra"):
                        response["extra"] = result["extra"]
                    future.set_result(response)
                    resolved.append(i)
            except Exception as e:
                future.set_exception(e)
                resolved.append(i)
        # Remove resolved waiters
        for i in reversed(resolved):
            self._waiters.pop(i)

    async def ensure_shell(self, *, cwd: Optional[str] = None) -> str:
        async with self.lock:
            if self.shell_id:
                return self.shell_id
            mgr = await get_framework_shell_manager()
            root = _agent_pty_root(self.conversation_id)
            root.mkdir(parents=True, exist_ok=True)
            rcfile = _rcfile_path(self.conversation_id)
            _write_rcfile(rcfile)
            command = ["env", "__FWS_MANUAL=1", "bash", "--rcfile", str(rcfile), "-i"]
            rec = await mgr.spawn_shell_dtach(command, cwd=cwd or str(Path.cwd()), label=f"agent-pty:{self.conversation_id}")
            self.shell_id = rec.id
            await self._ensure_reader(mgr)
            return self.shell_id

    async def _ensure_reader(self, mgr) -> None:
        if self._reader_task and not self._reader_task.done():
            return
        q = await mgr.subscribe_output(self.shell_id)

        async def _run() -> None:
            while True:
                chunk = await q.get()
                await self._on_chunk(chunk)

        self._reader_task = asyncio.create_task(_run(), name=f"agent-pty-reader:{self.conversation_id}")

    async def exec(self, *, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
        await self.ensure_shell(cwd=cwd)
        mgr = await get_framework_shell_manager()
        cmd_b64 = base64.b64encode(cmd.encode("utf-8", errors="replace")).decode("ascii")
        async with self.lock:
            loop = asyncio.get_running_loop()
            self._begin_waiter = loop.create_future()
            # Wrap the entire submitted command line in a single BEGIN/END marker pair.
            # This keeps `echo hi && pwd` as one block.
            wrapped = (
                f'__fws_cmd="$(printf %s \'{cmd_b64}\' | base64 -d 2>/dev/null)"; '
                'if [ -n "$__fws_cmd" ]; then '
                '__FWS_SEQ=$((__FWS_SEQ + 1)); __fws_seq="$__FWS_SEQ"; '
                '__fws_ts="$(__fws_now_ms)"; __fws_cwd="$(pwd -P 2>/dev/null || pwd)"; '
                '__fws_emit_begin "$__fws_cmd" "$__fws_cwd" "$__fws_ts" "$__fws_seq"; '
                'eval "$__fws_cmd"; __fws_ec="$?"; __fws_ts2="$(__fws_now_ms)"; '
                '__fws_emit_end "$__fws_ec" "$__fws_ts2" "$__fws_seq"; '
                'fi\n'
            )
            if cwd:
                wrapped = f'cd "{cwd}" 2>/dev/null || cd "{cwd}"\n' + wrapped
            await mgr.write_to_pty(self.shell_id, wrapped)
        try:
            info: BlockInfo = await asyncio.wait_for(self._begin_waiter, timeout=3.0)
        finally:
            async with self.lock:
                self._begin_waiter = None
        return {"ok": True, "block_id": info.block_id, "seq": info.seq, "ts": info.ts_begin}

    async def exec_interactive(self, *, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
        """
        Start an interactive session.
        
        The command runs without BEGIN/END wrappers - output streams until
        the shell prompt sentinel is detected or the session is ended.
        """
        await self.ensure_shell(cwd=cwd)
        mgr = await get_framework_shell_manager()
        
        # Create a session block manually (no shell wrapper)
        async with self.lock:
            ts = _now_ms()
            self._interactive_session_id = f"interactive:{ts}"
            seq = 0  # Interactive sessions don't use seq numbers
            block_id = f"{self.conversation_id}:interactive:{ts}"
            out_file = _blocks_dir(self.conversation_id) / f"interactive_{ts}.out"
            
            info = BlockInfo(
                block_id=block_id,
                conversation_id=self.conversation_id,
                seq=seq,
                ts_begin=ts,
                cwd=cwd or str(Path.cwd()),
                cmd=cmd,
                status="interactive",
                output_path=str(out_file),
            )
            self._active = info
            self._mode = "interactive"
            
            await self._append_event({
                "type": "agent_block_begin",
                "conversation_id": self.conversation_id,
                "block": info.__dict__
            })
            
            # Send command directly (no wrappers)
            if cwd:
                await mgr.write_to_pty(self.shell_id, f'cd "{cwd}" 2>/dev/null\n')
            await mgr.write_to_pty(self.shell_id, cmd + "\n")
        
        # Get current spool cursor so agent can wait_for from here
        _, cursor = await self.read_spool(0, 0)
        
        return {
            "ok": True,
            "session_id": self._interactive_session_id,
            "block_id": block_id,
            "ts_begin": ts,
            "cursor": cursor,
        }

    async def end_session(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """End an interactive session."""
        if self._mode != "interactive":
            return {"ok": False, "error": "No interactive session active"}
        if session_id and session_id != self._interactive_session_id:
            return {"ok": False, "error": "Session ID mismatch"}
        
        # Try graceful exit with Ctrl+C
        await self.send_stdin("\x03")
        
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

    async def _append_event(self, payload: Dict[str, Any]) -> None:
        path = _blocks_events_path(self.conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        await asyncio.to_thread(self._append_line, path, line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _append_block_index(self, info: BlockInfo) -> None:
        path = _blocks_index_path(self.conversation_id)
        payload = {
            "block_id": info.block_id,
            "conversation_id": info.conversation_id,
            "seq": info.seq,
            "ts_begin": info.ts_begin,
            "ts_end": info.ts_end,
            "cwd": info.cwd,
            "cmd": info.cmd,
            "status": info.status,
            "exit_code": info.exit_code,
            "output_path": info.output_path,
        }
        await asyncio.to_thread(self._append_line, path, json.dumps(payload, ensure_ascii=False))

    async def _on_chunk(self, chunk: str) -> None:
        # Always notify raw chunk callbacks first (for xterm.js streaming)
        await self._notify_raw_chunk(chunk)
        
        # Append to spool for cursor-based wait_for
        await self._append_spool(chunk)
        
        # Check waiters with new data
        await self._check_waiters(chunk)
        
        async with self.lock:
            self._buffer += chunk
            while "\n" in self._buffer:
                line, rest = self._buffer.split("\n", 1)
                self._buffer = rest
                await self._on_line(line)
            # Still write raw chunks to active block even if no newline boundaries.
            if self._active and chunk and "\n" not in chunk:
                await self._write_output(chunk)

    async def _write_output(self, text: str) -> None:
        if not self._active or not self._active.output_path:
            return
        path = Path(self._active.output_path)
        await asyncio.to_thread(self._append_line, path, text.rstrip("\n"))

    async def _on_line(self, line: str) -> None:
        if _MARKER_BEGIN in line:
            await self._handle_begin(line)
            return
        if _MARKER_END in line:
            await self._handle_end(line)
            return
        if _MARKER_PROMPT in line:
            await self._handle_prompt(line)
            return
        if self._active:
            # Preserve exact newlines by writing the line as-is; file is jsonl-ish but used as raw text.
            out_path = Path(self._active.output_path)
            await asyncio.to_thread(self._append_raw, out_path, line + "\n")
            await self._append_event(
                {
                    "type": "agent_block_delta",
                    "conversation_id": self.conversation_id,
                    "block_id": self._active.block_id,
                    "delta": line + "\n",
                }
            )

    async def _handle_prompt(self, line: str) -> None:
        """Handle prompt sentinel - transition from block_running/interactive to idle."""
        # If we were in interactive mode, end the session
        if self._mode == "interactive" and self._active:
            self._active.status = "completed"
            self._active.ts_end = _now_ms()
            await self._append_block_index(self._active)
            await self._append_event({
                "type": "agent_block_end",
                "conversation_id": self.conversation_id,
                "block": self._active.__dict__
            })
            self._active = None
            self._interactive_session_id = None
        self._mode = "idle"

    @staticmethod
    def _append_raw(path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(data)

    @staticmethod
    def _parse_kv(marker_line: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        parts = marker_line.strip().split()
        for part in parts[1:]:
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k] = v
        return out

    async def _handle_begin(self, line: str) -> None:
        kv = self._parse_kv(line)
        try:
            seq = int(kv.get("seq", "0"))
        except Exception:
            seq = 0
        try:
            ts = int(kv.get("ts", str(_now_ms())))
        except Exception:
            ts = _now_ms()
        cwd = _b64decode(kv.get("cwd_b64", ""))
        cmd = _b64decode(kv.get("cmd_b64", ""))
        block_id = f"{self.conversation_id}:{seq}:{ts}"
        out_file = _blocks_dir(self.conversation_id) / f"{seq}_{ts}.out"
        info = BlockInfo(
            block_id=block_id,
            conversation_id=self.conversation_id,
            seq=seq,
            ts_begin=ts,
            cwd=cwd,
            cmd=cmd,
            status="running",
            output_path=str(out_file),
        )
        self._active = info
        self._mode = "block_running"
        await self._append_event({"type": "agent_block_begin", "conversation_id": self.conversation_id, "block": info.__dict__})
        if self._begin_waiter and not self._begin_waiter.done():
            self._begin_waiter.set_result(info)

    async def _handle_end(self, line: str) -> None:
        kv = self._parse_kv(line)
        if not self._active:
            return
        try:
            seq = int(kv.get("seq", "0"))
        except Exception:
            seq = 0
        if seq and self._active.seq and seq != self._active.seq:
            return
        try:
            ts = int(kv.get("ts", str(_now_ms())))
        except Exception:
            ts = _now_ms()
        try:
            exit_code = int(kv.get("exit", "0"))
        except Exception:
            exit_code = None
        self._active.status = "completed"
        self._active.exit_code = exit_code
        self._active.ts_end = ts
        await self._append_block_index(self._active)
        await self._append_event({"type": "agent_block_end", "conversation_id": self.conversation_id, "block": self._active.__dict__})
        self._active = None
        self._mode = "idle"

    async def send_stdin(self, data: str) -> None:
        """Send raw bytes to PTY stdin."""
        if not self.shell_id:
            raise RuntimeError("No shell running")
        mgr = await get_framework_shell_manager()
        await mgr.write_to_pty(self.shell_id, data)

    async def wait_for(
        self,
        match: str,
        *,
        match_type: str = "substring",  # "substring", "regex", "prompt", "eof"
        from_cursor: int = 0,
        timeout_ms: int = 30000,
        max_bytes: int = 1024 * 1024,
    ) -> Dict[str, Any]:
        """
        Wait for a condition in output.
        Returns: {ok, matched, match_text, cursor}
        """
        import re
        
        await self._init_spool()
        
        # Build match function based on type
        # Returns: {matched, match_text, match_index, match_end, extra?} or None
        def make_matcher():
            if match_type == "prompt":
                def match_fn(data: str) -> Optional[Dict]:
                    if _MARKER_PROMPT in data:
                        idx = data.index(_MARKER_PROMPT)
                        end_idx = idx + len(_MARKER_PROMPT)
                        # Try to parse prompt fields for bonus info
                        extra = {}
                        try:
                            # Find the full line containing the prompt
                            line_end = data.find("\n", idx)
                            if line_end == -1:
                                line_end = len(data)
                            line = data[idx:line_end]
                            # Parse kv pairs like ts=123 cwd_b64=...
                            for part in line.split()[1:]:
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    if k == "cwd_b64":
                                        extra["cwd"] = _b64decode(v)
                                    elif k == "ts":
                                        extra["ts"] = int(v)
                        except Exception:
                            pass
                        return {"matched": True, "match_text": _MARKER_PROMPT, "match_index": idx, "match_end": end_idx, "extra": extra}
                    return None
                return match_fn
            elif match_type == "regex":
                pattern = re.compile(match)
                def match_fn(data: str) -> Optional[Dict]:
                    m = pattern.search(data)
                    if m:
                        return {"matched": True, "match_text": m.group(0), "match_index": m.start(), "match_end": m.end()}
                    return None
                return match_fn
            else:  # substring
                def match_fn(data: str) -> Optional[Dict]:
                    idx = data.find(match)
                    if idx >= 0:
                        return {"matched": True, "match_text": match, "match_index": idx, "match_end": idx + len(match)}
                    return None
                return match_fn
        
        match_fn = make_matcher()
        
        # First check existing spool data
        data, data_end_cursor = await self.read_spool(from_cursor, max_bytes)
        result = match_fn(data)
        if result:
            match_cursor = from_cursor + result["match_index"]
            match_end_cursor = from_cursor + result["match_end"]
            response = {
                "ok": True,
                "matched": True,
                "match_text": result["match_text"],
                "match_cursor": match_cursor,
                "match_span": {"start": match_cursor, "end": match_end_cursor},
                "next_cursor": data_end_cursor,  # Resume from end of scanned data
            }
            if result.get("extra"):
                response["extra"] = result["extra"]
            return response
        
        # Not found - register waiter
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters.append((match_fn, future, from_cursor))
        
        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return {"ok": True, **result}
        except asyncio.TimeoutError:
            # Return current cursor position even on timeout
            _, next_cursor = await self.read_spool(0, 0)  # Get current spool size
            return {"ok": False, "matched": False, "error": "timeout", "next_cursor": next_cursor}
        finally:
            # Clean up waiter if still present
            self._waiters = [(m, f, c) for (m, f, c) in self._waiters if f is not future]

    def get_status(self) -> Dict[str, Any]:
        """Get current PTY status."""
        return {
            "ok": True,
            "mode": self._mode,
            "active_session_id": self._interactive_session_id,
            "active_block_id": self._active.block_id if self._active else None,
            "shell_id": self.shell_id,
            "spool_cursor": self._spool_size,
        }


_states: Dict[str, ConversationState] = {}


def _state(conversation_id: str) -> ConversationState:
    st = _states.get(conversation_id)
    if not st:
        st = ConversationState(conversation_id)
        _states[conversation_id] = st
    return st


mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")


@mcp.tool(name="pty.exec", description="Execute a command (block mode) - waits for completion with BEGIN/END markers.")
async def pty_exec(conversation_id: str, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    state = _state(conversation_id)
    if state.mode == "interactive":
        return {"ok": False, "error": "PTY in interactive mode - use pty.send instead"}
    if state.mode == "block_running":
        return {"ok": False, "error": "PTY busy - block already running"}
    return await state.exec(cmd=cmd, cwd=cwd)


@mcp.tool(
    name="pty.exec_interactive",
    description="Start an interactive session - command runs without wrappers, use send+wait_for to interact."
)
async def pty_exec_interactive(conversation_id: str, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    """
    Start an interactive session (e.g. python REPL, vim, gdb).
    
    Returns session_id and cursor position. Use pty.send to send input,
    pty.wait_for to await output, and pty.end_session when done.
    """
    state = _state(conversation_id)
    if state.mode == "interactive":
        return {"ok": False, "error": "Already in interactive session"}
    if state.mode == "block_running":
        return {"ok": False, "error": "PTY busy - block already running"}
    return await state.exec_interactive(cmd=cmd, cwd=cwd)


@mcp.tool(name="pty.end_session", description="End an interactive session.")
async def pty_end_session(conversation_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    state = _state(conversation_id)
    return await state.end_session(session_id)


@mcp.tool(name="pty.send", description="Send raw bytes to PTY stdin (text, control chars, escape sequences).")
async def pty_send(conversation_id: str, data: str) -> Dict[str, Any]:
    """
    Send raw data to the PTY.
    Supports text, newlines (use \\n or \\r), and control chars (e.g. \\x03 for Ctrl+C).
    """
    state = _state(conversation_id)
    try:
        await state.ensure_shell()
        await state.send_stdin(data)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty.ctrl_c", description="Send Ctrl+C (SIGINT) to PTY.")
async def pty_ctrl_c(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\x03")


@mcp.tool(name="pty.ctrl_d", description="Send Ctrl+D (EOF) to PTY.")
async def pty_ctrl_d(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\x04")


@mcp.tool(name="pty.enter", description="Send Enter/newline to PTY.")
async def pty_enter(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\r")


@mcp.tool(
    name="pty.wait_for",
    description="Wait for a condition in PTY output. Returns when match found or timeout."
)
async def pty_wait_for(
    conversation_id: str,
    match: str,
    match_type: str = "substring",
    from_cursor: int = 0,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """
    Wait for output condition.
    
    Args:
        match: The pattern to match (substring, regex pattern, or ignored for 'prompt' type)
        match_type: 'substring', 'regex', or 'prompt' (wait for shell prompt)
        from_cursor: Byte offset in output spool to start searching from
        timeout_ms: Timeout in milliseconds (default 30s)
    
    Returns on match:
        {ok: true, matched: true, match_text, match_cursor, match_span: {start, end}, next_cursor, extra?}
        - match_cursor: byte offset where match starts (for bookmarking)
        - match_span: {start, end} byte offsets of the match
        - next_cursor: byte offset to use for next wait_for (end of scanned data)
        - extra: for prompt matches, includes parsed {cwd, ts}
    
    Returns on timeout:
        {ok: false, matched: false, error: "timeout", next_cursor}
    """
    state = _state(conversation_id)
    try:
        await state.ensure_shell()
        return await state.wait_for(
            match=match,
            match_type=match_type,
            from_cursor=from_cursor,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty.status", description="Get PTY status: mode, active block/session, cursor position.")
async def pty_status(conversation_id: str) -> Dict[str, Any]:
    state = _state(conversation_id)
    return state.get_status()


@mcp.tool(name="pty.read_spool", description="Read raw output from the conversation spool at a cursor position.")
async def pty_read_spool(
    conversation_id: str,
    from_cursor: int = 0,
    max_bytes: int = 65536,
) -> Dict[str, Any]:
    """Read output spool from cursor position."""
    state = _state(conversation_id)
    try:
        await state._init_spool()
        data, next_cursor = await state.read_spool(from_cursor, max_bytes)
        return {"ok": True, "data": data, "cursor": from_cursor, "next_cursor": next_cursor}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="blocks.since", description="List blocks since a byte cursor in blocks.jsonl (per conversation).")
async def blocks_since(conversation_id: str, cursor: int = 0, limit: int = 50) -> Dict[str, Any]:
    path = _blocks_index_path(conversation_id)
    if not path.exists():
        return {"ok": True, "cursor": 0, "next_cursor": 0, "items": []}
    cursor = max(0, int(cursor))
    limit = max(1, min(int(limit), 200))
    data = await asyncio.to_thread(path.read_bytes)
    if cursor > len(data):
        cursor = len(data)
    tail = data[cursor:]
    lines = tail.splitlines()[:limit]
    items = []
    for raw in lines:
        try:
            items.append(json.loads(raw))
        except Exception:
            continue
    # Advance cursor to end of the consumed lines
    consumed = b"\n".join(lines)
    next_cursor = cursor + len(consumed) + (1 if lines else 0)
    return {"ok": True, "cursor": cursor, "next_cursor": next_cursor, "items": items}


@mcp.tool(name="blocks.read", description="Read raw output bytes from a block output file.")
async def blocks_read(conversation_id: str, block_id: str, offset: int = 0, max_bytes: int = 65536) -> Dict[str, Any]:
    max_bytes = max(1, min(int(max_bytes), 512 * 1024))
    offset = max(0, int(offset))
    # Resolve output_path by scanning blocks.jsonl backwards (cheap enough for now)
    meta = await blocks_get(conversation_id, block_id)
    if not meta.get("ok") or not meta.get("block"):
        return {"ok": False, "error": "block not found"}
    out_path = meta["block"].get("output_path")
    if not out_path:
        return {"ok": False, "error": "no output path"}
    path = Path(out_path)
    if not path.exists():
        return {"ok": False, "error": "output missing"}
    data = await asyncio.to_thread(path.read_bytes)
    if offset > len(data):
        offset = len(data)
    chunk = data[offset : offset + max_bytes]
    return {"ok": True, "offset": offset, "next_offset": offset + len(chunk), "data": chunk.decode("utf-8", errors="replace")}


@mcp.tool(name="blocks.get", description="Get metadata for a block id (from blocks.jsonl).")
async def blocks_get(conversation_id: str, block_id: str) -> Dict[str, Any]:
    path = _blocks_index_path(conversation_id)
    if not path.exists():
        return {"ok": False, "error": "no blocks yet"}
    try:
        lines = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    except Exception:
        return {"ok": False, "error": "read failed"}
    # scan backwards for latest matching block_id
    for raw in reversed(lines.splitlines()):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("block_id") == block_id:
            return {"ok": True, "block": obj}
    return {"ok": False, "error": "block not found"}


@mcp.tool(name="blocks.search", description="Search within a block's output for a substring; returns matching line snippets.")
async def blocks_search(conversation_id: str, block_id: str, query: str, limit: int = 50) -> Dict[str, Any]:
    meta = await blocks_get(conversation_id, block_id)
    if not meta.get("ok") or not meta.get("block"):
        return {"ok": False, "error": "block not found"}
    out_path = meta["block"].get("output_path")
    if not out_path:
        return {"ok": False, "error": "no output path"}
    path = Path(out_path)
    if not path.exists():
        return {"ok": False, "error": "output missing"}
    query = str(query or "")
    limit = max(1, min(int(limit), 200))
    text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    hits = []
    for i, line in enumerate(text.splitlines()):
        if query in line:
            hits.append({"line": i + 1, "text": line})
            if len(hits) >= limit:
                break
    return {"ok": True, "hits": hits}


async def _main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())

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
    #
    # We rely on base64 + tr to avoid quoting issues.
    content = r"""
__FWS_SEQ=0
__FWS_LAST_SEQ=""
__FWS_IN_MARKER=0

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

__fws_should_ignore_cmd() {
  local cmd="$1"
  case "$cmd" in
    *__FWS_BLOCK_BEGIN__*|*__FWS_BLOCK_END__*) return 0 ;;
    __fws_*|__FWS_*) return 0 ;;
  esac
  return 1
}

trap '__fws_preexec' DEBUG
__fws_preexec() {
  if [ "${__FWS_IN_MARKER}" = "1" ]; then return 0; fi
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
  if [ -z "${__FWS_LAST_SEQ}" ]; then return 0; fi
  __FWS_IN_MARKER=1
  local exit_code="$?"
  local ts="$(__fws_now_ms)"
  __fws_emit_end "$exit_code" "$ts" "$__FWS_LAST_SEQ"
  __FWS_LAST_SEQ=""
  __FWS_IN_MARKER=0
}

PROMPT_COMMAND="__fws_precmd"
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


class ConversationState:
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.lock = asyncio.Lock()
        self.shell_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer = ""
        self._active: Optional[BlockInfo] = None
        self._begin_waiter: Optional[asyncio.Future] = None

    async def ensure_shell(self, *, cwd: Optional[str] = None) -> str:
        async with self.lock:
            if self.shell_id:
                return self.shell_id
            mgr = await get_framework_shell_manager()
            root = _agent_pty_root(self.conversation_id)
            root.mkdir(parents=True, exist_ok=True)
            rcfile = _rcfile_path(self.conversation_id)
            _write_rcfile(rcfile)
            command = ["bash", "--rcfile", str(rcfile), "-i"]
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
        async with self.lock:
            loop = asyncio.get_running_loop()
            self._begin_waiter = loop.create_future()
            await mgr.write_to_pty(self.shell_id, cmd.rstrip("\n") + "\n")
        try:
            info: BlockInfo = await asyncio.wait_for(self._begin_waiter, timeout=3.0)
        finally:
            async with self.lock:
                self._begin_waiter = None
        return {"ok": True, "block_id": info.block_id, "seq": info.seq, "ts": info.ts_begin}

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


_states: Dict[str, ConversationState] = {}


def _state(conversation_id: str) -> ConversationState:
    st = _states.get(conversation_id)
    if not st:
        st = ConversationState(conversation_id)
        _states[conversation_id] = st
    return st


mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")


@mcp.tool(name="pty.exec", description="Execute a command in the agent-owned per-conversation PTY; returns a block id.")
async def pty_exec(conversation_id: str, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    return await _state(conversation_id).exec(cmd=cmd, cwd=cwd)


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

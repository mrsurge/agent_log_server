#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import json
import os
import sys
import secrets
import time
import atexit
import contextlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pyte
import pyte.modes


def _ensure_framework_shells_secret() -> None:
    """Derive a stable secret from cwd/repo root if not already set."""
    # Prefer SIGWINCH delivery after resize_pty() for dtach-backed PTYs.
    os.environ.setdefault("FRAMEWORK_SHELLS_SIGWINCH_ON_RESIZE", "1")
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    repo_root = str(Path(__file__).resolve().parent)
    fingerprint = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
    base_dir = Path(os.path.expanduser("~/.cache/framework_shells"))
    secret_dir = base_dir / "runtimes" / fingerprint
    secret_file = secret_dir / "secret"
    if secret_file.exists():
        secret = secret_file.read_text(encoding="utf-8").strip()
    else:
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        secret_file.write_text(secret, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except Exception:
            pass
    os.environ["FRAMEWORK_SHELLS_SECRET"] = secret
    os.environ["FRAMEWORK_SHELLS_REPO_FINGERPRINT"] = fingerprint
    os.environ["FRAMEWORK_SHELLS_BASE_DIR"] = str(base_dir)
    os.environ.setdefault("FRAMEWORK_SHELLS_RUN_ID", "app-server")

# Auto-set secret before importing framework_shells
_ensure_framework_shells_secret()

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

def _marker_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "markers.log"


# Sprint 2: Screen model paths
def _screen_events_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "screen.jsonl"


def _screen_size_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "screen_size.json"


def _shell_id_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "shell_id.txt"

def _shell_manager_registry_path() -> Path:
    return Path(os.path.expanduser("~/.cache/app_server/shell_manager.json"))

def _load_shell_manager_registry() -> Optional[Dict[str, Any]]:
    path = _shell_manager_registry_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None

def _shell_manager_url() -> Optional[str]:
    env_url = os.environ.get("SHELL_MANAGER_URL")
    if env_url:
        return env_url
    registry = _load_shell_manager_registry()
    if registry and registry.get("url"):
        return str(registry["url"])
    host = os.environ.get("SHELL_MANAGER_HOST")
    port = os.environ.get("SHELL_MANAGER_PORT")
    if host and port:
        return f"http://{host}:{port}"
    return None

def _manager_run_id() -> str:
    return os.environ.get("FRAMEWORK_SHELLS_RUN_ID") or "app-server"

@contextlib.contextmanager
def _redirect_stdout_to_stderr() -> Any:
    """
    Redirect stdout to stderr inside a narrow scope.

    Why: framework_shells currently prints some lifecycle messages to stdout,
    which is fatal for STDIO MCP (stdout must be JSON-RPC only).
    """

    class _StdoutToStderr(io.TextIOBase):
        def write(self, s: str) -> int:
            return sys.stderr.write(s)

        def flush(self) -> None:
            sys.stderr.flush()

    with contextlib.redirect_stdout(_StdoutToStderr()):
        yield

async def _get_fws_manager():
    with _redirect_stdout_to_stderr():
        return await get_framework_shell_manager(run_id=_manager_run_id())

async def _shell_manager_request(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url_base = _shell_manager_url()
    if not url_base:
        raise RuntimeError("shell manager not discovered (no registry or env url)")
    url = url_base.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    def _post() -> Dict[str, Any]:
        import urllib.request
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    return await asyncio.to_thread(_post)


def _shell_manager_request_sync(path: str, payload: Dict[str, Any], *, timeout_s: float = 0.5) -> Optional[Dict[str, Any]]:
    """Best-effort synchronous request (used from atexit)."""
    url_base = _shell_manager_url()
    if not url_base:
        return None
    url = url_base.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        import urllib.request
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


_MARKER_BEGIN = "__FWS_BLOCK_BEGIN__"
_MARKER_END = "__FWS_BLOCK_END__"


def _write_rcfile(path: Path, marker_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Marker format:
    #   __FWS_BLOCK_BEGIN__ seq=<n> ts=<ms> cwd_b64=<...> cmd_b64=<...>
    #   __FWS_BLOCK_END__ seq=<n> ts=<ms> exit=<code>
    #   __FWS_PROMPT__ ts=<ms> cwd_b64=<...>
    #
    # We rely on base64 + tr to avoid quoting issues.
    content = r"""
# Termux guard: ensure env + shebang compatibility
if [ -n "${PREFIX:-}" ] && [ -x "${PREFIX}/bin/env" ]; then
  export PATH="${PREFIX}/bin:${PATH}"
  if [ -z "${TERMUX_VERSION:-}" ]; then
    export TERMUX_VERSION="1"
  fi
  if [ -f "${PREFIX}/lib/libtermux-exec.so" ]; then
    export LD_PRELOAD="${PREFIX}/lib/libtermux-exec.so"
  fi
elif [ -d "/data/data/com.termux/files/usr" ]; then
  export PATH="/data/data/com.termux/files/usr/bin:${PATH}"
  if [ -z "${TERMUX_VERSION:-}" ]; then
    export TERMUX_VERSION="1"
  fi
  if [ -f "/data/data/com.termux/files/usr/lib/libtermux-exec.so" ]; then
    export LD_PRELOAD="/data/data/com.termux/files/usr/lib/libtermux-exec.so"
  fi
fi

__FWS_MARKER_FILE="__FWS_MARKER_FILE_PATH__"
: > "$__FWS_MARKER_FILE"
exec 3>>"$__FWS_MARKER_FILE"

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
  printf '\n__FWS_BLOCK_BEGIN__ seq=%s ts=%s cwd_b64=%s cmd_b64=%s\n' "$seq" "$ts" "$cwd_b64" "$cmd_b64" >&3
}

__fws_emit_end() {
  local exit_code="$1"
  local ts="$2"
  local seq="$3"
  printf '\n__FWS_BLOCK_END__ seq=%s ts=%s exit=%s\n' "$seq" "$ts" "$exit_code" >&3
}

__fws_emit_prompt() {
  local exit_code="${1:-$?}"
  local ts="$(__fws_now_ms)"
  local cwd="$(pwd -P 2>/dev/null || pwd)"
  local cwd_b64="$(__fws_b64 "$cwd")"
  printf '\n__FWS_PROMPT__ ts=%s cwd_b64=%s exit=%s\n' "$ts" "$cwd_b64" "$exit_code" >&3
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
    local ec="$?"
    __fws_emit_prompt "$ec"
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
    __fws_emit_prompt "$exit_code"
  }

  PROMPT_COMMAND="__fws_precmd"
fi

PS1="agent-pty> "
"""
    content = content.replace("__FWS_MARKER_FILE_PATH__", str(marker_path))
    path.write_text(content.lstrip(), encoding="utf-8")


def _termux_env_overrides() -> Dict[str, str]:
    prefix = os.environ.get("PREFIX")
    if not prefix or not Path(prefix).exists():
        prefix = "/data/data/com.termux/files/usr"
    if not Path(prefix).exists():
        return {}
    env: Dict[str, str] = {}
    env["PATH"] = f"{prefix}/bin:" + os.environ.get("PATH", "")
    env["TERMUX_VERSION"] = os.environ.get("TERMUX_VERSION", "1")
    ld_preload = f"{prefix}/lib/libtermux-exec.so"
    if Path(ld_preload).exists():
        env["LD_PRELOAD"] = ld_preload
    return env


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

def _raw_events_path(conversation_id: str) -> Path:
    """Raw chunk event stream for UI playback (base64 bytes)."""
    return _agent_pty_root(conversation_id) / "raw_events.jsonl"


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
        
        # === Sprint 1: Screen model (pyte) ===
        # We keep two screen models to represent primary + alternate screen buffers.
        # Alt-screen is common for TUIs and must not corrupt the primary scrollback.
        self._screen_main: Optional[pyte.HistoryScreen] = None
        self._stream_main: Optional[pyte.ByteStream] = None
        self._screen_alt: Optional[pyte.HistoryScreen] = None
        self._stream_alt: Optional[pyte.ByteStream] = None
        self._in_alt_screen: bool = False
        self._ansi_mode_buf: bytes = b""  # carryover for split CSI ? 1049 h/l sequences
        self._screen_cols: int = 120
        self._screen_rows: int = 40
        self._pending_dirty_rows: set = set()
        self._screen_size_loaded: bool = False
        # Scrollback (HistoryScreen)
        self._scrollback_limit: int = 2000
        
        # Raw byte stream (truly lossless via subscribe_output_bytes)
        self._raw_path: Optional[Path] = None
        self._raw_size: int = 0
        # How many raw bytes have been fed into the screen model
        self._screen_raw_size: int = 0
        self._bytes_queue: Optional[asyncio.Queue] = None
        self._bytes_reader_task: Optional[asyncio.Task] = None
        # Marker sideband (stdout markers moved to fd3)
        self._marker_path: Optional[Path] = None
        self._marker_task: Optional[asyncio.Task] = None
        self._marker_buffer: bytes = b""
        
        # Dedicated lock for screen operations (avoid blocking wait_for)
        self._screen_lock = asyncio.Lock()
        
        # === Sprint 2: Screen delta rate limiting ===
        self._last_screen_delta_ts: float = 0.0
        self._screen_delta_min_interval: float = 0.1  # 100ms = max 10/sec
        self._screen_delta_task: Optional[asyncio.Task] = None

    async def _load_persisted_screen_size(self) -> None:
        """Best-effort load of persisted screen size for this conversation."""
        if self._screen_size_loaded:
            return
        self._screen_size_loaded = True
        path = _screen_size_path(self.conversation_id)
        if not path.exists():
            return
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            data = json.loads(raw)
            cols = int(data.get("cols") or 0)
            rows = int(data.get("rows") or 0)
            if cols > 0 and rows > 0:
                self._screen_cols = cols
                self._screen_rows = rows
        except Exception:
            return

    async def _save_persisted_screen_size(self) -> None:
        """Best-effort persist of current screen size for this conversation."""
        path = _screen_size_path(self.conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cols": int(self._screen_cols), "rows": int(self._screen_rows), "ts": _now_ms()}
        try:
            await asyncio.to_thread(path.write_text, json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            return

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
            if not self._spool_path.exists():
                self._spool_path.write_bytes(b"")
        # Always refresh from disk: multiple processes can append to the same spool.
        try:
            self._spool_size = self._spool_path.stat().st_size
        except Exception:
            pass

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
            # Refresh size under lock (external writers may have appended).
            try:
                self._spool_size = self._spool_path.stat().st_size
            except Exception:
                pass
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
        for i, (match_fn, future, from_cursor, match_type) in enumerate(self._waiters):
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
                        "resume_cursor": match_end_cursor,
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

    # === Sprint 1: Screen model methods ===
    
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

    async def _refresh_raw_size(self) -> None:
        """Refresh raw file size from disk."""
        await self._init_raw()
        try:
            self._raw_size = self._raw_path.stat().st_size
        except Exception:
            pass

    async def _append_raw(self, data: bytes) -> int:
        """Append raw bytes (lossless), return new size."""
        async with self._screen_lock:
            await self._init_raw()
            await asyncio.to_thread(self._append_bytes, self._raw_path, data)
            self._raw_size += len(data)
            return self._raw_size

    async def _append_raw_event(self, data: bytes) -> None:
        """Append raw chunk event (base64) for UI playback."""
        path = _raw_events_path(self.conversation_id)
        payload = {
            "type": "agent_pty_raw",
            "conversation_id": self.conversation_id,
            "block_id": self._active.block_id if self._active else None,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "ts": _now_ms(),
        }
        await asyncio.to_thread(self._append_text_line, path, json.dumps(payload, ensure_ascii=False) + "\n")

    def _active_screen(self) -> tuple[pyte.HistoryScreen, pyte.ByteStream]:
        """Return the active (main/alt) screen + stream, creating on demand."""
        if self._in_alt_screen:
            if self._screen_alt is None or self._stream_alt is None:
                self._screen_alt = pyte.HistoryScreen(self._screen_cols, self._screen_rows, history=self._scrollback_limit)
                self._stream_alt = pyte.ByteStream(self._screen_alt)
            return (self._screen_alt, self._stream_alt)
        if self._screen_main is None or self._stream_main is None:
            self._screen_main = pyte.HistoryScreen(self._screen_cols, self._screen_rows, history=self._scrollback_limit)
            self._stream_main = pyte.ByteStream(self._screen_main)
        return (self._screen_main, self._stream_main)

    def _mark_full_dirty(self) -> None:
        self._pending_dirty_rows.update(range(self._screen_rows))

    def _set_alt_screen(self, enabled: bool) -> None:
        if bool(enabled) == self._in_alt_screen:
            return
        self._in_alt_screen = bool(enabled)
        # Ensure target screen exists; when entering alt screen, reset it to match
        # typical TUI behavior (alt buffer starts fresh).
        screen, _ = self._active_screen()
        if enabled:
            try:
                screen.reset()
            except Exception:
                pass
        # Force a full repaint on mode switch.
        self._mark_full_dirty()

    _ALTBUF_RE = re.compile(rb"\x1b\[\?([0-9]{4})([hl])")

    def _feed_screen(self, data: bytes) -> set:
        """Feed bytes to the active pyte screen, tracking alt-screen transitions."""
        # Stitch with any split CSI prefix from the previous chunk.
        buf = self._ansi_mode_buf + (data or b"")
        self._ansi_mode_buf = b""

        dirty_total: set[int] = set()
        pos = 0

        for m in self._ALTBUF_RE.finditer(buf):
            start, end = m.span()
            segment = buf[pos:start]
            if segment:
                screen, stream = self._active_screen()
                try:
                    stream.feed(segment)
                except Exception:
                    pass
                dirty_total.update(getattr(screen, "dirty", set()))
            code = m.group(1)
            action = m.group(2)
            # Only treat 1049/1047 as alt-buffer toggles. 1048 is save/restore cursor.
            if code in (b"1049", b"1047"):
                self._set_alt_screen(action == b"h")
            pos = end

        tail = buf[pos:]
        # Preserve potentially split ESC[?1049h sequence at end of chunk.
        idx = tail.rfind(b"\x1b[?")
        if idx != -1 and (len(tail) - idx) < 10:
            feed_part = tail[:idx]
            self._ansi_mode_buf = tail[idx:]
        else:
            feed_part = tail
            self._ansi_mode_buf = b""
        if len(self._ansi_mode_buf) > 64:
            self._ansi_mode_buf = self._ansi_mode_buf[-64:]

        if feed_part:
            screen, stream = self._active_screen()
            try:
                stream.feed(feed_part)
            except Exception:
                pass
            dirty_total.update(getattr(screen, "dirty", set()))

        self._screen_raw_size += len(data or b"")
        self._pending_dirty_rows.update(dirty_total)
        return dirty_total

    def _get_screen_row(self, row: int) -> str:
        """Get text content of a screen row (0-indexed), with markers filtered."""
        screen, _ = self._active_screen()
        if screen is None:
            return ""
        # Use screen.display[row] for correct column-ordered string
        text = screen.display[row].rstrip()
        # Filter out shell markers that shouldn't appear in screen output
        if "__FWS_BLOCK_BEGIN__" in text or "__FWS_BLOCK_END__" in text or "__FWS_PROMPT__" in text:
            return ""
        # Filter manual wrapper fragments injected by pty_exec.
        # These can wrap across multiple rows, so match on multiple stable substrings.
        wrapper_markers = (
            "__fws_cmd=",
            "__fws_emit_begin",
            "__fws_emit_end",
            "__FWS_SEQ=",
            "__FWS_LAST_SEQ",
            "__FWS_IN_MARKER",
            "__fws_seq=",
            "$__fws_seq",
            "__fws_ts=",
            "$__fws_ts",
            "__fws_ts2=",
            "$__fws_ts2",
            "__fws_cwd=",
            "$__fws_cwd",
            "eval \"$__fws_cmd\"",
            "base64 -d",
            "2>/dev/null",
        )
        if any(m in text for m in wrapper_markers):
            return ""
        return text

    def _is_alt_screen(self) -> bool:
        """Check if terminal is in alternate screen mode."""
        return bool(self._in_alt_screen)

    def _get_screen_snapshot(self) -> dict:
        """Get full screen state as dict."""
        screen, _ = self._active_screen()
        rows = []
        for i in range(self._screen_rows):
            rows.append(self._get_screen_row(i))
        return {
            "rows": rows,
            "cursor": {"row": screen.cursor.y, "col": screen.cursor.x},
            "title": getattr(screen, 'title', '') or "",
            "alt_screen": self._is_alt_screen(),
            "cols": self._screen_cols,
            "rows_count": self._screen_rows,
            "raw_size": self._screen_raw_size,
            "ts": _now_ms(),
        }

    def _render_history_line(self, line: Any) -> str:
        """
        Best-effort render of a HistoryScreen line to plain text.

        In pyte 0.8.x, history lines are Screen buffer line objects (dict-like,
        mapping column -> Char). We render by sorting columns and concatenating
        Char.data, then rstrip trailing whitespace.
        """
        try:
            if hasattr(line, "items"):
                parts = []
                for _, ch in sorted(line.items()):  # type: ignore[arg-type]
                    parts.append(getattr(ch, "data", str(ch)))
                return "".join(parts).rstrip()
        except Exception:
            pass
        try:
            return str(line).rstrip()
        except Exception:
            return ""

    def _get_scrollback_snapshot(self, limit: int = 200) -> Dict[str, Any]:
        """Return a snapshot of scrollback (top history) plus metadata."""
        screen, _ = self._active_screen()
        limit = max(1, min(int(limit), 2000))
        lines: list[str] = []
        total = 0
        try:
            hist = getattr(screen, "history", None)
            top = getattr(hist, "top", None)
            if top is not None:
                total = len(top)
                for line in list(top)[-limit:]:
                    lines.append(self._render_history_line(line))
        except Exception:
            pass
        return {
            "ok": True,
            "lines": lines,
            "scrollback_total": total,
            "cols": self._screen_cols,
            "rows_count": self._screen_rows,
            "ts": _now_ms(),
        }

    async def _rehydrate_screen_from_raw(self, upto: Optional[int] = None) -> None:
        """Rebuild screen model from raw bytes on disk."""
        await self._refresh_raw_size()
        raw_size = self._raw_size if upto is None else min(self._raw_size, int(upto))
        # Fresh screens
        self._screen_main = pyte.HistoryScreen(self._screen_cols, self._screen_rows, history=self._scrollback_limit)
        self._stream_main = pyte.ByteStream(self._screen_main)
        self._screen_alt = None
        self._stream_alt = None
        self._in_alt_screen = False
        self._ansi_mode_buf = b""
        self._pending_dirty_rows.clear()
        self._screen_raw_size = 0
        if raw_size <= 0:
            return
        offset = 0
        chunk_size = 1024 * 1024
        while offset < raw_size:
            to_read = min(chunk_size, raw_size - offset)
            data = await asyncio.to_thread(self._read_bytes, self._raw_path, offset, to_read)
            if not data:
                break
            self._feed_screen(data)
            offset += len(data)
        for s in (self._screen_main, self._screen_alt):
            if s:
                try:
                    s.dirty.clear()
                except Exception:
                    pass

    async def _load_shell_id(self) -> Optional[str]:
        """Load cached shell id from disk."""
        path = _shell_id_path(self.conversation_id)
        if not path.exists():
            return None
        try:
            data = await asyncio.to_thread(path.read_text, encoding="utf-8")
            shell_id = data.strip()
            return shell_id or None
        except Exception:
            return None

    async def _save_shell_id(self, shell_id: str) -> None:
        """Persist shell id to disk."""
        path = _shell_id_path(self.conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(path.write_text, shell_id, encoding="utf-8")
        except Exception:
            pass

    # === Sprint 2: Screen delta events ===

    async def _emit_screen_delta(self) -> None:
        """Emit screen delta event (rate-limited). Flushes _pending_dirty_rows."""
        now = time.time()
        
        # Rate limit (skip if too soon)
        if now - self._last_screen_delta_ts < self._screen_delta_min_interval:
            if self._pending_dirty_rows:
                delay = self._screen_delta_min_interval - (now - self._last_screen_delta_ts)
                if not self._screen_delta_task or self._screen_delta_task.done():
                    async def _delayed_flush() -> None:
                        await asyncio.sleep(max(0.0, delay))
                        await self._emit_screen_delta()
                    self._screen_delta_task = asyncio.create_task(_delayed_flush())
            return
        
        if not self._pending_dirty_rows:
            return
        
        async with self._screen_lock:
            screen, _ = self._active_screen()
            # Build delta event from buffered dirty rows
            rows_data = []
            for row_idx in sorted(self._pending_dirty_rows):
                if 0 <= row_idx < self._screen_rows:
                    rows_data.append({
                        "row": row_idx,
                        "text": self._get_screen_row(row_idx),
                    })
            
            event = {
                "type": "screen_delta",
                "conversation_id": self.conversation_id,
                "block_id": self._active.block_id if self._active else None,
                "rows": rows_data,
                "cursor": {"row": screen.cursor.y, "col": screen.cursor.x},
                "title": getattr(screen, "title", "") or "",
                "alt_screen": self._is_alt_screen(),
                "cols": self._screen_cols,
                "rows_count": self._screen_rows,
                "ts": _now_ms(),
            }
            
            # Write to screen.jsonl
            path = _screen_events_path(self.conversation_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, ensure_ascii=False)
            await asyncio.to_thread(self._append_text_line, path, line + "\n")
            
        # Clear pending dirty rows and pyte's dirty set
        self._pending_dirty_rows.clear()
        for s in (self._screen_main, self._screen_alt):
            if s:
                try:
                    s.dirty.clear()
                except Exception:
                    pass
        self._last_screen_delta_ts = now

    async def _flush_screen_state(self) -> None:
        """Force flush any pending screen state (call on session end)."""
        # Force emit regardless of rate limit
        self._last_screen_delta_ts = 0
        await self._emit_screen_delta()

    async def _ensure_bytes_reader(self, mgr) -> None:
        """Subscribe to raw bytes stream from PTY (truly lossless)."""
        if self._bytes_reader_task and not self._bytes_reader_task.done():
            return
        
        # Require subscribe_output_bytes (framework_shells >= 0.0.4)
        if not hasattr(mgr, 'subscribe_output_bytes'):
            raise RuntimeError("subscribe_output_bytes() is required for lossless raw bytes")
        
        self._bytes_queue = await mgr.subscribe_output_bytes(self.shell_id)
        
        async def _run_bytes() -> None:
            while True:
                chunk_bytes: bytes = await self._bytes_queue.get()
                # Append raw bytes directly (truly lossless)
                await self._append_raw(chunk_bytes)
                await self._append_raw_event(chunk_bytes)
                # Feed raw bytes directly to pyte.ByteStream
                try:
                    async with self._screen_lock:
                        self._feed_screen(chunk_bytes)
                    # Sprint 2: Emit screen delta (rate-limited)
                    await self._emit_screen_delta()
                except Exception:
                    pass  # pyte may choke; raw bytes already saved
        
        self._bytes_reader_task = asyncio.create_task(
            _run_bytes(), 
            name=f"agent-pty-bytes-reader:{self.conversation_id}"
        )

    async def _ensure_marker_reader(self) -> None:
        """Read marker lines from sideband file (fd3) instead of stdout."""
        if self._marker_task and not self._marker_task.done():
            return
        if self._marker_path is None:
            self._marker_path = _marker_path(self.conversation_id)
        path = self._marker_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"")

        async def _run_markers() -> None:
            offset = 0
            buffer = b""
            while True:
                try:
                    if not path.exists():
                        await asyncio.sleep(0.5)
                        continue
                    raw = await asyncio.to_thread(path.read_bytes)
                    if offset > len(raw):
                        offset = 0
                    tail = raw[offset:]
                    if not tail:
                        await asyncio.sleep(0.2)
                        continue
                    offset = len(raw)
                    buffer += tail
                    if b"\n" not in buffer:
                        continue
                    parts = buffer.split(b"\n")
                    buffer = parts.pop()
                    for part in parts:
                        line = part.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        await self._append_spool(line + "\n")
                        await self._check_waiters(line)
                        async with self.lock:
                            if _MARKER_BEGIN in line:
                                await self._handle_begin(line)
                                continue
                            if _MARKER_END in line:
                                await self._handle_end(line)
                                continue
                            if _MARKER_PROMPT in line:
                                await self._handle_prompt(line)
                                continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(0.5)

        self._marker_task = asyncio.create_task(
            _run_markers(),
            name=f"agent-pty-markers:{self.conversation_id}"
        )

    async def ensure_shell(self, *, cwd: Optional[str] = None) -> str:
        async with self.lock:
            mgr = await _get_fws_manager()
            self._marker_path = _marker_path(self.conversation_id)
            # Load per-conversation preferred screen size (if any) before attach/resize.
            await self._load_persisted_screen_size()
            if self.shell_id:
                # Ensure dtach attach proxy has a sane winsize for pyte parity.
                try:
                    await mgr.resize_pty(self.shell_id, self._screen_cols, self._screen_rows)
                except Exception:
                    pass
                await self._ensure_reader(mgr)
                await self._ensure_marker_reader()
                return self.shell_id
            # Try cached shell id (reattach)
            cached_id = await self._load_shell_id()
            if cached_id:
                try:
                    rec = await mgr.get_shell(cached_id)
                    if rec and rec.status == "running":
                        self.shell_id = rec.id
                        # Ensure dtach attach proxy has a sane winsize for pyte parity.
                        try:
                            await mgr.resize_pty(self.shell_id, self._screen_cols, self._screen_rows)
                        except Exception:
                            pass
                        await self._ensure_reader(mgr)
                        await self._ensure_marker_reader()
                        return self.shell_id
                except Exception:
                    pass
            # Request shell from external shell manager (attach-only, no local spawn).
            info = await _shell_manager_request(
                "/shells/ensure",
                {
                    "conversation_id": self.conversation_id,
                    "cwd": cwd or str(Path.cwd()),
                },
            )
            shell_id = info.get("shell_id")
            if not shell_id:
                raise RuntimeError("shell manager did not return shell_id")
            rec = await mgr.get_shell(shell_id)
            if not rec or rec.status != "running":
                raise RuntimeError("shell manager returned non-running shell")
            self.shell_id = rec.id
            await self._save_shell_id(rec.id)
            # Ensure dtach attach proxy has a sane winsize for pyte parity.
            try:
                await mgr.resize_pty(self.shell_id, self._screen_cols, self._screen_rows)
            except Exception:
                pass
            await self._ensure_reader(mgr)
            await self._ensure_marker_reader()
            return self.shell_id

    async def _ensure_reader(self, mgr) -> None:
        if self._reader_task and not self._reader_task.done():
            return
        q = await mgr.subscribe_output(self.shell_id)
        
        # Sprint 1: Also start bytes reader for lossless raw stream + pyte
        await self._ensure_bytes_reader(mgr)

        async def _run() -> None:
            while True:
                chunk = await q.get()
                await self._on_chunk(chunk)

        self._reader_task = asyncio.create_task(_run(), name=f"agent-pty-reader:{self.conversation_id}")

    async def exec(self, *, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
        await self.ensure_shell(cwd=cwd)
        mgr = await _get_fws_manager()
        cmd_b64 = base64.b64encode(cmd.encode("utf-8", errors="replace")).decode("ascii")
        async with self.lock:
            loop = asyncio.get_running_loop()
            self._begin_waiter = loop.create_future()
            # Clear screen BEFORE running the command so the final screen reflects only this run.
            await mgr.write_to_pty(self.shell_id, "\x1b[2J\x1b[H")
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
        mgr = await _get_fws_manager()
        
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
            # Clear screen BEFORE interactive command so the prompt snapshot is just this run.
            await mgr.write_to_pty(self.shell_id, "\x1b[2J\x1b[H")
            await mgr.write_to_pty(self.shell_id, cmd + "\n")
        
        # Get current spool size so agent can wait_for from here
        await self._init_spool()
        
        return {
            "ok": True,
            "session_id": self._interactive_session_id,
            "block_id": block_id,
            "ts_begin": ts,
            "resume_cursor": self._spool_size,
        }

    async def end_session(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """End an interactive session."""
        if self._mode != "interactive":
            return {"ok": False, "error": "No interactive session active"}
        if session_id and session_id != self._interactive_session_id:
            return {"ok": False, "error": "Session ID mismatch"}
        
        # Try graceful exit with Ctrl+C
        await self.send_stdin("\x03")
        
        # Sprint 2: Flush screen state before ending
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
        if self._active:
            # Preserve exact newlines by writing the line as-is; file is jsonl-ish but used as raw text.
            out_path = Path(self._active.output_path)
            await asyncio.to_thread(self._append_text_line, out_path, line + "\n")
            await self._append_event(
                {
                    "type": "agent_block_delta",
                    "conversation_id": self.conversation_id,
                    "block_id": self._active.block_id,
                    "delta": line + "\n",
                }
            )

    async def _handle_prompt(self, line: str) -> None:
        """
        Handle prompt sentinel (__FWS_PROMPT__).

        IMPORTANT: This must not implicitly finalize interactive sessions or
        transition mode -> idle. Prompt markers are consumed explicitly via
        pty_wait_prompt / wait_for(match_type="prompt"), which performs the
        session finalization deterministically.
        """
        return

    async def _finalize_interactive_session(self, exit_code: Optional[int] = None) -> None:
        """Finalize an interactive session (idempotent)."""
        if self._mode != "interactive" or not self._active:
            return
        self._active.status = "completed"
        self._active.ts_end = _now_ms()
        if exit_code is not None:
            self._active.exit_code = exit_code
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
    def _append_text_line(path: Path, data: str) -> None:
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
        mgr = await _get_fws_manager()
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
        # Refresh spool size (external writers may have appended).
        try:
            if self._spool_path is not None:
                self._spool_size = self._spool_path.stat().st_size
        except Exception:
            pass
        
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
                            # Parse kv pairs like ts=123 cwd_b64=... exit=0
                            for part in line.split()[1:]:
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    if k == "cwd_b64":
                                        extra["cwd"] = _b64decode(v)
                                    elif k == "ts":
                                        extra["ts"] = int(v)
                                    elif k == "exit":
                                        extra["exit_code"] = int(v)
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
            # If prompt match, trigger session finalization (idempotent)
            if match_type == "prompt":
                exit_code = result.get("extra", {}).get("exit_code")
                await self._finalize_interactive_session(exit_code)
            response = {
                "ok": True,
                "matched": True,
                "match_text": result["match_text"],
                "match_cursor": match_cursor,
                "match_span": {"start": match_cursor, "end": match_end_cursor},
                "resume_cursor": match_end_cursor,
            }
            if result.get("extra"):
                response["extra"] = result["extra"]
            return response
        
        # Not found - register waiter
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters.append((match_fn, future, from_cursor, match_type))
        
        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            # If prompt match, trigger session finalization (idempotent)
            if match_type == "prompt":
                exit_code = result.get("extra", {}).get("exit_code")
                await self._finalize_interactive_session(exit_code)
            return {"ok": True, **result}
        except asyncio.TimeoutError:
            # Return current spool size so agent can resume from here
            try:
                if self._spool_path is not None:
                    self._spool_size = self._spool_path.stat().st_size
            except Exception:
                pass
            return {"ok": False, "matched": False, "error": "timeout", "resume_cursor": self._spool_size}
        finally:
            # Clean up waiter if still present
            self._waiters = [(m, f, c, t) for (m, f, c, t) in self._waiters if f is not future]

    def get_status(self) -> Dict[str, Any]:
        """Get current PTY status."""
        return {
            "ok": True,
            "mode": self._mode,
            "active_session_id": self._interactive_session_id,
            "active_block_id": self._active.block_id if self._active else None,
            "shell_id": self.shell_id,
            "resume_cursor": self._spool_size,
        }

    async def close_shell(self, *, force: bool = True) -> Dict[str, Any]:
        """
        Terminate the conversation-owned agent PTY shell (best-effort).

        This uses the external shell_manager for termination so that the dtach
        session is cleaned up even if the MCP process is exiting.
        """
        shell_id = self.shell_id
        # Cancel local readers (best-effort).
        for task in (self._reader_task, self._bytes_reader_task, self._marker_task, self._screen_delta_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = None
        self._bytes_reader_task = None
        self._marker_task = None
        self._screen_delta_task = None

        # Reset local state (screen/raw remain on disk).
        self._active = None
        self._mode = "idle"
        self._interactive_session_id = None
        self.shell_id = None

        try:
            result = await _shell_manager_request(
                "/shells/terminate",
                {"conversation_id": self.conversation_id, "force": bool(force)},
            )
            return {"ok": True, "shell_id": shell_id, "result": result}
        except Exception as e:
            # Still consider local cleanup successful; termination may be handled elsewhere.
            return {"ok": False, "shell_id": shell_id, "error": str(e)}


_states: Dict[str, ConversationState] = {}


def _state(conversation_id: str) -> ConversationState:
    st = _states.get(conversation_id)
    if not st:
        st = ConversationState(conversation_id)
        _states[conversation_id] = st
    return st


mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")

# Diagnostic markers for stdio MCP process lifetime
print(f"MCP SERVER STARTED pid={os.getpid()}", file=sys.stderr)

def _atexit_cleanup() -> None:
    # Best-effort: terminate any conversation shells this MCP process touched.
    # The contract for this project is that stdio MCP process lifetime owns the
    # PTY shells it uses. If the shell_manager isn't available, this is a no-op.
    try:
        for convo_id in list(_states.keys()):
            _shell_manager_request_sync("/shells/terminate", {"conversation_id": convo_id, "force": True})
    except Exception:
        pass
    print(f"MCP SERVER EXITING pid={os.getpid()}", file=sys.stderr)

atexit.register(_atexit_cleanup)


@mcp.tool(name="ping", description="Return MCP server pid (diagnostic).")
async def ping() -> Dict[str, Any]:
    return {"ok": True, "pid": os.getpid()}


@mcp.tool(name="pty_exec", description="Execute a command (block mode) - waits for completion with BEGIN/END markers.")
async def pty_exec(conversation_id: str, cmd: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    state = _state(conversation_id)
    if state.mode == "interactive":
        return {"ok": False, "error": "PTY in interactive mode - use pty.send instead"}
    if state.mode == "block_running":
        return {"ok": False, "error": "PTY busy - block already running"}
    return await state.exec(cmd=cmd, cwd=cwd)


@mcp.tool(
    name="pty_exec_interactive",
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


@mcp.tool(name="pty_end_session", description="End an interactive session.")
async def pty_end_session(conversation_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    state = _state(conversation_id)
    return await state.end_session(session_id)


@mcp.tool(name="pty_send", description="Send raw bytes to PTY stdin (text, control chars, escape sequences).")
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


@mcp.tool(name="pty_ctrl_c", description="Send Ctrl+C (SIGINT) to PTY.")
async def pty_ctrl_c(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\x03")


@mcp.tool(name="pty_ctrl_d", description="Send Ctrl+D (EOF) to PTY.")
async def pty_ctrl_d(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\x04")


@mcp.tool(name="pty_enter", description="Send Enter/newline to PTY.")
async def pty_enter(conversation_id: str) -> Dict[str, Any]:
    return await pty_send(conversation_id, "\r")


@mcp.tool(
    name="pty_wait_for",
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
        {ok: true, matched: true, match_text, match_cursor, match_span: {start, end}, resume_cursor, extra?}
        - match_cursor: byte offset where match starts (for bookmarking)
        - match_span: {start, end} byte offsets of the match
        - resume_cursor: byte offset to use as from_cursor for next wait_for (= match_span.end)
        - extra: for prompt matches, includes parsed {cwd, ts}
    
    Returns on timeout:
        {ok: false, matched: false, error: "timeout", resume_cursor}
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


@mcp.tool(name="pty_wait_prompt", description="Wait for shell prompt sentinel and finalize interactive session.")
async def pty_wait_prompt(
    conversation_id: str,
    from_cursor: int = 0,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """
    Wait for the shell prompt sentinel (__FWS_PROMPT__).
    
    This is a convenience wrapper around wait_for(match_type="prompt") that also
    ensures the interactive session is finalized and mode transitions to idle.
    
    Returns on match:
        {ok: true, matched: true, match_text, match_cursor, match_span, resume_cursor, extra: {cwd, ts, exit_code}}
    
    Returns on timeout:
        {ok: false, matched: false, error: "timeout", resume_cursor}
    """
    state = _state(conversation_id)
    try:
        await state.ensure_shell()
        return await state.wait_for(
            match="",
            match_type="prompt",
            from_cursor=from_cursor,
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_expect_send", description="Atomic wait-for-match then send input (under lock).")
async def pty_expect_send(
    conversation_id: str,
    expect: str,
    send: str,
    expect_type: str = "substring",
    from_cursor: int = 0,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """
    Atomically wait for a pattern, then send input.
    
    This prevents race conditions between detecting a prompt and sending input.
    The wait and send happen under a per-PTY lock.
    
    Args:
        expect: Pattern to wait for
        send: Data to send after match (supports \\r for Enter, \\x03 for Ctrl+C, etc.)
        expect_type: 'substring', 'regex', or 'prompt'
        from_cursor: Byte offset to start searching from
        timeout_ms: Timeout for the wait phase
    
    Returns on success:
        {ok: true, matched: true, match_text, match_span, resume_cursor, sent: true}
    
    Returns on timeout:
        {ok: false, matched: false, error: "timeout", resume_cursor, sent: false}
    """
    state = _state(conversation_id)
    try:
        await state.ensure_shell()
        async with state.lock:
            result = await state.wait_for(
                match=expect,
                match_type=expect_type,
                from_cursor=from_cursor,
                timeout_ms=timeout_ms,
            )
            if result.get("matched"):
                await state.send_stdin(send)
                result["sent"] = True
            else:
                result["sent"] = False
            return result
    except Exception as e:
        return {"ok": False, "error": str(e), "sent": False}


@mcp.tool(name="pty_status", description="Get PTY status: mode, active block/session, cursor position.")
async def pty_status(conversation_id: str) -> Dict[str, Any]:
    state = _state(conversation_id)
    return state.get_status()


@mcp.tool(name="pty_read_spool", description="Read raw output from the conversation spool at a cursor position.")
async def pty_read_spool(
    conversation_id: str,
    from_cursor: int = 0,
    max_bytes: int = 65536,
) -> Dict[str, Any]:
    """Read output spool from cursor position."""
    state = _state(conversation_id)
    try:
        await state._init_spool()
        data, data_end = await state.read_spool(from_cursor, max_bytes)
        return {"ok": True, "data": data, "cursor": from_cursor, "resume_cursor": data_end}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# === Sprint 3: Screen model MCP tools ===

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
    
    Note: Screen dimensions default to 120x40; callers can change them via `pty_resize`.
    """
    state = _state(conversation_id)
    try:
        async with state._screen_lock:
            await state._refresh_raw_size()
            # If in-memory screen is current, return it.
            if (state._screen_main is not None or state._screen_alt is not None) and state._screen_raw_size == state._raw_size:
                snapshot = state._get_screen_snapshot()
                return {"ok": True, **snapshot}
            # Rehydrate from raw bytes when in-memory state is stale.
            await state._rehydrate_screen_from_raw(state._raw_size)
            snapshot = state._get_screen_snapshot()
            return {"ok": True, **snapshot}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_read_scrollback", description="Read rendered scrollback lines from the terminal history buffer.")
async def pty_read_scrollback(conversation_id: str, limit: int = 200) -> Dict[str, Any]:
    """
    Read scrollback (lines that have scrolled off the top of the screen).

    This is derived from the pyte HistoryScreen model, which is fed from the
    lossless `output.raw` stream. For TUIs, prefer `pty_read_screen` for the
    current visible state.
    """
    state = _state(conversation_id)
    try:
        async with state._screen_lock:
            await state._refresh_raw_size()
            # Ensure the screen model is current before reading history.
            if (state._screen_main is None and state._screen_alt is None) or state._screen_raw_size != state._raw_size:
                await state._rehydrate_screen_from_raw(state._raw_size)
            return state._get_scrollback_snapshot(limit=limit)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_resize", description="Resize the PTY and screen model for this conversation.")
async def pty_resize(conversation_id: str, cols: int, rows: int) -> Dict[str, Any]:
    """
    Resize the underlying PTY winsize and rebuild the pyte screen model.

    Intended callers:
    - UI surface: set to xterm.js-fit cols/rows (preferred)
    - Agents: explicit resize when needed (headless / deterministic)

    This persists the last chosen size under agent_pty/screen_size.json so
    reattach/ensure_shell can restore it.
    """
    state = _state(conversation_id)
    try:
        cols_i = int(cols)
        rows_i = int(rows)
        cols_i = max(1, min(cols_i, 500))
        rows_i = max(1, min(rows_i, 300))

        await state.ensure_shell()
        mgr = await _get_fws_manager()

        async with state._screen_lock:
            state._screen_cols = cols_i
            state._screen_rows = rows_i
            await state._save_persisted_screen_size()
            try:
                await mgr.resize_pty(state.shell_id, cols_i, rows_i)
            except Exception:
                pass
            # Rebuild screen from the lossless byte stream at the new size.
            await state._refresh_raw_size()
            await state._rehydrate_screen_from_raw(state._raw_size)
            snapshot = state._get_screen_snapshot()
        return {"ok": True, **snapshot}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_get_size", description="Get the current screen model size for this conversation.")
async def pty_get_size(conversation_id: str) -> Dict[str, Any]:
    state = _state(conversation_id)
    try:
        await state._load_persisted_screen_size()
        return {"ok": True, "cols": int(state._screen_cols), "rows": int(state._screen_rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="pty_close", description="Terminate the conversation-owned agent PTY shell (dtach).")
async def pty_close(conversation_id: str, force: bool = True) -> Dict[str, Any]:
    """
    Terminate the conversation-owned agent PTY shell.

    This is the explicit agent-side cleanup hook. It is the only supported way
    for agents to kill the PTY; MCP process exit may also attempt best-effort
    cleanup, but callers should not rely on it.
    """
    state = _state(conversation_id)
    return await state.close_shell(force=force)


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
        file_size = await asyncio.to_thread(path.stat)
        file_size = file_size.st_size
        if cursor > file_size:
            cursor = file_size

        def _read_lines_from(path: Path, start: int, max_lines: int, chunk_size: int = 65536) -> tuple[list[bytes], int]:
            lines: list[bytes] = []
            bytes_read_total = 0
            buf = b""
            with path.open("rb") as f:
                f.seek(start)
                while len(lines) < max_lines:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    bytes_read_total += len(chunk)
                    buf += chunk
                    while len(lines) < max_lines:
                        idx = buf.find(b"\n")
                        if idx == -1:
                            break
                        lines.append(buf[:idx])
                        buf = buf[idx + 1 :]
                if buf and len(lines) < max_lines:
                    lines.append(buf)
                    buf = b""
            consumed = bytes_read_total - len(buf)
            return lines, consumed

        lines, consumed = await asyncio.to_thread(_read_lines_from, path, cursor, limit)

        deltas = []
        for raw in lines:
            try:
                deltas.append(json.loads(raw))
            except Exception:
                continue

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
    
    Note: Screen dimensions default to 120x40; callers can change them via `pty_resize`.
    """
    state = _state(conversation_id)
    try:
        async with state._screen_lock:
            await state._refresh_raw_size()
            if (state._screen_main is None and state._screen_alt is None) or state._screen_raw_size != state._raw_size:
                await state._rehydrate_screen_from_raw(state._raw_size)
            screen, _ = state._active_screen()
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "cursor": {"row": screen.cursor.y, "col": screen.cursor.x},
                "title": getattr(screen, "title", "") or "",
                "alt_screen": state._is_alt_screen(),
                "cols": state._screen_cols,
                "rows": state._screen_rows,
                "raw_size": state._raw_size,
                "spool_size": state._spool_size,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="blocks_since", description="List blocks since a byte cursor in blocks.jsonl (per conversation).")
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


@mcp.tool(name="blocks_read", description="Read raw output bytes from a block output file.")
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


@mcp.tool(name="blocks_get", description="Get metadata for a block id (from blocks.jsonl).")
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


@mcp.tool(name="blocks_search", description="Search within a block's output for a substring; returns matching line snippets.")
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
    transport = os.environ.get("MCP_TRANSPORT", "").strip().lower()
    if transport in ("streamable-http", "streamable_http", "http"):
        await mcp.run_streamable_http_async()
        return
    if transport == "sse":
        mount_path = os.environ.get("MCP_MOUNT_PATH") or None
        await mcp.run_sse_async(mount_path=mount_path)
        return
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())

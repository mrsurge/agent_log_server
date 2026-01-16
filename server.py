#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from contextlib import suppress, asynccontextmanager
import hashlib
import re
import secrets
import uuid
import subprocess
import socketio
import binascii

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query, Body, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from framework_shells import get_manager as get_framework_shell_manager
from framework_shells.api import fws_ui
from framework_shells.orchestrator import Orchestrator

from fasthtml.common import (
    HTMLResponse as FastHTMLResponse,
    Html, Head, Body, Div, Section, Header, Footer, Main, H1, H2, H3, P, Button,
    Span, Input, Textarea, Label, Small, A, Ul, Li, Code, Script, Link, Meta, to_xml
)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    agent_pty_monitor_task: Optional[asyncio.Task] = None
    try:
        info = await _get_or_start_appserver_shell()
        await _ensure_appserver_reader(info["shell_id"])
        await _ensure_appserver_initialized()
        # Ensure the shell manager is available for agent PTY attach/terminate operations.
        # This keeps the backend stable even when the MCP stdio worker is session-scoped.
        await _get_or_start_shell_manager()
        agent_pty_monitor_task = asyncio.create_task(_agent_pty_monitor_loop(), name="agent-pty-monitor")
    except Exception:
        pass
    yield
    if agent_pty_monitor_task:
        agent_pty_monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await agent_pty_monitor_task
    # Cleanup on server shutdown: kill extension-owned subprocess shells.
    with suppress(Exception):
        await _terminate_agent_pty_conversation_shells(force=True)
    with suppress(Exception):
        await _stop_mcp_shell()
    with suppress(Exception):
        await _stop_shell_manager()

app = FastAPI(lifespan=_lifespan)
socketio_server = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
socketio_app = socketio.ASGIApp(socketio_server, other_asgi_app=app)

# Host-provided UI hints are runtime-only (not persisted). These are meant for iframe/drawer integration.
_HOST_UI_STATE: Dict[str, Any] = {
    "show_close": False,
    "parent_origin": None,
    "ide_mode": False,
    "project_root": None,
}


@socketio_server.on("connect", namespace="/appserver")
async def _appserver_connect(sid, environ):
    return None


@socketio_server.on("disconnect", namespace="/appserver")
async def _appserver_disconnect(sid):
    return None
app.include_router(fws_ui.router, dependencies=[Depends(lambda: _ensure_framework_shells_secret())])

# --- Config & State ---
LOG_PATH: Optional[Path] = None
_lock = asyncio.Lock()
_config_lock = asyncio.Lock()
_appserver_shell_id: Optional[str] = None
_appserver_reader_task: Optional[asyncio.Task] = None
_appserver_ws_clients_ui: List[WebSocket] = []
_appserver_ws_clients_raw: List[WebSocket] = []
_appserver_turn_state: Dict[str, Dict[str, Any]] = {}
_appserver_item_state: Dict[str, Dict[str, Any]] = {}
_appserver_raw_buffer: List[str] = []
_approval_item_cache: Dict[str, Dict[str, Any]] = {}
_approval_request_map: Dict[str, str] = {}
_appserver_rpc_waiters: Dict[str, asyncio.Future] = {}
_pending_turn_starts: Dict[str, Dict[str, Any]] = {}  # request_id -> original payload for auto-resume
_appserver_initialized = False
_shell_call_ids: Dict[str, Dict[str, Any]] = {}  # Track active shell commands for streaming
_model_list_cache: Optional[List[Dict[str, Any]]] = None
_model_list_cache_time: float = 0
_agent_pty_event_tasks: Dict[str, asyncio.Task] = {}
_agent_pty_ws_offsets: Dict[str, int] = {}
_agent_pty_transcript_offsets: Dict[str, int] = {}
_agent_pty_screen_event_tasks: Dict[str, asyncio.Task] = {}
_agent_pty_screen_ws_offsets: Dict[str, int] = {}
_agent_pty_raw_event_tasks: Dict[str, asyncio.Task] = {}
_agent_pty_raw_ws_offsets: Dict[str, int] = {}
_mcp_shell_id: Optional[str] = None
_shell_manager_shell_id: Optional[str] = None
_agent_pty_exec_seq: int = 0
_pty_raw_subscribers: Dict[str, List[asyncio.Queue]] = {}  # conversation_id -> list of queues for raw PTY output
_pty_command_running: Dict[str, bool] = {}  # conversation_id -> whether a command is currently running
DEBUG_MODE = False
DEBUG_RAW_LOG_PATH: Optional[Path] = None
CODEX_AGENT_THEME_COLOR = "#0d0f13"
CODEX_AGENT_ICON_PATH = "/static/codexas-icon.svg"
CODEX_AGENT_START_URL = "/codex-agent/"
CODEX_AGENT_SCOPE = "/codex-agent/"

# Raw PTY websocket fanout: keep a single framework_shells subscription per conversation
# to avoid triggering a new dtach attach/proxy (and readline prompt redisplay) on every
# websocket open.
_pty_hub_lock = asyncio.Lock()
_pty_hubs: Dict[str, Dict[str, Any]] = {}  # conversation_id -> hub state
_PTY_HUB_IDLE_SECS = float(os.environ.get("APP_SERVER_PTY_HUB_IDLE_SECS", "60"))

# User terminal command capture (markers + raw bytes slicing)
_USER_PTY_RAW_MAX_BYTES_PER_CMD = 512 * 1024  # cap per command slice (safety)
_USER_PTY_RAW_DIRNAME = "user_pty"
_user_pty_capture_lock = asyncio.Lock()
_user_pty_capture: Dict[str, Dict[str, Any]] = {}  # conversation_id -> {raw_path, raw_cursor, open_blocks, marker_offset}


def _asset(url: str) -> str:
    """Append a cache-busting query string based on file mtime."""
    if not url.startswith("/static/"):
        return url
    rel = url.lstrip("/")
    path = Path(__file__).resolve().parent / rel
    try:
        mtime = int(path.stat().st_mtime)
        return f"{url}?v={mtime}"
    except Exception:
        return url
# Persist app server config under ~/.cache/app_server.
CONFIG_PATH = Path(os.path.expanduser("~/.cache/app_server/app_server_config.json"))
LEGACY_TRANSCRIPT_DIR = CONFIG_PATH.parent / "transcripts"
CONVERSATION_DIR = CONFIG_PATH.parent / "conversations"
_transcript_lock = asyncio.Lock()
_transcript_seen: set[tuple[str, str, str]] = set()


def _default_appserver_config() -> Dict[str, Any]:
    return {
        "cwd": None,
        "thread_id": None,
        "turn_id": None,
        "conversation_id": None,
        "conversations": [],
        "active_view": "splash",
        "app_server_command": None,
        "shell_id": None,
        "mcp_shell_id": None,
        "shell_manager_shell_id": None,
    }


def _load_appserver_config() -> Dict[str, Any]:
    cfg = _default_appserver_config()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        else:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # Fall back to defaults on any read/parse error.
        return cfg
    return cfg


def _save_appserver_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalize_conversation_list(cfg: Dict[str, Any]) -> List[str]:
    conversations = cfg.get("conversations")
    if not isinstance(conversations, list):
        conversations = []
    out: List[str] = []
    for item in conversations:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


def _add_conversation_to_config(conversation_id: str, cfg: Dict[str, Any]) -> bool:
    conversations = _normalize_conversation_list(cfg)
    if conversation_id in conversations:
        cfg["conversations"] = conversations
        return False
    conversations.append(conversation_id)
    cfg["conversations"] = conversations
    return True


def _remove_conversation_from_config(conversation_id: str, cfg: Dict[str, Any]) -> None:
    conversations = _normalize_conversation_list(cfg)
    if conversation_id in conversations:
        conversations = [c for c in conversations if c != conversation_id]
    cfg["conversations"] = conversations


def _conversation_ids_from_disk() -> List[str]:
    if not CONVERSATION_DIR.exists():
        return []
    ids = []
    for child in CONVERSATION_DIR.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if meta_path.exists():
            ids.append(child.name)
    return ids


def _sync_conversation_index(cfg: Dict[str, Any]) -> List[str]:
    ids = _normalize_conversation_list(cfg)
    for cid in _conversation_ids_from_disk():
        if cid not in ids:
            ids.append(cid)
    cfg["conversations"] = ids
    return ids


def _find_conversation_by_thread_id(thread_id: Optional[str]) -> Optional[str]:
    if not thread_id or not CONVERSATION_DIR.exists():
        return None
    for child in CONVERSATION_DIR.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("thread_id") == thread_id:
            return child.name
    return None

def _conversation_dir(conversation_id: str) -> Path:
    safe_id = _sanitize_conversation_id(conversation_id)
    return CONVERSATION_DIR / safe_id


def _conversation_meta_path(conversation_id: str) -> Path:
    return _conversation_dir(conversation_id) / "meta.json"


def _conversation_transcript_path(conversation_id: str) -> Path:
    return _conversation_dir(conversation_id) / "transcript.jsonl"


def _default_conversation_meta(conversation_id: str) -> Dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "created_at": utc_ts(),
        "thread_id": None,
        "settings": {},
        "status": "draft",
    }


def _user_pty_root(conversation_id: str) -> Path:
    return _conversation_dir(conversation_id) / _USER_PTY_RAW_DIRNAME


def _user_pty_raw_path(conversation_id: str) -> Path:
    return _user_pty_root(conversation_id) / "output.raw"


def _user_pty_marker_path(conversation_id: str) -> Path:
    # Markers are emitted by the agent_pty rcfile to fd3 and written under agent_pty.
    return _conversation_dir(conversation_id) / "agent_pty" / "markers.log"


def _user_pty_marker_offset_path(conversation_id: str) -> Path:
    return _user_pty_root(conversation_id) / ".markers_offset"


def _ansi_strip(text: str) -> str:
    # Strip CSI + OSC sequences; keep printable output for transcript cards.
    if not text:
        return ""
    try:
        # OSC: ESC ] ... BEL or ST
        text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
        # CSI: ESC [ ... letter
        text = re.sub(r"\x1b\[[0-9;:?]*[ -/]*[@-~]", "", text)
    except Exception:
        return text
    return text


def _scrub_user_cmd_output_keep_sgr(text: str) -> str:
    """Scrub terminal control noise but keep SGR color (CSI ... m) for UI rendering.

    This is used for *user terminal* command output cards. We want colored output,
    but we do not want cursor movement / clear-screen / save-restore cursor, etc.
    """
    if not text:
        return ""
    try:
        # Normalize line endings early
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Drop OSC sequences (titles, etc.)
        text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)

        # Drop save/restore cursor (ESC 7 / ESC 8)
        text = text.replace("\x1b7", "").replace("\x1b8", "")

        # Keep only CSI SGR (ending with 'm'); drop all other CSI sequences.
        def _keep_sgr(m: re.Match) -> str:
            seq = m.group(0)
            return seq if seq.endswith("m") else ""

        text = re.sub(r"\x1b\[[0-9;:?]*[ -/]*[@-~]", _keep_sgr, text)

        # Apply backspaces naively (common from readline/progress redraws).
        out_chars: list[str] = []
        for ch in text:
            if ch == "\b":
                if out_chars:
                    out_chars.pop()
                continue
            out_chars.append(ch)
        return "".join(out_chars)
    except Exception:
        return text


def _termux_user_prompt_from_cwd(cwd: str) -> str:
    """Render a prompt consistent with the agent_pty rcfile PS1 (SGR colors kept)."""
    if not isinstance(cwd, str):
        cwd = ""
    # Common Termux path: /data/data/com.termux/files/home -> ~
    home = os.path.expanduser("~")
    if cwd and home and cwd.startswith(home):
        cwd_disp = "~" + cwd[len(home):]
        if cwd_disp == "":
            cwd_disp = "~"
    else:
        cwd_disp = cwd or "~"
    return f"\x1b[0;32m{cwd_disp}\x1b[0m \x1b[0;97m$\x1b[0m "


def _strip_trailing_prompt_lines(text: str) -> str:
    """Drop trailing PS1 lines from a captured output slice (keep real output)."""
    if not text:
        return ""
    lines = text.splitlines()
    # Remove empty trailing lines first.
    while lines and not lines[-1].strip():
        lines.pop()
    # Strip one or more trailing prompt-looking lines.
    # We match the exact PS1 format set in shell_manager.py rcfile.
    prompt_re = re.compile(r"^\x1b\[0;32m.*?\x1b\[0m \x1b\[0;97m\$\x1b\[0m\s*$")
    while lines and prompt_re.match(lines[-1]):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)


def _strip_leading_echoed_command(text: str, prompt: str, cmd: str) -> str:
    """Drop a leading echoed `<prompt><cmd>` line from a captured output slice.

    Some interactive shells echo the typed command line into the PTY stream; for
    command cards we already render `prompt+cmd` in the ribbon, so this would be
    a duplicate line in the output body.
    """
    if not text or not cmd:
        return text or ""
    try:
        lines = text.splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            return ""
        first = lines[i]

        def _strip_sgr(s: str) -> str:
            # Remove only CSI SGR sequences (keeps semantics for comparison).
            return re.sub(r"\x1b\[[0-9;]*m", "", s or "")

        expected = _strip_sgr(f"{prompt}{cmd}").strip()
        first_norm = _strip_sgr(first).strip()

        # Exact match against rendered prompt+cmd (with or without SGR).
        if expected and first_norm == expected:
            lines.pop(i)
        else:
            # Fallback: many shells echo as `$ cmd` without cwd.
            if re.match(rf"^\$\s*{re.escape(cmd)}\s*$", first_norm):
                lines.pop(i)
            else:
                # Or as `<anything> $ cmd` (path stripped/simplified).
                if re.match(rf"^.*\$\s*{re.escape(cmd)}\s*$", first_norm):
                    lines.pop(i)

        return "\n".join(lines)
    except Exception:
        return text


def _safe_b64decode(s: str) -> str:
    if not s:
        return ""
    try:
        raw = base64.b64decode(s.encode("ascii"), validate=False)
        return raw.decode("utf-8", errors="replace")
    except (binascii.Error, UnicodeError):
        try:
            raw = base64.b64decode(s.encode("ascii"), validate=False)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""


async def _append_pending_cmd_buffer(conversation_id: str, entry: Dict[str, Any]) -> None:
    """Append a pre-built command entry to pending_cmd_buffer (for CODEX_META injection)."""
    meta = _load_conversation_meta(conversation_id)
    buffer = meta.get("pending_cmd_buffer", {})
    shell_id = _get_shell_id_for_envelope(conversation_id)

    if "commands" not in buffer:
        buffer = {
            "v": 1,
            "shell_id": shell_id,
            "conversation_id": conversation_id,
            "commands": [],
            "total_commands_run": 0,
        }

    # Drop stale/legacy entries that used older block_id formats. The envelope is
    # for *user terminal* commands and we standardize those as `user:<convo>:...`.
    try:
        cmds = buffer.get("commands", [])
        if isinstance(cmds, list) and cmds:
            buffer["commands"] = [
                c for c in cmds
                if isinstance(c, dict) and str(c.get("block_id") or "").startswith("user:")
            ]
    except Exception:
        pass

    buffer["total_commands_run"] = buffer.get("total_commands_run", 0) + 1
    buffer["commands"].append(entry)
    if len(buffer["commands"]) > _CMD_BUFFER_MAX_ENTRIES:
        buffer["commands"] = buffer["commands"][-_CMD_BUFFER_MAX_ENTRIES:]
    if shell_id:
        buffer["shell_id"] = shell_id

    meta["pending_cmd_buffer"] = buffer
    _save_conversation_meta(conversation_id, meta)

def _load_conversation_meta(conversation_id: str) -> Dict[str, Any]:
    path = _conversation_meta_path(conversation_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    meta = _default_conversation_meta(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def _save_conversation_meta(conversation_id: str, meta: Dict[str, Any]) -> None:
    path = _conversation_meta_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


# =============================================================================
# META ENVELOPE: User command context injection
# =============================================================================
# When user runs terminal commands, we buffer context and prepend it to the
# next chat message as a sentinel-wrapped envelope. Agents see the full context;
# transcript/frontend see clean messages (envelope stripped).

_META_ENVELOPE_START = "\x1eCODEX_META "  # RS + prefix for false-positive guard
_META_ENVELOPE_END = "\x1f"               # US
_CMD_BUFFER_MAX_ENTRIES = 10
_CMD_PREVIEW_MAX_LINES = 20
_CMD_PREVIEW_MAX_BYTES = 3000


def _get_shell_id_for_envelope(conversation_id: str) -> Optional[str]:
    """Read shell_id from persisted file for meta envelope."""
    path = _conversation_dir(conversation_id) / "agent_pty" / "shell_id.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None


def _record_last_injected_meta_envelope(conversation_id: str, envelope_json: str, *, command_count: int) -> None:
    """Persist last injected meta envelope for debugging/visibility.

    This does NOT affect what is sent to the model; it only stores a copy in SSOT.
    """
    try:
        meta = _load_conversation_meta(conversation_id)
        debug = meta.get("debug") if isinstance(meta.get("debug"), dict) else {}
        debug["last_meta_envelope"] = {
            "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "command_count": int(command_count),
            "envelope_json": envelope_json,
        }
        meta["debug"] = debug
        _save_conversation_meta(conversation_id, meta)
    except Exception:
        # Debug-only; do not fail the request.
        pass


async def _build_cmd_preview(conversation_id: str, block: Optional[dict] = None) -> dict:
    """Build bounded tail preview from per-block output file.
    
    Uses the block's output_path for command-scoped output.
    Falls back to inline stdout or conversation-wide snapshot if output_path unavailable.
    """
    def _read_block_output() -> List[str]:
        """Sync helper to read block output file."""
        result: List[str] = []
        
        # Try per-block output_path first (command-scoped)
        if block:
            output_path = block.get("output_path")
            if output_path:
                try:
                    text = Path(output_path).read_text(encoding="utf-8")
                    lines = text.splitlines()[-_CMD_PREVIEW_MAX_LINES:]
                    if lines:
                        return lines
                except Exception:
                    pass
            
            # Fallback to inline stdout
            stdout = block.get("stdout", "")
            if stdout:
                lines = stdout.splitlines()[-_CMD_PREVIEW_MAX_LINES:]
                if lines:
                    return lines
        
        # Last resort: conversation-wide scrollback (legacy/fallback)
        scrollback_path = _conversation_dir(conversation_id) / "agent_pty" / "scrollback.snapshot.json"
        if scrollback_path.exists():
            try:
                data = json.loads(scrollback_path.read_text(encoding="utf-8"))
                result = data.get("lines", [])[-_CMD_PREVIEW_MAX_LINES:]
                if result:
                    return result
            except Exception:
                pass
        
        return result
    
    lines = await asyncio.to_thread(_read_block_output)
    truncated = False
    
    # Apply byte cap
    total_bytes = 0
    capped_lines: List[str] = []
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
    
    shell_id = _get_shell_id_for_envelope(conversation_id)
    preview = await _build_cmd_preview(conversation_id, block)
    
    # The CODEX_META envelope is reserved for *user terminal* command context.
    # Skip buffering legacy agent blocks here (they can be read from transcript/blocks).
    if not str(block_id or "").startswith("user:"):
        return

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

    # Ensure we don't carry legacy entries across versions.
    try:
        cmds = buffer.get("commands", [])
        if isinstance(cmds, list) and cmds:
            buffer["commands"] = [
                c for c in cmds
                if isinstance(c, dict) and str(c.get("block_id") or "").startswith("user:")
            ]
    except Exception:
        pass
    
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


def _build_envelope_from_buffer(buffer: dict) -> str:
    """Build envelope JSON from command buffer."""
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


def _strip_meta_envelope(text: str) -> str:
    """Strip leading meta envelope from text if present.
    
    The envelope is prepended to user messages to provide command context
    to agents. It must be stripped before writing to transcript or
    displaying in frontend.
    """
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1:]
    return text


def _sanitize_transcript_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Strip meta envelope from transcript items for display.
    
    Applies to user messages that may have envelope in 'text' field.
    Returns a copy with envelope stripped.
    """
    if not isinstance(item, dict):
        return item
    role = item.get("role", "")
    if role == "user" and isinstance(item.get("text"), str):
        text = _strip_meta_envelope(item["text"])
        if text != item["text"]:
            item = dict(item)  # Shallow copy
            item["text"] = text
    return item


def _latest_legacy_transcript() -> Optional[Path]:
    if not LEGACY_TRANSCRIPT_DIR.exists():
        return None
    files = sorted(LEGACY_TRANSCRIPT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


async def _ensure_conversation(create_if_missing: bool = True) -> Optional[str]:
    async with _config_lock:
        cfg = _load_appserver_config()
        convo_id = cfg.get("conversation_id")

    if convo_id and _conversation_meta_path(convo_id).exists():
        return convo_id

    if not create_if_missing:
        return None

    convo_id = convo_id or uuid.uuid4().hex
    meta = _default_conversation_meta(convo_id)

    legacy = _latest_legacy_transcript()
    if legacy and not _conversation_transcript_path(convo_id).exists():
        try:
            _conversation_transcript_path(convo_id).parent.mkdir(parents=True, exist_ok=True)
            legacy.replace(_conversation_transcript_path(convo_id))
            meta["thread_id"] = legacy.stem
            meta["status"] = "active"
        except Exception:
            pass

    _save_conversation_meta(convo_id, meta)
    async with _config_lock:
        cfg = _load_appserver_config()
        _add_conversation_to_config(convo_id, cfg)
        cfg["conversation_id"] = convo_id
        cfg["active_view"] = cfg.get("active_view") or "conversation"
        if meta.get("thread_id"):
            cfg["thread_id"] = meta.get("thread_id")
        _save_appserver_config(cfg)
    return convo_id


async def _get_conversation_meta() -> Optional[Dict[str, Any]]:
    convo_id = await _ensure_conversation(create_if_missing=False)
    if not convo_id:
        return None
    return _load_conversation_meta(convo_id)


async def _update_conversation_meta(patch: Dict[str, Any]) -> Dict[str, Any]:
    convo_id = await _ensure_conversation()
    meta = _load_conversation_meta(convo_id)
    meta.update(patch)
    _save_conversation_meta(convo_id, meta)
    return meta


async def _set_thread_id(thread_id: str) -> None:
    if not thread_id:
        return
    
    # Check if this thread_id is already bound to another conversation
    existing_convo = _find_conversation_by_thread_id(thread_id)
    if existing_convo:
        # Thread already bound - don't rebind to another conversation
        return
    
    convo_id = await _ensure_conversation()
    meta = _load_conversation_meta(convo_id)
    if not meta.get("thread_id"):
        meta["thread_id"] = thread_id
        meta["status"] = "active"
        _save_conversation_meta(convo_id, meta)
    async with _config_lock:
        cfg = _load_appserver_config()
        if not cfg.get("thread_id"):
            cfg["thread_id"] = thread_id
            _save_appserver_config(cfg)


async def _set_turn_id(turn_id: Optional[str]) -> None:
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["turn_id"] = turn_id
        _save_appserver_config(cfg)


def _sanitize_conversation_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return safe or "unknown"


def _transcript_path(conversation_id: str) -> Path:
    return _conversation_transcript_path(conversation_id)


async def _write_transcript_entries(conversation_id: str, items: List[Dict[str, Any]]) -> None:
    if not conversation_id:
        return
    path = _transcript_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _transcript_lock:
        with path.open("w", encoding="utf-8") as f:
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                record = {"ts": utc_ts(), **entry}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

def _rollout_sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.codex/sessions"))


def _find_rollout_path(rollout_id: str) -> Optional[Path]:
    safe = _sanitize_conversation_id(rollout_id)
    if not safe:
        return None
    base = _rollout_sessions_dir()
    if not base.exists():
        return None
    for path in base.rglob(f"*{safe}*.jsonl"):
        if path.is_file():
            return path
    return None


def _parse_rollout_timestamp(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _rollout_content_text(payload: Dict[str, Any]) -> Optional[str]:
    content = payload.get("content")
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if not parts and isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    if not parts and isinstance(payload.get("message"), str):
        parts.append(payload["message"])
    text = "\n".join(parts).strip()
    return text or None


def _rollout_reasoning_text(payload: Dict[str, Any]) -> Optional[str]:
    summary = payload.get("summary")
    parts: List[str] = []
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict):
                text = item.get("text") or item.get("summary_text")
                if isinstance(text, str):
                    parts.append(text)
    if not parts and isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    text = "\n".join(parts).strip()
    return text or None


def _rollout_extract_diff(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("diff", "unified_diff", "patch"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    if isinstance(payload, list):
        for value in payload:
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    return None


def _rollout_preview_entries(path: Path, limit: int = 400) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, Optional[int]]] = set()
    token_total: Optional[int] = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if len(items) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_bucket = _parse_rollout_timestamp(rec.get("timestamp"))
                rtype = rec.get("type")
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if rtype == "response_item" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype == "message":
                        role = payload.get("role")
                        if role in {"user", "assistant"}:
                            text = _rollout_content_text(payload)
                            if text:
                                key = (role, text, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": role, "text": text, "ts": rec.get("timestamp")})
                    elif ptype == "reasoning":
                        text = _rollout_reasoning_text(payload)
                        if text:
                            key = ("reasoning", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": text, "ts": rec.get("timestamp")})
                elif rtype == "event_msg" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype == "user_message":
                        text = payload.get("message")
                        if isinstance(text, str):
                            text = _strip_meta_envelope(text)  # Strip BEFORE .strip()
                            text = text.strip()
                            if text:
                                key = ("user", text, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": "user", "text": text, "ts": rec.get("timestamp")})
                    elif ptype == "agent_message":
                        text = payload.get("message")
                        if isinstance(text, str) and text.strip():
                            key = ("assistant", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "assistant", "text": text.strip(), "ts": rec.get("timestamp")})
                    elif ptype == "agent_reasoning":
                        text = payload.get("text")
                        if isinstance(text, str) and text.strip():
                            key = ("reasoning", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": text.strip(), "ts": rec.get("timestamp")})
                    elif ptype == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            usage = info.get("total_token_usage") or info.get("last_token_usage") or {}
                            if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)):
                                token_total = int(usage["total_tokens"])
                diff = _rollout_extract_diff(payload)
                if diff:
                    key = ("diff", diff, ts_bucket)
                    if key not in seen:
                        seen.add(key)
                        items.append({"role": "diff", "text": diff, "ts": rec.get("timestamp")})
    except Exception:
        return {"items": [], "token_total": None}
    return {"items": items, "token_total": token_total}

def _extract_item_text(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract text from user/assistant message items.
    
    This is the SINGLE choke point for sanitizing user messages before
    they reach transcript or frontend. All envelope stripping happens here.
    
    Handles multiple schema variants:
    - Legacy: item.type == "usermessage" / "user_message" / "agentmessage"
    - ResponseItem: item.type == "message" with role == "user" / "assistant"
    """
    raw_type = str(item.get("type") or "")
    item_type = raw_type.lower()
    
    # Handle ResponseItem schema: type == "message" with role field
    if item_type == "message":
        role = str(item.get("role") or "").lower()
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
        text = "\n".join(text_parts)
        
        if role == "user":
            text = _strip_meta_envelope(text)  # Strip BEFORE .strip() (control chars)
            text = text.strip()
            if text:
                return {"role": "user", "text": text}
        elif role in {"assistant", "agent"}:
            text = text.strip()
            if text:
                return {"role": "assistant", "text": text}
        return None
    
    # Handle legacy usermessage schema
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
        text = "\n".join(text_parts)
        text = _strip_meta_envelope(text)  # Strip BEFORE .strip() (control chars)
        text = text.strip()
        if text:
            return {"role": "user", "text": text}
    
    # Handle legacy assistant message schema
    if item_type in {"agentmessage", "assistantmessage", "assistant"}:
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("message") if isinstance(item.get("message"), str) else None
        if isinstance(text, str) and text.strip():
            return {"role": "assistant", "text": text.strip()}
    
    return None


def _extract_reasoning_text(item: Dict[str, Any], fallback: Optional[str] = None) -> Optional[str]:
    summary = item.get("summary")
    parts: List[str] = []
    if isinstance(summary, list):
        for part in summary:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("summary")
                if isinstance(text, str):
                    parts.append(text)
    elif isinstance(summary, str):
        parts.append(summary)
    if not parts:
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    if not parts and isinstance(fallback, str):
        parts.append(fallback)
    text = "\n".join([p for p in parts if isinstance(p, str)]).strip()
    return text or None


def _extract_diff_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    diff = payload.get("diff") or payload.get("patch") or payload.get("unified_diff")
    if isinstance(diff, str) and diff.strip():
        return diff
    changes = payload.get("changes")
    if isinstance(changes, list):
        chunks: List[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            text = change.get("diff")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
        if chunks:
            return "\n".join(chunks)
    file_changes = payload.get("fileChanges")
    if isinstance(file_changes, dict):
        chunks = []
        for _, change in file_changes.items():
            if isinstance(change, dict):
                text = change.get("diff") or change.get("patch")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
        if chunks:
            return "\n".join(chunks)
    return None


def _extract_diff_with_path(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract diff text and file path from payload. Returns (diff_text, path)."""
    if not isinstance(payload, dict):
        return None, None
    path = None
    # Direct diff/patch
    diff = payload.get("diff") or payload.get("patch") or payload.get("unified_diff")
    if isinstance(diff, str) and diff.strip():
        path = payload.get("path")
        # If no path in payload, try to extract from diff headers
        if not path:
            path = _extract_path_from_diff(diff)
        return diff, path
    # Changes array
    changes = payload.get("changes")
    if isinstance(changes, list):
        chunks: List[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            text = change.get("diff")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
                if not path:
                    path = change.get("path")
        if chunks:
            combined = "\n".join(chunks)
            if not path:
                path = _extract_path_from_diff(combined)
            return combined, path
    # FileChanges dict
    file_changes = payload.get("fileChanges")
    if isinstance(file_changes, dict):
        chunks = []
        for fpath, change in file_changes.items():
            if isinstance(change, dict):
                text = change.get("diff") or change.get("patch")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
                    if not path:
                        path = fpath
        if chunks:
            return "\n".join(chunks), path
    return None, None


def _extract_path_from_diff(diff_text: str) -> Optional[str]:
    """Extract file path from diff headers like '--- a/README.md' or 'diff --git a/README.md b/README.md'."""
    if not diff_text:
        return None
    for line in diff_text.splitlines():
        # Try diff --git header first
        if line.startswith("diff --git "):
            # Format: diff --git a/path b/path
            parts = line.split()
            if len(parts) >= 4:
                # Get the b/path part (the destination)
                bpath = parts[3]
                if bpath.startswith("b/"):
                    return bpath[2:]
                return bpath
        # Try +++ header (new file path)
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                return path[2:]
            if path != "/dev/null":
                return path
        # Try --- header as fallback
        if line.startswith("--- "):
            path = line[4:].strip()
            if path.startswith("a/"):
                return path[2:]
            if path != "/dev/null":
                return path
    return None


def _diff_signature(diff_text: str) -> str:
    if not diff_text:
        return "empty"
    files: List[str] = []
    hunks: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            files.append(line.strip())
        elif line.startswith("@@"):
            hunks.append(line.strip())
    signature = "\n".join(files + hunks) + "\n" + diff_text
    return hashlib.sha1(signature.encode("utf-8")).hexdigest()


# Thought block pattern: **<thought content>**
_THOUGHT_PATTERN = re.compile(r'\*\*([^*]+)\*\*')


def _extract_and_scrub_thoughts_stream(delta: str, state: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Streaming thought extractor that handles **title** patterns across delta chunks.
    Returns (scrubbed_text, thoughts) and keeps any incomplete marker in state.
    """
    if not isinstance(delta, str) or not delta:
        return delta, []
    buffer = state.get("thought_buffer", "")
    text = buffer + delta
    thoughts: List[str] = []
    scrubbed_parts: List[str] = []
    idx = 0
    state["thought_buffer"] = ""
    while True:
        start = text.find("**", idx)
        if start == -1:
            scrubbed_parts.append(text[idx:])
            break
        scrubbed_parts.append(text[idx:start])
        end = text.find("**", start + 2)
        if end == -1:
            state["thought_buffer"] = text[start:]
            break
        content = text[start + 2:end]
        if content:
            thoughts.append(content)
        idx = end + 2
    scrubbed = "".join(scrubbed_parts)
    # If we ended on a single trailing '*', keep it for the next delta.
    if not state["thought_buffer"] and text.endswith("*") and not text.endswith("**"):
        state["thought_buffer"] = "*"
        if scrubbed.endswith("*"):
            scrubbed = scrubbed[:-1]
    return scrubbed, thoughts


def _extract_and_scrub_thoughts(text: str) -> Tuple[str, List[str]]:
    """
    Extract thought blocks from text and return (scrubbed_text, list_of_thoughts).
    Thought blocks are **<thought content>** patterns.
    """
    if not text:
        return text, []
    thoughts = _THOUGHT_PATTERN.findall(text)
    scrubbed = _THOUGHT_PATTERN.sub('', text)
    return scrubbed, thoughts


def _get_thread_id(conversation_id: Optional[str], payload: Any) -> Optional[str]:
    if conversation_id:
        return str(conversation_id)
    if isinstance(payload, dict):
        for key in ("threadId", "thread_id", "conversationId", "conversation_id"):
            if payload.get(key):
                return str(payload[key])
        thread = payload.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
    return None


def _get_turn_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("turnId", "turn_id"):
            if payload.get(key):
                return str(payload[key])
        # Check nested turn object (turn/completed format)
        if isinstance(payload.get("turn"), dict) and payload["turn"].get("id"):
            return str(payload["turn"]["id"])
        if payload.get("id") and payload.get("status") is not None:
            return str(payload.get("id"))
    return None


def _get_item_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("itemId", "item_id", "id"):
            if payload.get(key):
                return str(payload[key])
    return None


def _get_turn_state(thread_id: Optional[str], turn_id: Optional[str]) -> Dict[str, Any]:
    key = f"{thread_id or 'unknown'}:{turn_id or 'unknown'}"
    state = _appserver_turn_state.get(key)
    if not state:
        state = {
            "msg_source": None,
            "reason_source": None,
            "assistant_id": None,
            "reasoning_id": None,
            "assistant_started": False,
            "reasoning_started": False,
            "assistant_buffer": "",
            "reasoning_buffer": "",
            "thought_buffer": "",
            "diff_hashes": set(),
            "diff_seen": False,
            "plan_steps": [],  # Accumulate plan steps during turn
        }
        _appserver_turn_state[key] = state
    return state


def _tool_event_id(label: str, payload: Dict[str, Any], thread_id: Optional[str], turn_id: Optional[str]) -> str:
    base = payload.get("itemId") or payload.get("item_id") or payload.get("id") or payload.get("call_id") or payload.get("tool_call_id") or payload.get("command_id")
    if base:
        return str(base)
    return f"{label}:{thread_id or 'unknown'}:{turn_id or 'unknown'}"


def _get_state_for_item(thread_id: Optional[str], turn_id: Optional[str], item_id: Optional[str]) -> Dict[str, Any]:
    if item_id and item_id in _appserver_item_state:
        return _appserver_item_state[item_id]
    return _get_turn_state(thread_id, turn_id)


def _register_item_state(item_id: Optional[str], state: Dict[str, Any]) -> None:
    if item_id:
        _appserver_item_state[item_id] = state


async def _emit_diff_event(
    state: Dict[str, Any],
    diff: Optional[str],
    conversation_id: Optional[str],
    thread_id: Optional[str],
    turn_id: Optional[str],
    item_id: Optional[str],
    events: List[Dict[str, Any]],
    record_transcript: bool = True,
    path: Optional[str] = None,
) -> None:
    if not diff:
        return
    diff_text = diff.strip()
    if not diff_text:
        return
    diff_hashes = state.setdefault("diff_hashes", set())
    diff_hash = _diff_signature(diff_text)
    if diff_hash in diff_hashes:
        return
    diff_hashes.add(diff_hash)
    state["diff_seen"] = True
    if thread_id or turn_id:
        diff_id = f"{thread_id or 'unknown'}:{turn_id or 'unknown'}:{diff_hash[:12]}"
    elif item_id:
        diff_id = f"item:{item_id}:{diff_hash[:12]}"
    else:
        diff_id = f"diff:{diff_hash[:12]}"
    events.append({"type": "diff", "id": diff_id, "text": diff_text, "path": path})
    if record_transcript and conversation_id:
        await _append_transcript_entry(conversation_id, {
            "role": "diff",
            "text": diff_text,
            "path": path,
            "item_id": diff_id,
            "event": "turn_diff",
        })


async def _append_transcript_entry(conversation_id: str, entry: Dict[str, Any]) -> None:
    if not conversation_id:
        return
    path = _transcript_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_ts(), **entry}
    async with _transcript_lock:
        item_id = entry.get("item_id")
        role = entry.get("role")
        if item_id and role:
            key = (conversation_id, str(item_id), str(role))
            if key in _transcript_seen:
                return
            _transcript_seen.add(key)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _agent_pty_transcript_offset_path(conversation_id: str) -> Path:
    """Path to persisted transcript offset for agent PTY events."""
    safe_id = _sanitize_conversation_id(conversation_id)
    return _conversation_dir(safe_id) / "agent_pty" / ".transcript_offset"


def _load_agent_pty_transcript_offset(conversation_id: str) -> int:
    """Load persisted transcript offset, or 0 if not found."""
    path = _agent_pty_transcript_offset_path(conversation_id)
    try:
        if path.exists():
            return int(path.read_text().strip())
    except Exception:
        pass
    return 0


def _save_agent_pty_transcript_offset(conversation_id: str, offset: int) -> None:
    """Persist transcript offset to disk."""
    path = _agent_pty_transcript_offset_path(conversation_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(offset))
    except Exception:
        pass


async def _tail_agent_pty_events_to_transcript(conversation_id: str, *, max_lines_per_tick: int = 50) -> None:
    """Best-effort: mirror agent PTY block events into transcript SSOT for replay.

    Reads from conversations/<id>/agent_pty/events.jsonl and writes a compact entry to transcript.jsonl.
    Offset is persisted to disk to survive server restarts.
    Also buffers command context for meta envelope injection on next user message.
    """
    if not conversation_id:
        return
    path = _agent_pty_events_path(conversation_id)
    if not path.exists():
        return
    try:
        data = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    except Exception:
        return
    raw = data.encode("utf-8", errors="replace")
    # Load offset from memory cache or disk
    offset = _agent_pty_transcript_offsets.get(conversation_id)
    if offset is None:
        offset = _load_agent_pty_transcript_offset(conversation_id)
        _agent_pty_transcript_offsets[conversation_id] = offset
    if offset > len(raw):
        offset = 0
    tail = raw[offset:]
    if not tail:
        return
    lines = tail.splitlines()[:max_lines_per_tick]
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
        
        # Buffer command context on block end for meta envelope
        if etype == "agent_block_end":
            block = evt.get("block")
            if isinstance(block, dict):
                await _buffer_cmd_context(conversation_id, block)
                # For raw mode blocks (user terminal via WebSocket), emit shell_* events
                # so frontend creates transcript cards
                if evt.get("raw_mode"):
                    await _emit_shell_events_from_agent_block(conversation_id, block)
        
        block_id = evt.get("block_id") or (evt.get("block") or {}).get("block_id")
        payload = {
            "role": "agent_pty",
            "event": etype,
            "block_id": block_id,
            "block": evt.get("block"),
        }
        if etype == "agent_block_delta":
            payload["delta"] = evt.get("delta")
        await _append_transcript_entry(conversation_id, payload)
        # Note: do not synthesize additional shell_* transcript rows from agent PTY blocks.
        # It duplicates output and makes compound commands appear as multiple commands.
    consumed = b"\n".join(lines)
    new_offset = offset + len(consumed) + (1 if lines else 0)
    _agent_pty_transcript_offsets[conversation_id] = new_offset
    # Persist to disk
    _save_agent_pty_transcript_offset(conversation_id, new_offset)


async def _maybe_capture_transcript(
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str],
    raw: Any = None,
) -> None:
    if not conversation_id:
        return
    if not label:
        return
    label_norm = label.lower()
    item = None
    if isinstance(payload, dict) and "item" in payload:
        item = payload.get("item")
    elif isinstance(payload, dict):
        item = payload
    if not isinstance(item, dict):
        return
    if label_norm == "item/started":
        entry = _extract_item_text(item)
        if entry and entry["role"] == "user":
            await _append_transcript_entry(conversation_id, {
                "role": entry["role"],
                "text": entry["text"],
                "item_id": item.get("id"),
                "event": label_norm,
            })
        return
    if label_norm == "item/completed":
        entry = _extract_item_text(item)
        if entry and entry["role"] == "assistant":
            await _append_transcript_entry(conversation_id, {
                "role": entry["role"],
                "text": entry["text"],
                "item_id": item.get("id"),
                "event": label_norm,
            })
        # Reasoning is stored via codex/event/agent_reasoning (like messages via agent_message)
        return
    if label_norm.startswith("codex/event/"):
        event_type = label_norm.split("codex/event/", 1)[-1]
        if event_type in {"item_started", "item_completed"} and isinstance(payload, dict):
            item_payload = payload.get("item")
            if isinstance(item_payload, dict):
                entry = _extract_item_text(item_payload)
                if entry:
                    await _append_transcript_entry(conversation_id, {
                        "role": entry["role"],
                        "text": entry["text"],
                        "item_id": item_payload.get("id"),
                        "event": event_type,
                    })
                # Reasoning is stored via codex/event/agent_reasoning (like messages via agent_message)


def _ensure_framework_shells_secret() -> None:
    """Derive a stable secret from cwd/repo root if not already set."""
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    cfg = _load_appserver_config()
    repo_root = cfg.get("cwd") or str(Path(__file__).resolve().parent)
    try:
        repo_root = str(Path(repo_root).resolve())
    except Exception:
        repo_root = str(repo_root)
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


async def _get_fws_manager():
    _ensure_framework_shells_secret()
    return await get_framework_shell_manager(run_id=os.environ.get("FRAMEWORK_SHELLS_RUN_ID", "app-server"))


async def _get_or_start_appserver_shell() -> Dict[str, Any]:
    global _appserver_shell_id
    _ensure_framework_shells_secret()
    async with _config_lock:
        cfg = _load_appserver_config()
        if cfg.get("shell_id"):
            _appserver_shell_id = cfg["shell_id"]

    if _appserver_shell_id:
        mgr = await _get_fws_manager()
        shell = await mgr.get_shell(_appserver_shell_id)
        if shell and shell.status == "running":
            return {"shell_id": _appserver_shell_id, "status": "running", "pid": shell.pid}

    # Start a new app-server shell via shellspec
    mgr = await _get_fws_manager()
    orch = Orchestrator(mgr)
    cfg = _load_appserver_config()
    cwd = cfg.get("cwd") or "."
    command = cfg.get("app_server_command") or "codex-app-server"
    spec_path = Path(__file__).resolve().parent / "shellspec" / "app_server.yaml"
    shell = await orch.start_from_ref(
        f"{spec_path}#app_server",
        base_dir=spec_path.parent,
        ctx={"CWD": cwd, "APP_SERVER_COMMAND": command},
        label="app-server:codex",
        wait_ready=False,
    )
    _appserver_shell_id = shell.id
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["shell_id"] = shell.id
        _save_appserver_config(cfg)
    return {"shell_id": shell.id, "status": "running", "pid": shell.pid}


async def _stop_appserver_shell() -> None:
    global _appserver_shell_id
    global _appserver_reader_task
    global _appserver_initialized
    _ensure_framework_shells_secret()
    if not _appserver_shell_id:
        cfg = _load_appserver_config()
        _appserver_shell_id = cfg.get("shell_id")
    if not _appserver_shell_id:
        return
    if _appserver_reader_task:
        _appserver_reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await _appserver_reader_task
        _appserver_reader_task = None
    mgr = await _get_fws_manager()
    try:
        await mgr.terminate_shell(_appserver_shell_id, force=True)
    except Exception:
        pass
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["shell_id"] = None
        _save_appserver_config(cfg)
    _appserver_shell_id = None
    _appserver_initialized = False


async def _get_or_start_shell_manager() -> Dict[str, Any]:
    global _shell_manager_shell_id
    _ensure_framework_shells_secret()
    async with _config_lock:
        cfg = _load_appserver_config()
        if cfg.get("shell_manager_shell_id"):
            _shell_manager_shell_id = cfg["shell_manager_shell_id"]

    if _shell_manager_shell_id:
        mgr = await _get_fws_manager()
        shell = await mgr.get_shell(_shell_manager_shell_id)
        if shell and shell.status == "running":
            return {"shell_id": _shell_manager_shell_id, "status": "running", "pid": shell.pid}

    mgr = await _get_fws_manager()
    orch = Orchestrator(mgr)
    cfg = _load_appserver_config()
    cwd = cfg.get("cwd") or "."
    spec_path = Path(__file__).resolve().parent / "shellspec" / "mcp_agent_pty.yaml"
    shell = await orch.start_from_ref(
        f"{spec_path}#shell_manager",
        base_dir=spec_path.parent,
        ctx={"CWD": cwd},
        label="shell-manager:agent-pty",
        wait_ready=False,
    )
    _shell_manager_shell_id = shell.id
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["shell_manager_shell_id"] = shell.id
        _save_appserver_config(cfg)
    return {"shell_id": shell.id, "status": "running", "pid": shell.pid}


async def _stop_shell_manager() -> None:
    global _shell_manager_shell_id
    _ensure_framework_shells_secret()
    if not _shell_manager_shell_id:
        cfg = _load_appserver_config()
        _shell_manager_shell_id = cfg.get("shell_manager_shell_id")
    if not _shell_manager_shell_id:
        return
    mgr = await _get_fws_manager()
    try:
        await mgr.terminate_shell(_shell_manager_shell_id, force=True)
    except Exception:
        pass
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["shell_manager_shell_id"] = None
        _save_appserver_config(cfg)
    _shell_manager_shell_id = None


async def _get_or_start_mcp_shell() -> Dict[str, Any]:
    global _mcp_shell_id
    _ensure_framework_shells_secret()
    async with _config_lock:
        cfg = _load_appserver_config()
        if cfg.get("mcp_shell_id"):
            _mcp_shell_id = cfg["mcp_shell_id"]

    if _mcp_shell_id:
        mgr = await _get_fws_manager()
        shell = await mgr.get_shell(_mcp_shell_id)
        if shell and shell.status == "running":
            return {"shell_id": _mcp_shell_id, "status": "running", "pid": shell.pid}

    mgr = await _get_fws_manager()
    orch = Orchestrator(mgr)
    cfg = _load_appserver_config()
    cwd = cfg.get("cwd") or "."
    spec_path = Path(__file__).resolve().parent / "shellspec" / "mcp_agent_pty.yaml"
    shell = await orch.start_from_ref(
        f"{spec_path}#mcp_agent_pty",
        base_dir=spec_path.parent,
        ctx={"CWD": cwd},
        label="mcp:agent-pty",
        wait_ready=False,
    )
    _mcp_shell_id = shell.id
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["mcp_shell_id"] = shell.id
        _save_appserver_config(cfg)
    return {"shell_id": shell.id, "status": "running", "pid": shell.pid}


async def _stop_mcp_shell() -> None:
    global _mcp_shell_id
    _ensure_framework_shells_secret()
    if not _mcp_shell_id:
        cfg = _load_appserver_config()
        _mcp_shell_id = cfg.get("mcp_shell_id")
    if not _mcp_shell_id:
        return
    mgr = await _get_fws_manager()
    try:
        await mgr.terminate_shell(_mcp_shell_id, force=True)
    except Exception:
        pass
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["mcp_shell_id"] = None
        _save_appserver_config(cfg)
    _mcp_shell_id = None


async def _terminate_agent_pty_conversation_shells(*, force: bool = True) -> Dict[str, Any]:
    """Terminate all running conversation-owned agent PTY dtach shells (best-effort)."""
    _ensure_framework_shells_secret()
    mgr = await _get_fws_manager()
    try:
        records = await mgr.list_shells()
    except Exception:
        return {"ok": False, "terminated": 0}

    terminated = 0
    for rec in records:
        label = rec.label or ""
        if rec.status != "running":
            continue
        if not label.startswith("agent-pty:"):
            continue
        try:
            await mgr.terminate_shell(rec.id, force=force)
            terminated += 1
        except Exception:
            pass
    return {"ok": True, "terminated": terminated}

async def _broadcast_appserver_ui(event: Dict[str, Any]) -> None:
    if not _appserver_ws_clients_ui:
        # still try socket.io
        try:
            await socketio_server.emit("appserver_event", event, namespace="/appserver")
        except Exception:
            pass
        return
    data = json.dumps(event, ensure_ascii=False)
    stale: List[WebSocket] = []
    for ws in _appserver_ws_clients_ui:
        try:
            await ws.send_text(data)
        except Exception:
            stale.append(ws)
    for ws in stale:
        with suppress(Exception):
            _appserver_ws_clients_ui.remove(ws)
    try:
        await socketio_server.emit("appserver_event", event, namespace="/appserver")
    except Exception:
        pass


async def _broadcast_appserver_raw(message: str) -> None:
    _appserver_raw_buffer.append(message)
    if len(_appserver_raw_buffer) > 500:
        _appserver_raw_buffer[:] = _appserver_raw_buffer[-500:]
    # Write to debug log file if enabled
    if DEBUG_MODE and DEBUG_RAW_LOG_PATH:
        try:
            with DEBUG_RAW_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(message + "\n")
        except Exception:
            pass
    if not _appserver_ws_clients_raw:
        return
    stale: List[WebSocket] = []
    for ws in _appserver_ws_clients_raw:
        try:
            await ws.send_text(message)
        except Exception:
            stale.append(ws)
    for ws in stale:
        with suppress(Exception):
            _appserver_ws_clients_raw.remove(ws)


def _agent_pty_events_path(conversation_id: str) -> Path:
    safe_id = _sanitize_conversation_id(conversation_id)
    return _conversation_dir(safe_id) / "agent_pty" / "events.jsonl"


def _agent_pty_screen_events_path(conversation_id: str) -> Path:
    safe_id = _sanitize_conversation_id(conversation_id)
    return _conversation_dir(safe_id) / "agent_pty" / "screen.jsonl"

def _agent_pty_raw_events_path(conversation_id: str) -> Path:
    safe_id = _sanitize_conversation_id(conversation_id)
    return _conversation_dir(safe_id) / "agent_pty" / "raw_events.jsonl"


async def _ensure_agent_pty_event_tailer(conversation_id: str) -> None:
    if not conversation_id:
        return
    existing = _agent_pty_event_tasks.get(conversation_id)
    if existing and not existing.done():
        return

    async def _tail() -> None:
        path = _agent_pty_events_path(conversation_id)
        while True:
            try:
                if not path.exists():
                    await asyncio.sleep(0.5)
                    continue
                data = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
                # Track byte offsets (best-effort) to avoid rebroadcast loops.
                raw_bytes = data.encode("utf-8", errors="replace")
                offset = _agent_pty_ws_offsets.get(conversation_id, 0)
                if offset > len(raw_bytes):
                    offset = 0
                tail = raw_bytes[offset:]
                if not tail:
                    await asyncio.sleep(0.2)
                    continue
                for line in tail.splitlines():
                    try:
                        event = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    # Forward as-is to the UI websocket stream ONLY.
                    # Transcript writing is handled separately by _tail_agent_pty_events_to_transcript
                    # which uses its own offset tracking to avoid duplicates.
                    if isinstance(event, dict):
                        await _broadcast_appserver_ui(event)
                _agent_pty_ws_offsets[conversation_id] = len(raw_bytes)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)

    _agent_pty_event_tasks[conversation_id] = asyncio.create_task(_tail(), name=f"agent-pty-events:{conversation_id}")


async def _ensure_agent_pty_screen_event_tailer(conversation_id: str) -> None:
    if not conversation_id:
        return
    existing = _agent_pty_screen_event_tasks.get(conversation_id)
    if existing and not existing.done():
        return

    async def _tail() -> None:
        path = _agent_pty_screen_events_path(conversation_id)
        while True:
            try:
                if not path.exists():
                    await asyncio.sleep(0.5)
                    continue
                raw_bytes = await asyncio.to_thread(path.read_bytes)
                offset = _agent_pty_screen_ws_offsets.get(conversation_id, 0)
                if offset > len(raw_bytes):
                    offset = 0
                tail = raw_bytes[offset:]
                if not tail:
                    await asyncio.sleep(0.2)
                    continue
                for line in tail.splitlines():
                    try:
                        event = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    if isinstance(event, dict):
                        await _broadcast_appserver_ui(event)
                _agent_pty_screen_ws_offsets[conversation_id] = len(raw_bytes)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)

    _agent_pty_screen_event_tasks[conversation_id] = asyncio.create_task(
        _tail(), name=f"agent-pty-screen:{conversation_id}"
    )


async def _ensure_agent_pty_raw_event_tailer(conversation_id: str) -> None:
    if not conversation_id:
        return
    existing = _agent_pty_raw_event_tasks.get(conversation_id)
    if existing and not existing.done():
        return

    async def _tail() -> None:
        path = _agent_pty_raw_events_path(conversation_id)
        while True:
            try:
                if not path.exists():
                    await asyncio.sleep(0.5)
                    continue
                raw_bytes = await asyncio.to_thread(path.read_bytes)
                offset = _agent_pty_raw_ws_offsets.get(conversation_id, 0)
                if offset > len(raw_bytes):
                    offset = 0
                tail = raw_bytes[offset:]
                if not tail:
                    await asyncio.sleep(0.2)
                    continue
                for line in tail.splitlines():
                    try:
                        event = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    if isinstance(event, dict):
                        await _broadcast_appserver_ui(event)
                _agent_pty_raw_ws_offsets[conversation_id] = len(raw_bytes)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)

    _agent_pty_raw_event_tasks[conversation_id] = asyncio.create_task(
        _tail(), name=f"agent-pty-raw:{conversation_id}"
    )


async def _agent_pty_monitor_loop() -> None:
    while True:
        try:
            async with _config_lock:
                cfg = _load_appserver_config()
            convo_id = cfg.get("conversation_id")
            if isinstance(convo_id, str) and convo_id:
                # Agent PTY event tailers are debug tooling; they are noisy and can produce
                # duplicate user-facing cards. Keep them behind DEBUG_MODE.
                if DEBUG_MODE:
                    await _ensure_agent_pty_event_tailer(convo_id)
                    await _ensure_agent_pty_screen_event_tailer(convo_id)
                    await _ensure_agent_pty_raw_event_tailer(convo_id)
                # Do not mirror agent PTY block events into the transcript by default.
                # The user-facing terminal "command cards" are derived from deterministic
                # user-terminal markers + bytes slicing, and agent PTY begin/end events
                # are noisy/duplicative in the SSOT transcript.
                #
                # If you ever need these for debugging or replay of MCP tool calls, we
                # can re-enable behind an explicit setting flag.
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.5)


# =============================================================================
# EVENT ROUTER
# =============================================================================
# This router processes events from codex-app-server and:
#   1. Emits frontend events via WebSocket for live UI updates (streaming deltas)
#   2. Writes to transcript SSOT for replay/persistence
#
# Event flow:
#   codex-app-server stdout -> _appserver_reader -> _route_appserver_event
#                                                         |
#                                     +-------------------+-------------------+
#                                     |                                       |
#                            [Frontend Events]                      [Transcript SSOT]
#                            via _broadcast_appserver_ui()          via _append_transcript_entry()
#                            - Streaming deltas                     - Complete items for replay
#                            - Activity indicators                  - User messages, assistant msgs
#                            - Approvals                            - Commands, diffs, plans
# =============================================================================

async def _route_appserver_event(
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str],
    request_id: Optional[str] = None,
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Route codex-app-server events to frontend (streaming) and transcript (replay).
    
    Returns a tuple of (resolved_conversation_id, events_list).
    The conversation_id is resolved from thread_id lookup first, then falls back to active.
    Events should be broadcast with conversation_id so frontend can filter.
    Also writes completed items to the transcript SSOT for replay.
    """
    events: List[Dict[str, Any]] = []
    if not label:
        return None, events

    # Extract thread_id from payload FIRST - this is the authoritative source
    thread_id = _get_thread_id(conversation_id, payload)
    
    # Resolve conversation_id: prioritize thread_id lookup over active conversation
    # This ensures events route to the correct conversation even when viewing a different one
    convo_id: Optional[str] = None
    if thread_id:
        convo_id = _find_conversation_by_thread_id(thread_id)
    
    # Fallback to active conversation only if thread_id lookup fails
    if not convo_id:
        async with _config_lock:
            cfg = _load_appserver_config()
            convo_id = cfg.get("conversation_id")
    
    if not convo_id:
        convo_id = await _ensure_conversation()
    turn_id = _get_turn_id(payload)
    item_id = None
    if isinstance(payload, dict):
        item_id = payload.get("itemId") or payload.get("item_id")
        if not item_id and isinstance(payload.get("item"), dict):
            item_id = payload["item"].get("id")
    if not thread_id:
        thread_id = _load_appserver_config().get("thread_id")
    state = _get_state_for_item(thread_id, turn_id, item_id)
    if thread_id:
        await _set_thread_id(thread_id)

    label_lower = label.lower()

    # -------------------------------------------------------------------------
    # SECTION: Approval Events (Frontend only - user interaction required)
    # -------------------------------------------------------------------------
    # These events require user interaction and are only sent to frontend.
    # Approval decisions are recorded to transcript separately via /approval_record.
    
    if "commandexecution/requestapproval" in label_lower:
        if isinstance(payload, dict):
            item_id = payload.get("itemId") or payload.get("item_id") or payload.get("id")
            if item_id and request_id is not None:
                _approval_request_map[str(item_id)] = str(request_id)
            resolved_id = request_id if request_id is not None else payload.get("_request_id")
            if resolved_id is None and item_id:
                resolved_id = _approval_request_map.get(str(item_id))
            cached = _approval_item_cache.get(str(item_id)) if item_id else {}
            events.append({
                "type": "approval",
                "kind": "command",
                "id": resolved_id,
                "payload": {
                    "command": payload.get("command") or payload.get("parsedCmd") or payload.get("cmd") or cached.get("command"),
                    "cwd": payload.get("cwd") or cached.get("cwd"),
                    "reason": payload.get("reason"),
                    "risk": payload.get("risk"),
                },
            })
            events.append({"type": "activity", "label": "approval", "active": True})
        return convo_id, events

    # Legacy apply_patch_approval_request - cache the diff by call_id for later approval
    if "apply_patch_approval_request" in label_lower:
        if isinstance(payload, dict):
            call_id = payload.get("call_id")
            changes = payload.get("changes")
            if call_id and changes:
                # Extract unified diff from changes dict
                diff_parts = []
                for path, change in changes.items():
                    if isinstance(change, dict) and change.get("unified_diff"):
                        diff_parts.append(f"--- {path}\n+++ {path}\n{change.get('unified_diff')}")
                _approval_item_cache[str(call_id)] = {
                    "diff": "\n".join(diff_parts) if diff_parts else None,
                    "changes": changes,
                }
        # Don't return - let it fall through to filechange/requestapproval handler if also matches

    if "filechange/requestapproval" in label_lower or "applypatchapproval" in label_lower:
        if isinstance(payload, dict):
            item_id = payload.get("itemId") or payload.get("item_id") or payload.get("call_id") or payload.get("id")
            if item_id and request_id is not None:
                _approval_request_map[str(item_id)] = str(request_id)
            resolved_id = request_id if request_id is not None else payload.get("_request_id")
            if resolved_id is None and item_id:
                resolved_id = _approval_request_map.get(str(item_id))
            cached = _approval_item_cache.get(str(item_id)) if item_id else {}
            events.append({
                "type": "approval",
                "kind": "diff",
                "id": resolved_id,
                "payload": {
                    "diff": payload.get("diff") or payload.get("patch") or payload.get("unified_diff") or cached.get("diff"),
                    "changes": payload.get("changes") or cached.get("changes"),
                    "reason": payload.get("reason"),
                },
            })
            events.append({"type": "activity", "label": "approval", "active": True})
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Turn Lifecycle Events (Frontend + Transcript)
    # -------------------------------------------------------------------------
    # Turn start/complete events update UI activity state and write status to
    # transcript for replay. Plans are accumulated during turn and written on complete.
    
    if label_lower == "thread/started":
        # [Frontend] Activity indicator only
        events.append({"type": "activity", "label": "thread started", "active": True})
        # Best-effort: persist a "thread session marker" so a fresh frontend
        # can decide whether it must `thread/resume` for this conversation.
        try:
            # Try to extract thread id from payload if present.
            thread_obj = payload.get("thread", {}) if isinstance(payload, dict) else {}
            thread_id_from_event = (
                thread_obj.get("id")
                or payload.get("threadId")
                or payload.get("thread_id")
                or payload.get("id")
            )
            if isinstance(thread_id_from_event, str) and thread_id_from_event:
                await _set_thread_id(thread_id_from_event)
            async with _config_lock:
                cfg = _load_appserver_config()
            shell_id = cfg.get("shell_id")
            convo_id_local = cfg.get("conversation_id")
            if (
                isinstance(convo_id_local, str)
                and convo_id_local
                and isinstance(shell_id, str)
                and shell_id
                and _conversation_meta_path(convo_id_local).exists()
            ):
                meta = _load_conversation_meta(convo_id_local)
                settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
                if shell_id:
                    settings["thread_session_shell_id"] = shell_id
                if isinstance(thread_id_from_event, str) and thread_id_from_event:
                    settings["thread_session_thread_id"] = thread_id_from_event
                meta["settings"] = settings
                _save_conversation_meta(convo_id_local, meta)
        except Exception:
            pass
        return convo_id, events

    if label_lower in {"turn/started", "turn/completed"}:
        if label_lower == "turn/started":
            await _set_turn_id(turn_id)
        else:
            await _set_turn_id(None)
            state["thought_buffer"] = ""
            # Determine turn status from payload
            turn_obj = payload.get("turn", {}) if isinstance(payload, dict) else {}
            turn_status = turn_obj.get("status", "completed")  # completed, interrupted, failed, inProgress
            turn_error = turn_obj.get("error")
            # Map to ribbon status
            if turn_status == "failed":
                ribbon_status = "error"
            elif turn_status == "interrupted":
                ribbon_status = "warning"
            else:
                ribbon_status = "success"
            # Write status to transcript and emit to frontend
            if convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "turn_id": turn_id,
                    "error": turn_error,
                    "event": "turn/completed",
                })
            events.append({
                "type": "status",
                "status": ribbon_status,
                "turn_status": turn_status,
                "error": turn_error,
            })
            # On turn completion, write accumulated plan to transcript if any steps exist
            plan_steps = state.get("plan_steps", [])
            if plan_steps and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "plan",
                    "steps": plan_steps,
                    "turn_id": turn_id,
                    "event": "turn/completed",
                })
                # Also emit to frontend for live display
                events.append({
                    "type": "plan",
                    "steps": plan_steps,
                })
            # Clear plan state for next turn
            state["plan_steps"] = []
        events.append({"type": "activity", "label": "turn started" if label_lower == "turn/started" else "idle", "active": label_lower == "turn/started"})
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Diff Events (Frontend + Transcript for Replay)
    # -------------------------------------------------------------------------
    # Unified diffs are emitted to frontend for display and written to transcript
    # for replay. We dedupe diffs to avoid showing the same change multiple times.
    
    if label_lower == "turn/diff/updated" and isinstance(payload, dict):
        diff, path = _extract_diff_with_path(payload)
        if diff:
            # [Frontend] diff event + [Transcript] for replay
            await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item_id, events, path=path)
        return convo_id, events

    if label_lower == "codex/event/turn_diff" and isinstance(payload, dict):
        diff, path = _extract_diff_with_path(payload)
        if diff:
            # [Frontend] diff event + [Transcript] for replay
            await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item_id, events, path=path)
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Token Usage (Frontend + Transcript for Replay)
    # -------------------------------------------------------------------------
    # Token counts update the context window display and are saved for replay.
    
    if label_lower in {"codex/event/token_count", "thread/tokenusage/updated"} and isinstance(payload, dict):
        total = None
        input_tokens = None
        cached_input_tokens = None
        context_window = None
        
        # Handle codex/event/token_count format: { info: { last_token_usage: {...}, model_context_window } }
        # Use "last_token_usage" (current turn) not "total_token_usage" (cumulative)
        if isinstance(payload.get("info"), dict):
            info = payload["info"]
            usage = info.get("last_token_usage") or {}
            if isinstance(usage, dict):
                # For context %, use last turn's input_tokens
                total = usage.get("input_tokens")
                input_tokens = usage.get("input_tokens")
                cached_input_tokens = usage.get("cached_input_tokens")
            context_window = info.get("model_context_window")
        
        # Handle thread/tokenUsage/updated format: { tokenUsage: { last: {inputTokens, ...}, modelContextWindow } }
        # Use "last" (current turn) for context percentage, not "total" (cumulative)
        if total is None and isinstance(payload.get("tokenUsage"), dict):
            token_usage = payload["tokenUsage"]
            # Use "last" breakdown - this is the current turn's token usage
            last_breakdown = token_usage.get("last") or {}
            if isinstance(last_breakdown, dict):
                # For context %, use last.inputTokens (what's in the context window now)
                total = last_breakdown.get("inputTokens") or last_breakdown.get("input_tokens")
                input_tokens = last_breakdown.get("inputTokens") or last_breakdown.get("input_tokens")
                cached_input_tokens = last_breakdown.get("cachedInputTokens") or last_breakdown.get("cached_input_tokens")
            context_window = token_usage.get("modelContextWindow") or token_usage.get("model_context_window")
        
        # Fallback to direct fields
        if total is None:
            total = payload.get("total") or payload.get("total_tokens") or payload.get("tokenCount")
        if context_window is None:
            context_window = payload.get("model_context_window") or payload.get("modelContextWindow")
        
        if isinstance(total, (int, float)):
            total_int = int(total)
            event = {"type": "token_count", "total": total_int}
            
            # Calculate active context: input_tokens - cached_input_tokens
            # This is what actually counts against the context window
            if isinstance(input_tokens, (int, float)) and isinstance(cached_input_tokens, (int, float)):
                active_context = int(input_tokens) - int(cached_input_tokens)
                event["active_context"] = max(0, active_context)
                event["input_tokens"] = int(input_tokens)
                event["cached_input_tokens"] = int(cached_input_tokens)
            
            if isinstance(context_window, (int, float)):
                context_window_int = int(context_window)
                event["context_window"] = context_window_int
                # Save to transcript for replay
                if convo_id:
                    await _append_transcript_entry(convo_id, {
                        "role": "token_usage",
                        "total": total_int,
                        "active_context": event.get("active_context"),
                        "input_tokens": event.get("input_tokens"),
                        "cached_input_tokens": event.get("cached_input_tokens"),
                        "context_window": context_window_int,
                        "event": label_lower,
                    })
            events.append(event)
        return convo_id, events

    # Context compacted event - agent dropped some history to fit context window
    if label_lower in {"thread/compacted", "context_compacted", "codex/event/context_compacted"} and isinstance(payload, dict):
        thread_id_compact = payload.get("threadId") or payload.get("thread_id") or thread_id
        turn_id_compact = payload.get("turnId") or payload.get("turn_id") or turn_id
        # [Transcript] Record compaction event
        if convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "context_compacted",
                "thread_id": thread_id_compact,
                "turn_id": turn_id_compact,
                "event": label_lower,
            })
        # [Frontend] Notify user that context was compacted
        events.append({
            "type": "context_compacted",
            "thread_id": thread_id_compact,
            "turn_id": turn_id_compact,
        })
        events.append({"type": "activity", "label": "context compacted", "active": True})
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Error/Warning Events (Frontend + Transcript for Replay)
    # -------------------------------------------------------------------------
    
    if label_lower in {"codex/event/error", "error"} and isinstance(payload, dict):
        error_obj = payload.get("error") or payload
        message = error_obj.get("message") or str(error_obj)
        # [Transcript] Store for replay
        if convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "error",
                "text": message,
                "event": label_lower,
            })
        # [Frontend] Error display
        events.append({
            "type": "error",
            "message": message,
        })
        events.append({"type": "activity", "label": "error", "active": False})
        return convo_id, events

    if label_lower == "codex/event/warning" and isinstance(payload, dict):
        # [Frontend only] Warnings not persisted to transcript
        message = payload.get("message") or payload.get("msg", {}).get("message") or ""
        if message:
            events.append({
                "type": "warning",
                "message": message,
            })
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Suppressed Events (No action)
    # -------------------------------------------------------------------------
    # These events are noisy or redundant - we handle their data elsewhere.
    
    if label_lower in {
        "codex/event/item_started",
        "codex/event/item_completed",
        "codex/event/user_message",
        "codex/event/task_complete",
        "codex/event/task_started",
        "codex/event/mcp_startup_complete",
        "account/ratelimits/updated",
    }:
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Plan Events (Frontend streaming + Transcript on turn complete)
    # -------------------------------------------------------------------------
    # Plan updates stream to frontend for live overlay. Full plan is written to
    # transcript on turn/completed for replay.
    
    if label_lower == "codex/event/plan_update" and isinstance(payload, dict):
        plan_steps = payload.get("plan")
        if isinstance(plan_steps, list):
            normalized_steps = []
            for step_obj in plan_steps:
                if isinstance(step_obj, dict):
                    step = step_obj.get("step")
                    status = step_obj.get("status")
                    if step:
                        normalized_steps.append({
                            "step": step,
                            "status": status or "pending",
                        })
                        # [Frontend] Live overlay update
                        events.append({
                            "type": "plan_update",
                            "step": step,
                            "status": status or "pending",
                        })
            state["plan_steps"] = normalized_steps
        return convo_id, events

    if label_lower == "turn/plan/updated" and isinstance(payload, dict):
        # [Frontend] Live overlay + accumulate for [Transcript] on turn/completed
        plan_steps = payload.get("plan")
        if isinstance(plan_steps, list):
            normalized_steps = []
            for step_obj in plan_steps:
                if isinstance(step_obj, dict):
                    step = step_obj.get("step")
                    status = step_obj.get("status")  # pending, inProgress, completed
                    if step:
                        # Normalize status
                        normalized_status = status
                        if status == "inProgress":
                            normalized_status = "in_progress"
                        normalized_steps.append({
                            "step": step,
                            "status": normalized_status or "pending",
                        })
                        # Emit for live overlay
                        events.append({
                            "type": "plan_update",
                            "step": step,
                            "status": normalized_status or "pending",
                        })
            state["plan_steps"] = normalized_steps
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Item Events (Frontend streaming deltas + Transcript on complete)
    # -------------------------------------------------------------------------
    # Item lifecycle: item/started -> deltas -> item/completed
    # - Deltas stream to frontend for live display
    # - Complete items written to transcript for replay
    
    if label_lower == "item/started" and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        item_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
        
        # Handle user messages (both legacy "usermessage" and ResponseItem "message" with role="user")
        is_user_message = (
            item_type == "usermessage" or
            (item_type == "message" and str(item.get("role") or "").lower() == "user")
        )
        if is_user_message:
            # [Transcript] User messages saved immediately
            entry = _extract_item_text(item)
            if entry and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": entry["role"],
                    "text": entry["text"],
                    "item_id": item.get("id"),
                    "event": "item/started",
                })
            # [Frontend] Display user message
            if entry:
                events.append({"type": "message", "role": "user", "id": item.get("id"), "text": entry["text"]})
            return convo_id, events
            
        if item_type == "reasoning":
            # Track state for delta accumulation
            state["reason_source"] = state["reason_source"] or "item"
            if item.get("id"):
                state["reasoning_id"] = item.get("id")
                _register_item_state(item.get("id"), state)
            return convo_id, events
            
        if item_type == "filechange":
            # Cache diff info for approval - actual diff emitted via turn_diff
            diff, path = _extract_diff_with_path(item)
            if item.get("id"):
                _approval_item_cache[str(item.get("id"))] = {
                    "diff": diff,
                    "changes": item.get("changes"),
                    "path": path,
                }
            return convo_id, events
            
        if item_type == "commandexecution":
            # Cache command info, show activity
            command = item.get("command") or item.get("cmd") or item.get("argv") or ""
            cwd = item.get("cwd") or ""
            tool_id = _tool_event_id(label_lower, item, thread_id, turn_id)
            if item.get("id"):
                _approval_item_cache[str(item.get("id"))] = {
                    "command": command,
                    "cwd": cwd,
                    "tool_id": tool_id,
                }
            # [Frontend] Tool begin for command execution (creates tool:command row for streaming)
            events.append({
                "type": "tool_begin",
                "id": tool_id,
                "tool": "command",
                "arguments": {"command": command, "cwd": cwd} if command else {},
            })
            events.append({"type": "activity", "label": "running command", "active": True})
            return convo_id, events
            
        if item_type in {"agentmessage", "assistantmessage", "assistant"}:
            # Track state for delta accumulation
            state["msg_source"] = state["msg_source"] or "item"
            if item.get("id"):
                state["assistant_id"] = item.get("id")
                _register_item_state(item.get("id"), state)
            state["assistant_started"] = True
            return convo_id, events

    if label_lower == "item/completed" and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        item_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
        
        if item_type in {"agentmessage", "assistantmessage", "assistant"}:
            entry = _extract_item_text(item)
            # [Transcript] Save complete assistant message for replay
            if entry and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": entry["role"],
                    "text": entry["text"],
                    "item_id": item.get("id"),
                    "event": "item/completed",
                })
            # [Frontend] Finalize streaming message
            if state.get("msg_source") in {None, "item"} and state.get("assistant_started"):
                events.append({"type": "assistant_finalize", "id": item.get("id") or state.get("assistant_id") or "assistant", "text": entry["text"] if entry else item.get("text")})
            events.append({"type": "activity", "label": "idle", "active": False})
            return convo_id, events
            
        if item_type == "reasoning":
            summary = item.get("summary") or item.get("summary_text") or []
            if isinstance(summary, list) and summary:
                text = " ".join(str(s) for s in summary if s).strip()
            else:
                text = str(summary).strip() if summary else ""
            # Scrub thought titles from complete reasoning text
            scrubbed_text, thoughts = _extract_and_scrub_thoughts(text)
            # Emit thought titles to ribbon (smooth transition for mid-stream conversation switch)
            for thought in thoughts:
                events.append({"type": "thought", "text": thought})
            # [Transcript] Save complete reasoning for replay (scrubbed)
            if scrubbed_text and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "reasoning",
                    "text": scrubbed_text,
                    "item_id": item.get("id"),
                    "event": "item/completed",
                })
            # [Frontend] Finalize streaming reasoning (scrubbed)
            if state.get("reason_source") in {None, "item"} and state.get("reasoning_started"):
                events.append({"type": "reasoning_finalize", "id": item.get("id") or state.get("reasoning_id") or "reasoning", "text": scrubbed_text})
                state["reasoning_started"] = False
                state["reasoning_buffer"] = ""
                state["reasoning_id"] = None
            state["thought_buffer"] = ""
            return convo_id, events
            
        if item_type == "filechange":
            # Cache for approval tracking - diff emitted via turn_diff
            diff, path = _extract_diff_with_path(item)
            if item.get("id") and diff:
                _approval_item_cache[str(item.get("id"))] = {
                    "diff": diff,
                    "changes": item.get("changes"),
                    "path": path,
                }
            return convo_id, events
            
        if item_type == "commandexecution":
            command = item.get("command") or item.get("cmd") or item.get("argv") or ""
            cwd = item.get("cwd") or ""
            output = item.get("aggregatedOutput") or item.get("output") or item.get("stdout") or ""
            exit_code = item.get("exitCode") if item.get("exitCode") is not None else item.get("exit_code")
            duration_ms = item.get("durationMs") if item.get("durationMs") is not None else item.get("duration_ms")
            if isinstance(output, str):
                output = output.replace("\r\n", "\n").replace("\r", "\n")
            
            # Get tool_id from cache (set in item/started)
            cached = _approval_item_cache.get(str(item.get("id"))) or {}
            tool_id = cached.get("tool_id") or _tool_event_id(label_lower, item, thread_id, turn_id)
            
            # [Frontend] Tool end for command execution (closes tool:command row)
            events.append({
                "type": "tool_end",
                "id": tool_id,
                "tool": "command",
                "arguments": {"command": command, "cwd": cwd},
                "result": {"exit_code": exit_code, "output_lines": len(output.split('\n')) if output else 0},
                "duration_ms": duration_ms,
                "is_error": exit_code not in (None, 0),
            })
            
            # [Transcript] Save command result for replay
            if convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "command",
                    "command": command,
                    "cwd": cwd,
                    "output": output,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "item_id": item.get("id"),
                    "event": "item/completed",
                })
            # [Frontend] Display command result (full output)
            events.append({
                "type": "command_result",
                "id": item.get("id"),
                "command": command,
                "cwd": cwd,
                "output": output,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
            })
            events.append({"type": "activity", "label": "processing", "active": True})
            return convo_id, events
            
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Streaming Delta Events (Frontend only - not persisted)
    # -------------------------------------------------------------------------
    # Delta events stream token-by-token to frontend for live display.
    # Complete content is persisted on item/completed, not during streaming.
    
    # --- Assistant Message Deltas ---
    if label_lower == "item/agentmessage/delta" and isinstance(payload, dict):
        # [Frontend] Stream text delta
        if state["msg_source"] in {None, "item"}:
            state["msg_source"] = "item"
            item_id = payload.get("itemId") or payload.get("id") or state.get("assistant_id") or "assistant"
            if item_id:
                state["assistant_id"] = item_id
                _register_item_state(item_id, state)
            state["assistant_started"] = True
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                events.append({"type": "assistant_delta", "id": item_id, "delta": delta})
                events.append({"type": "activity", "label": "responding", "active": True})
        return convo_id, events

    # --- Reasoning Deltas ---
    if label_lower in {"item/reasoning/summarytextdelta", "item/reasoning/textdelta"} and isinstance(payload, dict):
        # [Frontend] Stream reasoning delta
        if state["reason_source"] in {None, "item"}:
            state["reason_source"] = "item"
            item_id = payload.get("itemId") or payload.get("id") or state.get("reasoning_id") or "reasoning"
            if item_id:
                state["reasoning_id"] = item_id
                _register_item_state(item_id, state)
            state["reasoning_started"] = True
            delta = payload.get("delta")
            if isinstance(delta, str):
                state["reasoning_buffer"] = state.get("reasoning_buffer", "") + delta
                # Extract thought titles from reasoning and show in status ribbon
                scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, state)
                for thought in thoughts:
                    events.append({"type": "thought", "text": thought})
                if scrubbed_delta:
                    events.append({"type": "reasoning_delta", "id": item_id, "delta": scrubbed_delta})
                # Show thought in activity or default to reasoning
                if thoughts:
                    events.append({"type": "activity", "label": thoughts[-1], "active": True})
                elif scrubbed_delta:
                    events.append({"type": "activity", "label": "reasoning", "active": True})
        return convo_id, events

    if label_lower == "item/reasoning/summarypartadded" and isinstance(payload, dict):
        # [Frontend] Reasoning section break
        if state["reason_source"] in {None, "item"}:
            state["reason_source"] = "item"
            item_id = payload.get("itemId") or payload.get("id") or state.get("reasoning_id") or "reasoning"
            if item_id:
                state["reasoning_id"] = item_id
                _register_item_state(item_id, state)
            state["reasoning_started"] = True
            state["reasoning_buffer"] = state.get("reasoning_buffer", "") + "\n"
            events.append({"type": "reasoning_delta", "id": item_id, "delta": "\n"})
        return convo_id, events

    # --- Legacy Codex Event Deltas (alternate protocol) ---
    if label_lower in {"codex/event/agent_message_content_delta", "codex/event/agent_message_delta"} and isinstance(payload, dict):
        # [Frontend] Stream text delta (legacy format)
        if state["msg_source"] in {None, "codex"}:
            state["msg_source"] = "codex"
            item_id = payload.get("item_id") or payload.get("itemId") or state.get("assistant_id") or "assistant"
            if item_id:
                state["assistant_id"] = item_id
                _register_item_state(item_id, state)
            state["assistant_started"] = True
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                events.append({"type": "assistant_delta", "id": item_id, "delta": delta})
                events.append({"type": "activity", "label": "responding", "active": True})
        return convo_id, events

    if label_lower == "codex/event/agent_message" and isinstance(payload, dict):
        # [Transcript] + [Frontend] Complete message (legacy format)
        text = payload.get("message") or payload.get("text")
        if isinstance(text, str):
            if convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "assistant",
                    "text": text.strip(),
                    "item_id": payload.get("item_id") or payload.get("itemId"),
                    "event": "agent_message",
                })
            if state.get("msg_source") in {None, "codex"} and state.get("assistant_started"):
                events.append({"type": "assistant_finalize", "id": payload.get("item_id") or payload.get("itemId") or state.get("assistant_id") or "assistant", "text": text.strip()})
        return convo_id, events

    if label_lower in {"codex/event/agent_reasoning_delta", "codex/event/reasoning_content_delta", "codex/event/reasoning_summary_delta"} and isinstance(payload, dict):
        # [Frontend] Stream reasoning delta (legacy format)
        if state["reason_source"] in {None, "codex"}:
            state["reason_source"] = "codex"
            item_id = payload.get("item_id") or payload.get("itemId") or state.get("reasoning_id") or "reasoning"
            if item_id:
                state["reasoning_id"] = item_id
                _register_item_state(item_id, state)
            state["reasoning_started"] = True
            delta = payload.get("delta")
            if isinstance(delta, str):
                state["reasoning_buffer"] = state.get("reasoning_buffer", "") + delta
                # Extract thought titles from reasoning and show in status ribbon
                scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, state)
                for thought in thoughts:
                    events.append({"type": "thought", "text": thought})
                if scrubbed_delta:
                    events.append({"type": "reasoning_delta", "id": item_id, "delta": scrubbed_delta})
                # Show thought in activity or default to reasoning
                if thoughts:
                    events.append({"type": "activity", "label": thoughts[-1], "active": True})
                elif scrubbed_delta:
                    events.append({"type": "activity", "label": "reasoning", "active": True})
        return convo_id, events

    if label_lower == "codex/event/agent_reasoning_section_break" and isinstance(payload, dict):
        # [Frontend] Reasoning section break (legacy format)
        if state["reason_source"] in {None, "codex"}:
            state["reason_source"] = "codex"
            item_id = payload.get("item_id") or payload.get("itemId") or state.get("reasoning_id") or "reasoning"
            if item_id:
                state["reasoning_id"] = item_id
                _register_item_state(item_id, state)
            state["reasoning_started"] = True
            state["reasoning_buffer"] = state.get("reasoning_buffer", "") + "\n\n"
            events.append({"type": "reasoning_delta", "id": item_id, "delta": "\n\n"})
        return convo_id, events

    if label_lower == "codex/event/agent_reasoning" and isinstance(payload, dict):
        # [Frontend] Finalize reasoning (legacy format)
        text = payload.get("text") or payload.get("message")
        # Scrub thought titles from complete reasoning text
        scrubbed_text, thoughts = _extract_and_scrub_thoughts(text) if text else ("", [])
        # Emit thought titles to ribbon (smooth transition for mid-stream conversation switch)
        for thought in thoughts:
            events.append({"type": "thought", "text": thought})
        if state.get("reason_source") in {None, "codex"} and state.get("reasoning_started"):
            events.append({"type": "reasoning_finalize", "id": payload.get("item_id") or payload.get("itemId") or state.get("reasoning_id") or "reasoning", "text": scrubbed_text})
            state["reasoning_started"] = False
            state["reasoning_buffer"] = ""
            state["reasoning_id"] = None
        state["thought_buffer"] = ""
        return convo_id, events

    # -------------------------------------------------------------------------
    # SECTION: Tool/Command Execution Events (Frontend streaming)
    # -------------------------------------------------------------------------
    # Command output deltas stream to frontend. Complete output is captured
    # on item/completed for transcript.
    
    if label_lower in {"exec_command_begin", "exec_command_output_delta", "exec_command_end"} and isinstance(payload, dict):
        # Legacy protocol - activity indicators only
        if label_lower == "exec_command_begin":
            events.append({"type": "activity", "label": "running command", "active": True})
        elif label_lower == "exec_command_end":
            events.append({"type": "activity", "label": "processing", "active": True})
        return convo_id, events

    if label_lower in {"item/commandexecution/outputdelta", "item/commandexecution/terminalinteraction"} and isinstance(payload, dict):
        # [Frontend] Stream command output deltas
        tool_id = _tool_event_id(label_lower, payload, thread_id, turn_id)
        if label_lower.endswith("outputdelta"):
            delta = payload.get("delta") or payload.get("output") or payload.get("stdout") or ""
            if isinstance(delta, str) and delta:
                events.append({
                    "type": "tool_delta",
                    "id": tool_id,
                    "tool": "command",
                    "delta": delta,
                })
        else:
            events.append({
                "type": "tool_interaction",
                "id": tool_id,
                "tool": "command",
                "payload": {
                    "stdin": payload.get("stdin"),
                    "stdout": payload.get("stdout"),
                    "pid": payload.get("pid"),
                },
            })
        return convo_id, events

    if ("mcp_tool_call_begin" in label_lower or "mcp_tool_call_end" in label_lower) and isinstance(payload, dict):
        # [Frontend + Transcript] MCP tool call begin/end
        # payload might be params wrapper with 'msg' inside, or the msg itself
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        call_id = msg.get("call_id") or ""
        invocation = msg.get("invocation") or {}
        server_name = invocation.get("server") or "mcp"
        tool_name = invocation.get("tool") or "unknown"
        arguments = invocation.get("arguments") or {}
        tool_id = f"mcp:{server_name}:{tool_name}:{call_id}"
        
        if "begin" in label_lower:
            # [Frontend] Tool begin event
            events.append({
                "type": "tool_begin",
                "id": tool_id,
                "tool": tool_name,
                "server": server_name,
                "arguments": arguments,
            })
            events.append({"type": "activity", "label": f"calling {tool_name}", "active": True})
        else:
            # Parse result - can be {Ok: ...} or {Err: ...}
            result_raw = msg.get("result") or {}
            duration_raw = msg.get("duration") or {}
            
            # Duration is {secs, nanos} - convert to ms
            if isinstance(duration_raw, dict):
                secs = duration_raw.get("secs", 0)
                nanos = duration_raw.get("nanos", 0)
                duration_ms = secs * 1000 + nanos // 1_000_000
            else:
                duration_ms = 0
            
            # Extract the actual result data from Ok/Err wrapper
            is_error = False
            result_data = None
            if isinstance(result_raw, dict):
                if "Ok" in result_raw:
                    ok_data = result_raw["Ok"]
                    is_error = ok_data.get("isError", False) if isinstance(ok_data, dict) else False
                    # Prefer structuredContent.result if available (actual JSON result)
                    if isinstance(ok_data, dict) and isinstance(ok_data.get("structuredContent"), dict):
                        result_data = ok_data["structuredContent"].get("result")
                    # Fallback to content[0].text if no structured content
                    if result_data is None and isinstance(ok_data, dict):
                        content = ok_data.get("content", [])
                        if content and isinstance(content[0], dict) and content[0].get("text"):
                            # Try to parse the text as JSON
                            try:
                                result_data = json.loads(content[0]["text"])
                            except (json.JSONDecodeError, TypeError):
                                result_data = content[0]["text"]
                    if result_data is None:
                        result_data = ok_data
                elif "Err" in result_raw:
                    result_data = {"error": result_raw["Err"]}
                    is_error = True
                else:
                    result_data = result_raw
            else:
                result_data = result_raw
            
            # [Frontend] Tool end event with full result
            events.append({
                "type": "tool_end",
                "id": tool_id,
                "tool": tool_name,
                "server": server_name,
                "arguments": arguments,
                "result": result_data,
                "duration_ms": duration_ms,
                "is_error": is_error,
            })
            events.append({"type": "activity", "label": "processing", "active": True})
            
            # [Transcript] Record completed MCP tool call for replay
            if convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "mcp_tool",
                    "server": server_name,
                    "tool": tool_name,
                    "call_id": call_id,
                    "arguments": arguments,
                    "result": result_data,
                    "duration_ms": duration_ms,
                    "is_error": is_error,
                    "timestamp": utc_ts(),
                })
        return convo_id, events

    if ("web_search_begin" in label_lower or "web_search_end" in label_lower) and isinstance(payload, dict):
        # [Frontend + Transcript] Web search begin/end
        # payload might be params wrapper with 'msg' inside, or the msg itself
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        call_id = msg.get("call_id") or ""
        tool_id = f"web_search:{call_id}"
        
        if "begin" in label_lower:
            # [Frontend] Search begin
            events.append({
                "type": "tool_begin",
                "id": tool_id,
                "tool": "web_search",
            })
            events.append({"type": "activity", "label": "searching web", "active": True})
        else:
            query = msg.get("query") or ""
            # [Frontend] Search end
            events.append({
                "type": "tool_end",
                "id": tool_id,
                "tool": "web_search",
                "query": query,
            })
            events.append({"type": "activity", "label": "processing", "active": True})
            
            # [Transcript] Record completed web search for replay
            if convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "web_search",
                    "query": query,
                    "call_id": call_id,
                    "timestamp": utc_ts(),
                })
        return convo_id, events

    # No handler matched - return empty events list
    return convo_id, events

# =============================================================================
# END EVENT ROUTER
# =============================================================================


async def _ensure_appserver_reader(shell_id: str) -> None:
    global _appserver_reader_task
    if _appserver_reader_task and not _appserver_reader_task.done():
        return

    async def _reader():
        mgr = await _get_fws_manager()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdout:
            return
        pending_label: Optional[str] = None
        buffer = b""
        max_buffer = 4_000_000

        async def _process_line(text: str) -> None:
            nonlocal pending_label
            text = text.strip()
            if not text:
                return
            await _broadcast_appserver_raw(text)

            # Handle label + JSON on same line.
            if "{" in text and not text.lstrip().startswith("{"):
                prefix, rest = text.split("{", 1)
                if prefix.strip() and rest.strip().startswith("{"):
                    pending_label = prefix.strip()
                    text = "{" + rest

            try:
                parsed = json.loads(text)
            except Exception:
                if "/" in text or text.endswith("started") or text.endswith("completed"):
                    pending_label = text
                return

            # JSON-RPC response (result/error) - forward as UI event
            if isinstance(parsed, dict) and "id" in parsed and ("result" in parsed or "error" in parsed) and "method" not in parsed:
                if pending_label:
                    pending_label = None
                if parsed.get("error"):
                    error_msg = parsed.get("error", {}).get("message", "")
                    req_id = str(parsed.get("id"))
                    
                    # Ignore "Already initialized" - harmless
                    if "Already initialized" in error_msg:
                        _pending_turn_starts.pop(req_id, None)
                        return
                    
                    # Auto-resume on "conversation not found" for pending turn/start
                    if "conversation not found" in error_msg and req_id in _pending_turn_starts:
                        original_payload = _pending_turn_starts.pop(req_id)
                        thread_id = original_payload.get("params", {}).get("threadId")
                        if thread_id:
                            print(f"[DEBUG] Auto-resuming thread {thread_id} after 'conversation not found'")
                            asyncio.create_task(_auto_resume_and_retry(thread_id, original_payload))
                            return  # Don't broadcast error to frontend
                    
                    # Clean up tracking for other errors
                    _pending_turn_starts.pop(req_id, None)
                    
                    await _broadcast_appserver_ui({
                        "type": "rpc_error",
                        "id": parsed.get("id"),
                        "message": parsed.get("error", {}).get("message"),
                        "code": parsed.get("error", {}).get("code"),
                    })
                else:
                    result = parsed.get("result")
                    if isinstance(result, dict):
                        thread = result.get("thread")
                        if isinstance(thread, dict) and thread.get("id"):
                            await _set_thread_id(str(thread.get("id")))
                    await _broadcast_appserver_ui({
                        "type": "rpc_response",
                        "id": parsed.get("id"),
                        "result": result,
                    })
                # Clean up pending turn/start tracking on any response
                _pending_turn_starts.pop(str(parsed.get("id")), None)
                waiter = _appserver_rpc_waiters.pop(str(parsed.get("id")), None)
                if waiter and not waiter.done():
                    waiter.set_result(parsed)
                return

            label = None
            payload: Any = parsed
            conversation_id = None
            request_id: Optional[str] = None
            if pending_label:
                label = pending_label
                pending_label = None
                if isinstance(parsed, dict) and isinstance(parsed.get("msg"), dict):
                    payload = parsed.get("msg")
                    conversation_id = parsed.get("conversationId")
                else:
                    payload = parsed
            elif isinstance(parsed, dict):
                if "method" in parsed:
                    label = parsed.get("method")
                    payload = parsed.get("params", parsed)
                    if parsed.get("id") is not None:
                        request_id = str(parsed.get("id"))
                elif isinstance(parsed.get("msg"), dict):
                    msg = parsed.get("msg", {})
                    label = f"codex/event/{msg.get('type', 'event')}"
                    payload = msg
                    conversation_id = parsed.get("conversationId")
                elif "type" in parsed:
                    label = str(parsed.get("type"))
                    payload = parsed
            if isinstance(parsed, dict) and parsed.get("conversationId"):
                conversation_id = parsed.get("conversationId")
            if isinstance(payload, dict):
                conversation_id = conversation_id or payload.get("threadId") or payload.get("thread_id")
                if not conversation_id and isinstance(payload.get("thread"), dict):
                    conversation_id = payload["thread"].get("id")
                if request_id is not None:
                    payload["_request_id"] = request_id

            resolved_convo_id, events = await _route_appserver_event(label, payload, conversation_id, request_id)
            for event in events:
                # Include conversation_id in every event so frontend can filter
                if resolved_convo_id:
                    event["conversation_id"] = resolved_convo_id
                await _broadcast_appserver_ui(event)

        try:
            while True:
                chunk = await state.process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > max_buffer and b"\n" not in buffer:
                    await _broadcast_appserver_raw("[warn] dropping oversized line")
                    buffer = b""
                    continue
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        await _process_line(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
            if buffer:
                try:
                    await _process_line(buffer.decode("utf-8", errors="replace"))
                except Exception:
                    pass
        except Exception:
            return

    _appserver_reader_task = asyncio.create_task(_reader(), name="appserver-stdout-reader")


async def _write_appserver(payload: Dict[str, Any]) -> None:
    shell_id = _appserver_shell_id
    if not shell_id:
        cfg = _load_appserver_config()
        shell_id = cfg.get("shell_id")
    if not shell_id:
        raise HTTPException(status_code=409, detail="app-server not running")
    mgr = await _get_fws_manager()
    state = mgr.get_pipe_state(shell_id)
    if not state or not state.process.stdin:
        raise HTTPException(status_code=409, detail="app-server pipe not available")
    line = json.dumps(payload, ensure_ascii=False)
    print(f"[DEBUG] Writing to appserver stdin: {line[:200]}...")
    state.process.stdin.write((line + "\n").encode("utf-8"))
    await state.process.stdin.drain()
    print(f"[DEBUG] Write complete")


async def _auto_resume_and_retry(thread_id: str, original_payload: Dict[str, Any]) -> None:
    """Auto-resume a thread and retry the original turn/start request.
    
    Called when codex-app-server returns "conversation not found" for a turn/start.
    Silently resumes the thread and re-sends the original request.
    """
    try:
        # Build thread/resume request
        resume_id = int(datetime.now(timezone.utc).timestamp() * 1000)
        resume_payload: Dict[str, Any] = {
            "id": resume_id,
            "method": "thread/resume",
            "params": {"threadId": thread_id}
        }
        
        # Inject settings from SSOT (same logic as in api_appserver_rpc)
        async with _config_lock:
            cfg = _load_appserver_config()
        convo_id = cfg.get("conversation_id")
        if convo_id:
            meta = _load_conversation_meta(convo_id)
            settings = meta.get("settings", {})
            params = resume_payload["params"]
            for key in ("model", "cwd", "approvalPolicy", "sandbox"):
                if key in settings and settings[key] and key not in params:
                    params[key] = settings[key]
        
        print(f"[DEBUG] Sending thread/resume for {thread_id}")
        await _write_appserver(resume_payload)
        
        # Wait briefly for resume to complete before retrying
        await asyncio.sleep(0.5)
        
        # Re-send original turn/start with a new request ID
        retry_id = int(datetime.now(timezone.utc).timestamp() * 1000) + 1
        retry_payload = original_payload.copy()
        retry_payload["id"] = retry_id
        
        print(f"[DEBUG] Retrying turn/start after resume: {retry_payload}")
        await _write_appserver(retry_payload)
        
    except Exception as e:
        print(f"[ERROR] Auto-resume failed for thread {thread_id}: {e}")


async def _ensure_appserver_initialized() -> None:
    global _appserver_initialized
    if _appserver_initialized:
        return
    try:
        await _write_appserver({
            "id": 0,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "agent_log_server",
                    "title": "Agent Log Server",
                    "version": "0.1.0"
                }
            }
        })
        await _write_appserver({
            "method": "initialized",
            "params": {}
        })
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if isinstance(detail, dict) and detail.get("message") == "Already initialized":
            _appserver_initialized = True
            return
        raise
    _appserver_initialized = True

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        data = json.dumps(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except Exception:
                pass

manager = ConnectionManager()

# --- Helpers ---
def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")

_next_msg_num: int = 1  # Auto-increment message number

def _init_msg_num() -> None:
    """Scan existing log, assign msg_num to any records missing it, set _next_msg_num."""
    global _next_msg_num
    if LOG_PATH is None or not LOG_PATH.exists():
        _next_msg_num = 1
        return
    
    # Read all records
    records = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    if not records:
        _next_msg_num = 1
        return
    
    # Check if any records need numbering
    needs_rewrite = any("msg_num" not in rec for rec in records)
    
    if needs_rewrite:
        # Assign sequential msg_num to all records
        for i, rec in enumerate(records, start=1):
            rec["msg_num"] = i
        # Rewrite the log file
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _next_msg_num = len(records) + 1
    else:
        # Find max existing msg_num
        max_num = max(rec.get("msg_num", 0) for rec in records)
        _next_msg_num = max_num + 1

def _delete_record_by_msg_num(msg_num: int) -> bool:
    """Delete a record by msg_num. Returns True if deleted, False if not found."""
    assert LOG_PATH is not None
    if not LOG_PATH.exists():
        return False
    
    records = []
    found = False
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("msg_num") == msg_num:
                    found = True
                    continue  # Skip this record (delete it)
                records.append(rec)
            except json.JSONDecodeError:
                continue
    
    if found:
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return found

async def append_record(record: Dict[str, Any]) -> None:
    global _next_msg_num
    assert LOG_PATH is not None
    async with _lock:
        # Auto-assign msg_num if not present
        if "msg_num" not in record:
            record["msg_num"] = _next_msg_num
            _next_msg_num += 1
        else:
            # Update _next_msg_num if record has higher number
            if isinstance(record["msg_num"], int) and record["msg_num"] >= _next_msg_num:
                _next_msg_num = record["msg_num"] + 1
        line = json.dumps(record, ensure_ascii=False)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    await manager.broadcast(record)

def read_records(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    assert LOG_PATH is not None
    if not LOG_PATH.exists():
        return []
    
    records = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    if limit is not None and limit > 0:
        return records[-limit:]
    return records

def get_record_by_msg_num(msg_num: int) -> Optional[Dict[str, Any]]:
    """Get a specific record by its msg_num."""
    assert LOG_PATH is not None
    if not LOG_PATH.exists():
        return None
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("msg_num") == msg_num:
                    return rec
            except json.JSONDecodeError:
                continue
    return None

# --- Models ---
class MessageIn(BaseModel):
    who: str
    message: str

# --- Routes ---
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    return templates.TemplateResponse("template.html", {"request": request})


@app.get("/appserver")
async def appserver_ui() -> FastHTMLResponse:
    return FastHTMLResponse(
        to_xml(
            Html(
            Head(
                Link(rel="stylesheet", href=_asset("/static/appserver.css")),
                Script(src="https://unpkg.com/htmx.org@1.9.12", defer=True),
                Script(src=_asset("/static/vendor/socket.io/socket.io.min.js"), defer=True),
                Script(src=_asset("/static/appserver.js"), defer=True),
            ),
            Body(
                Div(
                    Header(
                        Div(
                            H1("App Server"),
                            Small("Codex JSON-RPC • Framework-Shells pipe"),
                            cls="brand"
                        ),
                        Div(
                            Div(
                                Span("Status"),
                                Span("disconnected", id="appserver-status", cls="pill warn"),
                                cls="status-pill"
                            ),
                            Button("Start", id="appserver-start", cls="btn"),
                            Button("Stop", id="appserver-stop", cls="btn ghost"),
                            cls="toolbar"
                        ),
                        cls="topbar"
                    ),
                    Main(
                        Section(
                            H2("Project"),
                            Label(
                                Span("Root"),
                                Input(type="text", id="project-root", placeholder="/data/data/..."),
                            ),
                            Label(
                                Span("Command"),
                                Input(type="text", id="appserver-command", placeholder="codex-app-server"),
                            ),
                            Div(
                                Button("Pick CWD", id="pick-cwd", cls="btn ghost"),
                                Button("Apply", id="apply-project", cls="btn"),
                                cls="row"
                            ),
                            H3("Threads"),
                            Div(
                                Button("Refresh", id="threads-refresh", cls="btn ghost"),
                                Button("New", id="thread-new", cls="btn"),
                                cls="row"
                            ),
                            Ul(
                                Li("No threads yet", cls="muted"),
                                id="thread-list",
                                cls="thread-list"
                            ),
                            cls="panel"
                        ),
                        Section(
                            H2("Conversation"),
                            Div(
                                Div(id="timeline", cls="timeline"),
                                cls="timeline-wrap"
                            ),
                            Div(
                                Textarea(
                                    id="prompt",
                                    placeholder="Type a prompt... (Shift+Enter for newline)",
                                ),
                                Button("Send", id="turn-send", cls="btn primary"),
                                cls="composer"
                            ),
                            cls="panel wide"
                        ),
                        Section(
                            H2("Approvals"),
                            Div(
                                P("No pending approvals", cls="muted"),
                                id="approvals-list"
                            ),
                            H2("Diffs"),
                            Div(
                                P("No diffs yet", cls="muted"),
                                id="diffs-list"
                            ),
                            H2("Policy"),
                            Label(
                                Span("Sandbox"),
                                Input(type="text", id="policy-sandbox", placeholder="workspace-write"),
                            ),
                            Label(
                                Span("Approval"),
                                Input(type="text", id="policy-approval", placeholder="on-failure"),
                            ),
                            cls="panel"
                        ),
                        cls="grid"
                    ),
                    Footer(
                        Div(
                            Span("WS"),
                            Span("idle", id="ws-status", cls="pill"),
                            cls="status-pill"
                        ),
                        Div(
                            Span("Mode"),
                            Span("portrait-friendly", cls="pill ok"),
                            cls="status-pill"
                        ),
                        cls="footer"
                    ),
                    cls="appshell"
                )
            )
            )
        )
    )


def _codex_agent_manifest() -> Dict[str, Any]:
    version = _codex_agent_version()
    start_url = f"{CODEX_AGENT_START_URL}?v={version}"
    icon_url = _asset(CODEX_AGENT_ICON_PATH)
    return {
        "id": start_url,
        "name": "CodexAS-Extension",
        "short_name": "CodexAS",
        "start_url": start_url,
        "scope": CODEX_AGENT_SCOPE,
        "display": "standalone",
        "background_color": CODEX_AGENT_THEME_COLOR,
        "theme_color": CODEX_AGENT_THEME_COLOR,
        "icons": [
            {
                "src": icon_url,
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }


def _codex_agent_version() -> str:
    paths = [
        Path(__file__).resolve(),
        Path(__file__).resolve().parent / "static" / "codex_agent.css",
        Path(__file__).resolve().parent / "static" / "codex_agent.js",
        Path(__file__).resolve().parent / CODEX_AGENT_ICON_PATH.lstrip("/"),
    ]
    parts: List[str] = []
    for path in paths:
        try:
            parts.append(str(int(path.stat().st_mtime)))
        except Exception:
            continue
    raw = "|".join(parts)
    if not raw:
        return "v0"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _codex_agent_sw() -> str:
    version = _codex_agent_version()
    start_url = f"{CODEX_AGENT_START_URL}?v={version}"
    css_url = _asset("/static/codex_agent.css")
    js_url = _asset("/static/codex_agent.js")
    icon_url = _asset(CODEX_AGENT_ICON_PATH)
    return f"""const CACHE_NAME = 'codexas-extension-{version}';
const PRECACHE_URLS = [
  '{start_url}',
  '{css_url}',
  '{js_url}',
  '{icon_url}',
];

self.addEventListener('install', (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
}});

self.addEventListener('fetch', (event) => {{
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/codex-agent/sw.js' || url.pathname === '/codex-agent/manifest.json') {{
    event.respondWith(fetch(event.request));
    return;
  }}
  if (url.pathname.startsWith('/codex-agent')) {{
    event.respondWith(
      fetch(event.request)
        .then((response) => {{
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          return response;
        }})
        .catch(() => caches.match(event.request))
    );
    return;
  }}

  const key = url.pathname + url.search;
  if (PRECACHE_URLS.includes(key)) {{
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }}

  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
}});
"""


@app.get("/codex-agent")
@app.get("/codex-agent/")
async def codex_agent_ui() -> FastHTMLResponse:
    js_pill = Div(Span("JS"), Span("pending", id="js-status", cls="pill warn"), cls="status-pill footer-cell") if DEBUG_MODE else None
    version = _codex_agent_version()
    return FastHTMLResponse(
        to_xml(
            Html(
            Head(
                Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
                Link(rel="manifest", href=f"/codex-agent/manifest.json?v={version}"),
                Meta(name="theme-color", content=CODEX_AGENT_THEME_COLOR),
                Link(rel="icon", type="image/svg+xml", href=_asset(CODEX_AGENT_ICON_PATH)),
                Link(rel="stylesheet", href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap"),
                Link(rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css"),
                Link(rel="stylesheet", href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css"),
                Link(rel="stylesheet", href=_asset("/static/vendor/tribute.css")),
                Link(rel="stylesheet", href=_asset("/static/codex_agent.css")),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/python.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/bash.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/typescript.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/rust.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/go.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/yaml.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/sql.min.js"),
                Script(src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/dockerfile.min.js"),
                Script(src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"),
                Script(src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"),
                Script(src="https://unpkg.com/htmx.org@1.9.12", defer=True),
                Script(src=_asset("/static/vendor/socket.io/socket.io.min.js")),
                Script(src=_asset("/static/vendor/tribute.min.js")),
                Script(src="https://cdn.jsdelivr.net/npm/streaming-markdown/smd.min.js", type="module"),
                Script("window.addEventListener('load', () => console.log('socket.io', typeof io));", defer=True),
                Script(src=_asset("/static/modals/settings_modal.js"), defer=True),
                Script(src=_asset("/static/modals/cwd_picker.js"), defer=True),
                Script(src=_asset("/static/modals/rollout_picker.js"), defer=True),
                Script(src=_asset("/static/modals/warning_modal.js"), defer=True),
                Script(src=_asset("/static/ui/conversation_drawer.js"), defer=True),
                Script(src=_asset("/static/codex_agent.js"), type="module"),
            ),
            Body(
                Div(
	                    Header(
	                        Div(
	                            H1("CodexAS-Extension"),
	                            Small("App-Server JSON-RPC • Unified Timeline"),
	                            cls="brand"
	                        ),
	                        Div(
	                            Div(
	                                Span("Status"),
	                                Span("idle", id="agent-status", cls="pill warn"),
	                                cls="status-pill"
	                            ),
	                            Button("Start", id="agent-start", cls="btn"),
	                            Button("Stop", id="agent-stop", cls="btn ghost"),
	                            Button("×", id="host-close-top", cls="btn ghost host-close-btn"),
	                            cls="toolbar"
	                        ),
	                        cls="topbar"
	                    ),
	                    Main(
	                        # Threads panel intentionally removed for now.
	                        # NOTE: No native browser modals/dialogs/dropdowns allowed.
	                        # All future controls must be DOM-rendered.
	                        Section(
	                            H2("Conversations"),
	                            Div(
	                                Button("Project", id="splash-tab-project", cls="btn tiny toggle"),
	                                Button("All", id="splash-tab-all", cls="btn tiny toggle active"),
	                                cls="splash-tabs",
	                                id="splash-tabs",
	                            ),
	                            Div(
	                                P("Pick or create a conversation", cls="muted"),
	                                Div(id="conversation-list", cls="conversation-list"),
	                                cls="splash-body"
	                            ),
                            Footer(
                                Button("New Conversation", id="conversation-create", cls="btn primary"),
                                cls="splash-footer"
                            ),
                            cls="splash-view",
                            id="splash-view"
                        ),
                        Section(
                            Div(
                                Div(
                                    H2("Conversation"),
                                    Div("—", id="conversation-label", cls="conversation-label"),
                                    cls="brand"
                                ),
	                                Div(
	                                    Label(
	                                        Input(type="checkbox", id="markdown-toggle", checked=True),
	                                        Span("MD"),
	                                        cls="toggle-label"
	                                    ),
	                                    Span("disconnected", id="agent-ws", cls="pill warn"),
	                                    Button("Settings", id="conversation-settings", cls="btn"),
	                                    Button("Back", id="conversation-back", cls="btn ghost"),
	                                    Button("×", id="host-close-drawer", cls="btn ghost host-close-btn"),
	                                    cls="drawer-actions"
	                                ),
	                                cls="drawer-header"
	                            ),
                            Div(
                                Div(
                                    Div("Waiting for events...", id="timeline-placeholder", cls="timeline-row muted"),
                                    id="agent-timeline",
                                    cls="timeline"
                                ),
                                cls="timeline-wrap"
                            ),
                            Div(
                                Span(cls="status-spinner"),
                                Span("idle", id="status-label", cls="status-text"),
                                Span("", id="status-reasoning", cls="status-reasoning"),
                                Span(cls="status-dot", id="status-dot"),
                                cls="status-ribbon",
                                id="status-ribbon"
                            ),
                            Div(
                                Div(
                                    id="agent-prompt",
                                    contenteditable="true",
                                    cls="prompt-input",
                                    **{"data-placeholder": "@ to mention files"},
                                ),
                                Div(id="composer-terminal", cls="composer-terminal"),
                                Button("Send", id="agent-send", cls="btn primary"),
                                cls="composer"
                            ),
                            Footer(
                                Div(
                                    Span("Approval"),
                                    Div(
                                        Span("default", id="footer-approval-value", cls="pill"),
                                        Div(id="footer-approval-options", cls="dropdown-list"),
                                        cls="footer-dropdown"
                                    ),
                                    cls="status-pill footer-cell"
                                ),
                                Div(
                                    Span("context:"),
                                    Span("—", id="context-remaining", cls="pill"),
                                    cls="status-pill footer-cell"
                                ),
                                Div(
                                    Span(">_", id="footer-terminal-toggle", cls="pill"),
                                    cls="status-pill footer-cell"
                                ),
                                Div(cls="footer-cell footer-empty"),
                                Div(
                                    Span("Scroll"),
                                    Button("Pinned", id="scroll-pin", cls="btn tiny toggle active"),
                                    cls="status-pill footer-cell"
                                ),
                                Div(
                                    Span("mention", id="mention-pill", cls="pill"),
                                    cls="status-pill footer-cell"
                                ),
                                Div(
                                    Span("Tokens"),
                                    Span("0", id="counter-tokens", cls="pill"),
                                    cls="status-pill footer-cell"
                                ),
                                Div(
                                    Button("Interrupt", id="turn-interrupt", cls="btn danger"),
                                    cls="status-pill footer-cell footer-end"
                                ),
                                cls="footer"
                            ),
                            cls="conversation-drawer",
                            id="conversation-drawer"
                        ),
                        cls="grid"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Conversation Settings"),
                                Button("×", id="settings-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                Label(
                                    Span("CWD"),
                                    Div(
                                        Input(type="text", id="settings-cwd", placeholder="~/project"),
                                        Button("Browse", id="settings-cwd-browse", cls="btn ghost"),
                                        cls="settings-row"
                                    ),
                                ),
                                Label(
                                    Span("Approval Policy"),
                                    Div(
                                        Input(type="text", id="settings-approval", placeholder="on-failure"),
                                        Button("▾", id="settings-approval-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-approval-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                ),
                                Label(
                                    Span("Sandbox Policy"),
                                    Div(
                                        Input(type="text", id="settings-sandbox", placeholder="workspaceWrite"),
                                        Button("▾", id="settings-sandbox-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-sandbox-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                ),
                                Label(
                                    Span("Model"),
                                    Div(
                                        Input(type="text", id="settings-model", placeholder="gpt-5.1-codex"),
                                        Button("▾", id="settings-model-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-model-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                ),
                                Label(
                                    Span("Effort"),
                                    Div(
                                        Input(type="text", id="settings-effort", placeholder="medium"),
                                        Button("▾", id="settings-effort-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-effort-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                ),
                                Label(
                                    Span("Summary"),
                                    Div(
                                        Input(type="text", id="settings-summary", placeholder="concise"),
                                        Button("▾", id="settings-summary-toggle", cls="btn ghost dropdown-toggle"),
                                        Div(id="settings-summary-options", cls="dropdown-list"),
                                        cls="dropdown-field"
                                    ),
                                ),
                                Label(
                                    Span("Conversation Label"),
                                    Input(type="text", id="settings-label", placeholder="label"),
                                ),
                                Label(
                                    Span("Command Output Lines"),
                                    Input(type="number", id="settings-command-lines", placeholder="20", value="20", min="1", max="500"),
                                ),
                                Label(
                                    Span("Rollout"),
                                    Div(
                                        Input(type="text", id="settings-rollout", placeholder="(unselected)", readonly=True),
                                        Button("Pick", id="settings-rollout-browse", cls="btn ghost"),
                                        cls="settings-row"
                                    ),
                                    id="settings-rollout-row",
                                ),
                                Label(
                                    Span("Render Markdown"),
                                    Input(type="checkbox", id="settings-markdown", checked=True),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Use xterm.js (terminal)"),
                                    Input(type="checkbox", id="settings-xterm", checked=True),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Syntax highlighting (diffs & terminal)"),
                                    Input(type="checkbox", id="settings-diff-syntax", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                Label(
                                    Span("Semantic shell ribbon (Tree-sitter)"),
                                    Input(type="checkbox", id="settings-semantic-shell-ribbon", checked=False),
                                    cls="settings-checkbox-row"
                                ),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Cancel", id="settings-cancel", cls="btn ghost"),
                                Button("Save", id="settings-save", cls="btn primary"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="settings-modal"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Pick CWD", id="picker-title"),
                                Button("×", id="picker-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="picker-path", cls="picker-path"),
                                Div(id="picker-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            Div(
                                Div(
                                    Input(type="text", id="picker-filter", placeholder="filter (regex)..."),
                                    cls="picker-footer-left"
                                ),
                                Div(
                                    Button("Up", id="picker-up", cls="btn ghost"),
                                    Button("Select Current", id="picker-select", cls="btn primary"),
                                    cls="picker-footer-right"
                                ),
                                cls="picker-footer"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="cwd-picker"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Pick Rollout"),
                                Button("×", id="rollout-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="rollout-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="rollout-picker"
                    ),
                    Div(
                        Div(
                            Div(
                                H3("Confirm"),
                                Button("×", id="warning-close", cls="btn ghost"),
                                cls="settings-header"
                            ),
                            Div(
                                P("Are you sure?", id="warning-body"),
                                cls="settings-body"
                            ),
                            Div(
                                Button("Cancel", id="warning-cancel", cls="btn ghost"),
                                Button("Continue", id="warning-confirm", cls="btn danger"),
                                cls="settings-footer"
                            ),
                            cls="settings-dialog"
                        ),
                        cls="settings-overlay hidden",
                        id="warning-modal"
                    ),
                    cls="appshell"
                )
            )
            )
        )
    )


@app.get("/codex-agent/manifest.json")
async def codex_agent_manifest() -> Response:
    return Response(
        content=json.dumps(_codex_agent_manifest(), ensure_ascii=False),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/codex-agent/sw.js")
async def codex_agent_sw() -> Response:
    return Response(
        content=_codex_agent_sw(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/health")
async def api_health():
    return {"ok": True, "ts": utc_ts()}


@app.get("/api/appserver/config")
async def api_appserver_config():
    async with _config_lock:
        cfg = _load_appserver_config()
        _sync_conversation_index(cfg)
        _save_appserver_config(cfg)
        return cfg


@app.get("/api/appserver/conversation")
async def api_appserver_conversation():
    async with _config_lock:
        cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    meta = None
    if convo_id and _conversation_meta_path(convo_id).exists():
        meta = _load_conversation_meta(convo_id)
    if not meta:
        meta = {
            "conversation_id": convo_id,
            "thread_id": None,
            "settings": {},
            "status": "none",
        }
    meta["active_view"] = cfg.get("active_view", "splash")
    return meta


@app.post("/api/appserver/conversation")
async def api_appserver_conversation_update(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    convo_id = await _ensure_conversation()
    meta = _load_conversation_meta(convo_id)
    settings = payload.get("settings")
    if isinstance(settings, dict):
        meta_settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
        for key, value in settings.items():
            if value is None or value == "":
                if key in meta_settings:
                    meta_settings.pop(key, None)
            else:
                meta_settings[key] = value
        meta["settings"] = meta_settings
    thread_id = payload.get("thread_id")
    if thread_id and not meta.get("thread_id"):
        meta["thread_id"] = thread_id
        meta["status"] = "active"
    _save_conversation_meta(convo_id, meta)
    async with _config_lock:
        cfg = _load_appserver_config()
        _add_conversation_to_config(convo_id, cfg)
        cfg["conversation_id"] = convo_id
        cfg["active_view"] = cfg.get("active_view") or "conversation"
        _save_appserver_config(cfg)
    return meta


# Cache for draft hash to avoid unnecessary writes
_draft_hash_cache: Dict[str, str] = {}


@app.post("/api/appserver/conversation/draft")
async def api_appserver_conversation_draft(payload: Dict[str, Any] = Body(...)):
    """
    Save composer draft to conversation meta. Uses SHA-256 hash collision detection
    to avoid unnecessary disk writes when the draft hasn't changed.

    Accepts explicit conversation_id to avoid race conditions when switching conversations.
    """
    draft = payload.get("draft", "")
    if not isinstance(draft, str):
        draft = ""

    # Use explicit conversation_id if provided, otherwise fall back to active
    convo_id = payload.get("conversation_id")
    if not convo_id or not isinstance(convo_id, str):
        convo_id = await _ensure_conversation()

    # Validate the conversation exists
    if not _conversation_meta_path(convo_id).exists():
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Hash the draft content to detect changes
    draft_hash = hashlib.sha256(draft.encode("utf-8")).hexdigest()

    # Check if the draft has changed since last save
    cached_hash = _draft_hash_cache.get(convo_id)
    if cached_hash == draft_hash:
        # No change, skip write
        return {"status": "unchanged", "conversation_id": convo_id}

    # Update cache and save to meta
    _draft_hash_cache[convo_id] = draft_hash
    meta = _load_conversation_meta(convo_id)
    meta["draft"] = draft
    _save_conversation_meta(convo_id, meta)

    return {"status": "saved", "conversation_id": convo_id}


@app.get("/api/appserver/conversations")
async def api_appserver_conversations():
    async with _config_lock:
        cfg = _load_appserver_config()
        ids = _sync_conversation_index(cfg)
        _save_appserver_config(cfg)
    if not ids and _latest_legacy_transcript():
        await _ensure_conversation()
        async with _config_lock:
            cfg = _load_appserver_config()
            ids = _sync_conversation_index(cfg)
            _save_appserver_config(cfg)
    items: List[Dict[str, Any]] = []
    for convo_id in ids:
        if not convo_id:
            continue
        if _conversation_meta_path(convo_id).exists():
            meta = _load_conversation_meta(convo_id)
        else:
            meta = {"conversation_id": convo_id, "thread_id": None, "settings": {}, "status": "none"}
        items.append(meta)
    return {"items": items, "active_conversation_id": cfg.get("conversation_id"), "active_view": cfg.get("active_view", "splash")}


@app.post("/api/appserver/conversations")
async def api_appserver_conversation_create(payload: Dict[str, Any] = Body(None)):
    convo_id = uuid.uuid4().hex
    meta = _default_conversation_meta(convo_id)
    if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
        meta["settings"] = payload["settings"]
    _save_conversation_meta(convo_id, meta)
    async with _config_lock:
        cfg = _load_appserver_config()
        _add_conversation_to_config(convo_id, cfg)
        cfg["conversation_id"] = convo_id
        cfg["active_view"] = "conversation"
        cfg["thread_id"] = meta.get("thread_id")
        _save_appserver_config(cfg)
    return meta


@app.post("/api/appserver/conversations/select")
async def api_appserver_conversation_select(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    convo_id = payload.get("conversation_id") or payload.get("id")
    if not isinstance(convo_id, str) or not convo_id.strip():
        raise HTTPException(status_code=400, detail="Missing conversation_id")
    convo_id = convo_id.strip()
    if not _conversation_meta_path(convo_id).exists():
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Cleanup: delete previous draft conversation if switching away from it
    async with _config_lock:
        cfg = _load_appserver_config()
        prev_convo_id = cfg.get("conversation_id")
    if prev_convo_id and prev_convo_id != convo_id and _conversation_meta_path(prev_convo_id).exists():
        prev_meta = _load_conversation_meta(prev_convo_id)
        if prev_meta.get("status") == "draft" and not prev_meta.get("thread_id"):
            # Previous conversation was a draft with no thread - delete it using same logic as DELETE endpoint
            prev_path = _conversation_dir(prev_convo_id)
            if prev_path.exists():
                for child in prev_path.glob("**/*"):
                    if child.is_file():
                        try:
                            child.unlink()
                        except Exception:
                            pass
                try:
                    for child in sorted(prev_path.glob("**/*"), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    prev_path.rmdir()
                except Exception:
                    pass
                async with _config_lock:
                    cfg = _load_appserver_config()
                    _remove_conversation_from_config(prev_convo_id, cfg)
                    _save_appserver_config(cfg)
    
    meta = _load_conversation_meta(convo_id)
    async with _config_lock:
        cfg = _load_appserver_config()
        _add_conversation_to_config(convo_id, cfg)
        cfg["conversation_id"] = convo_id
        view = payload.get("view")
        if view in {"splash", "conversation"}:
            cfg["active_view"] = view
        else:
            cfg["active_view"] = "conversation"
        cfg["thread_id"] = meta.get("thread_id")
        _save_appserver_config(cfg)
    return meta


@app.post("/api/appserver/conversations/bind-rollout")
async def api_appserver_conversation_bind_rollout(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    rollout_id = payload.get("rollout_id")
    if not isinstance(rollout_id, str) or not rollout_id.strip():
        raise HTTPException(status_code=400, detail="Missing rollout_id")
    items = payload.get("items")
    convo_id = await _ensure_conversation()
    meta = _load_conversation_meta(convo_id)
    meta["thread_id"] = rollout_id
    meta["status"] = "active"
    meta_settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
    meta_settings["rolloutId"] = rollout_id
    meta["settings"] = meta_settings
    _save_conversation_meta(convo_id, meta)
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["thread_id"] = rollout_id
        _save_appserver_config(cfg)
    if isinstance(items, list):
        await _write_transcript_entries(convo_id, items)
    else:
        path = _find_rollout_path(_sanitize_conversation_id(rollout_id))
        if not path:
            raise HTTPException(status_code=404, detail="Rollout not found")
        preview = _rollout_preview_entries(path, limit=200000)
        await _write_transcript_entries(convo_id, preview.get("items", []))
    return {"ok": True, "conversation_id": convo_id, "thread_id": rollout_id}


@app.delete("/api/appserver/conversations/{conversation_id}")
async def api_appserver_conversation_delete(conversation_id: str):
    if not conversation_id:
        raise HTTPException(status_code=400, detail="Missing conversation_id")
    convo_id = _sanitize_conversation_id(conversation_id)
    path = _conversation_dir(convo_id)
    # Remove sidecar directory if it exists
    if path.exists():
        for child in path.glob("**/*"):
            if child.is_file():
                try:
                    child.unlink()
                except Exception:
                    pass
        try:
            for child in sorted(path.glob("**/*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            path.rmdir()
        except Exception:
            pass
    # Always clean up config regardless of directory state
    async with _config_lock:
        cfg = _load_appserver_config()
        _remove_conversation_from_config(convo_id, cfg)
        if cfg.get("conversation_id") == convo_id:
            cfg["conversation_id"] = None
            cfg["thread_id"] = None
            cfg["active_view"] = "splash"
        _save_appserver_config(cfg)
    return {"ok": True}


@app.post("/api/appserver/view")
async def api_appserver_set_view(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    view = payload.get("view")
    if view not in {"splash", "conversation"}:
        raise HTTPException(status_code=400, detail="view must be 'splash' or 'conversation'")
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["active_view"] = view
        _save_appserver_config(cfg)
        return cfg


def _detect_repo_root(start: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = result.stdout.strip()
        if root:
            return Path(root)
    except Exception:
        pass
    return start


def _rg_list_files(root: Path) -> List[str]:
    result = subprocess.run(
        ["rg", "--files", "--glob", "!.git/*"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


@app.get("/api/fs/list")
async def api_fs_list(path: Optional[str] = Query(None)):
    target = path or "~"
    try:
        resolved = Path(os.path.expanduser(target)).resolve()
    except Exception:
        resolved = Path(os.path.expanduser("~")).resolve()
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    items: List[Dict[str, Any]] = []
    try:
        with os.scandir(resolved) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    is_link = entry.is_symlink()
                except Exception:
                    is_dir = False
                    is_file = False
                    is_link = False
                if is_dir:
                    entry_type = "directory"
                elif is_file:
                    entry_type = "file"
                elif is_link:
                    entry_type = "symlink"
                else:
                    entry_type = "other"
                items.append({
                    "name": entry.name,
                    "path": str(Path(resolved) / entry.name),
                    "type": entry_type,
                })
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to list directory")

    items.sort(key=lambda item: (0 if item["type"] == "directory" else 1, item["name"].lower()))
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return {"path": str(resolved), "parent": parent, "items": items}


@app.get("/api/fs/search")
async def api_fs_search(query: str = Query(...), root: Optional[str] = Query(None), limit: int = Query(200, gt=0)):
    if not query.strip():
        return {"root": None, "items": []}
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        raise HTTPException(status_code=400, detail="Invalid regex")
    async with _config_lock:
        cfg = _load_appserver_config()
    base = root or cfg.get("cwd") or os.getcwd()
    try:
        resolved = Path(os.path.expanduser(base)).resolve()
    except Exception:
        resolved = Path(os.getcwd()).resolve()
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Root not found")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Root is not a directory")
    repo_root = _detect_repo_root(resolved)
    try:
        files = _rg_list_files(repo_root)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to search repo")
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rel in files:
        full = str((repo_root / rel).resolve())
        if pattern.search(rel) or pattern.search(full):
            if full not in seen:
                seen.add(full)
                items.append({"name": Path(rel).name, "path": full, "type": "file"})
                if len(items) >= limit:
                    break
        parts = Path(rel).parents
        for parent in parts:
            if parent == Path("."):
                continue
            parent_rel = str(parent)
            parent_full = str((repo_root / parent_rel).resolve())
            if parent_full in seen:
                continue
            if pattern.search(parent_rel) or pattern.search(parent_full):
                seen.add(parent_full)
                items.append({"name": Path(parent_rel).name, "path": parent_full, "type": "directory"})
                if len(items) >= limit:
                    break
        if len(items) >= limit:
            break
    items.sort(key=lambda item: (0 if item["type"] == "directory" else 1, item["name"].lower()))
    return {"root": str(repo_root), "items": items}


@app.get("/api/appserver/transcript")
async def api_appserver_transcript(conversation_id: Optional[str] = Query(None)):
    async with _config_lock:
        cfg = _load_appserver_config()
        convo_id = conversation_id or cfg.get("conversation_id")
    if not convo_id:
        return {"conversation_id": None, "items": []}
    path = _transcript_path(str(convo_id))
    if not path.exists():
        return {"conversation_id": str(convo_id), "items": []}
    items: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                items.append(_sanitize_transcript_item(record))
    except Exception:
        return {"conversation_id": str(convo_id), "items": []}
    return {"conversation_id": str(convo_id), "items": items}


@app.get("/api/appserver/transcript/range")
async def api_appserver_transcript_range(
    conversation_id: Optional[str] = Query(None),
    offset: int = Query(0),
    limit: int = Query(120, gt=0, le=500),
):
    async with _config_lock:
        cfg = _load_appserver_config()
        convo_id = conversation_id or cfg.get("conversation_id")
    if not convo_id:
        return {"conversation_id": None, "total": 0, "offset": 0, "items": []}
    path = _transcript_path(str(convo_id))
    if not path.exists():
        return {"conversation_id": str(convo_id), "total": 0, "offset": 0, "items": []}
    total = 0
    items: List[Dict[str, Any]] = []
    if offset < 0:
        from collections import deque
        buf: deque = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                buf.append(_sanitize_transcript_item(record))
        items = list(buf)
        offset = max(0, total - len(items))
    else:
        start = max(0, offset)
        end = start + limit
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if total >= start and total < end:
                    items.append(_sanitize_transcript_item(record))
                total += 1
                if total >= end and total >= start and len(items) >= limit:
                    # still count total by continuing
                    continue
        offset = start
    return {"conversation_id": str(convo_id), "total": total, "offset": offset, "items": items}


@app.get("/api/appserver/rollouts")
async def api_appserver_rollouts():
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    await _ensure_appserver_initialized()
    try:
        response = await _rpc_request("thread/list", params={"limit": 200}, timeout=15.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="thread/list timed out")
    # _rpc_request already extracts the "result" key, so response is the result directly
    items_raw = []
    if isinstance(response, dict):
        items_raw = response.get("data") or []
    items: List[Dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "")
        preview = str(item.get("preview") or "")
        cwd = item.get("cwd")
        items.append({
            "id": rid,
            "short_id": rid[-8:] if len(rid) > 8 else rid,
            "preview": preview,
            "cwd": cwd,
        })
    return {"items": items}


@app.get("/api/appserver/rollouts/{rollout_id}/preview")
async def api_appserver_rollout_preview(rollout_id: str):
    safe = _sanitize_conversation_id(rollout_id)
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid rollout id")
    path = _find_rollout_path(safe)
    if not path:
        raise HTTPException(status_code=404, detail="Rollout not found")
    preview = _rollout_preview_entries(path)
    return {"items": preview.get("items", []), "token_total": preview.get("token_total")}


@app.post("/api/appserver/config")
async def api_appserver_config_update(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Config payload must be a JSON object")
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg.update(payload)
        _save_appserver_config(cfg)
        return cfg


@app.post("/api/appserver/cwd")
async def api_appserver_set_cwd(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HTTPException(status_code=400, detail="Missing or invalid 'cwd'")
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["cwd"] = cwd
        _save_appserver_config(cfg)
    convo_id = await _ensure_conversation()
    meta = _load_conversation_meta(convo_id)
    settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
    settings["cwd"] = cwd
    meta["settings"] = settings
    _save_conversation_meta(convo_id, meta)
    return cfg


@app.post("/api/appserver/thread/start")
async def api_appserver_thread_start(payload: Dict[str, Any] = Body(None)):
    async with _config_lock:
        cfg = _load_appserver_config()
        thread_id = None
        if isinstance(payload, dict):
            thread_id = payload.get("thread_id") or payload.get("id")
        if thread_id:
            cfg["thread_id"] = thread_id
            _save_appserver_config(cfg)
        return {"ok": True, "thread_id": cfg.get("thread_id"), "note": "stub"}


@app.post("/api/appserver/thread/kill")
async def api_appserver_thread_kill():
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["thread_id"] = None
        cfg["turn_id"] = None
        _save_appserver_config(cfg)
        return {"ok": True}


@app.post("/api/appserver/stop")
async def api_appserver_stop():
    await _stop_appserver_shell()
    return {"ok": True}


@app.post("/api/appserver/start")
async def api_appserver_start():
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    # Persist current app-server shell id into the SSOT conversation meta so
    # a fresh frontend session can decide whether a `thread/resume` is needed.
    try:
        async with _config_lock:
            cfg = _load_appserver_config()
        convo_id = cfg.get("conversation_id")
        if isinstance(convo_id, str) and convo_id and _conversation_meta_path(convo_id).exists():
            meta = _load_conversation_meta(convo_id)
            settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
            if settings.get("appserver_shell_id") != info["shell_id"]:
                settings["appserver_shell_id"] = info["shell_id"]
                meta["settings"] = settings
                _save_conversation_meta(convo_id, meta)
    except Exception:
        pass
    return {"ok": True, **info}


@app.get("/api/appserver/status")
async def api_appserver_status():
    _ensure_framework_shells_secret()
    cfg = _load_appserver_config()
    shell_id = cfg.get("shell_id")
    if not shell_id:
        return {"running": False}
    mgr = await _get_fws_manager()
    shell = await mgr.get_shell(shell_id)
    if shell and shell.status == "running":
        # Best-effort: keep SSOT in sync with the live app-server shell id so a
        # brand new frontend session can decide whether it must `thread/resume`.
        try:
            convo_id = cfg.get("conversation_id")
            if isinstance(convo_id, str) and convo_id and _conversation_meta_path(convo_id).exists():
                meta = _load_conversation_meta(convo_id)
                settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
                if settings.get("appserver_shell_id") != shell_id:
                    settings["appserver_shell_id"] = shell_id
                    meta["settings"] = settings
                    _save_conversation_meta(convo_id, meta)
        except Exception:
            pass
        return {"running": True, "shell_id": shell_id, "pid": shell.pid}
    return {"running": False, "shell_id": shell_id}


@app.post("/api/mcp/agent-pty/start")
async def api_mcp_agent_pty_start():
    info = await _get_or_start_mcp_shell()
    return {"ok": True, **info}


@app.post("/api/mcp/agent-pty/stop")
async def api_mcp_agent_pty_stop():
    await _stop_mcp_shell()
    return {"ok": True}


@app.get("/api/mcp/agent-pty/status")
async def api_mcp_agent_pty_status():
    _ensure_framework_shells_secret()
    cfg = _load_appserver_config()
    shell_id = cfg.get("mcp_shell_id")
    if not shell_id:
        return {"running": False}
    mgr = await _get_fws_manager()
    shell = await mgr.get_shell(shell_id)
    if shell and shell.status == "running":
        return {"running": True, "shell_id": shell_id, "pid": shell.pid}
    return {"running": False, "shell_id": shell_id}


@app.get("/api/mcp/agent-pty/default-size")
async def api_mcp_agent_pty_default_size() -> Dict[str, Any]:
    # Keep in sync with mcp_agent_pty_server ConversationState defaults.
    return {"ok": True, "cols": 120, "rows": 40}


def _agent_pty_resize_api_enabled() -> bool:
    # Enabled by default for xterm composer; set AGENT_PTY_RESIZE_API=0 to disable.
    v = os.environ.get("AGENT_PTY_RESIZE_API", "1").strip().lower()
    return v not in {"0", "false", "no", "off"}


@app.get("/api/mcp/agent-pty/size")
async def api_mcp_agent_pty_size(conversation_id: str = Query(...)) -> Dict[str, Any]:
    if not _agent_pty_resize_api_enabled():
        raise HTTPException(status_code=404, detail="Resize API disabled")
    try:
        import mcp_agent_pty_server as mcp_srv  # type: ignore
        return await mcp_srv.pty_get_size(conversation_id=conversation_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"mcp size failed: {exc}")


@app.post("/api/mcp/agent-pty/resize")
async def api_mcp_agent_pty_resize(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if not _agent_pty_resize_api_enabled():
        raise HTTPException(status_code=404, detail="Resize API disabled")
    convo_id = str(payload.get("conversation_id") or "").strip()
    if not convo_id:
        raise HTTPException(status_code=400, detail="conversation_id required")
    try:
        cols = int(payload.get("cols") or 0)
        rows = int(payload.get("rows") or 0)
    except Exception:
        raise HTTPException(status_code=400, detail="cols/rows must be ints")

    try:
        import mcp_agent_pty_server as mcp_srv  # type: ignore
        return await mcp_srv.pty_resize(conversation_id=convo_id, cols=cols, rows=rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"mcp resize failed: {exc}")


@app.post("/api/mcp/agent-pty/exec")
async def api_mcp_agent_pty_exec(payload: Dict[str, Any] = Body(...)):
    """Execute a command in the agent-owned per-conversation PTY via MCP subprocess (stdio)."""
    command = payload.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=400, detail="No command provided")
    async with _config_lock:
        cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    if not isinstance(convo_id, str) or not convo_id:
        raise HTTPException(status_code=409, detail="No active conversation")
    # Ensure MCP service is running.
    await _get_or_start_mcp_shell()
    # For now, we call the MCP server by importing and using its FastMCP tool impl in-process.
    # This keeps wiring simple; we can later route through stdio transport for strict process boundaries.
    try:
        import mcp_agent_pty_server as mcp_srv  # type: ignore
        result = await mcp_srv.pty_exec(conversation_id=convo_id, cmd=command)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"mcp exec failed: {exc}")
    return {"ok": True, **(result if isinstance(result, dict) else {"result": result})}


async def _emit_shell_events_from_agent_block(conversation_id: str, block: Dict[str, Any]) -> None:
    """Bridge agent PTY blocks into the existing shell_* frontend event stream."""
    global _agent_pty_exec_seq
    _agent_pty_exec_seq += 1
    call_id = f"agentpty_{_agent_pty_exec_seq}"
    cmd = block.get("cmd") or ""
    cwd = block.get("cwd")
    
    # Read stdout from output_path if available, otherwise use inline stdout
    stdout = ""
    output_path = block.get("output_path")
    if output_path:
        try:
            stdout = await asyncio.to_thread(Path(output_path).read_text, encoding="utf-8")
        except Exception:
            stdout = block.get("stdout") or ""
    else:
        stdout = block.get("stdout") or ""
    
    await _broadcast_appserver_ui({"type": "shell_begin", "id": call_id, "command": cmd, "cwd": cwd, "stream": "stdout"})
    exit_code = block.get("exit_code") if isinstance(block.get("exit_code"), int) else 0
    await _broadcast_appserver_ui({"type": "shell_end", "id": call_id, "exitCode": exit_code, "stdout": stdout, "stderr": ""})


@app.post("/api/appserver/rpc")
async def api_appserver_rpc(payload: Dict[str, Any] = Body(...)):
    print(f"[DEBUG] /api/appserver/rpc received: {payload}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    
    # Intercept thread/resume, thread/start, turn/start to inject settings from SSOT
    method = payload.get("method", "")
    convo_id: Optional[str] = None
    if method in ("thread/resume", "thread/start", "turn/start"):
        async with _config_lock:
            cfg = _load_appserver_config()
        convo_id = cfg.get("conversation_id")
        if convo_id:
            meta = _load_conversation_meta(convo_id)
            settings = meta.get("settings", {})
            params = payload.get("params", {})
            
            # Different params supported by different methods:
            # thread/resume: model, cwd, approvalPolicy, sandbox (NOT reasoningEffort)
            # thread/start: model, cwd, approvalPolicy, sandbox, reasoningEffort
            # turn/start: model, cwd, approvalPolicy, sandboxPolicy, effort, summary
            
            if method == "turn/start":
                # turn/start uses 'effort' not 'reasoningEffort'
                for key in ("model", "cwd", "approvalPolicy", "sandboxPolicy", "summary"):
                    if key in settings and settings[key] and key not in params:
                        params[key] = settings[key]
                # Map our 'effort' setting to turn/start's 'effort' param
                if "effort" in settings and settings["effort"] and "effort" not in params:
                    params["effort"] = settings["effort"]
            elif method == "thread/start":
                # thread/start uses 'reasoningEffort'
                for key in ("model", "cwd", "approvalPolicy", "sandbox"):
                    if key in settings and settings[key] and key not in params:
                        params[key] = settings[key]
                if "effort" in settings and settings["effort"] and "reasoningEffort" not in params:
                    params["reasoningEffort"] = settings["effort"]
            else:  # thread/resume
                # thread/resume does NOT support reasoningEffort - only model, cwd, approvalPolicy, sandbox
                for key in ("model", "cwd", "approvalPolicy", "sandbox"):
                    if key in settings and settings[key] and key not in params:
                        params[key] = settings[key]
            
            payload["params"] = params
            print(f"[DEBUG] SSOT injection for {method}: {params}")
    
    # Inject pending command context envelope on turn/start
    if method == "turn/start" and convo_id:
        meta = _load_conversation_meta(convo_id)
        buffer = meta.pop("pending_cmd_buffer", None)
        if buffer and buffer.get("commands"):
            _save_conversation_meta(convo_id, meta)  # Clear buffer
            
            # Build envelope
            envelope_json = _build_envelope_from_buffer(buffer)
            envelope = _META_ENVELOPE_START + envelope_json + _META_ENVELOPE_END
            command_count = len(buffer.get("commands", []))
            _record_last_injected_meta_envelope(convo_id, envelope_json, command_count=command_count)
            # Debug-only: optionally surface to UI so you can see what the model saw.
            if DEBUG_MODE:
                try:
                    await _broadcast_appserver_ui({
                        "type": "meta_envelope_injected",
                        "conversation_id": convo_id,
                        "command_count": command_count,
                        "envelope_json": envelope_json,
                    })
                except Exception:
                    pass
            
            # Prepend envelope to first text input item
            # Frontend sends: params.input = [{ type: 'text', text: '...' }]
            params = payload.get("params", {})
            input_items = params.get("input", [])
            if input_items and isinstance(input_items[0], dict):
                if input_items[0].get("type") == "text":
                    original_text = input_items[0].get("text", "")
                    input_items[0]["text"] = envelope + original_text
                    print(f"[DEBUG] Meta envelope injected: {command_count} commands")
            
            payload["params"] = params
    
    # Track turn/start requests for auto-resume on "conversation not found" error
    if method == "turn/start" and payload.get("id") is not None:
        _pending_turn_starts[str(payload["id"])] = payload.copy()
    
    await _write_appserver(payload)
    return {"ok": True}


@app.post("/api/appserver/approval_record")
async def api_appserver_approval_record(payload: Dict[str, Any] = Body(...)):
    """Record an approval decision to the transcript."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    status = payload.get("status")  # "accepted" or "declined"
    diff = payload.get("diff")
    path = payload.get("path")
    item_id = payload.get("item_id")
    if status not in ("accepted", "declined"):
        raise HTTPException(status_code=400, detail="Invalid status")
    cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    if convo_id:
        await _append_transcript_entry(convo_id, {
            "role": "approval",
            "status": status,
            "diff": diff,
            "path": path,
            "item_id": item_id,
            "event": "approval_decision",
        })
    # If declined, broadcast a declined diff event to the UI
    if status == "declined" and diff:
        await _broadcast_appserver_ui({
            "type": "diff_declined",
            "id": item_id,
            "text": diff,
            "path": path,
        })
    return {"ok": True}


@app.post("/api/appserver/interrupt")
async def api_appserver_interrupt():
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    await _ensure_appserver_initialized()
    cfg = _load_appserver_config()
    thread_id = cfg.get("thread_id")
    turn_id = cfg.get("turn_id")
    if not thread_id or not turn_id:
        raise HTTPException(status_code=409, detail="No active turn to interrupt")
    await _rpc_request("turn/interrupt", params={"threadId": thread_id, "turnId": turn_id})
    return {"ok": True, "thread_id": thread_id, "turn_id": turn_id}


@app.post("/api/appserver/shell/exec")
async def api_appserver_shell_exec(payload: Dict[str, Any] = Body(...)):
    """Execute a shell command via codex-app-server's command/exec RPC with streaming."""
    command = payload.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="No command provided")
    
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    await _ensure_appserver_initialized()
    cfg = _load_appserver_config()
    cwd = cfg.get("cwd")
    convo_id = cfg.get("conversation_id")
    
    # Generate tracking ID for streaming
    call_id = f"shell_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    
    # Emit shell_begin immediately so frontend can create streaming row
    await _broadcast_appserver_ui({
        "type": "shell_begin",
        "id": call_id,
        "command": command,
        "cwd": cwd,
    })
    
    # Split command into array for shell execution
    # Using shell=True style by wrapping in sh -c
    cmd_array = ["sh", "-c", command]
    
    params = {
        "command": cmd_array,
        "timeoutMs": 30000,  # 30 second timeout
        "cwd": cwd,
        "sandboxPolicy": None,  # Use server default
    }
    
    # Track this call_id for routing deltas
    _shell_call_ids[call_id] = {"command": command, "cwd": cwd, "convo_id": convo_id}
    
    try:
        result = await _rpc_request("command/exec", params=params, timeout=35.0)
        exit_code = result.get("exitCode", 1)
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        
        # Emit shell_end with full result
        await _broadcast_appserver_ui({
            "type": "shell_end",
            "id": call_id,
            "exitCode": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        })
        
        # Write to transcript
        if convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "shell",
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "event": "command/exec",
            })
        
        return {"exitCode": exit_code, "stdout": stdout, "stderr": stderr, "callId": call_id}
    except Exception as e:
        error_msg = str(e)
        # Emit shell_end with error
        await _broadcast_appserver_ui({
            "type": "shell_end",
            "id": call_id,
            "exitCode": 1,
            "stdout": "",
            "stderr": error_msg,
            "error": True,
        })
        if convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "shell",
                "command": command,
                "stdout": "",
                "stderr": error_msg,
                "exit_code": 1,
                "event": "command/exec",
                "error": True,
            })
        return {"exitCode": 1, "stdout": "", "stderr": error_msg, "error": error_msg, "callId": call_id}
    finally:
        _shell_call_ids.pop(call_id, None)


@app.post("/api/appserver/compact")
async def api_appserver_compact():
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    await _ensure_appserver_initialized()
    cfg = _load_appserver_config()
    thread_id = cfg.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=409, detail="No active thread to compact")
    await _rpc_request("thread/compact", params={"threadId": thread_id})
    return {"ok": True, "thread_id": thread_id}


@app.post("/api/appserver/mention")
@app.put("/api/appserver/mention")
async def api_appserver_mention(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=400, detail="Missing or invalid 'path'")
    if "`" in path:
        raise HTTPException(status_code=400, detail="Invalid 'path' (backticks not supported)")
    conversation_id = payload.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise HTTPException(status_code=400, detail="Invalid 'conversation_id'")

    async with _config_lock:
        cfg = _load_appserver_config()
        active_conversation_id = cfg.get("conversation_id")
        active_view = cfg.get("active_view", "splash")

    if not conversation_id:
        conversation_id = active_conversation_id

    if not conversation_id:
        raise HTTPException(status_code=409, detail="No active conversation selected")

    # Validate the conversation exists.
    if not _conversation_meta_path(conversation_id).exists():
        raise HTTPException(status_code=404, detail="Conversation not found")

    queued = True
    if active_view == "conversation" and active_conversation_id == conversation_id:
        await _broadcast_appserver_ui({"type": "mention_insert", "path": path, "conversation_id": conversation_id})
        queued = False
    else:
        # Drawer closed (or different conversation active): append into the draft buffer
        # so it appears in the composer when the conversation is opened.
        meta = _load_conversation_meta(conversation_id)
        draft = meta.get("draft")
        if not isinstance(draft, str):
            draft = ""
        token = f"`{path}`"
        if draft and not draft.endswith((" ", "\n", "\t")):
            draft = draft + " " + token
        else:
            draft = draft + token
        meta["draft"] = draft
        _save_conversation_meta(conversation_id, meta)
        _draft_hash_cache[conversation_id] = hashlib.sha256(draft.encode("utf-8")).hexdigest()

    # CORS-friendly response for editor/iframe host apps.
    return JSONResponse(
        {"ok": True, "queued": queued, "conversation_id": conversation_id, "path": path},
        headers={
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.options("/api/appserver/mention")
async def api_appserver_mention_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@app.get("/api/host/ui")
async def api_host_ui_get():
    async with _config_lock:
        cfg = _load_appserver_config()
        return {
            "ok": True,
            "host_ui": {
                "show_close": bool(_HOST_UI_STATE.get("show_close")),
                "parent_origin": _HOST_UI_STATE.get("parent_origin") if isinstance(_HOST_UI_STATE.get("parent_origin"), str) else None,
                "ide_mode": bool(_HOST_UI_STATE.get("ide_mode")),
                "project_root": _HOST_UI_STATE.get("project_root") if isinstance(_HOST_UI_STATE.get("project_root"), str) else None,
            },
            "active_view": cfg.get("active_view", "splash"),
            "conversation_id": cfg.get("conversation_id"),
        }


async def _set_host_ui_state(
    *,
    show_close: Optional[bool] = None,
    parent_origin: Optional[Optional[str]] = None,
    ide_mode: Optional[bool] = None,
    project_root: Optional[Optional[str]] = None,
) -> Dict[str, Any]:
    if show_close is not None:
        _HOST_UI_STATE["show_close"] = bool(show_close)
    if parent_origin is not None:
        _HOST_UI_STATE["parent_origin"] = parent_origin or None
    if ide_mode is not None:
        _HOST_UI_STATE["ide_mode"] = bool(ide_mode)
    if project_root is not None:
        _HOST_UI_STATE["project_root"] = project_root or None

    event = {
        "type": "host_ui",
        "show_close": bool(_HOST_UI_STATE.get("show_close")),
        "parent_origin": _HOST_UI_STATE.get("parent_origin") if isinstance(_HOST_UI_STATE.get("parent_origin"), str) else None,
        "ide_mode": bool(_HOST_UI_STATE.get("ide_mode")),
        "project_root": _HOST_UI_STATE.get("project_root") if isinstance(_HOST_UI_STATE.get("project_root"), str) else None,
    }
    await _broadcast_appserver_ui(event)
    return event


@app.post("/api/host/ui")
@app.put("/api/host/ui")
async def api_host_ui_set(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    show_close = payload.get("show_close")
    parent_origin = payload.get("parent_origin")
    ide_mode = payload.get("ide_mode")
    project_root = payload.get("project_root")
    if show_close is not None and not isinstance(show_close, bool):
        raise HTTPException(status_code=400, detail="Invalid 'show_close'")
    if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
        raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
    if ide_mode is not None and not isinstance(ide_mode, bool):
        raise HTTPException(status_code=400, detail="Invalid 'ide_mode'")
    if project_root is not None and project_root != "" and not isinstance(project_root, str):
        raise HTTPException(status_code=400, detail="Invalid 'project_root'")

    event = await _set_host_ui_state(
        show_close=bool(show_close) if show_close is not None else None,
        parent_origin=(parent_origin or None) if parent_origin is not None else None,
        ide_mode=bool(ide_mode) if ide_mode is not None else None,
        project_root=(project_root or None) if project_root is not None else None,
    )

    return JSONResponse(
        {"ok": True, **event},
        headers={
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/host/drawer/open")
@app.put("/api/host/drawer/open")
async def api_host_drawer_open(payload: Dict[str, Any] = Body(default_factory=dict)):
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    parent_origin = payload.get("parent_origin")
    if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
        raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
    project_root = payload.get("project_root")
    if project_root is not None and project_root != "" and not isinstance(project_root, str):
        raise HTTPException(status_code=400, detail="Invalid 'project_root'")
    event = await _set_host_ui_state(
        show_close=True,
        ide_mode=True,
        parent_origin=(parent_origin or None) if parent_origin is not None else None,
        project_root=(project_root or None) if project_root is not None else None,
    )
    return JSONResponse(
        {"ok": True, **event},
        headers={
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.options("/api/host/drawer/open")
async def api_host_drawer_open_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@app.post("/api/host/drawer/close")
@app.put("/api/host/drawer/close")
async def api_host_drawer_close(payload: Dict[str, Any] = Body(default_factory=dict)):
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    parent_origin = payload.get("parent_origin")
    if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
        raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
    event = await _set_host_ui_state(
        show_close=False,
        ide_mode=False,
        parent_origin=(parent_origin or None) if parent_origin is not None else None,
    )
    return JSONResponse(
        {"ok": True, **event},
        headers={
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.options("/api/host/drawer/close")
async def api_host_drawer_close_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@app.post("/api/host/project/cwd")
@app.put("/api/host/project/cwd")
async def api_host_project_cwd(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        # Also accept the same shape as the host fetch response:
        # { ok: true, data: { cwd: "<abs>" } } (or { data: { cwd: "<abs>" } })
        data = payload.get("data")
        if isinstance(data, dict):
            cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HTTPException(status_code=400, detail="Missing or invalid 'cwd'")
    event = await _set_host_ui_state(project_root=cwd)
    return JSONResponse(
        {"ok": True, **event},
        headers={
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.options("/api/host/project/cwd")
async def api_host_project_cwd_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


def _cors_headers_for_origin(origin: Optional[str]) -> Dict[str, str]:
    if not origin:
        return {"Access-Control-Allow-Origin": "*"}
    # Best-effort allowlist: TE2 host UI typically runs on :8089 and calls into this server on :12359.
    # If an Origin is provided, reflect it (browser will enforce it), and vary on Origin for caches.
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
    }


@app.get("/api/host/resolve_iframe")
async def api_host_resolve_iframe(request: Request):
    # Resolve the correct iframe URL for the caller (host app), based on request host/scheme.
    # This is intentionally simple: the iframe is always served from this server's /codex-agent.
    scheme = request.url.scheme or "http"
    host = request.headers.get("host") or request.url.netloc
    url = f"{scheme}://{host}/codex-agent"
    origin = request.headers.get("origin")
    return JSONResponse(
        {"ok": True, "url": url, "data": {"url": url}},
        headers=_cors_headers_for_origin(origin),
    )


@app.options("/api/host/resolve_iframe")
async def api_host_resolve_iframe_options(request: Request):
    origin = request.headers.get("origin")
    headers = {
        **_cors_headers_for_origin(origin),
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }
    return Response(status_code=204, headers=headers)


@app.options("/api/host/ui")
async def api_host_ui_options():
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@app.post("/api/appserver/initialize")
async def api_appserver_initialize():
    await _ensure_appserver_initialized()
    return {"ok": True}


async def _rpc_request(method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 6.0) -> Dict[str, Any]:
    req_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _appserver_rpc_waiters[req_id] = future
    payload = {"id": int(req_id), "method": method}
    if params is not None:
        payload["params"] = params
    await _write_appserver(payload)
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    finally:
        _appserver_rpc_waiters.pop(req_id, None)
    if isinstance(result, dict):
        if result.get("error"):
            raise HTTPException(status_code=500, detail=result.get("error"))
        # RPC responses have the actual data nested in "result" key
        return result.get("result", result)
    raise HTTPException(status_code=500, detail="Invalid RPC response")


@app.get("/api/appserver/models")
async def api_appserver_models():
    global _model_list_cache, _model_list_cache_time
    # Cache for 5 minutes
    if _model_list_cache is not None and (time.time() - _model_list_cache_time) < 300:
        return {"data": _model_list_cache}
    
    info = await _get_or_start_appserver_shell()
    await _ensure_appserver_reader(info["shell_id"])
    await _ensure_appserver_initialized()
    response = await _rpc_request("model/list", params={})
    
    # _rpc_request already extracts the "result" key, so response is the result directly
    models = response.get("data", []) if isinstance(response, dict) else []
    if isinstance(models, list):
        _model_list_cache = models
        _model_list_cache_time = time.time()
    
    return {"data": models}


@app.get("/api/appserver/debug/raw")
async def api_appserver_debug_raw(limit: int = Query(200, gt=0, le=500)):
    return {"items": _appserver_raw_buffer[-limit:]}


@app.get("/api/appserver/debug/state")
async def api_appserver_debug_state():
    async with _config_lock:
        cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    meta = _load_conversation_meta(convo_id) if convo_id and _conversation_meta_path(convo_id).exists() else None
    return {
        "config": cfg,
        "conversation": meta,
        "shell_id": _appserver_shell_id,
        "reader_task": _appserver_reader_task is not None and not _appserver_reader_task.done(),
        "debug_mode": DEBUG_MODE,
        "debug_raw_log_path": str(DEBUG_RAW_LOG_PATH) if DEBUG_RAW_LOG_PATH else None,
    }


@app.post("/api/appserver/debug/toggle")
async def api_appserver_debug_toggle(enabled: bool = Body(..., embed=True)):
    """Toggle debug mode and raw event logging."""
    global DEBUG_MODE, DEBUG_RAW_LOG_PATH
    DEBUG_MODE = enabled
    if enabled and not DEBUG_RAW_LOG_PATH:
        cache_dir = Path.home() / ".cache" / "agent_log_server"
        cache_dir.mkdir(parents=True, exist_ok=True)
        DEBUG_RAW_LOG_PATH = cache_dir / "debug_raw.jsonl"
        DEBUG_RAW_LOG_PATH.write_text("")
    return {
        "debug_mode": DEBUG_MODE,
        "debug_raw_log_path": str(DEBUG_RAW_LOG_PATH) if DEBUG_RAW_LOG_PATH else None,
    }


@app.get("/api/messages")
async def get_messages(limit: int = Query(None, gt=0)):
    return read_records(limit=limit)

@app.get("/api/messages/{msg_num}")
async def get_message_by_num(msg_num: int):
    """Get a specific message by its msg_num."""
    record = get_record_by_msg_num(msg_num)
    if record is None:
        return JSONResponse({"error": f"Message {msg_num} not found"}, status_code=404)
    return record

@app.delete("/api/messages/{msg_num}")
async def delete_message_by_num(msg_num: int):
    """Delete a specific message by its msg_num."""
    async with _lock:
        deleted = _delete_record_by_msg_num(msg_num)
    if not deleted:
        return JSONResponse({"error": f"Message {msg_num} not found"}, status_code=404)
    return {"ok": True, "deleted": msg_num}

@app.post("/api/messages", status_code=201)
async def post_message(msg: MessageIn):
    who = msg.who.strip()
    text = msg.message.strip()
    if not who or not text:
        return JSONResponse({"error": "Both 'who' and 'message' are required"}, status_code=400)

    record = {"ts": utc_ts(), "who": who, "message": text}
    await append_record(record)
    # record now has msg_num assigned by append_record
    return record


class AwaitIn(BaseModel):
    after_msg_num: int
    from_who: Optional[str] = None
    timeout_ms: int = 180000  # default 3 minutes


@app.post("/api/messages/await")
async def await_message(req: AwaitIn):
    """Long-poll for the next message after a given msg_num, optionally filtered by author."""
    after_num = req.after_msg_num
    from_who = req.from_who.strip() if req.from_who else None
    timeout_s = max(1, min(req.timeout_ms, 600000)) / 1000.0  # cap at 10 minutes
    
    poll_interval = 0.5  # seconds
    elapsed = 0.0
    
    while elapsed < timeout_s:
        # Read all records and find first one after after_num matching criteria
        records = read_records()
        for rec in records:
            rec_num = rec.get("msg_num")
            if rec_num is None or rec_num <= after_num:
                continue
            if from_who and rec.get("who") != from_who:
                continue
            # Found a matching message
            return rec
        
        # No match yet, wait and poll again
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    
    return JSONResponse({"error": "timeout", "after_msg_num": after_num}, status_code=408)


@app.post("/api/shutdown")
async def api_shutdown():
    try:
        await append_record({"ts": utc_ts(), "who": "server", "message": "shutdown requested"})
    except Exception:
        pass

    try:
        await _stop_appserver_shell()
    except Exception:
        pass
    
    loop = asyncio.get_event_loop()
    loop.call_later(0.1, os._exit, 0)
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/ws/appserver")
async def appserver_ws(websocket: WebSocket):
    await websocket.accept()
    mode = websocket.query_params.get("mode", "ui")
    if mode == "raw":
        _appserver_ws_clients_raw.append(websocket)
    else:
        _appserver_ws_clients_ui.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        with suppress(Exception):
            if websocket in _appserver_ws_clients_ui:
                _appserver_ws_clients_ui.remove(websocket)
            if websocket in _appserver_ws_clients_raw:
                _appserver_ws_clients_raw.remove(websocket)


@app.websocket("/ws/pty/{conversation_id}")
async def pty_raw_ws(websocket: WebSocket, conversation_id: str):
    """Raw bidirectional PTY WebSocket - streams PTY output and accepts stdin input."""
    await websocket.accept()
    
    # Import MCP server module for PTY access (shell lookup/ensure only).
    try:
        import mcp_agent_pty_server as mcp_srv
    except ImportError:
        await websocket.close(code=1011, reason="MCP server not available")
        return
    
    # Get or create conversation state and ensure shell is running
    state = mcp_srv._state(conversation_id)
    try:
        # Prefer the SSOT CWD for this conversation so the user terminal always
        # opens in the expected directory (matches meta.json settings).
        desired_cwd = None
        try:
            meta = _load_conversation_meta(conversation_id)
            settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
            desired_cwd = settings.get("cwd") if isinstance(settings, dict) else None
        except Exception:
            desired_cwd = None
        await state.ensure_shell(cwd=desired_cwd)
    except Exception as e:
        await websocket.close(code=1011, reason=f"Failed to start PTY: {e}")
        return

    shell_id = state.shell_id
    if not shell_id:
        await websocket.close(code=1011, reason="No PTY shell_id")
        return

    sanitize = False
    try:
        raw = websocket.query_params.get("sanitize")
        sanitize = str(raw).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        sanitize = False

    async def _ensure_hub(convo_id: str, shell_id: str) -> Dict[str, Any]:
        """Ensure a single, long-lived framework_shells subscription for convo_id."""
        async with _pty_hub_lock:
            hub = _pty_hubs.get(convo_id)
            if hub:
                # If the shell id changed (shell restarted), rotate the hub.
                if hub.get("shell_id") == shell_id and hub.get("task") and not hub["task"].done():
                    return hub
                # Stop old hub (best-effort) before replacing.
                try:
                    hub.get("stop").set()
                except Exception:
                    pass
                try:
                    t = hub.get("task")
                    if t:
                        t.cancel()
                except Exception:
                    pass
                try:
                    mgr0 = hub.get("mgr")
                    q0 = hub.get("out_q")
                    if mgr0 and q0:
                        await mgr0.unsubscribe_output(hub.get("shell_id"), q0)
                except Exception:
                    pass
                _pty_hubs.pop(convo_id, None)

            mgr = await _get_fws_manager()
            out_q = await mgr.subscribe_output(shell_id)
            stop = asyncio.Event()

            hub = {
                "conversation_id": convo_id,
                "shell_id": shell_id,
                "mgr": mgr,
                "out_q": out_q,
                "stop": stop,
                "clients": set(),  # websockets
                "client_queues": {},  # ws -> asyncio.Queue[str]
                "client_sender_tasks": {},  # ws -> task
                "client_sanitize": {},  # ws -> bool
                "last_empty_ts": None,
                "idle_task": None,
            }

            async def _hub_loop() -> None:
                while not stop.is_set():
                    chunk = await out_q.get()
                    if not isinstance(chunk, str):
                        try:
                            chunk = str(chunk)
                        except Exception:
                            continue
                    if not chunk:
                        continue

                    # Fan out to all clients via their per-ws queues (avoid blocking here).
                    for ws in list(hub["clients"]):
                        q = hub["client_queues"].get(ws)
                        if not q:
                            continue
                        out = chunk
                        if hub["client_sanitize"].get(ws):
                            try:
                                out = mcp_srv.ConversationState._sanitize_user_terminal_stream(out)
                            except Exception:
                                out = chunk
                        if not out:
                            continue
                        try:
                            q.put_nowait(out)
                        except asyncio.QueueFull:
                            # Drop bursts rather than blocking (mobile reconnects etc.)
                            pass

            hub["task"] = asyncio.create_task(_hub_loop(), name=f"pty-hub:{convo_id}")
            _pty_hubs[convo_id] = hub
            return hub

    hub = None
    try:
        hub = await _ensure_hub(conversation_id, shell_id)
    except Exception as e:
        await websocket.close(code=1011, reason=f"Failed to start PTY hub: {e}")
        return

    async def _ensure_user_capture(convo_id: str, shell_id: str, mgr) -> None:
        """Ensure background capture of raw bytes + marker slicing for user terminal commands."""
        async with _user_pty_capture_lock:
            cap = _user_pty_capture.get(convo_id)
            if not cap:
                raw_path = _user_pty_raw_path(convo_id)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                if not raw_path.exists():
                    raw_path.write_bytes(b"")
                try:
                    raw_cursor = int(raw_path.stat().st_size)
                except Exception:
                    raw_cursor = 0

                marker_offset = 0
                off_path = _user_pty_marker_offset_path(convo_id)
                if off_path.exists():
                    try:
                        marker_offset = int(off_path.read_text(encoding="utf-8").strip() or "0")
                    except Exception:
                        marker_offset = 0

                cap = {
                    "raw_path": raw_path,
                    "raw_cursor": raw_cursor,
                    "shell_id": shell_id,
                    "open_blocks": {},  # seq -> {begin_cursor, ts_begin, cmd, cwd}
                    "marker_offset": marker_offset,
                    "emitted_block_ends": set(),  # (seq, ts_end) to avoid duplicates
                    "bytes_task": None,
                    "marker_task": None,
                    "bytes_queue": None,
                }
                _user_pty_capture[convo_id] = cap
            else:
                cap["shell_id"] = shell_id

        # Start bytes capture task once.
        if not cap.get("bytes_task") or cap["bytes_task"].done():
            if not hasattr(mgr, "subscribe_output_bytes"):
                # No lossless bytes stream available; can't do deterministic slicing.
                return
            try:
                bytes_q = await mgr.subscribe_output_bytes(shell_id)
            except Exception:
                return
            cap["bytes_queue"] = bytes_q

            def _append_bytes(path: Path, data: bytes) -> None:
                with path.open("ab") as f:
                    f.write(data)

            async def _bytes_loop():
                while True:
                    chunk_b = await bytes_q.get()
                    if not chunk_b:
                        continue
                    if isinstance(chunk_b, str):
                        chunk = chunk_b.encode("utf-8", errors="replace")
                    else:
                        chunk = bytes(chunk_b)
                    try:
                        await asyncio.to_thread(_append_bytes, cap["raw_path"], chunk)  # type: ignore[arg-type]
                    except Exception:
                        # If we can't write, still advance cursor best-effort
                        pass
                    cap["raw_cursor"] = int(cap.get("raw_cursor") or 0) + len(chunk)

            cap["bytes_task"] = asyncio.create_task(_bytes_loop(), name=f"user-pty-bytes:{convo_id}")

        # Start marker tailer task once.
        if cap.get("marker_task") and not cap["marker_task"].done():
            return

        marker_path = _user_pty_marker_path(convo_id)

        async def _read_marker_tail(path: Path, offset: int) -> tuple[bytes, int, bool]:
            if not path.exists():
                return (b"", offset, False)
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except Exception:
                return (b"", offset, False)
            rewound = False
            if offset > len(data):
                offset = 0
                rewound = True
            return (data[offset:], offset, rewound)

        async def _marker_loop():
            buf = b""
            while True:
                tail, base_off, rewound = await _read_marker_tail(marker_path, int(cap.get("marker_offset") or 0))
                if not tail:
                    await asyncio.sleep(0.15)
                    continue
                # IMPORTANT: if the file was truncated and we rewound to 0, we must
                # not keep adding to the previous (now invalid) offset, otherwise we'll
                # re-read the same markers forever and spam duplicate command_result events.
                if rewound:
                    cap["marker_offset"] = base_off + len(tail)
                else:
                    cap["marker_offset"] = int(cap.get("marker_offset") or 0) + len(tail)
                try:
                    off_path = _user_pty_marker_offset_path(convo_id)
                    off_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(off_path.write_text, str(cap["marker_offset"]), encoding="utf-8")
                except Exception:
                    pass

                buf += tail
                lines = buf.splitlines(keepends=False)
                # Keep trailing partial line in buffer.
                if buf and not (buf.endswith(b"\n") or buf.endswith(b"\r")):
                    buf = lines[-1] if lines else buf
                    lines = lines[:-1] if len(lines) > 1 else []
                else:
                    buf = b""

                for raw_line in lines:
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if not line:
                        continue

                    if line.startswith("__FWS_BLOCK_BEGIN__"):
                        # __FWS_BLOCK_BEGIN__ seq=<n> ts=<ms> cwd_b64=<...> cmd_b64=<...>
                        m = re.search(r"seq=([0-9]+)", line)
                        if not m:
                            continue
                        seq = int(m.group(1))
                        ts_m = re.search(r"ts=([0-9]+)", line)
                        ts_begin = int(ts_m.group(1)) if ts_m else None
                        cwd_b64 = ""
                        cmd_b64 = ""
                        mcwd = re.search(r"cwd_b64=([^\\s]+)", line)
                        mcmd = re.search(r"cmd_b64=([^\\s]+)", line)
                        if mcwd:
                            cwd_b64 = mcwd.group(1)
                        if mcmd:
                            cmd_b64 = mcmd.group(1)
                        cmd = _safe_b64decode(cmd_b64)
                        cwd = _safe_b64decode(cwd_b64)
                        cap["open_blocks"][seq] = {
                            "seq": seq,
                            "ts_begin": ts_begin,
                            "cmd": cmd,
                            "cwd": cwd,
                            "begin_cursor": int(cap.get("raw_cursor") or 0),
                        }
                        continue

                    if line.startswith("__FWS_BLOCK_END__"):
                        m = re.search(r"seq=([0-9]+)", line)
                        if not m:
                            continue
                        seq = int(m.group(1))
                        exit_m = re.search(r"exit=([-0-9]+)", line)
                        exit_code = int(exit_m.group(1)) if exit_m else 0
                        ts_m = re.search(r"ts=([0-9]+)", line)
                        ts_end = int(ts_m.group(1)) if ts_m else None

                        # Dedupe: if we have already emitted this block end, skip it.
                        # (This protects against marker offset bugs and noisy reconnects.)
                        try:
                            end_key = (seq, ts_end)
                            if end_key in cap.get("emitted_block_ends", set()):
                                continue
                            cap.setdefault("emitted_block_ends", set()).add(end_key)
                            # Bound memory.
                            if len(cap["emitted_block_ends"]) > 5000:
                                cap["emitted_block_ends"].clear()
                        except Exception:
                            pass

                        block = cap["open_blocks"].pop(seq, None)
                        if not isinstance(block, dict):
                            continue
                        begin_cursor = int(block.get("begin_cursor") or 0)
                        end_cursor = int(cap.get("raw_cursor") or 0)

                        # Marker events can arrive slightly ahead of the raw-bytes capture loop
                        # (separate streams). If we slice too early, we may capture only a prompt
                        # redraw and miss the actual command output.
                        try:
                            raw_path = cap.get("raw_path")
                            if isinstance(raw_path, Path):
                                last_size = None
                                for _ in range(6):  # ~300ms max (6 * 50ms)
                                    try:
                                        size_now = int(raw_path.stat().st_size)
                                    except Exception:
                                        size_now = None
                                    if size_now is None:
                                        break
                                    end_cursor = max(end_cursor, size_now)
                                    if last_size is not None and size_now == last_size:
                                        break
                                    last_size = size_now
                                    await asyncio.sleep(0.05)
                                # Keep cursor in sync with file length if it advanced.
                                if last_size is not None:
                                    cap["raw_cursor"] = max(int(cap.get("raw_cursor") or 0), int(last_size))
                        except Exception:
                            pass
                        if end_cursor < begin_cursor:
                            continue
                        # Safety cap: don't slice unbounded output.
                        if (end_cursor - begin_cursor) > _USER_PTY_RAW_MAX_BYTES_PER_CMD:
                            begin_cursor = max(0, end_cursor - _USER_PTY_RAW_MAX_BYTES_PER_CMD)
                            output_truncated_by_bytes = True
                        else:
                            output_truncated_by_bytes = False

                        def _read_slice() -> bytes:
                            try:
                                with cap["raw_path"].open("rb") as f:  # type: ignore
                                    f.seek(begin_cursor)
                                    return f.read(max(0, end_cursor - begin_cursor))
                            except Exception:
                                return b""

                        raw_out = await asyncio.to_thread(_read_slice)
                        text = raw_out.decode("utf-8", errors="replace")
                        scrubbed = _scrub_user_cmd_output_keep_sgr(text).strip("\n")

                        # Full command output (scrubbed of cursor movement etc, but keeps SGR).
                        full_text = scrubbed
                        duration_ms = None
                        try:
                            if block.get("ts_begin") and ts_end:
                                duration_ms = max(0, int(ts_end) - int(block["ts_begin"]))
                        except Exception:
                            duration_ms = None

                        cmd = str(block.get("cmd") or "")
                        cwd = str(block.get("cwd") or "")
                        prompt = _termux_user_prompt_from_cwd(cwd)
                        agent_block_id = None
                        try:
                            ts_begin = int(block.get("ts_begin") or 0)
                            if ts_begin:
                                agent_block_id = f"{convo_id}:{seq}:{ts_begin}"
                        except Exception:
                            agent_block_id = None

                        # Remove any trailing prompt redraws from the output slice; we render
                        # prompt+command in the ribbon instead.
                        full_text = _strip_trailing_prompt_lines(full_text)
                        full_text = _strip_leading_echoed_command(full_text, prompt=prompt, cmd=cmd)

                        full_lines = full_text.splitlines()
                        # User-facing transcript + live cards should not apply an additional arbitrary
                        # line cap. The UI already uses the SSOT setting (commandOutputLines).
                        user_text = full_text.strip()

                        # Agent envelope preview stays small/noisy-resistant: use a TAIL slice.
                        envelope_lines = full_lines[-_CMD_PREVIEW_MAX_LINES:]

                        # Persist for replay.
                        await _append_transcript_entry(convo_id, {
                            "role": "command",
                            "command": cmd,
                            "cwd": cwd,
                            "prompt": prompt,
                            "agent_block_id": agent_block_id,
                            "output": user_text,
                            "exit_code": exit_code,
                            "duration_ms": duration_ms,
                            "source": "user_terminal",
                            "seq": seq,
                            "output_truncated_by_bytes": bool(output_truncated_by_bytes),
                        })

                        # Live render.
                        await _broadcast_appserver_ui({
                            "type": "command_result",
                            "conversation_id": convo_id,
                            "command": cmd,
                            "cwd": cwd,
                            "prompt": prompt,
                            "agent_block_id": agent_block_id,
                            "output": user_text,
                            "exit_code": exit_code,
                            "duration_ms": duration_ms,
                            "source": "user_terminal",
                            "output_truncated_by_bytes": bool(output_truncated_by_bytes),
                        })

                        # Buffer for CODEX_META on next turn/start.
                        await _append_pending_cmd_buffer(convo_id, {
                            "cmd": cmd,
                            "exit_code": exit_code,
                            "cwd": cwd,
                            "block_id": f"user:{convo_id}:{seq}:{ts_end or int(time.time() * 1000)}",
                            "ts": ts_end or int(time.time() * 1000),
                            "preview": {
                                "lines": envelope_lines[-_CMD_PREVIEW_MAX_LINES:],
                                "truncated": bool(output_truncated_by_bytes or (len(full_lines) > _CMD_PREVIEW_MAX_LINES)),
                            },
                        })
                        continue

        cap["marker_task"] = asyncio.create_task(_marker_loop(), name=f"user-pty-markers:{convo_id}")

    # Start/ensure background capture for this conversation (best-effort).
    try:
        mgr_for_capture = hub.get("mgr") or await _get_fws_manager()
        await _ensure_user_capture(conversation_id, shell_id, mgr_for_capture)
    except Exception:
        pass

    # Register this websocket with the hub.
    client_q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    hub["clients"].add(websocket)
    hub["client_queues"][websocket] = client_q
    hub["client_sanitize"][websocket] = bool(sanitize)

    async def send_output():
        try:
            while True:
                chunk = await client_q.get()
                await websocket.send_text(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    send_task = asyncio.create_task(send_output(), name=f"pty-ws-sender:{conversation_id}")
    hub["client_sender_tasks"][websocket] = send_task

    async def _schedule_hub_idle_shutdown(hub: Dict[str, Any]) -> None:
        # Wait a bit; if still no clients, tear down subscription.
        await asyncio.sleep(max(0.0, _PTY_HUB_IDLE_SECS))
        async with _pty_hub_lock:
            if hub.get("clients"):
                return
            # Still empty; stop + unsubscribe.
            try:
                hub.get("stop").set()
            except Exception:
                pass
            try:
                t = hub.get("task")
                if t:
                    t.cancel()
            except Exception:
                pass
            try:
                mgr0 = hub.get("mgr")
                q0 = hub.get("out_q")
                sid0 = hub.get("shell_id")
                if mgr0 and q0 and sid0:
                    await mgr0.unsubscribe_output(sid0, q0)
            except Exception:
                pass
            _pty_hubs.pop(hub.get("conversation_id"), None)

    async def receive_input():
        try:
            mgr = hub.get("mgr") or await _get_fws_manager()
            while True:
                try:
                    data = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
                if not data:
                    continue
                try:
                    await mgr.write_to_pty(shell_id, data)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    recv_task = asyncio.create_task(receive_input(), name=f"pty-ws-recv:{conversation_id}")

    try:
        await asyncio.gather(send_task, recv_task, return_exceptions=True)
    finally:
        with suppress(Exception):
            recv_task.cancel()
        with suppress(Exception):
            send_task.cancel()
        # Remove from hub, and schedule idle shutdown if empty.
        try:
            hub["clients"].discard(websocket)
            hub["client_queues"].pop(websocket, None)
            hub["client_sanitize"].pop(websocket, None)
            hub["client_sender_tasks"].pop(websocket, None)
        except Exception:
            pass
        if hub and not hub.get("clients"):
            # Only one idle task per hub.
            try:
                if not hub.get("idle_task") or hub["idle_task"].done():
                    hub["idle_task"] = asyncio.create_task(_schedule_hub_idle_shutdown(hub), name=f"pty-hub-idle:{conversation_id}")
            except Exception:
                pass


@app.post("/api/pty/stdin")
async def api_pty_stdin(payload: Dict[str, Any] = Body(...)):
    """Send stdin to the PTY (for non-WebSocket clients)."""
    data = payload.get("data", "")
    if not isinstance(data, str):
        raise HTTPException(status_code=400, detail="data must be a string")
    
    async with _config_lock:
        cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    if not convo_id:
        raise HTTPException(status_code=409, detail="No active conversation")
    
    try:
        import mcp_agent_pty_server as mcp_srv
        state = mcp_srv._state(convo_id)
        if not state.shell_id:
            raise HTTPException(status_code=409, detail="No PTY running")
        mgr = await _get_fws_manager()
        await mgr.write_to_pty(state.shell_id, data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write stdin: {e}")
    
    return {"ok": True}


@app.get("/api/pty/status")
async def api_pty_status():
    """Get PTY status including whether a command is running."""
    async with _config_lock:
        cfg = _load_appserver_config()
    convo_id = cfg.get("conversation_id")
    if not convo_id:
        return {"running": False, "command_active": False}
    
    command_active = _pty_command_running.get(convo_id, False)
    
    try:
        import mcp_agent_pty_server as mcp_srv
        state = mcp_srv._state(convo_id)
        has_shell = state.shell_id is not None
        has_active_block = state._active is not None
    except Exception:
        has_shell = False
        has_active_block = False
    
    return {
        "running": has_shell,
        "command_active": has_active_block or command_active,
        "conversation_id": convo_id,
    }

@app.get("/api/pty/raw_tail")
async def api_pty_raw_tail(
    conversation_id: Optional[str] = Query(None),
    max_bytes: int = Query(65536),
) -> Dict[str, Any]:
    """Return a tail of the lossless raw PTY byte stream (for xterm.js priming).

    This is used to "rehydrate" the composer terminal on first open so the user
    sees an initial prompt/history immediately, similar to the standalone
    terminal app. Returned as base64 so we can preserve control bytes safely.
    """
    max_bytes = int(max_bytes or 0)
    if max_bytes <= 0:
        max_bytes = 65536
    # Keep bounded: this endpoint is for UI priming, not full replay.
    max_bytes = min(max_bytes, 256 * 1024)

    convo_id = (conversation_id or "").strip()
    if not convo_id:
        async with _config_lock:
            cfg = _load_appserver_config()
        convo_id = str(cfg.get("conversation_id") or "").strip()
    if not convo_id:
        raise HTTPException(status_code=409, detail="No conversation_id")

    safe_id = _sanitize_conversation_id(convo_id)
    raw_path = _conversation_dir(safe_id) / "agent_pty" / "output.raw"

    def _read_tail() -> tuple[bytes, int, int]:
        if not raw_path.exists():
            return (b"", 0, 0)
        try:
            st = raw_path.stat()
            size = int(st.st_size)
            start = max(0, size - max_bytes)
            with raw_path.open("rb") as f:
                f.seek(start)
                data = f.read(max_bytes)
            return (data, start, size)
        except Exception:
            return (b"", 0, 0)

    data, start, total = await asyncio.to_thread(_read_tail)

    # Avoid starting in the middle of a line/control sequence when possible:
    # drop a leading partial line fragment if the tail begins mid-line.
    try:
        cut = None
        for i, b in enumerate(data[:4096]):
            if b in (10, 13):  # \n or \r
                cut = i + 1
                break
        if cut is not None and cut > 0 and cut < len(data):
            data = data[cut:]
            start = min(total, start + cut)
    except Exception:
        pass

    # Best-effort sanitize wrapper noise to match the live /ws/pty stream behavior.
    # Keep ANSI; drop only obvious wrapper/marker lines.
    try:
        import mcp_agent_pty_server as mcp_srv
        text = data.decode("utf-8", errors="replace")
        sanitized = mcp_srv.ConversationState._sanitize_user_terminal_stream(text)
        data = sanitized.encode("utf-8", errors="replace")
    except Exception:
        pass

    return {
        "ok": True,
        "conversation_id": convo_id,
        "path": str(raw_path),
        "max_bytes": max_bytes,
        "raw_size": total,
        "offset": start,
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


@app.get("/api/pty/fws_tail")
async def api_pty_fws_tail(
    conversation_id: Optional[str] = Query(None),
    tail_lines: int = Query(200),
) -> Dict[str, Any]:
    """Return a tail of the framework_shells stdout log for the conversation's PTY.

    This is the preferred UI rehydration source because it matches what the user
    terminal sees (including ANSI) without depending on conversation-local
    artifacts like agent_pty/output.raw.
    """
    tail_lines = max(0, min(int(tail_lines or 0), 5000))

    convo_id = (conversation_id or "").strip()
    if not convo_id:
        async with _config_lock:
            cfg = _load_appserver_config()
        convo_id = str(cfg.get("conversation_id") or "").strip()
    if not convo_id:
        raise HTTPException(status_code=409, detail="No conversation_id")

    try:
        import mcp_agent_pty_server as mcp_srv  # type: ignore
        state = mcp_srv._state(convo_id)
        await state.ensure_shell()
        shell_id = state.shell_id
        if not shell_id:
            raise RuntimeError("No PTY shell_id")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to resolve PTY shell: {exc}")

    mgr = await _get_fws_manager()
    rec = await mgr.get_shell(shell_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Shell not found")
    try:
        detail = await mgr.describe(rec, include_logs=True, tail_lines=tail_lines)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"describe failed: {exc}")

    logs = (detail or {}).get("logs") if isinstance(detail, dict) else None
    stdout_tail = (logs or {}).get("stdout_tail") if isinstance(logs, dict) else None
    if not isinstance(stdout_tail, list):
        stdout_tail = []

    return {
        "ok": True,
        "conversation_id": convo_id,
        "shell_id": shell_id,
        "tail_lines": tail_lines,
        "stdout_tail": stdout_tail,
    }


# --- Startup ---
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="agent_chat.log.jsonl")
    p.add_argument("--port", type=int, default=12356)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--broadcast-all", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    return p.parse_args()

def main():
    global DEBUG_MODE
    global LOG_PATH
    global DEBUG_RAW_LOG_PATH
    args = parse_args()
    DEBUG_MODE = bool(args.debug)
    if args.broadcast_all:
        args.host = "0.0.0.0"
    
    log_p = Path(args.log)
    if not log_p.is_absolute():
        log_p = Path.cwd() / log_p
    ensure_log_file(log_p)
    LOG_PATH = log_p
    
    # Initialize message number counter from existing log
    _init_msg_num()

    # Set up debug raw log in .cache directory
    if DEBUG_MODE:
        cache_dir = Path.home() / ".cache" / "agent_log_server"
        cache_dir.mkdir(parents=True, exist_ok=True)
        DEBUG_RAW_LOG_PATH = cache_dir / "debug_raw.jsonl"
        # Clear previous debug log on startup
        DEBUG_RAW_LOG_PATH.write_text("")

    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            r = {"ts": utc_ts(), "who": "server", "message": f"started on {args.host}:{args.port}"}
            f.write(json.dumps(r) + "\n")
    except Exception:
        pass

    uvicorn.run(socketio_app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import asyncio
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
        await _get_or_start_shell_manager()
        await _get_or_start_mcp_shell()
        agent_pty_monitor_task = asyncio.create_task(_agent_pty_monitor_loop(), name="agent-pty-monitor")
    except Exception:
        pass
    yield
    if agent_pty_monitor_task:
        agent_pty_monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await agent_pty_monitor_task

app = FastAPI(lifespan=_lifespan)
socketio_server = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
socketio_app = socketio.ASGIApp(socketio_server, other_asgi_app=app)


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
                        if isinstance(text, str) and text.strip():
                            key = ("user", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "user", "text": text.strip(), "ts": rec.get("timestamp")})
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
        if text:
            return {"role": "user", "text": text}
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
                await _ensure_agent_pty_event_tailer(convo_id)
                await _ensure_agent_pty_screen_event_tailer(convo_id)
                await _ensure_agent_pty_raw_event_tailer(convo_id)
                await _tail_agent_pty_events_to_transcript(convo_id)
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
) -> List[Dict[str, Any]]:
    """
    Route codex-app-server events to frontend (streaming) and transcript (replay).
    
    Returns a list of events to broadcast to the frontend via WebSocket.
    Also writes completed items to the transcript SSOT for replay.
    """
    events: List[Dict[str, Any]] = []
    if not label:
        return events
    async with _config_lock:
        cfg = _load_appserver_config()
        convo_id = cfg.get("conversation_id")

    thread_id = _get_thread_id(conversation_id, payload)
    if not convo_id and thread_id:
        convo_id = _find_conversation_by_thread_id(thread_id)
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
        return events

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
        return events

    # -------------------------------------------------------------------------
    # SECTION: Turn Lifecycle Events (Frontend + Transcript)
    # -------------------------------------------------------------------------
    # Turn start/complete events update UI activity state and write status to
    # transcript for replay. Plans are accumulated during turn and written on complete.
    
    if label_lower == "thread/started":
        # [Frontend] Activity indicator only
        events.append({"type": "activity", "label": "thread started", "active": True})
        return events

    if label_lower in {"turn/started", "turn/completed"}:
        if label_lower == "turn/started":
            await _set_turn_id(turn_id)
        else:
            await _set_turn_id(None)
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
        return events

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
        return events

    if label_lower == "codex/event/turn_diff" and isinstance(payload, dict):
        diff, path = _extract_diff_with_path(payload)
        if diff:
            # [Frontend] diff event + [Transcript] for replay
            await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item_id, events, path=path)
        return events

    # -------------------------------------------------------------------------
    # SECTION: Token Usage (Frontend + Transcript for Replay)
    # -------------------------------------------------------------------------
    # Token counts update the context window display and are saved for replay.
    
    if label_lower in {"codex/event/token_count", "thread/tokenusage/updated"} and isinstance(payload, dict):
        total = None
        input_tokens = None
        cached_input_tokens = None
        context_window = None
        
        # Handle codex/event/token_count format: { info: { total_token_usage: {...}, model_context_window } }
        if isinstance(payload.get("info"), dict):
            info = payload["info"]
            usage = info.get("total_token_usage") or {}
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                input_tokens = usage.get("input_tokens")
                cached_input_tokens = usage.get("cached_input_tokens")
            context_window = info.get("model_context_window")
        
        # Handle thread/tokenUsage/updated format: { tokenUsage: { total: {totalTokens, inputTokens, cachedInputTokens}, modelContextWindow } }
        if total is None and isinstance(payload.get("tokenUsage"), dict):
            token_usage = payload["tokenUsage"]
            total_breakdown = token_usage.get("total") or {}
            if isinstance(total_breakdown, dict):
                total = total_breakdown.get("totalTokens") or total_breakdown.get("total_tokens")
                input_tokens = total_breakdown.get("inputTokens") or total_breakdown.get("input_tokens")
                cached_input_tokens = total_breakdown.get("cachedInputTokens") or total_breakdown.get("cached_input_tokens")
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
        return events

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
        return events

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
        return events

    if label_lower == "codex/event/warning" and isinstance(payload, dict):
        # [Frontend only] Warnings not persisted to transcript
        message = payload.get("message") or payload.get("msg", {}).get("message") or ""
        if message:
            events.append({
                "type": "warning",
                "message": message,
            })
        return events

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
        return events

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
        return events

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
        return events

    # -------------------------------------------------------------------------
    # SECTION: Item Events (Frontend streaming deltas + Transcript on complete)
    # -------------------------------------------------------------------------
    # Item lifecycle: item/started -> deltas -> item/completed
    # - Deltas stream to frontend for live display
    # - Complete items written to transcript for replay
    
    if label_lower == "item/started" and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        item_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
        
        if item_type == "usermessage":
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
            return events
            
        if item_type == "reasoning":
            # Track state for delta accumulation
            state["reason_source"] = state["reason_source"] or "item"
            if item.get("id"):
                state["reasoning_id"] = item.get("id")
                _register_item_state(item.get("id"), state)
            return events
            
        if item_type == "filechange":
            # Cache diff info for approval - actual diff emitted via turn_diff
            diff, path = _extract_diff_with_path(item)
            if item.get("id"):
                _approval_item_cache[str(item.get("id"))] = {
                    "diff": diff,
                    "changes": item.get("changes"),
                    "path": path,
                }
            return events
            
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
            return events
            
        if item_type in {"agentmessage", "assistantmessage", "assistant"}:
            # Track state for delta accumulation
            state["msg_source"] = state["msg_source"] or "item"
            if item.get("id"):
                state["assistant_id"] = item.get("id")
                _register_item_state(item.get("id"), state)
            state["assistant_started"] = True
            return events

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
            return events
            
        if item_type == "reasoning":
            summary = item.get("summary") or item.get("summary_text") or []
            if isinstance(summary, list) and summary:
                text = " ".join(str(s) for s in summary if s).strip()
            else:
                text = str(summary).strip() if summary else ""
            # [Transcript] Save complete reasoning for replay
            if text and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "reasoning",
                    "text": text,
                    "item_id": item.get("id"),
                    "event": "item/completed",
                })
            # [Frontend] Finalize streaming reasoning
            if state.get("reason_source") in {None, "item"} and state.get("reasoning_started"):
                events.append({"type": "reasoning_finalize", "id": item.get("id") or state.get("reasoning_id") or "reasoning", "text": text})
                state["reasoning_started"] = False
                state["reasoning_buffer"] = ""
                state["reasoning_id"] = None
            return events
            
        if item_type == "filechange":
            # Cache for approval tracking - diff emitted via turn_diff
            diff, path = _extract_diff_with_path(item)
            if item.get("id") and diff:
                _approval_item_cache[str(item.get("id"))] = {
                    "diff": diff,
                    "changes": item.get("changes"),
                    "path": path,
                }
            return events
            
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
            return events
            
        return events

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
        return events

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
                events.append({"type": "reasoning_delta", "id": item_id, "delta": delta})
                events.append({"type": "activity", "label": "reasoning", "active": True})
        return events

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
        return events

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
        return events

    if label_lower == "codex/event/agent_message" and isinstance(payload, dict):
        # [Transcript] + [Frontend] Complete message (legacy format)
        text = payload.get("message") or payload.get("text")
        if isinstance(text, str) and convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "assistant",
                "text": text.strip(),
                "item_id": payload.get("item_id") or payload.get("itemId"),
                "event": "agent_message",
            })
        if state.get("msg_source") in {None, "codex"} and state.get("assistant_started"):
            events.append({"type": "assistant_finalize", "id": payload.get("item_id") or payload.get("itemId") or state.get("assistant_id") or "assistant", "text": text})
        return events

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
                events.append({"type": "reasoning_delta", "id": item_id, "delta": delta})
                events.append({"type": "activity", "label": "reasoning", "active": True})
        return events

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
        return events

    if label_lower == "codex/event/agent_reasoning" and isinstance(payload, dict):
        # [Frontend] Finalize reasoning (legacy format)
        text = payload.get("text") or payload.get("message")
        if state.get("reason_source") in {None, "codex"} and state.get("reasoning_started"):
            events.append({"type": "reasoning_finalize", "id": payload.get("item_id") or payload.get("itemId") or state.get("reasoning_id") or "reasoning", "text": text})
            state["reasoning_started"] = False
            state["reasoning_buffer"] = ""
            state["reasoning_id"] = None
        return events

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
        return events

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
        return events

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
        return events

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
        return events

    # No handler matched - return empty events list
    return events

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

            events = await _route_appserver_event(label, payload, conversation_id, request_id)
            for event in events:
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

async def append_record(record: Dict[str, Any]) -> None:
    assert LOG_PATH is not None
    line = json.dumps(record, ensure_ascii=False)
    async with _lock:
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
    if not path.exists():
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Remove sidecar directory
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
                items.append(record)
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
                buf.append(record)
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
                    items.append(record)
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
    await _broadcast_appserver_ui({"type": "shell_begin", "id": call_id, "command": cmd, "cwd": cwd, "stream": "stdout"})
    # No deltas here; those are streamed via agent_block_delta. shell_end still helps unify UI handling.
    exit_code = block.get("exit_code") if isinstance(block.get("exit_code"), int) else 0
    await _broadcast_appserver_ui({"type": "shell_end", "id": call_id, "exitCode": exit_code, "stdout": "", "stderr": ""})


@app.post("/api/appserver/rpc")
async def api_appserver_rpc(payload: Dict[str, Any] = Body(...)):
    print(f"[DEBUG] /api/appserver/rpc received: {payload}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    
    # Intercept thread/resume, thread/start, turn/start to inject settings from SSOT
    method = payload.get("method", "")
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
async def api_appserver_mention(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=400, detail="Missing or invalid 'path'")
    await _broadcast_appserver_ui({"type": "mention_insert", "path": path})
    return {"ok": True}


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

@app.post("/api/messages", status_code=201)
async def post_message(msg: MessageIn):
    who = msg.who.strip()
    text = msg.message.strip()
    if not who or not text:
        return JSONResponse({"error": "Both 'who' and 'message' are required"}, status_code=400)

    record = {"ts": utc_ts(), "who": who, "message": text}
    await append_record(record)
    return record

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
    
    # Import MCP server module for PTY access
    try:
        import mcp_agent_pty_server as mcp_srv
    except ImportError:
        await websocket.close(code=1011, reason="MCP server not available")
        return
    
    # Get or create conversation state and ensure shell is running
    state = mcp_srv._state(conversation_id)
    try:
        await state.ensure_shell()
    except Exception as e:
        await websocket.close(code=1011, reason=f"Failed to start PTY: {e}")
        return
    
    # Create a queue for this connection
    output_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    ws_closed = False
    
    async def chunk_callback(chunk: str) -> None:
        """Callback to receive raw PTY chunks."""
        if not ws_closed:
            try:
                output_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass
    
    # Register callback for raw chunks
    state.add_raw_chunk_callback(chunk_callback)
    
    async def send_output():
        """Send PTY output to WebSocket."""
        nonlocal ws_closed
        try:
            while not ws_closed:
                chunk = await output_queue.get()
                try:
                    await websocket.send_text(chunk)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
    
    async def receive_input():
        """Receive stdin from WebSocket and write to PTY."""
        nonlocal ws_closed
        try:
            mgr = await _get_fws_manager()
            while True:
                try:
                    data = await websocket.receive_text()
                    if state.shell_id:
                        await mgr.write_to_pty(state.shell_id, data)
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            ws_closed = True
    
    send_task = asyncio.create_task(send_output())
    recv_task = asyncio.create_task(receive_input())
    
    try:
        await asyncio.gather(send_task, recv_task, return_exceptions=True)
    finally:
        ws_closed = True
        send_task.cancel()
        recv_task.cancel()
        state.remove_raw_chunk_callback(chunk_callback)


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

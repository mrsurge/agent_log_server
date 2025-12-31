#!/usr/bin/env python3
import asyncio
import json
import os
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import suppress
import hashlib
import re
import secrets
import uuid

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from framework_shells import get_manager as get_framework_shell_manager
from framework_shells.orchestrator import Orchestrator

from fasthtml.common import (
    HTMLResponse as FastHTMLResponse,
    Html, Head, Body, Div, Section, Header, Footer, Main, H1, H2, H3, P, Button,
    Span, Input, Textarea, Label, Small, A, Ul, Li, Code, Script, Link, Meta, to_xml
)

app = FastAPI()

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
_appserver_rpc_waiters: Dict[str, asyncio.Future] = {}
DEBUG_MODE = False
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
        "conversation_id": None,
        "conversations": [],
        "active_view": "splash",
        "app_server_command": None,
        "shell_id": None,
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


def _sanitize_conversation_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return safe or "unknown"


def _transcript_path(conversation_id: str) -> Path:
    return _conversation_transcript_path(conversation_id)


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
        }
        _appserver_turn_state[key] = state
    return state


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
    events.append({"type": "diff", "id": diff_id, "text": diff_text})
    if record_transcript and conversation_id:
        await _append_transcript_entry(conversation_id, {
            "role": "diff",
            "text": diff_text,
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
        # Capture reasoning summary on completion
        item_type = str(item.get("type") or "").lower()
        if item_type == "reasoning":
            summary = item.get("summary") or item.get("text") or ""
            if isinstance(summary, str) and summary.strip():
                await _append_transcript_entry(conversation_id, {
                    "role": "reasoning",
                    "text": summary.strip(),
                    "item_id": item.get("id"),
                    "event": "reasoning_completed",
                })
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
                # Capture reasoning summary from codex events
                item_type = str(item_payload.get("type") or "").lower()
                if event_type == "item_completed" and item_type == "reasoning":
                    summary = item_payload.get("summary") or item_payload.get("text") or ""
                    if isinstance(summary, str) and summary.strip():
                        await _append_transcript_entry(conversation_id, {
                            "role": "reasoning",
                            "text": summary.strip(),
                            "item_id": item_payload.get("id"),
                            "event": "reasoning_completed",
                        })


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


async def _get_or_start_appserver_shell() -> Dict[str, Any]:
    global _appserver_shell_id
    _ensure_framework_shells_secret()
    async with _config_lock:
        cfg = _load_appserver_config()
        if cfg.get("shell_id"):
            _appserver_shell_id = cfg["shell_id"]

    if _appserver_shell_id:
        mgr = await get_framework_shell_manager()
        shell = await mgr.get_shell(_appserver_shell_id)
        if shell and shell.status == "running":
            return {"shell_id": _appserver_shell_id, "status": "running", "pid": shell.pid}

    # Start a new app-server shell via shellspec
    mgr = await get_framework_shell_manager()
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
    mgr = await get_framework_shell_manager()
    try:
        await mgr.terminate_shell(_appserver_shell_id, force=True)
    except Exception:
        pass
    async with _config_lock:
        cfg = _load_appserver_config()
        cfg["shell_id"] = None
        _save_appserver_config(cfg)
    _appserver_shell_id = None


async def _broadcast_appserver_ui(event: Dict[str, Any]) -> None:
    if not _appserver_ws_clients_ui:
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


async def _broadcast_appserver_raw(message: str) -> None:
    _appserver_raw_buffer.append(message)
    if len(_appserver_raw_buffer) > 500:
        _appserver_raw_buffer[:] = _appserver_raw_buffer[-500:]
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


async def _route_appserver_event(
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str],
    request_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
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

    # Approvals
    if "commandexecution/requestapproval" in label_lower:
        if isinstance(payload, dict):
            events.append({
                "type": "approval",
                "kind": "command",
                "id": request_id or payload.get("_request_id") or payload.get("id"),
                "payload": {
                    "command": payload.get("command") or payload.get("parsedCmd") or payload.get("cmd"),
                    "cwd": payload.get("cwd"),
                    "reason": payload.get("reason"),
                    "risk": payload.get("risk"),
                },
            })
            events.append({"type": "activity", "label": "approval", "active": True})
        return events

    if "filechange/requestapproval" in label_lower or "applypatchapproval" in label_lower:
        if isinstance(payload, dict):
            events.append({
                "type": "approval",
                "kind": "diff",
                "id": request_id or payload.get("_request_id") or payload.get("id"),
                "payload": {
                    "diff": payload.get("diff") or payload.get("patch") or payload.get("unified_diff"),
                    "changes": payload.get("changes"),
                    "reason": payload.get("reason"),
                },
            })
            events.append({"type": "activity", "label": "approval", "active": True})
        return events

    # Thread/turn activity
    if label_lower == "thread/started":
        events.append({"type": "activity", "label": "thread started", "active": True})
        return events

    if label_lower in {"turn/started", "turn/completed"}:
        events.append({"type": "activity", "label": "turn started" if label_lower == "turn/started" else "idle", "active": label_lower == "turn/started"})
        return events

    # Unified diff
    if label_lower == "turn/diff/updated" and isinstance(payload, dict):
        diff = _extract_diff_text(payload)
        if diff:
            await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item_id, events)
        return events

    if label_lower == "codex/event/turn_diff" and isinstance(payload, dict):
        diff = _extract_diff_text(payload)
        if diff:
            await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item_id, events)
        return events

    # Token counts
    if label_lower in {"codex/event/token_count", "thread/tokenusage/updated"} and isinstance(payload, dict):
        total = payload.get("total") or payload.get("total_tokens") or payload.get("tokenCount") or payload.get("tokens")
        if total is None and isinstance(payload.get("usage"), dict):
            total = payload["usage"].get("total") or payload["usage"].get("total_tokens")
        if total is None and isinstance(payload.get("tokenUsage"), dict):
            total = payload["tokenUsage"].get("total") or payload["tokenUsage"].get("total_tokens")
        if isinstance(total, (int, float)):
            events.append({"type": "token_count", "total": int(total)})
        return events

    # Suppress noisy events
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

    # Item events
    if label_lower == "item/started" and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        item_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
        if item_type == "usermessage":
            entry = _extract_item_text(item)
            if entry and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": entry["role"],
                    "text": entry["text"],
                    "item_id": item.get("id"),
                    "event": "item/started",
                })
            if entry:
                events.append({"type": "message", "role": "user", "id": item.get("id"), "text": entry["text"]})
            return events
        if item_type == "reasoning":
            state["reason_source"] = state["reason_source"] or "item"
            if item.get("id"):
                state["reasoning_id"] = item.get("id")
                _register_item_state(item.get("id"), state)
            return events
        if item_type == "filechange":
            diff = _extract_diff_text(item)
            if diff:
                await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item.get("id"), events)
            return events
        if item_type in {"agentmessage", "assistantmessage", "assistant"}:
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
            if entry and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": entry["role"],
                    "text": entry["text"],
                    "item_id": item.get("id"),
                    "event": "item/completed",
                })
            if state.get("msg_source") in {None, "item"} and state.get("assistant_started"):
                events.append({"type": "assistant_finalize", "id": item.get("id") or state.get("assistant_id") or "assistant", "text": entry["text"] if entry else item.get("text")})
            events.append({"type": "activity", "label": "idle", "active": False})
            return events
        if item_type == "reasoning":
            summary = _extract_reasoning_text(item, state.get("reasoning_buffer"))
            if summary and convo_id:
                await _append_transcript_entry(convo_id, {
                    "role": "reasoning",
                    "text": summary,
                    "item_id": item.get("id"),
                    "event": "reasoning/completed",
                })
            events.append({"type": "activity", "label": "idle", "active": False})
            return events
        if item_type == "filechange":
            diff = _extract_diff_text(item)
            if diff:
                await _emit_diff_event(state, diff, convo_id, thread_id, turn_id, item.get("id"), events)
            return events
        return events

    # Agent message deltas (JSON-RPC)
    if label_lower == "item/agentmessage/delta" and isinstance(payload, dict):
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

    # Reasoning deltas (JSON-RPC)
    if label_lower in {"item/reasoning/summarytextdelta", "item/reasoning/textdelta"} and isinstance(payload, dict):
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

    # Codex event deltas
    if label_lower in {"codex/event/agent_message_content_delta", "codex/event/agent_message_delta"} and isinstance(payload, dict):
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
        events.append({"type": "activity", "label": "idle", "active": False})
        return events

    if label_lower in {"codex/event/agent_reasoning_delta", "codex/event/reasoning_content_delta", "codex/event/reasoning_summary_delta"} and isinstance(payload, dict):
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
        text = payload.get("text") or payload.get("message")
        if isinstance(text, str) and convo_id:
            await _append_transcript_entry(convo_id, {
                "role": "reasoning",
                "text": text.strip(),
                "item_id": payload.get("item_id") or payload.get("itemId"),
                "event": "agent_reasoning",
            })
        events.append({"type": "activity", "label": "idle", "active": False})
        return events

    return events


async def _ensure_appserver_reader(shell_id: str) -> None:
    global _appserver_reader_task
    if _appserver_reader_task and not _appserver_reader_task.done():
        return

    async def _reader():
        mgr = await get_framework_shell_manager()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdout:
            return
        pending_label: Optional[str] = None
        while True:
            line = await state.process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            await _broadcast_appserver_raw(text)

            # Handle label + JSON on same line.
            if "{" in text and not text.lstrip().startswith("{"):
                prefix, rest = text.split("{", 1)
                if prefix.strip() and rest.strip().startswith("{"):
                    pending_label = prefix.strip()
                    text = "{" + rest

            parsed = None
            try:
                parsed = json.loads(text)
            except Exception:
                if "/" in text or text.endswith("started") or text.endswith("completed"):
                    pending_label = text
                continue

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
                continue

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

    _appserver_reader_task = asyncio.create_task(_reader(), name="appserver-stdout-reader")


async def _write_appserver(payload: Dict[str, Any]) -> None:
    shell_id = _appserver_shell_id
    if not shell_id:
        cfg = _load_appserver_config()
        shell_id = cfg.get("shell_id")
    if not shell_id:
        raise HTTPException(status_code=409, detail="app-server not running")
    mgr = await get_framework_shell_manager()
    state = mgr.get_pipe_state(shell_id)
    if not state or not state.process.stdin:
        raise HTTPException(status_code=409, detail="app-server pipe not available")
    line = json.dumps(payload, ensure_ascii=False)
    state.process.stdin.write((line + "\n").encode("utf-8"))
    await state.process.stdin.drain()

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
    js_pill = Div(Span("JS"), Span("pending", id="js-status", cls="pill warn"), cls="status-pill") if DEBUG_MODE else None
    version = _codex_agent_version()
    return FastHTMLResponse(
        to_xml(
            Html(
            Head(
                Link(rel="manifest", href=f"/codex-agent/manifest.json?v={version}"),
                Meta(name="theme-color", content=CODEX_AGENT_THEME_COLOR),
                Link(rel="icon", type="image/svg+xml", href=_asset(CODEX_AGENT_ICON_PATH)),
                Link(rel="stylesheet", href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap"),
                Link(rel="stylesheet", href=_asset("/static/codex_agent.css")),
                Script(src="https://unpkg.com/htmx.org@1.9.12", defer=True),
                Script(src=_asset("/static/codex_agent.js"), defer=True),
            ),
            Body(
                Div(
                    Header(
                        Div(
                            H1("Codex Agent"),
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
                                Button("New Conversation", id="conversation-create", cls="btn primary"),
                                cls="splash-body"
                            ),
                            cls="splash-view",
                            id="splash-view"
                        ),
                        Section(
                            Div(
                                Div(
                                    H2("Conversation"),
                                    Small("Single-session mode"),
                                    cls="brand"
                                ),
                                Div(
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
                                Textarea(
                                    id="agent-prompt",
                                    placeholder="Message to Codex… (Shift+Enter for newline)",
                                ),
                                Button("Send", id="agent-send", cls="btn primary"),
                                cls="composer"
                            ),
                            Footer(
                                Div(
                                    Span("Conv"),
                                    Span("-", id="active-conversation", cls="pill"),
                                    cls="status-pill"
                                ),
                                Div(
                                    Span("WS"),
                                    Span("idle", id="agent-ws", cls="pill"),
                                    cls="status-pill"
                                ),
                                Div(
                                    Span("Msgs"),
                                    Span("0", id="counter-messages", cls="pill"),
                                    cls="status-pill"
                                ),
                                Div(
                                    Span("Tokens"),
                                    Span("0", id="counter-tokens", cls="pill"),
                                    cls="status-pill"
                                ),
                                Div(
                                    Span("Scroll"),
                                    Button("Pinned", id="scroll-pin", cls="btn tiny toggle active"),
                                    cls="status-pill"
                                ),
                                js_pill or "",
                                Div(
                                    Span("Mode"),
                                    Span("portrait-friendly", cls="pill ok"),
                                    cls="status-pill"
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
                                H3("Pick CWD"),
                                Button("×", id="picker-close", cls="btn ghost"),
                                cls="picker-header"
                            ),
                            Div(
                                Div(id="picker-path", cls="picker-path"),
                                Div(id="picker-list", cls="picker-list"),
                                cls="picker-body"
                            ),
                            Div(
                                Button("Up", id="picker-up", cls="btn ghost"),
                                Button("Select Current", id="picker-select", cls="btn primary"),
                                cls="picker-footer"
                            ),
                            cls="picker-dialog"
                        ),
                        cls="picker-overlay hidden",
                        id="cwd-picker"
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
        if meta.get("thread_id") and not cfg.get("thread_id"):
            cfg["thread_id"] = meta.get("thread_id")
        _save_appserver_config(cfg)
    return meta


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


@app.get("/api/appserver/transcript")
async def api_appserver_transcript(conversation_id: Optional[str] = Query(None)):
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
    mgr = await get_framework_shell_manager()
    shell = await mgr.get_shell(shell_id)
    if shell and shell.status == "running":
        return {"running": True, "shell_id": shell_id, "pid": shell.pid}
    return {"running": False, "shell_id": shell_id}


@app.post("/api/appserver/rpc")
async def api_appserver_rpc(payload: Dict[str, Any] = Body(...)):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    await _write_appserver(payload)
    return {"ok": True}


@app.post("/api/appserver/initialize")
async def api_appserver_initialize():
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
        return result
    raise HTTPException(status_code=500, detail="Invalid RPC response")


@app.get("/api/appserver/models")
async def api_appserver_models():
    response = await _rpc_request("model/list", params={})
    return response


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

# --- Startup ---
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="agent_chat.log.jsonl")
    p.add_argument("--port", type=int, default=12356)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()

def main():
    global DEBUG_MODE
    global LOG_PATH
    args = parse_args()
    DEBUG_MODE = bool(args.debug)
    
    log_p = Path(args.log)
    if not log_p.is_absolute():
        log_p = Path.cwd() / log_p
    ensure_log_file(log_p)
    LOG_PATH = log_p

    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            r = {"ts": utc_ts(), "who": "server", "message": f"started on {args.host}:{args.port}"}
            f.write(json.dumps(r) + "\n")
    except Exception:
        pass

    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()

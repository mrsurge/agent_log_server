#!/data/data/com.termux/files/usr/bin/python
import asyncio
import base64
import json
import os
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Dict, List, NoReturn, Optional, Tuple, cast
from contextlib import suppress, asynccontextmanager, redirect_stdout
import hashlib
import re
import secrets
import uuid
import subprocess
import socketio
import binascii

import uvicorn
from fastapi import FastAPI, Request, Body, HTTPException
from fastapi.responses import Response, RedirectResponse

from framework_shells import get_manager as get_framework_shell_manager
from framework_shells.api import fws_ui

import extensions as ext_loader
from agent_log_server import ask_user_interactions
from agent_log_server.agent_log_subsystem import AgentLogSubsystem
from agent_log_server.extension_cli import (
    register_extension_subcommands as _register_extension_subcommands,
    run_extension_command as _run_extension_command,
)
from agent_log_server.appserver_socketio import (
    APPSERVER_NAMESPACE,
    AppserverSocketioDeps,
    CONVERSATIONS_RPC_NAMESPACE,
    register_appserver_socketio_handlers,
)
from agent_log_server.appserver_routes import (
    AppserverMessageIn,
    AppserverRoutes,
    AppserverRoutesDeps,
    AppserverRoutesState,
    register_appserver_routes,
)
from agent_log_server.extension_api import (
    ExtensionApi,
    ExtensionApiDeps,
    register_extension_api_routes,
)
from agent_log_server.conversation_store import conversation_store, utc_ts
from agent_log_server.host_routes import HostRoutes, HostRoutesDeps, HostRoutesState
from agent_log_server.index import create_app_index
from agent_log_server.page_routes import PageRoutesDeps, register_page_routes
from agent_log_server.te2_sync import Te2SyncHelpers
from agent_log_server.ask_user_interactions import (
    AGENT_PTY_ASK_USER_REQUEST_METHOD as _AGENT_PTY_ASK_USER_REQUEST_METHOD,
)
from agent_log_server.conversations_rpc_contract import (
    conversation_rpc_notification_method as _conversation_rpc_notification_method,
)
from agent_log_server.settings_ui_rpc_contract import (
    SETTINGS_RPC_NAMESPACE,
    UI_RPC_NAMESPACE,
    settings_rpc_notification_method as _settings_rpc_notification_method,
    ui_rpc_notification_method as _ui_rpc_notification_method,
)
from agent_log_server.transcript_card_metadata import (
    normalize_live_transcript_event,
    normalize_transcript_card_record,
    transcript_card_id,
    transcript_card_reservation_key,
    transcript_order_id,
)
from agent_log_server.ipc_auth import load_or_create_ipc_secret
from agent_log_server.te2_mcp_config import (
    te2_mcp_integration_enabled,
)
from agent_log_server import pending_context as _pending_ctx
from agent_log_server.typing_helpers import ObjectMap, RequestId

@asynccontextmanager
async def _lifespan(app: FastAPI):
    warmup_task: Optional[asyncio.Task] = None
    try:
        if not ext_loader.is_initialized():
            _extension_api.init_extensions()
        _sync_te2_console_bridge_cache()
        _sync_te2_fws_readme_cache()
        _sync_te2_proxy_shell_readme_cache()
        try:
            _sync_codex_te2_mcp_from_app_config()
        except Exception as e:
            print(f"[Startup] Codex TE2 MCP config sync error: {e}")
        try:
            await _refresh_extension_runtime_state()
        except Exception as e:
            print(f"[Startup] Extension dependency sync error: {e}")
        # DEPRECATED: Legacy builtin codex app-server auto-start is disabled.
        # All codex conversations should use codex-ext or codex-ext-exp extensions,
        # which manage their own runtime through the generic extension transport.
        # Warm up extensions in background (SDK client start, model listing, etc.)
        # Don't block server startup - first message will wait if needed
        async def _warmup_background():
            try:
                results = await ext_loader.warm_up_extensions(timeout=60.0)
                for ext_id, ready in results.items():
                    print(f"[Startup] Extension {ext_id}: {'ready' if ready else 'FAILED'}")
            except Exception as e:
                print(f"[Startup] Extension warm-up error: {e}")
        warmup_task = asyncio.create_task(_warmup_background(), name="extension-warmup")
        # Try to connect to TE2 sidebar IPC on startup (non-blocking)
        async def _sidebar_connect_background():
            try:
                sio = await _host_routes.get_sidebar_sio() if _host_routes is not None else None
                if sio:
                    print("[Startup] Sidebar IPC connected")
                else:
                    print("[Startup] Sidebar IPC not available (will retry on first open)")
            except Exception as e:
                print(f"[Startup] Sidebar IPC connect error: {e}")
        asyncio.create_task(_sidebar_connect_background(), name="sidebar-ipc-connect")
        # Start pending-context watcher registry (tracks repo-local .repo_memory.md
        # files for eligible conversations and watches each file independently).
        try:
            await _pending_ctx.start_watcher(
                _load_conversation_meta,
                _conversation_ids_from_disk,
            )
        except Exception as e:
            print(f"[Startup] Pending-context watcher error: {e}")
    except Exception:
        pass
    yield
    if warmup_task:
        warmup_task.cancel()
        with suppress(asyncio.CancelledError):
            await warmup_task
    # Cleanup extensions
    for ext_id in ext_loader.list_extensions():
        if not isinstance(ext_id, dict):
            continue
        extension_id = ext_id.get("id")
        if not isinstance(extension_id, str) or not extension_id:
            continue
        with suppress(Exception):
            await ext_loader.shutdown_extension(extension_id)
    with suppress(Exception):
        _pending_ctx.stop_watcher()

_APP_INDEX = create_app_index(lifespan=_lifespan)
app = _APP_INDEX.app
socketio_server = _APP_INDEX.socketio_server
socketio_app = _APP_INDEX.socketio_app
PACKAGE_ROOT = Path(__file__).resolve().parent
_agent_log = AgentLogSubsystem(socketio_server=socketio_server, utc_ts=utc_ts)

# Host-provided UI hints are runtime-only (not persisted). These are meant for iframe/drawer integration.
_host_routes_state = HostRoutesState()
_appserver_routes_state = AppserverRoutesState()
_host_routes: Optional[HostRoutes] = None
_IPC_NAMESPACE = "/ipc"
_IPC_SIDS: set[str] = set()


def _ipc_error(msg: object) -> ObjectMap:
    return {"ok": False, "error": str(msg)}


async def _ipc_emit(event_name: str, payload: ObjectMap, sid: Optional[str] = None) -> None:
    if sid:
        await socketio_server.emit(event_name, payload, namespace=_IPC_NAMESPACE, to=sid)
        return
    await socketio_server.emit(event_name, payload, namespace=_IPC_NAMESPACE)


def _pending_approval_turn_id(descriptor: ObjectMap) -> str:
    render_event_raw = descriptor.get("render_event")
    render_event = _coerce_json_object(render_event_raw)
    return str(descriptor.get("turn_id") or render_event.get("turn_id") or "").strip()


def _ipc_refuse(reason: str) -> NoReturn:
    exceptions_mod = getattr(socketio, "exceptions", None)
    exc_type = getattr(exceptions_mod, "ConnectionRefusedError", RuntimeError)
    raise exc_type(reason)


def _iter_pending_approvals(
    *,
    request_method: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    request_id: RequestId = None,
) -> list[tuple[str, str, ObjectMap]]:
    request_method_text = str(request_method or "").strip().lower()
    request_id_text = str(request_id or "").strip()
    turn_id_text = str(turn_id or "").strip()
    conversation_id_text = str(conversation_id or "").strip()
    if conversation_id_text:
        conversation_id_text = _sanitize_conversation_id(conversation_id_text)
    conversation_ids = [conversation_id_text] if conversation_id_text else _conversation_ids_from_disk()

    matches: list[tuple[str, str, ObjectMap]] = []
    seen_conversations: set[str] = set()
    for candidate_conversation_id in conversation_ids:
        conversation_id_value = _sanitize_conversation_id(str(candidate_conversation_id or "").strip())
        if (
            not conversation_id_value
            or conversation_id_value in seen_conversations
            or not _conversation_meta_path(conversation_id_value).exists()
        ):
            continue
        seen_conversations.add(conversation_id_value)
        meta = _load_conversation_meta(conversation_id_value)
        pending = _ensure_pending_approvals(meta)
        for raw_request_id, descriptor in list(pending.items()):
            descriptor_request_id = str(raw_request_id or "").strip()
            if not descriptor_request_id or not isinstance(descriptor, dict):
                continue
            if request_id_text and descriptor_request_id != request_id_text:
                continue
            descriptor_method = str(descriptor.get("request_method") or "").strip().lower()
            if request_method_text and descriptor_method != request_method_text:
                continue
            if turn_id_text and _pending_approval_turn_id(descriptor) != turn_id_text:
                continue
            matches.append((conversation_id_value, descriptor_request_id, descriptor))
    return matches


def _find_pending_approval(request_id: RequestId) -> tuple[str, ObjectMap] | None:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        return None
    matches = _iter_pending_approvals(request_id=request_id_text)
    if not matches:
        return None
    conversation_id, _, descriptor = matches[0]
    return conversation_id, descriptor


def _record_pending_approval_submission(
    conversation_id: str,
    request_id: RequestId,
    resolution: ObjectMap,
) -> ObjectMap | None:
    conversation_id_text = _sanitize_conversation_id(str(conversation_id or "").strip())
    request_id_text = str(request_id or "").strip()
    if not conversation_id_text or not request_id_text or not _conversation_meta_path(conversation_id_text).exists():
        return None
    meta = _load_conversation_meta(conversation_id_text)
    pending = _ensure_pending_approvals(meta)
    descriptor = pending.get(request_id_text)
    if not isinstance(descriptor, dict):
        return None
    updated = dict(descriptor)
    updated["submitted_resolution"] = dict(resolution) if isinstance(resolution, dict) else {}
    updated["submitted_at"] = utc_ts()
    pending[request_id_text] = updated
    meta["pending_approvals"] = pending
    _save_conversation_meta(conversation_id_text, meta)
    return updated


async def _ipc_connect(sid: str, environ: object, auth: object = None):
    provided = auth.get("secret") if isinstance(auth, dict) else None
    expected = load_or_create_ipc_secret()
    if not isinstance(provided, str) or not provided.strip():
        _ipc_refuse("missing secret")
    if not secrets.compare_digest(provided.strip(), expected):
        _ipc_refuse("unauthorized")
    _IPC_SIDS.add(sid)
    return {"ok": True}


async def _ipc_disconnect(sid: str):
    _IPC_SIDS.discard(sid)
    return None


async def _ipc_repo_memory_delta(sid: str, data: object):
    if sid not in _IPC_SIDS:
        return _ipc_error("unauthorized")
    if not isinstance(data, dict):
        return _ipc_error("payload must be an object")

    source_path = data.get("source_path")
    previous_content = data.get("previous_content")
    current_content = data.get("current_content")
    delta_content = data.get("delta_content")
    ts = data.get("ts")

    if not isinstance(source_path, str) or not source_path.strip():
        return _ipc_error("source_path is required")
    if not isinstance(previous_content, str):
        return _ipc_error("previous_content must be a string")
    if not isinstance(current_content, str):
        return _ipc_error("current_content must be a string")
    if delta_content is not None and not isinstance(delta_content, str):
        return _ipc_error("delta_content must be a string when provided")

    try:
        result = _pending_ctx.queue_external_repo_memory_update(
            source_path,
            previous_content=previous_content,
            current_content=current_content,
            delta_content=delta_content if isinstance(delta_content, str) else None,
            ts=float(ts) if isinstance(ts, (int, float)) else None,
        )
    except Exception as exc:
        return _ipc_error(exc)
    return {"ok": True, **result}


async def _ipc_conversation_todo_changed(sid: str, data: object):
    if sid not in _IPC_SIDS:
        return _ipc_error("unauthorized")
    if not isinstance(data, dict):
        return _ipc_error("payload must be an object")

    conversation_id = _sanitize_conversation_id(str(data.get("conversation_id") or "").strip())
    if not conversation_id:
        return _ipc_error("conversation_id is required")
    if not _conversation_meta_path(conversation_id).exists():
        return _ipc_error("conversation not found")

    meta = _load_conversation_meta(conversation_id)
    settings_raw = meta.get("settings") if isinstance(meta, dict) else None
    settings = _coerce_json_object(settings_raw)
    extension_id = str(data.get("extension_id") or settings.get("agent") or "").strip()
    if extension_id != "codex-ext-exp":
        return {"ok": True, "ignored": True, "reason": "not experimental codex", "conversation_id": conversation_id}

    try:
        result = await ext_loader.read_plan(extension_id, conversation_id)
    except Exception as exc:
        return _ipc_error(exc)
    if not isinstance(result, dict):
        return _ipc_error("extension plan result must be an object")

    event = dict(result)
    event["type"] = "plan_state"
    event["conversation_id"] = conversation_id
    event["extension_id"] = extension_id
    await _broadcast_appserver_ui(event)
    return {"ok": True, "conversation_id": conversation_id, "extension_id": extension_id}


async def _ipc_ask_user_ack(sid: str, data: object):
    if sid not in _IPC_SIDS:
        return _ipc_error("unauthorized")
    if not isinstance(data, dict):
        return _ipc_error("payload must be an object")
    request_id = str(
        data.get("request_id")
        or data.get("requestId")
        or data.get("id")
        or ""
    ).strip()
    if not request_id:
        return _ipc_error("request_id is required")
    print(f"[ask_user server] ack request_id={request_id} sid={sid}", flush=True)
    ok = await ask_user_interactions.acknowledge_interaction(request_id)
    if not ok:
        print(f"[ask_user server] ack failed request_id={request_id} sid={sid}", flush=True)
        return _ipc_error("approval is no longer pending")
    print(f"[ask_user server] ack cleared request_id={request_id} sid={sid}", flush=True)
    return {"ok": True, "request_id": request_id}


def _register_ipc_socketio_handlers() -> None:
    socketio_server.on("connect", handler=_ipc_connect, namespace=_IPC_NAMESPACE)
    socketio_server.on("disconnect", handler=_ipc_disconnect, namespace=_IPC_NAMESPACE)
    socketio_server.on("repo_memory_delta", handler=_ipc_repo_memory_delta, namespace=_IPC_NAMESPACE)
    socketio_server.on("conversation_todo_changed", handler=_ipc_conversation_todo_changed, namespace=_IPC_NAMESPACE)
    socketio_server.on("ask_user_ack", handler=_ipc_ask_user_ack, namespace=_IPC_NAMESPACE)


_register_ipc_socketio_handlers()


# ── Socket.IO inbound handlers (mirrors HTTP endpoints) ──────────────
# Each handler returns a value which Socket.IO delivers as the ack callback
# argument on the client.  Errors are returned as {"__error": "..."}.

# --- Config & State ---
_config_lock = asyncio.Lock()
DEBUG_MODE = False
DEBUG_RAW_LOG_PATH: Optional[Path] = None


def _get_debug_mode() -> bool:
    return DEBUG_MODE


def _get_debug_raw_log_path() -> Optional[Path]:
    return DEBUG_RAW_LOG_PATH


def _set_debug_mode(enabled: bool) -> Optional[Path]:
    global DEBUG_MODE, DEBUG_RAW_LOG_PATH
    DEBUG_MODE = enabled
    if enabled and not DEBUG_RAW_LOG_PATH:
        cache_dir = CONFIG_PATH.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        DEBUG_RAW_LOG_PATH = cache_dir / 'debug_raw.jsonl'
        DEBUG_RAW_LOG_PATH.write_text('')
    return DEBUG_RAW_LOG_PATH

# Persist app server config under ~/.cache/app_server.
CONFIG_PATH = conversation_store.config_path
APP_SERVER_DATA_PATH = conversation_store.app_server_data_path
USER_EXTENSIONS_DIR = conversation_store.user_extensions_dir
CODEX_CONFIG_PATH = Path(os.path.expanduser("~/.codex/config.toml"))
LEGACY_TRANSCRIPT_DIR = conversation_store.legacy_transcript_dir
CONVERSATION_DIR = conversation_store.conversations_dir
conversation_store.set_meta_save_callback(_pending_ctx.refresh_conversation)
_default_appserver_config = conversation_store.default_appserver_config
_load_appserver_config = conversation_store.load_appserver_config
_save_appserver_config = conversation_store.save_appserver_config
_normalize_conversation_list = conversation_store.normalize_conversation_list
_add_conversation_to_config = conversation_store.add_conversation_to_config
_remove_conversation_from_config = conversation_store.remove_conversation_from_config
_normalize_pinned_conversation_list = conversation_store.normalize_pinned_conversation_list
_conversation_ids_from_disk = conversation_store.conversation_ids_from_disk
_sync_conversation_index = conversation_store.sync_conversation_index
_conversation_display_order = conversation_store.conversation_display_order
_find_conversation_by_thread_id = conversation_store.find_conversation_by_thread_id
_sanitize_conversation_id = conversation_store.sanitize_conversation_id
_conversation_dir = conversation_store.conversation_dir
_conversation_meta_path = conversation_store.conversation_meta_path
_conversation_transcript_path = conversation_store.conversation_transcript_path
_default_conversation_meta = conversation_store.default_conversation_meta
_load_conversation_meta = conversation_store.load_conversation_meta
_save_conversation_meta = conversation_store.save_conversation_meta
_latest_legacy_transcript = conversation_store.latest_legacy_transcript
_te2_sync_helpers = Te2SyncHelpers(
    package_root=PACKAGE_ROOT,
    config_path=CONFIG_PATH,
    codex_config_path=CODEX_CONFIG_PATH,
    te2_base_url=_host_routes_state.te2_base_url,
    load_appserver_config=_load_appserver_config,
)
_sync_te2_console_bridge_cache = _te2_sync_helpers.sync_te2_console_bridge_cache
_sync_te2_fws_readme_cache = _te2_sync_helpers.sync_te2_fws_readme_cache
_sync_te2_proxy_shell_readme_cache = _te2_sync_helpers.sync_te2_proxy_shell_readme_cache
_write_codex_te2_mcp_config = _te2_sync_helpers.write_codex_te2_mcp_config
_sync_codex_te2_mcp_from_app_config = _te2_sync_helpers.sync_codex_te2_mcp_from_app_config
_transcript_lock = asyncio.Lock()
_transcript_seen: set[tuple[str, str, str]] = set()
_transcript_next_order: dict[str, int] = {}
_transcript_live_order_reservations: dict[str, dict[str, int]] = {}


def _scan_next_transcript_order_id(conversation_id: str) -> int:
    path = _transcript_path(conversation_id)
    if not path.exists():
        return 0
    next_order = 0
    fallback_order = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record_raw = cast(object, json.loads(line))
                except json.JSONDecodeError:
                    continue
                if not isinstance(record_raw, dict):
                    continue
                record = normalize_transcript_card_record(record_raw, conversation_id=conversation_id)
                order_id = transcript_order_id(record.get("order_id"))
                if order_id is None:
                    order_id = fallback_order
                fallback_order = max(fallback_order + 1, order_id + 1)
                next_order = max(next_order, order_id + 1)
    except Exception:
        return 0
    return next_order


def _load_next_transcript_order_id(conversation_id: str) -> int:
    cached = _transcript_next_order.get(conversation_id)
    if isinstance(cached, int) and cached >= 0:
        return cached
    meta = _load_conversation_meta(conversation_id)
    next_order = transcript_order_id(meta.get("next_transcript_order_id")) if isinstance(meta, dict) else None
    if next_order is None:
        next_order = _scan_next_transcript_order_id(conversation_id)
    _transcript_next_order[conversation_id] = next_order
    if isinstance(meta, dict) and transcript_order_id(meta.get("next_transcript_order_id")) != next_order:
        meta["next_transcript_order_id"] = next_order
        _save_conversation_meta(conversation_id, meta)
    return next_order


def _set_next_transcript_order_id(conversation_id: str, next_order: int) -> None:
    normalized_next = max(0, int(next_order))
    _transcript_next_order[conversation_id] = normalized_next
    meta = _load_conversation_meta(conversation_id)
    if transcript_order_id(meta.get("next_transcript_order_id")) == normalized_next:
        return
    meta["next_transcript_order_id"] = normalized_next
    _save_conversation_meta(conversation_id, meta)


def _reserve_live_transcript_order_id(conversation_id: str, event: ObjectMap) -> int | None:
    reservation_key = transcript_card_reservation_key(event)
    if not reservation_key:
        return None
    conversation_reservations = _transcript_live_order_reservations.setdefault(conversation_id, {})
    existing_order = conversation_reservations.get(reservation_key)
    if isinstance(existing_order, int) and existing_order >= 0:
        return existing_order
    next_order = _load_next_transcript_order_id(conversation_id)
    conversation_reservations[reservation_key] = next_order
    _set_next_transcript_order_id(conversation_id, next_order + 1)
    return next_order


def _pop_reserved_live_transcript_order_id(conversation_id: str, record: ObjectMap) -> int | None:
    reservation_key = transcript_card_reservation_key(record)
    if not reservation_key:
        return None
    conversation_reservations = _transcript_live_order_reservations.get(conversation_id)
    if not conversation_reservations:
        return None
    reserved_order = conversation_reservations.pop(reservation_key, None)
    if not conversation_reservations:
        _transcript_live_order_reservations.pop(conversation_id, None)
    if isinstance(reserved_order, int) and reserved_order >= 0:
        return reserved_order
    return None


async def _require_conversation_id(*, create_if_missing: bool = True) -> str:
    convo_id = await _ensure_conversation(create_if_missing=create_if_missing)
    if not convo_id:
        raise RuntimeError("Conversation not initialized")
    return convo_id


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
def _coerce_json_object(value: object) -> ObjectMap:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


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


def _logical_absolute_path(raw_path: Optional[str], fallback: str = "~") -> Path:
    raw = raw_path if isinstance(raw_path, str) and raw_path.strip() else fallback
    expanded = os.path.expanduser(raw)
    return Path(os.path.abspath(expanded))


def _resolved_existing_path(logical_path: Path, fallback: Optional[Path] = None) -> Path:
    try:
        return logical_path.resolve()
    except Exception:
        return fallback or logical_path


def _logical_alias_for_resolved_ancestor(
    logical_base: Path,
    resolved_base: Path,
    resolved_ancestor: Path,
) -> Optional[Path]:
    try:
        rel_from_ancestor = resolved_base.relative_to(resolved_ancestor)
    except ValueError:
        return None
    candidate = logical_base
    for _ in rel_from_ancestor.parts:
        candidate = candidate.parent
    try:
        if candidate.resolve() == resolved_ancestor:
            return candidate
    except Exception:
        return None
    return None


def _rg_list_files(root: Path) -> List[str]:
    result = subprocess.run(
        ["rg", "--files", "--glob", "!.git/*"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in result.stdout.splitlines() if line]


def _meta_settings(meta: ObjectMap | None) -> ObjectMap:
    if not isinstance(meta, dict):
        return {}
    return _coerce_json_object(meta.get("settings"))


def _ensure_pending_approvals(meta: ObjectMap) -> dict[str, ObjectMap]:
    pending = meta.get("pending_approvals")
    if not isinstance(pending, dict):
        pending = {}
        meta["pending_approvals"] = pending
        return pending
    normalized: dict[str, ObjectMap] = {}
    changed = False
    for raw_request_id, descriptor in pending.items():
        request_id = str(raw_request_id or "").strip()
        if not request_id or not isinstance(descriptor, dict):
            changed = True
            continue
        normalized[request_id] = descriptor
    if changed or len(normalized) != len(pending):
        meta["pending_approvals"] = normalized
        return normalized
    return pending


def _codex_runtime_instance_id(meta: ObjectMap | None = None) -> Optional[str]:
    settings = _meta_settings(meta)
    value = settings.get("appserver_shell_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_pending_approval_descriptor(
    conversation_id: str,
    request_id: RequestId,
    *,
    agent: Optional[str] = None,
    kind: Optional[str] = None,
    request_method: Optional[str] = None,
    request_params: ObjectMap | None = None,
    payload: ObjectMap | None = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    runtime_signature: Optional[str] = None,
    runtime_instance_id: Optional[str] = None,
    transcript_anchor: ObjectMap | None = None,
    source: str = "live",
    created_at: Optional[str] = None,
    render_event: ObjectMap | None = None,
    meta: ObjectMap | None = None,
) -> ObjectMap | None:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        return None
    meta_dict = meta if isinstance(meta, dict) else _load_conversation_meta(conversation_id)
    if not isinstance(meta_dict, dict):
        meta_dict = _default_conversation_meta(conversation_id)
    settings = _meta_settings(meta_dict)
    agent_id = str(agent or settings.get("agent") or "codex").strip() or "codex"
    resolved_thread_id = thread_id if thread_id is not None else meta_dict.get("thread_id")
    if runtime_signature is None and agent_id == "codex":
        current_sig = meta_dict.get("thread_runtime_signature")
        runtime_signature = str(current_sig).strip() if isinstance(current_sig, str) and current_sig.strip() else None
    if runtime_instance_id is None and agent_id == "codex":
        runtime_instance_id = _codex_runtime_instance_id(meta_dict)
    anchor = _coerce_json_object(transcript_anchor)
    if turn_id and not anchor.get("turn_id"):
        anchor["turn_id"] = turn_id
    created_at_value = str(created_at or "").strip()
    if not created_at_value:
        created_at_value = datetime.now(timezone.utc).isoformat()
    resolved_kind = str(kind or "unknown")
    resolved_request_method = str(request_method or "").strip() or None
    resolved_request_params = _coerce_json_object(request_params)
    resolved_payload = _coerce_json_object(payload)
    normalized_render_event: ObjectMap
    if isinstance(render_event, dict):
        normalized_render_event = _coerce_json_object(render_event)
    else:
        normalized_render_event = {
            "type": "approval",
            "conversation_id": conversation_id,
            "id": request_id_text,
            "request_id": request_id_text,
            "kind": resolved_kind,
            "payload": resolved_payload,
            "turn_id": turn_id,
        }
    normalized_render_event["type"] = "approval"
    normalized_render_event["conversation_id"] = normalized_render_event.get("conversation_id") or conversation_id
    normalized_render_event["id"] = normalized_render_event.get("id") or request_id_text
    normalized_render_event["request_id"] = normalized_render_event.get("request_id") or request_id_text
    normalized_render_event["kind"] = normalized_render_event.get("kind") or resolved_kind
    normalized_render_event["request_method"] = normalized_render_event.get("request_method") or resolved_request_method
    render_request_params = normalized_render_event.get("request_params")
    normalized_render_event["request_params"] = (
        _coerce_json_object(render_request_params)
        if isinstance(render_request_params, dict)
        else dict(resolved_request_params)
    )
    render_payload = normalized_render_event.get("payload")
    normalized_render_event["payload"] = (
        _coerce_json_object(render_payload)
        if isinstance(render_payload, dict)
        else dict(resolved_payload)
    )
    normalized_render_event["turn_id"] = normalized_render_event.get("turn_id") or turn_id
    normalized_render_event["created_at"] = str(normalized_render_event.get("created_at") or created_at_value)
    if (
        resolved_request_method == _AGENT_PTY_ASK_USER_REQUEST_METHOD
        and "order_id" not in normalized_render_event
        and "orderId" not in normalized_render_event
    ):
        normalized_render_event["order_id"] = -1
    return {
        "request_id": request_id_text,
        "agent": agent_id,
        "kind": resolved_kind,
        "request_method": resolved_request_method,
        "request_params": resolved_request_params,
        "status": "pending",
        "payload": resolved_payload,
        "conversation_id": conversation_id,
        "thread_id": resolved_thread_id,
        "turn_id": turn_id,
        "runtime_signature": runtime_signature,
        "runtime_instance_id": runtime_instance_id,
        "transcript_anchor": anchor,
        "source": source or "live",
        "created_at": created_at_value,
        "render_event": normalized_render_event,
    }


def _upsert_pending_approval(conversation_id: str, descriptor: ObjectMap) -> ObjectMap | None:
    if not isinstance(descriptor, dict):
        return None
    request_id = str(descriptor.get("request_id") or descriptor.get("id") or "").strip()
    if not request_id:
        return None
    meta = _load_conversation_meta(conversation_id)
    pending = _ensure_pending_approvals(meta)
    existing = _coerce_json_object(pending.get(request_id))
    descriptor_request_params = descriptor.get("request_params")
    existing_request_params = existing.get("request_params")
    request_params = (
        _coerce_json_object(descriptor_request_params)
        if isinstance(descriptor_request_params, dict)
        else _coerce_json_object(existing_request_params)
    )
    descriptor_payload = descriptor.get("payload")
    existing_payload = existing.get("payload")
    payload = (
        _coerce_json_object(descriptor_payload)
        if isinstance(descriptor_payload, dict)
        else _coerce_json_object(existing_payload)
    )
    descriptor_transcript_anchor = descriptor.get("transcript_anchor")
    descriptor_render_event = descriptor.get("render_event")
    existing_render_event = existing.get("render_event")
    agent_value = descriptor.get("agent")
    agent = agent_value if isinstance(agent_value, str) and agent_value.strip() else None
    kind_value = descriptor.get("kind")
    kind = kind_value if isinstance(kind_value, str) and kind_value.strip() else None
    request_method_value = descriptor.get("request_method") or existing.get("request_method")
    request_method = (
        request_method_value
        if isinstance(request_method_value, str) and request_method_value.strip()
        else None
    )
    thread_id_value = descriptor.get("thread_id")
    thread_id = thread_id_value if isinstance(thread_id_value, str) and thread_id_value.strip() else None
    turn_id_value = descriptor.get("turn_id")
    turn_id = turn_id_value if isinstance(turn_id_value, str) and turn_id_value.strip() else None
    runtime_signature_value = descriptor.get("runtime_signature")
    runtime_signature = (
        runtime_signature_value
        if isinstance(runtime_signature_value, str) and runtime_signature_value.strip()
        else None
    )
    runtime_instance_id_value = descriptor.get("runtime_instance_id")
    runtime_instance_id = (
        runtime_instance_id_value
        if isinstance(runtime_instance_id_value, str) and runtime_instance_id_value.strip()
        else None
    )
    created_at_value = descriptor.get("created_at") or existing.get("created_at")
    created_at = created_at_value if isinstance(created_at_value, str) and created_at_value.strip() else None
    normalized = _build_pending_approval_descriptor(
        conversation_id,
        request_id,
        agent=agent,
        kind=kind,
        request_method=request_method,
        request_params=request_params,
        payload=payload,
        thread_id=thread_id,
        turn_id=turn_id,
        runtime_signature=runtime_signature,
        runtime_instance_id=runtime_instance_id,
        transcript_anchor=_coerce_json_object(descriptor_transcript_anchor),
        source=str(descriptor.get("source") or existing.get("source") or "live"),
        created_at=created_at,
        render_event=(
            _coerce_json_object(descriptor_render_event)
            if isinstance(descriptor_render_event, dict)
            else _coerce_json_object(existing_render_event)
        ),
        meta=meta,
    )
    if not normalized:
        return None
    normalized["status"] = "pending"
    normalized["created_at"] = existing.get("created_at") or descriptor.get("created_at") or utc_ts()
    normalized["updated_at"] = utc_ts()
    pending[request_id] = normalized
    meta["pending_approvals"] = pending
    _save_conversation_meta(conversation_id, meta)
    return normalized


def _approval_status_from_resolution(resolution: object) -> str:
    result = resolution if isinstance(resolution, dict) else {}
    decision = str(result.get("decision") or "").strip().lower()
    if decision == "decline":
        return "declined"
    if decision == "cancel":
        return "cancelled"
    action = str(result.get("action") or "").strip().lower()
    if action == "decline":
        return "declined"
    if action == "cancel":
        return "cancelled"
    if result.get("success") is False:
        return "declined"
    return "accepted"


def _next_ask_user_msg_id(conversation_id: str) -> int:
    meta = _load_conversation_meta(conversation_id)
    counter_value = meta.get("ask_user_msg_counter")
    if isinstance(counter_value, int):
        counter = counter_value
    elif isinstance(counter_value, str):
        try:
            counter = int(counter_value)
        except ValueError:
            counter = 0
    else:
        counter = 0
    meta["ask_user_msg_counter"] = counter + 1
    _save_conversation_meta(conversation_id, meta)
    return counter


def _build_approval_handoff_event(
    conversation_id: str,
    descriptor: ObjectMap,
    resolution: ObjectMap,
) -> ObjectMap | None:
    if not isinstance(descriptor, dict):
        return None
    request_id_text = str(descriptor.get("request_id") or descriptor.get("id") or "").strip()
    if not request_id_text:
        return None
    render_event = _coerce_json_object(descriptor.get("render_event"))
    payload = _coerce_json_object(descriptor.get("payload"))
    request_params = _coerce_json_object(descriptor.get("request_params"))
    transcript_anchor = _coerce_json_object(descriptor.get("transcript_anchor"))
    turn_id = descriptor.get("turn_id") or transcript_anchor.get("turn_id") or render_event.get("turn_id") or ""
    card_id = (
        str(render_event.get("card_id") or descriptor.get("card_id") or "").strip()
        or None
    )
    is_ask_user = (
        str(descriptor.get("request_method") or render_event.get("request_method") or "").strip()
        == _AGENT_PTY_ASK_USER_REQUEST_METHOD
    )
    ask_user_msg_id = _next_ask_user_msg_id(conversation_id) if is_ask_user else None
    return {
        **render_event,
        "type": "approval_handoff",
        "conversation_id": conversation_id,
        "id": render_event.get("id") or request_id_text,
        "request_id": render_event.get("request_id") or request_id_text,
        "kind": render_event.get("kind") or descriptor.get("kind") or "unknown",
        "request_method": render_event.get("request_method") or descriptor.get("request_method"),
        "request_params": (
            _coerce_json_object(render_event.get("request_params"))
            if isinstance(render_event.get("request_params"), dict)
            else dict(request_params)
        ),
        "payload": (
            _coerce_json_object(render_event.get("payload"))
            if isinstance(render_event.get("payload"), dict)
            else dict(payload)
        ),
        "turn_id": render_event.get("turn_id") or turn_id,
        "created_at": str(render_event.get("created_at") or descriptor.get("created_at") or utc_ts()),
        "card_id": card_id,
        "ask_user_msg_id": ask_user_msg_id,
        "status": _approval_status_from_resolution(resolution),
        "decision": resolution.get("decision"),
        "result": dict(resolution),
        "resolved_at": utc_ts(),
    }


async def _append_approval_handoff_transcript_entry(
    conversation_id: str,
    handoff_event: ObjectMap,
) -> ObjectMap | None:
    payload = _coerce_json_object(handoff_event.get("payload"))
    card_id = str(handoff_event.get("card_id") or "").strip() or None
    request_id = handoff_event.get("request_id", handoff_event.get("id"))
    item_id = card_id or request_id
    return await _append_transcript_entry(conversation_id, {
        "role": "approval",
        "status": handoff_event.get("status"),
        "decision": handoff_event.get("decision"),
        "result": _coerce_json_object(handoff_event.get("result")) or None,
        "request_method": handoff_event.get("request_method"),
        "payload": payload,
        "diff": handoff_event.get("diff") or payload.get("diff"),
        "path": handoff_event.get("path") or payload.get("path"),
        "request_id": request_id,
        "item_id": item_id,
        "card_id": card_id,
        "ask_user_msg_id": handoff_event.get("ask_user_msg_id"),
        "turn_id": handoff_event.get("turn_id"),
        "event": "approval_decision",
    })


def _remove_pending_approval(conversation_id: str, request_id: RequestId) -> bool:
    request_id_text = str(request_id or "").strip()
    if not request_id_text or not _conversation_meta_path(conversation_id).exists():
        return False
    meta = _load_conversation_meta(conversation_id)
    pending = _ensure_pending_approvals(meta)
    if request_id_text not in pending:
        return False
    pending.pop(request_id_text, None)
    meta["pending_approvals"] = pending
    _save_conversation_meta(conversation_id, meta)
    return True


def _legacy_builtin_codex_disabled_detail() -> str:
    return (
        "Legacy builtin Codex runtime is disabled. "
        "Use codex-ext or codex-ext-exp through the generic extension path."
    )


def _legacy_builtin_codex_disabled_result(**extra: object) -> ObjectMap:
    result: ObjectMap = {
        "ok": False,
        "legacy_disabled": True,
        "error": _legacy_builtin_codex_disabled_detail(),
    }
    result.update(extra)
    return result


async def _validate_pending_approval_descriptor(
    conversation_id: str,
    request_id: str,
    descriptor: ObjectMap,
    *,
    meta: ObjectMap | None = None,
) -> bool:
    if not isinstance(descriptor, dict):
        return False
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        return False
    meta_dict = meta if isinstance(meta, dict) else _load_conversation_meta(conversation_id)
    if descriptor.get("conversation_id") and descriptor.get("conversation_id") != conversation_id:
        return False
    pending_thread_id = descriptor.get("thread_id")
    current_thread_id = meta_dict.get("thread_id")
    if pending_thread_id and current_thread_id and pending_thread_id != current_thread_id:
        return False
    settings = _meta_settings(meta_dict)
    agent = str(descriptor.get("agent") or settings.get("agent") or "codex").strip() or "codex"
    if agent == "codex":
        # Builtin Codex runtime no longer exists; any surviving pending approval is stale.
        return False
    if ext_loader.has_extension(agent):
        try:
            return bool(ext_loader.validate_pending_approval(agent, conversation_id, request_id_text, descriptor))
        except Exception:
            return False
    return False


async def _validate_conversation_pending_approvals(
    conversation_id: str,
    meta: ObjectMap | None = None,
) -> ObjectMap:
    if meta is None:
        meta = _load_conversation_meta(conversation_id)
    if not isinstance(meta, dict):
        meta = _default_conversation_meta(conversation_id)
    pending = _ensure_pending_approvals(meta)
    if not pending:
        meta["pending_approvals"] = {}
        return meta
    valid: dict[str, ObjectMap] = {}
    changed = False
    for raw_request_id, descriptor in list(pending.items()):
        request_id = str(raw_request_id or "").strip()
        if not request_id or not isinstance(descriptor, dict):
            changed = True
            continue
        ok = await _validate_pending_approval_descriptor(
            conversation_id,
            request_id,
            descriptor,
            meta=meta,
        )
        if ok:
            normalized = dict(descriptor)
            normalized["request_id"] = request_id
            valid[request_id] = normalized
        else:
            changed = True
    if changed or valid != pending:
        meta["pending_approvals"] = valid
        _save_conversation_meta(conversation_id, meta)
    else:
        meta["pending_approvals"] = valid
    return meta


_PREVIEW_TEXT_MAX = 160


def _normalize_preview_text(text: object, max_len: int = _PREVIEW_TEXT_MAX) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = _ansi_strip(text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_len > 1 and len(text) > max_len:
        text = f"{text[:max_len - 1].rstrip()}…"
    return text


def _preview_tool_label(event: ObjectMap) -> str:
    server = _normalize_preview_text(event.get("server"), 64)
    tool = _normalize_preview_text(event.get("tool"), 64)
    if server and tool:
        return f"{server}:{tool}"
    return tool or server or "tool"


def _preview_from_event(event: ObjectMap) -> dict[str, str] | None:
    if not isinstance(event, dict):
        return None
    evt_type = str(event.get("type") or "").strip().lower()
    if not evt_type:
        return None

    if evt_type == "assistant_finalize":
        text = _normalize_preview_text(event.get("text"))
        return {"type": "assistant", "text": text} if text else None

    if evt_type == "message" and str(event.get("role") or "").strip().lower() == "assistant":
        text = _normalize_preview_text(event.get("text"))
        return {"type": "assistant", "text": text} if text else None

    if evt_type in {"tool_begin", "tool_end"}:
        arguments = _coerce_json_object(event.get("arguments"))
        tool_name = str(event.get("tool") or "").strip().lower()
        if tool_name in {"command", "shell"}:
            command = _normalize_preview_text(arguments.get("command") or event.get("command"))
            return {"type": "tool", "text": f"$ {command}"} if command else None
        if tool_name == "web_search":
            query = _normalize_preview_text(event.get("query") or arguments.get("query"))
            return {"type": "tool", "text": f"web_search: {query}"} if query else {"type": "tool", "text": "web_search"}
        return {"type": "tool", "text": _preview_tool_label(event)}

    if evt_type in {"shell_begin", "shell_end", "command_result"}:
        command = _normalize_preview_text(event.get("command"))
        if command:
            return {"type": "tool", "text": f"$ {command}"}
        output = _normalize_preview_text(event.get("output") or event.get("stdout") or event.get("stderr"))
        return {"type": "tool", "text": output} if output else None

    if evt_type == "subagent_start":
        name = _normalize_preview_text(event.get("name") or "subagent", 48)
        intent = _normalize_preview_text(event.get("intent") or "working", 120)
        return {"type": "subagent", "text": f"{name}: {intent}"}

    if evt_type == "subagent_end":
        summary = _normalize_preview_text(event.get("summary"))
        if summary:
            return {"type": "subagent", "text": summary}
        return {"type": "subagent", "text": "subagent failed" if event.get("success") is False else "subagent done"}

    return None


def _store_conversation_preview_from_event(event: ObjectMap) -> None:
    if not isinstance(event, dict):
        return
    if _is_internal_transcript_item(event):
        return
    convo_id = event.get("conversation_id")
    if not isinstance(convo_id, str) or not convo_id.strip():
        return
    convo_id = _sanitize_conversation_id(convo_id.strip())
    if not convo_id or not _conversation_meta_path(convo_id).exists():
        return
    preview = _preview_from_event(event)
    if not preview or not preview.get("text"):
        return
    meta = _load_conversation_meta(convo_id)
    current = _coerce_json_object(meta.get("last_preview"))
    next_preview = {
        "type": preview.get("type") or "event",
        "text": preview["text"],
        "updated_at": utc_ts(),
    }
    if current.get("type") == next_preview["type"] and current.get("text") == next_preview["text"]:
        return
    meta["last_preview"] = next_preview
    _save_conversation_meta(convo_id, meta)


# =============================================================================
# Draft mention envelope tokens
_DRAFT_MENTION_ENVELOPE_START = "\x1eCODEX_MENTION "
_DRAFT_MENTION_ENVELOPE_END = "\x1f"


def _encode_draft_mention_token(
    path: str,
    *,
    line_no: Optional[int] = None,
    end_line_no: Optional[int] = None,
    col: Optional[int] = None,
    end_col: Optional[int] = None,
    content: Optional[str] = None,
) -> str:
    path_text = str(path or "").strip()
    if not path_text:
        return ""
    payload: ObjectMap = {"path": path_text}
    if line_no is not None:
        payload["line"] = int(line_no)
    if end_line_no is not None:
        payload["endLine"] = int(end_line_no)
    if col is not None:
        payload["col"] = int(col)
    if end_col is not None:
        payload["endCol"] = int(end_col)
    if isinstance(content, str) and content:
        payload["content"] = content
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{_DRAFT_MENTION_ENVELOPE_START}{encoded}{_DRAFT_MENTION_ENVELOPE_END}"


def _is_internal_transcript_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    internal = item.get("internal")
    if internal is True:
        return True
    if isinstance(internal, str) and internal.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    visibility = item.get("visibility")
    return isinstance(visibility, str) and visibility.strip().lower() == "internal"


def _coerce_query_bool(value: object) -> bool:
    candidate = getattr(value, "default", value)
    if isinstance(candidate, str):
        return candidate.strip().lower() in {"1", "true", "yes", "on"}
    return bool(candidate)


async def _ensure_conversation(create_if_missing: bool = True) -> Optional[str]:
    async with _config_lock:
        cfg = _load_appserver_config()
        convo_id_value = cfg.get("conversation_id")
        convo_id = convo_id_value if isinstance(convo_id_value, str) and convo_id_value else None

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


async def _get_conversation_meta() -> ObjectMap | None:
    convo_id = await _ensure_conversation(create_if_missing=False)
    if not convo_id:
        return None
    return _load_conversation_meta(convo_id)


async def _update_conversation_meta(patch: ObjectMap) -> ObjectMap:
    convo_id = await _ensure_conversation()
    if not convo_id:
        raise RuntimeError("Conversation not initialized")
    meta = _load_conversation_meta(convo_id)
    meta.update(patch)
    _save_conversation_meta(convo_id, meta)
    return meta


def _apply_conversation_meta_patch(conversation_id: str, patch: ObjectMap) -> ObjectMap | None:
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    if not isinstance(patch, dict):
        return None
    convo_id = _sanitize_conversation_id(conversation_id)
    if not convo_id or not _conversation_meta_path(convo_id).exists():
        return None
    meta = _load_conversation_meta(convo_id)
    changed = False
    for key, value in patch.items():
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            if key in meta:
                meta.pop(key, None)
                changed = True
            continue
        if meta.get(key) != value:
            meta[key] = value
            changed = True
    if changed:
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
    if not convo_id:
        raise RuntimeError("Conversation not initialized")
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


def _set_conversation_turn_id(conversation_id: str, turn_id: Optional[str]) -> None:
    """Persist the current active turn id to the conversation sidecar.

    This allows conversation-scoped interrupt without the frontend knowing thread/turn ids.
    """
    if not conversation_id:
        return
    convo_id = _sanitize_conversation_id(conversation_id)
    if not _conversation_meta_path(convo_id).exists():
        return
    meta = _load_conversation_meta(convo_id)
    if turn_id:
        meta["turn_id"] = turn_id
    else:
        meta.pop("turn_id", None)
    _save_conversation_meta(convo_id, meta)


def _transcript_path(conversation_id: str) -> Path:
    return _conversation_transcript_path(conversation_id)


async def _write_transcript_entries(conversation_id: str, items: list[ObjectMap]) -> None:
    if not conversation_id:
        return
    path = _transcript_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _transcript_lock:
        next_order = 0
        with path.open("w", encoding="utf-8") as f:
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                record = normalize_transcript_card_record(entry, conversation_id=conversation_id)
                if not _is_internal_transcript_item(record):
                    order_id = transcript_order_id(record.get("order_id"))
                    if order_id is None:
                        order_id = next_order
                    next_order = max(next_order, order_id + 1)
                    record = normalize_transcript_card_record(
                        record,
                        conversation_id=conversation_id,
                        fallback_order_id=order_id,
                    )
                record = {"ts": utc_ts(), **record}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _transcript_live_order_reservations.pop(conversation_id, None)
        _set_next_transcript_order_id(conversation_id, next_order)


async def _append_transcript_entry(conversation_id: str, entry: ObjectMap) -> ObjectMap | None:
    if not conversation_id:
        return None
    path = _transcript_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with _transcript_lock:
        record = normalize_transcript_card_record(entry, conversation_id=conversation_id)
        item_id = record.get("item_id")
        role = record.get("role")
        if item_id and role:
            key = (conversation_id, str(item_id), str(role))
            if key in _transcript_seen:
                return
            _transcript_seen.add(key)
        if not _is_internal_transcript_item(record):
            current_next_order = _load_next_transcript_order_id(conversation_id)
            order_id = transcript_order_id(record.get("order_id"))
            reserved_order = _pop_reserved_live_transcript_order_id(conversation_id, record)
            if reserved_order is not None and not str(record.get("card_id") or "").strip():
                reserved_card_id = transcript_card_id(record)
                if reserved_card_id:
                    record["card_id"] = reserved_card_id
            if order_id is None and reserved_order is not None:
                order_id = reserved_order
            if order_id is None:
                order_id = current_next_order
            record = normalize_transcript_card_record(
                record,
                conversation_id=conversation_id,
                fallback_order_id=order_id,
            )
            _set_next_transcript_order_id(conversation_id, max(current_next_order, order_id + 1))
        record = {"ts": utc_ts(), **record}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _ensure_framework_shells_secret() -> None:
    """Derive a stable secret from cwd/repo root if not already set."""
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    cfg = _load_appserver_config()
    repo_root_value = cfg.get("cwd")
    repo_root = repo_root_value if isinstance(repo_root_value, str) and repo_root_value.strip() else str(Path.cwd())
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


async def _broadcast_appserver_ui(event: ObjectMap) -> None:
    """Broadcast an event to all connected frontends via Socket.IO."""
    if not isinstance(event, dict):
        return
    evt = dict(event)
    evt_type = str(evt.get("type") or "").strip().lower()
    conversation_id = str(evt.get("conversation_id") or "").strip()
    if conversation_id:
        async with _transcript_lock:
            if (
                evt_type == "approval"
                and str(evt.get("request_method") or evt.get("requestMethod") or "").strip().lower()
                == _AGENT_PTY_ASK_USER_REQUEST_METHOD
                and "order_id" not in evt
                and "orderId" not in evt
            ):
                evt["order_id"] = -1
            evt = normalize_live_transcript_event(evt, conversation_id=conversation_id)
            if evt_type != "approval" and transcript_order_id(evt.get("order_id")) is None:
                reserved_order = _reserve_live_transcript_order_id(conversation_id, evt)
                if reserved_order is not None:
                    evt = normalize_live_transcript_event(
                        evt,
                        conversation_id=conversation_id,
                        fallback_order_id=reserved_order,
                    )
    _store_conversation_preview_from_event(evt)
    rpc_method = _conversation_rpc_notification_method(evt_type)
    if rpc_method:
        try:
            await socketio_server.emit(
                "rpc.notify",
                {
                    "jsonrpc": "2.0",
                    "method": rpc_method,
                    "params": evt,
                },
                namespace=CONVERSATIONS_RPC_NAMESPACE,
            )
        except Exception:
            pass
    settings_rpc_method = _settings_rpc_notification_method(evt_type)
    if settings_rpc_method:
        try:
            await socketio_server.emit(
                "rpc.notify",
                {
                    "jsonrpc": "2.0",
                    "method": settings_rpc_method,
                    "params": evt,
                },
                namespace=SETTINGS_RPC_NAMESPACE,
            )
        except Exception:
            pass
    ui_rpc_method = _ui_rpc_notification_method(evt_type)
    if ui_rpc_method:
        try:
            await socketio_server.emit(
                "rpc.notify",
                {
                    "jsonrpc": "2.0",
                    "method": ui_rpc_method,
                    "params": evt,
                },
                namespace=UI_RPC_NAMESPACE,
            )
        except Exception:
            pass

    if evt_type == "status":
        turn_status = str(evt.get("turn_status") or "").strip().lower()
        if turn_status in {"interrupted", "failed"}:
            await ask_user_interactions.cancel_interactions(
                conversation_id=evt.get("conversation_id"),
                turn_id=evt.get("turn_id"),
                resolution=(
                    {"status": "interrupted"}
                    if turn_status == "interrupted"
                    else {"status": "error", "error": "turn failed"}
                ),
            )
    if _host_routes is not None:
        await _host_routes.maybe_emit_sidebar_edit(evt)


ask_user_interactions.configure(
    emit_ipc_fn=_ipc_emit,
    find_pending_approval_fn=_find_pending_approval,
    list_pending_approvals_fn=_iter_pending_approvals,
    record_submitted_resolution_fn=_record_pending_approval_submission,
    remove_pending_approval_fn=_remove_pending_approval,
    build_handoff_event_fn=_build_approval_handoff_event,
    append_handoff_fn=_append_approval_handoff_transcript_entry,
    broadcast_ui_fn=_broadcast_appserver_ui,
)


async def _emit_command_result_mirror(
    conversation_id: Optional[str],
    *,
    command: str,
    output: str = "",
    event: Optional[str] = None,
    exit_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
    source: Optional[str] = None,
    cwd: Optional[str] = None,
    prompt: Optional[str] = None,
    agent_block_id: Optional[str] = None,
    shared_fields: ObjectMap | None = None,
) -> None:
    if not conversation_id:
        return
    shared = dict(shared_fields or {})
    transcript_entry: ObjectMap = {
        "role": "command",
        "command": command,
        "output": output,
        **shared,
    }
    if cwd is not None:
        transcript_entry["cwd"] = cwd
    if prompt is not None:
        transcript_entry["prompt"] = prompt
    if agent_block_id is not None:
        transcript_entry["agent_block_id"] = agent_block_id
    if exit_code is not None:
        transcript_entry["exit_code"] = exit_code
    if duration_ms is not None:
        transcript_entry["duration_ms"] = duration_ms
    if source is not None:
        transcript_entry["source"] = source
    if event is not None:
        transcript_entry["event"] = event
    recorded_entry = await _append_transcript_entry(conversation_id, transcript_entry)

    live_event: ObjectMap = {
        "type": "command_result",
        "conversation_id": conversation_id,
        "command": command,
        "output": output,
        **shared,
    }
    if cwd is not None:
        live_event["cwd"] = cwd
    if prompt is not None:
        live_event["prompt"] = prompt
    if agent_block_id is not None:
        live_event["agent_block_id"] = agent_block_id
    if exit_code is not None:
        live_event["exit_code"] = exit_code
    if duration_ms is not None:
        live_event["duration_ms"] = duration_ms
    if source is not None:
        live_event["source"] = source
    if event is not None:
        live_event["event"] = event
    if isinstance(recorded_entry, dict):
        for key in ("nid", "card_id", "order_id"):
            if key in recorded_entry:
                live_event[key] = recorded_entry[key]
    await _broadcast_appserver_ui(live_event)


def _extract_line_from_diff(diff_text: str) -> int:
    """Extract the first changed line number from a unified diff."""
    import re
    m = re.search(r'^@@\s*[+-]\d+(?:,\d+)?\s+\+(\d+)', diff_text, re.MULTILINE)
    return int(m.group(1)) if m else 1


# =============================================================================
# EXTENSION SYSTEM
# =============================================================================
# Dynamic extension loading.


def _materialize_extension_runtime_settings(settings: ObjectMap | None) -> ObjectMap:
    if not isinstance(settings, dict):
        return {}
    merged = dict(settings)
    if te2_mcp_integration_enabled(merged):
        merged["te2_base_url"] = _te2_base_url()
    return merged


def _te2_base_url() -> str:
    return _host_routes_state.te2_base_url()


def _merge_extension_bind_settings(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: ObjectMap | None = None,
) -> ObjectMap:
    merged: ObjectMap = {}
    meta = _load_conversation_meta(conversation_id)
    meta_settings = meta.get("settings") if isinstance(meta, dict) else None
    if isinstance(meta_settings, dict):
        merged.update(_coerce_json_object(meta_settings))
    if isinstance(settings, dict):
        merged.update(settings)
    if isinstance(cwd, str) and cwd.strip():
        merged["cwd"] = cwd
    if isinstance(model, str) and model.strip():
        merged["model"] = model
    return _materialize_extension_runtime_settings(merged)


_extension_api = ExtensionApi(
    ExtensionApiDeps(
        package_root=PACKAGE_ROOT,
        user_extensions_dir=USER_EXTENSIONS_DIR,
        config_lock=_config_lock,
        broadcast_appserver_ui=_broadcast_appserver_ui,
        get_fws_manager=_get_fws_manager,
        append_transcript_entry=_append_transcript_entry,
        load_conversation_meta=_load_conversation_meta,
        save_conversation_meta=_save_conversation_meta,
        upsert_pending_approval=_upsert_pending_approval,
        remove_pending_approval=_remove_pending_approval,
        sanitize_conversation_id=_sanitize_conversation_id,
        conversation_meta_path=_conversation_meta_path,
        load_appserver_config=_load_appserver_config,
        save_appserver_config=_save_appserver_config,
        ensure_conversation=_ensure_conversation,
        merge_extension_bind_settings=_merge_extension_bind_settings,
        write_transcript_entries=_write_transcript_entries,
    )
)


def _normalize_extensions_config_for_store(raw: object) -> dict[str, ObjectMap]:
    normalized = _extension_api.normalize_extensions_config(raw)
    return {
        ext_id: {key: value for key, value in entry.items()}
        for ext_id, entry in normalized.items()
    }


conversation_store.set_extensions_config_normalizer(_normalize_extensions_config_for_store)

_extension_unavailable_detail = _extension_api.extension_unavailable_detail
_emit_extension_unavailable_warning = _extension_api.emit_extension_unavailable_warning
_refresh_extension_runtime_state = _extension_api.refresh_extension_runtime_state

appserver_routes = AppserverRoutes(
    AppserverRoutesDeps(
        config_lock=_config_lock,
        load_appserver_config=_load_appserver_config,
        save_appserver_config=_save_appserver_config,
        sync_conversation_index=_sync_conversation_index,
        normalize_pinned_conversation_list=_normalize_pinned_conversation_list,
        conversation_display_order=_conversation_display_order,
        add_conversation_to_config=_add_conversation_to_config,
        remove_conversation_from_config=_remove_conversation_from_config,
        default_conversation_meta=_default_conversation_meta,
        latest_legacy_transcript=_latest_legacy_transcript,
        require_conversation_id=_require_conversation_id,
        ensure_conversation=_ensure_conversation,
        sanitize_conversation_id=_sanitize_conversation_id,
        conversation_meta_path=_conversation_meta_path,
        conversation_dir=_conversation_dir,
        transcript_path=_transcript_path,
        meta_settings=_meta_settings,
        load_conversation_meta=_load_conversation_meta,
        save_conversation_meta=_save_conversation_meta,
        coerce_json_object=_coerce_json_object,
        validate_conversation_pending_approvals=_validate_conversation_pending_approvals,
        ensure_pending_approvals=_ensure_pending_approvals,
        find_pending_approval=_find_pending_approval,
        remove_pending_approval=_remove_pending_approval,
        build_approval_handoff_event=_build_approval_handoff_event,
        append_approval_handoff_transcript_entry=_append_approval_handoff_transcript_entry,
        append_transcript_entry=_append_transcript_entry,
        write_transcript_entries=_write_transcript_entries,
        is_internal_transcript_item=_is_internal_transcript_item,
        conversation_agent=_extension_api.conversation_agent,
        extension_unavailable_detail=_extension_unavailable_detail,
        emit_extension_unavailable_warning=_emit_extension_unavailable_warning,
        default_active_extension_id=_extension_api.default_active_extension_id,
        materialize_extension_runtime_settings=_materialize_extension_runtime_settings,
        merge_extension_bind_settings=_merge_extension_bind_settings,
        legacy_builtin_codex_disabled_result=_legacy_builtin_codex_disabled_result,
        legacy_builtin_codex_disabled_detail=_legacy_builtin_codex_disabled_detail,
        emit_command_result_mirror=_emit_command_result_mirror,
        broadcast_appserver_ui=_broadcast_appserver_ui,
        logical_absolute_path=_logical_absolute_path,
        resolved_existing_path=_resolved_existing_path,
        logical_alias_for_resolved_ancestor=_logical_alias_for_resolved_ancestor,
        detect_repo_root=_detect_repo_root,
        rg_list_files=_rg_list_files,
        get_host_project_root=lambda: _host_routes_state.project_root,
        utc_ts=utc_ts,
        write_codex_te2_mcp_config=_write_codex_te2_mcp_config,
        get_debug_mode=_get_debug_mode,
        get_debug_raw_log_path=_get_debug_raw_log_path,
        set_debug_mode=_set_debug_mode,
    ),
    state=_appserver_routes_state,
)

register_page_routes(app, PageRoutesDeps(package_root=PACKAGE_ROOT))
register_extension_api_routes(app, _extension_api)
register_appserver_routes(app, appserver_routes)
_agent_log.register_routes(app)

@app.post("/api/shutdown")
async def api_shutdown():
    loop = asyncio.get_event_loop()
    loop.call_later(0.1, os._exit, 0)
    return {"ok": True}


host_routes = HostRoutes(
    HostRoutesDeps(
        config_lock=_config_lock,
        load_appserver_config=_load_appserver_config,
        broadcast_appserver_ui=_broadcast_appserver_ui,
        process_mention=appserver_routes.process_mention,
        load_conversation_meta=_load_conversation_meta,
        meta_settings=_meta_settings,
    ),
    state=_host_routes_state,
)
_host_routes = host_routes
host_routes.register_routes(app)


def _register_appserver_socketio() -> None:
    register_appserver_socketio_handlers(
        socketio_server,
        AppserverSocketioDeps(
            make_appserver_message_in=lambda conversation_id, text: AppserverMessageIn(
                conversation_id=conversation_id,
                text=text,
            ),
            api_appserver_message=appserver_routes.api_appserver_message,
            api_appserver_shell_exec=appserver_routes.api_appserver_shell_exec,
            api_appserver_rpc=appserver_routes.api_appserver_rpc,
            api_conversations_rpc=appserver_routes.api_conversations_rpc,
            api_appserver_interrupt=appserver_routes.api_appserver_interrupt,
            api_appserver_compact=appserver_routes.api_appserver_compact,
            api_appserver_conversation=appserver_routes.api_appserver_conversation,
            api_appserver_conversation_meta=appserver_routes.api_appserver_conversation_meta,
            api_appserver_conversation_update=appserver_routes.api_appserver_conversation_update,
            api_appserver_conversation_draft=appserver_routes.api_appserver_conversation_draft,
            api_appserver_conversations=appserver_routes.api_appserver_conversations,
            api_appserver_conversation_create=appserver_routes.api_appserver_conversation_create,
            api_appserver_conversation_select=appserver_routes.api_appserver_conversation_select,
            api_appserver_conversation_delete=appserver_routes.api_appserver_conversation_delete,
            api_appserver_conversation_pins=appserver_routes.api_appserver_conversation_pins,
            api_appserver_set_view=appserver_routes.api_appserver_set_view,
            api_appserver_config=appserver_routes.api_appserver_config,
            api_appserver_config_update=appserver_routes.api_appserver_config_update,
            api_appserver_models=appserver_routes.api_appserver_models,
            api_appserver_runtime_options=appserver_routes.api_appserver_runtime_options,
            api_appserver_status=appserver_routes.api_appserver_status,
            api_appserver_start=appserver_routes.api_appserver_start,
            api_appserver_stop=appserver_routes.api_appserver_stop,
            api_appserver_initialize=appserver_routes.api_appserver_initialize,
            api_appserver_approval_record=appserver_routes.api_appserver_approval_record,
            api_appserver_approval_response=appserver_routes.api_appserver_approval_response,
            api_appserver_transcript=appserver_routes.api_appserver_transcript,
            api_appserver_transcript_range=appserver_routes.api_appserver_transcript_range,
            api_extensions_list=_extension_api.api_extensions_list,
            api_extension_enabled=_extension_api.api_extension_enabled,
            api_extension_install=_extension_api.api_extension_install,
            api_extensions_validate=_extension_api.api_extensions_validate,
            api_extensions_install_package=_extension_api.api_extensions_install_package,
            api_extension_update_package=_extension_api.api_extension_update_package,
            api_extension_remove_package=_extension_api.api_extension_remove_package,
            api_extensions_reload=_extension_api.api_extensions_reload,
            api_extension_settings_schema=_extension_api.api_extension_settings_schema,
            api_extension_splash_schema=_extension_api.api_extension_splash_schema,
            api_extension_splash_action=_extension_api.api_extension_splash_action,
            api_extension_request_cards=_extension_api.api_extension_request_cards,
            api_extension_plan=_extension_api.api_extension_plan,
            api_fs_list=appserver_routes.api_fs_list,
            api_fs_search=appserver_routes.api_fs_search,
            api_host_ui_get=host_routes.api_host_ui_get,
            api_shutdown=api_shutdown,
            append_record=_agent_log.append_record,
            coerce_query_bool=_coerce_query_bool,
            emit_sidebar_agent_open=host_routes.emit_sidebar_agent_open,
            ensure_conversation=_require_conversation_id,
            merge_extension_bind_settings=_merge_extension_bind_settings,
            read_records=_agent_log.read_records,
            sidebar_recheck_status=host_routes.sidebar_recheck_status,
            utc_ts=utc_ts,
            write_transcript_entries=_write_transcript_entries,
        ),
    )


_register_appserver_socketio()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="agent_chat.log.jsonl")
    p.add_argument("--port", type=int, default=12359)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--broadcast-all", action="store_true", help="Bind to 0.0.0.0 for LAN access")
    subparsers = p.add_subparsers(dest="command")
    _register_extension_subcommands(subparsers)
    return p.parse_args()

def main():
    global DEBUG_MODE
    global DEBUG_RAW_LOG_PATH
    args = parse_args()
    command = getattr(args, "command", None)
    if command == "extension":
        if not ext_loader.is_initialized():
            with redirect_stdout(sys.stderr):
                _extension_api.init_extensions()
        raise SystemExit(_run_extension_command(args))
    debug_raw = getattr(args, "debug", False)
    DEBUG_MODE = bool(debug_raw)
    broadcast_all_raw = getattr(args, "broadcast_all", False)
    broadcast_all = bool(broadcast_all_raw)
    host_raw = getattr(args, "host", "127.0.0.1")
    host = host_raw if isinstance(host_raw, str) and host_raw else "127.0.0.1"
    if broadcast_all:
        host = "0.0.0.0"

    log_raw = getattr(args, "log", "agent_chat.log.jsonl")
    log_path = log_raw if isinstance(log_raw, str) and log_raw else "agent_chat.log.jsonl"
    port_raw = getattr(args, "port", 12359)
    port = port_raw if isinstance(port_raw, int) else 12359

    _agent_log.initialize_log_path(log_path)

    # Set up debug raw log in .cache directory
    if DEBUG_MODE:
        cache_dir = CONFIG_PATH.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        DEBUG_RAW_LOG_PATH = cache_dir / "debug_raw.jsonl"
        # Clear previous debug log on startup
        DEBUG_RAW_LOG_PATH.write_text("")

    uvicorn.run(socketio_app, host=host, port=port)

if __name__ == "__main__":
    main()

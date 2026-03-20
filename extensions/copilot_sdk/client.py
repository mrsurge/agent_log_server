"""
Copilot SDK Client Handler for Extension System

Manages Copilot CLI agent sessions via the vendored Copilot SDK source.
Replaces the ACP client handler with a cleaner, SDK-managed approach.

Key advantages over ACP:
- Session resume built-in (client.resume_session)
- SDK manages CLI process lifecycle (no shellspec/FWS pipe needed)
- Streaming via SessionConfig.streaming=True
- All Copilot models including Gemini
- Rich event model via session.on(handler)
"""

import asyncio
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Awaitable, get_args

from ._vendor.copilot import (
    CopilotClient,
    CopilotSession,
    SessionConfig,
    ResumeSessionConfig,
    MessageOptions,
    SessionEvent,
    PermissionRequest,
    PermissionRequestResult,
)
from ._vendor.copilot.generated.rpc import SessionModeSetParams, Mode as SessionMode
from ._vendor.copilot.types import SessionHooks, PermissionRequestResultKind

from .router import CopilotEventRouter, _looks_like_diff, _FILE_CHANGE_TOOLS
from te2_runtime import (
    build_copilot_mcp_servers,
    build_effective_developer_instructions,
    build_te2_mcp_streamable_http_url,
    TE2_MCP_SERVER_NAME,
    te2_mcp_integration_enabled,
)
from watchfiles import awatch


# ── Global state ────────────────────────────────────────────────────

_client: Optional[CopilotClient] = None
_client_lock: Optional[asyncio.Lock] = None


def _get_client_lock() -> asyncio.Lock:
    """Lazy-init the lock on first use (inside the running event loop)."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock

# Server callbacks (injected by init_copilot_manager)
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[Dict[str, Callable]] = None

# Session tracking: conversation_id -> CopilotSession
_sessions: Dict[str, CopilotSession] = {}
# Router tracking: conversation_id -> CopilotEventRouter
_routers: Dict[str, CopilotEventRouter] = {}
# Event unsubscribe fns: conversation_id -> unsubscribe callable
_unsubs: Dict[str, Callable] = {}
# Runtime signature tracking: conversation_id -> signature of effective session config inputs
_runtime_signatures: Dict[str, str] = {}
# Per-conversation session locks: serialize init/resume/send/destroy per conversation.
_session_locks: Dict[str, asyncio.Lock] = {}
# Live todo watch tasks keyed by conversation_id.
_todo_watch_tasks: Dict[str, asyncio.Task[None]] = {}
_todo_watch_sessions: Dict[str, str] = {}
_todo_signatures: Dict[str, str] = {}
_plan_doc_signatures: Dict[str, str] = {}
# Latest known plan-document state keyed by conversation_id.
_plan_doc_state: Dict[str, Dict[str, Any]] = {}

# Ready state
_ready_event: Optional[asyncio.Event] = None
_initialized: bool = False

# Debug buffer (circular)
_raw_buffer: List[Dict[str, Any]] = []
_RAW_BUFFER_MAX = 2000

_APPROVAL_POLICY_OPTIONS: List[Dict[str, str]] = [
    {"value": "auto-approve", "label": "Auto-Approve"},
    {"value": "suggest", "label": "Suggest (auto-approve on timeout)"},
    {"value": "always-ask", "label": "Always Ask"},
]
_SANDBOX_POLICY_OPTIONS: List[Dict[str, str]] = [
    {"value": "cwd-only", "label": "CWD Only"},
    {"value": "allow-all-paths", "label": "Allow All Paths"},
    {"value": "ask", "label": "Ask"},
]
_WEB_POLICY_OPTIONS: List[Dict[str, str]] = [
    {"value": "deny", "label": "Deny"},
    {"value": "allow", "label": "Allow"},
    {"value": "ask", "label": "Ask"},
]
_MODE_OPTIONS: List[Dict[str, str]] = [
    {"value": "interactive", "label": "Interactive"},
    {"value": "plan", "label": "Plan"},
    {"value": "autopilot", "label": "Autopilot"},
]
_DEFAULT_APPROVAL_POLICY = "suggest"
_DEFAULT_SANDBOX_POLICY = "cwd-only"
_DEFAULT_WEB_POLICY = "deny"
_DEFAULT_MODE = "interactive"
_COPILOT_SESSION_STATE_ROOT = Path.home() / ".copilot" / "session-state"
_COPILOT_TODO_FILENAMES = frozenset({"session.db", "session.db-wal", "session.db-shm"})
_COPILOT_PLAN_FILENAME = "plan.md"
_COPILOT_SESSION_STATE_READ_SETTLE_SECONDS = 0.10


def _add_to_raw_buffer(direction: str, conversation_id: str, data: Any) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dir": direction,
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": data if isinstance(data, str) else str(data)[:500],
    }
    _raw_buffer.append(entry)
    if len(_raw_buffer) > _RAW_BUFFER_MAX:
        _raw_buffer.pop(0)


def get_raw_buffer(limit: int = 50) -> List[Dict[str, Any]]:
    return _raw_buffer[-limit:]


def _get_session_lock(conversation_id: str) -> asyncio.Lock:
    lock = _session_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[conversation_id] = lock
    return lock


def _summarize_runtime_config(config: Dict[str, Any]) -> Dict[str, Any]:
    mcp_servers = config.get("mcp_servers")
    te2_cfg = mcp_servers.get("te2-mcp") if isinstance(mcp_servers, dict) else None
    system_message = config.get("system_message")
    system_content = system_message.get("content") if isinstance(system_message, dict) else ""
    return {
        "model": config.get("model"),
        "working_directory": config.get("working_directory"),
        "config_dir": config.get("config_dir"),
        "reasoning_effort": config.get("reasoning_effort"),
        "streaming": config.get("streaming") is True,
        "has_system_message": bool(system_message),
        "system_message_mode": system_message.get("mode") if isinstance(system_message, dict) else None,
        "system_message_chars": len(system_content) if isinstance(system_content, str) else 0,
        "mcp_server_names": sorted(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
        "te2_mcp_present": isinstance(te2_cfg, dict),
        "te2_mcp_type": te2_cfg.get("type") if isinstance(te2_cfg, dict) else None,
        "te2_mcp_url": te2_cfg.get("url") if isinstance(te2_cfg, dict) else None,
    }


def _log_runtime_config(stage: str, conversation_id: str, config: Dict[str, Any]) -> None:
    summary = _summarize_runtime_config(config)
    print(f"[CopilotSDK] {stage} config convo={conversation_id[:8]} summary={summary}")
    _add_to_raw_buffer("out", conversation_id, f"{stage}_config {summary}")


# ── Permission / Approval handler ───────────────────────────────────

# Pending approval futures: request_id -> asyncio.Future
_pending_approvals: Dict[str, asyncio.Future[Any]] = {}
_PERMISSION_RESULT_KIND_VALUES = set(get_args(PermissionRequestResultKind))


def _get_conversation_settings(conversation_id: str) -> Dict[str, Any]:
    """Read settings from conversation meta.json."""
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta and isinstance(meta.get("settings"), dict):
            return meta["settings"]
    return {}


def _resolve_sdk_session_id(conversation_id: str) -> Optional[str]:
    session = _sessions.get(conversation_id)
    if session is not None and isinstance(session.session_id, str) and session.session_id:
        return session.session_id
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if isinstance(meta, dict):
            thread_id = meta.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def _copilot_session_dir(session_id: str) -> Path:
    return _COPILOT_SESSION_STATE_ROOT / session_id


def _copilot_session_db_path(session_id: str) -> Path:
    return _copilot_session_dir(session_id) / "session.db"


def _copilot_plan_doc_path(session_id: str) -> Path:
    return _copilot_session_dir(session_id) / _COPILOT_PLAN_FILENAME


def _persist_meta_plan_exists(conversation_id: str, plan_exists: bool) -> None:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return
    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return
    normalized = bool(plan_exists)
    if meta.get("plan_exists") is normalized:
        return
    meta["plan_exists"] = normalized
    _meta_fns["save"](conversation_id, meta)


def _plan_doc_signature(plan_exists: bool, plan_content: str) -> str:
    return json.dumps(
        [bool(plan_exists), plan_content if isinstance(plan_content, str) else ""],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _default_plan_doc_state(source: str = "unknown") -> Dict[str, Any]:
    return {
        "plan_exists": False,
        "plan_content": "",
        "plan_path": None,
        "plan_source": source,
    }


def _get_plan_doc_state(conversation_id: str) -> Dict[str, Any]:
    state = _plan_doc_state.get(conversation_id)
    if not isinstance(state, dict):
        return _default_plan_doc_state()
    return {
        "plan_exists": bool(state.get("plan_exists")),
        "plan_content": state.get("plan_content") if isinstance(state.get("plan_content"), str) else "",
        "plan_path": state.get("plan_path") if isinstance(state.get("plan_path"), str) and state.get("plan_path") else None,
        "plan_source": state.get("plan_source") if isinstance(state.get("plan_source"), str) and state.get("plan_source") else "unknown",
    }


def _set_plan_doc_state(
    conversation_id: str,
    *,
    plan_exists: bool,
    plan_content: str,
    plan_path: Optional[str],
    plan_source: str,
) -> Dict[str, Any]:
    state = {
        "plan_exists": bool(plan_exists),
        "plan_content": plan_content if isinstance(plan_content, str) else "",
        "plan_path": plan_path if isinstance(plan_path, str) and plan_path else None,
        "plan_source": plan_source if isinstance(plan_source, str) and plan_source else "unknown",
    }
    _plan_doc_state[conversation_id] = state
    _persist_meta_plan_exists(conversation_id, state["plan_exists"])
    return state


def _normalize_todo_status(status: Any) -> str:
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in {"done", "completed", "complete"}:
            return "completed"
        if normalized in {"in_progress", "inprogress", "in progress"}:
            return "in_progress"
    return "pending"


def _todo_step_text(todo_id: Any, title: Any, description: Any) -> str:
    if isinstance(title, str) and title.strip():
        return title.strip()
    if isinstance(description, str) and description.strip():
        return description.strip()
    if isinstance(todo_id, str) and todo_id.strip():
        return todo_id.strip()
    return "Untitled todo"


def _read_todo_snapshot_sync(sdk_session_id: str) -> Dict[str, Any]:
    db_path = _copilot_session_db_path(sdk_session_id)
    if not db_path.is_file():
        return {
            "steps": [],
            "signature": "[]",
            "db_path": str(db_path),
            "source": "session_db_missing",
        }

    db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            """
            SELECT
              rowid,
              id,
              title,
              description,
              status,
              created_at,
              updated_at
            FROM todos
            ORDER BY
              CASE WHEN created_at IS NULL OR created_at = '' THEN 1 ELSE 0 END,
              created_at,
              rowid
            """
        ).fetchall()
    finally:
        conn.close()

    steps: List[Dict[str, str]] = []
    signature_rows: List[Dict[str, str]] = []
    for rowid, todo_id, title, description, status, created_at, updated_at in rows:
        step_text = _todo_step_text(todo_id, title, description)
        normalized_status = _normalize_todo_status(status)
        steps.append({
            "step": step_text,
            "status": normalized_status,
        })
        signature_rows.append({
            "id": str(todo_id or rowid),
            "step": step_text,
            "status": normalized_status,
            "created_at": str(created_at or ""),
            "updated_at": str(updated_at or ""),
        })

    return {
        "steps": steps,
        "signature": json.dumps(signature_rows, separators=(",", ":"), ensure_ascii=False),
        "db_path": str(db_path),
        "source": "session_db",
    }


async def _read_todo_snapshot(
    conversation_id: str,
    sdk_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_session_id = sdk_session_id or _resolve_sdk_session_id(conversation_id)
    if not isinstance(resolved_session_id, str) or not resolved_session_id:
        return {
            "steps": [],
            "signature": "[]",
            "db_path": None,
            "source": "missing_session",
        }

    try:
        snapshot = await asyncio.to_thread(_read_todo_snapshot_sync, resolved_session_id)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"[CopilotSDK] todo DB read failed for {conversation_id[:8]}: {exc}")
        _add_to_raw_buffer("err", conversation_id, f"todo_db_read_failed: {exc}")
        return {
            "steps": [],
            "signature": "[]",
            "db_path": str(_copilot_session_db_path(resolved_session_id)),
            "source": "session_db_error",
        }

    snapshot["session_id"] = resolved_session_id
    return snapshot


def _read_plan_doc_snapshot_sync(sdk_session_id: str) -> Dict[str, Any]:
    plan_path = _copilot_plan_doc_path(sdk_session_id)
    if not plan_path.is_file():
        return {
            "plan_exists": False,
            "plan_content": "",
            "plan_path": None,
            "plan_source": "session_file_missing",
            "signature": _plan_doc_signature(False, ""),
        }

    content = plan_path.read_text(encoding="utf-8")
    plan_exists = bool(content.strip())
    return {
        "plan_exists": plan_exists,
        "plan_content": content,
        "plan_path": str(plan_path),
        "plan_source": "session_file",
        "signature": _plan_doc_signature(plan_exists, content),
    }


async def _read_plan_doc_snapshot(
    conversation_id: str,
    sdk_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_session_id = sdk_session_id or _resolve_sdk_session_id(conversation_id)
    if not isinstance(resolved_session_id, str) or not resolved_session_id:
        return {
            **_default_plan_doc_state("missing_session"),
            "signature": _plan_doc_signature(False, ""),
            "session_id": None,
        }

    try:
        snapshot = await asyncio.to_thread(_read_plan_doc_snapshot_sync, resolved_session_id)
    except OSError as exc:
        print(f"[CopilotSDK] plan.md read failed for {conversation_id[:8]}: {exc}")
        _add_to_raw_buffer("err", conversation_id, f"plan_doc_read_failed: {exc}")
        return {
            **_default_plan_doc_state("session_file_error"),
            "signature": _plan_doc_signature(False, ""),
            "session_id": resolved_session_id,
        }

    snapshot["session_id"] = resolved_session_id
    return snapshot


def _apply_plan_doc_snapshot(conversation_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return _set_plan_doc_state(
        conversation_id,
        plan_exists=bool(snapshot.get("plan_exists")),
        plan_content=snapshot.get("plan_content") if isinstance(snapshot.get("plan_content"), str) else "",
        plan_path=snapshot.get("plan_path") if isinstance(snapshot.get("plan_path"), str) else None,
        plan_source=snapshot.get("plan_source") if isinstance(snapshot.get("plan_source"), str) else "unknown",
    )


def _select_plan_doc_state_for_read(
    conversation_id: str,
    disk_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    cached_state = _get_plan_doc_state(conversation_id)
    disk_content = disk_snapshot.get("plan_content") if isinstance(disk_snapshot.get("plan_content"), str) else ""
    disk_exists = bool(disk_snapshot.get("plan_exists"))
    cached_exists = bool(cached_state.get("plan_exists"))

    if disk_exists:
        if (
            cached_exists
            and cached_state.get("plan_source") == "sdk"
            and isinstance(cached_state.get("plan_content"), str)
            and cached_state["plan_content"] != disk_content
        ):
            return cached_state
        return _apply_plan_doc_snapshot(conversation_id, disk_snapshot)

    if cached_exists and cached_state.get("plan_source") == "sdk":
        return cached_state

    return _apply_plan_doc_snapshot(conversation_id, disk_snapshot)


def _build_plan_state_payload(
    conversation_id: str,
    todo_snapshot: Dict[str, Any],
    *,
    include_plan_content: bool,
) -> Dict[str, Any]:
    plan_state = _get_plan_doc_state(conversation_id)
    payload: Dict[str, Any] = {
        "has_plan": True,
        "has_todo": True,
        "plan_exists": bool(plan_state["plan_exists"]),
        "plan_steps": list(todo_snapshot.get("steps") or []),
        "plan_path": plan_state["plan_path"],
        "plan_source": plan_state["plan_source"],
        "todo_source": todo_snapshot.get("source"),
    }
    if include_plan_content or not plan_state["plan_exists"]:
        payload["plan_content"] = plan_state["plan_content"]
    return payload


async def _emit_todo_plan_state(
    conversation_id: str,
    sdk_session_id: str,
    *,
    force: bool = False,
    derive_plan_operation: bool = False,
) -> None:
    if _broadcast_fn is None:
        return
    previous_plan_state = _get_plan_doc_state(conversation_id)
    previous_todo_signature = _todo_signatures.get(conversation_id)
    previous_plan_signature = _plan_doc_signatures.get(conversation_id)
    plan_snapshot = await _read_plan_doc_snapshot(conversation_id, sdk_session_id=sdk_session_id)
    plan_state = _select_plan_doc_state_for_read(conversation_id, plan_snapshot)
    plan_signature = _plan_doc_signature(
        bool(plan_state.get("plan_exists")),
        plan_state.get("plan_content") if isinstance(plan_state.get("plan_content"), str) else "",
    )
    snapshot = await _read_todo_snapshot(conversation_id, sdk_session_id=sdk_session_id)
    todo_signature = snapshot.get("signature") if isinstance(snapshot.get("signature"), str) else "[]"
    changed_todo = previous_todo_signature != todo_signature
    changed_plan = previous_plan_signature != plan_signature
    if not force and not changed_todo and not changed_plan:
        return
    _todo_signatures[conversation_id] = todo_signature
    _plan_doc_signatures[conversation_id] = plan_signature
    payload = _build_plan_state_payload(
        conversation_id,
        snapshot,
        include_plan_content=False,
    )
    if derive_plan_operation and changed_plan:
        previous_exists = bool(previous_plan_state.get("plan_exists"))
        current_exists = bool(plan_state.get("plan_exists"))
        if not previous_exists and current_exists:
            payload["plan_operation"] = "create"
        elif previous_exists and not current_exists:
            payload["plan_operation"] = "delete"
        else:
            payload["plan_operation"] = "update"
    payload.update({
        "type": "plan_state",
        "conversation_id": conversation_id,
    })
    await _broadcast_fn(payload)


async def _watch_todo_db_changes(conversation_id: str, sdk_session_id: str) -> None:
    await _emit_todo_plan_state(conversation_id, sdk_session_id, force=True)
    session_dir = _copilot_session_dir(sdk_session_id)
    if not session_dir.is_dir():
        raise RuntimeError(f"Copilot session dir missing for todo watch: {session_dir}")

    try:
        async for changes in awatch(session_dir, recursive=False, debounce=200, step=50):
            relevant = False
            for _change, changed_path in changes:
                changed_name = Path(changed_path).name
                if changed_name in _COPILOT_TODO_FILENAMES or changed_name == _COPILOT_PLAN_FILENAME:
                    relevant = True
                    break
            if not relevant:
                continue
            await asyncio.sleep(_COPILOT_SESSION_STATE_READ_SETTLE_SECONDS)
            await _emit_todo_plan_state(
                conversation_id,
                sdk_session_id,
                derive_plan_operation=any(
                    Path(changed_path).name == _COPILOT_PLAN_FILENAME for _change, changed_path in changes
                ),
            )
    except asyncio.CancelledError:
        raise


async def _stop_todo_watch(conversation_id: str) -> None:
    task = _todo_watch_tasks.pop(conversation_id, None)
    _todo_watch_sessions.pop(conversation_id, None)
    _todo_signatures.pop(conversation_id, None)
    _plan_doc_signatures.pop(conversation_id, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _ensure_todo_watch(conversation_id: str, sdk_session_id: str) -> None:
    existing_task = _todo_watch_tasks.get(conversation_id)
    existing_session_id = _todo_watch_sessions.get(conversation_id)
    if (
        existing_task is not None
        and not existing_task.done()
        and existing_session_id == sdk_session_id
    ):
        return

    await _stop_todo_watch(conversation_id)
    _todo_watch_sessions[conversation_id] = sdk_session_id
    task = asyncio.create_task(
        _watch_todo_db_changes(conversation_id, sdk_session_id),
        name=f"copilot-todo-watch-{conversation_id[:8]}",
    )
    _todo_watch_tasks[conversation_id] = task


async def _build_live_plan_state(
    conversation_id: str,
    plan_doc_update: Dict[str, Any],
) -> Dict[str, Any]:
    plan_state = _set_plan_doc_state(
        conversation_id,
        plan_exists=bool(plan_doc_update.get("plan_exists")),
        plan_content=plan_doc_update.get("plan_content") if isinstance(plan_doc_update.get("plan_content"), str) else "",
        plan_path=plan_doc_update.get("plan_path") if isinstance(plan_doc_update.get("plan_path"), str) else None,
        plan_source=plan_doc_update.get("plan_source") if isinstance(plan_doc_update.get("plan_source"), str) else "sdk",
    )
    _plan_doc_signatures[conversation_id] = _plan_doc_signature(
        bool(plan_state.get("plan_exists")),
        plan_state.get("plan_content") if isinstance(plan_state.get("plan_content"), str) else "",
    )
    sdk_session_id = _resolve_sdk_session_id(conversation_id)
    if isinstance(sdk_session_id, str) and sdk_session_id:
        await _ensure_todo_watch(conversation_id, sdk_session_id)
    snapshot = await _read_todo_snapshot(conversation_id, sdk_session_id=sdk_session_id)
    signature = snapshot.get("signature")
    if isinstance(signature, str):
        _todo_signatures[conversation_id] = signature
    return _build_plan_state_payload(
        conversation_id,
        snapshot,
        include_plan_content=True,
    )


def _upsert_pending_approval(conversation_id: str, descriptor: Dict[str, Any]) -> None:
    if _meta_fns and "upsert_pending_approval" in _meta_fns:
        _meta_fns["upsert_pending_approval"](conversation_id, descriptor)
        return
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        pending = meta.get("pending_approvals") if isinstance(meta.get("pending_approvals"), dict) else {}
        pending[str(descriptor.get("request_id") or "")] = descriptor
        meta["pending_approvals"] = pending
        _meta_fns["save"](conversation_id, meta)


def _remove_pending_approval(conversation_id: str, request_id: str) -> None:
    if _meta_fns and "remove_pending_approval" in _meta_fns:
        _meta_fns["remove_pending_approval"](conversation_id, request_id)
        return
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        pending = meta.get("pending_approvals") if isinstance(meta.get("pending_approvals"), dict) else {}
        pending.pop(str(request_id or ""), None)
        meta["pending_approvals"] = pending
        _meta_fns["save"](conversation_id, meta)


def _json_safe_sdk_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        return _json_safe_sdk_value(value.to_dict())
    if is_dataclass(value):
        return {key: _json_safe_sdk_value(val) for key, val in asdict(value).items() if val is not None}
    if isinstance(value, dict):
        return {str(key): _json_safe_sdk_value(val) for key, val in value.items() if val is not None}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_sdk_value(item) for item in value]
    return value


def _normalize_permission_kind(kind: Any) -> str:
    normalized = _json_safe_sdk_value(kind)
    if isinstance(normalized, str) and normalized.strip():
        return normalized.strip()
    return "unknown"


def _extract_permission_request_fields(request: PermissionRequest) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for field_name in getattr(request, "__dataclass_fields__", {}) or {}:
        value = getattr(request, field_name, None)
        if value is None:
            continue
        fields[field_name] = _json_safe_sdk_value(value)
    return fields


def _decision_to_permission_result(decision: Any) -> PermissionRequestResult:
    decision_text = str(decision or "").strip().lower()
    if decision_text == "accept":
        return PermissionRequestResult(kind="approved", rules=[])
    return PermissionRequestResult(kind="denied-interactively-by-user", rules=[])


def _normalize_permission_resolution(resolution: Any) -> PermissionRequestResult:
    if isinstance(resolution, PermissionRequestResult):
        return resolution

    result_payload = resolution
    if isinstance(resolution, dict) and isinstance(resolution.get("result"), dict):
        result_payload = resolution.get("result")

    if not isinstance(result_payload, dict):
        return _decision_to_permission_result(result_payload)

    decision_text = str(
        result_payload.get("decision")
        or (resolution.get("decision") if isinstance(resolution, dict) else "")
        or ""
    ).strip().lower()
    kind_text = str(result_payload.get("kind") or "").strip()
    if kind_text not in _PERMISSION_RESULT_KIND_VALUES:
        kind_text = ""
    if not kind_text:
        kind_text = "approved" if decision_text == "accept" else "denied-interactively-by-user"

    rules = result_payload.get("rules")
    if not isinstance(rules, list):
        rules = []
    feedback = result_payload.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        feedback = str(feedback)
    message = result_payload.get("message")
    if message is not None and not isinstance(message, str):
        message = str(message)
    path = result_payload.get("path")
    if path is not None and not isinstance(path, str):
        path = str(path)

    return PermissionRequestResult(
        kind=kind_text,
        rules=rules,
        feedback=feedback,
        message=message,
        path=path,
    )


def _merge_runtime_settings(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    merged = dict(_get_conversation_settings(conversation_id))
    if isinstance(settings, dict):
        merged.update(settings)
    if cwd:
        merged["cwd"] = cwd
    if model:
        merged["model"] = model
    return merged


def _copilot_debug_trace_enabled(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> bool:
    merged = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model)
    raw = merged.get("debug_trace")
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _copilot_config_dir() -> str:
    return str(Path.home() / ".copilot")


def _resolve_external_copilot_cli_path() -> Optional[str]:
    env_path = os.environ.get("COPILOT_CLI_PATH")
    if isinstance(env_path, str) and env_path.strip():
        return os.path.abspath(os.path.expanduser(env_path.strip()))
    resolved = shutil.which("copilot")
    if resolved:
        return os.path.abspath(resolved)
    return None


def _runtime_signature_payload(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    merged = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model)
    payload = {
        "cwd": merged.get("cwd"),
        "model": merged.get("model"),
        "config_dir": _copilot_config_dir(),
    }

    te2_enabled = te2_mcp_integration_enabled(merged)
    payload["reasoning_effort"] = merged.get("reasoning_effort") or merged.get("effort")
    payload["developer_instructions"] = build_effective_developer_instructions(
        merged.get("developer_instructions"),
        te2_enabled=te2_enabled,
    )
    payload["mcp_servers"] = build_copilot_mcp_servers(
        merged.get("mcp_servers"),
        te2_enabled=te2_enabled,
        base_url=merged.get("te2_base_url"),
    )
    return payload


def _runtime_signature(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    payload = _runtime_signature_payload(conversation_id, settings=settings, cwd=cwd, model=model)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _build_session_runtime_config(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    merged = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model)
    config: Dict[str, Any] = {
        "streaming": True,
        "config_dir": _copilot_config_dir(),
        "on_permission_request": _make_permission_handler(conversation_id),
        "hooks": SessionHooks(
            on_pre_tool_use=_make_pre_tool_use_hook(conversation_id),
        ),
    }

    working_directory = merged.get("cwd")
    if isinstance(working_directory, str) and working_directory.strip():
        resolved = os.path.expanduser(working_directory) if working_directory.startswith("~") else working_directory
        config["working_directory"] = resolved

    model_id = merged.get("model")
    if isinstance(model_id, str) and model_id.strip():
        config["model"] = model_id.strip()

    reasoning_effort = merged.get("reasoning_effort") or merged.get("effort")
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        config["reasoning_effort"] = reasoning_effort.strip()

    developer_instructions = build_effective_developer_instructions(
        merged.get("developer_instructions"),
        te2_enabled=te2_mcp_integration_enabled(merged),
    )
    if developer_instructions:
        config["system_message"] = {
            "mode": "append",
            "content": developer_instructions,
        }

    mcp_servers = build_copilot_mcp_servers(
        merged.get("mcp_servers"),
        te2_enabled=te2_mcp_integration_enabled(merged),
        base_url=merged.get("te2_base_url"),
    )
    if mcp_servers is not None:
        te2_cfg = mcp_servers.get(TE2_MCP_SERVER_NAME) if isinstance(mcp_servers, dict) else None
        if isinstance(te2_cfg, dict):
            mcp_servers[TE2_MCP_SERVER_NAME] = {
                **te2_cfg,
                "type": "http",
                "url": build_te2_mcp_streamable_http_url(merged.get("te2_base_url") or ""),
            }
        if mcp_servers:
            config["mcp_servers"] = mcp_servers

    return config


def _normalize_mode_value(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in {"interactive", "plan", "autopilot"}:
        return text
    return None


async def _apply_session_mode(
    session: CopilotSession,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    desired_mode = _normalize_mode_value(settings.get("mode")) if isinstance(settings, dict) else None
    if not desired_mode:
        return None
    result = await session.rpc.mode.set(SessionModeSetParams(mode=SessionMode(desired_mode)))
    applied = getattr(result, "mode", None)
    if isinstance(getattr(applied, "value", None), str):
        return applied.value
    return _normalize_mode_value(applied)


def _session_event_paths(session_id: str) -> List[Path]:
    root = Path(_copilot_config_dir()) / "session-state"
    return [
        root / session_id / "events.jsonl",
        root / f"{session_id}.jsonl",
    ]


def _sanitize_session_attachments(session_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"session_id": session_id, "records_rewritten": 0, "paths": []}
    for path in _session_event_paths(session_id):
        if not path.is_file():
            continue
        tmp = path.with_name(f".{path.name}.tmp")
        rewritten = 0
        with path.open("r", encoding="utf-8", errors="ignore") as src, tmp.open("w", encoding="utf-8") as dst:
            for line in src:
                try:
                    record = json.loads(line)
                except Exception:
                    dst.write(line)
                    continue
                data = record.get("data")
                if isinstance(data, dict) and "attachments" in data and data.get("attachments") is None:
                    data["attachments"] = []
                    rewritten += 1
                    dst.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
                else:
                    dst.write(line)
        if rewritten:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
        result["records_rewritten"] += rewritten
        result["paths"].append({"path": str(path), "rewritten": rewritten})
    return result


def _runtime_option_descriptor(
    setting_key: str,
    label: str,
    options: List[Dict[str, str]],
    current: Optional[str],
    default: str,
) -> Dict[str, Any]:
    return {
        "settingKey": setting_key,
        "label": label,
        "options": [dict(item) for item in options],
        "current": current or "",
        "default": default,
    }


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = _merge_runtime_settings(
        conversation_id or "",
        settings=settings,
    ) if conversation_id else dict(settings or {})
    return {
        "agent": extension_id,
        "approval": _runtime_option_descriptor(
            "approval_policy",
            "Approval Policy",
            _APPROVAL_POLICY_OPTIONS,
            merged.get("approval_policy") if isinstance(merged.get("approval_policy"), str) else None,
            _DEFAULT_APPROVAL_POLICY,
        ),
        "sandbox": _runtime_option_descriptor(
            "sandbox_policy",
            "Directory Trust",
            _SANDBOX_POLICY_OPTIONS,
            merged.get("sandbox_policy") if isinstance(merged.get("sandbox_policy"), str) else None,
            _DEFAULT_SANDBOX_POLICY,
        ),
        "web": _runtime_option_descriptor(
            "web_policy",
            "Web Access",
            _WEB_POLICY_OPTIONS,
            merged.get("web_policy") if isinstance(merged.get("web_policy"), str) else None,
            _DEFAULT_WEB_POLICY,
        ),
        "mode": _runtime_option_descriptor(
            "mode",
            "Mode",
            _MODE_OPTIONS,
            _normalize_mode_value(merged.get("mode")),
            _DEFAULT_MODE,
        ),
    }


async def read_plan(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    async with _get_session_lock(conversation_id):
        sdk_session_id = _resolve_sdk_session_id(conversation_id)
        if not isinstance(sdk_session_id, str) or not sdk_session_id:
            return {
                "has_plan": True,
                "has_todo": True,
                "plan_exists": False,
                "plan_content": "",
                "plan_steps": [],
                "todo_source": "missing_session",
                "plan_source": "missing_session",
            }

        plan_snapshot = await _read_plan_doc_snapshot(conversation_id, sdk_session_id=sdk_session_id)
        plan_doc_state = _select_plan_doc_state_for_read(conversation_id, plan_snapshot)
        _plan_doc_signatures[conversation_id] = _plan_doc_signature(
            bool(plan_doc_state.get("plan_exists")),
            plan_doc_state.get("plan_content") if isinstance(plan_doc_state.get("plan_content"), str) else "",
        )

        if _broadcast_fn is not None:
            await _ensure_todo_watch(conversation_id, sdk_session_id)

        todo_snapshot = await _read_todo_snapshot(conversation_id, sdk_session_id=sdk_session_id)
        payload = _build_plan_state_payload(
            conversation_id,
            todo_snapshot,
            include_plan_content=True,
        )
        payload["plan_exists"] = bool(plan_doc_state["plan_exists"])
        payload["plan_content"] = plan_doc_state["plan_content"]
        payload["plan_path"] = plan_doc_state["plan_path"]
        payload["plan_source"] = plan_doc_state["plan_source"]
        return payload


def resolve_approval(request_id: str, resolution: Any) -> bool:
    """Called from WS handler when user responds to an approval request."""
    fut = _pending_approvals.pop(request_id, None)
    if fut and not fut.done():
        fut.set_result(_normalize_permission_resolution(resolution))
        return True
    return False


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: Dict[str, Any]) -> bool:
    if not isinstance(descriptor, dict):
        return False
    runtime_signature = descriptor.get("runtime_signature")
    current_signature = _runtime_signatures.get(conversation_id)
    if runtime_signature and not current_signature:
        return False
    if runtime_signature and current_signature and runtime_signature != current_signature:
        return False
    descriptor_thread_id = descriptor.get("thread_id")
    session = _sessions.get(conversation_id)
    current_session_id = getattr(session, "session_id", None) if session else None
    if descriptor_thread_id and current_session_id and descriptor_thread_id != current_session_id:
        return False
    if descriptor_thread_id and not current_session_id:
        return False
    return request_id in _pending_approvals


def _build_preview_diff(payload: Dict[str, Any], args: Dict[str, Any]) -> None:
    """
    Compute a unified-diff preview from tool arguments and attach to payload.
    Supports edit-style tools (old_str/new_str) and create/write tools (file_text/content).
    Sets payload["diff"] and payload["path"] for the frontend's formatDiff().
    """
    import difflib

    file_path = args.get("path") or args.get("file_path") or args.get("file") or ""
    old_str = args.get("old_str")
    if old_str is None:
        old_str = args.get("oldString") or args.get("old_text") or args.get("oldText")
    new_str = args.get("new_str")
    if new_str is None:
        new_str = args.get("newString") or args.get("new_text") or args.get("newText")
    file_text = args.get("file_text") or args.get("content") or args.get("new_content") or args.get("fileText")
    command = args.get("command") or args.get("cmd")

    if old_str is not None and new_str is not None:
        # edit/replace style — compute unified diff
        # Ensure trailing newlines so difflib produces separate lines
        old_text = str(old_str)
        new_text = str(new_str)
        if not old_text.endswith("\n"):
            old_text += "\n"
        if not new_text.endswith("\n"):
            new_text += "\n"
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=file_path or "a", tofile=file_path or "b",
        )
        payload["diff"] = "".join(diff)
        if file_path:
            payload["path"] = file_path
    elif file_text is not None and file_path:
        # create/write style — show as full addition
        ft = str(file_text)
        if not ft.endswith("\n"):
            ft += "\n"
        new_lines = ft.splitlines(keepends=True)
        diff = difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null", tofile=file_path,
        )
        payload["diff"] = "".join(diff)
        payload["path"] = file_path
    elif command and file_path:
        # shell command on a file — just show command + path
        payload["path"] = file_path

def _make_permission_handler(conversation_id: str) -> Callable:
    """
    Create a permission handler for a session.

    Respects approval_policy from conversation settings:
      - auto-approve: silently approve everything
      - suggest: broadcast to frontend, auto-approve on timeout (120s)
      - always-ask: broadcast to frontend, wait indefinitely
    """
    async def handler(
        request: PermissionRequest,
        context: Dict[str, str],
    ) -> PermissionRequestResult:
        kind = _normalize_permission_kind(getattr(request, "kind", "unknown"))
        tool_call_id = getattr(request, "tool_call_id", "") or ""
        _add_to_raw_buffer("in", conversation_id, f"permission_request: {kind} tool={tool_call_id}")

        settings = _get_conversation_settings(conversation_id)
        policy = settings.get("approval_policy", _DEFAULT_APPROVAL_POLICY)

        # Auto-approve: no user interaction needed
        if policy == "auto-approve":
            print(f"[CopilotSDK] Auto-approving {kind} tool={tool_call_id} convo={conversation_id[:8]}")
            return PermissionRequestResult(kind="approved", rules=[])

        print(f"[CopilotSDK] Permission request: kind={kind} tool={tool_call_id} policy={policy} convo={conversation_id[:8]}")

        # Build a unique request ID for this approval
        request_id = f"approval_{conversation_id[:8]}_{tool_call_id or id(request)}"

        # Look up tool context from the router if available
        router = _routers.get(conversation_id)
        tool_info = router.tool_calls.get(tool_call_id, {}) if router else {}

        # Build the payload the frontend expects
        payload: Dict[str, Any] = {"kind": kind}
        command = tool_info.get("title", "")
        if command:
            payload["command"] = command
        # Include tool name and raw arguments so frontend can render diffs
        tool_name = tool_info.get("tool_name", "")
        if tool_name:
            payload["tool_name"] = tool_name
        raw_args = tool_info.get("arguments")
        normalized_args = raw_args
        if isinstance(raw_args, str):
            try:
                normalized_args = json.loads(raw_args)
            except Exception:
                normalized_args = raw_args
        if normalized_args:
            payload["arguments"] = _json_safe_sdk_value(normalized_args)
        request_fields = _extract_permission_request_fields(request)
        if request_fields:
            payload["request"] = request_fields
        payload["path"] = payload.get("path") or request_fields.get("path") or request_fields.get("file_name")
        payload["cwd"] = payload.get("cwd") or request_fields.get("cwd")
        payload["diff"] = payload.get("diff") or request_fields.get("diff")
        for field_name in ("can_offer_session_approval", "possible_paths", "possible_urls", "warning", "intention"):
            if field_name in request_fields and field_name not in payload:
                payload[field_name] = request_fields[field_name]
        if not payload.get("changes") and request_fields.get("new_file_contents") is not None:
            payload["changes"] = request_fields.get("new_file_contents")

        # Compute a preview diff from tool arguments if possible
        if isinstance(normalized_args, dict):
            _build_preview_diff(payload, normalized_args)

        # Create a Future that the WS handler will resolve
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        _pending_approvals[request_id] = fut

        runtime_signature = _runtime_signatures.get(conversation_id) or _runtime_signature(conversation_id)
        session = _sessions.get(conversation_id)
        approval_event = {
            "type": "approval",
            "conversation_id": conversation_id,
            "id": request_id,
            "request_id": request_id,
            "kind": kind,
            "tool_call_id": tool_call_id,
            "turn_id": router.current_turn_id if router else "",
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        subagent_id = tool_info.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id:
            approval_event["subagent_id"] = subagent_id
        descriptor = {
            "request_id": request_id,
            "agent": "copilot-sdk",
            "kind": kind,
            "payload": payload,
            "thread_id": getattr(session, "session_id", None),
            "turn_id": router.current_turn_id if router else "",
            "runtime_signature": runtime_signature,
            "runtime_instance_id": getattr(session, "session_id", None),
            "transcript_anchor": {"turn_id": router.current_turn_id if router else ""},
            "source": "live",
            "created_at": approval_event["created_at"],
            "render_event": approval_event,
        }
        _upsert_pending_approval(conversation_id, descriptor)

        # Broadcast approval_request to frontend
        if _broadcast_fn:
            await _broadcast_fn(approval_event)

        # Wait based on policy
        if policy == "always-ask":
            # No timeout — wait indefinitely for user decision
            permission_result = await fut
        else:
            # "suggest" — auto-approve after 120s timeout
            try:
                permission_result = await asyncio.wait_for(fut, timeout=120.0)
            except asyncio.TimeoutError:
                _pending_approvals.pop(request_id, None)
                _remove_pending_approval(conversation_id, request_id)
                print(f"[CopilotSDK] Approval timeout for {request_id}, auto-approving")
                permission_result = PermissionRequestResult(kind="approved", rules=[])

        return _normalize_permission_resolution(permission_result)

    return handler


# ── Pre-tool-use hook (sandbox + web policy) ────────────────────────

# Tool names known to perform web/network access
_WEB_TOOLS = {"web_search", "web_fetch", "fetch_url", "curl", "wget", "http_request"}

# Tool names known to perform file operations
_FILE_TOOLS = {"edit", "create", "write", "read_file", "write_file", "delete", "move",
               "bash", "shell", "exec", "run_command", "apply_patch"}


def _make_pre_tool_use_hook(conversation_id: str) -> Callable:
    """
    Create a pre_tool_use hook that enforces sandbox_policy and web_policy.

    sandbox_policy:
      - allow-all-paths: allow any file path
      - cwd-only: deny file ops outside cwd (default)
      - ask: prompt user for file ops outside cwd

    web_policy:
      - allow: allow web tools
      - deny: block web tools (default)
      - ask: prompt user for web tools
    """
    async def hook(
        input: Dict[str, Any],
        context: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        tool_name = input.get("toolName", "")
        tool_args = input.get("toolArgs") or {}
        settings = _get_conversation_settings(conversation_id)

        # ── Web policy check ──
        web_policy = settings.get("web_policy", _DEFAULT_WEB_POLICY)
        if tool_name.lower() in _WEB_TOOLS or any(w in tool_name.lower() for w in ("web", "fetch", "url", "http")):
            if web_policy == "deny":
                print(f"[CopilotSDK] Web tool '{tool_name}' denied by web_policy convo={conversation_id[:8]}")
                return {"permissionDecision": "deny", "permissionDecisionReason": "Web access denied by policy"}
            elif web_policy == "ask":
                return {"permissionDecision": "ask"}
            # "allow" → fall through

        # ── Sandbox / directory trust check ──
        sandbox_policy = settings.get("sandbox_policy", _DEFAULT_SANDBOX_POLICY)
        if sandbox_policy != "allow-all-paths" and tool_name.lower() in _FILE_TOOLS:
            cwd = settings.get("cwd") or os.path.expanduser("~")
            cwd = os.path.realpath(os.path.expanduser(cwd))
            # Check path arguments
            target_path = None
            if isinstance(tool_args, dict):
                target_path = tool_args.get("path") or tool_args.get("file_path") or tool_args.get("file") or tool_args.get("command")
            if target_path and isinstance(target_path, str) and os.path.sep in target_path:
                real_target = os.path.realpath(os.path.expanduser(target_path))
                if not real_target.startswith(cwd + os.path.sep) and real_target != cwd:
                    if sandbox_policy == "cwd-only":
                        print(f"[CopilotSDK] Path '{target_path}' outside cwd, denied by sandbox_policy convo={conversation_id[:8]}")
                        return {"permissionDecision": "deny",
                                "permissionDecisionReason": f"Path outside working directory ({cwd})"}
                    elif sandbox_policy == "ask":
                        return {"permissionDecision": "ask"}

        # Allow by default
        return None

    return hook


# ── Event handler factory ───────────────────────────────────────────

def _make_event_handler(conversation_id: str) -> Callable[[SessionEvent], None]:
    """Create an event handler that routes SessionEvents to the conversation's router."""
    def handler(event: SessionEvent) -> None:
        _add_to_raw_buffer("in", conversation_id, f"{event.type.value}: {str(event.data)[:200]}")
        router = _routers.get(conversation_id)
        if router:
            # Schedule the coroutine on the running event loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(router.route_event(event))
            except RuntimeError:
                print(f"[CopilotSDK] No running loop for event routing: {event.type.value}")
        else:
            print(f"[CopilotSDK] No router for {conversation_id[:8]}, event: {event.type.value}")

    return handler


# ── Initialization ──────────────────────────────────────────────────

def init_copilot_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> None:
    """
    Initialize the Copilot SDK manager with server callbacks.
    
    Called by extensions/__init__.py during load_extensions().
    The fws_getter is accepted for interface compat but not used —
    the SDK manages its own CLI process.
    """
    global _broadcast_fn, _transcript_fn, _meta_fns, _initialized
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns or {}
    _initialized = True
    print("[CopilotSDK] Manager initialized")


async def _ensure_client() -> CopilotClient:
    """Get or create the global CopilotClient singleton."""
    global _client
    async with _get_client_lock():
        if _client is None:
            client_options: Dict[str, Any] = {
                "use_stdio": True,
                "auto_start": True,
                "auto_restart": True,
                "log_level": "info",
            }
            cli_path = _resolve_external_copilot_cli_path()
            if cli_path:
                client_options["cli_path"] = cli_path
            _client = CopilotClient(client_options)
            await _client.start()
            print(f"[CopilotSDK] Client started, state={_client.get_state()} cli_path={cli_path or 'bundled/default'}")
        return _client


# ── Warm-up ─────────────────────────────────────────────────────────

async def warm_up_extension(
    extension_id: str,
    timeout: float = 60.0,
) -> bool:
    """
    Start the CopilotClient and verify it's responsive.
    Much faster than ACP warm-up since SDK manages the process itself.
    """
    global _ready_event

    if _ready_event and _ready_event.is_set():
        return True

    _ready_event = asyncio.Event()

    try:
        client = await asyncio.wait_for(_ensure_client(), timeout=timeout)
        ping = await client.ping("warmup")
        print(f"[CopilotSDK] Warm-up ping: {ping}")
        _ready_event.set()
        return True
    except Exception as e:
        print(f"[CopilotSDK] Warm-up failed: {e}")
        return False


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """Warm up the Copilot SDK client."""
    result = await warm_up_extension("copilot-sdk", timeout=timeout)
    return {"copilot-sdk": result}


def is_extension_ready(extension_id: str) -> bool:
    return _ready_event.is_set() if _ready_event else False


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    if _ready_event and _ready_event.is_set():
        return True
    return await warm_up_extension(extension_id, timeout=timeout)


# ── Session management ──────────────────────────────────────────────


def has_session(conversation_id: str) -> bool:
    return conversation_id in _sessions


async def init_session(
    conversation_id: str,
    extension_id: str,
    cwd: Optional[str],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    async with _get_session_lock(conversation_id):
        return await _init_session_unlocked(conversation_id, extension_id, cwd, settings=settings)


async def _init_session_unlocked(
    conversation_id: str,
    extension_id: str,
    cwd: Optional[str],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a new Copilot session for a conversation.
    
    Called eagerly on settings save (eagerSessionInit) or on first message.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Copilot SDK manager not initialized"}

    # Already has session?
    if conversation_id in _sessions:
        session = _sessions[conversation_id]
        return {"ok": True, "session_id": session.session_id, "already_initialized": True}

    try:
        client = await _ensure_client()
        runtime_signature = _runtime_signature(
            conversation_id,
            settings=settings,
            cwd=cwd,
        )

        # Create router for this conversation
        router = CopilotEventRouter(
            conversation_id=conversation_id,
            broadcast_fn=_broadcast_fn,
            transcript_fn=_transcript_fn,
            debug_trace=_copilot_debug_trace_enabled(
                conversation_id,
                settings=settings,
                cwd=cwd,
            ),
            plan_state_provider=_build_live_plan_state,
        )
        _routers[conversation_id] = router

        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
        )
        _log_runtime_config("create_session", conversation_id, config)

        # Let SDK generate its own session_id (don't pass ours)
        session = await client.create_session(config)
        sdk_session_id = session.session_id
        _sessions[conversation_id] = session
        _runtime_signatures[conversation_id] = runtime_signature

        # Subscribe to events
        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        # Store SDK session_id as thread_id in conversation meta (like codex)
        # INVARIANT: thread_id is immutable once set — never overwrite
        if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
            meta = _meta_fns["load"](conversation_id)
            if meta:
                existing = meta.get("thread_id")
                if existing and existing != sdk_session_id:
                    print(f"[CopilotSDK] WARNING: init_session refusing to overwrite thread_id "
                          f"{existing[:8]} with {sdk_session_id[:8]} for convo {conversation_id[:8]}")
                else:
                    meta["thread_id"] = sdk_session_id
                    meta["status"] = "active"
                    _meta_fns["save"](conversation_id, meta)

        await _ensure_todo_watch(conversation_id, sdk_session_id)

        resolved_cwd = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd).get("cwd")
        print(f"[CopilotSDK] Session created: convo={conversation_id[:8]} sdk_session={sdk_session_id[:8]} cwd={resolved_cwd}")
        _add_to_raw_buffer("out", conversation_id, f"session_created sdk={sdk_session_id[:8]} cwd={resolved_cwd}")
        return {"ok": True, "session_id": sdk_session_id}

    except Exception as e:
        print(f"[CopilotSDK] init_session failed: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


async def resume_session(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    async with _get_session_lock(conversation_id):
        return await _resume_session_unlocked(
            conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )


async def _resume_session_unlocked(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resume an existing Copilot session (survives server restarts).
    
    Looks up the SDK session_id from meta["thread_id"], resumes via SDK,
    and keys in-memory state by conversation_id.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # Already active in memory?
    if conversation_id in _sessions:
        s = _sessions[conversation_id]
        return {"ok": True, "session_id": s.session_id, "already_active": True}

    # Look up SDK session ID from conversation meta
    sdk_session_id = None
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta:
            sdk_session_id = meta.get("thread_id")

    if not sdk_session_id:
        return {"ok": False, "error": f"No thread_id (SDK session) for conversation {conversation_id[:8]}"}

    try:
        client = await _ensure_client()
        sanitize_result = _sanitize_session_attachments(str(sdk_session_id))
        if sanitize_result.get("records_rewritten"):
            _add_to_raw_buffer("out", conversation_id, f"sanitize_session_attachments {sanitize_result}")
        runtime_signature = _runtime_signature(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=model,
        )

        # Create router keyed by our conversation_id
        router = CopilotEventRouter(
            conversation_id=conversation_id,
            broadcast_fn=_broadcast_fn,
            transcript_fn=_transcript_fn,
            debug_trace=_copilot_debug_trace_enabled(
                conversation_id,
                settings=settings,
                cwd=cwd,
                model=model,
            ),
            plan_state_provider=_build_live_plan_state,
        )
        _routers[conversation_id] = router

        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=model,
        )
        _log_runtime_config("resume_session", conversation_id, config)

        # Resume using the real SDK session ID
        session = await client.resume_session(
            sdk_session_id,
            config,
        )
        # Key in-memory by our conversation_id
        _sessions[conversation_id] = session
        _runtime_signatures[conversation_id] = runtime_signature

        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        await _ensure_todo_watch(conversation_id, str(sdk_session_id))

        print(f"[CopilotSDK] Session resumed: convo={conversation_id[:8]} sdk_session={sdk_session_id[:8]}")
        _add_to_raw_buffer("out", conversation_id, f"session_resumed sdk={sdk_session_id[:8]}")
        return {"ok": True, "session_id": sdk_session_id}

    except Exception as e:
        print(f"[CopilotSDK] resume_session failed: {e}")
        _add_to_raw_buffer("err", conversation_id, f"resume_failed: {e}")
        return {"ok": False, "error": f"Session resume failed: {e}"}


def _register_attached_session(
    client: CopilotClient,
    conversation_id: str,
    sdk_session_id: str,
    runtime_signature: str,
    config: Dict[str, Any],
    debug_trace: bool = False,
) -> CopilotSession:
    """
    Attach to a known SDK session id without issuing session.resume first.

    This preserves the send-first contract: try the turn against the persisted
    session id, and only fall back to session.resume if the CLI reports that the
    session is not currently loaded.
    """
    router = CopilotEventRouter(
        conversation_id=conversation_id,
        broadcast_fn=_broadcast_fn,
        transcript_fn=_transcript_fn,
        debug_trace=debug_trace,
        plan_state_provider=_build_live_plan_state,
    )
    _routers[conversation_id] = router

    session = CopilotSession(sdk_session_id, client._client)  # type: ignore[attr-defined]
    session._register_permission_handler(config.get("on_permission_request"))
    hooks = config.get("hooks")
    if hooks:
        session._register_hooks(hooks)
    on_user_input_request = config.get("on_user_input_request")
    if on_user_input_request:
        session._register_user_input_handler(on_user_input_request)

    with client._sessions_lock:  # type: ignore[attr-defined]
        client._sessions[sdk_session_id] = session  # type: ignore[attr-defined]

    _sessions[conversation_id] = session
    _runtime_signatures[conversation_id] = runtime_signature
    unsub = session.on(_make_event_handler(conversation_id))
    _unsubs[conversation_id] = unsub
    _add_to_raw_buffer("out", conversation_id, f"session_attached sdk={sdk_session_id[:8]}")
    return session


# ── Message handling ────────────────────────────────────────────────

async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle a user message for a Copilot SDK conversation.
    
    Main entry point called by server.py extension router.
    Follows the codex pattern: lazy resume on first message, not on conversation select.
    """
    async with _get_session_lock(conversation_id):
        if not _broadcast_fn or not _transcript_fn:
            return {"ok": False, "error": "Manager not initialized"}

        cwd = settings.get("cwd") or os.path.expanduser("~")
        desired_signature = _runtime_signature(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=settings.get("model"),
        )
        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=settings.get("model"),
        )

        # Ensure session exists — attach/send first for known sessions, init for new ones
        if conversation_id not in _sessions:
            # Check if this conversation already has a thread_id.
            thread_id = None
            result: Dict[str, Any] = {"ok": True}
            if _meta_fns and "load" in _meta_fns:
                meta = _meta_fns["load"](conversation_id)
                if meta:
                    thread_id = meta.get("thread_id")

            if thread_id:
                client = await _ensure_client()
                _register_attached_session(
                    client,
                    conversation_id,
                    str(thread_id),
                    desired_signature,
                    config,
                    debug_trace=_copilot_debug_trace_enabled(
                        conversation_id,
                        settings=settings,
                        cwd=cwd,
                        model=settings.get("model"),
                    ),
                )
                await _ensure_todo_watch(conversation_id, str(thread_id))
            else:
                # Brand new conversation — create a fresh session
                result = await _init_session_unlocked(
                    conversation_id,
                    agent_type,
                    cwd,
                    settings=settings,
                )

            if not result.get("ok"):
                return result
        elif _runtime_signatures.get(conversation_id) != desired_signature:
            _add_to_raw_buffer("out", conversation_id, "runtime_changed, re-resuming session")
            await _destroy_session_unlocked(conversation_id)
            result = await _resume_session_unlocked(
                conversation_id,
                cwd=cwd,
                model=settings.get("model"),
                settings=settings,
            )
            if not result.get("ok"):
                return result

        session = _sessions.get(conversation_id)
        if not session:
            return {"ok": False, "error": "Session not found after init"}

        router = _routers.get(conversation_id)
        if not router:
            return {"ok": False, "error": "Router not found"}
        router.set_debug_trace(
            _copilot_debug_trace_enabled(
                conversation_id,
                settings=settings,
                cwd=cwd,
                model=settings.get("model"),
            )
        )
        applied_mode = await _apply_session_mode(session, settings=settings)
        if applied_mode:
            settings["mode"] = applied_mode

        # Notify router of turn start
        await router.on_turn_start(text)

        try:
            _add_to_raw_buffer("out", conversation_id, f"prompt: {text[:200]}")

            # Fire-and-forget send — events come via session.on() handler
            await session.send(
                MessageOptions(prompt=text, attachments=[]),
            )

            return {"ok": True, "session_id": conversation_id}

        except Exception as e:
            err_msg = str(e)
            # SDK binary evicts inactive sessions — catch and retry via resume
            if "Session not found" in err_msg or "session not found" in err_msg:
                print(f"[CopilotSDK] Session evicted, attempting re-resume for {conversation_id[:8]}")
                _add_to_raw_buffer("out", conversation_id, "session_evicted, re-resuming...")
                # Clear stale in-memory state so resume rebuilds it
                stale_session = _sessions.pop(conversation_id, None)
                _routers.pop(conversation_id, None)
                _runtime_signatures.pop(conversation_id, None)
                if conversation_id in _unsubs:
                    try:
                        _unsubs.pop(conversation_id)()
                    except Exception:
                        pass
                if stale_session and _client:
                    with _client._sessions_lock:  # type: ignore[attr-defined]
                        _client._sessions.pop(stale_session.session_id, None)  # type: ignore[attr-defined]
                result = await _resume_session_unlocked(
                    conversation_id,
                    cwd=cwd,
                    model=settings.get("model"),
                    settings=settings,
                )
                if not result.get("ok"):
                    print(f"[CopilotSDK] Re-resume failed: {result}")
                    return result
                session = _sessions.get(conversation_id)
                router = _routers.get(conversation_id)
                if not session or not router:
                    return {"ok": False, "error": "Session not found after re-resume"}
                await router.on_turn_start(text)
                try:
                    await session.send(MessageOptions(prompt=text, attachments=[]))
                    return {"ok": True, "session_id": conversation_id}
                except Exception as e2:
                    print(f"[CopilotSDK] Retry send failed: {e2}")
                    return {"ok": False, "error": str(e2)}

            print(f"[CopilotSDK] send failed: {e}")
            _add_to_raw_buffer("out", conversation_id, f"send_error: {e}")
            return {"ok": False, "error": err_msg}


# ── Model listing ───────────────────────────────────────────────────

async def list_models() -> List[Dict[str, Any]]:
    """List available models from the Copilot CLI."""
    try:
        client = await _ensure_client()
        models = await client.list_models()
        def _safe(obj):
            """Recursively convert SDK objects to JSON-safe dicts/primitives."""
            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, dict):
                return {k: _safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_safe(v) for v in obj]
            if hasattr(obj, "__dict__"):
                return {k: _safe(v) for k, v in vars(obj).items() if not k.startswith("_")}
            return str(obj)

        return [
            {
                "id": m.id,
                "name": getattr(m, "name", m.id),
                "billing": _safe(getattr(m, "billing", None)),
                "capabilities": _safe(getattr(m, "capabilities", None)),
                "policy": _safe(getattr(m, "policy", None)),
                "supported_reasoning_efforts": _safe(getattr(m, "supported_reasoning_efforts", None)),
                "default_reasoning_effort": _safe(getattr(m, "default_reasoning_effort", None)),
            }
            for m in models
        ]
    except Exception as e:
        print(f"[CopilotSDK] list_models failed: {e}")
        return []


# ── Session listing ─────────────────────────────────────────────────

async def list_sessions(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all known sessions from the Copilot CLI.
    
    If cwd is provided, sessions are sorted with CWD-matching sessions first.
    """
    try:
        client = await _ensure_client()
        sessions = await client.list_sessions()
        
        result = []
        for s in sessions:
            entry: Dict[str, Any] = {
                "session_id": s.sessionId,
                "start_time": getattr(s, "startTime", None),
                "modified_time": getattr(s, "modifiedTime", None),
                "is_remote": getattr(s, "isRemote", False),
                "summary": getattr(s, "summary", None),
            }
            # Check if session has context (newer SDK versions)
            ctx = getattr(s, "context", None)
            if ctx:
                entry["context"] = {
                    "cwd": getattr(ctx, "cwd", None),
                    "git_root": getattr(ctx, "gitRoot", None),
                    "repository": getattr(ctx, "repository", None),
                    "branch": getattr(ctx, "branch", None),
                }
            # Check if this session is currently active in our server
            entry["active"] = s.sessionId in _sessions
            result.append(entry)
        
        # Sort: CWD-matching first, then by modified_time descending
        if cwd:
            resolved_cwd = os.path.expanduser(cwd) if cwd.startswith("~") else cwd
            resolved_cwd = os.path.realpath(resolved_cwd)
            
            def relevance(entry):
                ctx = entry.get("context") or {}
                session_cwd = ctx.get("cwd") or ""
                session_git = ctx.get("git_root") or ""
                # Exact CWD match = highest priority
                if session_cwd and os.path.realpath(session_cwd) == resolved_cwd:
                    return 0
                # Same git root = next priority
                if session_git:
                    try:
                        import subprocess
                        git_root = subprocess.run(
                            ["git", "-C", resolved_cwd, "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=2
                        ).stdout.strip()
                        if git_root and os.path.realpath(session_git) == os.path.realpath(git_root):
                            return 1
                    except Exception:
                        pass
                # Fallback: check if CWD is a parent/child of session CWD
                if session_cwd:
                    r = os.path.realpath(session_cwd)
                    if r.startswith(resolved_cwd) or resolved_cwd.startswith(r):
                        return 2
                return 9
            
            for entry in result:
                entry["_relevance"] = relevance(entry)
            # Sort: relevance ascending, modified_time descending within group
            from functools import cmp_to_key
            def _session_cmp(a, b):
                ra, rb = a["_relevance"], b["_relevance"]
                if ra != rb:
                    return -1 if ra < rb else 1
                ma, mb = a.get("modified_time") or "", b.get("modified_time") or ""
                if ma != mb:
                    return 1 if ma < mb else -1
                return 0
            result.sort(key=cmp_to_key(_session_cmp))
            for entry in result:
                entry.pop("_relevance", None)
        
        return result
    except Exception as e:
        print(f"[CopilotSDK] list_sessions failed: {e}")
        return []


async def resume_session_with_history(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    extension_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Bind a Copilot SDK session to a conversation.

    Session picker flow: user picks an existing SDK session for a new internal
    conversation.  We:
      1. Write thread_id into meta (binding).
      2. Resume the SDK session (so it's live for future messages).
    Transcript hydration is handled separately by hydrate_transcript().
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # 1. Bind thread_id into meta
    # INVARIANT: thread_id is immutable once set — never overwrite
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta:
            existing = meta.get("thread_id")
            if existing and existing != session_id:
                print(f"[CopilotSDK] WARNING: resume_session_with_history refusing to overwrite "
                      f"thread_id {existing[:8]} with {session_id[:8]} for convo {conversation_id[:8]}")
                return {"ok": False, "error": f"Conversation already bound to thread {existing[:8]}"}
            meta["thread_id"] = session_id
            meta["status"] = "active"
            merged_settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
            if isinstance(merged_settings, dict):
                merged_settings = dict(merged_settings)
            else:
                merged_settings = {}
            merged_settings["agent"] = "copilot-sdk"
            if cwd:
                merged_settings["cwd"] = cwd
            if model:
                merged_settings["model"] = model
            if isinstance(settings, dict):
                for key, value in settings.items():
                    if value is None or value == "":
                        merged_settings.pop(key, None)
                    else:
                        merged_settings[key] = value
            meta["settings"] = merged_settings
            _meta_fns["save"](conversation_id, meta)

    # 2. Resume the SDK session (creates in-memory session + router)
    result = await resume_session(conversation_id, cwd=cwd, model=model, settings=settings)
    if not result.get("ok"):
        print(f"[CopilotSDK] resume_session_with_history: resume failed: {result}")
        return result

    print(f"[CopilotSDK] Bound session {session_id[:8]} to convo {conversation_id[:8]}")
    return {
        "ok": True,
        "session_id": session_id,
        "conversation_id": conversation_id,
    }


async def hydrate_transcript(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Build flat transcript entries from an existing SDK session's history.

    This is the SDK equivalent of _rollout_preview_entries() for Codex.
    Calls get_messages() on the SDK session and converts each SessionEvent
    into the standard transcript entry format that _write_transcript_entries
    expects: {role, text, ts, ...}.

    Returns a list — server.py writes them to transcript.jsonl.
    """
    from ._vendor.copilot.generated.session_events import SessionEventType

    # Ensure session is resumed so we can call get_messages()
    session = _sessions.get(conversation_id)
    if not session:
        # Try resuming first
        result = await resume_session(conversation_id, cwd=cwd, model=model, settings=settings)
        if not result.get("ok"):
            print(f"[CopilotSDK] hydrate_transcript: resume failed: {result}")
            return []
        session = _sessions.get(conversation_id)
    if not session:
        return []

    try:
        events = await session.get_messages()
    except Exception as e:
        print(f"[CopilotSDK] hydrate_transcript get_messages failed: {e}")
        return []

    print(f"[CopilotSDK] hydrate_transcript: got {len(events)} events for {conversation_id[:8]}")

    items: List[Dict[str, Any]] = []
    ts_now = datetime.now(timezone.utc).isoformat()

    for ev in events:
        try:
            etype = ev.type
            data = ev.data

            if etype == SessionEventType.USER_MESSAGE:
                text = getattr(data, "content", None) or getattr(data, "message", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "user", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.ASSISTANT_MESSAGE:
                text = getattr(data, "content", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "assistant", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.ASSISTANT_REASONING:
                text = getattr(data, "reasoning_text", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "reasoning", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
                tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or ""
                result_obj = getattr(data, "result", None)
                content = ""
                detailed = ""
                if result_obj and hasattr(result_obj, "content"):
                    content = getattr(result_obj, "content", "") or ""
                    detailed = getattr(result_obj, "detailed_content", "") or ""
                elif isinstance(result_obj, str):
                    content = result_obj
                else:
                    content = str(result_obj or "")
                file_path = getattr(data, "path", None) or ""
                items.append({
                    "role": "command",
                    "command": str(tool_name),
                    "output": content,
                    "exit_code": 0,
                    "ts": ts_now,
                })
                # Append diff only for file-mutating tools
                if detailed and tool_name.lower() in _FILE_CHANGE_TOOLS:
                    items.append({
                        "role": "diff",
                        "path": file_path,
                        "text": detailed,
                        "ts": ts_now,
                    })

            elif etype == SessionEventType.ASSISTANT_USAGE:
                total = getattr(data, "output_tokens", None)
                # Record usage if available, but not critical for hydration
                pass

            # Skip delta events, turn lifecycle, session events — they're
            # intermediate; we only care about completed items for hydration.

        except Exception as ev_err:
            print(f"[CopilotSDK] hydrate_transcript: skipping event: {ev_err}")

    print(f"[CopilotSDK] hydrate_transcript: built {len(items)} transcript entries")
    return items


# ── Cleanup ─────────────────────────────────────────────────────────

async def destroy_session(conversation_id: str) -> bool:
    async with _get_session_lock(conversation_id):
        return await _destroy_session_unlocked(conversation_id)


async def _destroy_session_unlocked(conversation_id: str) -> bool:
    """Destroy a session (keeps data on disk for resume)."""
    unsub = _unsubs.pop(conversation_id, None)
    if unsub:
        unsub()

    await _stop_todo_watch(conversation_id)

    session = _sessions.pop(conversation_id, None)
    _routers.pop(conversation_id, None)
    _runtime_signatures.pop(conversation_id, None)
    _plan_doc_state.pop(conversation_id, None)

    if session and _client:
        with _client._sessions_lock:  # type: ignore[attr-defined]
            _client._sessions.pop(session.session_id, None)  # type: ignore[attr-defined]

    if session:
        try:
            await session.destroy()
            print(f"[CopilotSDK] Session destroyed: {conversation_id[:8]}")
            return True
        except Exception as e:
            print(f"[CopilotSDK] destroy_session error: {e}")
    return False


async def delete_session(conversation_id: str) -> bool:
    """Delete our side of the conversation only.
    
    Like codex: removing a conversation removes our meta/transcript,
    but the SDK session persists and can be resumed by a new conversation
    via the session picker.
    """
    return await destroy_session(conversation_id)


async def stop_client() -> None:
    """Stop the global CopilotClient (server shutdown)."""
    global _client
    for conversation_id in list(_todo_watch_tasks.keys()):
        await _stop_todo_watch(conversation_id)
    if _client:
        try:
            errors = await _client.stop()
            if errors:
                print(f"[CopilotSDK] Stop errors: {errors}")
            _client = None
            print("[CopilotSDK] Client stopped")
        except Exception as e:
            print(f"[CopilotSDK] stop_client error: {e}")


# ── Abort ───────────────────────────────────────────────────────────

async def abort_session(conversation_id: str) -> bool:
    """Abort the current request in a session."""
    session = _sessions.get(conversation_id)
    if not session:
        return False
    try:
        await session.abort()
        print(f"[CopilotSDK] Aborted: {conversation_id[:8]}")
        return True
    except Exception as e:
        print(f"[CopilotSDK] abort error: {e}")
        return False


# ── Shutdown alias (for server.py lifespan) ─────────────────────────

shutdown_client = stop_client

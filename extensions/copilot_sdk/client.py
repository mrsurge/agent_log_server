"""
Copilot SDK Client Handler for Extension System

Manages Copilot CLI agent sessions via the vendored Copilot SDK source.

Key advantages:
- Session resume built-in (client.resume_session)
- Streaming via SessionConfig.streaming=True
- All Copilot models including Gemini
- Rich event model via session.on(handler)
"""

import asyncio
import contextlib
import inspect
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Awaitable, Deque, Tuple, Protocol, TypeAlias, TypedDict, cast, get_args, runtime_checkable
from collections import deque
from uuid import uuid4

from framework_shells.orchestrator import Orchestrator
from framework_shells.manager import FrameworkShellManager

from ._vendor.copilot import (
    CopilotClient,
    CopilotSession,
    SubprocessConfig,
)
from ._vendor.copilot.generated.rpc import ModeSetRequest as SessionModeSetParams, SessionMode
from ._vendor.copilot.session import (
    SessionConfig,
    ResumeSessionConfig,
    SessionEvent,
    PermissionRequest,
    PermissionRequestResult,
    ErrorOccurredHandler,
    MCPStdioServerConfig as MCPLocalServerConfig,
    MCPHTTPServerConfig as MCPRemoteServerConfig,
    MCPServerConfig,
    PostToolUseHandler,
    PreToolUseHandler,
    PreToolUseHookInput,
    PreToolUseHookOutput,
    _PermissionHandlerFn,
    ReasoningEffort,
    SessionHooks,
    SessionEndHandler,
    SessionStartHandler,
    SystemMessageConfig,
    PermissionRequestResultKind,
    UserInputHandler,
    UserInputRequest,
    UserInputResponse,
    UserPromptSubmittedHandler,
)
from ._vendor.copilot.generated.session_events import SessionEventType

from .file_change_preview import build_file_change_preview
from .fws_pipe_process import FrameworkShellPipeProcess
from .protocol_adapter import compact_sdk_session, resume_sdk_session
from .router import CopilotEventRouter, _looks_like_diff, _FILE_CHANGE_TOOLS
from .te2_runtime import build_copilot_mcp_servers
import agent_log_server.ask_user_interactions as ask_user_interactions
from agent_log_server.prompt_context import build_effective_prompt_context
from agent_log_server.te2_mcp_config import (
    te2_mcp_integration_enabled,
)
from watchfiles import awatch


PayloadDict: TypeAlias = dict[str, object]
SettingsDict: TypeAlias = dict[str, object]
RequestContext: TypeAlias = dict[str, str]


class RawBufferEntry(TypedDict):
    ts: str
    dir: str
    convo: str
    data: str


class PlanDocState(TypedDict):
    plan_exists: bool
    plan_content: str
    plan_path: str | None
    plan_source: str


# ── Global state ────────────────────────────────────────────────────

_client: Optional[CopilotClient] = None
_client_lock: Optional[asyncio.Lock] = None
_fws_getter: Optional[Callable] = None
_copilot_shell_id: Optional[str] = None
_copilot_fws_process: Optional[FrameworkShellPipeProcess] = None
_BroadcastFn = Callable[[PayloadDict], Awaitable[None]]
_TranscriptFn = Callable[[str, PayloadDict], Awaitable[None]]


def _get_client_lock() -> asyncio.Lock:
    """Lazy-init the lock on first use (inside the running event loop)."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _noop_broadcast(_: PayloadDict) -> None:
    return None


async def _noop_transcript(_: str, __: PayloadDict) -> None:
    return None


def _resolved_broadcast_fn() -> _BroadcastFn:
    return _broadcast_fn if _broadcast_fn is not None else _noop_broadcast


def _resolved_transcript_fn() -> _TranscriptFn:
    return _transcript_fn if _transcript_fn is not None else _noop_transcript


async def _append_debug_transcript(conversation_id: str, transcript_entry: PayloadDict) -> None:
    await _resolved_transcript_fn()(conversation_id, transcript_entry)


# Server callbacks (injected by init_copilot_manager)
_broadcast_fn: Optional[_BroadcastFn] = None
_transcript_fn: Optional[_TranscriptFn] = None
_meta_fns: Optional[Dict[str, Callable]] = None

# Session tracking: conversation_id -> CopilotSession
_sessions: Dict[str, CopilotSession] = {}
# Router tracking: conversation_id -> CopilotEventRouter
_routers: Dict[str, CopilotEventRouter] = {}
# Event unsubscribe fns: conversation_id -> unsubscribe callable
_unsubs: Dict[str, Callable] = {}
# Per-conversation event queue / worker to preserve SDK arrival order.
_event_queues: Dict[str, asyncio.Queue[Optional[SessionEvent]]] = {}
_event_tasks: Dict[str, asyncio.Task[None]] = {}
_recent_event_keys: Dict[str, Deque[Tuple[str, str, str]]] = {}
_recent_event_key_sets: Dict[str, set[Tuple[str, str, str]]] = {}
# Runtime signature tracking: conversation_id -> signature of effective session config inputs
_runtime_signatures: Dict[str, str] = {}
# Last known agent mode per conversation.
_session_modes: Dict[str, str] = {}
# Deferred cold-send recovery tasks keyed by conversation_id.
_deferred_send_tasks: Dict[str, asyncio.Task[None]] = {}
# Per-conversation session locks: serialize init/resume/send/destroy per conversation.
_session_locks: Dict[str, asyncio.Lock] = {}
# Live todo watch tasks keyed by conversation_id.
_todo_watch_tasks: Dict[str, asyncio.Task[None]] = {}
_todo_watch_sessions: Dict[str, str] = {}
_todo_signatures: Dict[str, str] = {}
_plan_doc_signatures: Dict[str, str] = {}
# Latest known plan-document state keyed by conversation_id.
_plan_doc_state: dict[str, PlanDocState] = {}
_model_context_window_cache: Dict[str, Optional[int]] = {}
_model_context_window_lock: Optional[asyncio.Lock] = None

# Ready state
_ready_event: Optional[asyncio.Event] = None
_initialized: bool = False

# Debug buffer (circular)
_raw_buffer: list[RawBufferEntry] = []
_RAW_BUFFER_MAX = 2000
_debug_raw_entry_counters: Dict[str, int] = {}
_RECENT_EVENT_KEY_LIMIT = 512

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


def _get_model_context_window_lock() -> asyncio.Lock:
    global _model_context_window_lock
    if _model_context_window_lock is None:
        _model_context_window_lock = asyncio.Lock()
    return _model_context_window_lock


@runtime_checkable
class _SupportsToDict(Protocol):
    def to_dict(self) -> object: ...


class _FrameworkShellRecord(Protocol):
    id: str
    status: str
    label: str | None
    spec_id: str | None


class _FrameworkShellManager(Protocol):
    async def list_shells(self) -> list[_FrameworkShellRecord]: ...

    async def get_shell(self, shell_id: str) -> _FrameworkShellRecord | None: ...

    async def terminate_shell(self, shell_id: str, force: bool) -> None: ...

class TodoStep(TypedDict):
    step: str
    status: str


class TodoSnapshot(TypedDict, total=False):
    steps: list[TodoStep]
    signature: str
    db_path: str | None
    source: str
    session_id: str

class PlanDocSnapshot(PlanDocState, total=False):
    signature: str
    session_id: str | None


class RuntimeOptionDescriptor(TypedDict):
    settingKey: str
    label: str
    options: list[dict[str, str]]
    current: str
    default: str


class QuotaInfo(TypedDict):
    text: str
    detail: str
    tone: str


class PendingRequestSpec(TypedDict, total=False):
    type: str
    request_method: str
    request_params: PayloadDict
    choices: list[str]


class ApprovalDescriptor(TypedDict, total=False):
    request_id: str
    agent: str
    kind: str
    payload: PayloadDict
    request_method: str
    request_params: PayloadDict
    thread_id: str | None
    turn_id: str
    runtime_signature: str
    runtime_instance_id: str | None
    transcript_anchor: PayloadDict
    source: str
    created_at: str
    render_event: PayloadDict


def _object_mapping(value: object) -> PayloadDict | None:
    if isinstance(value, dict):
        return cast(PayloadDict, value)
    with contextlib.suppress(TypeError):
        raw = vars(value)
        if isinstance(raw, dict):
            return cast(PayloadDict, raw)
    return None


def _extract_model_context_window(model: object) -> Optional[int]:
    if model is None:
        return None
    model_dict = _object_mapping(model)
    if model_dict is None:
        return None
    capabilities = model_dict.get("capabilities")
    capabilities_dict = _object_mapping(capabilities)
    limits = capabilities_dict.get("limits") if capabilities_dict is not None else None
    limits_dict = _object_mapping(limits)
    candidates = (
        limits_dict.get("max_context_window_tokens") if limits_dict is not None else None,
        limits_dict.get("maxContextWindowTokens") if limits_dict is not None else None,
        model_dict.get("max_context_window_tokens"),
        model_dict.get("maxContextWindowTokens"),
    )
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
    return None


async def _refresh_model_context_window_cache() -> None:
    async with _get_model_context_window_lock():
        models = await list_models()
        if not models:
            return
        next_cache: Dict[str, Optional[int]] = {}
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            next_cache[model_id.strip()] = _extract_model_context_window(item)
        _model_context_window_cache.clear()
        _model_context_window_cache.update(next_cache)


async def _context_window_for_model(model_id: Optional[str]) -> Optional[int]:
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    normalized = model_id.strip()
    if normalized not in _model_context_window_cache:
        await _refresh_model_context_window_cache()
        if normalized not in _model_context_window_cache:
            _model_context_window_cache[normalized] = None
    value = _model_context_window_cache.get(normalized)
    if isinstance(value, int) and value > 0:
        return value
    return None
_DEFAULT_SANDBOX_POLICY = "cwd-only"
_DEFAULT_WEB_POLICY = "deny"
_DEFAULT_MODE = "interactive"
_COPILOT_PERMISSION_REQUEST_METHOD = "copilot/permission/request"
_COPILOT_USER_INPUT_REQUEST_METHOD = "copilot/user_input/request"
_COPILOT_SESSION_STATE_ROOT = Path.home() / ".copilot" / "session-state"
_COPILOT_TODO_FILENAMES = frozenset({"session.db", "session.db-wal", "session.db-shm"})
_COPILOT_PLAN_FILENAME = "plan.md"
_COPILOT_SESSION_STATE_READ_SETTLE_SECONDS = 0.10
_COPILOT_SHELL_LABEL = "copilot-sdk:cli"
_COPILOT_SHELL_SPEC_ID = "copilot_cli"


def _next_debug_raw_entry_index(conversation_id: str) -> int:
    next_value = _debug_raw_entry_counters.get(conversation_id, 0) + 1
    _debug_raw_entry_counters[conversation_id] = next_value
    return next_value


def _serialize_session_event(event: SessionEvent) -> PayloadDict:
    data = event.data
    return {
        "event_type": event.type.value,
        "sdk_event_id": (str(event.id).strip() or None),
        "parent_id": (str(event.parent_id).strip() or None) if event.parent_id is not None else None,
        "data_type": type(data).__name__,
        "data": _json_safe_sdk_value(data),
    }


def _event_identity_key(event: SessionEvent) -> Optional[Tuple[str, str, str]]:
    event_type = event.type.value
    event_id = str(event.id).strip()
    if not event_id:
        return None
    parent_id = str(event.parent_id).strip() if event.parent_id is not None else ""
    return (event_type, event_id, parent_id)


def _mark_recent_event(conversation_id: str, key: Tuple[str, str, str]) -> bool:
    seen = _recent_event_key_sets.setdefault(conversation_id, set())
    if key in seen:
        return False
    order = _recent_event_keys.setdefault(conversation_id, deque())
    order.append(key)
    seen.add(key)
    while len(order) > _RECENT_EVENT_KEY_LIMIT:
        stale = order.popleft()
        seen.discard(stale)
    return True


async def _drain_event_queue(conversation_id: str) -> None:
    queue = _event_queues.get(conversation_id)
    if queue is None:
        return
    try:
        while True:
            event = await queue.get()
            try:
                if event is None:
                    return
                key = _event_identity_key(event)
                if key is not None and not _mark_recent_event(conversation_id, key):
                    continue
                router = _routers.get(conversation_id)
                if router is None:
                    continue
                if await _handle_mcp_ask_user_event(conversation_id, event):
                    continue
                await router.route_event(event)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        raise


def _ensure_event_worker(conversation_id: str) -> asyncio.Queue[Optional[SessionEvent]]:
    queue = _event_queues.get(conversation_id)
    if queue is None:
        queue = asyncio.Queue()
        _event_queues[conversation_id] = queue
    task = _event_tasks.get(conversation_id)
    if task is None or task.done():
        _event_tasks[conversation_id] = asyncio.create_task(
            _drain_event_queue(conversation_id),
            name=f"copilot-event-drain-{conversation_id[:8]}",
        )
    return queue


async def _stop_event_worker(conversation_id: str) -> None:
    queue = _event_queues.pop(conversation_id, None)
    task = _event_tasks.pop(conversation_id, None)
    _recent_event_keys.pop(conversation_id, None)
    _recent_event_key_sets.pop(conversation_id, None)
    if queue is not None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _replace_session_subscription(conversation_id: str, session: CopilotSession) -> None:
    previous = _unsubs.pop(conversation_id, None)
    if previous:
        try:
            previous()
        except Exception:
            pass
    _unsubs[conversation_id] = session.on(_make_event_handler(conversation_id))


def _add_to_raw_buffer(
    direction: str,
    conversation_id: str,
    data: object,
    *,
    payload: object = None,
    category: Optional[str] = None,
) -> None:
    summary = data if isinstance(data, str) else str(data)[:500]
    entry: RawBufferEntry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dir": direction,
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": summary,
    }
    _raw_buffer.append(entry)
    if len(_raw_buffer) > _RAW_BUFFER_MAX:
        _raw_buffer.pop(0)

    router = _routers.get(conversation_id)
    if not conversation_id or not router or not router.debug_trace or not _transcript_fn:
        return

    transcript_entry: PayloadDict = {
        "role": "debug_raw",
        "type": "debug_raw",
        "internal": True,
        "visibility": "internal",
        "source": "copilot-sdk.raw",
        "direction": direction,
        "conversation_id": conversation_id,
        "debug_index": _next_debug_raw_entry_index(conversation_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    }
    if category:
        transcript_entry["category"] = category
    if router.current_turn_id:
        transcript_entry["turn_id"] = router.current_turn_id

    if payload is not None:
        transcript_entry["payload"] = _json_safe_sdk_value(payload)
    elif isinstance(data, (dict, list, tuple, set)):
        transcript_entry["payload"] = _json_safe_sdk_value(data)
    else:
        transcript_entry["payload"] = summary

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_append_debug_transcript(conversation_id, transcript_entry))
    except RuntimeError:
        print(f"[CopilotSDK] No running loop for debug transcript append: {conversation_id[:8]}")


def get_raw_buffer(limit: int = 50) -> list[RawBufferEntry]:
    return _raw_buffer[-limit:]


def _get_session_lock(conversation_id: str) -> asyncio.Lock:
    lock = _session_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[conversation_id] = lock
    return lock


def _summarize_runtime_config(config: SettingsDict) -> PayloadDict:
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
        "include_sub_agent_streaming_events": config.get("include_sub_agent_streaming_events") is True,
        "has_system_message": bool(system_message),
        "system_message_mode": system_message.get("mode") if isinstance(system_message, dict) else None,
        "system_message_chars": len(system_content) if isinstance(system_content, str) else 0,
        "mcp_server_names": sorted(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
        "te2_mcp_present": isinstance(te2_cfg, dict),
        "te2_mcp_type": te2_cfg.get("type") if isinstance(te2_cfg, dict) else None,
        "te2_mcp_url": te2_cfg.get("url") if isinstance(te2_cfg, dict) else None,
    }


def _log_runtime_config(stage: str, conversation_id: str, config: SettingsDict) -> None:
    summary = _summarize_runtime_config(config)
    print(f"[CopilotSDK] {stage} config convo={conversation_id[:8]} summary={summary}")
    _add_to_raw_buffer("out", conversation_id, f"{stage}_config {summary}")


# ── Permission / Approval handler ───────────────────────────────────

# Pending approval futures: request_id -> asyncio.Future
_pending_approvals: dict[str, asyncio.Future[object]] = {}
_pending_request_specs: dict[str, PendingRequestSpec] = {}
_pending_mcp_ask_user_tools: Dict[str, Dict[str, str]] = {}
_PERMISSION_RESULT_KIND_VALUES = set(get_args(PermissionRequestResultKind))


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_string(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _coerce_copilot_tool_arguments(raw_args: object) -> PayloadDict:
    direct_args = _object_mapping(raw_args)
    if direct_args is not None:
        return dict(direct_args)
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = cast(object, json.loads(text))
            except Exception:
                parsed = None
            parsed_args = _object_mapping(parsed)
            if parsed_args is not None:
                return dict(parsed_args)
    return {}


def _copilot_mcp_tool_identity(data: object) -> Tuple[str, str]:
    raw_tool_name = _optional_string(getattr(data, "tool_name", None)) or ""
    server_name = _optional_string(getattr(data, "mcp_server_name", None)) or ""
    tool_name = _optional_string(getattr(data, "mcp_tool_name", None)) or ""
    if not server_name and not tool_name:
        prefix = f"{ask_user_interactions.AGENT_PTY_ASK_USER_SERVER}-"
        if raw_tool_name.startswith(prefix):
            return ask_user_interactions.AGENT_PTY_ASK_USER_SERVER, raw_tool_name[len(prefix):]
    return server_name, tool_name or raw_tool_name


def _is_copilot_mcp_ask_user_event(data: object) -> bool:
    server_name, tool_name = _copilot_mcp_tool_identity(data)
    return (
        server_name == ask_user_interactions.AGENT_PTY_ASK_USER_SERVER
        and tool_name == ask_user_interactions.AGENT_PTY_ASK_USER_TOOL
    )


def _normalize_reasoning_effort(value: object) -> Optional[ReasoningEffort]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text == "low":
        return "low"
    if text == "medium":
        return "medium"
    if text == "high":
        return "high"
    if text == "xhigh":
        return "xhigh"
    return None


def _normalize_string_list(value: object, *, field_name: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return list(value)


def _normalize_string_dict(value: object, *, field_name: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    normalized: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only string keys and values")
        normalized[key] = item
    return normalized


def _normalize_system_message_config(value: object) -> Optional[SystemMessageConfig]:
    if not isinstance(value, dict):
        return None
    content = value.get("content")
    if not isinstance(content, str):
        return None
    mode = value.get("mode")
    if mode == "replace":
        return {"mode": "replace", "content": content}
    if mode in (None, "", "append"):
        return {"mode": "append", "content": content}
    return None


def _normalize_session_hooks(value: object) -> Optional[SessionHooks]:
    if not isinstance(value, dict):
        return None
    hooks: SessionHooks = {}
    on_pre_tool_use = value.get("on_pre_tool_use")
    if callable(on_pre_tool_use):
        pre_tool_use = cast(PreToolUseHandler, on_pre_tool_use)
        hooks["on_pre_tool_use"] = pre_tool_use
    on_post_tool_use = value.get("on_post_tool_use")
    if callable(on_post_tool_use):
        post_tool_use = cast(PostToolUseHandler, on_post_tool_use)
        hooks["on_post_tool_use"] = post_tool_use
    on_user_prompt_submitted = value.get("on_user_prompt_submitted")
    if callable(on_user_prompt_submitted):
        user_prompt_submitted = cast(UserPromptSubmittedHandler, on_user_prompt_submitted)
        hooks["on_user_prompt_submitted"] = user_prompt_submitted
    on_session_start = value.get("on_session_start")
    if callable(on_session_start):
        session_start = cast(SessionStartHandler, on_session_start)
        hooks["on_session_start"] = session_start
    on_session_end = value.get("on_session_end")
    if callable(on_session_end):
        session_end = cast(SessionEndHandler, on_session_end)
        hooks["on_session_end"] = session_end
    on_error_occurred = value.get("on_error_occurred")
    if callable(on_error_occurred):
        error_occurred = cast(ErrorOccurredHandler, on_error_occurred)
        hooks["on_error_occurred"] = error_occurred
    return hooks or None


def _normalize_mcp_server_config(name: str, value: object) -> MCPServerConfig:
    if not isinstance(value, dict):
        raise ValueError(f"MCP server '{name}' must be a JSON object")
    tools = _normalize_string_list(value.get("tools"), field_name=f"MCP server '{name}'.tools")
    server_type = value.get("type")
    timeout = value.get("timeout")
    if timeout is not None and not isinstance(timeout, int):
        raise ValueError(f"MCP server '{name}'.timeout must be an integer")

    command = value.get("command")
    if isinstance(command, str) and command.strip():
        local: MCPLocalServerConfig = {
            "command": command,
            "tools": tools,
        }
        args = value.get("args")
        if args is not None:
            local["args"] = _normalize_string_list(args, field_name=f"MCP server '{name}'.args")
        env = value.get("env")
        if env is not None:
            local["env"] = _normalize_string_dict(env, field_name=f"MCP server '{name}'.env")
        cwd = value.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ValueError(f"MCP server '{name}'.cwd must be a string")
            local["cwd"] = cwd
        if timeout is not None:
            local["timeout"] = timeout
        if server_type is not None:
            if server_type not in {"local", "stdio"}:
                raise ValueError(f"MCP server '{name}'.type must be 'local' or 'stdio'")
            local["type"] = server_type
        return local

    url = value.get("url")
    if server_type == "http" and isinstance(url, str) and url.strip():
        remote: MCPRemoteServerConfig = {
            "type": "http",
            "url": url,
            "tools": tools,
        }
        headers = value.get("headers")
        if headers is not None:
            remote["headers"] = _normalize_string_dict(headers, field_name=f"MCP server '{name}'.headers")
        if timeout is not None:
            remote["timeout"] = timeout
        return remote

    if server_type == "sse" and isinstance(url, str) and url.strip():
        remote_sse: MCPRemoteServerConfig = {
            "type": "sse",
            "url": url,
            "tools": tools,
        }
        headers = value.get("headers")
        if headers is not None:
            remote_sse["headers"] = _normalize_string_dict(headers, field_name=f"MCP server '{name}'.headers")
        if timeout is not None:
            remote_sse["timeout"] = timeout
        return remote_sse

    raise ValueError(f"MCP server '{name}' must be a valid local/stdio or http/sse config")


def _normalize_mcp_servers(value: object) -> Optional[Dict[str, MCPServerConfig]]:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("MCP servers must be a JSON object")
    normalized: Dict[str, MCPServerConfig] = {}
    for name, server in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError("MCP server names must be non-empty strings")
        normalized[name] = _normalize_mcp_server_config(name, server)
    return normalized or None


def _populate_common_session_config(
    target: SessionConfig | ResumeSessionConfig,
    config: SettingsDict,
) -> None:
    streaming = config.get("streaming")
    if isinstance(streaming, bool):
        target["streaming"] = streaming

    include_sub_agent_streaming_events = config.get("include_sub_agent_streaming_events")
    if isinstance(include_sub_agent_streaming_events, bool):
        target["include_sub_agent_streaming_events"] = include_sub_agent_streaming_events

    config_dir = config.get("config_dir")
    if isinstance(config_dir, str) and config_dir:
        target["config_dir"] = config_dir

    on_permission_request = config.get("on_permission_request")
    if callable(on_permission_request):
        permission_handler = cast(_PermissionHandlerFn, on_permission_request)
        target["on_permission_request"] = permission_handler

    on_user_input_request = config.get("on_user_input_request")
    if callable(on_user_input_request):
        user_input_handler = cast(UserInputHandler, on_user_input_request)
        target["on_user_input_request"] = user_input_handler

    hooks = _normalize_session_hooks(config.get("hooks"))
    if hooks is not None:
        target["hooks"] = hooks

    working_directory = config.get("working_directory")
    if isinstance(working_directory, str) and working_directory:
        target["working_directory"] = working_directory

    model = config.get("model")
    if isinstance(model, str) and model:
        target["model"] = model

    reasoning_effort = _normalize_reasoning_effort(config.get("reasoning_effort"))
    if reasoning_effort is not None:
        target["reasoning_effort"] = reasoning_effort

    system_message = _normalize_system_message_config(config.get("system_message"))
    if system_message is not None:
        target["system_message"] = system_message

    mcp_servers = _normalize_mcp_servers(config.get("mcp_servers"))
    if mcp_servers is not None:
        target["mcp_servers"] = mcp_servers


def _build_create_session_config(config: SettingsDict) -> SessionConfig:
    typed: SessionConfig = {}
    _populate_common_session_config(typed, config)
    return typed


def _build_resume_session_config(config: SettingsDict) -> ResumeSessionConfig:
    typed: ResumeSessionConfig = {}
    _populate_common_session_config(typed, config)
    return typed


def _coerce_permission_result_kind(
    kind: object,
    *,
    decision_text: str,
) -> PermissionRequestResultKind:
    kind_text = str(kind or "").strip()
    if kind_text not in _PERMISSION_RESULT_KIND_VALUES:
        kind_text = "approved" if decision_text == "accept" else "denied-interactively-by-user"
    if kind_text == "approved":
        return "approved"
    if kind_text == "denied-by-rules":
        return "denied-by-rules"
    if kind_text == "denied-by-content-exclusion-policy":
        return "denied-by-content-exclusion-policy"
    if kind_text == "denied-no-approval-rule-and-could-not-request-from-user":
        return "denied-no-approval-rule-and-could-not-request-from-user"
    return "denied-interactively-by-user"


def _get_conversation_settings(conversation_id: str) -> SettingsDict:
    """Read settings from conversation meta.json."""
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        settings = meta.get("settings") if isinstance(meta, dict) else None
        if isinstance(settings, dict):
            return cast(SettingsDict, settings)
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


def _default_plan_doc_state(source: str = "unknown") -> PlanDocState:
    return {
        "plan_exists": False,
        "plan_content": "",
        "plan_path": None,
        "plan_source": source,
    }


def _get_plan_doc_state(conversation_id: str) -> PlanDocState:
    state = _plan_doc_state.get(conversation_id)
    if not isinstance(state, dict):
        return _default_plan_doc_state()
    return {
        "plan_exists": bool(state.get("plan_exists")),
        "plan_content": _string_or_empty(state.get("plan_content")),
        "plan_path": _optional_string(state.get("plan_path")),
        "plan_source": _optional_string(state.get("plan_source")) or "unknown",
    }


def _set_plan_doc_state(
    conversation_id: str,
    *,
    plan_exists: bool,
    plan_content: str,
    plan_path: Optional[str],
    plan_source: str,
 ) -> PlanDocState:
    state: PlanDocState = {
        "plan_exists": bool(plan_exists),
        "plan_content": plan_content if isinstance(plan_content, str) else "",
        "plan_path": plan_path if isinstance(plan_path, str) and plan_path else None,
        "plan_source": plan_source if isinstance(plan_source, str) and plan_source else "unknown",
    }
    _plan_doc_state[conversation_id] = state
    _persist_meta_plan_exists(conversation_id, state["plan_exists"])
    return state


def _normalize_todo_status(status: object) -> str:
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in {"done", "completed", "complete"}:
            return "completed"
        if normalized in {"in_progress", "inprogress", "in progress"}:
            return "in_progress"
    return "pending"


def _todo_step_text(todo_id: object, title: object, description: object) -> str:
    if isinstance(title, str) and title.strip():
        return title.strip()
    if isinstance(description, str) and description.strip():
        return description.strip()
    if isinstance(todo_id, str) and todo_id.strip():
        return todo_id.strip()
    return "Untitled todo"


def _read_todo_snapshot_sync(sdk_session_id: str) -> TodoSnapshot:
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

    rows = cast(list[tuple[object, object, object, object, object, object, object]], rows)
    steps: list[TodoStep] = []
    signature_rows: list[dict[str, str]] = []
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
) -> TodoSnapshot:
    snapshot: TodoSnapshot
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


def _read_plan_doc_snapshot_sync(sdk_session_id: str) -> PlanDocSnapshot:
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
) -> PlanDocSnapshot:
    snapshot: PlanDocSnapshot
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


def _apply_plan_doc_snapshot(conversation_id: str, snapshot: PlanDocSnapshot) -> PlanDocState:
    plan_content = _string_or_empty(snapshot.get("plan_content"))
    plan_path = _optional_string(snapshot.get("plan_path"))
    plan_source = _string_or_empty(snapshot.get("plan_source")) or "unknown"
    return _set_plan_doc_state(
        conversation_id,
        plan_exists=bool(snapshot.get("plan_exists")),
        plan_content=plan_content,
        plan_path=plan_path,
        plan_source=plan_source,
    )


def _select_plan_doc_state_for_read(
    conversation_id: str,
    disk_snapshot: PlanDocSnapshot,
) -> PlanDocState:
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
    todo_snapshot: TodoSnapshot,
    *,
    include_plan_content: bool,
) -> PayloadDict:
    plan_state = _get_plan_doc_state(conversation_id)
    payload: PayloadDict = {
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
    plan_content = _string_or_empty(plan_state.get("plan_content"))
    plan_signature = _plan_doc_signature(
        bool(plan_state.get("plan_exists")),
        plan_content,
    )
    snapshot = await _read_todo_snapshot(conversation_id, sdk_session_id=sdk_session_id)
    signature_value = snapshot.get("signature")
    todo_signature = signature_value if isinstance(signature_value, str) else "[]"
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
    plan_doc_update: PayloadDict,
) -> PayloadDict:
    plan_content = _string_or_empty(plan_doc_update.get("plan_content"))
    plan_path = _optional_string(plan_doc_update.get("plan_path"))
    plan_source = _string_or_empty(plan_doc_update.get("plan_source")) or "sdk"
    plan_state = _set_plan_doc_state(
        conversation_id,
        plan_exists=bool(plan_doc_update.get("plan_exists")),
        plan_content=plan_content,
        plan_path=plan_path,
        plan_source=plan_source,
    )
    state_plan_content = _string_or_empty(plan_state.get("plan_content"))
    _plan_doc_signatures[conversation_id] = _plan_doc_signature(
        bool(plan_state.get("plan_exists")),
        state_plan_content,
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


def _upsert_pending_approval(conversation_id: str, descriptor: ApprovalDescriptor) -> None:
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


def _json_safe_sdk_value(value: object) -> object:
    if isinstance(value, Enum):
        return cast(object, value.value)
    if isinstance(value, _SupportsToDict):
        return _json_safe_sdk_value(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        data = cast(dict[str, object], asdict(value))
        return {key: _json_safe_sdk_value(val) for key, val in data.items() if val is not None}
    if isinstance(value, dict):
        return {str(key): _json_safe_sdk_value(val) for key, val in value.items() if val is not None}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_sdk_value(item) for item in value]
    return value


def _normalize_permission_kind(kind: object) -> str:
    normalized = _json_safe_sdk_value(kind)
    if isinstance(normalized, str) and normalized.strip():
        return normalized.strip()
    return "unknown"


def _extract_permission_request_fields(request: PermissionRequest) -> PayloadDict:
    fields: PayloadDict = {}
    for field_name, value in cast(dict[str, object], asdict(request)).items():
        if value is None:
            continue
        fields[field_name] = _json_safe_sdk_value(value)
    return fields


def _decision_to_permission_result(decision: object) -> PermissionRequestResult:
    decision_text = str(decision or "").strip().lower()
    if decision_text == "accept":
        return PermissionRequestResult(kind="approved", rules=[])
    return PermissionRequestResult(kind="denied-interactively-by-user", rules=[])


def _normalize_permission_resolution(resolution: object) -> PermissionRequestResult:
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
    kind_value = _coerce_permission_result_kind(
        result_payload.get("kind"),
        decision_text=decision_text,
    )

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
        kind=kind_value,
        rules=rules,
        feedback=feedback,
        message=message,
        path=path,
    )


def _build_permission_request_params(
    kind: str,
    request_fields: PayloadDict,
    payload: PayloadDict,
) -> PayloadDict:
    params: PayloadDict = {
        "kind": kind,
        "availableDecisions": ["accept", "decline"],
    }
    for field_name in (
        "path",
        "cwd",
        "diff",
        "warning",
        "intention",
        "possible_paths",
        "possible_urls",
        "can_offer_session_approval",
    ):
        value = payload.get(field_name)
        if value is None:
            value = request_fields.get(field_name)
        if value is not None:
            params[field_name] = value
    if payload.get("tool_name"):
        params["tool_name"] = payload["tool_name"]
    if payload.get("command"):
        params["command"] = payload["command"]
    if payload.get("arguments") is not None:
        params["arguments"] = payload["arguments"]
    if payload.get("changes") is not None:
        params["changes"] = payload["changes"]
    if request_fields:
        params["request"] = dict(request_fields)
    return params


def _build_user_input_request_params(request: UserInputRequest) -> PayloadDict:
    question = request.get("question")
    choices = request.get("choices")
    allow_freeform = request.get("allowFreeform")
    return {
        "question": str(question or "").strip(),
        "choices": [str(item) for item in choices] if isinstance(choices, list) else [],
        "allowFreeform": True if allow_freeform is None else bool(allow_freeform),
    }


def _normalize_user_input_resolution(
    resolution: object,
    request_spec: Optional[PendingRequestSpec] = None,
) -> UserInputResponse:
    payload = resolution
    if isinstance(resolution, dict) and isinstance(resolution.get("result"), dict):
        payload = resolution.get("result")

    if isinstance(payload, str):
        answer = payload.strip()
        return {
            "answer": answer,
            "wasFreeform": True,
        }

    if not isinstance(payload, dict):
        return {
            "answer": str(payload or ""),
            "wasFreeform": True,
        }

    answer = str(payload.get("answer") or payload.get("choice") or "").strip()
    if not answer:
        answers = payload.get("answers")
        if isinstance(answers, list) and answers:
            answer = str(answers[0] or "").strip()
    choices = request_spec.get("choices") if isinstance(request_spec, dict) else []
    normalized_choices = [str(item) for item in choices] if isinstance(choices, list) else []
    was_freeform = payload.get("wasFreeform")
    if not isinstance(was_freeform, bool):
        was_freeform = not bool(answer and answer in normalized_choices)
    return {
        "answer": answer,
        "wasFreeform": bool(was_freeform),
    }


def _merge_runtime_settings(
    conversation_id: str,
    settings: Optional[SettingsDict] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> SettingsDict:
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
    settings: Optional[SettingsDict] = None,
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


def _copilot_shell_cwd() -> str:
    return os.path.expanduser("~")


async def _adopt_existing_copilot_shell(mgr: _FrameworkShellManager) -> Optional[str]:
    try:
        records = await mgr.list_shells()
    except Exception:
        return None
    for rec in records:
        if rec.status != "running":
            continue
        if (rec.label or "") != _COPILOT_SHELL_LABEL:
            continue
        if getattr(rec, "spec_id", "") != _COPILOT_SHELL_SPEC_ID:
            continue
        return rec.id
    return None


async def _start_new_copilot_shell(mgr: _FrameworkShellManager) -> str:
    spec_path = Path(__file__).parent / "shellspec" / "copilot_cli.yaml"
    orch = Orchestrator(cast(FrameworkShellManager, mgr))
    cli_path = _resolve_external_copilot_cli_path() or "copilot"
    shell = await orch.start_from_ref(
        f"{spec_path}#{_COPILOT_SHELL_SPEC_ID}",
        base_dir=spec_path.parent,
        ctx={
            "CWD": _copilot_shell_cwd(),
            "COPILOT_CLI_PATH": cli_path,
            "LOG_LEVEL": "info",
        },
        label=_COPILOT_SHELL_LABEL,
        wait_ready=False,
    )
    return shell.id


async def _get_or_start_copilot_shell() -> str:
    global _copilot_shell_id
    if _fws_getter is None:
        raise RuntimeError("FWS getter not initialized")
    mgr = await _fws_getter()
    if _copilot_shell_id:
        shell = await mgr.get_shell(_copilot_shell_id)
        if shell and shell.status == "running" and getattr(shell, "spec_id", "") == _COPILOT_SHELL_SPEC_ID:
            return _copilot_shell_id
        _copilot_shell_id = None
    adopted = await _adopt_existing_copilot_shell(mgr)
    if adopted:
        _copilot_shell_id = adopted
        return adopted
    _copilot_shell_id = await _start_new_copilot_shell(mgr)
    return _copilot_shell_id


async def _stop_copilot_shell() -> None:
    global _copilot_shell_id
    if _copilot_shell_id is None or _fws_getter is None:
        _copilot_shell_id = None
        return
    mgr = await _fws_getter()
    try:
        await mgr.terminate_shell(_copilot_shell_id, force=True)
    except Exception:
        pass
    _copilot_shell_id = None


def _runtime_signature_payload(
    conversation_id: str,
    settings: Optional[SettingsDict] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> PayloadDict:
    merged = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model)
    payload = {
        "cwd": merged.get("cwd"),
        "model": merged.get("model"),
        "config_dir": _copilot_config_dir(),
    }

    te2_enabled = te2_mcp_integration_enabled(merged)
    payload["reasoning_effort"] = merged.get("reasoning_effort") or merged.get("effort")
    payload["developer_instructions"] = build_effective_prompt_context(
        merged.get("developer_instructions"),
        te2_enabled=te2_enabled,
        cwd=merged.get("cwd"),
    )
    payload["mcp_servers"] = build_copilot_mcp_servers(
        merged.get("mcp_servers"),
        te2_enabled=te2_enabled,
        base_url=_optional_string(merged.get("te2_base_url")),
        cwd=_optional_string(merged.get("cwd")),
    )
    return payload


def _runtime_signature(
    conversation_id: str,
    settings: Optional[SettingsDict] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    payload = _runtime_signature_payload(conversation_id, settings=settings, cwd=cwd, model=model)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _build_session_runtime_config(
    conversation_id: str,
    settings: Optional[SettingsDict] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> SettingsDict:
    merged = _merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model)
    config: SettingsDict = {
        "streaming": True,
        "include_sub_agent_streaming_events": True,
        "config_dir": _copilot_config_dir(),
        "on_permission_request": _make_permission_handler(conversation_id),
        "on_user_input_request": _make_user_input_handler(conversation_id),
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

    developer_instructions = build_effective_prompt_context(
        merged.get("developer_instructions"),
        te2_enabled=te2_mcp_integration_enabled(merged),
        cwd=merged.get("cwd"),
    )
    if developer_instructions:
        config["system_message"] = {
            "mode": "append",
            "content": developer_instructions,
        }

    mcp_servers = build_copilot_mcp_servers(
        merged.get("mcp_servers"),
        te2_enabled=te2_mcp_integration_enabled(merged),
        base_url=_optional_string(merged.get("te2_base_url")),
        cwd=_optional_string(merged.get("cwd")),
        conversation_id=conversation_id,
    )
    if mcp_servers is not None:
        if mcp_servers:
            config["mcp_servers"] = mcp_servers

    return config


def _normalize_mode_value(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in {"interactive", "plan", "autopilot"}:
        return text
    return None


def _desired_session_mode(settings: Optional[SettingsDict] = None) -> Optional[str]:
    if not isinstance(settings, dict):
        return None
    return _normalize_mode_value(settings.get("mode"))


async def _apply_session_mode(
    session: CopilotSession,
    settings: Optional[SettingsDict] = None,
) -> Optional[str]:
    desired_mode = _desired_session_mode(settings)
    if not desired_mode:
        return None
    result = await session.rpc.mode.set(SessionModeSetParams(mode=SessionMode(desired_mode)))
    applied = getattr(result, "mode", None)
    applied_value = getattr(applied, "value", None)
    if isinstance(applied_value, str):
        return applied_value
    return _normalize_mode_value(applied)


def _is_session_not_found_error(error: BaseException | str) -> bool:
    message = str(error)
    return "Session not found" in message or "session not found" in message


async def _recover_evicted_session(
    conversation_id: str,
    *,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
    print(f"[CopilotSDK] Session evicted, attempting re-resume for {conversation_id[:8]}")
    _add_to_raw_buffer("out", conversation_id, "session_evicted, re-resuming...")

    stale_session = _sessions.pop(conversation_id, None)
    _routers.pop(conversation_id, None)
    _runtime_signatures.pop(conversation_id, None)
    _session_modes.pop(conversation_id, None)
    if conversation_id in _unsubs:
        try:
            _unsubs.pop(conversation_id)()
        except Exception:
            pass
    await _stop_event_worker(conversation_id)
    if stale_session and _client:
        with _client._sessions_lock:  # type: ignore[attr-defined]
            _client._sessions.pop(stale_session.session_id, None)  # type: ignore[attr-defined]

    return await _resume_session_unlocked(
        conversation_id,
        cwd=cwd,
        model=model,
        settings=settings,
    )


async def _await_deferred_send(conversation_id: str) -> None:
    task = _deferred_send_tasks.get(conversation_id)
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except Exception:
        pass
    finally:
        if _deferred_send_tasks.get(conversation_id) is task and task.done():
            _deferred_send_tasks.pop(conversation_id, None)


async def _emit_deferred_send_error(
    conversation_id: str,
    message: str,
    *,
    error_type: str = "message_send_failed",
    router: Optional[CopilotEventRouter] = None,
    turn_id: Optional[str] = None,
) -> None:
    active_router = router or _routers.get(conversation_id)
    if active_router is None:
        return
    msg = str(message or "Message send failed").strip() or "Message send failed"
    active_turn_id = turn_id or getattr(active_router, "current_turn_id", None)
    event: PayloadDict = {
        "type": "error",
        "conversation_id": conversation_id,
        "message": msg,
        "source": "copilot-sdk",
    }
    entry: PayloadDict = {
        "role": "error",
        "message": msg,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "copilot-sdk",
        "event": "copilot-sdk",
    }
    if active_turn_id:
        event["turn_id"] = active_turn_id
        entry["turn_id"] = active_turn_id
    if error_type:
        event["error_type"] = error_type
        entry["error_type"] = error_type
    await active_router._emit(event)
    await active_router._record(entry)
    await active_router._emit(
        {
            "type": "activity",
            "conversation_id": conversation_id,
            "label": "error",
            "active": False,
            "turn_id": active_turn_id,
        }
    )


async def _complete_deferred_cold_send(
    conversation_id: str,
    text: str,
    *,
    cwd: Optional[str],
    model: Optional[str],
    settings: SettingsDict,
) -> None:
    preserved_router = _routers.get(conversation_id)
    preserved_turn_id = getattr(preserved_router, "current_turn_id", None)
    try:
        async with _get_session_lock(conversation_id):
            result = await _recover_evicted_session(
                conversation_id,
                cwd=cwd,
                model=model,
                settings=settings,
            )
            if not result.get("ok"):
                await _emit_deferred_send_error(
                    conversation_id,
                    str(result.get("error") or "Session resume failed"),
                    error_type="session_resume_failed",
                    router=preserved_router,
                    turn_id=preserved_turn_id,
                )
                return

            session = _sessions.get(conversation_id)
            active_router = _routers.get(conversation_id) or preserved_router
            if not session:
                await _emit_deferred_send_error(
                    conversation_id,
                    "Session not found after resume",
                    error_type="session_resume_failed",
                    router=active_router,
                    turn_id=preserved_turn_id,
                )
                return

            desired_mode = _desired_session_mode(settings)
            known_mode = _session_modes.get(conversation_id, _DEFAULT_MODE)
            if desired_mode and desired_mode != known_mode:
                try:
                    applied_mode = await _apply_session_mode(session, settings=settings)
                except Exception as exc:
                    print(f"[CopilotSDK] Deferred retry mode.set failed: {exc}")
                    _add_to_raw_buffer("out", conversation_id, f"mode_set_error: {exc}")
                    await _emit_deferred_send_error(
                        conversation_id,
                        str(exc),
                        error_type="mode_set_failed",
                        router=active_router,
                        turn_id=preserved_turn_id,
                    )
                    return
                if applied_mode:
                    _session_modes[conversation_id] = applied_mode

            await session.send(text, attachments=[])
            _add_to_raw_buffer("out", conversation_id, "deferred_send_completed")
    except Exception as exc:
        print(f"[CopilotSDK] Deferred cold send failed: {exc}")
        _add_to_raw_buffer("out", conversation_id, f"deferred_send_error: {exc}")
        await _emit_deferred_send_error(
            conversation_id,
            str(exc),
            router=_routers.get(conversation_id) or preserved_router,
            turn_id=preserved_turn_id,
        )
    finally:
        current = asyncio.current_task()
        if current is not None and _deferred_send_tasks.get(conversation_id) is current:
            _deferred_send_tasks.pop(conversation_id, None)


def _schedule_deferred_cold_send(
    conversation_id: str,
    text: str,
    *,
    cwd: Optional[str],
    model: Optional[str],
    settings: SettingsDict,
) -> PayloadDict:
    task = asyncio.create_task(
        _complete_deferred_cold_send(
            conversation_id,
            text,
            cwd=cwd,
            model=model,
            settings=settings,
        ),
        name=f"copilot-deferred-send-{conversation_id[:8]}",
    )
    _deferred_send_tasks[conversation_id] = task
    _add_to_raw_buffer("out", conversation_id, "deferred_send_scheduled reason=session_resume")
    result: PayloadDict = {
        "ok": True,
        "session_id": conversation_id,
        "deferred": True,
        "resume_ack": "session.resume",
    }
    return result


def _session_event_paths(session_id: str) -> List[Path]:
    root = Path(_copilot_config_dir()) / "session-state"
    return [
        root / session_id / "events.jsonl",
        root / f"{session_id}.jsonl",
    ]


def _sanitize_session_attachments(session_id: str) -> PayloadDict:
    result: PayloadDict = {"session_id": session_id, "records_rewritten": 0, "paths": []}
    for path in _session_event_paths(session_id):
        if not path.is_file():
            continue
        tmp = path.with_name(f".{path.name}.tmp")
        rewritten = 0
        with path.open("r", encoding="utf-8", errors="ignore") as src, tmp.open("w", encoding="utf-8") as dst:
            for line in src:
                try:
                    record = cast(object, json.loads(line))
                except Exception:
                    dst.write(line)
                    continue
                if not isinstance(record, dict):
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
        current_rewritten = result.get("records_rewritten")
        result["records_rewritten"] = (
            current_rewritten if isinstance(current_rewritten, int) else 0
        ) + rewritten
        path_entries = cast(list[PayloadDict], result.setdefault("paths", []))
        path_entries.append({"path": str(path), "rewritten": rewritten})
    return result


def _runtime_option_descriptor(
    setting_key: str,
    label: str,
    options: List[Dict[str, str]],
    current: Optional[str],
    default: str,
) -> RuntimeOptionDescriptor:
    descriptor: RuntimeOptionDescriptor = {
        "settingKey": setting_key,
        "label": label,
        "options": [dict(item) for item in options],
        "current": current or "",
        "default": default,
    }
    return descriptor


def _json_safe(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(cast(dict[str, object], asdict(value)))
    if isinstance(value, Enum):
        return cast(object, value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, _SupportsToDict):
        return _json_safe(value.to_dict())
    value_dict = _object_mapping(value)
    if value_dict is not None:
        return {
            k: _json_safe(v)
            for k, v in value_dict.items()
            if not str(k).startswith("_")
        }
    return str(value)


def _load_settings_schema_template() -> PayloadDict:
    schema_path = Path(__file__).with_name("settings_schema.json")
    with schema_path.open("r", encoding="utf-8") as handle:
        loaded = cast(object, json.load(handle))
    return loaded if isinstance(loaded, dict) else {}


def _format_settings_datetime(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def _quota_info_unavailable(
    message: str,
    *,
    tone: str = "warning",
    detail: str = "",
) -> QuotaInfo:
    return {
        "text": message,
        "detail": detail,
        "tone": tone,
    }


def _quota_tone_from_remaining(remaining_percentage: Optional[float]) -> str:
    if remaining_percentage is None:
        return "success"
    if remaining_percentage <= 10.0:
        return "error"
    if remaining_percentage <= 25.0:
        return "warning"
    return "success"


def _format_quota_info(raw: object) -> QuotaInfo:
    payload = raw if isinstance(raw, dict) else {}
    snapshots_raw = payload.get("quota_snapshots")
    if not isinstance(snapshots_raw, dict):
        snapshots_raw = payload.get("quotaSnapshots")
    if not isinstance(snapshots_raw, dict) or not snapshots_raw:
        return _quota_info_unavailable(
            "Usage info unavailable.",
            tone="warning",
            detail="No quota snapshots were returned.",
        )

    lines: List[str] = []
    remaining_values: List[float] = []
    for quota_name, raw_snapshot in sorted(snapshots_raw.items()):
        snapshot = _json_safe(raw_snapshot)
        if not isinstance(snapshot, dict):
            continue
        remaining_percentage = snapshot.get("remaining_percentage")
        if remaining_percentage is None:
            remaining_percentage = snapshot.get("remainingPercentage")
        if isinstance(remaining_percentage, bool) or not isinstance(remaining_percentage, (int, float)):
            continue
        remaining = max(0.0, min(100.0, float(remaining_percentage)))
        remaining_values.append(remaining)
        label = str(quota_name or "quota").replace("_", " ").title()
        parts = [f"{label}: {remaining:.0f}% remaining"]

        used_requests = snapshot.get("used_requests")
        if used_requests is None:
            used_requests = snapshot.get("usedRequests")
        entitlement_requests = snapshot.get("entitlement_requests")
        if entitlement_requests is None:
            entitlement_requests = snapshot.get("entitlementRequests")
        if isinstance(used_requests, (int, float)) and not isinstance(used_requests, bool):
            if isinstance(entitlement_requests, (int, float)) and not isinstance(entitlement_requests, bool) and entitlement_requests > 0:
                parts.append(f"{float(used_requests):g}/{float(entitlement_requests):g} used")
            else:
                parts.append(f"{float(used_requests):g} used")

        overage = snapshot.get("overage")
        if isinstance(overage, (int, float)) and not isinstance(overage, bool) and float(overage) > 0:
            parts.append(f"{float(overage):g} overage")
        if snapshot.get("overage_allowed_with_exhausted_quota") is True or snapshot.get("overageAllowedWithExhaustedQuota") is True:
            parts.append("overage allowed")

        reset_text = _format_settings_datetime(snapshot.get("reset_date") or snapshot.get("resetDate"))
        if reset_text:
            parts.append(f"resets {reset_text}")
        lines.append("  •  ".join(parts))

    if not lines:
        return _quota_info_unavailable(
            "Usage info unavailable.",
            tone="warning",
            detail="No usable quota snapshots were returned.",
        )

    minimum_remaining = min(remaining_values) if remaining_values else None
    return {
        "text": (
            f"Usage remaining: {minimum_remaining:.0f}%"
            if minimum_remaining is not None
            else "Usage details available."
        ),
        "detail": "\n".join(lines),
        "tone": _quota_tone_from_remaining(minimum_remaining),
    }


async def _get_quota_info() -> QuotaInfo:
    try:
        client = await _ensure_client()
        rpc = getattr(client, "_rpc", None)
        account_api = getattr(rpc, "account", None)
        get_quota = getattr(account_api, "get_quota", None)
        if not callable(get_quota):
            return _quota_info_unavailable(
                "Usage info unavailable in this Copilot SDK build.",
                tone="warning",
            )
        quota_call = get_quota(timeout=15.0)
        quota_result = await quota_call if inspect.isawaitable(quota_call) else quota_call
    except Exception as exc:
        return _quota_info_unavailable(
            f"Failed to read Copilot usage info: {exc}",
            tone="error",
        )
    return _format_quota_info(_json_safe(quota_result))


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
    merged = _merge_runtime_settings(
        conversation_id or "",
        settings=settings,
    ) if conversation_id else dict(settings or {})
    approval_policy = _optional_string(merged.get("approval_policy"))
    sandbox_policy = _optional_string(merged.get("sandbox_policy"))
    web_policy = _optional_string(merged.get("web_policy"))
    return {
        "agent": extension_id,
        "approval": _runtime_option_descriptor(
            "approval_policy",
            "Approval Policy",
            _APPROVAL_POLICY_OPTIONS,
            approval_policy,
            _DEFAULT_APPROVAL_POLICY,
        ),
        "sandbox": _runtime_option_descriptor(
            "sandbox_policy",
            "Directory Trust",
            _SANDBOX_POLICY_OPTIONS,
            sandbox_policy,
            _DEFAULT_SANDBOX_POLICY,
        ),
        "web": _runtime_option_descriptor(
            "web_policy",
            "Web Access",
            _WEB_POLICY_OPTIONS,
            web_policy,
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


async def get_settings_schema(extension_id: str) -> PayloadDict:
    schema = _load_settings_schema_template()
    fields = schema.get("fields")
    usage_info = await _get_quota_info()
    schema["fields"] = [
        {
            "id": "information_section",
            "type": "section",
            "label": "Information",
            "description": "Live provider usage data from the active Copilot runtime.",
        },
        {
            "id": "usage_information",
            "type": "info",
            "label": "Usage",
            "text": usage_info.get("text") or "Usage info unavailable.",
            "detail": usage_info.get("detail") or "",
            "tone": usage_info.get("tone") or "warning",
        },
        *(list(fields) if isinstance(fields, list) else []),
    ]
    schema["cache"] = "none"
    return schema


async def read_plan(extension_id: str, conversation_id: str) -> PayloadDict:
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
        plan_doc_content = _string_or_empty(plan_doc_state.get("plan_content"))
        _plan_doc_signatures[conversation_id] = _plan_doc_signature(
            bool(plan_doc_state.get("plan_exists")),
            plan_doc_content,
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


async def get_request_card_schemas(extension_id: str) -> PayloadDict:
    return {
        ask_user_interactions.AGENT_PTY_ASK_USER_REQUEST_METHOD: {
            "request": {
                "type": "object",
                "properties": {
                    "requestId": {"type": "string"},
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "allowFreeform": {"type": "boolean"},
                },
                "required": ["question"],
                "additionalProperties": True,
            },
            "response": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["accept", "decline", "cancel"]},
                    "answer": {"type": "string"},
                    "answers": {"type": "array", "items": {"type": "string"}},
                    "selected_choice": {"type": "string"},
                    "freeform_answer": {"type": "string"},
                    "wasFreeform": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
        _COPILOT_PERMISSION_REQUEST_METHOD: {
            "request": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "warning": {"type": "string"},
                    "intention": {"type": "string"},
                    "path": {"type": "string"},
                    "cwd": {"type": "string"},
                    "diff": {"type": "string"},
                    "possible_paths": {"type": "array", "items": {"type": "string"}},
                    "possible_urls": {"type": "array", "items": {"type": "string"}},
                    "can_offer_session_approval": {"type": "boolean"},
                    "availableDecisions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "request": {"type": "object"},
                    "arguments": {},
                    "changes": {},
                },
                "additionalProperties": True,
            },
            "response": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["accept", "decline"]},
                    "kind": {"type": "string"},
                    "rules": {"type": "array"},
                    "feedback": {"type": "string"},
                    "message": {"type": "string"},
                    "path": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        _COPILOT_USER_INPUT_REQUEST_METHOD: {
            "request": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "allowFreeform": {"type": "boolean"},
                },
                "required": ["question"],
                "additionalProperties": True,
            },
            "response": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "wasFreeform": {"type": "boolean"},
                },
                "required": ["answer", "wasFreeform"],
                "additionalProperties": True,
            },
        },
    }


def resolve_approval(request_id: str, resolution: object) -> bool:
    """Called from WS handler when user responds to an approval request."""
    fut = _pending_approvals.pop(request_id, None)
    request_spec = _pending_request_specs.pop(request_id, None)
    if fut and not fut.done():
        request_type = request_spec.get("type") if isinstance(request_spec, dict) else "permission"
        if request_type == "user_input":
            fut.set_result(_normalize_user_input_resolution(resolution, request_spec=request_spec))
        else:
            fut.set_result(_normalize_permission_resolution(resolution))
        return True
    return False


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: ApprovalDescriptor) -> bool:
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


def _make_permission_handler(conversation_id: str) -> _PermissionHandlerFn:
    """
    Create a permission handler for a session.

    Respects approval_policy from conversation settings:
      - auto-approve: silently approve everything
      - suggest: broadcast to frontend, auto-approve on timeout (120s)
      - always-ask: broadcast to frontend, wait indefinitely
    """
    async def handler(
        request: PermissionRequest,
        context: RequestContext,
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
        tool_info = cast(PayloadDict, router.tool_calls.get(tool_call_id, {}) if router else {})

        # Build the payload the frontend expects
        payload: PayloadDict = {"kind": kind}
        command = tool_info.get("title", "")
        if command:
            payload["command"] = str(command)
        # Include tool name and raw arguments so frontend can render diffs
        tool_name = tool_info.get("tool_name", "")
        if tool_name:
            payload["tool_name"] = str(tool_name)
        raw_args = tool_info.get("arguments")
        normalized_args: object = raw_args
        if isinstance(raw_args, str):
            try:
                normalized_args = cast(object, json.loads(raw_args))
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
            payload.update(build_file_change_preview(normalized_args))

        # Create a Future that the WS handler will resolve
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[object] = loop.create_future()
        _pending_approvals[request_id] = fut
        request_params = _build_permission_request_params(kind, request_fields, payload)
        _pending_request_specs[request_id] = {
            "type": "permission",
            "request_method": _COPILOT_PERMISSION_REQUEST_METHOD,
            "request_params": request_params,
        }

        runtime_signature = _runtime_signatures.get(conversation_id) or _runtime_signature(conversation_id)
        session = _sessions.get(conversation_id)
        approval_event: PayloadDict = {
            "type": "approval",
            "conversation_id": conversation_id,
            "id": request_id,
            "request_id": request_id,
            "kind": kind,
            "tool_call_id": tool_call_id,
            "turn_id": router.current_turn_id if router else "",
            "request_method": _COPILOT_PERMISSION_REQUEST_METHOD,
            "request_params": request_params,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        subagent_id = tool_info.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id:
            approval_event["subagent_id"] = subagent_id
        session_thread_id = _optional_string(getattr(session, "session_id", None))
        turn_id = _string_or_empty(router.current_turn_id if router else "")
        transcript_anchor: PayloadDict = {"turn_id": turn_id}
        created_at = _string_or_empty(approval_event.get("created_at"))
        descriptor: ApprovalDescriptor = {}
        descriptor["request_id"] = request_id
        descriptor["agent"] = "copilot-sdk"
        descriptor["kind"] = kind
        descriptor["payload"] = payload
        descriptor["request_method"] = _COPILOT_PERMISSION_REQUEST_METHOD
        descriptor["request_params"] = request_params
        descriptor["thread_id"] = session_thread_id
        descriptor["turn_id"] = turn_id
        descriptor["runtime_signature"] = runtime_signature
        descriptor["runtime_instance_id"] = session_thread_id
        descriptor["transcript_anchor"] = transcript_anchor
        descriptor["source"] = "live"
        descriptor["created_at"] = created_at
        descriptor["render_event"] = approval_event
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
                _pending_request_specs.pop(request_id, None)
                _remove_pending_approval(conversation_id, request_id)
                print(f"[CopilotSDK] Approval timeout for {request_id}, auto-approving")
                permission_result = PermissionRequestResult(kind="approved", rules=[])

        return _normalize_permission_resolution(permission_result)

    return handler


def _make_user_input_handler(conversation_id: str) -> UserInputHandler:
    async def handler(
        request: UserInputRequest,
        context: RequestContext,
    ) -> UserInputResponse:
        request_params = _build_user_input_request_params(request)
        request_choices = request_params.get("choices")
        normalized_choices = [str(item) for item in request_choices] if isinstance(request_choices, list) else []
        response_request_spec: PendingRequestSpec = {"choices": normalized_choices}
        request_id = f"user_input_{conversation_id[:8]}_{id(request)}"
        router = _routers.get(conversation_id)
        runtime_signature = _runtime_signatures.get(conversation_id) or _runtime_signature(conversation_id)
        session = _sessions.get(conversation_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[object] = loop.create_future()
        _pending_approvals[request_id] = fut
        _pending_request_specs[request_id] = {
            "type": "user_input",
            "request_method": _COPILOT_USER_INPUT_REQUEST_METHOD,
            "request_params": request_params,
            "choices": normalized_choices,
        }

        payload: PayloadDict = {
            "kind": "user_input",
            **request_params,
        }
        approval_event: PayloadDict = {
            "type": "approval",
            "conversation_id": conversation_id,
            "id": request_id,
            "request_id": request_id,
            "kind": "user_input",
            "turn_id": router.current_turn_id if router else "",
            "request_method": _COPILOT_USER_INPUT_REQUEST_METHOD,
            "request_params": request_params,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        session_thread_id = _optional_string(getattr(session, "session_id", None))
        turn_id = _string_or_empty(router.current_turn_id if router else "")
        transcript_anchor: PayloadDict = {"turn_id": turn_id}
        created_at = _string_or_empty(approval_event.get("created_at"))
        descriptor: ApprovalDescriptor = {}
        descriptor["request_id"] = request_id
        descriptor["agent"] = "copilot-sdk"
        descriptor["kind"] = "user_input"
        descriptor["payload"] = payload
        descriptor["request_method"] = _COPILOT_USER_INPUT_REQUEST_METHOD
        descriptor["request_params"] = request_params
        descriptor["thread_id"] = session_thread_id
        descriptor["turn_id"] = turn_id
        descriptor["runtime_signature"] = runtime_signature
        descriptor["runtime_instance_id"] = session_thread_id
        descriptor["transcript_anchor"] = transcript_anchor
        descriptor["source"] = "live"
        descriptor["created_at"] = created_at
        descriptor["render_event"] = approval_event
        _upsert_pending_approval(conversation_id, descriptor)

        if _broadcast_fn:
            await _broadcast_fn(approval_event)

        result = await fut
        return _normalize_user_input_resolution(result, request_spec=response_request_spec)

    return handler


async def _start_mcp_ask_user_request(conversation_id: str, event: SessionEvent) -> None:
    request_id = conversation_id.strip()
    if not request_id:
        return
    data = event.data
    tool_call_id = _optional_string(getattr(data, "tool_call_id", None)) or _optional_string(event.id) or request_id
    arguments = _coerce_copilot_tool_arguments(cast(object, getattr(data, "arguments", None)))
    question = str(arguments.get("question") or "").strip()
    choices = ask_user_interactions.normalize_choices(arguments.get("choices"))
    allow_freeform = bool(arguments.get("allow_freeform", arguments.get("allowFreeform", True)))
    if not question:
        return

    _pending_mcp_ask_user_tools.setdefault(conversation_id, {})[tool_call_id] = request_id

    existing_future = _pending_approvals.get(request_id)
    if isinstance(existing_future, asyncio.Future) and not existing_future.done():
        return

    loop = asyncio.get_running_loop()
    _pending_approvals[request_id] = loop.create_future()
    request_params: PayloadDict = {
        "requestId": request_id,
        "question": question,
        "choices": list(choices),
        "allowFreeform": allow_freeform,
    }
    _pending_request_specs[request_id] = {
        "type": "user_input",
        "request_method": ask_user_interactions.AGENT_PTY_ASK_USER_REQUEST_METHOD,
        "request_params": request_params,
        "choices": list(choices),
    }

    router = _routers.get(conversation_id)
    session = _sessions.get(conversation_id)
    runtime_signature = _runtime_signatures.get(conversation_id) or _runtime_signature(conversation_id)
    turn_id = _string_or_empty(router.current_turn_id if router else "")
    created_at = datetime.now(timezone.utc).isoformat()
    payload: PayloadDict = {
        "requestId": request_id,
        "question": question,
        "choices": list(choices),
        "allowFreeform": allow_freeform,
        "message": question,
        "tool_call_id": tool_call_id,
    }
    approval_event: PayloadDict = {
        "type": "approval",
        "conversation_id": conversation_id,
        "id": request_id,
        "request_id": request_id,
        "card_id": tool_call_id,
        "kind": "user_input",
        "tool_call_id": tool_call_id,
        "turn_id": turn_id,
        "request_method": ask_user_interactions.AGENT_PTY_ASK_USER_REQUEST_METHOD,
        "request_params": request_params,
        "payload": payload,
        "created_at": created_at,
    }
    session_thread_id = _optional_string(getattr(session, "session_id", None))
    transcript_anchor: PayloadDict = {"turn_id": turn_id}
    descriptor: ApprovalDescriptor = {}
    descriptor["request_id"] = request_id
    descriptor["agent"] = "copilot-sdk"
    descriptor["kind"] = "user_input"
    descriptor["payload"] = payload
    descriptor["request_method"] = ask_user_interactions.AGENT_PTY_ASK_USER_REQUEST_METHOD
    descriptor["request_params"] = request_params
    descriptor["thread_id"] = session_thread_id
    descriptor["turn_id"] = turn_id
    descriptor["runtime_signature"] = runtime_signature
    descriptor["runtime_instance_id"] = session_thread_id
    descriptor["transcript_anchor"] = transcript_anchor
    descriptor["source"] = "live"
    descriptor["created_at"] = created_at
    descriptor["render_event"] = approval_event
    _upsert_pending_approval(conversation_id, descriptor)

    if _broadcast_fn:
        await _broadcast_fn(approval_event)


def _mcp_ask_user_completion_resolution(event: SessionEvent) -> PayloadDict:
    data = event.data
    result_obj = getattr(data, "result", None)
    normalized_result = _json_safe_sdk_value(cast(object, result_obj))
    if isinstance(normalized_result, dict):
        return cast(PayloadDict, normalized_result)

    error_reason = str(getattr(data, "error_reason", "") or "").strip()
    error_value = getattr(data, "error", None)
    if error_reason or error_value not in (None, "", {}):
        resolution: PayloadDict = {"status": "error"}
        if error_reason:
            resolution["error"] = error_reason
        elif isinstance(error_value, str):
            resolution["error"] = error_value
        elif error_value not in (None, "", {}):
            resolution["error"] = str(error_value)
        return resolution

    content = ""
    if isinstance(result_obj, str):
        content = result_obj
    elif isinstance(getattr(data, "content", None), str):
        content = cast(str, getattr(data, "content"))
    elif isinstance(getattr(data, "output", None), str):
        content = cast(str, getattr(data, "output"))
    if content.strip():
        return {"answer": content.strip()}
    return {"action": "cancel"}


async def _complete_mcp_ask_user_request(conversation_id: str, event: SessionEvent) -> None:
    data = event.data
    tool_call_id = (
        _optional_string(getattr(data, "tool_call_id", None))
        or (_optional_string(event.parent_id) or _optional_string(event.id))
        or ""
    )
    tool_calls = _pending_mcp_ask_user_tools.get(conversation_id)
    request_id = tool_calls.pop(tool_call_id, None) if isinstance(tool_calls, dict) else None
    if isinstance(tool_calls, dict) and not tool_calls:
        _pending_mcp_ask_user_tools.pop(conversation_id, None)
    resolved_request_id = request_id or conversation_id.strip()
    if not resolved_request_id:
        return

    pending_future = _pending_approvals.pop(resolved_request_id, None)
    if isinstance(pending_future, asyncio.Future) and not pending_future.done():
        pending_future.cancel()
    _pending_request_specs.pop(resolved_request_id, None)

    await ask_user_interactions.finalize_interaction(
        resolved_request_id,
        _mcp_ask_user_completion_resolution(event),
    )


async def _handle_mcp_ask_user_event(conversation_id: str, event: SessionEvent) -> bool:
    etype = event.type
    data = event.data
    if etype == SessionEventType.TOOL_EXECUTION_START:
        if not _is_copilot_mcp_ask_user_event(data):
            return False
        await _start_mcp_ask_user_request(conversation_id, event)
        return True

    tool_call_id = (
        _optional_string(getattr(data, "tool_call_id", None))
        or (_optional_string(event.parent_id) or _optional_string(event.id))
        or ""
    )
    active_tools = _pending_mcp_ask_user_tools.get(conversation_id) or {}
    is_known_tool = bool(tool_call_id and tool_call_id in active_tools)

    if etype in {SessionEventType.TOOL_EXECUTION_PROGRESS, SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT}:
        return is_known_tool

    if etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
        if not is_known_tool and not _is_copilot_mcp_ask_user_event(data):
            return False
        await _complete_mcp_ask_user_request(conversation_id, event)
        return True

    return False


# ── Pre-tool-use hook (sandbox + web policy) ────────────────────────

# Tool names known to perform web/network access
_WEB_TOOLS = {"web_search", "web_fetch", "fetch_url", "curl", "wget", "http_request"}

# Tool names known to perform file operations
_FILE_TOOLS = {"edit", "create", "write", "read_file", "write_file", "delete", "move",
               "bash", "shell", "exec", "run_command", "apply_patch"}


def _make_pre_tool_use_hook(conversation_id: str) -> PreToolUseHandler:
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
        input: PreToolUseHookInput,
        context: RequestContext,
    ) -> PreToolUseHookOutput | None:
        tool_name = input.get("toolName")
        tool_args = input.get("toolArgs") or {}
        settings = _get_conversation_settings(conversation_id)
        normalized_tool_name = str(tool_name or "")

        # ── Web policy check ──
        web_policy = settings.get("web_policy", _DEFAULT_WEB_POLICY)
        if normalized_tool_name.lower() in _WEB_TOOLS or any(
            word in normalized_tool_name.lower() for word in ("web", "fetch", "url", "http")
        ):
            if web_policy == "deny":
                print(f"[CopilotSDK] Web tool '{normalized_tool_name}' denied by web_policy convo={conversation_id[:8]}")
                return {"permissionDecision": "deny", "permissionDecisionReason": "Web access denied by policy"}
            elif web_policy == "ask":
                return {"permissionDecision": "ask"}
            # "allow" → fall through

        # ── Sandbox / directory trust check ──
        sandbox_policy = settings.get("sandbox_policy", _DEFAULT_SANDBOX_POLICY)
        if sandbox_policy != "allow-all-paths" and normalized_tool_name.lower() in _FILE_TOOLS:
            cwd = _optional_string(settings.get("cwd")) or os.path.expanduser("~")
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
        router = _routers.get(conversation_id)
        debug_payload = _serialize_session_event(event) if router and router.debug_trace else None
        _add_to_raw_buffer(
            "in",
            conversation_id,
            f"{event.type.value}: {str(event.data)[:200]}",
            payload=debug_payload,
            category="session_event",
        )
        if router:
            # Schedule the coroutine on the running event loop
            try:
                _ensure_event_worker(conversation_id).put_nowait(event)
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
    The FWS getter is used for the observed raw-pipe Copilot CLI transport.
    """
    global _broadcast_fn, _transcript_fn, _meta_fns, _initialized, _fws_getter
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns or {}
    _fws_getter = fws_getter
    _initialized = True
    print("[CopilotSDK] Manager initialized")


async def _ensure_client() -> CopilotClient:
    """Get or create the global CopilotClient singleton."""
    global _client, _copilot_fws_process
    async with _get_client_lock():
        if _client is None:
            client_config = SubprocessConfig(
                use_stdio=True,
                log_level="info",
                cwd=_copilot_shell_cwd(),
            )
            cli_path = _resolve_external_copilot_cli_path()
            if _fws_getter is not None:
                shell_id = await _get_or_start_copilot_shell()
                mgr = await _fws_getter()
                _copilot_fws_process = await FrameworkShellPipeProcess.create(
                    mgr,
                    shell_id,
                    asyncio.get_running_loop(),
                )
                client_config.process = _copilot_fws_process
                launch_mode = f"fws:{shell_id}"
            else:
                if cli_path:
                    client_config.cli_path = cli_path
                launch_mode = cli_path or "bundled/default"
            _client = CopilotClient(client_config, auto_start=True)
            try:
                await _client.start()
            except Exception:
                _client = None
                if _copilot_fws_process is not None:
                    with contextlib.suppress(Exception):
                        await _copilot_fws_process.aclose()
                    _copilot_fws_process = None
                raise
            print(f"[CopilotSDK] Client started, state={_client.get_state()} transport={launch_mode}")
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

    if _ready_event and _ready_event.is_set() and _client is not None:
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
        _ready_event = None
        return False


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """Warm up the Copilot SDK client."""
    result = await warm_up_extension("copilot-sdk", timeout=timeout)
    return {"copilot-sdk": result}


def is_extension_ready(extension_id: str) -> bool:
    return _ready_event.is_set() if _ready_event else False


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    return await warm_up_extension(extension_id, timeout=timeout)


# ── Session management ──────────────────────────────────────────────


def has_session(conversation_id: str) -> bool:
    return conversation_id in _sessions


async def init_session(
    conversation_id: str,
    extension_id: str,
    cwd: Optional[str],
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
    async with _get_session_lock(conversation_id):
        return await _init_session_unlocked(conversation_id, extension_id, cwd, settings=settings)


async def _init_session_unlocked(
    conversation_id: str,
    extension_id: str,
    cwd: Optional[str],
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
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
            broadcast_fn=_resolved_broadcast_fn(),
            transcript_fn=_resolved_transcript_fn(),
            debug_trace=_copilot_debug_trace_enabled(
                conversation_id,
                settings=settings,
                cwd=cwd,
            ),
            plan_state_provider=_build_live_plan_state,
            initial_model=_optional_string(_merge_runtime_settings(conversation_id, settings=settings, cwd=cwd).get("model")),
            model_context_window_resolver=_context_window_for_model,
        )
        _routers[conversation_id] = router

        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
        )
        create_config = _build_create_session_config(config)
        _log_runtime_config("create_session", conversation_id, config)

        # Let SDK generate its own session_id (don't pass ours)
        session = await client.create_session(**create_config)
        sdk_session_id = session.session_id
        _sessions[conversation_id] = session
        _runtime_signatures[conversation_id] = runtime_signature
        _session_modes[conversation_id] = _DEFAULT_MODE

        # Subscribe to events
        _replace_session_subscription(conversation_id, session)

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
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
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
    settings: Optional[SettingsDict] = None,
) -> PayloadDict:
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
        if isinstance(sanitize_result.get("records_rewritten"), int) and sanitize_result.get("records_rewritten"):
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
            broadcast_fn=_resolved_broadcast_fn(),
            transcript_fn=_resolved_transcript_fn(),
            debug_trace=_copilot_debug_trace_enabled(
                conversation_id,
                settings=settings,
                cwd=cwd,
                model=model,
            ),
            plan_state_provider=_build_live_plan_state,
            initial_model=_optional_string(_merge_runtime_settings(conversation_id, settings=settings, cwd=cwd, model=model).get("model")),
            model_context_window_resolver=_context_window_for_model,
        )
        _routers[conversation_id] = router

        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=model,
        )
        resume_config = _build_resume_session_config(config)
        _log_runtime_config("resume_session", conversation_id, config)

        # Resume using the real SDK session ID
        session = await resume_sdk_session(client, sdk_session_id, resume_config)
        # Key in-memory by our conversation_id
        _sessions[conversation_id] = session
        _runtime_signatures[conversation_id] = runtime_signature
        _session_modes[conversation_id] = _desired_session_mode(settings) or _DEFAULT_MODE

        _replace_session_subscription(conversation_id, session)

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
    config: ResumeSessionConfig,
    assumed_mode: Optional[str] = None,
    debug_trace: bool = False,
) -> tuple[CopilotSession, bool]:
    """
    Attach to a known SDK session id without issuing session.resume first.

    This preserves the send-first contract: try the turn against the persisted
    session id, and only fall back to session.resume if the CLI reports that the
    session is not currently loaded.
    """
    router = CopilotEventRouter(
        conversation_id=conversation_id,
        broadcast_fn=_resolved_broadcast_fn(),
        transcript_fn=_resolved_transcript_fn(),
        debug_trace=debug_trace,
        plan_state_provider=_build_live_plan_state,
        initial_model=(config.get("model") if isinstance(config.get("model"), str) else None),
        model_context_window_resolver=_context_window_for_model,
    )
    _routers[conversation_id] = router

    cold_bound_session = False
    with client._sessions_lock:  # type: ignore[attr-defined]
        session = client._sessions.get(sdk_session_id)  # type: ignore[attr-defined]
        if session is None:
            session = CopilotSession(sdk_session_id, client._client)  # type: ignore[attr-defined]
            client._sessions[sdk_session_id] = session  # type: ignore[attr-defined]
            cold_bound_session = True
    session._register_permission_handler(config.get("on_permission_request"))
    hooks = config.get("hooks")
    if hooks:
        session._register_hooks(hooks)
    on_user_input_request = config.get("on_user_input_request")
    if on_user_input_request:
        session._register_user_input_handler(on_user_input_request)

    _sessions[conversation_id] = session
    _runtime_signatures[conversation_id] = runtime_signature
    _session_modes[conversation_id] = assumed_mode or _DEFAULT_MODE
    _replace_session_subscription(conversation_id, session)
    attach_mode = "cold" if cold_bound_session else "hot"
    _add_to_raw_buffer("out", conversation_id, f"session_attached sdk={sdk_session_id[:8]} mode={attach_mode}")
    return session, cold_bound_session


# ── Message handling ────────────────────────────────────────────────

async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: SettingsDict,
) -> PayloadDict:
    """
    Handle a user message for a Copilot SDK conversation.
    
    Main entry point called by server.py extension router.
    Follows the codex pattern: lazy resume on first message, not on conversation select.
    """
    await _await_deferred_send(conversation_id)
    async with _get_session_lock(conversation_id):
        if not _broadcast_fn or not _transcript_fn:
            return {"ok": False, "error": "Manager not initialized"}

        cwd = _optional_string(settings.get("cwd")) or os.path.expanduser("~")
        model = _optional_string(settings.get("model"))
        desired_signature = _runtime_signature(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=model,
        )
        config = _build_session_runtime_config(
            conversation_id,
            settings=settings,
            cwd=cwd,
            model=model,
        )
        resume_config = _build_resume_session_config(config)

        cold_bound_session = False

        # Ensure session exists — attach/send first for known sessions, init for new ones
        if conversation_id not in _sessions:
            # Check if this conversation already has a thread_id.
            thread_id = None
            result: PayloadDict = {"ok": True}
            if _meta_fns and "load" in _meta_fns:
                meta = _meta_fns["load"](conversation_id)
                if meta:
                    thread_id = meta.get("thread_id")

            if thread_id:
                client = await _ensure_client()
                _, cold_bound_session = _register_attached_session(
                    client,
                    conversation_id,
                    str(thread_id),
                    desired_signature,
                    resume_config,
                    assumed_mode=_desired_session_mode(settings) or _DEFAULT_MODE,
                    debug_trace=_copilot_debug_trace_enabled(
                        conversation_id,
                        settings=settings,
                        cwd=cwd,
                        model=model,
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
                model=model,
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
                model=model,
            )
        )
        desired_mode = _desired_session_mode(settings)
        known_mode = _session_modes.get(conversation_id)
        if known_mode is None:
            known_mode = desired_mode or _DEFAULT_MODE
            _session_modes[conversation_id] = known_mode
        applied_mode = None
        if desired_mode and desired_mode != known_mode and not cold_bound_session:
            try:
                applied_mode = await _apply_session_mode(session, settings=settings)
            except Exception as e:
                if not _is_session_not_found_error(e):
                    print(f"[CopilotSDK] mode.set failed: {e}")
                    _add_to_raw_buffer("out", conversation_id, f"mode_set_error: {e}")
                    return {"ok": False, "error": str(e)}

                result = await _recover_evicted_session(
                    conversation_id,
                    cwd=cwd,
                    model=model,
                    settings=settings,
                )
                if not result.get("ok"):
                    print(f"[CopilotSDK] Re-resume after mode.set failed: {result}")
                    return result

                session = _sessions.get(conversation_id)
                router = _routers.get(conversation_id)
                if not session or not router:
                    return {"ok": False, "error": "Session not found after mode re-resume"}
                known_mode = _session_modes.get(conversation_id, _DEFAULT_MODE)
                if desired_mode and desired_mode != known_mode:
                    try:
                        applied_mode = await _apply_session_mode(session, settings=settings)
                    except Exception as retry_error:
                        print(f"[CopilotSDK] Retry mode.set failed: {retry_error}")
                        _add_to_raw_buffer("out", conversation_id, f"mode_set_error: {retry_error}")
                        return {"ok": False, "error": str(retry_error)}
        if applied_mode:
            settings["mode"] = applied_mode
            _session_modes[conversation_id] = applied_mode

        turn_token = uuid4().hex

        # Notify router of turn start
        await router.on_turn_start(text, turn_token=turn_token)

        try:
            _add_to_raw_buffer("out", conversation_id, f"prompt: {text[:200]}")

            # Fire-and-forget send — events come via session.on() handler
            await session.send(
                text,
                attachments=[],
            )

            return {"ok": True, "session_id": conversation_id}

        except Exception as e:
            err_msg = str(e)
            # SDK binary evicts inactive sessions — catch and retry via resume
            if _is_session_not_found_error(e):
                return _schedule_deferred_cold_send(
                    conversation_id,
                    text,
                    cwd=cwd,
                    model=model,
                    settings=dict(settings),
                )

            print(f"[CopilotSDK] send failed: {e}")
            _add_to_raw_buffer("out", conversation_id, f"send_error: {e}")
            return {"ok": False, "error": err_msg}


# ── Model listing ───────────────────────────────────────────────────

async def list_models() -> list[PayloadDict]:
    """List available models from the Copilot CLI."""
    try:
        client = await _ensure_client()
        models = await client.list_models()
        def _normalize_reasoning_efforts(model: object) -> list[str] | None:
            explicit = getattr(model, "supported_reasoning_efforts", None)
            supports = getattr(getattr(model, "capabilities", None), "supports", None)
            raw_supported = getattr(supports, "reasoning_effort", None)
            ordered: list[str] = []
            for raw in (explicit, raw_supported):
                if not isinstance(raw, list):
                    continue
                for item in raw:
                    if not isinstance(item, str):
                        continue
                    value = item.strip().lower()
                    if not value or value == "none" or value in ordered:
                        continue
                    ordered.append(value)
            return ordered or None

        return [
            {
                "id": m.id,
                "name": getattr(m, "name", m.id),
                "billing": _json_safe(getattr(m, "billing", None)),
                "capabilities": _json_safe(getattr(m, "capabilities", None)),
                "policy": _json_safe(getattr(m, "policy", None)),
                "supported_reasoning_efforts": _json_safe(_normalize_reasoning_efforts(m)),
                "default_reasoning_effort": _json_safe(getattr(m, "default_reasoning_effort", None)),
            }
            for m in models
        ]
    except Exception as e:
        print(f"[CopilotSDK] list_models failed: {e}")
        return []


# ── Session listing ─────────────────────────────────────────────────

async def list_sessions(cwd: Optional[str] = None) -> list[PayloadDict]:
    """
    List all known sessions from the Copilot CLI.
    
    If cwd is provided, sessions are sorted with CWD-matching sessions first.
    """
    try:
        client = await _ensure_client()
        sessions = await client.list_sessions()
        
        result: list[PayloadDict] = []
        for s in sessions:
            entry: PayloadDict = {
                "session_id": s.sessionId,
                "start_time": s.startTime,
                "modified_time": s.modifiedTime,
                "is_remote": s.isRemote,
                "summary": s.summary,
            }
            # Check if session has context (newer SDK versions)
            ctx = s.context
            if ctx:
                entry["context"] = {
                    "cwd": ctx.cwd,
                    "git_root": ctx.gitRoot,
                    "repository": ctx.repository,
                    "branch": ctx.branch,
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
    settings: Optional[SettingsDict] = None,
    extension_id: Optional[str] = None,
) -> PayloadDict:
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
    settings: Optional[SettingsDict] = None,
) -> list[PayloadDict]:
    """
    Build flat transcript entries from an existing SDK session's history.

    This is the SDK equivalent of Codex rollout transcript import.
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

    items: list[PayloadDict] = []
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
                result_mapping = _object_mapping(cast(object, result_obj)) if result_obj is not None else None
                if result_mapping is not None:
                    content = _string_or_empty(result_mapping.get("content"))
                    detailed = _string_or_empty(result_mapping.get("detailed_content"))
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

    await _stop_event_worker(conversation_id)
    await _stop_todo_watch(conversation_id)

    session = _sessions.pop(conversation_id, None)
    _routers.pop(conversation_id, None)
    _runtime_signatures.pop(conversation_id, None)
    _session_modes.pop(conversation_id, None)
    _plan_doc_state.pop(conversation_id, None)
    _debug_raw_entry_counters.pop(conversation_id, None)
    _pending_mcp_ask_user_tools.pop(conversation_id, None)

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
    global _client, _copilot_fws_process
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
    if _copilot_fws_process is not None:
        with contextlib.suppress(Exception):
            await _copilot_fws_process.aclose()
        _copilot_fws_process = None
    await _stop_copilot_shell()


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


# ── Compact ─────────────────────────────────────────────────────────

async def compact_session(conversation_id: str) -> PayloadDict:
    """Compact/condense the context window for a copilot-sdk session."""
    session = _sessions.get(conversation_id)
    if not session:
        return {"ok": False, "error": "no active session for conversation"}
    try:
        result = await compact_sdk_session(session)
        success = getattr(result, "success", None)
        print(f"[CopilotSDK] Compacted: {conversation_id[:8]} success={success}")
        return {"ok": bool(success), "conversation_id": conversation_id}
    except Exception as e:
        print(f"[CopilotSDK] compact error: {e}")
        return {"ok": False, "error": str(e), "conversation_id": conversation_id}


# ── Shutdown alias (for server.py lifespan) ─────────────────────────

shutdown_client = stop_client

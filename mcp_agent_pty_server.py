#!/usr/bin/env python3
import asyncio
import difflib
import hashlib
import json
import os
import sys
import secrets
import contextlib
import re
import tomllib
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Optional, Protocol, cast

import socketio  # type: ignore[reportMissingTypeStubs]

from als_deprecated.markdown_sections import SectionNode, normalize_heading as _normalize_heading, parse_markdown
from als_deprecated.ipc_auth import load_or_create_ipc_secret
from als_deprecated.socketio_config import socketio_client_kwargs
from als_deprecated import conversation_todos as _conv_todos
from als_deprecated.typing_helpers import ObjectList, ObjectMap, coerce_object_list, coerce_object_map


def _ensure_framework_shells_secret() -> None:
    """Derive a stable secret from cwd/repo root if not already set."""
    # Prefer SIGWINCH delivery after resize_pty() for dtach-backed PTYs.
    os.environ.setdefault("FRAMEWORK_SHELLS_SIGWINCH_ON_RESIZE", "1")
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    repo_root = os.path.abspath(os.path.dirname(__file__))
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

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import ToolAnnotations


class _UrlopenResponse(Protocol):
    status: int

    def read(self) -> bytes: ...

    def __enter__(self) -> "_UrlopenResponse": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...


class _SocketIOAsyncClient(Protocol):
    connected: bool

    def on(self, event: str, handler: object, *, namespace: str | None = None) -> object: ...

    def connect(
        self,
        url: str,
        *,
        auth: ObjectMap | None = None,
        namespaces: list[str] | None = None,
        transports: list[str] | None = None,
        wait_timeout: float | int | None = None,
    ) -> Awaitable[object]: ...

    def disconnect(self) -> Awaitable[object]: ...

    def call(
        self,
        event: str,
        data: object = None,
        *,
        namespace: str | None = None,
        timeout: float | int | None = None,
    ) -> Awaitable[object]: ...

    def emit(
        self,
        event: str,
        data: object = None,
        *,
        namespace: str | None = None,
    ) -> Awaitable[object]: ...


class _SocketIOModule(Protocol):
    def AsyncClient(self, **kwargs: object) -> _SocketIOAsyncClient: ...


_socketio = cast(_SocketIOModule, socketio)


def _json_loads_object(text: str) -> object:
    return cast(object, json.loads(text))


def _json_loads_object_map(text: str) -> ObjectMap:
    return coerce_object_map(_json_loads_object(text))


def _json_loads_object_list(text: str) -> ObjectList:
    return coerce_object_list(_json_loads_object(text))


def _read_urlopen_response_body(response: _UrlopenResponse) -> str:
    return response.read().decode("utf-8")


def _coerce_optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return int(value.strip())
    return None


def _logical_abspath(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _launch_cwd() -> Path:
    raw = os.environ.get("PWD")
    if isinstance(raw, str) and raw.strip():
        logical = _logical_abspath(raw.strip())
        if logical.is_dir():
            return logical
    return _logical_abspath(os.getcwd())


def _project_root_search_starts() -> list[Path]:
    starts: list[Path] = []
    logical = _launch_cwd()
    starts.append(logical)
    actual = _logical_abspath(os.getcwd())
    if actual not in starts:
        starts.append(actual)
    return starts


def _find_project_root(start: Optional[Path | str] = None) -> Path:
    """Walk up from the harness cwd to find the directory containing .agent-pty.toml."""
    start_path = _logical_abspath(start or _launch_cwd())
    cur = start_path
    while True:
        if (cur / ".agent-pty.toml").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return start_path


def _current_project_root() -> Path:
    return _find_project_root(_project_root_search_starts()[0])


def _current_project_roots() -> list[Path]:
    roots: list[Path] = []
    for start in _project_root_search_starts():
        root = _find_project_root(start)
        if root not in roots:
            roots.append(root)
    return roots


def _load_project_config(root: Optional[Path] = None) -> ObjectMap:
    """Load .agent-pty.toml from a directory (defaults to the current project root)."""
    base = _logical_abspath(root) if root is not None else _current_project_root()
    config_path = base / ".agent-pty.toml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return coerce_object_map(cast(object, tomllib.load(f)))
    except Exception:
        return {}


def _kb_configured_files(root: Optional[Path] = None) -> list[str]:
    """Return the list of knowledge files from config, validated."""
    knowledge = coerce_object_map(_load_project_config(root).get("knowledge"))
    raw = knowledge.get("files", [])
    if isinstance(raw, str):
        raw = [raw]
    result: list[str] = []
    for f in cast(list[object], raw) if isinstance(raw, list) else []:
        if not isinstance(f, str):
            continue
        p = Path(f)
        # Security: no absolute paths, no .. traversal
        if p.is_absolute() or ".." in p.parts:
            continue
        result.append(str(p))
    return result


_APPSERVER_ORIGIN = os.environ.get("AGENT_LOG_SERVER_ORIGIN", "").strip()
_APPSERVER_IPC_NAMESPACE = "/ipc"
_SIDEBAR_DRAFTS_LIST_METHOD = "sidebar.drafts.list"
_SIDEBAR_DRAFT_STATE_GET_METHOD = "sidebar.draftState.get"
_SIDEBAR_DRAFT_CLEAR_METHOD = "sidebar.draft.clear"
_appserver_ipc_sio: Optional[_SocketIOAsyncClient] = None
_appserver_ipc_lock = asyncio.Lock()
_ask_user_pending_requests: dict[str, asyncio.Future[ObjectMap]] = {}


async def _get_appserver_ipc_sio() -> _SocketIOAsyncClient:
    global _appserver_ipc_sio
    async with _appserver_ipc_lock:
        if not _APPSERVER_ORIGIN:
            raise RuntimeError("AGENT_LOG_SERVER_ORIGIN is required for agent-pty-blocks IPC")
        if _appserver_ipc_sio and _appserver_ipc_sio.connected:
            return _appserver_ipc_sio
        if _appserver_ipc_sio:
            with contextlib.suppress(Exception):
                await _appserver_ipc_sio.disconnect()
            _appserver_ipc_sio = None

        client_kwargs = coerce_object_map(cast(object, socketio_client_kwargs()))
        client = _socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=3,
            **client_kwargs,
        )

        async def _on_ask_user_response(data: object) -> None:
            data_map = coerce_object_map(data)
            if not data_map:
                return
            request_id = str(
                data_map.get("request_id")
                or data_map.get("requestId")
                or data_map.get("id")
                or ""
            ).strip()
            if not request_id:
                return
            waiter = _ask_user_pending_requests.get(request_id)
            print(
                f"[ask_user mcp] recv_response request_id={request_id} waiter={'yes' if waiter and not waiter.done() else 'no'}",
                file=sys.stderr,
                flush=True,
            )
            if waiter and not waiter.done():
                waiter.set_result(data_map)

        async def _on_ask_user_terminal(data: object) -> None:
            data_map = coerce_object_map(data)
            if not data_map:
                return
            request_id = str(
                data_map.get("request_id")
                or data_map.get("requestId")
                or data_map.get("interaction_id")
                or data_map.get("id")
                or ""
            ).strip()
            if not request_id:
                return
            waiter = _ask_user_pending_requests.get(request_id)
            print(
                f"[ask_user mcp] recv_terminal request_id={request_id} waiter={'yes' if waiter and not waiter.done() else 'no'} status={data_map.get('status')!r}",
                file=sys.stderr,
                flush=True,
            )
            if waiter and not waiter.done():
                waiter.set_result(data_map)

        async def _on_ipc_disconnect():
            for request_id, pending in list(_ask_user_pending_requests.items()):
                if pending and not pending.done():
                    pending.set_result({
                        "request_id": request_id,
                        "status": "error",
                        "error": "ask_user IPC disconnected",
                    })
            _ask_user_pending_requests.clear()

        client.on("ask_user_response", _on_ask_user_response, namespace=_APPSERVER_IPC_NAMESPACE)
        client.on("ask_user_terminal", _on_ask_user_terminal, namespace=_APPSERVER_IPC_NAMESPACE)
        client.on("disconnect", _on_ipc_disconnect, namespace=_APPSERVER_IPC_NAMESPACE)

        try:
            await client.connect(
                _APPSERVER_ORIGIN,
                auth={"secret": load_or_create_ipc_secret()},
                namespaces=[_APPSERVER_IPC_NAMESPACE],
                transports=["websocket"],
                wait_timeout=5,
            )
        except Exception:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        _appserver_ipc_sio = client
        return client


async def _notify_conversation_todo_ipc(conversation_id: str) -> None:
    cid = str(conversation_id or "").strip()
    if not cid:
        return
    client = await _get_appserver_ipc_sio()
    ack = await client.call(
        "conversation_todo_changed",
        {"conversation_id": cid},
        namespace=_APPSERVER_IPC_NAMESPACE,
        timeout=10,
    )
    ack_map = coerce_object_map(cast(object, ack)) if isinstance(ack, dict) else {}
    if not ack_map.get("ok"):
        detail: object = ack_map.get("error") if ack_map else cast(object, ack)
        raise RuntimeError(f"conversation_todo_changed IPC failed: {detail}")


async def _call_appserver_ipc(event: str, payload: ObjectMap, *, timeout_seconds: int = 10) -> ObjectMap:
    client = await _get_appserver_ipc_sio()
    ack = await client.call(
        event,
        payload,
        namespace=_APPSERVER_IPC_NAMESPACE,
        timeout=timeout_seconds,
    )
    ack_map = coerce_object_map(ack)
    if not ack_map:
        return {
            "ok": False,
            "error": f"{event} IPC returned invalid ack",
            "ack": ack,
        }
    return ack_map


async def _call_sidebar_draft_rpc(method: str, params: ObjectMap) -> ObjectMap:
    ack = await _call_appserver_ipc(
        "sidebar_rpc",
        {
            "method": method,
            "params": params,
        },
        timeout_seconds=10,
    )
    if not ack.get("ok"):
        return {
            "ok": False,
            "method": method,
            "error": str(ack.get("error") or "sidebar draft RPC failed"),
        }
    result = ack.get("result")
    if isinstance(result, dict):
        result_map = coerce_object_map(cast(object, result))
        result_map.setdefault("method", method)
        return result_map
    return {
        "ok": True,
        "method": method,
        "result": result,
    }


def _add_optional_text(params: ObjectMap, key: str, value: Optional[str]) -> None:
    text = str(value or "").strip()
    if text:
        params[key] = text


def _render_kb_result(header: str, diff: str) -> str:
    return "\n".join([header, diff])


_DEFAULT_CONVERSATION_DIR = Path(os.path.expanduser("~/.cache/app_server/conversations"))
_conv_todos.configure(_DEFAULT_CONVERSATION_DIR)


mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")

_ASK_USER_ANSWER_FIELD = "answer"


# Diagnostic markers for stdio MCP process lifetime
print(f"MCP SERVER STARTED pid={os.getpid()}", file=sys.stderr)


def _configure_http_transport_from_env() -> None:
    host = os.environ.get("MCP_HTTP_HOST") or os.environ.get("FASTMCP_HOST")
    port = _coerce_optional_int(os.environ.get("MCP_HTTP_PORT") or os.environ.get("FASTMCP_PORT"))
    streamable_http_path = (
        os.environ.get("MCP_STREAMABLE_HTTP_PATH") or os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH")
    )
    if host and host.strip():
        mcp.settings.host = host.strip()
    if isinstance(port, int):
        mcp.settings.port = port
    if streamable_http_path and streamable_http_path.strip():
        mcp.settings.streamable_http_path = streamable_http_path.strip()

@mcp.tool(name="ping", description="Return MCP server pid (diagnostic).")
async def ping() -> ObjectMap:
    return {"ok": True, "pid": os.getpid()}


def _normalize_choice_list(raw_choices: Optional[list[str]]) -> list[str]:
    if not isinstance(raw_choices, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_choices:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _extract_ask_user_answers(content: object) -> list[str]:
    payload = coerce_object_map(content)
    raw_answers = payload.get(_ASK_USER_ANSWER_FIELD)
    if isinstance(raw_answers, str):
        value = raw_answers.strip()
        return [value] if value else []
    if not isinstance(raw_answers, list):
        return []
    answers: list[str] = []
    for item in cast(list[object], raw_answers):
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value:
            answers.append(value)
    return answers


async def _wait_for_ask_user_event(request_id: str) -> ObjectMap:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        raise RuntimeError("request_id is required")
    future = _ask_user_pending_requests.get(request_id_text)
    if future is None:
        future = asyncio.get_running_loop().create_future()
        _ask_user_pending_requests[request_id_text] = future
    try:
        return await future
    finally:
        current = _ask_user_pending_requests.get(request_id_text)
        if current is future:
            _ask_user_pending_requests.pop(request_id_text, None)


def _extract_ask_user_answers_from_resolution(resolution: object) -> list[str]:
    payload = coerce_object_map(resolution)
    answers = payload.get("answers")
    if isinstance(answers, str):
        value = answers.strip()
        return [value] if value else []
    if isinstance(answers, list):
        normalized: list[str] = []
        for item in cast(list[object], answers):
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value:
                normalized.append(value)
        if normalized:
            return normalized
    answer = payload.get("answer")
    if isinstance(answer, str):
        value = answer.strip()
        return [value] if value else []
    content = payload.get("content")
    if isinstance(content, dict):
        answers_from_content = _extract_ask_user_answers(cast(object, content))
        if answers_from_content:
            return answers_from_content
    return []


def _normalize_ask_user_resolution(
    resolution: object,
    *,
    choices: list[str],
) -> ObjectMap:
    payload = coerce_object_map(resolution)
    action = str(payload.get("action") or payload.get("status") or "accept").strip().lower() or "accept"
    if action not in {"accept", "decline", "cancel"}:
        action = "accept"
    if action != "accept":
        return {
            "ok": True,
            "status": action,
            "accepted": False,
            "answer": None,
            "answers": [],
            "selected_choice": None,
            "freeform_answer": None,
        }

    answers = _extract_ask_user_answers_from_resolution(payload)
    answer = answers[0] if answers else None
    choice_set = set(choices)
    selected_choice = answer if isinstance(answer, str) and answer in choice_set else None
    freeform_answer = answer if isinstance(answer, str) and answer not in choice_set else None
    return {
        "ok": True,
        "status": "accept",
        "accepted": True,
        "answer": answer,
        "answers": answers,
        "selected_choice": selected_choice,
        "freeform_answer": freeform_answer,
    }


def _normalize_ask_user_terminal(
    event: object,
    *,
    choices: list[str],
) -> ObjectMap:
    payload = coerce_object_map(event)
    if isinstance(payload.get("response"), dict):
        return _normalize_ask_user_resolution(payload["response"], choices=choices)
    if isinstance(payload.get("result"), dict):
        return _normalize_ask_user_resolution(payload["result"], choices=choices)
    status = str(payload.get("status") or "").strip().lower()
    error = str(payload.get("error") or "").strip()
    if status == "error" or error:
        return {"ok": False, "error": error or "ask_user terminated with error"}
    if status not in {"cancel", "interrupted"}:
        status = "cancel"
    return {
        "ok": True,
        "status": status,
        "accepted": False,
        "answer": None,
        "answers": [],
        "selected_choice": None,
        "freeform_answer": None,
    }


@mcp.tool(
    name="ask_user",
    description="Ask the user a question and wait for a response. Supports choices and optional freeform input.",
    annotations=ToolAnnotations(readOnlyHint=False),
)
async def ask_user(
    question: str,
    choices: Optional[list[str]] = None,
    allow_freeform: bool = True,
    ctx: Context[ServerSession, object, object] | None = None,
) -> ObjectMap:
    question_text = str(question or "").strip()
    if not question_text:
        return {"ok": False, "error": "question is required"}
    if ctx is None:
        return {"ok": False, "error": "request context unavailable"}

    normalized_choices = _normalize_choice_list(choices)
    allow_freeform_value = bool(allow_freeform)
    if not normalized_choices and not allow_freeform_value:
        return {
            "ok": False,
            "error": "At least one choice is required when allow_freeform is false",
        }
    request_id = os.environ.get("CONVERSATION_ID", "").strip()
    if not request_id:
        return {"ok": False, "error": "CONVERSATION_ID not available — MCP server not conversation-scoped"}
    print(
        f"[ask_user mcp] start request_id={request_id} question={question_text!r} choices={normalized_choices!r} allow_freeform={allow_freeform_value}",
        file=sys.stderr,
        flush=True,
    )
    try:
        client = await _get_appserver_ipc_sio()
        loop = asyncio.get_running_loop()
        existing_future = _ask_user_pending_requests.get(request_id)
        if isinstance(existing_future, asyncio.Future) and not existing_future.done():
            return {"ok": False, "error": f"ask_user request already pending for {request_id}"}
        result_future: asyncio.Future[ObjectMap] = loop.create_future()
        _ask_user_pending_requests[request_id] = result_future
        print(f"[ask_user mcp] waiting request_id={request_id}", file=sys.stderr, flush=True)
        result_event = await _wait_for_ask_user_event(request_id)
    except Exception as exc:
        if "request_id" in locals() and request_id:
            _ask_user_pending_requests.pop(request_id, None)
        print(f"[ask_user mcp] error request_id={request_id or '-'} error={exc!r}", file=sys.stderr, flush=True)
        return {"ok": False, "error": str(exc)}
    if isinstance(result_event.get("response"), dict):
        print(
            f"[ask_user mcp] got_response request_id={request_id} response={result_event.get('response')!r}",
            file=sys.stderr,
            flush=True,
        )
        with contextlib.suppress(Exception):
            await client.emit(
                "ask_user_ack",
                {"request_id": request_id},
                namespace=_APPSERVER_IPC_NAMESPACE,
            )
            print(f"[ask_user mcp] ack_sent request_id={request_id}", file=sys.stderr, flush=True)
        return _normalize_ask_user_resolution(
            result_event.get("response"),
            choices=normalized_choices,
        )
    if isinstance(result_event.get("result"), dict):
        print(
            f"[ask_user mcp] got_legacy_result request_id={request_id} result={result_event.get('result')!r}",
            file=sys.stderr,
            flush=True,
        )
        with contextlib.suppress(Exception):
            await client.emit(
                "ask_user_ack",
                {"request_id": request_id},
                namespace=_APPSERVER_IPC_NAMESPACE,
            )
            print(f"[ask_user mcp] ack_sent request_id={request_id}", file=sys.stderr, flush=True)
        return _normalize_ask_user_resolution(
            result_event.get("result"),
            choices=normalized_choices,
        )
    print(f"[ask_user mcp] terminal request_id={request_id} event={result_event!r}", file=sys.stderr, flush=True)
    return _normalize_ask_user_terminal(
        result_event,
        choices=normalized_choices,
    )


@mcp.tool(name="conv_id", description="Return the conversation ID for this MCP session.")
async def conv_id() -> ObjectMap:
    cid = os.environ.get("CONVERSATION_ID", "")
    return {"ok": bool(cid), "conversation_id": cid}


@mcp.tool(
    name="sidebar_drafts_list",
    description="List TE2 user draft files for a project through the ALS-RS sidebar IPC proxy. Does not return draft content.",
)
async def sidebar_drafts_list(project_path: Optional[str] = None) -> ObjectMap:
    params: ObjectMap = {}
    _add_optional_text(params, "projectPath", project_path)
    return await _call_sidebar_draft_rpc(_SIDEBAR_DRAFTS_LIST_METHOD, params)


@mcp.tool(
    name="sidebar_draft_state_get",
    description="Get TE2 draft state for a project or target file without returning draft content by default.",
)
async def sidebar_draft_state_get(
    scope: str = "file",
    project_path: Optional[str] = None,
    target_file: Optional[str] = None,
    include_disk_content: bool = False,
    include_hunks: bool = False,
) -> ObjectMap:
    params: ObjectMap = {
        "scope": str(scope or "file").strip() or "file",
        "includeContent": False,
        "includeDiskContent": bool(include_disk_content),
        "includeHunks": bool(include_hunks),
    }
    _add_optional_text(params, "projectPath", project_path)
    _add_optional_text(params, "targetFile", target_file)
    return await _call_sidebar_draft_rpc(_SIDEBAR_DRAFT_STATE_GET_METHOD, params)


@mcp.tool(
    name="sidebar_draft_content_get",
    description="Explicitly read TE2 draft content for one target file after draft discovery indicates a draft exists.",
)
async def sidebar_draft_content_get(
    target_file: str,
    project_path: Optional[str] = None,
    include_disk_content: bool = False,
    include_hunks: bool = False,
) -> ObjectMap:
    target_text = str(target_file or "").strip()
    if not target_text:
        return {"ok": False, "error": "target_file is required"}
    params: ObjectMap = {
        "scope": "file",
        "targetFile": target_text,
        "includeContent": True,
        "includeDiskContent": bool(include_disk_content),
        "includeHunks": bool(include_hunks),
    }
    _add_optional_text(params, "projectPath", project_path)
    return await _call_sidebar_draft_rpc(_SIDEBAR_DRAFT_STATE_GET_METHOD, params)


@mcp.tool(
    name="sidebar_draft_clear",
    description="Clear one TE2 user draft through sidebar IPC. Use only after explicit user approval.",
)
async def sidebar_draft_clear(
    target_file: str,
    project_path: Optional[str] = None,
    request_id: Optional[str] = None,
    confirmed: bool = False,
) -> ObjectMap:
    if not confirmed:
        return {
            "ok": False,
            "error": "sidebar_draft_clear requires confirmed=true after explicit user approval",
        }
    target_text = str(target_file or "").strip()
    if not target_text:
        return {"ok": False, "error": "target_file is required"}
    params: ObjectMap = {
        "targetFile": target_text,
    }
    _add_optional_text(params, "projectPath", project_path)
    _add_optional_text(params, "requestId", request_id)
    return await _call_sidebar_draft_rpc(_SIDEBAR_DRAFT_CLEAR_METHOD, params)


# ── Conversation Todo MCP tools ──────────────────────────────────────

def _todo_cid() -> str:
    """Return the conversation ID or raise."""
    cid = os.environ.get("CONVERSATION_ID", "").strip()
    if not cid:
        raise ValueError("CONVERSATION_ID not set — not conversation-scoped")
    return cid


@mcp.tool(name="todo_list", description="List todos for this conversation. Optionally filter by status.")
async def todo_list(status: Optional[str] = None) -> ObjectMap:
    """
    List all todos for this conversation.

    Args:
        status: Optional filter — 'pending', 'in_progress', 'done', or 'blocked'.

    Returns:
        {ok: true, todos: [...]} or {ok: false, error: "..."}.
    """
    try:
        cid = _todo_cid()
        return {"ok": True, "todos": _conv_todos.list_todos(cid, status=status)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="todo_add", description="Add a todo to this conversation.")
async def todo_add(title: str, description: str = "", status: str = "pending") -> ObjectMap:
    """
    Add a new todo.

    Args:
        title: Short title for the todo.
        description: Optional longer description.
        status: Initial status — defaults to 'pending'.

    Returns:
        {ok: true, todo: {...}} or {ok: false, error: "..."}.
    """
    try:
        cid = _todo_cid()
        if not title.strip():
            return {"ok": False, "error": "title required"}
        todo = _conv_todos.add_todo(cid, title.strip(), description=description, status=status)
        await _notify_conversation_todo_ipc(cid)
        return {"ok": True, "todo": todo}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="todo_update", description="Update a todo's title, description, or status.")
async def todo_update(
    id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
) -> ObjectMap:
    """
    Update fields on an existing todo.

    Args:
        id: The todo's numeric ID.
        title: New title (optional).
        description: New description (optional).
        status: New status — 'pending', 'in_progress', 'done', 'blocked' (optional).

    Returns:
        {ok: true, todo: {...}} or {ok: false, error: "..."}.
    """
    try:
        cid = _todo_cid()
        result = _conv_todos.update_todo(cid, id, title=title, description=description, status=status)
        if result is None:
            return {"ok": False, "error": f"todo {id} not found"}
        await _notify_conversation_todo_ipc(cid)
        return {"ok": True, "todo": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="todo_remove", description="Remove a todo by ID.")
async def todo_remove(id: int) -> ObjectMap:
    """
    Delete a todo.

    Args:
        id: The todo's numeric ID.

    Returns:
        {ok: true, removed: true/false}.
    """
    try:
        cid = _todo_cid()
        removed = _conv_todos.remove_todo(cid, id)
        if removed:
            await _notify_conversation_todo_ipc(cid)
        return {"ok": True, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="todo_toggle", description="Toggle a todo between 'pending' and 'done'.")
async def todo_toggle(id: int) -> ObjectMap:
    """
    Toggle a todo's status between 'pending' and 'done'.

    Args:
        id: The todo's numeric ID.

    Returns:
        {ok: true, todo: {...}} or {ok: false, error: "..."}.
    """
    try:
        cid = _todo_cid()
        result = _conv_todos.toggle_todo(cid, id)
        if result is None:
            return {"ok": False, "error": f"todo {id} not found"}
        await _notify_conversation_todo_ipc(cid)
        return {"ok": True, "todo": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="todo_ready", description="List todos with all dependencies satisfied (ready to work on).")
async def todo_ready() -> ObjectMap:
    """
    List pending todos whose dependencies are all 'done'.

    Returns:
        {ok: true, todos: [...]}.
    """
    try:
        cid = _todo_cid()
        return {"ok": True, "todos": _conv_todos.list_ready(cid)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =============================================================================
# Agent Log MCP Tools
# =============================================================================
# These tools provide a plain-text interface to the agent log server,
# abstracting away JSON escaping and HTTP details.

_AGENT_LOG_URL = _APPSERVER_ORIGIN.rstrip("/") + "/api/messages"


async def _agent_log_fetch(limit: int = 10) -> ObjectList:
    """Fetch messages from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}?limit={limit}"
    try:
        def _get() -> ObjectList:
            req = urllib.request.Request(url, method="GET")
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=5)) as resp:
                return _json_loads_object_list(_read_urlopen_response_body(resp))
        messages = await asyncio.to_thread(_get)
        return messages
    except Exception:
        return []


async def _agent_log_fetch_by_num(msg_num: int) -> Optional[ObjectMap]:
    """Fetch a specific message by msg_num from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}/{msg_num}"
    try:
        def _get() -> ObjectMap:
            req = urllib.request.Request(url, method="GET")
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=5)) as resp:
                return _json_loads_object_map(_read_urlopen_response_body(resp))
        return await asyncio.to_thread(_get)
    except Exception:
        return None


async def _agent_log_post_internal(who: str, message: str) -> ObjectMap:
    """Post a message to agent log server."""
    import urllib.request
    payload = json.dumps({"who": who, "message": message}, ensure_ascii=False).encode("utf-8")
    try:
        def _post() -> ObjectMap:
            req = urllib.request.Request(
                _AGENT_LOG_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=5)) as resp:
                return _json_loads_object_map(_read_urlopen_response_body(resp))
        return await asyncio.to_thread(_post)
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _agent_log_delete_by_num(msg_num: int) -> ObjectMap:
    """Delete a message by msg_num from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}/{msg_num}"
    try:
        def _delete() -> ObjectMap:
            req = urllib.request.Request(url, method="DELETE")
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=5)) as resp:
                return _json_loads_object_map(_read_urlopen_response_body(resp))
        return await asyncio.to_thread(_delete)
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _agent_log_await(after_msg_num: int, from_who: Optional[str] = None, timeout_ms: int = 180000) -> ObjectMap:
    """Wait for the next message after a given msg_num."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}/await"
    payload = json.dumps({
        "after_msg_num": after_msg_num,
        "from_who": from_who,
        "timeout_ms": timeout_ms
    }, ensure_ascii=False).encode("utf-8")
    
    # Use a longer socket timeout than the await timeout
    socket_timeout = (timeout_ms / 1000.0) + 10
    
    try:
        def _post() -> ObjectMap:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=socket_timeout)) as resp:
                status = int(resp.status)
                body = _json_loads_object_map(_read_urlopen_response_body(resp))
                if status == 408:
                    return {"ok": False, "error": "timeout"}
                return body
        return await asyncio.to_thread(_post)
    except Exception as e:
        err_str = str(e)
        if "408" in err_str or "timeout" in err_str.lower():
            return {"ok": False, "error": "timeout"}
        return {"ok": False, "error": err_str}


@mcp.tool(name="agent_log_post", description="Post a message to the agent log. Plain text, no escaping needed.")
async def agent_log_post(who: str, message: str) -> ObjectMap:
    """
    Post a message to the shared agent log.
    
    Args:
        who: Your identifier/pseudonym (e.g., "Dex", "vectorArc")
        message: Plain text message. Newlines are preserved as-is.
    
    Returns:
        {ok: true, msg_num: <number>} on success, or {ok: false, error: "..."} on failure.
    """
    if not who or not message:
        return {"ok": False, "error": "who and message are required"}
    result = await _agent_log_post_internal(who, message)
    if "ts" in result:
        return {"ok": True, "msg_num": result.get("msg_num"), "ts": result.get("ts")}
    return result


@mcp.tool(name="agent_log_inbox", description="Get a preview inbox of recent agent log messages.")
async def agent_log_inbox(limit: int = 10, preview_chars: int = 60) -> ObjectMap:
    """
    Fetch a preview inbox of recent messages.
    
    Returns truncated previews so you can quickly scan without reading full messages.
    Use agent_log_get_by_num(msg_num) to read a specific message in full.
    
    Args:
        limit: Number of messages to fetch (default 10, max 50)
        preview_chars: Max characters for preview (default 60)
    
    Returns:
        {ok: true, items: [{msg_num, ts, who, preview}, ...]}
        Items are ordered oldest-first as stored in log.
    """
    limit = max(1, min(int(limit), 50))
    preview_chars = max(10, min(int(preview_chars), 200))
    
    messages = await _agent_log_fetch(limit)
    items: ObjectList = []
    for msg in messages:
        msg_num = msg.get("msg_num")
        ts = msg.get("ts", "")
        who = msg.get("who", "")
        full_message = msg.get("message", "")
        # Preview: first line, truncated
        first_line = full_message.split("\n")[0] if isinstance(full_message, str) and full_message else ""
        if len(first_line) > preview_chars:
            preview = first_line[:preview_chars - 3] + "..."
        else:
            preview = first_line
        items.append({"msg_num": msg_num, "ts": ts, "who": who, "preview": preview})
    
    return {"ok": True, "items": items}


@mcp.tool(name="agent_log_get", description="Get the full text of a message by inbox index.")
async def agent_log_get(idx: int = 0, limit: int = 10) -> str:
    """
    Get the full text of a specific message from the inbox.
    
    Args:
        idx: Index from inbox (0 = most recent, 1 = second most recent, etc.)
        limit: How many messages to fetch when building inbox (must be > idx)
    
    Returns:
        Formatted plain text: "#msg_num [timestamp] who:\nmessage"
    """
    idx = max(0, int(idx))
    limit = max(idx + 1, min(int(limit), 50))
    
    messages = await _agent_log_fetch(limit)
    if not messages:
        return "(no messages in log)"
    
    if idx >= len(messages):
        return f"(error: index {idx} out of range, only {len(messages)} messages)"
    
    msg = messages[idx]
    msg_num = msg.get("msg_num", "?")
    ts = msg.get("ts", "")
    who = msg.get("who", "")
    message = msg.get("message", "")
    return f"#{msg_num} [{ts}] {who}:\n{message}"


@mcp.tool(name="agent_log_get_by_num", description="Get a specific message by its permanent message number.")
async def agent_log_get_by_num(msg_num: int) -> str:
    """
    Get the full text of a specific message by its msg_num.
    
    Args:
        msg_num: The permanent message number (shown in inbox as msg_num)
    
    Returns:
        Formatted plain text: "#msg_num [timestamp] who:\nmessage"
    """
    msg = await _agent_log_fetch_by_num(msg_num)
    if not msg:
        return f"(error: message #{msg_num} not found)"
    
    ts = msg.get("ts", "")
    who = msg.get("who", "")
    message = msg.get("message", "")
    return f"#{msg_num} [{ts}] {who}:\n{message}"


@mcp.tool(name="agent_log_last", description="Get the most recent message from the agent log.")
async def agent_log_last() -> str:
    """
    Shortcut to get the most recent message.
    
    Returns:
        Formatted plain text: "#msg_num [timestamp] who:\nmessage"
    """
    messages = await _agent_log_fetch(1)
    if not messages:
        return "(no messages in log)"
    
    msg = messages[0]
    msg_num = msg.get("msg_num", "?")
    ts = msg.get("ts", "")
    who = msg.get("who", "")
    message = msg.get("message", "")
    return f"#{msg_num} [{ts}] {who}:\n{message}"


@mcp.tool(name="agent_log_read", description="Read recent messages as formatted plain text.")
async def agent_log_read(limit: int = 10) -> str:
    """
    Read recent messages formatted as plain text.
    
    Returns all messages in a single text block, formatted as:
    #msg_num [timestamp] who:
    message
    
    #msg_num [timestamp] who:
    message
    ...
    
    Args:
        limit: Number of messages to fetch (default 10, max 50)
    
    Returns:
        Formatted plain text with all messages
    """
    limit = max(1, min(int(limit), 50))
    
    messages = await _agent_log_fetch(limit)
    if not messages:
        return "(no messages)"
    
    formatted: list[str] = []
    for msg in messages:
        msg_num = msg.get("msg_num", "?")
        ts = msg.get("ts", "")
        who = msg.get("who", "")
        message = msg.get("message", "")
        formatted.append(f"#{msg_num} [{ts}] {who}:\n{message}")
    
    return "\n\n".join(formatted)


@mcp.tool(name="agent_log_delete", description="Delete a message from the agent log by its message number.")
async def agent_log_delete(msg_num: int) -> ObjectMap:
    """
    Delete a specific message from the agent log.
    
    Args:
        msg_num: The permanent message number to delete
    
    Returns:
        {ok: true, deleted: <msg_num>} on success, or {ok: false, error: "..."} on failure.
    """
    result = await _agent_log_delete_by_num(msg_num)
    return result


@mcp.tool(name="agent_log_await", description="Wait for the next message in the agent log after a given msg_num.")
async def agent_log_await(after_msg_num: int, from_who: Optional[str] = None, timeout_ms: int = 180000) -> str:
    """
    Block and wait for the next message posted to the agent log.
    
    Args:
        after_msg_num: Wait for messages with msg_num greater than this
        from_who: Optional filter - only return messages from this author
        timeout_ms: How long to wait (default 3 minutes, max 10 minutes)
    
    Returns:
        Formatted plain text: "#msg_num [timestamp] who:\nmessage"
        Or "(timeout)" if no message arrives in time.
    """
    result = await _agent_log_await(after_msg_num, from_who, timeout_ms)
    
    if result.get("error") == "timeout":
        return "(timeout)"
    
    if "msg_num" in result:
        msg_num = result.get("msg_num", "?")
        ts = result.get("ts", "")
        who = result.get("who", "")
        message = result.get("message", "")
        return f"#{msg_num} [{ts}] {who}:\n{message}"
    
    return f"(error: {result.get('error', 'unknown')})"


@mcp.tool(name="agent_log_post_await", description="Post a message and wait for the next reply in a single call.")
async def agent_log_post_await(
    who: str, 
    message: str, 
    await_from: Optional[str] = None, 
    timeout_ms: int = 180000
) -> ObjectMap:
    """
    Post a message to the agent log, then block waiting for a reply.
    
    This enables back-and-forth conversation within a single tool call.
    
    Args:
        who: Your identifier/pseudonym
        message: The message to post
        await_from: Optional - only accept replies from this author
        timeout_ms: How long to wait for reply (default 3 minutes)
    
    Returns:
        {
            ok: true,
            posted_msg_num: <your message's number>,
            reply: "#msg_num [timestamp] who:\nmessage"
        }
        Or {ok: false, error: "..."} on failure.
    """
    if not who or not message:
        return {"ok": False, "error": "who and message are required"}
    
    # Post the message
    post_result = await _agent_log_post_internal(who, message)
    if "msg_num" not in post_result:
        return {"ok": False, "error": post_result.get("error", "failed to post")}
    
    posted_num = _coerce_optional_int(post_result["msg_num"]) or 0
    
    # Wait for reply
    await_result = await _agent_log_await(posted_num, await_from, timeout_ms)
    
    if await_result.get("error") == "timeout":
        return {
            "ok": False, 
            "error": "timeout waiting for reply",
            "posted_msg_num": posted_num
        }
    
    if "msg_num" in await_result:
        msg_num = await_result.get("msg_num", "?")
        ts = await_result.get("ts", "")
        reply_who = await_result.get("who", "")
        reply_msg = await_result.get("message", "")
        return {
            "ok": True,
            "posted_msg_num": posted_num,
            "reply_msg_num": msg_num,
            "reply": f"#{msg_num} [{ts}] {reply_who}:\n{reply_msg}"
        }
    
    return {"ok": False, "error": await_result.get("error", "unknown")}


# =============================================================================
# Knowledge Base (KB) MCP Tools
# =============================================================================

def _kb_error(error: str, detail: str, **extra: object) -> ObjectMap:
    payload: ObjectMap = {"error": error, "detail": detail}
    payload.update(extra)
    return payload


def _kb_error_text(error: str, detail: str, **extra: object) -> str:
    """Format a KB error as clean plain text."""
    parts = [f"[ERROR: {error}] {detail}"]
    for k, v in extra.items():
        parts.append(f"  {k}: {v}")
    return "\n".join(parts)


def _kb_dict_to_error_text(d: Mapping[str, object]) -> str:
    """Convert an error dict from _kb_error() to plain text."""
    error_value = d.get("error", "Unknown")
    detail_value = d.get("detail", "")
    error = error_value if isinstance(error_value, str) else str(error_value)
    detail = detail_value if isinstance(detail_value, str) else str(detail_value)
    extra = {k: v for k, v in d.items() if k not in ("error", "detail")}
    return _kb_error_text(error, detail, **extra)


def _kb_resolve_file(file: Optional[str] = None) -> Path | ObjectMap:
    """Resolve a knowledge file path. Returns absolute Path or error dict."""
    root = _current_project_root()
    files = _kb_configured_files(root)
    if not files:
        return _kb_error("NotConfigured", "No knowledge files in .agent-pty.toml")

    selected = file
    if selected is None:
        if len(files) == 1:
            selected = files[0]
        else:
            return _kb_error(
                "FileNotAllowed",
                "Multiple knowledge files configured; specify 'file'",
                configured=files,
            )

    if not selected:
        return _kb_error("FileNotAllowed", "Invalid file value", configured=files)
    if selected not in files:
        return _kb_error(
            "FileNotAllowed",
            f"File '{selected}' not in configured knowledge files",
            configured=files,
        )
    return _logical_abspath(root / selected)


def _kb_resolve_target(target: Optional[str] = None) -> Path | ObjectMap:
    """Resolve a target knowledge file path. Returns absolute Path or error dict."""
    root = _current_project_root()
    files = _kb_configured_files(root)
    if not files:
        return _kb_error("NotConfigured", "No knowledge files in .agent-pty.toml")

    selected = target
    if selected is None:
        if len(files) == 1:
            selected = files[0]
        else:
            return _kb_error(
                "FileNotAllowed",
                "Multiple knowledge files configured; specify 'target'",
                configured=files,
            )

    if not selected:
        return _kb_error("FileNotAllowed", "Invalid target value", configured=files)
    if selected not in files:
        return _kb_error(
            "FileNotAllowed",
            f"Target '{selected}' not in configured knowledge files",
            configured=files,
        )
    return _logical_abspath(root / selected)


def _kb_select_target_file(file: Optional[str], target: Optional[str]) -> Optional[str] | ObjectMap:
    """Normalize the KB target/file aliases for read-only tools."""
    if target is None:
        return file
    if not target.strip():
        return _kb_error("InvalidParameter", "target must be a non-empty string when supplied")
    selected_target = target.strip()
    if file is None:
        return selected_target
    if not file.strip():
        return _kb_error("InvalidParameter", "file must be a non-empty string when supplied")
    selected_file = file.strip()
    if selected_file != selected_target:
        return _kb_error(
            "InvalidParameter",
            "file and target refer to different knowledge files",
            file=selected_file,
            target=selected_target,
        )
    return selected_file


def _kb_resolve_search_files(file: Optional[str], target: Optional[str]) -> list[Path] | ObjectMap:
    """Resolve KB search files. Omitted file/target means all configured KB files."""
    selected_file = _kb_select_target_file(file, target)
    if isinstance(selected_file, dict):
        return selected_file
    if selected_file is not None:
        resolved = _kb_resolve_file(selected_file)
        if isinstance(resolved, dict):
            return resolved
        return [resolved]

    root = _current_project_root()
    files = _kb_configured_files(root)
    if not files:
        return _kb_error("NotConfigured", "No knowledge files in .agent-pty.toml")
    return [_logical_abspath(root / configured_file) for configured_file in files]


def _kb_display_path(path: Path) -> str:
    root = _current_project_root()
    with contextlib.suppress(ValueError):
        return str(path.relative_to(root))
    return path.name


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically: temp file, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _content_hash(text: str) -> str:
    """SHA256 hex digest of text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _unified_diff(old: str, new: str, filename: str) -> str:
    """Return unified diff string."""
    return "\n".join(
        difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            fromfile=filename,
            tofile=filename,
            lineterm="",
        )
    )


def _extract_line_range(lines: list[str], start: int, end: int) -> str:
    if start < 1:
        start = 1
    if end < start:
        return ""
    if not lines:
        return ""
    s = start - 1
    e = min(end, len(lines))
    if s >= len(lines):
        return ""
    return "\n".join(lines[s:e])


def _replace_line_range(lines: list[str], start: int, end: int, replacement: str) -> list[str]:
    return _replace_line_range_with_lines(lines, start, end, (replacement or "").splitlines())


def _replace_line_range_with_lines(lines: list[str], start: int, end: int, rep: list[str]) -> list[str]:
    if start < 1:
        start = 1
    insert_at = max(0, start - 1)
    if end < start:
        return lines[:insert_at] + rep + lines[insert_at:]
    left = lines[:insert_at]
    right = lines[min(end, len(lines)):]
    return left + rep + right


_KB_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+\S.*$")
_KB_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _strip_outer_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _kb_normalize_markdown_spacing(lines: list[str]) -> list[str]:
    normalized: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_match = _KB_FENCE_RE.match(line)
        if fence_match:
            normalized.append(line)
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence and _KB_ATX_HEADING_RE.match(line):
            if normalized and normalized[-1].strip():
                normalized.append("")
            normalized.append(line)
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            normalized.append("")
            continue
        normalized.append(line)
        i += 1
    return normalized


def _kb_auto_body_lines(content: str, *, has_heading: bool) -> list[str]:
    body_lines = _kb_normalize_markdown_spacing(
        _strip_outer_blank_lines((content or "").splitlines())
    )
    if not has_heading:
        return body_lines
    if not body_lines:
        return [""]
    return ["", *body_lines]


def _kb_auto_append_lines(existing_lines: list[str], insert_at: int, content: str) -> list[str]:
    content_lines = _kb_normalize_markdown_spacing(
        _strip_outer_blank_lines((content or "").splitlines())
    )
    if not content_lines:
        return []
    insert_lines: list[str] = []
    if insert_at > 0 and existing_lines[insert_at - 1].strip():
        insert_lines.append("")
    insert_lines.extend(content_lines)
    if (
        insert_at < len(existing_lines)
        and insert_lines
        and insert_lines[-1].strip()
        and existing_lines[insert_at].strip()
    ):
        insert_lines.append("")
    return insert_lines


def _kb_root_section(total_lines: int) -> SectionNode:
    last_line = max(0, int(total_lines))
    return SectionNode(
        id="",
        id_disambiguated="",
        depth=0,
        title="",
        line_start=1,
        body_start=1,
        body_end=last_line,
        subtree_end=last_line,
    )


def _kb_ambiguous_section_error(section_id: str, matches: list[SectionNode]) -> ObjectMap:
    candidates = [{"id_disambiguated": n.id_disambiguated, "line_start": n.line_start} for n in matches]
    return _kb_error(
        "AmbiguousSection",
        f"Section id '{section_id}' is ambiguous",
        candidates=candidates,
    )


def _kb_section_label(target: SectionNode) -> str:
    return target.id if target.id else "<file-root>"


def _section_suffix_matches(node: SectionNode, query_parts: list[str]) -> bool:
    if not query_parts:
        return False
    node_parts = [_normalize_heading(part) for part in node.id.split(" > ") if part.strip()]
    if len(query_parts) > len(node_parts):
        return False
    return node_parts[-len(query_parts):] == query_parts


def _resolve_section_or_error(
    nodes: list[SectionNode],
    section_id: str,
    *,
    total_lines: int = 0,
    allow_root: bool = False,
) -> SectionNode | ObjectMap:
    raw_id = (section_id or "").strip()
    normalized_id = _normalize_heading(raw_id)
    if allow_root and not raw_id:
        return _kb_root_section(total_lines)

    # 1. Exact full-path matches.
    exact = [n for n in nodes if n.id == raw_id or n.id == normalized_id]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raw_exact = [n for n in exact if n.id == raw_id]
        if len(raw_exact) == 1:
            return raw_exact[0]
        return _kb_ambiguous_section_error(raw_id, exact)

    # 2. Exact disambiguated-path matches.
    by_disambiguated = [n for n in nodes if n.id_disambiguated == raw_id or n.id_disambiguated == normalized_id]
    if len(by_disambiguated) == 1:
        return by_disambiguated[0]
    if len(by_disambiguated) > 1:
        return _kb_ambiguous_section_error(raw_id, by_disambiguated)

    # 3. Unique visible heading title matches.
    by_title = [n for n in nodes if n.title == raw_id or _normalize_heading(n.title) == normalized_id]
    if len(by_title) == 1:
        return by_title[0]
    if len(by_title) > 1:
        return _kb_ambiguous_section_error(raw_id, by_title)

    # 4. Unique trailing path suffix matches, e.g. "Parent > Child" without the top-level wrapper.
    query_parts = [_normalize_heading(part.strip()) for part in raw_id.split(">") if part.strip()]
    by_suffix = [n for n in nodes if _section_suffix_matches(n, query_parts)]
    if len(by_suffix) == 1:
        return by_suffix[0]
    if len(by_suffix) > 1:
        return _kb_ambiguous_section_error(raw_id, by_suffix)

    # Line aliases make schema output such as "L42 # Heading" directly usable.
    line_match = re.fullmatch(r"[Ll]?(\d+)", raw_id)
    if line_match:
        line_no = int(line_match.group(1))
        by_line = [n for n in nodes if n.line_start == line_no]
        if len(by_line) == 1:
            return by_line[0]
        if len(by_line) > 1:
            return _kb_ambiguous_section_error(raw_id, by_line)

    return _kb_error("SectionNotFound", f"Section '{raw_id}' not found", id=raw_id)


def _read_text_or_error(path: Path) -> str | ObjectMap:
    if not path.exists():
        return _kb_error("FileNotAllowed", f"Knowledge file does not exist: {path}")
    return path.read_text(encoding="utf-8")


def _toml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _save_kb_files_config(files: list[str], *, root: Path) -> None:
    """Write knowledge.files config to .agent-pty.toml (minimal managed format)."""
    config_path = _logical_abspath(root / ".agent-pty.toml")
    lines = [
        "# Agent PTY project configuration",
        "# Auto-managed by kb tools.",
        "",
        "[knowledge]",
        "files = [" + ", ".join(_toml_quote(f) for f in files) + "]",
        "",
    ]
    _atomic_write(config_path, "\n".join(lines))


def _load_kb_files_config(*, root: Path) -> list[str]:
    cfg = _load_project_config(root)
    kb = coerce_object_map(cfg.get("knowledge"))
    raw_files = kb.get("files", [])
    if isinstance(raw_files, str):
        return [raw_files]
    if isinstance(raw_files, list):
        return [str(x) for x in cast(list[object], raw_files) if isinstance(x, str)]
    return []


def _kb_depth_value(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    parsed = int(value)
    return max(0, parsed)


def _kb_depth_label(value: Optional[int]) -> str:
    return "all" if value is None else str(value)


def _kb_node_number_map(nodes: list[SectionNode]) -> dict[int, int]:
    return {node.line_start: idx for idx, node in enumerate(nodes, start=1)}


def _kb_line_count(start: int, end: int) -> int:
    return max(0, end - start + 1)


def _kb_body_range(node: SectionNode) -> tuple[int, int]:
    return node.body_start, node.body_end


def _kb_heading_body_range(node: SectionNode) -> tuple[int, int]:
    return node.line_start, max(node.line_start, node.body_end)


def _kb_node_ancestors(nodes: list[SectionNode], target: SectionNode) -> list[SectionNode]:
    if target.depth <= 0:
        return []
    return [
        node
        for node in nodes
        if node.depth < target.depth
        and node.line_start < target.line_start
        and target.line_start <= node.subtree_end
    ]


def _kb_node_children(nodes: list[SectionNode], target: SectionNode) -> list[SectionNode]:
    if target.depth <= 0:
        return [node for node in nodes if node.depth == min((n.depth for n in nodes), default=1)]
    return [
        node
        for node in nodes
        if node.depth == target.depth + 1
        and node.line_start > target.line_start
        and node.line_start <= target.subtree_end
    ]


def _kb_preview_text(text: str, max_chars: int) -> str:
    cap = max(0, int(max_chars))
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if cap <= 0:
        return ""
    if len(cleaned) <= cap:
        return cleaned
    return cleaned[: max(0, cap - 3)].rstrip() + "..."


def _kb_extract_node_body(lines: list[str], node: SectionNode) -> str:
    start, end = _kb_body_range(node)
    return _extract_line_range(lines, start, end)


def _kb_extract_heading_body(lines: list[str], node: SectionNode) -> str:
    start, end = _kb_heading_body_range(node)
    return _extract_line_range(lines, start, end)


def _kb_apply_max_chars(text: str, max_chars: Optional[int]) -> str:
    if max_chars is None:
        return text
    cap = int(max_chars)
    if cap <= 0 or len(text) <= cap:
        return text
    notice = f"\n[TRUNCATED: output exceeded max_chars={cap}]\n"
    return text[: max(0, cap - len(notice))].rstrip() + notice


def _kb_split_section_specs(
    sections: Optional[str],
    id_value: Optional[str] = None,
    section_value: Optional[str] = None,
) -> list[str]:
    specs: list[str] = []
    for raw_value in (sections, section_value):
        if raw_value is None:
            continue
        raw_sections = raw_value.strip()
        if not raw_sections:
            specs.append("")
            continue
        parsed: object = None
        with contextlib.suppress(Exception):
            parsed = _json_loads_object(raw_sections)
        if isinstance(parsed, list):
            specs.extend(str(item).strip() for item in cast(list[object], parsed) if str(item).strip())
        elif isinstance(parsed, str) and parsed.strip():
            specs.append(parsed.strip())
        else:
            specs.extend(part.strip() for part in re.split(r"[\n,;]+", raw_sections) if part.strip())
    raw_id = (id_value or "").strip()
    if raw_id:
        specs.append(raw_id)
    return specs


def _resolve_section_selector_or_error(
    nodes: list[SectionNode],
    selector: str,
    *,
    total_lines: int,
    allow_root: bool = False,
) -> SectionNode | ObjectMap:
    raw = (selector or "").strip()
    if raw in {"", "root", "<file-root>"}:
        if allow_root:
            return _kb_root_section(total_lines)
        return _kb_error(
            "InvalidParameter",
            "File-root selection is not supported for this operation",
            section=raw,
        )

    ordinal_match = re.fullmatch(r"#?(\d+)", raw)
    if ordinal_match:
        ordinal = int(ordinal_match.group(1))
        if 1 <= ordinal <= len(nodes):
            return nodes[ordinal - 1]
        return _kb_error(
            "SectionNotFound",
            f"Section number {ordinal} is outside the available range 1-{len(nodes)}",
            section=raw,
        )

    line_match = re.fullmatch(r"(?:[Ll]|line:)(\d+)", raw)
    if line_match:
        line_no = int(line_match.group(1))
        by_line = [node for node in nodes if node.line_start == line_no]
        if len(by_line) == 1:
            return by_line[0]
        if len(by_line) > 1:
            return _kb_ambiguous_section_error(raw, by_line)
        return _kb_error("SectionNotFound", f"No heading starts at line {line_no}", section=raw)

    return _resolve_section_or_error(nodes, raw, total_lines=total_lines, allow_root=allow_root)


def _resolve_section_list_or_error(
    nodes: list[SectionNode],
    sections: Optional[str],
    id_value: Optional[str],
    *,
    total_lines: int,
    allow_root: bool = False,
    section_value: Optional[str] = None,
) -> list[SectionNode] | ObjectMap:
    specs = _kb_split_section_specs(sections, id_value, section_value)
    if not specs:
        message = "At least one section selector is required"
        if allow_root:
            message += '; use sections="root" or section="" to read the file root'
        return _kb_error("InvalidParameter", message)

    selected: list[SectionNode] = []
    seen: set[tuple[int, int]] = set()
    for spec in specs:
        node = _resolve_section_selector_or_error(nodes, spec, total_lines=total_lines, allow_root=allow_root)
        if isinstance(node, dict):
            return node
        key = (node.depth, node.line_start)
        if key not in seen:
            selected.append(node)
            seen.add(key)
    return selected


def _resolve_single_section_or_error(
    nodes: list[SectionNode],
    section: Optional[str],
    *,
    total_lines: int,
    allow_root: bool = False,
) -> SectionNode | ObjectMap:
    selected = _resolve_section_list_or_error(
        nodes,
        section,
        None,
        total_lines=total_lines,
        allow_root=allow_root,
    )
    if isinstance(selected, dict):
        return selected
    if len(selected) != 1:
        return _kb_error(
            "InvalidParameter",
            "Mutation tools accept exactly one section selector",
            section=section,
            selected=len(selected),
        )
    return selected[0]


def _kb_schema_line(node: SectionNode, ordinal: int, *, include_id: bool = True) -> str:
    indent = "  " * max(0, node.depth - 1)
    base = f"{ordinal:03d} H{node.depth} L{node.line_start} {indent}{'#' * node.depth} {node.title}"
    if include_id:
        base += f" | id: {node.id_disambiguated}"
    return base


def _kb_node_info_block(
    lines: list[str],
    nodes: list[SectionNode],
    node: SectionNode,
    *,
    number_by_line: dict[int, int],
    max_chars: int,
    label: str,
) -> list[str]:
    number = number_by_line.get(node.line_start)
    prefix = f"{label}: "
    if number is not None:
        prefix += f"{number:03d} "
    prefix += f"H{node.depth} L{node.line_start} {'#' * node.depth} {node.title}"
    body_start, body_end = _kb_body_range(node)
    body = _kb_extract_node_body(lines, node)
    children = _kb_node_children(nodes, node)
    out = [
        prefix,
        f"  id: {node.id_disambiguated}",
        f"  body: L{body_start}-{body_end} ({_kb_line_count(body_start, body_end)} lines, {len(body)} chars)",
        f"  subtree: L{node.line_start}-{node.subtree_end} ({_kb_line_count(node.line_start, node.subtree_end)} lines)",
    ]
    if children:
        child_labels = [
            f"{number_by_line.get(child.line_start, 0):03d} H{child.depth} L{child.line_start} {child.title}"
            for child in children
        ]
        out.append("  children: " + "; ".join(child_labels))
    else:
        out.append("  children: (none)")
    preview = _kb_preview_text(body, max_chars)
    out.append("  body_preview: " + (preview if preview else "(empty)"))
    return out


@mcp.tool(name="kb_list", description="List configured markdown knowledge files.")
async def kb_list() -> str:
    root = _current_project_root()
    files = _kb_configured_files(root)
    if not files:
        return _kb_error_text("NotConfigured", "No knowledge files in .agent-pty.toml")
    out = [f"[kb_list: {len(files)} files  root: {root}]"]
    for f in files:
        out.append(f"  {f}")
    return "\n".join(out)


@mcp.tool(name="kb_reload_config", description="Reload .agent-pty.toml config for the current project root.")
async def kb_reload_config() -> str:
    root = _current_project_root()
    files = _kb_configured_files(root)
    out = [f"[kb_reload_config: OK  root: {root}]"]
    out.append(f"  files: {', '.join(files) if files else '(none)'}")
    return "\n".join(out)


@mcp.tool(name="kb_add_file", description="Add a file (absolute path) to knowledge.files and hot-reload config.")
async def kb_add_file(abs_path: str) -> str:
    if not abs_path.strip():
        return _kb_error_text("InvalidParameter", "abs_path is required")
    p = _logical_abspath(abs_path)
    if not p.is_absolute():
        return _kb_error_text("InvalidParameter", "abs_path must be an absolute path")
    if not p.exists() or not p.is_file():
        return _kb_error_text("FileNotAllowed", f"File not found: {p}")

    roots = _current_project_roots()
    root = roots[0]
    rel: Optional[Path] = None
    for candidate_root in roots:
        try:
            rel = p.relative_to(candidate_root)
            break
        except Exception:
            continue
    if rel is None:
        return _kb_error_text(
            "FileNotAllowed",
            "File must be inside current project root",
            project_root=str(root),
            abs_path=str(p),
        )

    rel_str = rel.as_posix()
    if ".." in rel.parts:
        return _kb_error_text("FileNotAllowed", "Path traversal not allowed", path=rel_str)

    files = _load_kb_files_config(root=root)

    if rel_str not in files:
        files.append(rel_str)

    _save_kb_files_config(files, root=root)

    all_files = _kb_configured_files(root)
    config_path = _logical_abspath(root / ".agent-pty.toml")
    return f"[kb_add_file: OK  added: {rel_str}]\n  config: {config_path}\n  files: {', '.join(all_files)}"


@mcp.tool(name="kb_remove_file", description="Remove a file (absolute path) from knowledge.files and hot-reload config.")
async def kb_remove_file(abs_path: str) -> str:
    if not abs_path.strip():
        return _kb_error_text("InvalidParameter", "abs_path is required")
    p = _logical_abspath(abs_path)
    if not p.is_absolute():
        return _kb_error_text("InvalidParameter", "abs_path must be an absolute path")

    roots = _current_project_roots()
    root = roots[0]
    rel: Optional[Path] = None
    for candidate_root in roots:
        try:
            rel = p.relative_to(candidate_root)
            break
        except Exception:
            continue
    if rel is None:
        return _kb_error_text(
            "FileNotAllowed",
            "File must be inside current project root",
            project_root=str(root),
            abs_path=str(p),
        )

    rel_str = rel.as_posix()
    if ".." in rel.parts:
        return _kb_error_text("FileNotAllowed", "Path traversal not allowed", path=rel_str)

    files = _load_kb_files_config(root=root)
    if rel_str not in files:
        return _kb_error_text(
            "FileNotConfigured",
            "File is not registered in knowledge.files",
            file=rel_str,
        )

    files = [f for f in files if f != rel_str]
    _save_kb_files_config(files, root=root)

    all_files = _kb_configured_files(root)
    config_path = _logical_abspath(root / ".agent-pty.toml")
    rendered_files = ", ".join(all_files) if all_files else "(none)"
    return f"[kb_remove_file: OK  removed: {rel_str}]\n  config: {config_path}\n  files: {rendered_files}"


@mcp.tool(name="kb_schema", description="Show the complete numbered ATX heading index for one markdown knowledge file.")
async def kb_schema(
    file: Optional[str] = None,
    target: Optional[str] = None,
    id: Optional[str] = None,
    max_depth: Optional[int] = None,
    root_depth: Optional[int] = None,
) -> str:
    selected_file = _kb_select_target_file(file, target)
    if isinstance(selected_file, dict):
        return _kb_dict_to_error_text(selected_file)
    resolved = _kb_resolve_file(selected_file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    nodes = parse_markdown(text)
    if not nodes:
        return f"[schema: {path}]\n(no sections found)"

    # Root listing mode: one compact line per ATX heading, not a nested JSON tree.
    if not id:
        if root_depth is None:
            top_depth = min(node.depth for node in nodes)
        else:
            top_depth = int(root_depth)
        rel_depth = _kb_depth_value(max_depth)
        upper_depth = None if rel_depth is None else top_depth + rel_depth
        included = [
            (idx, node)
            for idx, node in enumerate(nodes, start=1)
            if node.depth >= top_depth and (upper_depth is None or node.depth <= upper_depth)
        ]
        header = (
            f"[schema: {path.name}  sections: {len(included)}/{len(nodes)}  root_depth: {top_depth}  "
            f"max_depth: {_kb_depth_label(rel_depth)}]"
        )
        help_line = "Use section numbers, L<line>, heading paths, unique titles, or suffixes with kb_info/kb_read/kb_write."
        outline = [_kb_schema_line(node, idx) for idx, node in included]
        return "\n".join([header, help_line, *outline])

    lines = text.splitlines()
    target_node = _resolve_section_selector_or_error(nodes, id, total_lines=len(lines))
    if isinstance(target_node, dict):
        return _kb_dict_to_error_text(target_node)

    rel_depth = _kb_depth_value(max_depth)
    upper_depth = None if rel_depth is None else target_node.depth + rel_depth
    subtree_nodes = [
        (idx, node)
        for idx, node in enumerate(nodes, start=1)
        if (
            node.line_start == target_node.line_start
            or (node.line_start > target_node.line_start and node.line_start <= target_node.subtree_end)
        )
        and (upper_depth is None or node.depth <= upper_depth)
    ]
    header = (
        f"[schema: {path.name}  target: {_kb_section_label(target_node)}  "
        f"sections: {len(subtree_nodes)}  max_depth: {_kb_depth_label(rel_depth)}]"
    )
    help_line = "Use section numbers, L<line>, heading paths, unique titles, or suffixes with kb_info/kb_read/kb_write."
    outline = [_kb_schema_line(node, idx) for idx, node in subtree_nodes]
    return "\n".join([header, help_line, *outline])


@mcp.tool(name="kb_info", description="Show parent-chain context, ranges, children, and body previews for selected KB sections.")
async def kb_info(
    file: Optional[str] = None,
    target: Optional[str] = None,
    section: Optional[str] = None,
    sections: Optional[str] = None,
    id: str = "",
    max_chars: int = 600,
) -> str:
    selected_file = _kb_select_target_file(file, target)
    if isinstance(selected_file, dict):
        return _kb_dict_to_error_text(selected_file)
    resolved = _kb_resolve_file(selected_file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    lines = text.splitlines()
    nodes = parse_markdown(text)
    selected = _resolve_section_list_or_error(
        nodes,
        sections,
        id,
        total_lines=len(lines),
        allow_root=False,
        section_value=section,
    )
    if isinstance(selected, dict):
        return _kb_dict_to_error_text(selected)

    number_by_line = _kb_node_number_map(nodes)
    out = [
        f"[info: {path.name}  selected: {len(selected)}  body_preview_chars: {max(0, int(max_chars))}]",
        "Selectors accept schema numbers, L<line>, full ids, unique titles, or unique trailing id suffixes.",
    ]
    for selected_idx, node in enumerate(selected, start=1):
        out.append("")
        out.append(f"## Selection {selected_idx}")
        ancestors = _kb_node_ancestors(nodes, node)
        if ancestors:
            out.append("parent_chain:")
            for ancestor in ancestors:
                out.extend(
                    "  " + line
                    for line in _kb_node_info_block(
                        lines,
                        nodes,
                        ancestor,
                        number_by_line=number_by_line,
                        max_chars=max_chars,
                        label="parent",
                    )
                )
        else:
            out.append("parent_chain: (none)")
        out.append("target:")
        out.extend(
            "  " + line
            for line in _kb_node_info_block(
                lines,
                nodes,
                node,
                number_by_line=number_by_line,
                max_chars=max_chars,
                label="target",
            )
        )
    return "\n".join(out)


@mcp.tool(name="kb_read", description="Read selected KB section bodies with parent heading/body context.")
async def kb_read(
    file: Optional[str] = None,
    target: Optional[str] = None,
    section: Optional[str] = None,
    sections: Optional[str] = None,
    id: str = "",
    include_children: bool = False,
    max_chars: int = 20000,
) -> str:
    selected_file = _kb_select_target_file(file, target)
    if isinstance(selected_file, dict):
        return _kb_dict_to_error_text(selected_file)
    resolved = _kb_resolve_file(selected_file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    lines = text.splitlines()
    nodes = parse_markdown(text)
    selected = _resolve_section_list_or_error(
        nodes,
        sections,
        id,
        total_lines=len(lines),
        allow_root=True,
        section_value=section,
    )
    if isinstance(selected, dict):
        return _kb_dict_to_error_text(selected)

    number_by_line = _kb_node_number_map(nodes)
    out = [
        f"[read: {path.name}  selected: {len(selected)}  include_children: {include_children}  max_chars: {max_chars}]",
        "Returned blocks include parent heading/body context before each selected target.",
    ]
    for selected_idx, node in enumerate(selected, start=1):
        out.append("")
        out.append(f"## Selection {selected_idx}: {_kb_section_label(node)}")
        if node.depth == 0:
            section_text = _extract_line_range(lines, node.line_start, node.subtree_end)
            out.append(f"[file-root L{node.line_start}-{node.subtree_end} hash: {_content_hash(section_text)}]")
            out.append(section_text)
            continue

        for ancestor in _kb_node_ancestors(nodes, node):
            number = number_by_line.get(ancestor.line_start, 0)
            start, end = _kb_heading_body_range(ancestor)
            block = _kb_extract_heading_body(lines, ancestor)
            out.append("")
            out.append(
                f"[parent {number:03d} H{ancestor.depth} L{start}-{end} id: {ancestor.id_disambiguated}]"
            )
            out.append(block)

        number = number_by_line.get(node.line_start, 0)
        if include_children:
            start, end = node.line_start, node.subtree_end
            block = _extract_line_range(lines, start, end)
            mode = "target_subtree"
        else:
            start, end = _kb_heading_body_range(node)
            block = _kb_extract_heading_body(lines, node)
            mode = "target_body"
        out.append("")
        out.append(f"[{mode} {number:03d} H{node.depth} L{start}-{end} id: {node.id_disambiguated}]")
        out.append(block)

    return _kb_apply_max_chars("\n".join(out), max_chars)


@mcp.tool(name="kb_search_headers", description="Case-insensitive substring search across heading titles.")
async def kb_search_headers(
    file: Optional[str] = None,
    target: Optional[str] = None,
    query: str = "",
    max_hits: int = 25,
) -> str:
    q = (query or "").strip()
    resolved = _kb_resolve_search_files(file, target)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    paths = resolved
    scope = _kb_display_path(paths[0]) if len(paths) == 1 else "all KB files"
    if not q:
        return f"[search_headers: {scope}  query: (empty)]\nNo query provided."
    qf = q.casefold()
    cap = max(1, int(max_hits))
    out: list[str] = []
    total_matches = 0
    for path in paths:
        text = _read_text_or_error(path)
        if isinstance(text, dict):
            return _kb_dict_to_error_text(text)
        nodes = parse_markdown(text)
        matches = [node for node in nodes if qf in node.title.casefold()]
        total_matches += len(matches)
        number_by_line = _kb_node_number_map(nodes)
        for node in matches:
            if len(out) >= cap:
                break
            line = _kb_schema_line(node, number_by_line.get(node.line_start, 0))
            if len(paths) > 1:
                line = f"{_kb_display_path(path)}: {line}"
            out.append(line)
    header = (
        f"[search_headers: {scope}  query: {q}  "
        f"matches: {len(out)}/{total_matches}  max_hits: {cap}]"
    )
    if not out:
        out.append("  (no matches)")
    return "\n".join([header, *out])


def _kb_search_body_match_blocks(
    *,
    path: Path,
    text: str,
    pattern: re.Pattern[str],
    max_hits: int,
    preview_chars: int,
    from_match: bool,
    start_index: int = 1,
    include_file: bool = False,
) -> list[str]:
    lines = text.splitlines()
    nodes = parse_markdown(text)
    number_by_line = _kb_node_number_map(nodes)
    cap = max(1, int(max_hits))
    preview_cap = max(20, int(preview_chars))
    results: list[str] = []

    for node in nodes:
        if len(results) >= cap:
            break
        body_start, body_end = _kb_body_range(node)
        body_text = _extract_line_range(lines, body_start, body_end)
        if not body_text:
            continue
        body_offsets: list[int] = []
        offset = 0
        for line_no in range(body_start, body_end + 1):
            if line_no > len(lines):
                break
            body_offsets.append(offset)
            offset += len(lines[line_no - 1]) + 1
        for match in pattern.finditer(body_text):
            if len(results) >= cap:
                break
            match_start = match.start()
            line_index = 0
            for idx, start_offset in enumerate(body_offsets):
                if start_offset <= match_start:
                    line_index = idx
                else:
                    break
            line_no = body_start + line_index
            if from_match:
                preview_source = body_text[match.start() :]
            else:
                preview_source = body_text
            preview = _kb_preview_text(preview_source, preview_cap)
            number = number_by_line.get(node.line_start, 0)
            prefix = f"[{start_index + len(results)}]"
            if include_file:
                prefix += f" file {_kb_display_path(path)}"
            results.append(
                "\n".join(
                    [
                        f"{prefix} section {number:03d} H{node.depth} match_line L{line_no}",
                        f"  id: {node.id_disambiguated}",
                        f"  preview: {preview if preview else '(empty)'}",
                    ]
                )
            )
    return results


@mcp.tool(name="kb_search", description="Regex-capable section-aware KB body search with previews.")
async def kb_search(
    file: Optional[str] = None,
    target: Optional[str] = None,
    query: str = "",
    regex: bool = False,
    max_hits: int = 10,
    preview_chars: int = 240,
    from_match: bool = True,
) -> str:
    q = (query or "").strip()
    resolved = _kb_resolve_search_files(file, target)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    paths = resolved
    scope = _kb_display_path(paths[0]) if len(paths) == 1 else "all KB files"
    if not q:
        return f"[search: {scope}  query: (empty)]\nNo query provided."

    flags = re.IGNORECASE | re.MULTILINE
    try:
        pattern = re.compile(q if regex else re.escape(q), flags)
    except re.error as exc:
        return _kb_error_text("InvalidParameter", f"Invalid regex: {exc}", query=q)

    cap = max(1, int(max_hits))
    preview_cap = max(20, int(preview_chars))
    results: list[str] = []
    for path in paths:
        if len(results) >= cap:
            break
        text = _read_text_or_error(path)
        if isinstance(text, dict):
            return _kb_dict_to_error_text(text)
        results.extend(
            _kb_search_body_match_blocks(
                path=path,
                text=text,
                pattern=pattern,
                max_hits=cap - len(results),
                preview_chars=preview_cap,
                from_match=from_match,
                start_index=len(results) + 1,
                include_file=len(paths) > 1,
            )
        )

    out = (
        f"[search: {scope}  query: {q}  regex: {regex}  "
        f"matches: {len(results)}  max_hits: {cap}]"
    )
    if results:
        return "\n".join([out, *results])
    return "\n".join([out, "(no matches)"])


@mcp.tool(name="kb_search_content", description="Alias for kb_search.")
async def kb_search_content(
    file: Optional[str] = None,
    target: Optional[str] = None,
    query: str = "",
    max_results: int = 10,
    context_chars: int = 240,
    regex: bool = False,
    from_match: bool = True,
) -> str:
    return cast(str, await kb_search(
        file=file,
        target=target,
        query=query,
        regex=regex,
        max_hits=max_results,
        preview_chars=context_chars,
        from_match=from_match,
    ))


@mcp.tool(name="kb_help", description="List KB tool modes, section-id forms, and common examples.")
async def kb_help() -> str:
    return """[kb_help]
Section ids:
  - kb_schema(target="...") returns every ATX heading as a numbered index
  - selectors accept schema numbers, L<line>, heading paths, unique titles, or unique trailing path suffixes
  - kb_read requires an explicit selector; section="" or sections="root" targets the file root

kb_write modes:
  - append: append content to the target section body
  - heading: create a child heading after the target section subtree
  - child/create_child: aliases for heading
  - heading_title creates a child heading automatically when mode="append"
  - spacing="auto" normalizes blank lines for heading creation; spacing="preserve" keeps raw heading/content adjacency
  example: kb_write(target="AGENTS.md", section="12", mode="create_child", heading_title="Auth", content="Notes")

kb_update modes:
  - body: replace only the section body
  - replace: alias for body
  - subtree: replace the heading and all descendants
  - spacing="auto" normalizes one blank line after headings; spacing="preserve" keeps exact raw replacement

kb_remove modes:
  - subtree: remove the heading and descendants
  - body: remove only the section body

Discovery:
  - kb_list shows configured files
  - kb_schema(target="...") returns a compact complete heading index for one target file
  - kb_info(target="...", sections="1,L42") shows parent-chain context, ranges, child headings, and body previews
  - kb_read(target="...", sections="1,L42") returns parent heading/body context plus selected target bodies
  - kb_search(query="...", regex=true, max_hits=10, preview_chars=240) searches all configured files
  - kb_search(target="...", query="...") limits search to one target file"""


@mcp.resource(
    "kb://knowledge",
    name="knowledge",
    title="Knowledge base",
    description="Configured KB files plus KB tool usage help.",
    mime_type="text/plain",
)
async def kb_resource_knowledge() -> str:
    return f"{await kb_list()}\n\n{await kb_help()}"


def _kb_heading_insert_lines(
    existing_lines: list[str],
    insert_at: int,
    heading_line: str,
    content_lines: list[str],
) -> list[str]:
    insert_lines: list[str] = []
    if insert_at > 0 and existing_lines[insert_at - 1].strip():
        insert_lines.append("")
    insert_lines.append(heading_line)
    insert_lines.append("")
    normalized_content = _kb_normalize_markdown_spacing(_strip_outer_blank_lines(content_lines))
    if normalized_content:
        insert_lines.extend(normalized_content)
    if (
        insert_at < len(existing_lines)
        and insert_lines
        and insert_lines[-1].strip()
        and existing_lines[insert_at].strip()
    ):
        insert_lines.append("")
    return insert_lines


@mcp.tool(name="kb_write", description="Append content to one KB section, or create a child heading under it.")
async def kb_write(
    target: Optional[str] = None,
    section: str = "",
    content: str = "",
    mode: str = "append",
    heading_title: Optional[str] = None,
    heading_depth: Optional[int] = None,
    spacing: str = "auto",
    dry_run: bool = False,
) -> str:
    resolved = _kb_resolve_target(target)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    selected = _resolve_single_section_or_error(nodes, section, total_lines=len(lines), allow_root=True)
    if isinstance(selected, dict):
        return _kb_dict_to_error_text(selected)

    normalized_mode = str(mode or "append").strip().lower().replace("-", "_")
    spacing_mode = str(spacing or "auto").strip().lower().replace("-", "_")
    if spacing_mode not in {"auto", "preserve"}:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported spacing '{spacing}'",
            section=section,
            mode=mode,
            allowed_spacing="auto, preserve",
            example='Use spacing="auto" for Markdown section creation or spacing="preserve" for exact raw insertion.',
        )
    insert_at = max(0, selected.body_end)
    is_heading_write = False
    if heading_title and normalized_mode == "append":
        normalized_mode = "heading"
    if normalized_mode == "append":
        insert_lines = (content or "").splitlines()
    elif normalized_mode in {"heading", "child", "create_child"}:
        is_heading_write = True
        # Insert after the entire subtree, not just the body
        insert_at = max(0, selected.subtree_end)
        if not heading_title:
            return _kb_error_text(
                "InvalidParameter",
                "heading_title is required for child-heading writes",
                section=section,
                mode=mode,
                allowed_modes="append, heading, child, create_child",
                example='kb_write(target="...", section="12", mode="create_child", heading_title="New Section", content="...")',
            )
        depth = _coerce_optional_int(heading_depth) if heading_depth is not None else (selected.depth + 1)
        if not isinstance(depth, int) or depth < 1 or depth > 6:
            return _kb_error_text("InvalidParameter", "heading_depth must be between 1 and 6", section=section, mode=mode)
        heading_line = f"{'#' * depth} {heading_title}"
        content_lines = (content or "").splitlines()
        if spacing_mode == "auto":
            insert_lines = _kb_heading_insert_lines(lines, insert_at, heading_line, content_lines)
        else:
            insert_lines = [heading_line, *content_lines]
    else:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported mode '{mode}'",
            section=section,
            allowed_modes="append, heading, child, create_child",
            example='Use mode="create_child" with heading_title for a new child heading, or mode="append" to append body text.',
        )

    if normalized_mode == "append" and spacing_mode == "auto":
        insert_lines = _kb_auto_append_lines(lines, insert_at, content)

    new_lines = lines[:insert_at] + insert_lines + lines[insert_at:]
    new_text = "\n".join(new_lines)
    if (had_trailing_newline or (is_heading_write and spacing_mode == "auto")) and not new_text.endswith("\n"):
        new_text += "\n"

    diff = _unified_diff(old_text, new_text, str(path))
    action = "DRY RUN" if dry_run else "WRITTEN"
    if not dry_run:
        _atomic_write(path, new_text)
    return _render_kb_result(f"[kb_write: {action}  hash: {_content_hash(new_text)}]", diff)


@mcp.tool(name="kb_update", description="Replace the body of one KB section, or replace its full subtree.")
async def kb_update(
    target: Optional[str] = None,
    section: str = "",
    content: str = "",
    mode: str = "body",
    spacing: str = "auto",
    dry_run: bool = False,
) -> str:
    resolved = _kb_resolve_target(target)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    selected = _resolve_single_section_or_error(nodes, section, total_lines=len(lines), allow_root=True)
    if isinstance(selected, dict):
        return _kb_dict_to_error_text(selected)

    normalized_mode = str(mode or "body").strip().lower()
    if normalized_mode == "replace":
        normalized_mode = "body"
    spacing_mode = str(spacing or "auto").strip().lower().replace("-", "_")
    if spacing_mode not in {"auto", "preserve"}:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported spacing '{spacing}'",
            section=section,
            mode=mode,
            allowed_spacing="auto, preserve",
            example='Use spacing="auto" for deterministic Markdown spacing or spacing="preserve" for exact raw replacement.',
        )

    if normalized_mode == "body":
        start, end = selected.body_start, selected.body_end
        if spacing_mode == "auto":
            replacement_lines = _kb_auto_body_lines(content, has_heading=selected.depth > 0)
        else:
            replacement_lines = (content or "").splitlines()
    elif normalized_mode == "subtree":
        start, end = selected.line_start, selected.subtree_end
        if spacing_mode == "auto":
            replacement_lines = _kb_normalize_markdown_spacing(
                _strip_outer_blank_lines((content or "").splitlines())
            )
        else:
            replacement_lines = (content or "").splitlines()
    else:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported mode '{mode}'",
            section=section,
            allowed_modes="body, replace, subtree",
        )

    new_lines = _replace_line_range_with_lines(lines, start, end, replacement_lines)
    new_text = "\n".join(new_lines)
    if had_trailing_newline:
        new_text += "\n"
    diff = _unified_diff(old_text, new_text, str(path))

    action = "DRY RUN" if dry_run else "WRITTEN"
    if not dry_run:
        _atomic_write(path, new_text)
    return _render_kb_result(f"[kb_update: {action}  hash: {_content_hash(content)}]", diff)


@mcp.tool(name="kb_remove", description="Remove the body of one KB section, or remove its full subtree.")
async def kb_remove(
    target: Optional[str] = None,
    section: str = "",
    mode: str = "subtree",
    dry_run: bool = False,
) -> str:
    resolved = _kb_resolve_target(target)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    selected = _resolve_single_section_or_error(nodes, section, total_lines=len(lines), allow_root=True)
    if isinstance(selected, dict):
        return _kb_dict_to_error_text(selected)

    normalized_mode = str(mode or "subtree").strip().lower().replace("-", "_")
    if normalized_mode == "subtree":
        start, end = selected.line_start, selected.subtree_end
    elif normalized_mode == "body":
        start, end = selected.body_start, selected.body_end
    else:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported mode '{mode}'",
            section=section,
            allowed_modes="body, subtree",
        )

    new_lines = _replace_line_range(lines, start, end, "")
    new_text = "\n".join(new_lines)
    if had_trailing_newline and new_text:
        new_text += "\n"
    diff = _unified_diff(old_text, new_text, str(path))

    action = "DRY RUN" if dry_run else "REMOVED"
    if not dry_run:
        _atomic_write(path, new_text)
    return _render_kb_result(f"[kb_remove: {action}]", diff)


# =============================================================================
# Agent-to-Agent User Message Tools
# =============================================================================
# These tools allow an agent to send a user message to codex-app-server,
# appearing as an agent-initiated prompt with metadata.

_APPSERVER_MESSAGE_URL = "http://127.0.0.1:12359/api/appserver/message"


def _build_agent_message_header(
    pseudonym: str,
    model: str,
    repo: str,
    subject: str,
    reply_to: Optional[str] = None,
) -> str:
    """Build the agent message header block."""
    header = f"""[AGENT MESSAGE]
from: {pseudonym}
model: {model}
repo: {repo}
subject: {subject}"""
    if reply_to:
        header += f"\nreply_to: {reply_to}"
    header += "\n---\n"
    return header


async def _send_agent_user_message(
    conversation_id: str,
    pseudonym: str,
    model: str,
    repo: str,
    subject: str,
    message: str,
    reply_to: Optional[str] = None,
) -> ObjectMap:
    """Send a user message to codex-app-server via unified message endpoint.
    
    Uses /api/appserver/message which handles all thread resolution:
    - Looks up thread_id from conversation meta
    - Resumes thread if needed
    - Starts new thread if none exists
    - Sends turn/start with the message
    """
    import urllib.request
    
    header = _build_agent_message_header(pseudonym, model, repo, subject, reply_to)
    full_message = header + message
    
    # Use unified message endpoint - it handles all thread resolution
    payload = {
        "conversation_id": conversation_id,
        "text": full_message
    }
    
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    
    try:
        def _post() -> ObjectMap:
            req = urllib.request.Request(
                _APPSERVER_MESSAGE_URL,
                data=payload_bytes,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with cast(_UrlopenResponse, urllib.request.urlopen(req, timeout=15)) as resp:
                return _json_loads_object_map(_read_urlopen_response_body(resp))
        result = await asyncio.to_thread(_post)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="agent_send_message", description="Send message to another agent. ASK THE USER for your conversation_id to use as reply_to.")
async def agent_send_message(
    conversation_id: str,
    pseudonym: str,
    model: str,
    repo: str,
    subject: str,
    message: str,
    reply_to: str,
) -> ObjectMap:
    """
    Send a user message to another codex agent via codex-app-server.
    
    Args:
        conversation_id: Target agent's conversation ID (where to send)
        pseudonym: Your identifier (e.g., "Copilot", "Codex-Main")
        model: Your model name (e.g., "gpt-5.2", "claude-sonnet-4")
        repo: Your working repository path
        subject: Brief subject/topic of the message
        message: The actual message content
        reply_to: YOUR conversation ID (so recipient can reply back to you)
    
    Returns:
        {ok: true, thread_id, conversation_id} on success
    """
    if not conversation_id or not message or not reply_to:
        return {"ok": False, "error": "conversation_id, message, and reply_to are required"}
    
    return await _send_agent_user_message(
        conversation_id=conversation_id,
        pseudonym=pseudonym or "Agent",
        model=model or "unknown",
        repo=repo or "unknown",
        subject=subject or "Agent Message",
        message=message,
        reply_to=reply_to,
    )


@mcp.tool(name="agent_send_message_await", description="Send message to another agent and wait for reply. ASK THE USER for your conversation_id to use as reply_to.")
async def agent_send_message_await(
    conversation_id: str,
    pseudonym: str,
    model: str,
    repo: str,
    subject: str,
    message: str,
    reply_to: str,
    await_from: Optional[str] = None,
    timeout_ms: int = 300000,
) -> ObjectMap:
    """
    Send a user message to another codex agent and wait for their response on the agent log.
    
    Args:
        conversation_id: Target agent's conversation ID (where to send)
        pseudonym: Your identifier
        model: Your model name
        repo: Your working repository path
        subject: Brief subject/topic
        message: The message content (should instruct recipient to respond via agent log)
        reply_to: YOUR conversation ID (so recipient can reply back to you)
        await_from: Optional - only accept log responses from this author
        timeout_ms: How long to wait for response (default 5 minutes)
    
    Returns:
        {ok: true, thread_id, reply_msg_num, reply: "..."} on success
    """
    if not conversation_id or not message or not reply_to:
        return {"ok": False, "error": "conversation_id, message, and reply_to are required"}
    
    # Get current highest msg_num before sending
    messages = await _agent_log_fetch(1)
    after_msg_num = 0
    if messages:
        after_msg_num = _coerce_optional_int(messages[0].get("msg_num")) or 0
    
    # Send the message
    send_result = await _send_agent_user_message(
        conversation_id=conversation_id,
        pseudonym=pseudonym or "Agent",
        model=model or "unknown",
        repo=repo or "unknown",
        subject=subject or "Agent Message",
        message=message,
        reply_to=reply_to,
    )
    
    if not send_result.get("ok"):
        return send_result
    
    # Wait for response on agent log
    await_result = await _agent_log_await(after_msg_num, await_from, timeout_ms)
    
    if await_result.get("error") == "timeout":
        return {
            "ok": False,
            "error": "timeout waiting for reply on agent log",
            "rpc_id": send_result.get("rpc_id"),
        }
    
    if "msg_num" in await_result:
        return {
            "ok": True,
            "rpc_id": send_result.get("rpc_id"),
            "reply_msg_num": await_result["msg_num"],
            "reply": f"#{await_result['msg_num']} [{await_result.get('ts', '')}] {await_result.get('who', '')}:\n{await_result.get('message', '')}",
        }
    
    return {"ok": False, "error": await_result.get("error", "unknown")}


async def _main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "").strip().lower()
    if transport in ("streamable-http", "streamable_http", "http"):
        _configure_http_transport_from_env()
        await mcp.run_streamable_http_async()
        return
    if transport == "sse":
        _configure_http_transport_from_env()
        mount_path = os.environ.get("MCP_MOUNT_PATH") or None
        await mcp.run_sse_async(mount_path=mount_path)
        return
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())

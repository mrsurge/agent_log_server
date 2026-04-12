#!/usr/bin/env python3
import asyncio
import base64
import difflib
import hashlib
import json
import os
import sys
import secrets
import time
import contextlib
import io
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import socketio

from agent_log_server.markdown_sections import SectionNode, normalize_heading as _normalize_heading, parse_markdown
from agent_log_server.ipc_auth import load_or_create_ipc_secret
from agent_log_server.prompt_context import REPO_MEMORY_FILENAME
from agent_log_server.repo_memory_delta import build_repo_memory_delta
from agent_log_server import conversation_todos as _conv_todos


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


def _load_project_config(root: Optional[Path] = None) -> dict:
    """Load .agent-pty.toml from a directory (defaults to the current project root)."""
    base = _logical_abspath(root) if root is not None else _current_project_root()
    config_path = base / ".agent-pty.toml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _kb_configured_files(root: Optional[Path] = None) -> list[str]:
    """Return the list of knowledge files from config, validated."""
    raw = _load_project_config(root).get("knowledge", {}).get("files", [])
    if isinstance(raw, str):
        raw = [raw]
    result: list[str] = []
    for f in raw:
        p = Path(f)
        # Security: no absolute paths, no .. traversal
        if p.is_absolute() or ".." in p.parts:
            continue
        result.append(str(p))
    return result


_APPSERVER_ORIGIN = os.environ.get("AGENT_LOG_SERVER_ORIGIN", "http://127.0.0.1:12359")
_APPSERVER_IPC_NAMESPACE = "/ipc"
_appserver_ipc_sio: Optional[socketio.AsyncClient] = None
_appserver_ipc_lock = asyncio.Lock()
_ask_user_pending_requests: dict[str, asyncio.Future] = {}


async def _get_appserver_ipc_sio() -> socketio.AsyncClient:
    global _appserver_ipc_sio
    async with _appserver_ipc_lock:
        if _appserver_ipc_sio and _appserver_ipc_sio.connected:
            return _appserver_ipc_sio
        if _appserver_ipc_sio:
            with contextlib.suppress(Exception):
                await _appserver_ipc_sio.disconnect()
            _appserver_ipc_sio = None

        client = socketio.AsyncClient(reconnection=True, reconnection_attempts=3)

        async def _on_ask_user_response(data):
            if not isinstance(data, dict):
                return
            request_id = str(
                data.get("request_id")
                or data.get("requestId")
                or data.get("id")
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
                waiter.set_result(dict(data))

        async def _on_ask_user_terminal(data):
            if not isinstance(data, dict):
                return
            request_id = str(
                data.get("request_id")
                or data.get("requestId")
                or data.get("interaction_id")
                or data.get("id")
                or ""
            ).strip()
            if not request_id:
                return
            waiter = _ask_user_pending_requests.get(request_id)
            print(
                f"[ask_user mcp] recv_terminal request_id={request_id} waiter={'yes' if waiter and not waiter.done() else 'no'} status={data.get('status')!r}",
                file=sys.stderr,
                flush=True,
            )
            if waiter and not waiter.done():
                waiter.set_result(dict(data))

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


async def _notify_repo_memory_ipc(path: Path, old_text: str, new_text: str) -> Optional[str]:
    if path.name != REPO_MEMORY_FILENAME:
        return None

    previous_content = old_text.strip()
    current_content = new_text.strip()
    update_ts = time.time()
    delta_content = build_repo_memory_delta(
        previous_content,
        current_content,
        source_path=str(path),
        ts=update_ts,
    )
    payload = {
        "source_path": str(path),
        "previous_content": previous_content,
        "current_content": current_content,
        "delta_content": delta_content or "",
        "ts": update_ts,
    }
    client = await _get_appserver_ipc_sio()
    ack = await client.call(
        "repo_memory_delta",
        payload,
        namespace=_APPSERVER_IPC_NAMESPACE,
        timeout=10,
    )
    if not isinstance(ack, dict) or not ack.get("ok"):
        detail = ack.get("error") if isinstance(ack, dict) else ack
        raise RuntimeError(f"repo_memory_delta IPC failed: {detail}")
    return (
        f"[kb_ipc: OK  queued: {int(ack.get('queued') or 0)}"
        f"  mode: {ack.get('mode') or '-'}  hash: {ack.get('content_hash') or '-'}]"
    )


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
    if not isinstance(ack, dict) or not ack.get("ok"):
        detail = ack.get("error") if isinstance(ack, dict) else ack
        raise RuntimeError(f"conversation_todo_changed IPC failed: {detail}")


def _render_kb_result(header: str, diff: str, ipc_note: Optional[str] = None) -> str:
    parts = [header]
    if isinstance(ipc_note, str) and ipc_note.strip():
        parts.append(ipc_note.strip())
    parts.append(diff)
    return "\n".join(parts)


_DEFAULT_CONVERSATION_DIR = Path(os.path.expanduser("~/.cache/app_server/conversations"))
_conv_todos.configure(_DEFAULT_CONVERSATION_DIR)


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


def _resolve_section(nodes: list[SectionNode], section_id: str) -> SectionNode | None:
    """Resolve a section_id to a unique node.

    Returns None if not found. Raises ValueError if ambiguous.
    """
    if not isinstance(section_id, str) or not section_id:
        return None

    by_id = [node for node in nodes if node.id == section_id]
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        by_disambiguated = [node for node in nodes if node.id_disambiguated == section_id]
        if len(by_disambiguated) == 1:
            return by_disambiguated[0]
        raise ValueError(f"Ambiguous section_id: {section_id}")

    by_disambiguated = [node for node in nodes if node.id_disambiguated == section_id]
    if len(by_disambiguated) == 1:
        return by_disambiguated[0]
    if len(by_disambiguated) > 1:
        raise ValueError(f"Ambiguous section_id: {section_id}")
    return None


mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")

_ASK_USER_CARD_KIND = "ask_user"
_ASK_USER_ANSWER_FIELD = "answer"


# Diagnostic markers for stdio MCP process lifetime
print(f"MCP SERVER STARTED pid={os.getpid()}", file=sys.stderr)

@mcp.tool(name="ping", description="Return MCP server pid (diagnostic).")
async def ping() -> Dict[str, Any]:
    return {"ok": True, "pid": os.getpid()}


def _normalize_choice_list(raw_choices: Optional[list[str]]) -> list[str]:
    if not isinstance(raw_choices, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_choices:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _build_ask_user_requested_schema(
    *,
    question: str,
    choices: list[str],
    allow_freeform: bool,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "title": "User Input",
        "additionalProperties": False,
        "required": [_ASK_USER_ANSWER_FIELD],
        "properties": {
            _ASK_USER_ANSWER_FIELD: {
                "type": "array",
                "items": {"type": "string"},
                "title": "Answer",
            },
        },
        "x-agent-pty-card": _ASK_USER_CARD_KIND,
        "x-agent-pty-question": question,
        "x-agent-pty-choices": list(choices),
        "x-agent-pty-allowFreeform": bool(allow_freeform),
    }


def _extract_ask_user_answers(content: Any) -> list[str]:
    payload = content if isinstance(content, dict) else {}
    raw_answers = payload.get(_ASK_USER_ANSWER_FIELD)
    if isinstance(raw_answers, str):
        value = raw_answers.strip()
        return [value] if value else []
    if not isinstance(raw_answers, list):
        return []
    answers: list[str] = []
    for item in raw_answers:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value:
            answers.append(value)
    return answers


async def _wait_for_ask_user_event(request_id: str) -> Dict[str, Any]:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        raise RuntimeError("request_id is required")
    future = _ask_user_pending_requests.get(request_id_text)
    if not isinstance(future, asyncio.Future):
        future = asyncio.get_running_loop().create_future()
        _ask_user_pending_requests[request_id_text] = future
    try:
        return await future
    finally:
        current = _ask_user_pending_requests.get(request_id_text)
        if current is future:
            _ask_user_pending_requests.pop(request_id_text, None)


def _extract_ask_user_answers_from_resolution(resolution: Any) -> list[str]:
    payload = resolution if isinstance(resolution, dict) else {}
    answers = payload.get("answers")
    if isinstance(answers, str):
        value = answers.strip()
        return [value] if value else []
    if isinstance(answers, list):
        normalized: list[str] = []
        for item in answers:
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
        answers_from_content = _extract_ask_user_answers(content)
        if answers_from_content:
            return answers_from_content
    return []


def _coerce_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        with contextlib.suppress(Exception):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
    to_dict = getattr(value, "dict", None)
    if callable(to_dict):
        with contextlib.suppress(Exception):
            dumped = to_dict()
            if isinstance(dumped, dict):
                return dumped
    return None


def _normalize_ask_user_resolution(
    resolution: Any,
    *,
    choices: list[str],
) -> Dict[str, Any]:
    payload = resolution if isinstance(resolution, dict) else {}
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
    event: Any,
    *,
    choices: list[str],
) -> Dict[str, Any]:
    payload = event if isinstance(event, dict) else {}
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
)
async def ask_user(
    question: str,
    choices: Optional[list[str]] = None,
    allow_freeform: bool = True,
    ctx: Context | None = None,
) -> Dict[str, Any]:
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
    raw_request_id = str(getattr(ctx, "request_id", "") or "").strip()
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
        result_future = loop.create_future()
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
async def conv_id() -> Dict[str, Any]:
    cid = os.environ.get("CONVERSATION_ID", "")
    return {"ok": bool(cid), "conversation_id": cid}


# ── Conversation Todo MCP tools ──────────────────────────────────────

def _todo_cid() -> str:
    """Return the conversation ID or raise."""
    cid = os.environ.get("CONVERSATION_ID", "").strip()
    if not cid:
        raise ValueError("CONVERSATION_ID not set — not conversation-scoped")
    return cid


@mcp.tool(name="todo_list", description="List todos for this conversation. Optionally filter by status.")
async def todo_list(status: Optional[str] = None) -> Dict[str, Any]:
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
async def todo_add(title: str, description: str = "", status: str = "pending") -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
async def todo_remove(id: int) -> Dict[str, Any]:
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
async def todo_toggle(id: int) -> Dict[str, Any]:
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
async def todo_ready() -> Dict[str, Any]:
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

_AGENT_LOG_URL = "http://127.0.0.1:12359/api/messages"


async def _agent_log_fetch(limit: int = 10) -> list:
    """Fetch messages from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}?limit={limit}"
    try:
        def _get():
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        messages = await asyncio.to_thread(_get)
        return messages
    except Exception:
        return []


async def _agent_log_fetch_by_num(msg_num: int) -> Optional[dict]:
    """Fetch a specific message by msg_num from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}/{msg_num}"
    try:
        def _get():
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        return await asyncio.to_thread(_get)
    except Exception:
        return None


async def _agent_log_post_internal(who: str, message: str) -> dict:
    """Post a message to agent log server."""
    import urllib.request
    payload = json.dumps({"who": who, "message": message}, ensure_ascii=False).encode("utf-8")
    try:
        def _post():
            req = urllib.request.Request(
                _AGENT_LOG_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        return await asyncio.to_thread(_post)
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _agent_log_delete_by_num(msg_num: int) -> dict:
    """Delete a message by msg_num from agent log server."""
    import urllib.request
    url = f"{_AGENT_LOG_URL}/{msg_num}"
    try:
        def _delete():
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        return await asyncio.to_thread(_delete)
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _agent_log_await(after_msg_num: int, from_who: Optional[str] = None, timeout_ms: int = 180000) -> dict:
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
        def _post():
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                status = resp.status
                body = json.loads(resp.read().decode("utf-8"))
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
async def agent_log_post(who: str, message: str) -> Dict[str, Any]:
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
async def agent_log_inbox(limit: int = 10, preview_chars: int = 60) -> Dict[str, Any]:
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
    items = []
    for msg in messages:
        msg_num = msg.get("msg_num")
        ts = msg.get("ts", "")
        who = msg.get("who", "")
        full_message = msg.get("message", "")
        # Preview: first line, truncated
        first_line = full_message.split("\n")[0] if full_message else ""
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
    
    formatted = []
    for msg in messages:
        msg_num = msg.get("msg_num", "?")
        ts = msg.get("ts", "")
        who = msg.get("who", "")
        message = msg.get("message", "")
        formatted.append(f"#{msg_num} [{ts}] {who}:\n{message}")
    
    return "\n\n".join(formatted)


@mcp.tool(name="agent_log_delete", description="Delete a message from the agent log by its message number.")
async def agent_log_delete(msg_num: int) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
    
    posted_num = post_result["msg_num"]
    
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

def _kb_error(error: str, detail: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"error": error, "detail": detail}
    payload.update(extra)
    return payload


def _kb_error_text(error: str, detail: str, **extra: Any) -> str:
    """Format a KB error as clean plain text."""
    parts = [f"[ERROR: {error}] {detail}"]
    for k, v in extra.items():
        parts.append(f"  {k}: {v}")
    return "\n".join(parts)


def _kb_dict_to_error_text(d: Dict[str, Any]) -> str:
    """Convert an error dict from _kb_error() to plain text."""
    error = d.get("error", "Unknown")
    detail = d.get("detail", "")
    extra = {k: v for k, v in d.items() if k not in ("error", "detail")}
    return _kb_error_text(error, detail, **extra)


def _kb_resolve_file(file: Optional[str] = None) -> Path | Dict[str, Any]:
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

    if not isinstance(selected, str) or not selected:
        return _kb_error("FileNotAllowed", "Invalid file value", configured=files)
    if selected not in files:
        return _kb_error(
            "FileNotAllowed",
            f"File '{selected}' not in configured knowledge files",
            configured=files,
        )
    return _logical_abspath(root / selected)


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
    rep = (replacement or "").splitlines()
    if start < 1:
        start = 1
    insert_at = max(0, start - 1)
    if end < start:
        return lines[:insert_at] + rep + lines[insert_at:]
    left = lines[:insert_at]
    right = lines[min(end, len(lines)):]
    return left + rep + right


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


def _kb_ambiguous_section_error(section_id: str, matches: list[SectionNode]) -> Dict[str, Any]:
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
) -> SectionNode | Dict[str, Any]:
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

    return _kb_error("SectionNotFound", f"Section '{raw_id}' not found", id=raw_id)


def _read_text_or_error(path: Path) -> str | Dict[str, Any]:
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
    kb = cfg.get("knowledge", {})
    raw_files = kb.get("files", [])
    if isinstance(raw_files, str):
        return [raw_files]
    if isinstance(raw_files, list):
        return [str(x) for x in raw_files if isinstance(x, str)]
    return []


def _snippet_with_offsets(line: str, match_start: int, match_end: int, context_chars: int) -> tuple[str, int, int]:
    text = line or ""
    if context_chars < 20:
        context_chars = 20
    if len(text) <= context_chars:
        return text, match_start, match_end

    center = (match_start + match_end) // 2
    half = max(1, context_chars // 2)
    start = max(0, center - half)
    end = min(len(text), start + context_chars)
    if end - start < context_chars:
        start = max(0, end - context_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
        start_adjust = 3
    else:
        start_adjust = 0
    if end < len(text):
        snippet = snippet + "..."
    local_start = (match_start - start) + start_adjust
    local_end = (match_end - start) + start_adjust
    local_start = max(0, min(local_start, len(snippet)))
    local_end = max(local_start, min(local_end, len(snippet)))
    return snippet, local_start, local_end


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
    if not isinstance(abs_path, str) or not abs_path.strip():
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
    if not isinstance(abs_path, str) or not abs_path.strip():
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


@mcp.tool(name="kb_schema", description="Show heading schema for a markdown knowledge file with optional drill-down.")
async def kb_schema(
    file: Optional[str] = None,
    id: Optional[str] = None,
    max_depth: Optional[int] = 1,
    root_depth: Optional[int] = None,
) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    nodes = parse_markdown(text)
    if not nodes:
        return f"[schema: {path}]\n(no sections found)"

    lines = text.splitlines()

    def _fmt_node(node, indent: int = 0) -> str:
        prefix = "  " * indent
        dis = f"  (use: {node.id_disambiguated})" if node.id_disambiguated != node.id else ""
        # Always show full id when it differs from the displayed title
        if node.id != node.title:
            return f"{prefix}L{node.line_start} {'#' * node.depth} {node.title}  id: {node.id}{dis}"
        return f"{prefix}L{node.line_start} {'#' * node.depth} {node.title}{dis}"

    # Root listing mode
    if not id:
        if root_depth is None:
            top_depth = min(node.depth for node in nodes)
        else:
            top_depth = int(root_depth)
        top_nodes = [node for node in nodes if node.depth == top_depth]
        out = [f"[schema: {path.name}  sections: {len(top_nodes)}]"]
        for node in top_nodes:
            out.append(_fmt_node(node))
        return "\n".join(out)

    target = _resolve_section_or_error(nodes, id)
    if isinstance(target, dict):
        return _kb_dict_to_error_text(target)

    rel_depth = 1 if max_depth is None else int(max_depth)
    if rel_depth < 0:
        rel_depth = 0

    body_text = _extract_line_range(lines, target.body_start, target.body_end)
    upper_depth = target.depth + rel_depth
    children = []
    if rel_depth > 0:
        for node in nodes:
            if node.line_start <= target.line_start or node.line_start > target.subtree_end:
                continue
            if node.depth <= target.depth or node.depth > upper_depth:
                continue
            children.append(node)

    h = _content_hash(body_text)
    out = [f"[section: {_kb_section_label(target)}  depth: {target.depth}  lines: {target.line_start}-{target.subtree_end}  hash: {h}]"]
    if body_text.strip():
        out.append(body_text.rstrip())
    if children:
        out.append(f"\nChild headings ({len(children)}):")
        for child in children:
            indent = child.depth - target.depth - 1
            out.append(_fmt_node(child, indent))
    return "\n".join(out)


@mcp.tool(name="kb_read", description="Read a section body or subtree from a markdown knowledge file.")
async def kb_read(file: Optional[str] = None, id: str = "", include_children: bool = False) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    lines = text.splitlines()
    nodes = parse_markdown(text)
    target = _resolve_section_or_error(nodes, id, total_lines=len(lines), allow_root=True)
    if isinstance(target, dict):
        return _kb_dict_to_error_text(target)
    if include_children or target.depth == 0:
        start = target.line_start
        end = target.subtree_end
    else:
        start = target.body_start
        end = target.body_end
    section_text = _extract_line_range(lines, start, end)
    h = _content_hash(section_text)
    header = f"[section: {_kb_section_label(target)}  lines: {start}-{end}  hash: {h}]\n"
    return header + section_text


@mcp.tool(name="kb_search_headers", description="Case-insensitive substring search across heading titles.")
async def kb_search_headers(file: Optional[str] = None, query: str = "") -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    q = (query or "").strip()
    if not q:
        return f"[search_headers: {path.name}  query: (empty)]\nNo query provided."
    qf = q.casefold()
    nodes = parse_markdown(text)
    matches = [node for node in nodes if qf in node.title.casefold()]
    out = [f"[search_headers: {path.name}  query: {q}  matches: {len(matches)}]"]
    for node in matches:
        dis = f"  (disambiguated: {node.id_disambiguated})" if node.id_disambiguated != node.id else ""
        out.append(f"  L{node.line_start} {'#' * node.depth} {node.title}  id: {node.id}{dis}")
    if not matches:
        out.append("  (no matches)")
    return "\n".join(out)


@mcp.tool(name="kb_search_content", description="Case-insensitive substring search across section bodies.")
async def kb_search_content(
    file: Optional[str] = None,
    query: str = "",
    max_results: int = 10,
    context_chars: int = 80,
) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    text = _read_text_or_error(path)
    if isinstance(text, dict):
        return _kb_dict_to_error_text(text)
    q = (query or "").strip()
    if not q:
        return f"[search_content: {path.name}  query: (empty)]\nNo query provided."

    lines = text.splitlines()
    nodes = parse_markdown(text)
    qf = q.casefold()
    cap = max(1, int(max_results))
    ctx = max(20, int(context_chars))

    results: list[str] = []
    for node in nodes:
        if len(results) >= cap:
            break
        body_start = max(1, node.body_start)
        body_end = max(body_start - 1, node.body_end)
        for ln in range(body_start, min(body_end, len(lines)) + 1):
            if len(results) >= cap:
                break
            line_text = lines[ln - 1]
            lf = line_text.casefold()
            start = 0
            while len(results) < cap:
                idx = lf.find(qf, start)
                if idx == -1:
                    break
                end = idx + len(q)
                snippet, ms, me = _snippet_with_offsets(line_text, idx, end, ctx)
                dis = f"  (id: {node.id_disambiguated})" if node.id_disambiguated != node.id else f"  (id: {node.id})"
                results.append(f"  L{ln} [{node.title}]{dis}  ...{snippet}...")
                start = idx + max(1, len(qf))
                if start >= len(lf):
                    break

    out = [f"[search_content: {path.name}  query: {q}  results: {len(results)}]"]
    if results:
        out.extend(results)
    else:
        out.append("  (no matches)")
    return "\n".join(out)


@mcp.tool(name="kb_write", description="Append content to a section body, or create a child heading under a markdown section.")
async def kb_write(
    file: Optional[str] = None,
    id: str = "",
    content: str = "",
    mode: str = "append",
    heading_title: Optional[str] = None,
    heading_depth: Optional[int] = None,
    dry_run: bool = False,
    confirm_hash: Optional[str] = None,
) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    target = _resolve_section_or_error(nodes, id, total_lines=len(lines), allow_root=True)
    if isinstance(target, dict):
        return _kb_dict_to_error_text(target)
    _ = confirm_hash  # Legacy no-op; KB mutations are patch-style edits.

    insert_at = max(0, target.body_end)
    if heading_title and mode == "append":
        mode = "heading"
    if mode == "append":
        insert_lines = (content or "").splitlines()
    elif mode == "heading":
        # Insert after the entire subtree, not just the body
        insert_at = max(0, target.subtree_end)
        if not heading_title:
            return _kb_error_text("SectionNotFound", "heading_title is required for mode='heading'", id=id)
        depth = heading_depth if heading_depth is not None else (target.depth + 1)
        if not isinstance(depth, int) or depth < 1 or depth > 6:
            return _kb_error_text("SectionNotFound", "heading_depth must be between 1 and 6", id=id)
        heading_line = f"{'#' * depth} {heading_title}"
        insert_lines = [heading_line]
        if content:
            insert_lines.extend(content.splitlines())
    else:
        return _kb_error_text("SectionNotFound", f"Unsupported mode '{mode}'", id=id)

    new_lines = lines[:insert_at] + insert_lines + lines[insert_at:]
    new_text = "\n".join(new_lines)
    if had_trailing_newline:
        new_text += "\n"

    diff = _unified_diff(old_text, new_text, str(path))
    action = "DRY RUN" if dry_run else "WRITTEN"
    ipc_note = None
    if not dry_run:
        _atomic_write(path, new_text)
        try:
            ipc_note = await _notify_repo_memory_ipc(path, old_text, new_text)
        except Exception as exc:
            ipc_note = f"[kb_ipc: ERROR  {exc}]"
    return _render_kb_result(f"[kb_write: {action}  hash: {_content_hash(new_text)}]", diff, ipc_note)


@mcp.tool(name="kb_update", description="Replace the body of a markdown section, or replace its full subtree.")
async def kb_update(
    file: Optional[str] = None,
    id: str = "",
    content: str = "",
    mode: str = "body",
    dry_run: bool = False,
    confirm_hash: Optional[str] = None,
) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    target = _resolve_section_or_error(nodes, id, total_lines=len(lines), allow_root=True)
    if isinstance(target, dict):
        return _kb_dict_to_error_text(target)
    _ = confirm_hash  # Legacy no-op; KB mutations are patch-style edits.

    normalized_mode = str(mode or "body").strip().lower()
    if normalized_mode == "replace":
        normalized_mode = "body"

    if normalized_mode == "body":
        start, end = target.body_start, target.body_end
    elif normalized_mode == "subtree":
        start, end = target.line_start, target.subtree_end
    else:
        return _kb_error_text(
            "InvalidParameter",
            f"Unsupported mode '{mode}'",
            id=id,
            allowed_modes="body, replace, subtree",
        )

    new_lines = _replace_line_range(lines, start, end, content)
    new_text = "\n".join(new_lines)
    if had_trailing_newline:
        new_text += "\n"
    diff = _unified_diff(old_text, new_text, str(path))

    action = "DRY RUN" if dry_run else "WRITTEN"
    ipc_note = None
    if not dry_run:
        _atomic_write(path, new_text)
        try:
            ipc_note = await _notify_repo_memory_ipc(path, old_text, new_text)
        except Exception as exc:
            ipc_note = f"[kb_ipc: ERROR  {exc}]"
    return _render_kb_result(f"[kb_update: {action}  hash: {_content_hash(content)}]", diff, ipc_note)


@mcp.tool(name="kb_remove", description="Remove the body of a markdown section, or remove its full subtree.")
async def kb_remove(
    file: Optional[str] = None,
    id: str = "",
    mode: str = "subtree",
    dry_run: bool = False,
    confirm_hash: Optional[str] = None,
) -> str:
    resolved = _kb_resolve_file(file)
    if isinstance(resolved, dict):
        return _kb_dict_to_error_text(resolved)
    path = resolved
    old_text = _read_text_or_error(path)
    if isinstance(old_text, dict):
        return _kb_dict_to_error_text(old_text)
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()
    nodes = parse_markdown(old_text)
    target = _resolve_section_or_error(nodes, id, total_lines=len(lines), allow_root=True)
    if isinstance(target, dict):
        return _kb_dict_to_error_text(target)
    _ = confirm_hash  # Legacy no-op; KB mutations are patch-style edits.

    if mode == "subtree":
        start, end = target.line_start, target.subtree_end
    elif mode == "body":
        start, end = target.body_start, target.body_end
    else:
        return _kb_error_text("SectionNotFound", f"Unsupported mode '{mode}'", id=id)

    new_lines = _replace_line_range(lines, start, end, "")
    new_text = "\n".join(new_lines)
    if had_trailing_newline and new_text:
        new_text += "\n"
    diff = _unified_diff(old_text, new_text, str(path))

    action = "DRY RUN" if dry_run else "REMOVED"
    ipc_note = None
    if not dry_run:
        _atomic_write(path, new_text)
        try:
            ipc_note = await _notify_repo_memory_ipc(path, old_text, new_text)
        except Exception as exc:
            ipc_note = f"[kb_ipc: ERROR  {exc}]"
    return _render_kb_result(f"[kb_remove: {action}]", diff, ipc_note)


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
) -> dict:
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
        def _post():
            req = urllib.request.Request(
                _APPSERVER_MESSAGE_URL,
                data=payload_bytes,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
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
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
    if messages and messages[0].get("msg_num"):
        after_msg_num = messages[0]["msg_num"]
    
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
        await mcp.run_streamable_http_async()
        return
    if transport == "sse":
        mount_path = os.environ.get("MCP_MOUNT_PATH") or None
        await mcp.run_sse_async(mount_path=mount_path)
        return
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())

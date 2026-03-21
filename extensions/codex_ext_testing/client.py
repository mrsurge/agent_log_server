"""
Codex App Server Client

Extension handler for Codex app-server using runtime-generated protocol schema
from the installed binary plus the generic extension hook surface.
"""

import asyncio
import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .plan_utils import normalize_plan_steps, render_plan_markdown
from .runtime_protocol import (
    RuntimeProtocol,
    build_request_params,
    build_settings_schema,
    build_thread_runtime_signature_payload,
    configure_runtime_protocol,
    get_runtime_protocol,
)
from .transport import CodexAppServerTransport
from agent_log_server.te2_runtime import te2_mcp_integration_enabled

# Stored references to server callbacks
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[Dict[str, Callable]] = None
_registered_extension_ids: set[str] = set()
_ready_extensions: set[str] = set()
_transport: Optional[CodexAppServerTransport] = None

# Debug buffer (circular)
_raw_buffer: List[Dict[str, Any]] = []
_RAW_BUFFER_MAX = 1000


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_to_raw_buffer(direction: str, conversation_id: str, data: Any) -> None:
    entry = {
        "ts": _utc_ts(),
        "dir": direction,
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": data if isinstance(data, str) else str(data)[:500],
    }
    _raw_buffer.append(entry)
    if len(_raw_buffer) > _RAW_BUFFER_MAX:
        _raw_buffer.pop(0)


def get_raw_buffer(limit: int = 50) -> List[Dict[str, Any]]:
    return _raw_buffer[-limit:]


def _server_module():
    return importlib.import_module("agent_log_server.server")


def _merge_runtime_settings(
    conversation_id: str,
    settings: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta and isinstance(meta.get("settings"), dict):
            merged.update(meta["settings"])
    if isinstance(settings, dict):
        for key, value in settings.items():
            if value is None or value == "":
                continue
            merged[key] = value
    if isinstance(cwd, str) and cwd.strip():
        merged["cwd"] = cwd
    if isinstance(model, str) and model.strip():
        merged["model"] = model
    return _materialize_runtime_settings(merged)


def _thread_runtime_signature(protocol: RuntimeProtocol, settings: Dict[str, Any]) -> str:
    payload = build_thread_runtime_signature_payload(protocol, settings)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _materialize_runtime_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    merged = dict(settings)
    if not isinstance(merged.get("model"), str) or not merged.get("model", "").strip():
        extension_id = merged.get("agent") if isinstance(merged.get("agent"), str) and merged.get("agent").strip() else "codex"
        try:
            ext_loader = importlib.import_module("extensions")
            ext_info = ext_loader.get_extension_info(extension_id)
        except Exception:
            ext_info = None
        manifest = ext_info.get("manifest") if isinstance(ext_info, dict) and isinstance(ext_info.get("manifest"), dict) else {}
        model = manifest.get("model") if isinstance(manifest.get("model"), dict) else {}
        model_name = model.get("name")
        if isinstance(model_name, str) and model_name.strip():
            merged["model"] = model_name.strip()
    if te2_mcp_integration_enabled(merged):
        merged["te2_base_url"] = _server_module()._te2_base_url()
    return merged


def _extract_thread_id_from_result(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        thread = payload.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
    return None


def _mark_transport_ready() -> None:
    for ext_id in _registered_extension_ids:
        _ready_extensions.add(ext_id)


def _ensure_transport() -> CodexAppServerTransport:
    if _transport is None:
        raise RuntimeError("Codex transport not initialized")
    return _transport


async def _ensure_transport_ready() -> CodexAppServerTransport:
    transport = _ensure_transport()
    await transport.ensure_ready()
    _mark_transport_ready()
    return transport


def _sort_session_entries(entries: List[Dict[str, Any]], cwd: Optional[str]) -> List[Dict[str, Any]]:
    if not cwd:
        return entries
    resolved_cwd = os.path.realpath(os.path.expanduser(cwd))

    def relevance(entry: Dict[str, Any]) -> int:
        ctx = entry.get("context") or {}
        session_cwd = ctx.get("cwd") or ""
        if not session_cwd:
            return 9
        resolved_session_cwd = os.path.realpath(session_cwd)
        if resolved_session_cwd == resolved_cwd:
            return 0
        if resolved_session_cwd.startswith(resolved_cwd) or resolved_cwd.startswith(resolved_session_cwd):
            return 1
        return 9

    return sorted(entries, key=relevance)


def _build_plan_state(
    steps: List[Dict[str, str]],
    *,
    explanation: Optional[str] = None,
    source: str,
) -> Dict[str, Any]:
    normalized_steps = normalize_plan_steps(steps)
    return {
        "has_plan": False,
        "has_todo": True,
        "plan_exists": False,
        "plan_content": render_plan_markdown(normalized_steps, explanation),
        "plan_steps": normalized_steps,
        "plan_source": source,
    }


def _latest_transcript_plan(conversation_id: str) -> Optional[Dict[str, Any]]:
    transcript_path = _server_module()._conversation_transcript_path(conversation_id)
    if not isinstance(transcript_path, Path) or not transcript_path.is_file():
        return None
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict) and entry.get("role") == "plan":
            return entry
    return None


def init_codex_app_server_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    global _broadcast_fn, _transcript_fn, _meta_fns
    global _registered_extension_ids, _transport

    _registered_extension_ids = {
        ext_id
        for ext_id in (registered_extension_ids or [])
        if isinstance(ext_id, str) and ext_id
    }
    _ready_extensions.clear()
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns
    configure_runtime_protocol(server_root=server_root, extensions_dir=extensions_dir)
    _transport = CodexAppServerTransport(
        server_root=server_root,
        fws_getter=fws_getter,
        broadcast_fn=broadcast_fn,
        transcript_fn=transcript_fn,
        meta_fns=meta_fns,
        raw_log_fn=_add_to_raw_buffer,
    )
    print("[Codex] Extension initialized (app-server binary handler)")


def init_codex_ext_testing_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    init_codex_app_server_manager(
        extensions_dir,
        server_root,
        fws_getter,
        broadcast_fn,
        transcript_fn,
        meta_fns,
        registered_extension_ids=registered_extension_ids,
    )


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    results = {ext_id: False for ext_id in sorted(_registered_extension_ids)}
    try:
        await asyncio.wait_for(get_runtime_protocol(), timeout=timeout)
        await asyncio.wait_for(_ensure_transport_ready(), timeout=timeout)
        for ext_id in results:
            results[ext_id] = True
    except Exception as exc:
        print(f"[Codex] warm-up failed: {exc}")
    return results


def is_extension_ready(extension_id: str) -> bool:
    return extension_id in _ready_extensions and _transport is not None and _transport.is_ready()


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    if is_extension_ready(extension_id):
        return True
    try:
        await asyncio.wait_for(get_runtime_protocol(), timeout=timeout)
        await asyncio.wait_for(_ensure_transport_ready(), timeout=timeout)
    except Exception as exc:
        print(f"[Codex] wait_extension_ready failed for {extension_id}: {exc}")
        return False
    _ready_extensions.add(extension_id)
    return True


async def get_settings_schema(extension_id: str) -> Dict[str, Any]:
    protocol = await get_runtime_protocol()
    return build_settings_schema(protocol, extension_id)


async def get_request_card_schemas(extension_id: str) -> Dict[str, Any]:
    protocol = await get_runtime_protocol()
    methods = (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "item/tool/call",
        "mcpServer/elicitation/request",
    )
    schemas: Dict[str, Any] = {}
    for method in methods:
        request_schema = protocol.server_request_schema(method)
        response_schema = protocol.server_request_response_schema(method)
        if isinstance(request_schema, dict) and isinstance(response_schema, dict):
            schemas[method.lower()] = {
                "request": request_schema,
                "response": response_schema,
            }
    return schemas


async def list_models() -> List[Dict[str, Any]]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("model/list", params={}, timeout=15.0)
    items = result.get("data", []) if isinstance(result, dict) else []
    models: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model = dict(item)
        if not model.get("id"):
            model_id = model.get("name")
            if isinstance(model_id, str) and model_id:
                model["id"] = model_id
        if not model.get("name"):
            display_name = model.get("displayName")
            if isinstance(display_name, str) and display_name:
                model["name"] = display_name
            elif isinstance(model.get("id"), str):
                model["name"] = model["id"]
        models.append(model)
    return models


async def list_sessions(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("thread/list", params={"limit": 200}, timeout=15.0)
    items_raw = result.get("data", []) if isinstance(result, dict) else []
    sessions: List[Dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        session_id = item.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        entry: Dict[str, Any] = {
            "session_id": session_id,
            "summary": item.get("preview") or "",
            "active": False,
        }
        session_cwd = item.get("cwd")
        if isinstance(session_cwd, str) and session_cwd:
            entry["context"] = {"cwd": session_cwd}
        sessions.append(entry)
    return _sort_session_entries(sessions, cwd)


async def route_event(
    extension_id: str,
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    transport = _ensure_transport()
    return await transport.route_event(
        label=label,
        payload=payload,
        conversation_id=conversation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        request_id=request_id,
    )


async def read_plan(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    if not _meta_fns or "load" not in _meta_fns:
        return {
            "has_plan": False,
            "has_todo": True,
            "plan_exists": False,
            "plan_content": "",
            "plan_steps": [],
            "plan_source": "unavailable",
        }

    meta = _meta_fns["load"](conversation_id)
    active_plan = meta.get("active_plan") if isinstance(meta, dict) and isinstance(meta.get("active_plan"), dict) else None
    if isinstance(active_plan, dict):
        active_steps = normalize_plan_steps(active_plan.get("steps"))
        if active_steps:
            explanation = active_plan.get("explanation")
            return _build_plan_state(
                active_steps,
                explanation=explanation if isinstance(explanation, str) else None,
                source="active_meta",
            )

    transcript_plan = _latest_transcript_plan(conversation_id)
    if isinstance(transcript_plan, dict):
        steps = normalize_plan_steps(transcript_plan.get("steps"))
        if steps:
            explanation = transcript_plan.get("explanation")
            return _build_plan_state(
                steps,
                explanation=explanation if isinstance(explanation, str) else None,
                source="transcript",
            )

    return {
        "has_plan": False,
        "has_todo": True,
        "plan_exists": False,
        "plan_content": "",
        "plan_steps": [],
        "plan_source": "none",
    }


async def resume_session(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}
    thread_id = meta.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"ok": False, "error": f"No thread_id for conversation {conversation_id[:8]}"}

    transport = await _ensure_transport_ready()
    protocol = await get_runtime_protocol()
    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=cwd,
        model=model,
    )
    try:
        resume_params = build_request_params(protocol, "thread/resume", merged_settings, thread_id=thread_id)
        await transport.rpc_request(
            "thread/resume",
            params=resume_params,
            conversation_id=conversation_id,
            timeout=10.0,
        )
        transport.mark_thread_ready(thread_id)
        meta["status"] = "active"
        meta["thread_runtime_signature"] = _thread_runtime_signature(protocol, merged_settings)
        meta["settings"] = merged_settings
        _meta_fns["save"](conversation_id, meta)
        _add_to_raw_buffer("out", conversation_id, f"thread_resumed {thread_id[:8]}")
        return {"ok": True, "session_id": thread_id}
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"resume_failed {exc}")
        return {"ok": False, "error": f"Thread resume failed: {exc}"}


async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    if not conversation_id or not text:
        return {"ok": False, "error": "conversation_id and text required"}

    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}

    transport = await _ensure_transport_ready()
    protocol = await get_runtime_protocol()
    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=settings.get("cwd") if isinstance(settings, dict) else None,
        model=settings.get("model") if isinstance(settings, dict) else None,
    )
    thread_id = meta.get("thread_id")
    base_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_signature = _thread_runtime_signature(protocol, merged_settings)

    try:
        if thread_id:
            if meta.get("thread_runtime_signature") != current_signature or transport.needs_thread_resume(thread_id):
                resume_params = build_request_params(protocol, "thread/resume", merged_settings, thread_id=thread_id)
                await transport.rpc_request(
                    "thread/resume",
                    params=resume_params,
                    conversation_id=conversation_id,
                    timeout=10.0,
                )
                transport.mark_thread_ready(thread_id)
                meta["thread_runtime_signature"] = current_signature
                meta["settings"] = merged_settings
                _meta_fns["save"](conversation_id, meta)

            turn_params = build_request_params(
                protocol,
                "turn/start",
                merged_settings,
                thread_id=thread_id,
                text=text,
            )
            await transport.rpc_request(
                "turn/start",
                params=turn_params,
                conversation_id=conversation_id,
                timeout=15.0,
            )
        else:
            start_params = build_request_params(protocol, "thread/start", merged_settings)
            start_result = await transport.rpc_request(
                "thread/start",
                params=start_params,
                conversation_id=conversation_id,
                timeout=15.0,
            )
            thread_id = _extract_thread_id_from_result(start_result)

            if not thread_id:
                meta = _meta_fns["load"](conversation_id)
                thread_id = meta.get("thread_id") if isinstance(meta, dict) else None

            if not thread_id:
                return {"ok": False, "error": "Failed to start thread - no thread_id received"}

            transport.mark_thread_ready(thread_id)
            meta["thread_id"] = thread_id
            meta["status"] = "active"
            meta["settings"] = merged_settings
            meta["thread_runtime_signature"] = current_signature
            _meta_fns["save"](conversation_id, meta)

            turn_params = build_request_params(
                protocol,
                "turn/start",
                merged_settings,
                thread_id=thread_id,
                text=text,
            )
            await transport.rpc_request(
                "turn/start",
                params=turn_params,
                conversation_id=conversation_id,
                timeout=15.0,
            )

        _add_to_raw_buffer("out", conversation_id, f"turn_start thread={thread_id[:8]} text={text[:120]}")
        return {"ok": True, "thread_id": thread_id, "conversation_id": conversation_id}
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"handle_message_failed {exc}")
        return {"ok": False, "error": str(exc)}


async def resume_session_with_history(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    extension_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}

    existing = meta.get("thread_id")
    if existing and existing != session_id:
        return {"ok": False, "error": f"Conversation already bound to thread {existing[:8]}"}

    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=cwd,
        model=model,
    )
    if isinstance(extension_id, str) and extension_id:
        merged_settings["agent"] = extension_id

    meta["thread_id"] = session_id
    meta["status"] = "active"
    meta["settings"] = merged_settings
    _meta_fns["save"](conversation_id, meta)

    result = await resume_session(
        conversation_id,
        cwd=cwd,
        model=model,
        settings=merged_settings,
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "session_id": session_id, "conversation_id": conversation_id}


async def hydrate_transcript(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    server = _server_module()
    path = server._find_rollout_path(session_id)
    if not path:
        _add_to_raw_buffer("err", conversation_id, f"hydrate_transcript rollout_not_found session={session_id[:8]}")
        return []
    preview = await asyncio.to_thread(server._rollout_preview_entries, path, 200000)
    items = preview.get("items", []) if isinstance(preview, dict) else []
    _add_to_raw_buffer("out", conversation_id, f"hydrate_transcript imported={len(items)} session={session_id[:8]}")
    return items if isinstance(items, list) else []


def resolve_approval(request_id: str, resolution: Any) -> bool:
    transport = _transport
    if transport is None:
        return False
    return transport.resolve_approval(request_id, resolution)


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: Dict[str, Any]) -> bool:
    if not isinstance(descriptor, dict):
        return False
    transport = _transport
    if transport is None:
        return False
    meta = _meta_fns["load"](conversation_id) if _meta_fns and "load" in _meta_fns else {}
    if descriptor.get("conversation_id") and descriptor.get("conversation_id") != conversation_id:
        return False
    pending_thread_id = descriptor.get("thread_id")
    current_thread_id = meta.get("thread_id") if isinstance(meta, dict) else None
    if pending_thread_id and current_thread_id and pending_thread_id != current_thread_id:
        return False
    if pending_thread_id and not current_thread_id:
        return False
    runtime_signature = descriptor.get("runtime_signature")
    current_signature = meta.get("thread_runtime_signature") if isinstance(meta, dict) else None
    if runtime_signature and not current_signature:
        return False
    if runtime_signature and current_signature and runtime_signature != current_signature:
        return False
    runtime_instance_id = descriptor.get("runtime_instance_id")
    current_instance_id = transport.runtime_instance_id()
    if runtime_instance_id and not current_instance_id:
        return False
    if runtime_instance_id and current_instance_id and runtime_instance_id != current_instance_id:
        return False
    return transport.has_pending_approval(request_id)


async def abort_session(conversation_id: str) -> bool:
    if not _meta_fns or "load" not in _meta_fns:
        return False
    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return False
    thread_id = meta.get("thread_id")
    turn_id = meta.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id:
        return False
    if not isinstance(turn_id, str) or not turn_id:
        return False

    try:
        transport = await _ensure_transport_ready()
        protocol = await get_runtime_protocol()
        params = build_request_params(protocol, "turn/interrupt", {}, thread_id=thread_id, turn_id=turn_id)
        await transport.rpc_request(
            "turn/interrupt",
            params=params,
            conversation_id=conversation_id,
            timeout=10.0,
        )
        _add_to_raw_buffer("out", conversation_id, f"turn_interrupt thread={thread_id[:8]} turn={turn_id[:8]}")
        return True
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"interrupt_failed {exc}")
        return False


async def shutdown_client() -> None:
    if _transport is not None:
        await _transport.stop()

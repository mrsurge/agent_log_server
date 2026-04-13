import asyncio
import json
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import socketio
import extensions as ext_loader
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from agent_log_server import conversation_todos as _conv_todos

APPSERVER_NAMESPACE = "/appserver"
CONVERSATIONS_RPC_NAMESPACE = "/rpc/conversations"

_AsyncAnyCallable = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class AppserverSocketioDeps:
    make_appserver_message_in: Callable[[str, str], object]
    api_appserver_message: _AsyncAnyCallable
    api_appserver_shell_exec: _AsyncAnyCallable
    api_appserver_rpc: _AsyncAnyCallable
    api_conversations_rpc: _AsyncAnyCallable
    api_appserver_interrupt: _AsyncAnyCallable
    api_appserver_compact: _AsyncAnyCallable
    api_appserver_conversation: _AsyncAnyCallable
    api_appserver_conversation_meta: _AsyncAnyCallable
    api_appserver_conversation_update: _AsyncAnyCallable
    api_appserver_conversation_draft: _AsyncAnyCallable
    api_appserver_conversations: _AsyncAnyCallable
    api_appserver_conversation_create: _AsyncAnyCallable
    api_appserver_conversation_select: _AsyncAnyCallable
    api_appserver_conversation_delete: _AsyncAnyCallable
    api_appserver_conversation_pins: _AsyncAnyCallable
    api_appserver_set_view: _AsyncAnyCallable
    api_appserver_config: _AsyncAnyCallable
    api_appserver_config_update: _AsyncAnyCallable
    api_appserver_models: _AsyncAnyCallable
    api_appserver_runtime_options: _AsyncAnyCallable
    api_appserver_status: _AsyncAnyCallable
    api_appserver_start: _AsyncAnyCallable
    api_appserver_stop: _AsyncAnyCallable
    api_appserver_initialize: _AsyncAnyCallable
    api_appserver_approval_record: _AsyncAnyCallable
    api_appserver_approval_response: _AsyncAnyCallable
    api_appserver_transcript: _AsyncAnyCallable
    api_appserver_transcript_range: _AsyncAnyCallable
    api_extensions_list: _AsyncAnyCallable
    api_extension_enabled: _AsyncAnyCallable
    api_extension_install: _AsyncAnyCallable
    api_extensions_validate: _AsyncAnyCallable
    api_extensions_install_package: _AsyncAnyCallable
    api_extension_update_package: _AsyncAnyCallable
    api_extension_remove_package: _AsyncAnyCallable
    api_extensions_reload: _AsyncAnyCallable
    api_extension_settings_schema: _AsyncAnyCallable
    api_extension_splash_schema: _AsyncAnyCallable
    api_extension_splash_action: _AsyncAnyCallable
    api_extension_request_cards: _AsyncAnyCallable
    api_extension_plan: _AsyncAnyCallable
    api_fs_list: _AsyncAnyCallable
    api_fs_search: _AsyncAnyCallable
    api_host_ui_get: _AsyncAnyCallable
    api_shutdown: _AsyncAnyCallable
    append_record: _AsyncAnyCallable
    coerce_query_bool: Callable[[Any], bool]
    emit_sidebar_agent_open: _AsyncAnyCallable
    ensure_conversation: Callable[[], Awaitable[str]]
    merge_extension_bind_settings: Callable[..., dict[str, Any]]
    read_records: Callable[..., Any]
    sidebar_recheck_status: _AsyncAnyCallable
    utc_ts: Callable[[], str]
    write_transcript_entries: Callable[[str, list[dict[str, Any]]], Awaitable[Any]]


def register_appserver_socketio_handlers(
    socketio_server: socketio.AsyncServer,
    deps: AppserverSocketioDeps,
) -> None:
    def _sio_error(msg: object) -> dict[str, str]:
        return {"__error": str(msg)}

    def _payload(data: object) -> dict[str, Any]:
        return data if isinstance(data, dict) else {}

    def _unwrap_json_response(result: JSONResponse, fallback: str) -> dict[str, Any]:
        try:
            body = json.loads(bytes(result.body).decode("utf-8"))
        except Exception:
            return _sio_error(fallback)
        if isinstance(body, dict):
            return {"ok": False, **body}
        return _sio_error(fallback)

    def _resolve_conversation_id(data: object) -> str | None:
        payload = _payload(data)
        if payload.get("conversation_id"):
            return str(payload["conversation_id"]).strip() or None
        return None

    async def _open_external_http_url(url: str) -> tuple[bool, str]:
        opener = shutil.which("xdg-open")
        if not opener:
            return False, "xdg-open not found"
        target = str(url or "").strip()
        if not target:
            return False, "Empty URL"
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "Only http/https URLs are supported"
        try:
            proc = await asyncio.create_subprocess_exec(
                opener,
                target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as exc:
            return False, str(exc)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        except asyncio.TimeoutError:
            return True, ""
        if proc.returncode == 0:
            return True, ""
        message = stderr.decode("utf-8", errors="replace").strip() if isinstance(stderr, (bytes, bytearray)) else ""
        return False, message or f"xdg-open exited with {proc.returncode}"

    async def _appserver_connect(sid: str, environ: dict[str, Any]) -> None:
        return None

    async def _appserver_disconnect(sid: str) -> None:
        return None

    async def _sio_send_message(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            return await deps.api_appserver_message(
                deps.make_appserver_message_in(
                    str(payload.get("conversation_id") or ""),
                    str(payload.get("text") or ""),
                )
            )
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_shell_exec(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_shell_exec(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_rpc(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_rpc(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversations_rpc(sid: str, data: object) -> Any:
        try:
            return await deps.api_conversations_rpc(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_interrupt(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_interrupt(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_compact(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_compact(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_get(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            cid = payload.get("conversation_id")
            if cid:
                return await deps.api_appserver_conversation_meta(cid)
            return await deps.api_appserver_conversation()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_meta(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_meta(_payload(data).get("conversation_id", ""))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_update(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_update(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_draft(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_draft(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversations_list(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversations()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_create(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_create(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_select(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_select(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_delete(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_delete(_payload(data).get("conversation_id", ""))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_pins_update(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_conversation_pins(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_set_view(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_set_view(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_config(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_config()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_update_config(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_config_update(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_models(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_models()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_runtime_options(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            return await deps.api_appserver_runtime_options(
                conversation_id=payload.get("conversation_id"),
                agent=payload.get("agent"),
            )
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extensions(sid: str, data: object) -> Any:
        try:
            return await deps.api_extensions_list()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_set_enabled(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            extension_id = str(payload.get("extension_id") or "").strip()
            return await deps.api_extension_enabled(extension_id, {"enabled": payload.get("enabled")})
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_install(sid: str, data: object) -> Any:
        try:
            extension_id = str(_payload(data).get("extension_id") or "").strip()
            return await deps.api_extension_install(extension_id)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_validate_package(sid: str, data: object) -> Any:
        try:
            return await deps.api_extensions_validate(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_install_package(sid: str, data: object) -> Any:
        try:
            return await deps.api_extensions_install_package(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_update_package(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            extension_id = str(payload.get("extension_id") or "").strip()
            return await deps.api_extension_update_package(extension_id, payload)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_remove_package(sid: str, data: object) -> Any:
        try:
            extension_id = str(_payload(data).get("extension_id") or "").strip()
            return await deps.api_extension_remove_package(extension_id)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extensions_reload(sid: str, data: object) -> Any:
        try:
            return await deps.api_extensions_reload(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_settings_schema(sid: str, data: object) -> Any:
        try:
            result = await deps.api_extension_settings_schema(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _sio_error("Extension not found")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_splash_schema(sid: str, data: object) -> Any:
        try:
            result = await deps.api_extension_splash_schema(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Extension splash schema unavailable")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_run_extension_splash_action(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            result = await deps.api_extension_splash_action(payload.get("extension_id", ""), payload)
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Extension splash action failed")
            return result
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_request_cards(sid: str, data: object) -> Any:
        try:
            result = await deps.api_extension_request_cards(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Extension request-card config unavailable")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_ui_features(sid: str, data: object) -> Any:
        try:
            extension_id = str(_payload(data).get("extension_id") or "").strip()
            if not extension_id:
                return _sio_error("Missing required field: extension_id")
            info = ext_loader.get_extension_info(extension_id)
            if not isinstance(info, dict):
                return _sio_error(f"Extension not found: {extension_id}")
            return {
                "ok": True,
                "extension_id": extension_id,
                "ui_features": ext_loader.get_extension_ui_features(extension_id),
            }
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_plan(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            result = await deps.api_extension_plan(
                extension_id=payload.get("extension_id", ""),
                conversation_id=payload.get("conversation_id"),
            )
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Failed to read extension plan")
            return result
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_sessions(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            ext_id = payload.get("extension_id", "")
            if not ext_id or not ext_loader.has_extension(ext_id):
                return _sio_error(f"Unknown extension: {ext_id}")
            return await ext_loader.list_sessions(ext_id, cwd=payload.get("cwd"))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_session_resume(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            ext_id = payload.get("extension_id", "")
            if not ext_id or not ext_loader.has_extension(ext_id):
                return _sio_error(f"Unknown extension: {ext_id}")
            session_id = payload.get("session_id")
            if not session_id:
                return _sio_error("Missing session_id")
            conversation_id = payload.get("conversation_id") or await deps.ensure_conversation()
            bind_settings = deps.merge_extension_bind_settings(
                conversation_id,
                cwd=payload.get("cwd"),
                model=payload.get("model"),
                settings=payload.get("settings"),
            )
            result = await ext_loader.resume_session_with_history(
                ext_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=payload.get("cwd"),
                model=payload.get("model"),
                settings=bind_settings,
            )
            if not result.get("ok"):
                return result
            items = await ext_loader.hydrate_transcript(
                ext_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=payload.get("cwd"),
                model=payload.get("model"),
                settings=bind_settings,
            )
            if items:
                await deps.write_transcript_entries(conversation_id, items)
            result["history_count"] = len(items)
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_status(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_status()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_host_ui(sid: str, data: object) -> Any:
        try:
            return await deps.api_host_ui_get()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_sidebar_recheck(sid: str, data: object) -> Any:
        try:
            return await deps.sidebar_recheck_status()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_start(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_start()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_stop(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_stop()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_initialize(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_initialize()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_approval_record(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_approval_record(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_approval_response(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_approval_response(_payload(data))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_fs_list(sid: str, data: object) -> Any:
        try:
            return await deps.api_fs_list(path=_payload(data).get("path"))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_fs_search(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            try:
                limit = int(payload.get("limit", 200) or 200)
            except Exception:
                return _sio_error("limit must be an integer")
            return await deps.api_fs_search(
                query=str(payload.get("query") or ""),
                root=payload.get("root"),
                limit=min(max(limit, 1), 200),
            )
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_transcript(sid: str, data: object) -> Any:
        try:
            return await deps.api_appserver_transcript(conversation_id=_payload(data).get("conversation_id"))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_transcript_range(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            offset = payload.get("offset", 0)
            limit = payload.get("limit", 120)
            include_internal = deps.coerce_query_bool(payload.get("include_internal", False))
            return await deps.api_appserver_transcript_range(
                conversation_id=payload.get("conversation_id"),
                offset=offset,
                limit=min(limit, 500),
                include_internal=include_internal,
            )
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_models(sid: str, data: object) -> Any:
        try:
            ext_id = _payload(data).get("extension_id", "")
            if not ext_id or not ext_loader.has_extension(ext_id):
                return _sio_error(f"Unknown extension: {ext_id}")
            return await ext_loader.list_models(ext_id)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_te2_agent_open(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            print(f"[Sidebar] te2_agent_open received: {payload}")
            await deps.emit_sidebar_agent_open(payload)
            return {"ok": True}
        except Exception as exc:
            print(f"[Sidebar] te2_agent_open error: {exc}")
            return _sio_error(exc)

    async def _sio_open_external_url(sid: str, data: object) -> Any:
        try:
            url = _payload(data).get("url")
            ok, error = await _open_external_http_url(str(url or ""))
            if not ok:
                return _sio_error(error or "Failed to open URL")
            return {"ok": True, "url": str(url or "").strip()}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_log_messages(sid: str, data: object) -> Any:
        try:
            limit = _payload(data).get("limit")
            if limit is not None:
                try:
                    limit = int(limit)
                except Exception:
                    return _sio_error("limit must be an integer")
            return deps.read_records(limit=limit)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_post_log_message(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            who = str(payload.get("who") or "").strip()
            text = str(payload.get("message") or "").strip()
            if not who or not text:
                return _sio_error("Both 'who' and 'message' are required")
            record = {"ts": deps.utc_ts(), "who": who, "message": text}
            await deps.append_record(record)
            return record
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_shutdown_request(sid: str, data: object) -> Any:
        try:
            return await deps.api_shutdown()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_list(sid: str, data: object) -> Any:
        try:
            cid = _resolve_conversation_id(data)
            if not cid:
                return _sio_error("no conversation")
            return {"ok": True, "todos": _conv_todos.list_todos(cid, status=_payload(data).get("status"))}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_add(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            title = str(payload.get("title") or "").strip()
            if not title:
                return _sio_error("title required")
            todo = _conv_todos.add_todo(
                cid,
                title,
                description=payload.get("description", ""),
                status=payload.get("status", "pending"),
            )
            return {"ok": True, "todo": todo}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_update(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            todo_id = int(payload["id"])
            result = _conv_todos.update_todo(
                cid,
                todo_id,
                title=payload.get("title"),
                description=payload.get("description"),
                status=payload.get("status"),
            )
            if result is None:
                return _sio_error("todo not found")
            return {"ok": True, "todo": result}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_remove(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            removed = _conv_todos.remove_todo(cid, int(payload["id"]))
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_toggle(sid: str, data: object) -> Any:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            result = _conv_todos.toggle_todo(cid, int(payload["id"]))
            if result is None:
                return _sio_error("todo not found")
            return {"ok": True, "todo": result}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_ready(sid: str, data: object) -> Any:
        try:
            cid = _resolve_conversation_id(data)
            if not cid:
                return _sio_error("no conversation")
            return {"ok": True, "todos": _conv_todos.list_ready(cid)}
        except Exception as exc:
            return _sio_error(exc)

    registrations: list[tuple[str, Callable[..., Awaitable[Any]]]] = [
        ("connect", _appserver_connect),
        ("disconnect", _appserver_disconnect),
        ("send_message", _sio_send_message),
        ("shell_exec", _sio_shell_exec),
        ("rpc", _sio_rpc),
        ("interrupt", _sio_interrupt),
        ("compact", _sio_compact),
        ("conversation_get", _sio_conversation_get),
        ("conversation_meta", _sio_conversation_meta),
        ("conversation_update", _sio_conversation_update),
        ("conversation_draft", _sio_conversation_draft),
        ("conversations_list", _sio_conversations_list),
        ("conversation_create", _sio_conversation_create),
        ("conversation_select", _sio_conversation_select),
        ("conversation_delete", _sio_conversation_delete),
        ("conversation_pins_update", _sio_conversation_pins_update),
        ("set_view", _sio_set_view),
        ("get_config", _sio_get_config),
        ("update_config", _sio_update_config),
        ("get_models", _sio_get_models),
        ("get_runtime_options", _sio_get_runtime_options),
        ("get_extensions", _sio_get_extensions),
        ("extension_set_enabled", _sio_extension_set_enabled),
        ("extension_install", _sio_extension_install),
        ("extension_validate_package", _sio_extension_validate_package),
        ("extension_install_package", _sio_extension_install_package),
        ("extension_update_package", _sio_extension_update_package),
        ("extension_remove_package", _sio_extension_remove_package),
        ("extensions_reload", _sio_extensions_reload),
        ("get_extension_settings_schema", _sio_get_extension_settings_schema),
        ("get_extension_splash_schema", _sio_get_extension_splash_schema),
        ("run_extension_splash_action", _sio_run_extension_splash_action),
        ("get_extension_request_cards", _sio_get_extension_request_cards),
        ("get_extension_ui_features", _sio_get_extension_ui_features),
        ("get_extension_plan", _sio_get_extension_plan),
        ("get_sessions", _sio_get_sessions),
        ("session_resume", _sio_session_resume),
        ("get_status", _sio_get_status),
        ("get_host_ui", _sio_get_host_ui),
        ("sidebar_recheck", _sio_sidebar_recheck),
        ("app_start", _sio_app_start),
        ("app_stop", _sio_app_stop),
        ("app_initialize", _sio_app_initialize),
        ("approval_record", _sio_approval_record),
        ("approval_response", _sio_approval_response),
        ("fs_list", _sio_fs_list),
        ("fs_search", _sio_fs_search),
        ("get_transcript", _sio_get_transcript),
        ("get_transcript_range", _sio_get_transcript_range),
        ("get_extension_models", _sio_get_extension_models),
        ("te2_agent_open", _sio_te2_agent_open),
        ("open_external_url", _sio_open_external_url),
        ("get_log_messages", _sio_get_log_messages),
        ("post_log_message", _sio_post_log_message),
        ("shutdown_request", _sio_shutdown_request),
        ("todo_list", _sio_todo_list),
        ("todo_add", _sio_todo_add),
        ("todo_update", _sio_todo_update),
        ("todo_remove", _sio_todo_remove),
        ("todo_toggle", _sio_todo_toggle),
        ("todo_ready", _sio_todo_ready),
    ]
    for event, handler in registrations:
        socketio_server.on(event, handler, namespace=APPSERVER_NAMESPACE)

    conversations_rpc_registrations: list[tuple[str, Callable[..., Awaitable[Any]]]] = [
        ("connect", _appserver_connect),
        ("disconnect", _appserver_disconnect),
        ("rpc", _sio_conversations_rpc),
    ]
    for event, handler in conversations_rpc_registrations:
        socketio_server.on(event, handler, namespace=CONVERSATIONS_RPC_NAMESPACE)

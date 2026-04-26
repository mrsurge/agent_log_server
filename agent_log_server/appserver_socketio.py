import asyncio
import json
import shutil
import urllib.parse
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Literal, cast

import socketio
import extensions as ext_loader
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from agent_log_server import conversation_todos as _conv_todos
from agent_log_server.conversations_rpc_contract import CONVERSATIONS_RPC_NAMESPACE
from agent_log_server.settings_ui_rpc_contract import (
    SETTINGS_CONFIG_GET_METHOD,
    SETTINGS_CONFIG_UPDATE_METHOD,
    SETTINGS_CONFIG_UPDATED_NOTIFICATION,
    SETTINGS_EXTENSION_PLAN_GET_METHOD,
    SETTINGS_EXTENSION_MODELS_LIST_METHOD,
    SETTINGS_EXTENSION_REQUEST_CARDS_GET_METHOD,
    SETTINGS_EXTENSION_RUNTIME_OPTIONS_GET_METHOD,
    SETTINGS_EXTENSION_SESSION_BIND_METHOD,
    SETTINGS_EXTENSION_SESSIONS_LIST_METHOD,
    SETTINGS_EXTENSION_SETTINGS_SCHEMA_GET_METHOD,
    SETTINGS_EXTENSION_UI_FEATURES_GET_METHOD,
    SETTINGS_EXTENSIONS_LIST_METHOD,
    SETTINGS_EXTENSIONS_RELOAD_METHOD,
    SETTINGS_EXTENSIONS_UPDATED_NOTIFICATION,
    SETTINGS_RPC_NAMESPACE,
    SETTINGS_STATUS_GET_METHOD,
    UI_FILE_OPEN_METHOD,
    UI_FILESYSTEM_LIST_METHOD,
    UI_FILESYSTEM_SEARCH_METHOD,
    UI_HOST_UI_GET_METHOD,
    UI_HOST_UI_RECHECK_METHOD,
    UI_HOST_UI_UPDATED_NOTIFICATION,
    UI_RPC_NAMESPACE,
    UI_URL_OPEN_METHOD,
    UI_VIEW_CHANGED_NOTIFICATION,
    UI_VIEW_GET_METHOD,
    UI_VIEW_SET_METHOD,
    build_jsonrpc_error_response,
    build_jsonrpc_error_response_from_http_exception,
    build_jsonrpc_notification,
    build_jsonrpc_success_response,
    parse_settings_rpc_request,
    parse_ui_rpc_request,
    SettingsUiRpcProtocolError,
)
from agent_log_server.typing_helpers import (
    AsyncObjectCallable,
    ObjectEntriesWriter,
    ObjectMap,
    coerce_object_list,
    coerce_object_map,
)

APPSERVER_NAMESPACE = "/appserver"
@dataclass(frozen=True)
class AppserverSocketioDeps:
    make_appserver_message_in: Callable[[str, str], object]
    api_appserver_message: AsyncObjectCallable
    api_appserver_shell_exec: AsyncObjectCallable
    api_appserver_rpc: AsyncObjectCallable
    api_conversations_rpc: AsyncObjectCallable
    api_appserver_interrupt: AsyncObjectCallable
    api_appserver_compact: AsyncObjectCallable
    api_appserver_conversation: AsyncObjectCallable
    api_appserver_conversation_meta: AsyncObjectCallable
    api_appserver_conversation_update: AsyncObjectCallable
    api_appserver_conversation_draft: AsyncObjectCallable
    api_appserver_conversations: AsyncObjectCallable
    api_appserver_conversation_create: AsyncObjectCallable
    api_appserver_conversation_select: AsyncObjectCallable
    api_appserver_conversation_delete: AsyncObjectCallable
    api_appserver_conversation_pins: AsyncObjectCallable
    api_appserver_set_view: AsyncObjectCallable
    api_appserver_config: AsyncObjectCallable
    api_appserver_config_update: AsyncObjectCallable
    api_appserver_models: AsyncObjectCallable
    api_appserver_runtime_options: AsyncObjectCallable
    api_appserver_status: AsyncObjectCallable
    api_appserver_start: AsyncObjectCallable
    api_appserver_stop: AsyncObjectCallable
    api_appserver_initialize: AsyncObjectCallable
    api_appserver_approval_record: AsyncObjectCallable
    api_appserver_approval_response: AsyncObjectCallable
    api_appserver_transcript: AsyncObjectCallable
    api_appserver_transcript_range: AsyncObjectCallable
    api_extensions_list: AsyncObjectCallable
    api_extension_enabled: AsyncObjectCallable
    api_extension_install: AsyncObjectCallable
    api_extensions_validate: AsyncObjectCallable
    api_extensions_install_package: AsyncObjectCallable
    api_extension_update_package: AsyncObjectCallable
    api_extension_remove_package: AsyncObjectCallable
    api_extensions_reload: AsyncObjectCallable
    api_extension_settings_schema: AsyncObjectCallable
    api_extension_splash_schema: AsyncObjectCallable
    api_extension_splash_action: AsyncObjectCallable
    api_extension_request_cards: AsyncObjectCallable
    api_extension_plan: AsyncObjectCallable
    api_fs_list: AsyncObjectCallable
    api_fs_search: AsyncObjectCallable
    api_host_ui_get: AsyncObjectCallable
    api_shutdown: AsyncObjectCallable
    append_record: AsyncObjectCallable
    coerce_query_bool: Callable[[object], bool]
    emit_sidebar_agent_open: AsyncObjectCallable
    ensure_conversation: Callable[[], Awaitable[str]]
    merge_extension_bind_settings: Callable[..., ObjectMap]
    read_records: Callable[..., object]
    sidebar_recheck_status: AsyncObjectCallable
    utc_ts: Callable[[], str]
    write_transcript_entries: ObjectEntriesWriter


def register_appserver_socketio_handlers(
    socketio_server: socketio.AsyncServer,
    deps: AppserverSocketioDeps,
) -> None:
    def _sio_error(msg: object) -> ObjectMap:
        return {"__error": str(msg)}

    def _payload(data: object) -> ObjectMap:
        return coerce_object_map(data)

    def _unwrap_json_response(result: JSONResponse, fallback: str) -> ObjectMap:
        try:
            body = cast(object, json.loads(bytes(result.body).decode("utf-8")))
        except Exception:
            return _sio_error(fallback)
        if isinstance(body, dict):
            return {"ok": False, **coerce_object_map(body)}
        return _sio_error(fallback)

    def _resolve_conversation_id(data: object) -> str | None:
        payload = _payload(data)
        if payload.get("conversation_id"):
            return str(payload["conversation_id"]).strip() or None
        return None

    def _http_exception_from_json_response(result: JSONResponse, fallback: str) -> HTTPException:
        try:
            body = cast(object, json.loads(bytes(result.body).decode("utf-8")))
        except Exception:
            return HTTPException(status_code=result.status_code or 500, detail=fallback)
        if isinstance(body, dict):
            body_map = coerce_object_map(body)
            detail = body_map.get("error") or body_map.get("detail") or body_map.get("message")
            if detail:
                return HTTPException(status_code=result.status_code or 500, detail=detail)
        return HTTPException(status_code=result.status_code or 500, detail=fallback)

    def _require_extension_id(payload: ObjectMap) -> str:
        extension_id = str(payload.get("extension_id") or "").strip()
        if not extension_id:
            raise HTTPException(status_code=400, detail="Missing required field: extension_id")
        if not ext_loader.has_extension(extension_id):
            raise HTTPException(status_code=404, detail=f"Extension not found: {extension_id}")
        return extension_id

    def _optional_str(payload: ObjectMap, key: str) -> str | None:
        value = payload.get(key)
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    async def _emit_rpc_notification(
        namespace: str,
        method: str,
        params: ObjectMap | None = None,
    ) -> None:
        try:
            await socketio_server.emit(
                "rpc.notify",
                build_jsonrpc_notification(method, params),
                namespace=namespace,
            )
        except Exception:
            pass

    async def _resume_extension_session(payload: ObjectMap) -> ObjectMap:
        ext_id = _require_extension_id(payload)
        session_id = _optional_str(payload, "session_id")
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id")
        conversation_id = _optional_str(payload, "conversation_id") or await deps.ensure_conversation()
        if not conversation_id:
            raise HTTPException(status_code=500, detail="Failed to create conversation")
        cwd = _optional_str(payload, "cwd")
        model = _optional_str(payload, "model")
        settings = coerce_object_map(payload.get("settings")) or None
        bind_settings = deps.merge_extension_bind_settings(
            conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
        result = coerce_object_map(
            await ext_loader.resume_session_with_history(
                ext_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=cwd,
                model=model,
                settings=bind_settings,
            )
        )
        if not result.get("ok"):
            return result
        items = coerce_object_list(
            await ext_loader.hydrate_transcript(
                ext_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=cwd,
                model=model,
                settings=bind_settings,
            )
        )
        if items:
            await deps.write_transcript_entries(conversation_id, items)
        result["history_count"] = len(items)
        return result

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

    async def _appserver_connect(sid: str, environ: ObjectMap) -> object:
        return None

    async def _appserver_disconnect(sid: str) -> object:
        return None

    async def _conversations_rpc_connect(sid: str, environ: ObjectMap) -> object:
        return None

    async def _conversations_rpc_disconnect(sid: str) -> object:
        return None

    async def _settings_rpc_connect(sid: str, environ: ObjectMap) -> object:
        return None

    async def _settings_rpc_disconnect(sid: str) -> object:
        return None

    async def _ui_rpc_connect(sid: str, environ: ObjectMap) -> object:
        return None

    async def _ui_rpc_disconnect(sid: str) -> object:
        return None

    async def _sio_send_message(sid: str, data: object) -> object:
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

    async def _sio_shell_exec(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_shell_exec(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_rpc(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_rpc(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversations_rpc(sid: str, data: object) -> object:
        try:
            return await deps.api_conversations_rpc(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_settings_rpc(sid: str, data: object) -> object:
        request_id = None
        try:
            request = parse_settings_rpc_request(data)
            request_id = request.request_id
            result: ObjectMap
            if request.method == SETTINGS_CONFIG_GET_METHOD:
                result = coerce_object_map(await deps.api_appserver_config())
            elif request.method == SETTINGS_CONFIG_UPDATE_METHOD:
                result = coerce_object_map(await deps.api_appserver_config_update(request.params))
                await _emit_rpc_notification(
                    SETTINGS_RPC_NAMESPACE,
                    SETTINGS_CONFIG_UPDATED_NOTIFICATION,
                    {"config": result},
                )
            elif request.method == SETTINGS_EXTENSIONS_LIST_METHOD:
                result = coerce_object_map(await deps.api_extensions_list())
            elif request.method == SETTINGS_EXTENSIONS_RELOAD_METHOD:
                result = coerce_object_map(await deps.api_extensions_reload(request.params))
                await _emit_rpc_notification(
                    SETTINGS_RPC_NAMESPACE,
                    SETTINGS_EXTENSIONS_UPDATED_NOTIFICATION,
                    result or {"ok": True},
                )
            elif request.method == SETTINGS_EXTENSION_SETTINGS_SCHEMA_GET_METHOD:
                extension_id = _require_extension_id(request.params)
                schema = await deps.api_extension_settings_schema(extension_id)
                if isinstance(schema, JSONResponse):
                    raise _http_exception_from_json_response(schema, "Extension settings schema unavailable")
                result = coerce_object_map(schema)
            elif request.method == SETTINGS_EXTENSION_RUNTIME_OPTIONS_GET_METHOD:
                result = coerce_object_map(
                    await deps.api_appserver_runtime_options(
                        conversation_id=request.params.get("conversation_id"),
                        agent=request.params.get("agent"),
                    )
                )
            elif request.method == SETTINGS_EXTENSION_REQUEST_CARDS_GET_METHOD:
                extension_id = _require_extension_id(request.params)
                request_cards = await deps.api_extension_request_cards(extension_id)
                if isinstance(request_cards, JSONResponse):
                    raise _http_exception_from_json_response(request_cards, "Extension request-card config unavailable")
                result = coerce_object_map(request_cards)
            elif request.method == SETTINGS_EXTENSION_UI_FEATURES_GET_METHOD:
                extension_id = _require_extension_id(request.params)
                info = ext_loader.get_extension_info(extension_id)
                if not isinstance(info, dict):
                    raise HTTPException(status_code=404, detail=f"Extension not found: {extension_id}")
                result = coerce_object_map(
                    {
                        "ok": True,
                        "extension_id": extension_id,
                        "ui_features": ext_loader.get_extension_ui_features(extension_id),
                    }
                )
            elif request.method == SETTINGS_EXTENSION_PLAN_GET_METHOD:
                extension_id = _require_extension_id(request.params)
                plan_result = await deps.api_extension_plan(
                    extension_id=extension_id,
                    conversation_id=_optional_str(request.params, "conversation_id"),
                )
                if isinstance(plan_result, JSONResponse):
                    raise _http_exception_from_json_response(plan_result, "Failed to read extension plan")
                result = coerce_object_map(plan_result)
            elif request.method == SETTINGS_EXTENSION_MODELS_LIST_METHOD:
                extension_id = _require_extension_id(request.params)
                result = coerce_object_map(
                    {"models": coerce_object_list(await ext_loader.list_models(extension_id))}
                )
            elif request.method == SETTINGS_EXTENSION_SESSIONS_LIST_METHOD:
                extension_id = _require_extension_id(request.params)
                result = coerce_object_map(
                    {
                        "sessions": coerce_object_list(
                            await ext_loader.list_sessions(
                                extension_id,
                                cwd=_optional_str(request.params, "cwd"),
                            )
                        )
                    }
                )
            elif request.method == SETTINGS_EXTENSION_SESSION_BIND_METHOD:
                result = await _resume_extension_session(request.params)
            elif request.method == SETTINGS_STATUS_GET_METHOD:
                result = coerce_object_map(await deps.api_appserver_status())
            else:
                raise HTTPException(status_code=404, detail=f"Unknown method: {request.method}")
            return build_jsonrpc_success_response(request.request_id, result).to_json()
        except SettingsUiRpcProtocolError as exc:
            return build_jsonrpc_error_response(
                exc.request_id,
                code=exc.code,
                message=exc.message,
                data=exc.data or None,
            ).to_json()
        except HTTPException as exc:
            return build_jsonrpc_error_response_from_http_exception(request_id, exc).to_json()
        except Exception as exc:
            return build_jsonrpc_error_response(
                request_id,
                code=500,
                message=str(exc or "RPC request failed"),
            ).to_json()

    async def _sio_ui_rpc(sid: str, data: object) -> object:
        request_id = None
        try:
            request = parse_ui_rpc_request(data)
            request_id = request.request_id
            result: ObjectMap
            if request.method == UI_VIEW_GET_METHOD:
                config = coerce_object_map(await deps.api_appserver_config())
                result = coerce_object_map(
                    {
                        "active_view": _optional_str(config, "active_view"),
                        "conversation_id": _optional_str(config, "conversation_id"),
                    }
                )
            elif request.method == UI_VIEW_SET_METHOD:
                updated = coerce_object_map(await deps.api_appserver_set_view(request.params))
                result = coerce_object_map(
                    {
                        "active_view": _optional_str(updated, "active_view"),
                        "conversation_id": _optional_str(updated, "conversation_id"),
                    }
                )
                await _emit_rpc_notification(
                    UI_RPC_NAMESPACE,
                    UI_VIEW_CHANGED_NOTIFICATION,
                    result,
                )
            elif request.method == UI_HOST_UI_GET_METHOD:
                result = coerce_object_map(await deps.api_host_ui_get())
            elif request.method == UI_HOST_UI_RECHECK_METHOD:
                recheck = coerce_object_map(await deps.sidebar_recheck_status())
                host_ui = coerce_object_map(await deps.api_host_ui_get())
                result = coerce_object_map({"recheck": recheck, **host_ui})
                await _emit_rpc_notification(
                    UI_RPC_NAMESPACE,
                    UI_HOST_UI_UPDATED_NOTIFICATION,
                    host_ui,
                )
            elif request.method == UI_FILESYSTEM_LIST_METHOD:
                result = coerce_object_map(await deps.api_fs_list(path=request.params.get("path")))
            elif request.method == UI_FILESYSTEM_SEARCH_METHOD:
                limit_value = request.params.get("limit")
                if limit_value in (None, ""):
                    limit = 200
                elif isinstance(limit_value, (int, float, str)):
                    try:
                        limit = int(limit_value)
                    except Exception as exc:
                        raise HTTPException(status_code=400, detail="limit must be an integer") from exc
                else:
                    raise HTTPException(status_code=400, detail="limit must be an integer")
                result = coerce_object_map(
                    await deps.api_fs_search(
                        query=str(request.params.get("query") or ""),
                        root=_optional_str(request.params, "root"),
                        limit=min(max(limit, 1), 200),
                    )
                )
            elif request.method == UI_FILE_OPEN_METHOD:
                await deps.emit_sidebar_agent_open(request.params)
                result = coerce_object_map(
                    {
                        "ok": True,
                        "path": _optional_str(request.params, "path"),
                        "line": request.params.get("line"),
                        "column": request.params.get("column"),
                    }
                )
            elif request.method == UI_URL_OPEN_METHOD:
                url = str(request.params.get("url") or "").strip()
                ok, error = await _open_external_http_url(url)
                if not ok:
                    raise HTTPException(status_code=400, detail=error or "Failed to open URL")
                result = coerce_object_map({"ok": True, "url": url})
            else:
                raise HTTPException(status_code=404, detail=f"Unknown method: {request.method}")
            return build_jsonrpc_success_response(request.request_id, result).to_json()
        except SettingsUiRpcProtocolError as exc:
            return build_jsonrpc_error_response(
                exc.request_id,
                code=exc.code,
                message=exc.message,
                data=exc.data or None,
            ).to_json()
        except HTTPException as exc:
            return build_jsonrpc_error_response_from_http_exception(request_id, exc).to_json()
        except Exception as exc:
            return build_jsonrpc_error_response(
                request_id,
                code=500,
                message=str(exc or "RPC request failed"),
            ).to_json()

    async def _sio_interrupt(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_interrupt(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_compact(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_compact(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_get(sid: str, data: object) -> object:
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

    async def _sio_conversation_meta(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_meta(_payload(data).get("conversation_id", ""))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_update(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_update(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_draft(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_draft(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversations_list(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversations()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_create(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_create(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_select(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_select(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_delete(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_delete(_payload(data).get("conversation_id", ""))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_conversation_pins_update(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_conversation_pins(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_set_view(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_set_view(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_config(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_config()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_update_config(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_config_update(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_models(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_models()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_runtime_options(sid: str, data: object) -> object:
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

    async def _sio_get_extensions(sid: str, data: object) -> object:
        try:
            return await deps.api_extensions_list()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_set_enabled(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            extension_id = str(payload.get("extension_id") or "").strip()
            return await deps.api_extension_enabled(extension_id, {"enabled": payload.get("enabled")})
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_install(sid: str, data: object) -> object:
        try:
            extension_id = str(_payload(data).get("extension_id") or "").strip()
            return await deps.api_extension_install(extension_id)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_validate_package(sid: str, data: object) -> object:
        try:
            return await deps.api_extensions_validate(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_install_package(sid: str, data: object) -> object:
        try:
            return await deps.api_extensions_install_package(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_update_package(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            extension_id = str(payload.get("extension_id") or "").strip()
            return await deps.api_extension_update_package(extension_id, payload)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extension_remove_package(sid: str, data: object) -> object:
        try:
            extension_id = str(_payload(data).get("extension_id") or "").strip()
            return await deps.api_extension_remove_package(extension_id)
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_extensions_reload(sid: str, data: object) -> object:
        try:
            return await deps.api_extensions_reload(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_settings_schema(sid: str, data: object) -> object:
        try:
            result = await deps.api_extension_settings_schema(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _sio_error("Extension not found")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_splash_schema(sid: str, data: object) -> object:
        try:
            result = await deps.api_extension_splash_schema(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Extension splash schema unavailable")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_run_extension_splash_action(sid: str, data: object) -> object:
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

    async def _sio_get_extension_request_cards(sid: str, data: object) -> object:
        try:
            result = await deps.api_extension_request_cards(_payload(data).get("extension_id", ""))
            if isinstance(result, JSONResponse):
                return _unwrap_json_response(result, "Extension request-card config unavailable")
            return result
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_ui_features(sid: str, data: object) -> object:
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

    async def _sio_get_extension_plan(sid: str, data: object) -> object:
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

    async def _sio_get_sessions(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            ext_id_value = payload.get("extension_id")
            ext_id = ext_id_value.strip() if isinstance(ext_id_value, str) and ext_id_value.strip() else ""
            if not ext_id or not ext_loader.has_extension(ext_id):
                return _sio_error(f"Unknown extension: {ext_id}")
            cwd_value = payload.get("cwd")
            cwd = cwd_value if isinstance(cwd_value, str) else None
            return cast(object, await ext_loader.list_sessions(ext_id, cwd=cwd))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_session_resume(sid: str, data: object) -> object:
        try:
            return await _resume_extension_session(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_status(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_status()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_host_ui(sid: str, data: object) -> object:
        try:
            return await deps.api_host_ui_get()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_sidebar_recheck(sid: str, data: object) -> object:
        try:
            return await deps.sidebar_recheck_status()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_start(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_start()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_stop(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_stop()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_app_initialize(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_initialize()
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_approval_record(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_approval_record(_payload(data))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_approval_response(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_approval_response(_payload(data))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_fs_list(sid: str, data: object) -> object:
        try:
            return await deps.api_fs_list(path=_payload(data).get("path"))
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_fs_search(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            limit_value = payload.get("limit")
            if limit_value in (None, ""):
                limit = 200
            elif isinstance(limit_value, (int, float, str)):
                try:
                    limit = int(limit_value)
                except Exception:
                    return _sio_error("limit must be an integer")
            else:
                return _sio_error("limit must be an integer")
            root_value = payload.get("root")
            root = root_value if isinstance(root_value, str) else None
            return await deps.api_fs_search(
                query=str(payload.get("query") or ""),
                root=root,
                limit=min(max(limit, 1), 200),
            )
        except HTTPException as exc:
            return _sio_error(exc.detail)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_transcript(sid: str, data: object) -> object:
        try:
            return await deps.api_appserver_transcript(conversation_id=_payload(data).get("conversation_id"))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_transcript_range(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            offset_raw = payload.get("offset", 0)
            limit_raw = payload.get("limit", 120)
            if not isinstance(offset_raw, (int, float, str)) or not isinstance(limit_raw, (int, float, str)):
                return _sio_error("offset and limit must be integers")
            try:
                offset = int(offset_raw)
                limit = int(limit_raw)
            except Exception:
                return _sio_error("offset and limit must be integers")
            include_internal = deps.coerce_query_bool(payload.get("include_internal", False))
            return await deps.api_appserver_transcript_range(
                conversation_id=payload.get("conversation_id"),
                offset=offset,
                limit=min(limit, 500),
                include_internal=include_internal,
            )
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_extension_models(sid: str, data: object) -> object:
        try:
            ext_id_value = _payload(data).get("extension_id")
            ext_id = ext_id_value.strip() if isinstance(ext_id_value, str) and ext_id_value.strip() else ""
            if not ext_id or not ext_loader.has_extension(ext_id):
                return _sio_error(f"Unknown extension: {ext_id}")
            return cast(object, await ext_loader.list_models(ext_id))
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_te2_agent_open(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            print(f"[Sidebar] te2_agent_open received: {payload}")
            await deps.emit_sidebar_agent_open(payload)
            return {"ok": True}
        except Exception as exc:
            print(f"[Sidebar] te2_agent_open error: {exc}")
            return _sio_error(exc)

    async def _sio_open_external_url(sid: str, data: object) -> object:
        try:
            url = _payload(data).get("url")
            ok, error = await _open_external_http_url(str(url or ""))
            if not ok:
                return _sio_error(error or "Failed to open URL")
            return {"ok": True, "url": str(url or "").strip()}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_get_log_messages(sid: str, data: object) -> object:
        try:
            limit_value = _payload(data).get("limit")
            limit: int | None = None
            if limit_value is not None:
                if not isinstance(limit_value, (int, float, str)):
                    return _sio_error("limit must be an integer")
                try:
                    limit = int(limit_value)
                except Exception:
                    return _sio_error("limit must be an integer")
            return deps.read_records(limit=limit)
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_post_log_message(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            who = str(payload.get("who") or "").strip()
            text = str(payload.get("message") or "").strip()
            if not who or not text:
                return _sio_error("Both 'who' and 'message' are required")
            record: ObjectMap = {"ts": deps.utc_ts(), "who": who, "message": text}
            await deps.append_record(record)
            return record
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_shutdown_request(sid: str, data: object) -> object:
        try:
            return await deps.api_shutdown()
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_list(sid: str, data: object) -> object:
        try:
            cid = _resolve_conversation_id(data)
            if not cid:
                return _sio_error("no conversation")
            status_value = _payload(data).get("status")
            status = status_value if isinstance(status_value, str) else None
            return {"ok": True, "todos": _conv_todos.list_todos(cid, status=status)}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_add(sid: str, data: object) -> object:
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
                description=str(payload.get("description") or ""),
                status=str(payload.get("status") or "pending"),
            )
            return {"ok": True, "todo": todo}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_update(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            try:
                todo_id_value = payload["id"]
                if not isinstance(todo_id_value, (int, float, str)):
                    return _sio_error("id must be an integer")
                todo_id = int(todo_id_value)
            except Exception:
                return _sio_error("id must be an integer")
            title_value = payload.get("title")
            title = title_value if isinstance(title_value, str) else None
            description_value = payload.get("description")
            description = description_value if isinstance(description_value, str) else None
            status_value = payload.get("status")
            status = status_value if isinstance(status_value, str) else None
            result = _conv_todos.update_todo(
                cid,
                todo_id,
                title=title,
                description=description,
                status=status,
            )
            if result is None:
                return _sio_error("todo not found")
            return {"ok": True, "todo": result}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_remove(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            try:
                todo_id_value = payload["id"]
                if not isinstance(todo_id_value, (int, float, str)):
                    return _sio_error("id must be an integer")
                todo_id = int(todo_id_value)
            except Exception:
                return _sio_error("id must be an integer")
            removed = _conv_todos.remove_todo(cid, todo_id)
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_toggle(sid: str, data: object) -> object:
        try:
            payload = _payload(data)
            cid = _resolve_conversation_id(payload)
            if not cid:
                return _sio_error("no conversation")
            if "id" not in payload:
                return _sio_error("id required")
            try:
                todo_id_value = payload["id"]
                if not isinstance(todo_id_value, (int, float, str)):
                    return _sio_error("id must be an integer")
                todo_id = int(todo_id_value)
            except Exception:
                return _sio_error("id must be an integer")
            result = _conv_todos.toggle_todo(cid, todo_id)
            if result is None:
                return _sio_error("todo not found")
            return {"ok": True, "todo": result}
        except Exception as exc:
            return _sio_error(exc)

    async def _sio_todo_ready(sid: str, data: object) -> object:
        try:
            cid = _resolve_conversation_id(data)
            if not cid:
                return _sio_error("no conversation")
            return {"ok": True, "todos": _conv_todos.list_ready(cid)}
        except Exception as exc:
            return _sio_error(exc)

    registrations: list[tuple[str, Callable[..., Awaitable[object]]]] = [
        ("connect", _appserver_connect),
        ("disconnect", _appserver_disconnect),
        ("shell_exec", _sio_shell_exec),
        ("rpc", _sio_rpc),
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

    conversations_rpc_registrations: list[tuple[str, Callable[..., Awaitable[object]]]] = [
        ("connect", _conversations_rpc_connect),
        ("disconnect", _conversations_rpc_disconnect),
        ("rpc", _sio_conversations_rpc),
    ]
    for event, handler in conversations_rpc_registrations:
        socketio_server.on(event, handler, namespace=CONVERSATIONS_RPC_NAMESPACE)

    settings_rpc_registrations: list[tuple[str, Callable[..., Awaitable[object]]]] = [
        ("connect", _settings_rpc_connect),
        ("disconnect", _settings_rpc_disconnect),
        ("rpc", _sio_settings_rpc),
    ]
    for event, handler in settings_rpc_registrations:
        socketio_server.on(event, handler, namespace=SETTINGS_RPC_NAMESPACE)

    ui_rpc_registrations: list[tuple[str, Callable[..., Awaitable[object]]]] = [
        ("connect", _ui_rpc_connect),
        ("disconnect", _ui_rpc_disconnect),
        ("rpc", _sio_ui_rpc),
    ]
    for event, handler in ui_rpc_registrations:
        socketio_server.on(event, handler, namespace=UI_RPC_NAMESPACE)

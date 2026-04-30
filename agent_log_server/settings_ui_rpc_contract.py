from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from fastapi import HTTPException

from agent_log_server.typing_helpers import ObjectMap, RequestId, coerce_object_map

SETTINGS_RPC_NAMESPACE = "/rpc/settings"
UI_RPC_NAMESPACE = "/rpc/ui"

SETTINGS_CONFIG_GET_METHOD = "config.get"
SETTINGS_CONFIG_UPDATE_METHOD = "config.update"
SETTINGS_EXTENSIONS_LIST_METHOD = "extensions.list"
SETTINGS_EXTENSIONS_RELOAD_METHOD = "extensions.reload"
SETTINGS_EXTENSION_ENABLED_SET_METHOD = "extension.enabled.set"
SETTINGS_EXTENSION_INSTALL_METHOD = "extension.install"
SETTINGS_EXTENSION_SPLASH_SCHEMA_GET_METHOD = "extension.splashSchema.get"
SETTINGS_EXTENSION_SPLASH_ACTION_RUN_METHOD = "extension.splashAction.run"
SETTINGS_EXTENSION_SETTINGS_SCHEMA_GET_METHOD = "extension.settingsSchema.get"
SETTINGS_EXTENSION_RUNTIME_OPTIONS_GET_METHOD = "extension.runtimeOptions.get"
SETTINGS_EXTENSION_REQUEST_CARDS_GET_METHOD = "extension.requestCards.get"
SETTINGS_EXTENSION_UI_FEATURES_GET_METHOD = "extension.uiFeatures.get"
SETTINGS_EXTENSION_PLAN_GET_METHOD = "extension.plan.get"
SETTINGS_EXTENSION_MODELS_LIST_METHOD = "extension.models.list"
SETTINGS_EXTENSION_SESSIONS_LIST_METHOD = "extension.sessions.list"
SETTINGS_EXTENSION_SESSION_BIND_METHOD = "extension.session.bind"
SETTINGS_STATUS_GET_METHOD = "status.get"

SETTINGS_EXTENSIONS_UPDATED_NOTIFICATION = "extensions.updated"
SETTINGS_CONFIG_UPDATED_NOTIFICATION = "config.updated"

UI_VIEW_GET_METHOD = "view.get"
UI_VIEW_SET_METHOD = "view.set"
UI_HOST_UI_GET_METHOD = "hostUi.get"
UI_HOST_UI_RECHECK_METHOD = "hostUi.recheck"
UI_FILESYSTEM_LIST_METHOD = "filesystem.list"
UI_FILESYSTEM_SEARCH_METHOD = "filesystem.search"
UI_FILE_OPEN_METHOD = "file.open"
UI_URL_OPEN_METHOD = "url.open"

UI_VIEW_CHANGED_NOTIFICATION = "view.changed"
UI_HOST_UI_UPDATED_NOTIFICATION = "hostUi.updated"

SETTINGS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE: dict[str, str] = {
    "extensions_updated": SETTINGS_EXTENSIONS_UPDATED_NOTIFICATION,
}
UI_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE: dict[str, str] = {
    "host_ui": UI_HOST_UI_UPDATED_NOTIFICATION,
}

SettingsRpcMethod: TypeAlias = Literal[
    "config.get",
    "config.update",
    "extensions.list",
    "extensions.reload",
    "extension.enabled.set",
    "extension.install",
    "extension.splashSchema.get",
    "extension.splashAction.run",
    "extension.settingsSchema.get",
    "extension.runtimeOptions.get",
    "extension.requestCards.get",
    "extension.uiFeatures.get",
    "extension.plan.get",
    "extension.models.list",
    "extension.sessions.list",
    "extension.session.bind",
    "status.get",
]

UiRpcMethod: TypeAlias = Literal[
    "view.get",
    "view.set",
    "hostUi.get",
    "hostUi.recheck",
    "filesystem.list",
    "filesystem.search",
    "file.open",
    "url.open",
]


class SettingsUiRpcProtocolError(Exception):
    def __init__(
        self,
        request_id: RequestId,
        *,
        code: int,
        message: str,
        data: ObjectMap | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass(frozen=True)
class JsonRpcSuccessResponse:
    request_id: RequestId
    result: ObjectMap

    def to_json(self) -> ObjectMap:
        return {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "result": self.result,
        }


@dataclass(frozen=True)
class JsonRpcErrorResponse:
    request_id: RequestId
    code: int
    message: str
    data: ObjectMap | None = None

    def to_json(self) -> ObjectMap:
        error: ObjectMap = {
            "code": self.code,
            "message": self.message,
        }
        if self.data:
            error["data"] = self.data
        return {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "error": error,
        }


@dataclass(frozen=True)
class ParsedSettingsRpcRequest:
    request_id: RequestId
    method: SettingsRpcMethod
    params: ObjectMap


@dataclass(frozen=True)
class ParsedUiRpcRequest:
    request_id: RequestId
    method: UiRpcMethod
    params: ObjectMap


def coerce_request_id(value: object) -> RequestId:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int)) else None


def settings_rpc_notification_method(evt_type: object) -> str | None:
    normalized = str(evt_type or "").strip().lower()
    if not normalized:
        return None
    return SETTINGS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE.get(normalized)


def ui_rpc_notification_method(evt_type: object) -> str | None:
    normalized = str(evt_type or "").strip().lower()
    if not normalized:
        return None
    return UI_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE.get(normalized)


def build_jsonrpc_success_response(
    request_id: RequestId,
    result: ObjectMap,
) -> JsonRpcSuccessResponse:
    return JsonRpcSuccessResponse(request_id=request_id, result=result)


def build_jsonrpc_error_response(
    request_id: RequestId,
    *,
    code: int,
    message: str,
    data: ObjectMap | None = None,
) -> JsonRpcErrorResponse:
    return JsonRpcErrorResponse(
        request_id=request_id,
        code=code,
        message=message,
        data=data,
    )


def build_jsonrpc_error_response_from_http_exception(
    request_id: RequestId,
    exc: HTTPException,
) -> JsonRpcErrorResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        detail_map = coerce_object_map(detail)
        message = str(detail_map.get("message") or detail_map.get("error") or "Request failed")
        return build_jsonrpc_error_response(
            request_id,
            code=int(exc.status_code or 500),
            message=message,
            data=detail_map or None,
        )
    return build_jsonrpc_error_response(
        request_id,
        code=int(exc.status_code or 500),
        message=str(detail or "Request failed"),
    )


def build_jsonrpc_notification(
    method: str,
    params: ObjectMap | None = None,
) -> ObjectMap:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }


def _parse_rpc_request(
    payload: object,
    *,
    allowed_methods: set[str],
) -> tuple[RequestId, str, ObjectMap]:
    request = coerce_object_map(payload)
    request_id = coerce_request_id(request.get("id"))
    if request.get("jsonrpc") != "2.0":
        raise SettingsUiRpcProtocolError(
            request_id,
            code=-32600,
            message="Expected jsonrpc='2.0'",
        )
    method = str(request.get("method") or "").strip()
    if not method:
        raise SettingsUiRpcProtocolError(
            request_id,
            code=-32600,
            message="Missing method",
        )
    if method not in allowed_methods:
        raise SettingsUiRpcProtocolError(
            request_id,
            code=-32601,
            message=f"Unknown method: {method}",
        )
    params = request.get("params")
    if params is None:
        params_map: ObjectMap = {}
    else:
        params_map = coerce_object_map(params)
        if not params_map and params not in ({}, None):
            raise SettingsUiRpcProtocolError(
                request_id,
                code=-32602,
                message="Params must be an object",
            )
    return request_id, method, params_map


def parse_settings_rpc_request(payload: object) -> ParsedSettingsRpcRequest:
    request_id, method, params = _parse_rpc_request(
        payload,
        allowed_methods={
            SETTINGS_CONFIG_GET_METHOD,
            SETTINGS_CONFIG_UPDATE_METHOD,
            SETTINGS_EXTENSIONS_LIST_METHOD,
            SETTINGS_EXTENSIONS_RELOAD_METHOD,
            SETTINGS_EXTENSION_ENABLED_SET_METHOD,
            SETTINGS_EXTENSION_INSTALL_METHOD,
            SETTINGS_EXTENSION_SPLASH_SCHEMA_GET_METHOD,
            SETTINGS_EXTENSION_SPLASH_ACTION_RUN_METHOD,
            SETTINGS_EXTENSION_SETTINGS_SCHEMA_GET_METHOD,
            SETTINGS_EXTENSION_RUNTIME_OPTIONS_GET_METHOD,
            SETTINGS_EXTENSION_REQUEST_CARDS_GET_METHOD,
            SETTINGS_EXTENSION_UI_FEATURES_GET_METHOD,
            SETTINGS_EXTENSION_PLAN_GET_METHOD,
            SETTINGS_EXTENSION_MODELS_LIST_METHOD,
            SETTINGS_EXTENSION_SESSIONS_LIST_METHOD,
            SETTINGS_EXTENSION_SESSION_BIND_METHOD,
            SETTINGS_STATUS_GET_METHOD,
        },
    )
    return ParsedSettingsRpcRequest(
        request_id=request_id,
        method=method,  # type: ignore[arg-type]
        params=params,
    )


def parse_ui_rpc_request(payload: object) -> ParsedUiRpcRequest:
    request_id, method, params = _parse_rpc_request(
        payload,
        allowed_methods={
            UI_VIEW_GET_METHOD,
            UI_VIEW_SET_METHOD,
            UI_HOST_UI_GET_METHOD,
            UI_HOST_UI_RECHECK_METHOD,
            UI_FILESYSTEM_LIST_METHOD,
            UI_FILESYSTEM_SEARCH_METHOD,
            UI_FILE_OPEN_METHOD,
            UI_URL_OPEN_METHOD,
        },
    )
    return ParsedUiRpcRequest(
        request_id=request_id,
        method=method,  # type: ignore[arg-type]
        params=params,
    )

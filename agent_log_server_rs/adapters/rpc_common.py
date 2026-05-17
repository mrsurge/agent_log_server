from __future__ import annotations

from pathlib import Path
from typing import TypeAlias, cast

from agent_log_server_rs.adapter_protocol import (
    AdapterModelInfo,
    ConversationAckResult,
    JsonMap,
)

RpcId: TypeAlias = str | int | None

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcAdapterError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def adapter_model(model: JsonMap) -> AdapterModelInfo:
    model_id = required_string(model, "id")
    name = optional_string(model.get("name"))
    capabilities = optional_map(model.get("capabilities")) or {}
    efforts = string_list(model.get("supported_reasoning_efforts"))
    return AdapterModelInfo(
        id=model_id,
        name=name,
        context_window=context_window(model, capabilities),
        supported_reasoning_efforts=efforts,
        capabilities=capabilities,
        raw=dict(model),
    )


def context_window(model: JsonMap, capabilities: JsonMap) -> int | None:
    limits = optional_map(capabilities.get("limits")) or {}
    for candidate in (
        limits.get("max_context_window_tokens"),
        limits.get("maxContextWindowTokens"),
        model.get("context_window"),
        model.get("max_context_window_tokens"),
        model.get("maxContextWindowTokens"),
    ):
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return None


def ack_from_result(conversation_id: str, result: JsonMap) -> ConversationAckResult:
    ok = result.get("ok") is True or result.get("accepted") is True
    provider_session_id = (
        optional_string(result.get("provider_session_id"))
        or optional_string(result.get("thread_id"))
        or optional_string(result.get("session_id"))
    )
    if provider_session_id == conversation_id:
        provider_session_id = None
    if not ok:
        provider_session_id = None
    return ConversationAckResult(
        conversation_id=conversation_id,
        accepted=ok,
        provider_session_id=provider_session_id,
        provider_call_id=optional_string(result.get("provider_call_id")),
        turn_id=optional_string(result.get("turn_id")),
        restore_draft=result.get("restore_draft") is True,
        metadata=dict(result),
    )


def merged_settings(base: JsonMap, override: JsonMap | None) -> JsonMap:
    merged = dict(base)
    if override:
        merged.update(override)
    return merged


def params_map(params: object) -> JsonMap:
    if params is None:
        return {}
    mapping = optional_map(params)
    if mapping is None:
        raise RpcAdapterError(INVALID_PARAMS, "Params must be an object")
    return mapping


def optional_map(value: object) -> JsonMap | None:
    if isinstance(value, dict):
        return cast(JsonMap, value)
    return None


def required_string(mapping: JsonMap, key: str) -> str:
    value = optional_string(mapping.get(key))
    if value is None:
        raise RpcAdapterError(INVALID_PARAMS, f"Missing string param: {key}")
    return value


def optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def string(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    raise RpcAdapterError(INVALID_REQUEST, "Missing method")


def optional_path(value: object) -> Path | None:
    text = optional_string(value)
    return Path(text) if text else None


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def rpc_id(value: object) -> RpcId:
    if value is None or isinstance(value, (str, int)):
        return value
    raise RpcAdapterError(INVALID_REQUEST, "Invalid request id")

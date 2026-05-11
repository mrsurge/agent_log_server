from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol, cast
from uuid import UUID

from ._vendor.copilot.client import CopilotClient
from ._vendor.copilot.generated.session_events import SessionEvent, SessionEventType
from ._vendor.copilot.session import (
    CopilotSession,
    _PermissionHandlerFn,
)
from .protocol_registry import load_copilot_protocol_registry


def _snake_to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _coerce_scalar(value: object) -> object:
    if isinstance(value, Enum):
        return cast(object, value.value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _payload_value(payload: object, key: str) -> object:
    if payload is None:
        return None
    if hasattr(payload, key):
        return _coerce_scalar(cast(object, getattr(payload, key)))
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if value is None:
            value = payload.get(_snake_to_camel(key))
        return _coerce_scalar(cast(object, value))
    return None


def _payload_string(payload: object, key: str) -> Optional[str]:
    value = _payload_value(payload, key)
    return value if isinstance(value, str) and value else None


def _payload_bool(payload: object, key: str) -> Optional[bool]:
    value = _payload_value(payload, key)
    return value if isinstance(value, bool) else None


class _SupportsCompact(Protocol):
    async def compact(self, *, timeout: float | None = None) -> object: ...


def _compact_api_from_rpc(rpc: object) -> Optional[_SupportsCompact]:
    for group_name in ("history", "compaction"):
        group = getattr(rpc, group_name, None)
        compact = getattr(group, "compact", None)
        if callable(compact):
            return cast(_SupportsCompact, group)
    return None


async def compact_sdk_session(session: CopilotSession, *, timeout: float | None = None) -> object:
    api = _compact_api_from_rpc(session.rpc)
    if api is None:
        registry = load_copilot_protocol_registry()
        raise RuntimeError(
            "No public compaction API available on vendored Copilot session RPC "
            f"(generated methods: {sorted(method for method in registry.generated_session_methods if 'compact' in method)})"
        )
    return await api.compact(timeout=timeout)


def _resume_session_kwargs(
    client: CopilotClient,
    config: Mapping[str, object],
) -> dict[str, object]:
    supported = inspect.signature(client.resume_session).parameters
    kwargs: dict[str, object] = {}
    for name in supported:
        if name in {"self", "session_id"}:
            continue
        if name not in config:
            continue
        value = config[name]
        if value is None:
            continue
        kwargs[name] = value
    return kwargs


async def resume_sdk_session(
    client: CopilotClient,
    session_id: str,
    config: Mapping[str, object],
) -> CopilotSession:
    kwargs = _resume_session_kwargs(client, config)
    permission_handler = kwargs.get("on_permission_request")
    if permission_handler is None:
        raise ValueError("resume config missing on_permission_request")
    kwargs["on_permission_request"] = cast(_PermissionHandlerFn, permission_handler)
    resume_call = cast(Callable[..., Awaitable[CopilotSession]], client.resume_session)
    return await resume_call(session_id, **kwargs)


@dataclass(frozen=True)
class CopilotEventView:
    event_type: SessionEventType
    event_id: str
    parent_id: Optional[str]
    payload: object

    @classmethod
    def from_event(cls, event: SessionEvent) -> "CopilotEventView":
        event_id = str(event.id).strip()
        parent_id = str(event.parent_id).strip() if event.parent_id is not None else None
        return cls(
            event_type=event.type,
            event_id=event_id,
            parent_id=parent_id or None,
            payload=event.data,
        )

    @property
    def current_model(self) -> Optional[str]:
        return _payload_string(self.payload, "current_model")

    @property
    def selected_model(self) -> Optional[str]:
        return _payload_string(self.payload, "selected_model")

    @property
    def new_model(self) -> Optional[str]:
        return _payload_string(self.payload, "new_model")

    @property
    def model(self) -> Optional[str]:
        return _payload_string(self.payload, "model")

    @property
    def token_limit(self) -> object:
        return _payload_value(self.payload, "token_limit")

    @property
    def post_compaction_tokens(self) -> object:
        return _payload_value(self.payload, "post_compaction_tokens")

    @property
    def post_truncation_tokens_in_messages(self) -> object:
        return _payload_value(self.payload, "post_truncation_tokens_in_messages")

    @property
    def current_tokens(self) -> object:
        return _payload_value(self.payload, "current_tokens")

    @property
    def pre_compaction_tokens(self) -> object:
        return _payload_value(self.payload, "pre_compaction_tokens")

    @property
    def pre_truncation_tokens_in_messages(self) -> object:
        return _payload_value(self.payload, "pre_truncation_tokens_in_messages")

    @property
    def messages_removed(self) -> object:
        return _payload_value(self.payload, "messages_removed")

    @property
    def messages_removed_during_truncation(self) -> object:
        return _payload_value(self.payload, "messages_removed_during_truncation")

    @property
    def tokens_removed(self) -> object:
        return _payload_value(self.payload, "tokens_removed")

    @property
    def tokens_removed_during_truncation(self) -> object:
        return _payload_value(self.payload, "tokens_removed_during_truncation")

    @property
    def tool_call_id(self) -> Optional[str]:
        return _payload_string(self.payload, "tool_call_id")

    @property
    def parent_tool_call_id(self) -> Optional[str]:
        return _payload_string(self.payload, "parent_tool_call_id")

    @property
    def reasoning_id(self) -> Optional[str]:
        return _payload_string(self.payload, "reasoning_id")

    @property
    def message_id(self) -> Optional[str]:
        return _payload_string(self.payload, "message_id")

    @property
    def reasoning_text(self) -> Optional[str]:
        return _payload_string(self.payload, "reasoning_text") or _payload_string(self.payload, "content")

    @property
    def delta_content(self) -> Optional[str]:
        return _payload_string(self.payload, "delta_content")

    @property
    def content(self) -> Optional[str]:
        return _payload_string(self.payload, "content")

    @property
    def agent_mode(self) -> Optional[str]:
        return _payload_string(self.payload, "agent_mode")

    @property
    def mcp_server_name(self) -> Optional[str]:
        return _payload_string(self.payload, "mcp_server_name")

    @property
    def mcp_tool_name(self) -> Optional[str]:
        return _payload_string(self.payload, "mcp_tool_name")

    @property
    def tool_name(self) -> Optional[str]:
        return _payload_string(self.payload, "tool_name")

    @property
    def arguments(self) -> object:
        return _payload_value(self.payload, "arguments")

    @property
    def path(self) -> Optional[str]:
        return _payload_string(self.payload, "path")

    @property
    def output(self) -> object:
        return _payload_value(self.payload, "output")

    @property
    def error(self) -> object:
        return _payload_value(self.payload, "error")

    @property
    def error_reason(self) -> object:
        return _payload_value(self.payload, "error_reason")

    @property
    def partial_output(self) -> Optional[str]:
        return _payload_string(self.payload, "partial_output")

    @property
    def progress_message(self) -> Optional[str]:
        return _payload_string(self.payload, "progress_message")

    @property
    def result(self) -> object:
        return _payload_value(self.payload, "result")

    @property
    def result_content(self) -> str:
        return _payload_string(self.result, "content") or ""

    @property
    def result_detailed_content(self) -> str:
        return _payload_string(self.result, "detailed_content") or ""

    @property
    def intent(self) -> Optional[str]:
        return _payload_string(self.payload, "intent")

    @property
    def input_tokens(self) -> object:
        return _payload_value(self.payload, "input_tokens")

    @property
    def output_tokens(self) -> object:
        return _payload_value(self.payload, "output_tokens")

    @property
    def cache_read_tokens(self) -> object:
        return _payload_value(self.payload, "cache_read_tokens")

    @property
    def message(self) -> Optional[str]:
        return _payload_string(self.payload, "message")

    @property
    def error_type(self) -> Optional[str]:
        return _payload_string(self.payload, "error_type")

    @property
    def status_code(self) -> object:
        return _payload_value(self.payload, "status_code")

    @property
    def provider_call_id(self) -> Optional[str]:
        return _payload_string(self.payload, "provider_call_id")

    @property
    def stack(self) -> Optional[str]:
        return _payload_string(self.payload, "stack")

    @property
    def plan_content(self) -> Optional[str]:
        return _payload_string(self.payload, "plan_content")

    @property
    def operation(self) -> Optional[str]:
        return _payload_string(self.payload, "operation")

    @property
    def recommended_action(self) -> Optional[str]:
        return _payload_string(self.payload, "recommended_action")

    @property
    def new_mode(self) -> Optional[str]:
        return _payload_string(self.payload, "new_mode")

    @property
    def mode(self) -> Optional[str]:
        return _payload_string(self.payload, "mode")

    @property
    def previous_mode(self) -> Optional[str]:
        return _payload_string(self.payload, "previous_mode")

    @property
    def agent_display_name(self) -> Optional[str]:
        return _payload_string(self.payload, "agent_display_name")

    @property
    def agent_name(self) -> Optional[str]:
        return _payload_string(self.payload, "agent_name")

    @property
    def summary(self) -> Optional[str]:
        return _payload_string(self.payload, "summary")

    @property
    def success(self) -> Optional[bool]:
        return _payload_bool(self.payload, "success")

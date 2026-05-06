from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

JsonMap: TypeAlias = dict[str, object]

ADAPTER_PROTOCOL_VERSION = "0.1.0"


def _put_optional(payload: JsonMap, key: str, value: object | None) -> None:
    if value is not None:
        payload[key] = value


class AdapterMethod(StrEnum):
    EXTENSION_INITIALIZE = "extension.initialize"
    EXTENSION_SHUTDOWN = "extension.shutdown"
    EXTENSION_RELOAD = "extension.reload"
    EXTENSION_INSTALL_DEPENDENCIES = "extension.install_dependencies"
    EXTENSION_DEBUG_PROBE = "extension.debug_probe"
    EXTENSION_WARM_UP = "extension.warm_up"
    EXTENSION_GET_SETTINGS_SCHEMA = "extension.get_settings_schema"
    EXTENSION_GET_SPLASH_SCHEMA = "extension.get_splash_schema"
    EXTENSION_LIST_MODELS = "extension.list_models"
    EXTENSION_LIST_SESSIONS = "extension.list_sessions"
    CONVERSATION_START = "conversation.start"
    CONVERSATION_RESUME = "conversation.resume"
    CONVERSATION_SEND = "conversation.send"
    CONVERSATION_INTERRUPT = "conversation.interrupt"
    CONVERSATION_COMPACT = "conversation.compact"
    APPROVAL_RESPOND = "approval.respond"


class AdapterEventMethod(StrEnum):
    LIVE_EVENT = "event.live"
    TRANSCRIPT_RECORD = "event.transcript_record"
    STATUS = "event.status"
    USER_MESSAGE = "event.user_message"
    ASSISTANT_DELTA = "event.assistant_delta"
    ASSISTANT_FINALIZE = "event.assistant_finalize"
    REASONING_DELTA = "event.reasoning_delta"
    REASONING_FINALIZE = "event.reasoning_finalize"
    TOOL_BEGIN = "event.tool_begin"
    TOOL_DELTA = "event.tool_delta"
    TOOL_END = "event.tool_end"
    SHELL_BEGIN = "event.shell_begin"
    SHELL_DELTA = "event.shell_delta"
    SHELL_END = "event.shell_end"
    APPROVAL_REQUEST = "event.approval_request"
    TOKEN_USAGE = "event.token_usage"
    ERROR = "event.error"
    WARNING = "event.warning"


class AdapterLiveEventType(StrEnum):
    MESSAGE = "message"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_FINALIZE = "assistant_finalize"
    REASONING_DELTA = "reasoning_delta"
    REASONING_FINALIZE = "reasoning_finalize"
    THOUGHT = "thought"
    TOOL_INTERACTION = "tool_interaction"
    TOOL_BEGIN = "tool_begin"
    TOOL_DELTA = "tool_delta"
    TOOL_END = "tool_end"
    SHELL_BEGIN = "shell_begin"
    SHELL_DELTA = "shell_delta"
    SHELL_END = "shell_end"
    COMMAND_RESULT = "command_result"
    DIFF = "diff"
    ERROR = "error"
    WARNING = "warning"
    STATUS = "status"
    TOKEN_COUNT = "token_count"
    APPROVAL = "approval"
    APPROVAL_HANDOFF = "approval_handoff"
    TOAST = "toast"
    PLAN = "plan"
    PLAN_STATE = "plan_state"
    PLAN_UPDATE = "plan_update"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    SEARCH = "search"
    VIEW = "view"
    ACTIVITY = "activity"


class AdapterTranscriptRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    COMMAND = "command"
    VIEW = "view"
    SEARCH = "search"
    DIFF = "diff"
    ERROR = "error"
    TOKEN_USAGE = "token_usage"
    CONTEXT_COMPACTED = "context_compacted"
    TOOL = "tool"
    MCP_TOOL = "mcp_tool"
    WEB_SEARCH = "web_search"
    DEBUG_RAW = "debug_raw"


@dataclass(frozen=True)
class AdapterCapabilities:
    conversations: bool = False
    models: bool = False
    sessions: bool = False
    approvals: bool = False
    compaction: bool = False
    interruption: bool = False
    live_events: bool = False
    transcript_records: bool = False
    extra: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {
            "conversations": self.conversations,
            "models": self.models,
            "sessions": self.sessions,
            "approvals": self.approvals,
            "compaction": self.compaction,
            "interruption": self.interruption,
            "live_events": self.live_events,
            "transcript_records": self.transcript_records,
        }
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


@dataclass(frozen=True)
class ExtensionInitializeParams:
    extension_id: str
    cwd: Path
    data_dir: Path
    cache_dir: Path
    config_dir: Path
    extensions_dir: Path | None = None
    settings: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {
            "extension_id": self.extension_id,
            "cwd": str(self.cwd),
            "data_dir": str(self.data_dir),
            "cache_dir": str(self.cache_dir),
            "config_dir": str(self.config_dir),
        }
        if self.extensions_dir:
            payload["extensions_dir"] = str(self.extensions_dir)
        if self.settings:
            payload["settings"] = dict(self.settings)
        return payload


@dataclass(frozen=True)
class ExtensionInitializeResult:
    extension_id: str
    capabilities: AdapterCapabilities
    provider: str | None = None
    protocol_version: str = ADAPTER_PROTOCOL_VERSION

    def to_json(self) -> JsonMap:
        payload: JsonMap = {
            "extension_id": self.extension_id,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities.to_json(),
        }
        if self.provider:
            payload["provider"] = self.provider
        return payload


@dataclass(frozen=True)
class AdapterModelInfo:
    id: str
    name: str | None = None
    context_window: int | None = None
    supported_reasoning_efforts: list[str] = field(default_factory=list)
    capabilities: JsonMap = field(default_factory=dict)
    raw: object | None = None

    def to_json(self) -> JsonMap:
        payload: JsonMap = {"id": self.id}
        if self.name:
            payload["name"] = self.name
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        if self.supported_reasoning_efforts:
            payload["supported_reasoning_efforts"] = list(self.supported_reasoning_efforts)
        if self.capabilities:
            payload["capabilities"] = dict(self.capabilities)
        if self.raw is not None:
            payload["raw"] = self.raw
        return payload


@dataclass(frozen=True)
class AdapterSessionInfo:
    id: str
    label: str | None = None
    cwd: Path | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {"id": self.id}
        if self.label:
            payload["label"] = self.label
        if self.cwd is not None:
            payload["cwd"] = str(self.cwd)
        if self.created_at:
            payload["created_at"] = self.created_at
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class ConversationSendParams:
    extension_id: str
    conversation_id: str
    text: str
    thread_id: str | None = None
    provider_session_id: str | None = None
    turn_id: str | None = None
    cwd: Path | None = None
    attachments: list[JsonMap] = field(default_factory=list)
    toast_context: JsonMap | None = None
    settings: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {
            "extension_id": self.extension_id,
            "conversation_id": self.conversation_id,
            "text": self.text,
        }
        if self.thread_id:
            payload["thread_id"] = self.thread_id
        if self.provider_session_id:
            payload["provider_session_id"] = self.provider_session_id
        if self.turn_id:
            payload["turn_id"] = self.turn_id
        if self.cwd:
            payload["cwd"] = str(self.cwd)
        if self.attachments:
            payload["attachments"] = [dict(item) for item in self.attachments]
        if self.toast_context:
            payload["toast_context"] = dict(self.toast_context)
        if self.settings:
            payload["settings"] = dict(self.settings)
        return payload


@dataclass(frozen=True)
class ConversationAckResult:
    conversation_id: str
    accepted: bool
    provider_session_id: str | None = None
    provider_call_id: str | None = None
    turn_id: str | None = None
    restore_draft: bool = False
    metadata: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {
            "conversation_id": self.conversation_id,
            "ok": self.accepted,
            "accepted": self.accepted,
            "restore_draft": self.restore_draft,
        }
        if self.provider_session_id:
            payload["provider_session_id"] = self.provider_session_id
        if self.provider_call_id:
            payload["provider_call_id"] = self.provider_call_id
        if self.turn_id:
            payload["turn_id"] = self.turn_id
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class AdapterLiveEvent:
    type: AdapterLiveEventType
    conversation_id: str | None = None
    id: str | None = None
    turn_id: str | None = None
    subagent_id: str | None = None
    role: str | None = None
    text: str | None = None
    delta: str | None = None
    message: str | None = None
    path: Path | None = None
    line: int | None = None
    column: int | None = None
    timestamp: str | None = None
    extra: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {"type": self.type.value}
        _put_optional(payload, "conversation_id", self.conversation_id)
        _put_optional(payload, "id", self.id)
        _put_optional(payload, "turn_id", self.turn_id)
        _put_optional(payload, "subagent_id", self.subagent_id)
        _put_optional(payload, "role", self.role)
        _put_optional(payload, "text", self.text)
        _put_optional(payload, "delta", self.delta)
        _put_optional(payload, "message", self.message)
        _put_optional(payload, "line", self.line)
        _put_optional(payload, "column", self.column)
        _put_optional(payload, "timestamp", self.timestamp)
        if self.path is not None:
            payload["path"] = str(self.path)
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class AdapterTranscriptRecord:
    role: AdapterTranscriptRole
    id: str | None = None
    item_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    subagent_id: str | None = None
    text: str | None = None
    message: str | None = None
    timestamp: str | None = None
    path: Path | None = None
    line: int | None = None
    column: int | None = None
    internal: bool | None = None
    extra: JsonMap = field(default_factory=dict)

    def to_json(self) -> JsonMap:
        payload: JsonMap = {"role": self.role.value}
        _put_optional(payload, "id", self.id)
        _put_optional(payload, "item_id", self.item_id)
        _put_optional(payload, "conversation_id", self.conversation_id)
        _put_optional(payload, "turn_id", self.turn_id)
        _put_optional(payload, "subagent_id", self.subagent_id)
        _put_optional(payload, "text", self.text)
        _put_optional(payload, "message", self.message)
        _put_optional(payload, "timestamp", self.timestamp)
        _put_optional(payload, "line", self.line)
        _put_optional(payload, "column", self.column)
        _put_optional(payload, "internal", self.internal)
        if self.path is not None:
            payload["path"] = str(self.path)
        payload.update(self.extra)
        return payload

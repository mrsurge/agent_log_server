from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from fastapi import HTTPException

from agent_log_server.typing_helpers import (
    ObjectList,
    ObjectMap,
    RequestId,
    coerce_object_list,
    coerce_object_map,
)

CONVERSATIONS_RPC_NAMESPACE = "/rpc/conversations"
CONVERSATION_SEND_METHOD = "conversation.send"
CONVERSATION_INTERRUPT_METHOD = "conversation.interrupt"
CONVERSATION_COMPACT_METHOD = "conversation.compact"
CONVERSATION_REPLAY_GET_CHUNK_METHOD = "conversation.replay.getChunk"
CONVERSATION_APPROVAL_RESPONSE_METHOD = "conversation.approval.respond"
CONVERSATION_SHELL_EXEC_METHOD = "conversation.shell.exec"
CONVERSATION_GET_METHOD = "conversation.get"
CONVERSATION_LIST_METHOD = "conversation.list"
CONVERSATION_CREATE_METHOD = "conversation.create"
CONVERSATION_SELECT_METHOD = "conversation.select"
CONVERSATION_UPDATE_METHOD = "conversation.update"
CONVERSATION_DELETE_METHOD = "conversation.delete"
CONVERSATION_PINS_SET_METHOD = "conversation.pins.set"
CONVERSATION_DRAFT_SET_METHOD = "conversation.draft.set"
CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE: dict[str, str] = {
    "activity": "conversation.activity",
    "approval": "conversation.approval.request",
    "approval_handoff": "conversation.approval.handoff",
    "assistant_delta": "conversation.message.delta",
    "assistant_end": "conversation.message.final",
    "assistant_finalize": "conversation.message.final",
    "command_result": "conversation.command.result",
    "context_compacted": "conversation.context.compacted",
    "diff": "conversation.diff",
    "diff_declined": "conversation.diff.declined",
    "draft_update": "conversation.draft.updated",
    "error": "conversation.error",
    "mention_insert": "conversation.mention.inserted",
    "message": "conversation.user.message",
    "meta_updated": "conversation.meta.updated",
    "mode": "conversation.mode.changed",
    "plan": "conversation.plan",
    "plan_state": "conversation.plan.state",
    "plan_update": "conversation.plan.update",
    "preview_updated": "conversation.preview.updated",
    "reasoning_delta": "conversation.reasoning.delta",
    "reasoning_end": "conversation.reasoning.final",
    "reasoning_finalize": "conversation.reasoning.final",
    "shell_begin": "conversation.command.begin",
    "shell_delta": "conversation.command.delta",
    "shell_end": "conversation.command.end",
    "status": "conversation.status",
    "subagent_end": "conversation.subagent.end",
    "subagent_start": "conversation.subagent.start",
    "thought": "conversation.thought",
    "toast": "conversation.toast",
    "token_count": "conversation.token.updated",
    "tool_interaction": "conversation.tool.interaction",
    "tool_begin": "conversation.tool.begin",
    "tool_delta": "conversation.tool.delta",
    "tool_end": "conversation.tool.end",
    "search": "conversation.search",
    "view": "conversation.view",
    "warning": "conversation.warning",
}

ConversationsRpcMethod: TypeAlias = Literal[
    "conversation.get",
    "conversation.list",
    "conversation.create",
    "conversation.select",
    "conversation.update",
    "conversation.delete",
    "conversation.pins.set",
    "conversation.draft.set",
    "conversation.send",
    "conversation.interrupt",
    "conversation.compact",
    "conversation.replay.getChunk",
    "conversation.approval.respond",
    "conversation.shell.exec",
]
SanitizeConversationId: TypeAlias = Callable[[str], str]


class ConversationsRpcProtocolError(Exception):
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
class ParsedConversationsRpcRequest:
    request_id: RequestId
    method: ConversationsRpcMethod
    params: ObjectMap


@dataclass(frozen=True)
class ConversationSendParams:
    conversation_id: str
    text: str


@dataclass(frozen=True)
class ConversationGetParams:
    conversation_id: str | None


@dataclass(frozen=True)
class ConversationCreateParams:
    settings: ObjectMap | None

    def to_json(self) -> ObjectMap:
        payload: ObjectMap = {}
        if self.settings:
            payload["settings"] = self.settings
        return payload


@dataclass(frozen=True)
class ConversationSelectParams:
    conversation_id: str
    view: Literal["splash", "conversation"] | None = None

    def to_json(self) -> ObjectMap:
        payload: ObjectMap = {"conversation_id": self.conversation_id}
        if self.view is not None:
            payload["view"] = self.view
        return payload


@dataclass(frozen=True)
class ConversationUpdateParams:
    conversation_id: str | None
    settings: ObjectMap | None = None
    thread_id: str | None = None

    def to_json(self) -> ObjectMap:
        payload: ObjectMap = {}
        if self.conversation_id:
            payload["conversation_id"] = self.conversation_id
        if self.settings is not None:
            payload["settings"] = self.settings
        if self.thread_id:
            payload["thread_id"] = self.thread_id
        return payload


@dataclass(frozen=True)
class ConversationDeleteParams:
    conversation_id: str


@dataclass(frozen=True)
class ConversationPinsSetParams:
    pinned_conversations: list[str]

    def to_json(self) -> ObjectMap:
        return {"pinned_conversations": list(self.pinned_conversations)}


@dataclass(frozen=True)
class ConversationDraftSetParams:
    conversation_id: str | None
    draft: str

    def to_json(self) -> ObjectMap:
        payload: ObjectMap = {"draft": self.draft}
        if self.conversation_id:
            payload["conversation_id"] = self.conversation_id
        return payload


@dataclass(frozen=True)
class ConversationControlParams:
    conversation_id: str

    def to_json(self) -> ObjectMap:
        return {"conversation_id": self.conversation_id}


@dataclass(frozen=True)
class ConversationApprovalResponseParams:
    payload: ObjectMap


@dataclass(frozen=True)
class ConversationShellExecParams:
    payload: ObjectMap


@dataclass(frozen=True)
class ReplayChunkCursor:
    offset: int

    def to_json(self) -> ObjectMap:
        return {"offset": self.offset}


@dataclass(frozen=True)
class ConversationReplayGetChunkParams:
    conversation_id: str | None
    cursor: ReplayChunkCursor
    max_entries: int
    max_bytes: int
    include_internal: bool
    format_name: Literal["jsonl"] = "jsonl"


@dataclass(frozen=True)
class ConversationSendResult:
    conversation_id: str
    accepted: bool
    payload: ObjectMap

    def to_json(self) -> ObjectMap:
        normalized = dict(self.payload)
        normalized.setdefault("conversation_id", self.conversation_id)
        normalized["accepted"] = self.accepted
        return normalized


@dataclass(frozen=True)
class ConversationControlResult:
    ok: bool
    error: str | None
    payload: ObjectMap

    def to_json(self) -> ObjectMap:
        normalized = dict(self.payload)
        normalized.setdefault("ok", self.ok)
        if self.error is not None:
            normalized.setdefault("error", self.error)
        return normalized


@dataclass(frozen=True)
class ReplayRangeData:
    conversation_id: str | None
    items: ObjectList
    offset: int
    total_count: int


@dataclass(frozen=True)
class ReplayJsonlChunk:
    jsonl: str
    item_count: int


@dataclass(frozen=True)
class ReplayChunkFrame:
    format_name: Literal["jsonl"]
    offset: int
    item_count: int
    total_count: int
    chunk_index: int
    complete: bool
    next_cursor: ReplayChunkCursor | None
    jsonl: str

    def to_json(self) -> ObjectMap:
        return {
            "format": self.format_name,
            "offset": self.offset,
            "item_count": self.item_count,
            "total_count": self.total_count,
            "chunk_index": self.chunk_index,
            "complete": self.complete,
            "next_cursor": None if self.next_cursor is None else self.next_cursor.to_json(),
            "jsonl": self.jsonl,
        }


@dataclass(frozen=True)
class ReplayChunkResult:
    conversation_id: str | None
    replay_id: str
    frame: ReplayChunkFrame

    def to_json(self) -> ObjectMap:
        return {
            "conversation_id": self.conversation_id,
            "replay_id": self.replay_id,
            "frame": self.frame.to_json(),
        }


def coerce_request_id(value: object) -> RequestId:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int)) else None


def conversation_rpc_notification_method(evt_type: object) -> str | None:
    normalized = str(evt_type or "").strip().lower()
    if not normalized:
        return None
    return CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE.get(normalized)


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
    *,
    method: str,
    exc: HTTPException,
) -> JsonRpcErrorResponse:
    status_code = int(getattr(exc, "status_code", 500) or 500)
    detail = exc.detail
    message = detail if isinstance(detail, str) and detail.strip() else "Request failed"
    if status_code == 400:
        code = -32602
        error_code = "INVALID_REQUEST"
    elif status_code == 404:
        code = -32004
        error_code = "NOT_FOUND"
    elif status_code == 409:
        code = -32009
        error_code = "CONFLICT"
    else:
        code = -32603
        error_code = "INTERNAL_ERROR"
    return build_jsonrpc_error_response(
        request_id,
        code=code,
        message=message,
        data={
            "code": error_code,
            "status_code": status_code,
            "method": method,
        },
    )


def parse_conversations_rpc_request(payload: object) -> ParsedConversationsRpcRequest:
    request_id = coerce_request_id(payload.get("id") if isinstance(payload, dict) else None)
    if not isinstance(payload, dict):
        raise ConversationsRpcProtocolError(
            request_id,
            code=-32600,
            message="Invalid request",
            data={"code": "INVALID_REQUEST", "reason": "Payload must be an object"},
        )

    payload_map = coerce_object_map(payload)
    if payload_map.get("jsonrpc") != "2.0":
        raise ConversationsRpcProtocolError(
            request_id,
            code=-32600,
            message="Invalid request",
            data={"code": "INVALID_REQUEST", "reason": "jsonrpc must be '2.0'"},
        )

    method_value = payload_map.get("method")
    if not isinstance(method_value, str) or not method_value.strip():
        raise ConversationsRpcProtocolError(
            request_id,
            code=-32600,
            message="Invalid request",
            data={"code": "INVALID_REQUEST", "reason": "method is required"},
        )

    params_value = payload_map.get("params", {})
    if params_value is None:
        params_value = {}
    if not isinstance(params_value, dict):
        raise ConversationsRpcProtocolError(
            request_id,
            code=-32602,
            message="Invalid params",
            data={"code": "INVALID_REQUEST", "reason": "params must be an object"},
        )

    method = method_value.strip()
    if method == CONVERSATION_GET_METHOD:
        parsed_method: ConversationsRpcMethod = CONVERSATION_GET_METHOD
    elif method == CONVERSATION_LIST_METHOD:
        parsed_method = CONVERSATION_LIST_METHOD
    elif method == CONVERSATION_CREATE_METHOD:
        parsed_method = CONVERSATION_CREATE_METHOD
    elif method == CONVERSATION_SELECT_METHOD:
        parsed_method = CONVERSATION_SELECT_METHOD
    elif method == CONVERSATION_UPDATE_METHOD:
        parsed_method = CONVERSATION_UPDATE_METHOD
    elif method == CONVERSATION_DELETE_METHOD:
        parsed_method = CONVERSATION_DELETE_METHOD
    elif method == CONVERSATION_PINS_SET_METHOD:
        parsed_method = CONVERSATION_PINS_SET_METHOD
    elif method == CONVERSATION_DRAFT_SET_METHOD:
        parsed_method = CONVERSATION_DRAFT_SET_METHOD
    elif method == CONVERSATION_SEND_METHOD:
        parsed_method = CONVERSATION_SEND_METHOD
    elif method == CONVERSATION_INTERRUPT_METHOD:
        parsed_method = CONVERSATION_INTERRUPT_METHOD
    elif method == CONVERSATION_COMPACT_METHOD:
        parsed_method = CONVERSATION_COMPACT_METHOD
    elif method == CONVERSATION_REPLAY_GET_CHUNK_METHOD:
        parsed_method = CONVERSATION_REPLAY_GET_CHUNK_METHOD
    elif method == CONVERSATION_APPROVAL_RESPONSE_METHOD:
        parsed_method = CONVERSATION_APPROVAL_RESPONSE_METHOD
    elif method == CONVERSATION_SHELL_EXEC_METHOD:
        parsed_method = CONVERSATION_SHELL_EXEC_METHOD
    else:
        raise ConversationsRpcProtocolError(
            request_id,
            code=-32601,
            message="Method not found",
            data={"code": "NOT_FOUND", "method": method},
        )

    return ParsedConversationsRpcRequest(
        request_id=request_id,
        method=parsed_method,
        params=coerce_object_map(params_value),
    )


def parse_conversation_send_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationSendParams:
    conversation_id = _require_sanitized_conversation_id(
        params.get("conversation_id"),
        sanitize_conversation_id=sanitize_conversation_id,
        detail="conversation_id and text required",
    )
    text_value = params.get("text")
    text = text_value if isinstance(text_value, str) else ""
    if not text:
        raise HTTPException(status_code=400, detail="conversation_id and text required")
    return ConversationSendParams(conversation_id=conversation_id, text=text)


def parse_conversation_control_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationControlParams:
    conversation_id = _require_sanitized_conversation_id(
        params.get("conversation_id"),
        sanitize_conversation_id=sanitize_conversation_id,
        detail="Missing required field: conversation_id",
    )
    return ConversationControlParams(conversation_id=conversation_id)


def parse_conversation_get_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
    active_conversation_id: str | None,
) -> ConversationGetParams:
    conversation_id_raw = params.get("conversation_id")
    if isinstance(conversation_id_raw, str) and conversation_id_raw.strip():
        return ConversationGetParams(
            conversation_id=sanitize_conversation_id(conversation_id_raw.strip()),
        )
    if isinstance(active_conversation_id, str) and active_conversation_id.strip():
        return ConversationGetParams(
            conversation_id=sanitize_conversation_id(active_conversation_id.strip()),
        )
    return ConversationGetParams(conversation_id=None)


def parse_conversation_approval_response_params(
    params: ObjectMap,
) -> ConversationApprovalResponseParams:
    request_id_value = params.get("request_id", params.get("requestId", params.get("id")))
    request_id = str(request_id_value or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="Missing request_id")
    return ConversationApprovalResponseParams(payload=dict(params))


def parse_conversation_shell_exec_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationShellExecParams:
    conversation_id = _require_sanitized_conversation_id(
        params.get("conversation_id"),
        sanitize_conversation_id=sanitize_conversation_id,
        detail="Missing required field: conversation_id",
    )
    command = params.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=400, detail="No command provided")
    payload = dict(params)
    payload["conversation_id"] = conversation_id
    payload["command"] = command
    return ConversationShellExecParams(payload=payload)


def parse_conversation_create_params(params: ObjectMap) -> ConversationCreateParams:
    settings_value = params.get("settings")
    settings = coerce_object_map(settings_value) if isinstance(settings_value, dict) else None
    return ConversationCreateParams(settings=settings or None)


def parse_conversation_select_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationSelectParams:
    conversation_id = _require_sanitized_conversation_id(
        params.get("conversation_id", params.get("id")),
        sanitize_conversation_id=sanitize_conversation_id,
        detail="Missing conversation_id",
    )
    view_value = params.get("view")
    view: Literal["splash", "conversation"] | None = None
    if isinstance(view_value, str):
        normalized_view = view_value.strip().lower()
        if normalized_view in {"splash", "conversation"}:
            view = cast(Literal["splash", "conversation"], normalized_view)
    return ConversationSelectParams(conversation_id=conversation_id, view=view)


def parse_conversation_update_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationUpdateParams:
    conversation_id_raw = params.get("conversation_id")
    conversation_id = (
        sanitize_conversation_id(conversation_id_raw.strip())
        if isinstance(conversation_id_raw, str) and conversation_id_raw.strip()
        else None
    )
    settings_value = params.get("settings")
    settings = coerce_object_map(settings_value) if isinstance(settings_value, dict) else None
    thread_id_value = params.get("thread_id")
    thread_id = thread_id_value.strip() if isinstance(thread_id_value, str) and thread_id_value.strip() else None
    return ConversationUpdateParams(
        conversation_id=conversation_id,
        settings=settings,
        thread_id=thread_id,
    )


def parse_conversation_delete_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationDeleteParams:
    conversation_id = _require_sanitized_conversation_id(
        params.get("conversation_id"),
        sanitize_conversation_id=sanitize_conversation_id,
        detail="Missing conversation_id",
    )
    return ConversationDeleteParams(conversation_id=conversation_id)


def parse_conversation_pins_set_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationPinsSetParams:
    requested = params.get("pinned_conversations")
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="pinned_conversations must be a list")
    pinned: list[str] = []
    for item in requested:
        if not isinstance(item, str) or not item.strip():
            continue
        convo_id = sanitize_conversation_id(item.strip())
        if convo_id and convo_id not in pinned:
            pinned.append(convo_id)
    return ConversationPinsSetParams(pinned_conversations=pinned)


def parse_conversation_draft_set_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
) -> ConversationDraftSetParams:
    conversation_id_raw = params.get("conversation_id")
    conversation_id = (
        sanitize_conversation_id(conversation_id_raw.strip())
        if isinstance(conversation_id_raw, str) and conversation_id_raw.strip()
        else None
    )
    draft_value = params.get("draft", "")
    draft = draft_value if isinstance(draft_value, str) else ""
    return ConversationDraftSetParams(conversation_id=conversation_id, draft=draft)


def parse_conversation_replay_get_chunk_params(
    params: ObjectMap,
    *,
    sanitize_conversation_id: SanitizeConversationId,
    active_conversation_id: str | None,
) -> ConversationReplayGetChunkParams:
    conversation_id_raw = params.get("conversation_id")
    conversation_id = (
        sanitize_conversation_id(conversation_id_raw.strip())
        if isinstance(conversation_id_raw, str) and conversation_id_raw.strip()
        else None
    )
    if conversation_id is None and isinstance(active_conversation_id, str) and active_conversation_id.strip():
        conversation_id = sanitize_conversation_id(active_conversation_id.strip())

    cursor_value = params.get("cursor") or {}
    if not isinstance(cursor_value, dict):
        raise HTTPException(status_code=400, detail="cursor must be an object")
    cursor_map = coerce_object_map(cursor_value)
    offset = _parse_int(cursor_map.get("offset", 0), detail="cursor.offset must be an integer")
    max_entries = min(max(_parse_int(params.get("max_entries", 500), detail="max_entries must be an integer"), 1), 500)
    max_bytes = max(_parse_int(params.get("max_bytes", 524288), detail="max_bytes must be an integer"), 1)

    format_name = str(params.get("format", "jsonl") or "jsonl").strip().lower()
    if format_name != "jsonl":
        raise HTTPException(status_code=400, detail="Only format=jsonl is supported")

    return ConversationReplayGetChunkParams(
        conversation_id=conversation_id,
        cursor=ReplayChunkCursor(offset=offset),
        max_entries=max_entries,
        max_bytes=max_bytes,
        include_internal=(params.get("include_internal") is True),
    )


def normalize_conversation_send_result(
    result: object,
    *,
    conversation_id: str,
) -> ConversationSendResult:
    payload = (
        coerce_object_map(result)
        if isinstance(result, dict)
        else {"ok": False, "error": "Invalid send result"}
    )
    return ConversationSendResult(
        conversation_id=conversation_id,
        accepted=(payload.get("ok") is True),
        payload=payload,
    )


def normalize_conversation_control_result(
    result: object,
    *,
    invalid_error: str,
) -> ConversationControlResult:
    payload = (
        coerce_object_map(result)
        if isinstance(result, dict)
        else {"ok": False, "error": invalid_error}
    )
    error_value = payload.get("error")
    return ConversationControlResult(
        ok=(payload.get("ok") is True),
        error=error_value if isinstance(error_value, str) else None,
        payload=payload,
    )


def normalize_replay_range_data(range_data: object) -> ReplayRangeData:
    range_map = coerce_object_map(range_data)
    conversation_id_value = range_map.get("conversation_id")
    return ReplayRangeData(
        conversation_id=conversation_id_value if isinstance(conversation_id_value, str) else None,
        items=coerce_object_list(range_map.get("items")),
        offset=_parse_int(range_map.get("offset"), detail="Invalid replay offset", default=0),
        total_count=_parse_int(range_map.get("total"), detail="Invalid replay total", default=0),
    )


def encode_replay_items_jsonl(
    items: ObjectList,
    *,
    max_bytes: int,
) -> ReplayJsonlChunk:
    jsonl_parts: list[str] = []
    kept_item_count = 0
    encoded_bytes = 0
    for item in items:
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        line_size = len(line.encode("utf-8"))
        if kept_item_count > 0 and encoded_bytes + line_size > max_bytes:
            break
        jsonl_parts.append(line)
        kept_item_count += 1
        encoded_bytes += line_size

    if not jsonl_parts and items:
        jsonl_parts.append(json.dumps(items[0], ensure_ascii=False, separators=(",", ":")) + "\n")
        kept_item_count = 1

    return ReplayJsonlChunk(
        jsonl="".join(jsonl_parts),
        item_count=kept_item_count,
    )


def build_empty_replay_chunk_result(
    *,
    conversation_id: str | None,
) -> ReplayChunkResult:
    return ReplayChunkResult(
        conversation_id=conversation_id,
        replay_id=_next_replay_id(),
        frame=ReplayChunkFrame(
            format_name="jsonl",
            offset=0,
            item_count=0,
            total_count=0,
            chunk_index=0,
            complete=True,
            next_cursor=None,
            jsonl="",
        ),
    )


def build_replay_chunk_result(
    *,
    params: ConversationReplayGetChunkParams,
    range_data: ReplayRangeData,
    jsonl_chunk: ReplayJsonlChunk,
) -> ReplayChunkResult:
    next_offset = range_data.offset + jsonl_chunk.item_count
    complete = next_offset >= range_data.total_count
    return ReplayChunkResult(
        conversation_id=range_data.conversation_id,
        replay_id=_next_replay_id(),
        frame=ReplayChunkFrame(
            format_name=params.format_name,
            offset=range_data.offset,
            item_count=jsonl_chunk.item_count,
            total_count=range_data.total_count,
            chunk_index=range_data.offset // max(params.max_entries, 1),
            complete=complete,
            next_cursor=None if complete else ReplayChunkCursor(offset=next_offset),
            jsonl=jsonl_chunk.jsonl,
        ),
    )


def _next_replay_id() -> str:
    return f"replay_{uuid.uuid4().hex[:12]}"


def _require_sanitized_conversation_id(
    value: object,
    *,
    sanitize_conversation_id: SanitizeConversationId,
    detail: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=detail)
    return sanitize_conversation_id(value.strip())


def _parse_int(
    value: object,
    *,
    detail: str,
    default: int | None = None,
) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool):
        raise HTTPException(status_code=400, detail=detail)
    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str):
        try:
            return int(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=detail) from exc
    raise HTTPException(status_code=400, detail=detail)

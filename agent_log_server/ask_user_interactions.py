from __future__ import annotations

import sys
from typing import TypedDict

from .typing_helpers import ObjectMap, RequestId, coerce_object_map

AGENT_PTY_ASK_USER_REQUEST_METHOD = "agent-pty/ask-user"
AGENT_PTY_ASK_USER_SERVER = "agent-pty-blocks"
AGENT_PTY_ASK_USER_TOOL = "ask_user"

from collections.abc import Awaitable, Callable


class NormalizedRequest(TypedDict):
    question: str
    choices: list[str]
    allow_freeform: bool


PendingApprovalMatch = tuple[str, ObjectMap]
PendingApprovalItem = tuple[str, str, ObjectMap]

EmitIpcFn = Callable[[str, ObjectMap, str | None], Awaitable[None]]
FindPendingApprovalFn = Callable[[str], object]
ListPendingApprovalsFn = Callable[..., object]
RecordSubmittedResolutionFn = Callable[[str, str, ObjectMap], object]
RemovePendingApprovalFn = Callable[[str, RequestId], bool]
BuildHandoffEventFn = Callable[[str, ObjectMap, ObjectMap], object]
AppendHandoffFn = Callable[[str, ObjectMap], Awaitable[ObjectMap | None]]
BroadcastUiFn = Callable[[ObjectMap], Awaitable[None]]

_emit_ipc_fn: EmitIpcFn | None = None
_find_pending_approval_fn: FindPendingApprovalFn | None = None
_list_pending_approvals_fn: ListPendingApprovalsFn | None = None
_record_submitted_resolution_fn: RecordSubmittedResolutionFn | None = None
_remove_pending_approval_fn: RemovePendingApprovalFn | None = None
_build_handoff_event_fn: BuildHandoffEventFn | None = None
_append_handoff_fn: AppendHandoffFn | None = None
_broadcast_ui_fn: BroadcastUiFn | None = None


def _ask_user_log(message: str) -> None:
    print(f"[ask_user_interactions] {message}", file=sys.stderr, flush=True)


def _coerce_pending_match(value: object) -> PendingApprovalMatch | None:
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    conversation_id_obj, descriptor_obj = value
    conversation_id = str(conversation_id_obj or "").strip()
    if not conversation_id or not isinstance(descriptor_obj, dict):
        return None
    return conversation_id, coerce_object_map(descriptor_obj)


def _coerce_pending_approval_list(value: object) -> list[PendingApprovalItem]:
    if not isinstance(value, list):
        return []
    approvals: list[PendingApprovalItem] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        conversation_id_obj, request_id_obj, descriptor_obj = item
        conversation_id = str(conversation_id_obj or "").strip()
        request_id = str(request_id_obj or "").strip()
        if not conversation_id or not request_id or not isinstance(descriptor_obj, dict):
            continue
        approvals.append((conversation_id, request_id, coerce_object_map(descriptor_obj)))
    return approvals


def _submitted_resolution(descriptor: ObjectMap) -> ObjectMap | None:
    submitted_obj = descriptor.get("submitted_resolution")
    if not isinstance(submitted_obj, dict):
        return None
    submitted = coerce_object_map(submitted_obj)
    return submitted if submitted else None


def _merge_recorded_handoff_entry(
    handoff_event: ObjectMap,
    recorded_handoff_entry: ObjectMap | None,
) -> ObjectMap:
    if not isinstance(recorded_handoff_entry, dict):
        return handoff_event
    merged = dict(handoff_event)
    for key in ("nid", "card_id", "order_id"):
        if key in recorded_handoff_entry:
            merged[key] = recorded_handoff_entry[key]
    return merged

def configure(
    *,
    emit_ipc_fn: EmitIpcFn,
    find_pending_approval_fn: FindPendingApprovalFn,
    list_pending_approvals_fn: ListPendingApprovalsFn,
    record_submitted_resolution_fn: RecordSubmittedResolutionFn,
    remove_pending_approval_fn: RemovePendingApprovalFn,
    build_handoff_event_fn: BuildHandoffEventFn,
    append_handoff_fn: AppendHandoffFn,
    broadcast_ui_fn: BroadcastUiFn,
) -> None:
    global _emit_ipc_fn
    global _find_pending_approval_fn
    global _list_pending_approvals_fn
    global _record_submitted_resolution_fn
    global _remove_pending_approval_fn
    global _build_handoff_event_fn
    global _append_handoff_fn
    global _broadcast_ui_fn
    _emit_ipc_fn = emit_ipc_fn
    _find_pending_approval_fn = find_pending_approval_fn
    _list_pending_approvals_fn = list_pending_approvals_fn
    _record_submitted_resolution_fn = record_submitted_resolution_fn
    _remove_pending_approval_fn = remove_pending_approval_fn
    _build_handoff_event_fn = build_handoff_event_fn
    _append_handoff_fn = append_handoff_fn
    _broadcast_ui_fn = broadcast_ui_fn


def normalize_choices(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        choice = item.strip()
        if not choice or choice in seen:
            continue
        normalized.append(choice)
        seen.add(choice)
    return normalized


def normalize_request(question: object, choices: object, allow_freeform: object) -> NormalizedRequest:
    return {
        "question": str(question or "").strip(),
        "choices": normalize_choices(choices),
        "allow_freeform": bool(allow_freeform),
    }



def is_agent_pty_ask_user_tool(server_name: object, tool_name: object) -> bool:
    return (
        str(server_name or "").strip() == AGENT_PTY_ASK_USER_SERVER
        and str(tool_name or "").strip() == AGENT_PTY_ASK_USER_TOOL
    )


def is_agent_pty_ask_user_request(tool_name: object, arguments: object) -> bool:
    if str(tool_name or "").strip() != AGENT_PTY_ASK_USER_TOOL:
        return False
    if not isinstance(arguments, dict):
        return False
    arguments_map = coerce_object_map(arguments)
    normalized = normalize_request(
        arguments_map.get("question"),
        arguments_map.get("choices"),
        arguments_map.get("allow_freeform", arguments_map.get("allowFreeform", True)),
    )
    return bool(normalized["question"] and (normalized["choices"] or normalized["allow_freeform"]))


def has_request(request_id: object) -> bool:
    request_id_text = str(request_id or "").strip()
    if not request_id_text or _find_pending_approval_fn is None:
        return False
    return _coerce_pending_match(_find_pending_approval_fn(request_id_text)) is not None


def conversation_id_for_request(request_id: object) -> str | None:
    request_id_text = str(request_id or "").strip()
    if not request_id_text or _find_pending_approval_fn is None:
        return None
    found = _coerce_pending_match(_find_pending_approval_fn(request_id_text))
    if found is None:
        return None
    conversation_id, _descriptor = found
    return conversation_id or None


def _normalize_resolution(resolution: object) -> ObjectMap:
    if isinstance(resolution, dict):
        result = resolution.get("result")
        if isinstance(result, dict):
            return coerce_object_map(result)
        return coerce_object_map(resolution)
    return {}


def _resolution_prefers_terminal_state(resolution: ObjectMap) -> bool:
    action = str(resolution.get("action") or resolution.get("status") or "").strip().lower()
    if action in {"cancel", "cancelled", "decline", "declined", "failed", "error", "interrupted"}:
        return True
    if resolution.get("accepted") is False:
        return True
    if resolution.get("success") is False:
        return True
    if resolution.get("error") not in (None, "", {}):
        return True
    return False


def _terminal_resolution(resolution: object) -> ObjectMap:
    normalized = _normalize_resolution(resolution)
    if normalized:
        return normalized
    return {"action": "cancel"}


def _ipc_terminal_status(resolution: ObjectMap) -> str:
    status = str(resolution.get("status") or resolution.get("action") or "").strip().lower()
    if status == "interrupted":
        return "interrupted"
    if status in {"cancel", "cancelled", "decline", "declined"}:
        return "cancel"
    if status in {"error", "failed"}:
        return "error"
    if resolution.get("error") not in (None, "", {}):
        return "error"
    return "cancel"


async def emit_response(request_id: object, resolution: object, *, sid: str | None = None) -> bool:
    request_id_text = str(request_id or "").strip()
    if not request_id_text or _emit_ipc_fn is None:
        _ask_user_log(
            f"emit_response skipped request_id={request_id_text or '-'} has_emit={_emit_ipc_fn is not None}"
        )
        return False
    _ask_user_log(
        f"emit_response request_id={request_id_text} sid={sid or '-'} response={_normalize_resolution(resolution)!r}"
    )
    await _emit_ipc_fn(
        "ask_user_response",
        {
            "request_id": request_id_text,
            "response": _normalize_resolution(resolution),
        },
        sid,
    )
    return True


async def emit_terminal(
    request_id: object,
    status: object,
    *,
    error: object = None,
    sid: str | None = None,
) -> bool:
    request_id_text = str(request_id or "").strip()
    status_text = str(status or "").strip().lower()
    if not request_id_text or not status_text or _emit_ipc_fn is None:
        _ask_user_log(
            f"emit_terminal skipped request_id={request_id_text or '-'} status={status_text or '-'} has_emit={_emit_ipc_fn is not None}"
        )
        return False
    payload: ObjectMap = {
        "request_id": request_id_text,
        "status": status_text,
    }
    if error not in (None, "", {}):
        payload["error"] = str(error)
    await _emit_ipc_fn("ask_user_terminal", payload, sid)
    _ask_user_log(
        f"emit_terminal request_id={request_id_text} status={status_text} sid={sid or '-'} error={payload.get('error', '')!r}"
    )
    return True


async def acknowledge_interaction(request_id: object) -> bool:
    request_id_text = str(request_id or "").strip()
    _ask_user_log(f"acknowledge request_id={request_id_text or '-'}")
    if (
        not request_id_text
        or _find_pending_approval_fn is None
        or _remove_pending_approval_fn is None
    ):
        _ask_user_log(
            f"acknowledge unavailable request_id={request_id_text or '-'} configured={_find_pending_approval_fn is not None and _remove_pending_approval_fn is not None}"
        )
        return False
    found = _coerce_pending_match(_find_pending_approval_fn(request_id_text))
    if found is None:
        _ask_user_log(f"acknowledge missing_pending request_id={request_id_text}")
        return False
    conversation_id, descriptor = found
    submitted = _submitted_resolution(descriptor)
    _remove_pending_approval_fn(conversation_id, request_id_text)
    if (
        submitted is not None
        and _build_handoff_event_fn is not None
        and _append_handoff_fn is not None
        and _broadcast_ui_fn is not None
    ):
        handoff_event_obj = _build_handoff_event_fn(conversation_id, descriptor, submitted)
        if isinstance(handoff_event_obj, dict):
            handoff_event = coerce_object_map(handoff_event_obj)
            recorded_handoff_entry = await _append_handoff_fn(conversation_id, handoff_event)
            await _broadcast_ui_fn(
                _merge_recorded_handoff_entry(handoff_event, recorded_handoff_entry),
            )
    _ask_user_log(f"acknowledge cleared request_id={request_id_text} conversation_id={conversation_id}")
    return True


async def submit_user_response(request_id: object, resolution: object) -> ObjectMap:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        _ask_user_log("submit missing request_id")
        return {"ok": False, "error": "request_id is required"}
    if _find_pending_approval_fn is None or _record_submitted_resolution_fn is None:
        _ask_user_log(
            f"submit unavailable request_id={request_id_text} configured={_find_pending_approval_fn is not None and _record_submitted_resolution_fn is not None}"
        )
        return {"ok": False, "error": "ask_user interactions are not configured"}
    found = _coerce_pending_match(_find_pending_approval_fn(request_id_text))
    if found is None:
        _ask_user_log(f"submit missing_pending request_id={request_id_text}")
        return {"ok": False, "error": "interaction is no longer pending"}
    conversation_id, _descriptor = found
    normalized_resolution = _normalize_resolution(resolution)
    _ask_user_log(
        f"submit matched request_id={request_id_text} conversation_id={conversation_id} response={normalized_resolution!r}"
    )
    _record_submitted_resolution_fn(conversation_id, request_id_text, normalized_resolution)
    if not await emit_response(request_id_text, normalized_resolution):
        _ask_user_log(f"submit emit_failed request_id={request_id_text}")
        return {"ok": False, "error": "failed to emit ask_user response"}
    _ask_user_log(f"submit emitted request_id={request_id_text}")
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "request_id": request_id_text,
        "awaiting_harness_ack": True,
    }


async def finalize_interaction(request_id: object, resolution: object = None) -> bool:
    request_id_text = str(request_id or "").strip()
    if (
        not request_id_text
        or _find_pending_approval_fn is None
        or _remove_pending_approval_fn is None
    ):
        return False
    found = _coerce_pending_match(_find_pending_approval_fn(request_id_text))
    if found is None:
        return False
    conversation_id, descriptor = found

    terminal = _terminal_resolution(resolution)
    submitted = _submitted_resolution(descriptor)
    final_resolution: ObjectMap
    if _resolution_prefers_terminal_state(terminal):
        await emit_terminal(
            request_id_text,
            _ipc_terminal_status(terminal),
            error=terminal.get("error"),
        )
        final_resolution = {"action": "cancel"}
        if terminal.get("error") not in (None, "", {}):
            final_resolution["error"] = terminal.get("error")
    else:
        final_resolution = dict(submitted) if submitted else terminal

    _remove_pending_approval_fn(conversation_id, request_id_text)
    if (
        _build_handoff_event_fn is not None
        and _append_handoff_fn is not None
        and _broadcast_ui_fn is not None
    ):
        handoff_event_obj = _build_handoff_event_fn(conversation_id, descriptor, final_resolution)
        if isinstance(handoff_event_obj, dict):
            handoff_event = coerce_object_map(handoff_event_obj)
            recorded_handoff_entry = await _append_handoff_fn(conversation_id, handoff_event)
            await _broadcast_ui_fn(
                _merge_recorded_handoff_entry(handoff_event, recorded_handoff_entry),
            )
    return True


async def cancel_interactions(
    *,
    conversation_id: object = None,
    turn_id: object = None,
    request_id: object = None,
    resolution: object = None,
) -> dict[str, int]:
    if _list_pending_approvals_fn is None:
        return {"cancelled": 0}
    terminal = _terminal_resolution(resolution)
    matching = _coerce_pending_approval_list(
        _list_pending_approvals_fn(
            request_method=AGENT_PTY_ASK_USER_REQUEST_METHOD,
            conversation_id=conversation_id,
            turn_id=turn_id,
            request_id=request_id,
        )
    )
    finalized = 0
    seen: set[str] = set()
    for _conversation_id, candidate_request_id, _descriptor in matching:
        if not candidate_request_id or candidate_request_id in seen:
            continue
        seen.add(candidate_request_id)
        finalized += 1 if await finalize_interaction(candidate_request_id, terminal) else 0
    return {"cancelled": finalized}

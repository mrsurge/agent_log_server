from __future__ import annotations

import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

AGENT_PTY_ASK_USER_REQUEST_METHOD = "agent-pty/ask-user"
AGENT_PTY_ASK_USER_SERVER = "agent-pty-blocks"
AGENT_PTY_ASK_USER_TOOL = "ask_user"

EmitIpcFn = Callable[[str, Dict[str, Any], Optional[str]], Awaitable[None]]
FindPendingApprovalFn = Callable[[str], Optional[Tuple[str, Dict[str, Any]]]]
ListPendingApprovalsFn = Callable[..., List[Tuple[str, str, Dict[str, Any]]]]
RecordSubmittedResolutionFn = Callable[[str, str, Dict[str, Any]], Optional[Dict[str, Any]]]
RemovePendingApprovalFn = Callable[[str, Any], bool]
BuildHandoffEventFn = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]
AppendHandoffFn = Callable[[str, Dict[str, Any]], Awaitable[None]]
BroadcastUiFn = Callable[[Dict[str, Any]], Awaitable[None]]

_emit_ipc_fn: Optional[EmitIpcFn] = None
_find_pending_approval_fn: Optional[FindPendingApprovalFn] = None
_list_pending_approvals_fn: Optional[ListPendingApprovalsFn] = None
_record_submitted_resolution_fn: Optional[RecordSubmittedResolutionFn] = None
_remove_pending_approval_fn: Optional[RemovePendingApprovalFn] = None
_build_handoff_event_fn: Optional[BuildHandoffEventFn] = None
_append_handoff_fn: Optional[AppendHandoffFn] = None
_broadcast_ui_fn: Optional[BroadcastUiFn] = None


def _ask_user_log(message: str) -> None:
    print(f"[ask_user_interactions] {message}", file=sys.stderr, flush=True)

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


def normalize_choices(value: Any) -> list[str]:
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


def normalize_request(question: Any, choices: Any, allow_freeform: Any) -> Dict[str, Any]:
    return {
        "question": str(question or "").strip(),
        "choices": normalize_choices(choices),
        "allow_freeform": bool(allow_freeform),
    }



def is_agent_pty_ask_user_tool(server_name: Any, tool_name: Any) -> bool:
    return (
        str(server_name or "").strip() == AGENT_PTY_ASK_USER_SERVER
        and str(tool_name or "").strip() == AGENT_PTY_ASK_USER_TOOL
    )


def is_agent_pty_ask_user_request(tool_name: Any, arguments: Any) -> bool:
    if str(tool_name or "").strip() != AGENT_PTY_ASK_USER_TOOL:
        return False
    if not isinstance(arguments, dict):
        return False
    normalized = normalize_request(
        arguments.get("question"),
        arguments.get("choices"),
        arguments.get("allow_freeform", arguments.get("allowFreeform", True)),
    )
    return bool(normalized["question"] and (normalized["choices"] or normalized["allow_freeform"]))


def has_request(request_id: Any) -> bool:
    request_id_text = str(request_id or "").strip()
    return bool(
        request_id_text
        and _find_pending_approval_fn is not None
        and _find_pending_approval_fn(request_id_text)
    )


def conversation_id_for_request(request_id: Any) -> Optional[str]:
    request_id_text = str(request_id or "").strip()
    if not request_id_text or _find_pending_approval_fn is None:
        return None
    found = _find_pending_approval_fn(request_id_text)
    if not isinstance(found, tuple) or len(found) != 2:
        return None
    conversation_id = str(found[0] or "").strip()
    return conversation_id or None


def _normalize_resolution(resolution: Any) -> Dict[str, Any]:
    if isinstance(resolution, dict):
        if isinstance(resolution.get("result"), dict):
            return dict(resolution["result"])
        return dict(resolution)
    return {}


def _resolution_prefers_terminal_state(resolution: Dict[str, Any]) -> bool:
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


def _terminal_resolution(resolution: Any) -> Dict[str, Any]:
    normalized = _normalize_resolution(resolution)
    if normalized:
        return normalized
    return {"action": "cancel"}


def _ipc_terminal_status(resolution: Dict[str, Any]) -> str:
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


async def emit_response(request_id: Any, resolution: Any, *, sid: Optional[str] = None) -> bool:
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
    request_id: Any,
    status: Any,
    *,
    error: Any = None,
    sid: Optional[str] = None,
) -> bool:
    request_id_text = str(request_id or "").strip()
    status_text = str(status or "").strip().lower()
    if not request_id_text or not status_text or _emit_ipc_fn is None:
        _ask_user_log(
            f"emit_terminal skipped request_id={request_id_text or '-'} status={status_text or '-'} has_emit={_emit_ipc_fn is not None}"
        )
        return False
    payload: Dict[str, Any] = {
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


async def acknowledge_interaction(request_id: Any) -> bool:
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
    found = _find_pending_approval_fn(request_id_text)
    if not isinstance(found, tuple) or len(found) != 2:
        _ask_user_log(f"acknowledge missing_pending request_id={request_id_text}")
        return False
    conversation_id, descriptor = found
    if not isinstance(descriptor, dict):
        return False
    submitted = descriptor.get("submitted_resolution") if isinstance(descriptor.get("submitted_resolution"), dict) else {}
    _remove_pending_approval_fn(conversation_id, request_id_text)
    if (
        submitted
        and _build_handoff_event_fn is not None
        and _append_handoff_fn is not None
        and _broadcast_ui_fn is not None
    ):
        handoff_event = _build_handoff_event_fn(conversation_id, descriptor, dict(submitted))
        if isinstance(handoff_event, dict):
            await _append_handoff_fn(conversation_id, handoff_event)
            await _broadcast_ui_fn(handoff_event)
    _ask_user_log(f"acknowledge cleared request_id={request_id_text} conversation_id={conversation_id}")
    return True


async def submit_user_response(request_id: Any, resolution: Any) -> Dict[str, Any]:
    request_id_text = str(request_id or "").strip()
    if not request_id_text:
        _ask_user_log("submit missing request_id")
        return {"ok": False, "error": "request_id is required"}
    if _find_pending_approval_fn is None or _record_submitted_resolution_fn is None:
        _ask_user_log(
            f"submit unavailable request_id={request_id_text} configured={_find_pending_approval_fn is not None and _record_submitted_resolution_fn is not None}"
        )
        return {"ok": False, "error": "ask_user interactions are not configured"}
    found = _find_pending_approval_fn(request_id_text)
    if not isinstance(found, tuple) or len(found) != 2:
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


async def finalize_interaction(request_id: Any, resolution: Any = None) -> bool:
    request_id_text = str(request_id or "").strip()
    if (
        not request_id_text
        or _find_pending_approval_fn is None
        or _remove_pending_approval_fn is None
    ):
        return False
    found = _find_pending_approval_fn(request_id_text)
    if not isinstance(found, tuple) or len(found) != 2:
        return False
    conversation_id, descriptor = found
    if not isinstance(descriptor, dict):
        return False

    terminal = _terminal_resolution(resolution)
    submitted = descriptor.get("submitted_resolution") if isinstance(descriptor.get("submitted_resolution"), dict) else None
    final_resolution: Dict[str, Any]
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
        handoff_event = _build_handoff_event_fn(conversation_id, descriptor, final_resolution)
        if isinstance(handoff_event, dict):
            await _append_handoff_fn(conversation_id, handoff_event)
            await _broadcast_ui_fn(handoff_event)
    return True


async def cancel_interactions(
    *,
    conversation_id: Any = None,
    turn_id: Any = None,
    request_id: Any = None,
    resolution: Any = None,
) -> Dict[str, int]:
    if _list_pending_approvals_fn is None:
        return {"cancelled": 0}
    terminal = _terminal_resolution(resolution)
    matching = _list_pending_approvals_fn(
        request_method=AGENT_PTY_ASK_USER_REQUEST_METHOD,
        conversation_id=conversation_id,
        turn_id=turn_id,
        request_id=request_id,
    )
    finalized = 0
    seen: set[str] = set()
    for _conversation_id, candidate_request_id, _descriptor in matching:
        candidate_request_id = str(candidate_request_id or "").strip()
        if not candidate_request_id or candidate_request_id in seen:
            continue
        seen.add(candidate_request_id)
        finalized += 1 if await finalize_interaction(candidate_request_id, terminal) else 0
    return {"cancelled": finalized}

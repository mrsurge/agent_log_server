from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

TRANSCRIPT_CARD_NID = "conversation.transcript"
_STATE_ONLY_ROLES = {"mode", "status", "token_usage"}
_LIVE_EVENT_CARD_FAMILIES = {
    "agent_block_begin": "agent_pty",
    "agent_block_delta": "agent_pty",
    "agent_block_end": "agent_pty",
    "approval": "approval",
    "approval_handoff": "approval",
    "assistant_delta": "assistant",
    "assistant_end": "assistant",
    "assistant_finalize": "assistant",
    "command_result": "command",
    "context_compacted": "context_compacted",
    "diff": "diff",
    "error": "error",
    "plan": "plan",
    "reasoning_delta": "reasoning",
    "reasoning_end": "reasoning",
    "reasoning_finalize": "reasoning",
    "screen_delta": "agent_pty",
    "search": "search",
    "shell_begin": "command",
    "shell_delta": "command",
    "shell_end": "command",
    "subagent_end": "subagent_end",
    "subagent_start": "subagent_start",
    "tool_begin": "tool",
    "tool_delta": "tool",
    "tool_end": "tool",
    "tool_interaction": "tool",
    "view": "view",
    "web_search": "web_search",
}
_ROLE_CARD_FAMILY_ALIASES = {
    "mcp_tool": "tool",
}


def _clean_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_order_id(value: object, *, allow_unresolved: bool = False) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value >= 0:
            return value
        return -1 if allow_unresolved and value == -1 else None
    if isinstance(value, float) and value.is_integer():
        parsed = int(value)
        if parsed >= 0:
            return parsed
        return -1 if allow_unresolved and parsed == -1 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        if parsed >= 0:
            return parsed
        return -1 if allow_unresolved and parsed == -1 else None
    return None


def transcript_card_family(record: Mapping[str, object]) -> Optional[str]:
    role = _clean_str(record.get("role"))
    if role:
        normalized_role = _ROLE_CARD_FAMILY_ALIASES.get(role.lower(), role.lower())
        if normalized_role in _STATE_ONLY_ROLES:
            return None
        return normalized_role
    event_type = _clean_str(record.get("type"))
    if not event_type:
        return None
    normalized_event_type = event_type.lower()
    if normalized_event_type == "message":
        message_role = _clean_str(record.get("role"))
        if message_role:
            return transcript_card_family({"role": message_role})
        return "message"
    return _LIVE_EVENT_CARD_FAMILIES.get(normalized_event_type)


def is_visible_transcript_card_record(record: Mapping[str, object]) -> bool:
    return transcript_card_family(record) is not None


def derive_transcript_card_id(
    record: Mapping[str, object],
    *,
    fallback_order_id: int | None = None,
) -> Optional[str]:
    for key in (
        "card_id",
        "cardId",
        "id",
        "item_id",
        "itemId",
        "request_id",
        "requestId",
        "call_id",
        "callId",
        "block_id",
        "blockId",
    ):
        value = _clean_str(record.get(key))
        if value:
            return value
    role = _clean_str(record.get("role")) or "card"
    turn_id = _clean_str(record.get("turn_id")) or _clean_str(record.get("turnId"))
    if fallback_order_id is not None:
        return f"{role}:{fallback_order_id}"
    if turn_id:
        return f"{role}:{turn_id}"
    return role


def transcript_card_id(
    record: Mapping[str, object],
    *,
    fallback_order_id: int | None = None,
) -> Optional[str]:
    family = transcript_card_family(record)
    augmented_record = dict(record)
    if family and _clean_str(augmented_record.get("role")) is None:
        augmented_record["role"] = family
    return derive_transcript_card_id(
        augmented_record,
        fallback_order_id=fallback_order_id,
    )


def normalize_transcript_card_record(
    record: Mapping[str, object],
    *,
    conversation_id: str | None = None,
    fallback_order_id: int | None = None,
) -> dict[str, object]:
    normalized = {str(key): value for key, value in record.items()}
    if conversation_id and _clean_str(normalized.get("conversation_id")) is None:
        normalized["conversation_id"] = conversation_id

    order_id = _coerce_order_id(normalized.get("order_id"))
    if order_id is None:
        order_id = _coerce_order_id(normalized.get("orderId"))
    if order_id is None and fallback_order_id is not None:
        order_id = fallback_order_id
    if order_id is not None:
        normalized["order_id"] = order_id

    if is_visible_transcript_card_record(normalized):
        if _clean_str(normalized.get("nid")) is None:
            normalized["nid"] = TRANSCRIPT_CARD_NID
        if _clean_str(normalized.get("card_id")) is None:
            card_id = transcript_card_id(normalized, fallback_order_id=order_id)
            if card_id:
                normalized["card_id"] = card_id
    return normalized


def normalize_live_transcript_event(
    event: Mapping[str, object],
    *,
    conversation_id: str | None = None,
    fallback_order_id: int | None = None,
) -> dict[str, object]:
    normalized = {str(key): value for key, value in event.items()}
    if conversation_id and _clean_str(normalized.get("conversation_id")) is None:
        normalized["conversation_id"] = conversation_id
    if not is_visible_transcript_card_record(normalized):
        return normalized
    order_id = _coerce_order_id(normalized.get("order_id"), allow_unresolved=True)
    if order_id is None:
        order_id = _coerce_order_id(normalized.get("orderId"), allow_unresolved=True)
    if order_id is None and fallback_order_id is not None:
        order_id = fallback_order_id
    if order_id is not None:
        normalized["order_id"] = order_id
    if _clean_str(normalized.get("nid")) is None:
        normalized["nid"] = TRANSCRIPT_CARD_NID
    if _clean_str(normalized.get("card_id")) is None:
        card_id = transcript_card_id(normalized, fallback_order_id=order_id)
        if card_id:
            normalized["card_id"] = card_id
    return normalized


def transcript_card_reservation_key(record: Mapping[str, object]) -> Optional[str]:
    family = transcript_card_family(record)
    if not family:
        return None
    card_id = transcript_card_id(record)
    if not card_id:
        return None
    return f"{family}:{card_id}"


def transcript_order_id(value: object) -> Optional[int]:
    return _coerce_order_id(value)

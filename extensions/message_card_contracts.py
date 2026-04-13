from __future__ import annotations

from typing import Optional, TypeAlias

EntryMap: TypeAlias = dict[str, object]


def _clean_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _apply_common_fields(
    entry: EntryMap,
    *,
    entry_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    if entry_id:
        entry["id"] = entry_id
    if conversation_id:
        entry["conversation_id"] = conversation_id
    if turn_id:
        entry["turn_id"] = turn_id
    if subagent_id:
        entry["subagent_id"] = subagent_id
    return entry


def build_message_event(
    *,
    role: str,
    text: str,
    entry_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "message",
        "role": role,
        "text": text,
    }
    return _apply_common_fields(
        event,
        entry_id=_clean_str(entry_id),
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_message_transcript_entry(
    *,
    role: str,
    text: str,
    timestamp: Optional[str] = None,
    entry_id: Optional[str] = None,
    item_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    event: Optional[str] = None,
) -> EntryMap:
    record: EntryMap = {
        "role": role,
        "text": text,
    }
    normalized_id = _clean_str(entry_id)
    normalized_item_id = _clean_str(item_id)
    if normalized_id:
        record["id"] = normalized_id
    if normalized_item_id:
        record["item_id"] = normalized_item_id
    elif normalized_id:
        record["item_id"] = normalized_id
    if timestamp:
        record["timestamp"] = timestamp
    if event:
        record["event"] = event
    return _apply_common_fields(
        record,
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_assistant_delta_event(
    *,
    entry_id: str,
    delta: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "assistant_delta",
        "delta": delta,
    }
    return _apply_common_fields(
        event,
        entry_id=_clean_str(entry_id),
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_assistant_finalize_event(
    *,
    entry_id: str,
    text: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "assistant_finalize",
        "text": text,
    }
    return _apply_common_fields(
        event,
        entry_id=_clean_str(entry_id),
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_reasoning_delta_event(
    *,
    entry_id: str,
    delta: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "reasoning_delta",
        "delta": delta,
    }
    return _apply_common_fields(
        event,
        entry_id=_clean_str(entry_id),
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_reasoning_finalize_event(
    *,
    entry_id: str,
    text: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "reasoning_finalize",
        "text": text,
    }
    return _apply_common_fields(
        event,
        entry_id=_clean_str(entry_id),
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )


def build_reasoning_transcript_entry(
    *,
    text: str,
    timestamp: Optional[str] = None,
    entry_id: Optional[str] = None,
    item_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    event: Optional[str] = None,
) -> EntryMap:
    return build_message_transcript_entry(
        role="reasoning",
        text=text,
        timestamp=timestamp,
        entry_id=entry_id,
        item_id=item_id,
        turn_id=turn_id,
        subagent_id=subagent_id,
        event=event,
    )


def build_thought_event(
    *,
    text: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
) -> EntryMap:
    event: EntryMap = {
        "type": "thought",
        "text": text,
    }
    return _apply_common_fields(
        event,
        conversation_id=_clean_str(conversation_id),
        turn_id=_clean_str(turn_id),
        subagent_id=_clean_str(subagent_id),
    )

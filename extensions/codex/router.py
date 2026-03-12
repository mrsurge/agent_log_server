from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .runtime_protocol import RuntimeProtocol


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_type_from_label(label_lower: str) -> Optional[str]:
    if label_lower.startswith("codex/event/"):
        return label_lower.split("codex/event/", 1)[-1]
    return None


def _extract_known_event_fields(
    protocol: RuntimeProtocol,
    event_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    schema = protocol.event_schema(event_type)
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    fields: Dict[str, Any] = {}
    for key in props:
        if key == "type":
            continue
        value = payload.get(key)
        if value is not None:
            fields[key] = value
    return fields


def _collab_events(
    protocol: RuntimeProtocol,
    event_type: str,
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not event_type.startswith("collab_"):
        return []

    fields = _extract_known_event_fields(protocol, event_type, payload)
    call_id = str(fields.get("call_id") or "")
    ts = utc_ts()

    if event_type == "collab_agent_spawn_begin":
        name = f"subagent-{call_id[:8]}" if call_id else "subagent"
        return [{
            "type": "subagent_start",
            "name": name,
            "intent": fields.get("prompt", ""),
            "timestamp": ts,
            "_transcript_entry": {
                "role": "subagent_start",
                "id": call_id,
                "name": name,
                "intent": fields.get("prompt", ""),
                "timestamp": ts,
            },
        }]

    if event_type == "collab_agent_spawn_end":
        status = fields.get("status", {})
        success = status.get("type") == "success" if isinstance(status, dict) else status == "success"
        summary = f"spawn {'succeeded' if success else 'failed'}"
        return [{
            "type": "subagent_end",
            "id": call_id,
            "success": success,
            "summary": summary,
            "timestamp": ts,
            "_transcript_entry": {
                "role": "subagent_end",
                "id": call_id,
                "success": success,
                "summary": summary,
                "timestamp": ts,
            },
        }]

    if event_type == "collab_agent_interaction_begin":
        receiver = str(fields.get("receiver_thread_id") or "")
        name = f"collab-{receiver[:8]}" if receiver else f"collab-{call_id[:8] or 'subagent'}"
        return [{
            "type": "subagent_start",
            "id": call_id,
            "name": name,
            "intent": fields.get("prompt", ""),
            "timestamp": ts,
            "_transcript_entry": {
                "role": "subagent_start",
                "id": call_id,
                "name": name,
                "intent": fields.get("prompt", ""),
                "timestamp": ts,
            },
        }]

    if event_type == "collab_agent_interaction_end":
        status = fields.get("status", {})
        success = status.get("type") == "success" if isinstance(status, dict) else status == "success"
        summary = fields.get("prompt", "interaction ended")
        return [{
            "type": "subagent_end",
            "id": call_id,
            "success": success,
            "summary": summary,
            "timestamp": ts,
            "_transcript_entry": {
                "role": "subagent_end",
                "id": call_id,
                "success": success,
                "summary": summary,
                "timestamp": ts,
            },
        }]

    if event_type == "collab_close_end":
        return [{
            "type": "subagent_end",
            "id": call_id,
            "success": True,
            "summary": "subagent closed",
            "timestamp": ts,
            "_transcript_entry": {
                "role": "subagent_end",
                "id": call_id,
                "success": True,
                "summary": "subagent closed",
                "timestamp": ts,
            },
        }]

    if event_type in {"collab_waiting_begin", "collab_resume_begin", "collab_close_begin"}:
        return [{"type": "activity", "label": f"collab: {event_type.replace('collab_', '')}", "active": True}]

    if event_type in {"collab_waiting_end", "collab_resume_end"}:
        return [{"type": "activity", "label": "processing", "active": True}]

    return []


def _direct_event_text(payload: Dict[str, Any]) -> Optional[str]:
    text = payload.get("message")
    if not isinstance(text, str):
        text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    text_elements = payload.get("text_elements") or payload.get("textElements")
    if isinstance(text_elements, list):
        parts = [part for part in text_elements if isinstance(part, str) and part.strip()]
        if parts:
            return "\n".join(parts).strip()
    return None


def _assistant_id(payload: Dict[str, Any], thread_id: Optional[str], turn_id: Optional[str]) -> str:
    if isinstance(payload.get("item"), dict) and isinstance(payload["item"].get("id"), str):
        return payload["item"]["id"]
    for key in ("item_id", "itemId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if turn_id:
        return f"assistant_{turn_id}"
    if thread_id:
        return f"assistant_{thread_id}"
    return "assistant"


def _normalize_turn_status(payload: Dict[str, Any]) -> tuple[str, Optional[str]]:
    turn_obj = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
    status = turn_obj.get("status") if isinstance(turn_obj, dict) else None
    if isinstance(status, dict):
        turn_status = str(status.get("type") or status.get("status") or "completed")
    elif isinstance(status, str):
        turn_status = status
    else:
        turn_status = str(payload.get("status") or "completed")
    turn_error = turn_obj.get("error") if isinstance(turn_obj, dict) else payload.get("error")
    if not isinstance(turn_error, str):
        turn_error = None
    return turn_status, turn_error


def route_event(
    protocol: RuntimeProtocol,
    *,
    label: Optional[str],
    payload: Any,
    thread_id: Optional[str],
    turn_id: Optional[str],
    extract_item_text: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, str]]]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "handled": False,
        "events": [],
        "transcript_entries": [],
    }
    if not label:
        return result

    label_lower = label.lower()
    event_type = _event_type_from_label(label_lower)
    if event_type and protocol.has_event_type(event_type) and isinstance(payload, dict):
        collab = _collab_events(protocol, event_type, payload)
        if collab:
            transcript_entries = []
            events = []
            for event in collab:
                transcript_entry = event.pop("_transcript_entry", None)
                if isinstance(transcript_entry, dict):
                    transcript_entries.append(transcript_entry)
                events.append(event)
            return {
                "handled": True,
                "events": events,
                "transcript_entries": transcript_entries,
            }

    if label_lower == "thread/started" and protocol.has_notification("thread/started"):
        return {
            "handled": True,
            "events": [{"type": "activity", "label": "thread started", "active": True}],
            "transcript_entries": [],
        }

    if label_lower == "turn/started" and protocol.has_notification("turn/started") and isinstance(payload, dict):
        next_turn_id = turn_id
        turn_obj = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        if isinstance(turn_obj, dict):
            next_turn_id = turn_obj.get("id") or next_turn_id
        return {
            "handled": True,
            "set_turn_id": next_turn_id,
            "events": [{"type": "activity", "label": "turn started", "active": True}],
            "transcript_entries": [],
        }

    if label_lower == "turn/completed" and protocol.has_notification("turn/completed") and isinstance(payload, dict):
        turn_status, turn_error = _normalize_turn_status(payload)
        if turn_status == "failed":
            ribbon_status = "error"
        elif turn_status == "interrupted":
            ribbon_status = "warning"
        else:
            ribbon_status = "success"
        return {
            "handled": True,
            "clear_turn_id": True,
            "events": [
                {
                    "type": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "error": turn_error,
                },
                {"type": "activity", "label": "idle", "active": False},
            ],
            "transcript_entries": [
                {
                    "role": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "turn_id": turn_id,
                    "error": turn_error,
                    "event": label_lower,
                }
            ],
        }

    if label_lower == "item/started" and protocol.has_notification("item/started") and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        if extract_item_text and isinstance(item, dict):
            entry = extract_item_text(item)
            if entry and entry.get("role") == "user":
                item_id = item.get("id")
                return {
                    "handled": True,
                    "events": [{"type": "message", "role": "user", "id": item_id, "text": entry["text"]}],
                    "transcript_entries": [{
                        "role": "user",
                        "text": entry["text"],
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }

    if label_lower == "item/agentmessage/delta" and protocol.has_notification("item/agentmessage/delta") and isinstance(payload, dict):
        delta = payload.get("delta")
        if isinstance(delta, str):
            item_id = _assistant_id(payload, thread_id, turn_id)
            return {
                "handled": True,
                "events": [
                    {"type": "assistant_delta", "id": item_id, "delta": delta},
                    {"type": "activity", "label": "responding", "active": True},
                ],
                "transcript_entries": [],
            }

    if label_lower == "item/completed" and protocol.has_notification("item/completed") and isinstance(payload, dict):
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        if extract_item_text and isinstance(item, dict):
            entry = extract_item_text(item)
            if entry and entry.get("role") == "assistant":
                item_id = item.get("id")
                return {
                    "handled": True,
                    "events": [{"type": "assistant_finalize", "id": item_id or _assistant_id(item, thread_id, turn_id), "text": entry["text"]}],
                    "transcript_entries": [{
                        "role": "assistant",
                        "text": entry["text"],
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }

    if event_type == "user_message" and protocol.has_event_type(event_type) and isinstance(payload, dict):
        text = _direct_event_text(payload)
        if text:
            item_id = _assistant_id(payload, thread_id, turn_id)
            return {
                "handled": True,
                "events": [{"type": "message", "role": "user", "id": item_id, "text": text}],
                "transcript_entries": [{
                    "role": "user",
                    "text": text,
                    "item_id": payload.get("item_id") or payload.get("itemId"),
                    "turn_id": turn_id,
                    "event": label_lower,
                }],
            }

    if event_type in {"agent_message_content_delta", "agent_message_delta"} and protocol.has_event_type(event_type) and isinstance(payload, dict):
        delta = payload.get("delta")
        if isinstance(delta, str):
            item_id = _assistant_id(payload, thread_id, turn_id)
            return {
                "handled": True,
                "events": [
                    {"type": "assistant_delta", "id": item_id, "delta": delta},
                    {"type": "activity", "label": "responding", "active": True},
                ],
                "transcript_entries": [],
            }

    if event_type == "agent_message" and protocol.has_event_type(event_type) and isinstance(payload, dict):
        text = _direct_event_text(payload)
        if text:
            item_id = _assistant_id(payload, thread_id, turn_id)
            return {
                "handled": True,
                "events": [{"type": "assistant_finalize", "id": item_id, "text": text}],
                "transcript_entries": [{
                    "role": "assistant",
                    "text": text,
                    "item_id": payload.get("item_id") or payload.get("itemId"),
                    "turn_id": turn_id,
                    "event": label_lower,
                }],
            }

    return result

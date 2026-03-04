"""
Codex App Server Router

Translates codex app-server binary events (JSON-RPC notifications from
stdout) into internal UI event format.

Uses schema-generated constants from protocol.py so event routing tracks
binary updates automatically.
"""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import COLLAB_EVENT_TYPES, EVENT_FIELDS


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def route_collab_event(
    raw_event_type: str,
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Route a collab event to internal subagent_start / subagent_end format.

    Args:
        raw_event_type: The event type string (e.g. "collab_agent_spawn_begin")
        payload: The event payload dict from the binary

    Returns:
        List of internal events to broadcast + write to transcript.
        Each event has a "type" key and optionally a "_transcript_role" key
        indicating what role to use when writing to transcript.
    """
    if raw_event_type not in COLLAB_EVENT_TYPES:
        return []
    if not isinstance(payload, dict):
        return []

    # Extract known fields using schema constants
    fields = {
        k: payload.get(k)
        for k in EVENT_FIELDS.get(raw_event_type, [])
        if payload.get(k) is not None
    }
    call_id = fields.get("call_id", "")
    ts = utc_ts()

    if raw_event_type == "collab_agent_spawn_begin":
        name = f"subagent-{call_id[:8]}"
        return [{
            "type": "subagent_start",
            "_transcript_role": "subagent_start",
            "id": call_id,
            "name": name,
            "intent": fields.get("prompt", ""),
            "timestamp": ts,
        }]

    if raw_event_type == "collab_agent_spawn_end":
        status = fields.get("status", {})
        success = (
            status.get("type") == "success"
            if isinstance(status, dict)
            else status == "success"
        )
        return [{
            "type": "subagent_end",
            "_transcript_role": "subagent_end",
            "id": call_id,
            "success": success,
            "summary": f"spawn {'succeeded' if success else 'failed'}",
            "timestamp": ts,
        }]

    if raw_event_type == "collab_agent_interaction_begin":
        receiver = fields.get("receiver_thread_id", "")
        name = (
            f"collab-{receiver[:8]}" if receiver
            else f"collab-{call_id[:8]}"
        )
        return [{
            "type": "subagent_start",
            "_transcript_role": "subagent_start",
            "id": call_id,
            "name": name,
            "intent": fields.get("prompt", ""),
            "timestamp": ts,
        }]

    if raw_event_type == "collab_agent_interaction_end":
        status = fields.get("status", {})
        success = (
            status.get("type") == "success"
            if isinstance(status, dict)
            else status == "success"
        )
        return [{
            "type": "subagent_end",
            "_transcript_role": "subagent_end",
            "id": call_id,
            "success": success,
            "summary": fields.get("prompt", "interaction ended"),
            "timestamp": ts,
        }]

    if raw_event_type == "collab_close_end":
        return [{
            "type": "subagent_end",
            "_transcript_role": "subagent_end",
            "id": call_id,
            "success": True,
            "summary": "subagent closed",
            "timestamp": ts,
        }]

    # Activity indicators for other collab events
    if raw_event_type in {
        "collab_waiting_begin", "collab_resume_begin", "collab_close_begin",
    }:
        return [{
            "type": "activity",
            "label": f"collab: {raw_event_type.replace('collab_', '')}",
            "active": True,
        }]

    if raw_event_type in {"collab_waiting_end", "collab_resume_end"}:
        return [{"type": "activity", "label": "processing", "active": True}]

    return []

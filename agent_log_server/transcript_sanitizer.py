from __future__ import annotations

from typing import Any

_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"


def strip_meta_envelope(text: str) -> str:
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1 :]
    return text


def sanitize_transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    role = item.get("role", "")
    if role == "user" and isinstance(item.get("text"), str):
        text = strip_meta_envelope(item["text"])
        if text != item["text"]:
            item = dict(item)
            item["text"] = text
    return item

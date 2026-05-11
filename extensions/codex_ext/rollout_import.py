from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TypeAlias, cast

PayloadMap: TypeAlias = dict[str, object]
PreviewEntry: TypeAlias = dict[str, object]
PreviewResult: TypeAlias = dict[str, object]

_ROLLOUT_SESSIONS_DIR = Path(os.path.expanduser("~/.codex/sessions"))
_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"


def _coerce_payload_map(value: object) -> PayloadMap:
    if not isinstance(value, dict):
        return {}
    return cast(PayloadMap, value).copy()


def _sanitize_rollout_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("_")
    return safe or "unknown"


def find_rollout_path(rollout_id: str) -> Path | None:
    safe = _sanitize_rollout_id(rollout_id)
    if not _ROLLOUT_SESSIONS_DIR.exists():
        return None
    for path in _ROLLOUT_SESSIONS_DIR.rglob(f"*{safe}*.jsonl"):
        if path.is_file():
            return path
    return None


def _strip_meta_envelope(text: str) -> str:
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1 :]
    return text


def _parse_rollout_timestamp(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _rollout_content_text(payload: PayloadMap) -> str | None:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in cast(list[object], content):
            item_map = _coerce_payload_map(item)
            if not item_map:
                continue
            text = item_map.get("text")
            if isinstance(text, str):
                parts.append(text)
    text_value = payload.get("text")
    if not parts and isinstance(text_value, str):
        parts.append(text_value)
    message_value = payload.get("message")
    if not parts and isinstance(message_value, str):
        parts.append(message_value)
    text = "\n".join(parts).strip()
    return text or None


def _rollout_reasoning_text(payload: PayloadMap) -> str | None:
    summary = payload.get("summary")
    parts: list[str] = []
    if isinstance(summary, list):
        for item in cast(list[object], summary):
            item_map = _coerce_payload_map(item)
            if not item_map:
                continue
            text_value = item_map.get("text")
            summary_text_value = item_map.get("summary_text")
            if isinstance(text_value, str):
                parts.append(text_value)
            elif isinstance(summary_text_value, str):
                parts.append(summary_text_value)
    text_value = payload.get("text")
    if not parts and isinstance(text_value, str):
        parts.append(text_value)
    text = "\n".join(parts).strip()
    return text or None


def _rollout_extract_diff(payload: object) -> str | None:
    payload_map = _coerce_payload_map(payload)
    if payload_map:
        for key in ("diff", "unified_diff", "patch"):
            value = payload_map.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload_map.values():
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    if isinstance(payload, list):
        for value in cast(list[object], payload):
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    return None


def preview_entries(path: Path, limit: int = 400) -> PreviewResult:
    items: list[PreviewEntry] = []
    seen: set[tuple[str, str, int | None]] = set()
    token_total: int | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if len(items) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = cast(object, json.loads(line))
                except json.JSONDecodeError:
                    continue
                rec = _coerce_payload_map(loaded)
                if not rec:
                    continue
                timestamp = rec.get("timestamp")
                ts_bucket = _parse_rollout_timestamp(timestamp if isinstance(timestamp, str) else None)
                rtype = rec.get("type")
                payload = rec.get("payload")
                payload_map = _coerce_payload_map(payload)
                if rtype == "response_item" and payload_map:
                    ptype = payload_map.get("type")
                    if ptype == "message":
                        role = payload_map.get("role")
                        if isinstance(role, str) and role in {"user", "assistant"}:
                            text = _rollout_content_text(payload_map)
                            if text:
                                key = (role, text, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": role, "text": text, "ts": timestamp})
                    elif ptype == "reasoning":
                        text = _rollout_reasoning_text(payload_map)
                        if text:
                            key = ("reasoning", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": text, "ts": timestamp})
                elif rtype == "event_msg" and payload_map:
                    ptype = payload_map.get("type")
                    if ptype == "user_message":
                        text = payload_map.get("message")
                        if isinstance(text, str):
                            stripped = _strip_meta_envelope(text).strip()
                            if stripped:
                                key = ("user", stripped, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": "user", "text": stripped, "ts": timestamp})
                    elif ptype == "agent_message":
                        text = payload_map.get("message")
                        if isinstance(text, str) and text.strip():
                            stripped = text.strip()
                            key = ("assistant", stripped, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "assistant", "text": stripped, "ts": timestamp})
                    elif ptype == "agent_reasoning":
                        text = payload_map.get("text")
                        if isinstance(text, str) and text.strip():
                            stripped = text.strip()
                            key = ("reasoning", stripped, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": stripped, "ts": timestamp})
                    elif ptype == "token_count":
                        info = _coerce_payload_map(payload_map.get("info"))
                        usage_value = info.get("total_token_usage")
                        if not isinstance(usage_value, dict):
                            usage_value = info.get("last_token_usage")
                        usage = cast(PayloadMap, usage_value).copy() if isinstance(usage_value, dict) else {}
                        total_tokens = usage.get("total_tokens")
                        if isinstance(total_tokens, (int, float)):
                            token_total = int(total_tokens)
                diff = _rollout_extract_diff(payload)
                if diff:
                    key = ("diff", diff, ts_bucket)
                    if key not in seen:
                        seen.add(key)
                        items.append({"role": "diff", "text": diff, "ts": timestamp})
    except (OSError, UnicodeDecodeError):
        return {"items": [], "token_total": None}
    return {"items": items, "token_total": token_total}

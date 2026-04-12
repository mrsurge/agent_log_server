from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_ROLLOUT_SESSIONS_DIR = Path(os.path.expanduser("~/.codex/sessions"))
_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"


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


def _rollout_content_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    if not parts and isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    if not parts and isinstance(payload.get("message"), str):
        parts.append(payload["message"])
    text = "\n".join(parts).strip()
    return text or None


def _rollout_reasoning_text(payload: dict[str, Any]) -> str | None:
    summary = payload.get("summary")
    parts: list[str] = []
    if isinstance(summary, list):
        for item in summary:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("summary_text")
            if isinstance(text, str):
                parts.append(text)
    if not parts and isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    text = "\n".join(parts).strip()
    return text or None


def _rollout_extract_diff(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("diff", "unified_diff", "patch"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in payload.values():
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    if isinstance(payload, list):
        for value in payload:
            diff = _rollout_extract_diff(value)
            if diff:
                return diff
    return None


def preview_entries(path: Path, limit: int = 400) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
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
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_bucket = _parse_rollout_timestamp(rec.get("timestamp"))
                rtype = rec.get("type")
                payload = rec.get("payload")
                if rtype == "response_item" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype == "message":
                        role = payload.get("role")
                        if role in {"user", "assistant"}:
                            text = _rollout_content_text(payload)
                            if text:
                                key = (role, text, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": role, "text": text, "ts": rec.get("timestamp")})
                    elif ptype == "reasoning":
                        text = _rollout_reasoning_text(payload)
                        if text:
                            key = ("reasoning", text, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": text, "ts": rec.get("timestamp")})
                elif rtype == "event_msg" and isinstance(payload, dict):
                    ptype = payload.get("type")
                    if ptype == "user_message":
                        text = payload.get("message")
                        if isinstance(text, str):
                            text = _strip_meta_envelope(text).strip()
                            if text:
                                key = ("user", text, ts_bucket)
                                if key not in seen:
                                    seen.add(key)
                                    items.append({"role": "user", "text": text, "ts": rec.get("timestamp")})
                    elif ptype == "agent_message":
                        text = payload.get("message")
                        if isinstance(text, str) and text.strip():
                            stripped = text.strip()
                            key = ("assistant", stripped, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "assistant", "text": stripped, "ts": rec.get("timestamp")})
                    elif ptype == "agent_reasoning":
                        text = payload.get("text")
                        if isinstance(text, str) and text.strip():
                            stripped = text.strip()
                            key = ("reasoning", stripped, ts_bucket)
                            if key not in seen:
                                seen.add(key)
                                items.append({"role": "reasoning", "text": stripped, "ts": rec.get("timestamp")})
                    elif ptype == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            usage = info.get("total_token_usage") or info.get("last_token_usage") or {}
                            if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)):
                                token_total = int(usage["total_tokens"])
                diff = _rollout_extract_diff(payload)
                if diff:
                    key = ("diff", diff, ts_bucket)
                    if key not in seen:
                        seen.add(key)
                        items.append({"role": "diff", "text": diff, "ts": rec.get("timestamp")})
    except (OSError, UnicodeDecodeError):
        return {"items": [], "token_total": None}
    return {"items": items, "token_total": token_total}

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    for key in ("item_id", "itemId", "id", "callId", "call_id"):
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


def _item_type(item: Dict[str, Any]) -> str:
    return str(item.get("type") or "").strip().lower()


def _normalize_output(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_output(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _extract_path_from_diff(diff_text: str) -> Optional[str]:
    if not diff_text:
        return None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                bpath = parts[3]
                if bpath.startswith("b/"):
                    return bpath[2:]
                return bpath
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                return path[2:]
            if path != "/dev/null":
                return path
        if line.startswith("--- "):
            path = line[4:].strip()
            if path.startswith("a/"):
                return path[2:]
            if path != "/dev/null":
                return path
    return None


def _extract_diff_with_path(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    path = payload.get("path") if isinstance(payload.get("path"), str) else None
    diff = payload.get("diff") or payload.get("patch") or payload.get("unified_diff")
    if isinstance(diff, str) and diff.strip():
        return diff, path or _extract_path_from_diff(diff)

    changes = payload.get("changes")
    if isinstance(changes, list):
        chunks: List[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            text = change.get("diff") or change.get("patch") or change.get("unified_diff")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
                if not path and isinstance(change.get("path"), str):
                    path = change["path"]
        if chunks:
            combined = "\n".join(chunks)
            return combined, path or _extract_path_from_diff(combined)

    if isinstance(changes, dict):
        chunks = []
        for change_path, change in changes.items():
            if not isinstance(change, dict):
                continue
            text = change.get("diff") or change.get("patch") or change.get("unified_diff")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
                if not path:
                    candidate = change.get("path") or change.get("file_path") or change_path
                    if isinstance(candidate, str) and candidate:
                        path = candidate
        if chunks:
            combined = "\n".join(chunks)
            return combined, path or _extract_path_from_diff(combined)

    return None, None


def _diff_signature(diff_text: str) -> str:
    if not diff_text:
        return "empty"
    files: List[str] = []
    hunks: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            files.append(line.strip())
        elif line.startswith("@@"):
            hunks.append(line.strip())
    signature = "\n".join(files + hunks) + "\n" + diff_text
    return hashlib.sha1(signature.encode("utf-8")).hexdigest()


def _split_unified_diff_by_file(diff_text: str) -> List[Tuple[Optional[str], str]]:
    if not isinstance(diff_text, str) or not diff_text.strip():
        return []
    lines = diff_text.splitlines(keepends=True)
    start_idxs: List[int] = []
    for idx, line in enumerate(lines):
        if line.startswith("diff --git "):
            start_idxs.append(idx)
    if not start_idxs:
        return [(_extract_path_from_diff(diff_text), diff_text.strip())]

    sections: List[Tuple[Optional[str], str]] = []
    for idx, start in enumerate(start_idxs):
        end = start_idxs[idx + 1] if (idx + 1) < len(start_idxs) else len(lines)
        section = "".join(lines[start:end]).strip()
        if not section:
            continue
        sections.append((_extract_path_from_diff(section), section))
    return sections


def _paths_from_changes(changes: Any) -> List[str]:
    paths: List[str] = []
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path") or _extract_path_from_diff(str(change.get("diff") or ""))
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    elif isinstance(changes, dict):
        for change_path, change in changes.items():
            candidate = None
            if isinstance(change, dict):
                candidate = change.get("path") or change.get("file_path") or change_path
            elif isinstance(change_path, str):
                candidate = change_path
            if isinstance(candidate, str) and candidate and candidate not in paths:
                paths.append(candidate)
    return paths


def _summarize_paths(paths: List[str]) -> Optional[str]:
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    return f"{paths[0]} (+{len(paths) - 1} more)"


def _result_error_status(status: str, exit_code: Optional[int], error: Any = None) -> bool:
    return bool(error) or status in {"failed", "declined", "error"} or exit_code not in (None, 0)


class CodexEventRouter:
    def __init__(self) -> None:
        self._turn_states: Dict[str, Dict[str, Any]] = {}
        self._item_states: Dict[str, Dict[str, Any]] = {}
        self._approval_request_map: Dict[str, str] = {}

    def reset(self) -> None:
        self._turn_states.clear()
        self._item_states.clear()
        self._approval_request_map.clear()

    def _turn_key(self, thread_id: Optional[str], turn_id: Optional[str]) -> str:
        return f"{thread_id or 'unknown'}:{turn_id or 'unknown'}"

    def _get_turn_state(self, thread_id: Optional[str], turn_id: Optional[str]) -> Dict[str, Any]:
        key = self._turn_key(thread_id, turn_id)
        state = self._turn_states.get(key)
        if state is None:
            state = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "diff_hashes": set(),
            }
            self._turn_states[key] = state
        return state

    def _get_item_state(self, item_id: Optional[str], thread_id: Optional[str], turn_id: Optional[str]) -> Dict[str, Any]:
        turn_state = self._get_turn_state(thread_id, turn_id)
        if not item_id:
            return turn_state
        state = self._item_states.get(item_id)
        if state is None:
            state = {
                "item_id": item_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_key": self._turn_key(thread_id, turn_id),
                "output_buffer": "",
            }
            self._item_states[item_id] = state
        else:
            state["thread_id"] = thread_id or state.get("thread_id")
            state["turn_id"] = turn_id or state.get("turn_id")
            state["turn_key"] = self._turn_key(thread_id or state.get("thread_id"), turn_id or state.get("turn_id"))
        return state

    def _clear_item_state(self, item_id: Optional[str]) -> Dict[str, Any]:
        if not item_id:
            return {}
        return self._item_states.pop(item_id, {})

    def _emit_diff_entries(
        self,
        result: Dict[str, Any],
        *,
        diff_text: str,
        path: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        item_id: Optional[str],
        event_name: str,
    ) -> None:
        turn_state = self._get_turn_state(thread_id, turn_id)
        diff_hashes = turn_state.setdefault("diff_hashes", set())
        for section_path, section_text in _split_unified_diff_by_file(diff_text):
            if not section_text:
                continue
            diff_hash = _diff_signature(section_text)
            if diff_hash in diff_hashes:
                continue
            diff_hashes.add(diff_hash)
            effective_path = section_path or path
            if thread_id or turn_id:
                diff_id = f"{thread_id or 'unknown'}:{turn_id or 'unknown'}:{diff_hash[:12]}"
            elif item_id:
                diff_id = f"item:{item_id}:{diff_hash[:12]}"
            else:
                diff_id = f"diff:{diff_hash[:12]}"
            result["events"].append({
                "type": "diff",
                "id": diff_id,
                "text": section_text,
                "path": effective_path,
            })
            result["transcript_entries"].append({
                "role": "diff",
                "text": section_text,
                "path": effective_path,
                "item_id": diff_id,
                "turn_id": turn_id,
                "event": event_name,
            })

    def _tool_request_result(
        self,
        *,
        request_id: str,
        kind: str,
        payload: Dict[str, Any],
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Dict[str, Any]:
        approval_event = {
            "type": "approval",
            "kind": kind,
            "id": request_id,
            "request_id": request_id,
            "payload": payload,
            "turn_id": turn_id,
            "created_at": utc_ts(),
        }
        return {
            "handled": True,
            "events": [
                approval_event,
                {"type": "activity", "label": "approval", "active": True},
            ],
            "transcript_entries": [],
            "approval_descriptors": [{
                "request_id": request_id,
                "kind": kind,
                "payload": payload,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "transcript_anchor": {"turn_id": turn_id},
                "source": "live",
                "created_at": approval_event["created_at"],
                "render_event": approval_event,
            }],
        }

    def route_event(
        self,
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
            self._get_turn_state(thread_id, next_turn_id)
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
            self._turn_states.pop(self._turn_key(thread_id, turn_id), None)
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
                "transcript_entries": [{
                    "role": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "turn_id": turn_id,
                    "error": turn_error,
                    "event": label_lower,
                }],
            }

        if label_lower in {"turn/diff/updated", "codex/event/turn_diff"} and isinstance(payload, dict):
            diff_text, path = _extract_diff_with_path(payload)
            if diff_text:
                result["handled"] = True
                self._emit_diff_entries(
                    result,
                    diff_text=diff_text,
                    path=path,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=None,
                    event_name=label_lower,
                )
            return result if result["handled"] else {"handled": True, "events": [], "transcript_entries": []}

        if label_lower == "item/started" and protocol.has_notification("item/started") and isinstance(payload, dict):
            item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
            if not isinstance(item, dict):
                return result
            item_type = _item_type(item)
            item_id = item.get("id") if isinstance(item.get("id"), str) else None
            item_state = self._get_item_state(item_id, thread_id, turn_id)

            if extract_item_text:
                entry = extract_item_text(item)
                if entry and entry.get("role") == "user":
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

            if item_type == "commandexecution":
                command = item.get("command") or item.get("parsedCmd") or item.get("cmd") or item.get("argv") or ""
                cwd = item.get("cwd") or ""
                item_state.update({
                    "item_type": item_type,
                    "command": command,
                    "cwd": cwd,
                    "output_buffer": "",
                })
                return {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_begin",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "command",
                            "arguments": {"command": command, "cwd": cwd} if command or cwd else {},
                        },
                        {"type": "activity", "label": "running command", "active": True},
                    ],
                    "transcript_entries": [],
                }

            if item_type == "filechange":
                changes = item.get("changes")
                diff_text, path = _extract_diff_with_path(item)
                paths = _paths_from_changes(changes)
                item_state.update({
                    "item_type": item_type,
                    "changes": changes,
                    "diff": diff_text,
                    "path": path,
                    "paths": paths,
                    "output_buffer": "",
                })
                arguments: Dict[str, Any] = {}
                if paths:
                    arguments["paths"] = paths
                    arguments["change_count"] = len(paths)
                return {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_begin",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "apply_patch",
                            "arguments": arguments,
                        },
                        {"type": "activity", "label": "preparing diff", "active": True},
                    ],
                    "transcript_entries": [],
                }

            if item_type in {"mcptoolcall", "websearch", "imageview"}:
                tool_name = item.get("tool")
                server_name = item.get("server")
                arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
                if item_type == "websearch":
                    tool_name = "web_search"
                    arguments = {"query": item.get("query")}
                elif item_type == "imageview":
                    tool_name = "view_image"
                    arguments = {"path": item.get("path")}
                item_state.update({
                    "item_type": item_type,
                    "tool": tool_name or item_type,
                    "server": server_name or "",
                    "arguments": arguments,
                })
                activity_label = f"calling {tool_name}" if isinstance(tool_name, str) and tool_name else "calling tool"
                return {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_begin",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": tool_name or item_type,
                            "server": server_name or "",
                            "arguments": arguments,
                        },
                        {"type": "activity", "label": activity_label, "active": True},
                    ],
                    "transcript_entries": [],
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

        if label_lower == "item/commandexecution/outputdelta" and protocol.has_notification("item/commandexecution/outputdelta") and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            delta = _normalize_output(payload.get("delta"))
            if delta:
                state["output_buffer"] = f"{state.get('output_buffer', '')}{delta}"
                return {
                    "handled": True,
                    "events": [{
                        "type": "tool_delta",
                        "id": item_id or _assistant_id(payload, thread_id, turn_id),
                        "tool": "command",
                        "delta": delta,
                    }],
                    "transcript_entries": [],
                }
            return {"handled": True, "events": [], "transcript_entries": []}

        if label_lower == "item/filechange/outputdelta" and protocol.has_notification("item/filechange/outputdelta") and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            delta = _normalize_output(payload.get("delta"))
            if delta:
                state["output_buffer"] = f"{state.get('output_buffer', '')}{delta}"
                return {
                    "handled": True,
                    "events": [{
                        "type": "tool_delta",
                        "id": item_id or _assistant_id(payload, thread_id, turn_id),
                        "tool": "apply_patch",
                        "delta": delta,
                    }],
                    "transcript_entries": [],
                }
            return {"handled": True, "events": [], "transcript_entries": []}

        if label_lower == "item/commandexecution/terminalinteraction" and protocol.has_notification("item/commandexecution/terminalinteraction") and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            return {
                "handled": True,
                "events": [{
                    "type": "tool_interaction",
                    "id": item_id or _assistant_id(payload, thread_id, turn_id),
                    "tool": "command",
                    "payload": {
                        "stdin": payload.get("stdin"),
                        "stdout": payload.get("stdout"),
                        "pid": payload.get("pid"),
                    },
                }],
                "transcript_entries": [],
            }

        if label_lower == "item/completed" and protocol.has_notification("item/completed") and isinstance(payload, dict):
            item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
            if not isinstance(item, dict):
                return result
            item_type = _item_type(item)
            item_id = item.get("id") if isinstance(item.get("id"), str) else None
            item_state = self._clear_item_state(item_id)

            if extract_item_text:
                entry = extract_item_text(item)
                if entry and entry.get("role") == "assistant":
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

            if item_type == "commandexecution":
                command = item.get("command") or item.get("parsedCmd") or item_state.get("command") or ""
                display_command = _command_text(command)
                cwd = item.get("cwd") or item_state.get("cwd") or ""
                output = _normalize_output(
                    item.get("aggregatedOutput") or item.get("output") or item.get("stdout") or item_state.get("output_buffer")
                )
                exit_code = item.get("exitCode") if item.get("exitCode") is not None else item.get("exit_code")
                duration_ms = item.get("durationMs") if item.get("durationMs") is not None else item.get("duration_ms")
                status = str(item.get("status") or "").strip().lower()
                is_error = _result_error_status(status, exit_code, item.get("error"))
                routed: Dict[str, Any] = {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "command",
                            "arguments": {"command": command, "cwd": cwd} if command or cwd else {},
                            "result": {
                                "status": status or "completed",
                                "exit_code": exit_code,
                                "output_lines": len(output.splitlines()) if output else 0,
                            },
                            "duration_ms": duration_ms,
                            "is_error": is_error,
                        },
                        {
                            "type": "command_result",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "command": display_command,
                            "cwd": cwd,
                            "output": output,
                            "exit_code": exit_code,
                            "duration_ms": duration_ms,
                        },
                        {"type": "activity", "label": "processing", "active": True},
                    ],
                    "transcript_entries": [{
                        "role": "command",
                        "command": display_command,
                        "cwd": cwd,
                        "output": output,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "status": status or ("error" if is_error else "completed"),
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }
                approval_request_id = item_state.get("approval_request_id")
                if approval_request_id:
                    routed["clear_live_approval_ids"] = [approval_request_id]
                    self._approval_request_map.pop(str(item_id), None)
                return routed

            if item_type == "filechange":
                changes = item.get("changes") if item.get("changes") is not None else item_state.get("changes")
                paths = _paths_from_changes(changes) or item_state.get("paths") or []
                output = _normalize_output(item_state.get("output_buffer"))
                status = str(item.get("status") or "").strip().lower()
                first_path = _summarize_paths(paths) or item_state.get("path")
                command_label = f"apply_patch {first_path}".strip() if first_path else "apply_patch"
                is_error = status in {"failed", "declined", "error"}
                routed = {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "apply_patch",
                            "arguments": {
                                "paths": paths,
                                "change_count": len(paths),
                            } if paths else {},
                            "result": {
                                "status": status or "completed",
                                "changed_files": len(paths),
                            },
                            "is_error": is_error,
                        },
                        {"type": "activity", "label": "processing", "active": True},
                    ],
                    "transcript_entries": [{
                        "role": "command",
                        "command": command_label,
                        "output": output,
                        "status": status or ("error" if is_error else "completed"),
                        "path": paths[0] if paths else item_state.get("path"),
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }
                approval_request_id = item_state.get("approval_request_id")
                if approval_request_id:
                    routed["clear_live_approval_ids"] = [approval_request_id]
                    self._approval_request_map.pop(str(item_id), None)
                return routed

            if item_type in {"mcptoolcall", "websearch", "imageview"}:
                tool_name = item.get("tool") or item_state.get("tool") or item_type
                server_name = item.get("server") or item_state.get("server") or ""
                arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else item_state.get("arguments") or {}
                if item_type == "websearch":
                    tool_name = "web_search"
                    arguments = {"query": item.get("query")}
                elif item_type == "imageview":
                    tool_name = "view_image"
                    arguments = {"path": item.get("path")}
                status = str(item.get("status") or "").strip().lower()
                error = item.get("error")
                result_value = error if error is not None else item.get("result")
                output = _stringify_value(result_value or {"status": status or "completed"})
                is_error = bool(error) or status in {"failed", "error"}
                command_label = f"{server_name}:{tool_name}" if server_name else str(tool_name)
                return {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": tool_name,
                            "server": server_name,
                            "arguments": arguments,
                            "result": result_value if result_value is not None else {"status": status or "completed"},
                            "is_error": is_error,
                        },
                        {"type": "activity", "label": "processing", "active": True},
                    ],
                    "transcript_entries": [{
                        "role": "command",
                        "command": command_label,
                        "output": output,
                        "status": status or ("error" if is_error else "completed"),
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }

            return {"handled": True, "events": [], "transcript_entries": []}

        if label_lower == "item/tool/call" and isinstance(payload, dict):
            tool_id = payload.get("callId") or payload.get("call_id") or payload.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = _assistant_id(payload, thread_id, turn_id)
            tool_name = payload.get("tool") or "tool"
            arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
            item_state = self._get_item_state(tool_id, thread_id, turn_id)
            item_state.update({
                "item_type": "tool",
                "tool": tool_name,
                "arguments": arguments,
            })
            return {
                "handled": True,
                "events": [{
                    "type": "tool_begin",
                    "id": tool_id,
                    "tool": tool_name,
                    "arguments": arguments,
                }],
                "transcript_entries": [],
            }

        if label_lower == "item/tool/requestuserinput" and isinstance(payload, dict):
            message = payload.get("message") or "Tool requested user input"
            return {
                "handled": True,
                "events": [{
                    "type": "warning",
                    "message": str(message),
                }],
                "transcript_entries": [],
            }

        if label_lower == "item/commandexecution/requestapproval" and isinstance(payload, dict):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            item_id = payload.get("itemId") or payload.get("item_id")
            request_id_text = str(request_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            if request_id_text and item_id:
                self._approval_request_map[str(item_id)] = request_id_text
                item_state["approval_request_id"] = request_id_text
            payload_data: Dict[str, Any] = {
                "command": payload.get("parsedCmd") or payload.get("command") or item_state.get("command"),
                "cwd": payload.get("cwd") or item_state.get("cwd"),
                "reason": payload.get("reason"),
                "risk": payload.get("risk"),
            }
            return self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="command",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [])},
                thread_id=thread_id,
                turn_id=turn_id,
            )

        if label_lower == "item/filechange/requestapproval" and isinstance(payload, dict):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            item_id = payload.get("itemId") or payload.get("item_id")
            request_id_text = str(request_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            if request_id_text and item_id:
                self._approval_request_map[str(item_id)] = request_id_text
                item_state["approval_request_id"] = request_id_text
            diff_text, path = _extract_diff_with_path({
                "diff": payload.get("diff"),
                "patch": payload.get("patch"),
                "unified_diff": payload.get("unified_diff"),
                "changes": payload.get("changes") or item_state.get("changes"),
                "path": payload.get("path") or item_state.get("path"),
            })
            payload_data = {
                "diff": diff_text,
                "changes": payload.get("changes") or item_state.get("changes"),
                "reason": payload.get("reason"),
                "path": path or item_state.get("path"),
            }
            return self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="diff",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
            )

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

        if event_type == "exec_approval_request" and isinstance(payload, dict):
            item_id = payload.get("call_id")
            request_id_text = str(payload.get("approval_id") or item_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            if request_id_text and item_id:
                self._approval_request_map[str(item_id)] = request_id_text
                item_state["approval_request_id"] = request_id_text
            payload_data = {
                "command": payload.get("parsed_cmd") or payload.get("command") or item_state.get("command"),
                "cwd": payload.get("cwd") or item_state.get("cwd"),
                "reason": payload.get("reason"),
                "risk": payload.get("risk"),
            }
            return self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="command",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [])},
                thread_id=thread_id,
                turn_id=turn_id,
            )

        if event_type == "apply_patch_approval_request" and isinstance(payload, dict):
            item_id = payload.get("call_id")
            request_id_text = str(payload.get("id") or item_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            if request_id_text and item_id:
                self._approval_request_map[str(item_id)] = request_id_text
                item_state["approval_request_id"] = request_id_text
            diff_text, path = _extract_diff_with_path({
                "changes": payload.get("changes") or item_state.get("changes"),
                "path": item_state.get("path"),
            })
            return self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="diff",
                payload={
                    "diff": diff_text,
                    "changes": payload.get("changes") or item_state.get("changes"),
                    "reason": payload.get("reason"),
                    "path": path or item_state.get("path"),
                },
                thread_id=thread_id,
                turn_id=turn_id,
            )

        return result


def route_event(
    protocol: RuntimeProtocol,
    *,
    label: Optional[str],
    payload: Any,
    thread_id: Optional[str],
    turn_id: Optional[str],
    extract_item_text: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, str]]]] = None,
) -> Dict[str, Any]:
    return CodexEventRouter().route_event(
        protocol,
        label=label,
        payload=payload,
        thread_id=thread_id,
        turn_id=turn_id,
        extract_item_text=extract_item_text,
    )

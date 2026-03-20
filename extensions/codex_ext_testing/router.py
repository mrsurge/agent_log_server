import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .plan_utils import normalize_plan_steps, plan_signature, render_plan_markdown
from .runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_type_from_label(label_lower: str) -> Optional[str]:
    if label_lower.startswith("codex/event/"):
        return label_lower.split("codex/event/", 1)[-1]
    return None


def _extract_known_fields(spec: Optional[ProtocolSemanticSpec], payload: Dict[str, Any]) -> Dict[str, Any]:
    if spec is None:
        return {}
    fields: Dict[str, Any] = {}
    for key in spec.properties:
        value = payload.get(key)
        if value is not None:
            fields[key] = value
    return fields


def _subagent_display_name(fields: Dict[str, Any], call_id: str) -> str:
    nickname = fields.get("receiver_agent_nickname") or fields.get("new_agent_nickname")
    role = fields.get("receiver_agent_role") or fields.get("new_agent_role")
    if not nickname and not role:
        for record in _collab_agent_records(fields):
            nickname = record.get("agent_nickname") or record.get("receiver_agent_nickname")
            role = record.get("agent_role") or record.get("receiver_agent_role")
            if nickname or role:
                break
    if isinstance(nickname, str) and nickname.strip():
        if isinstance(role, str) and role.strip():
            return f"{nickname.strip()} ({role.strip()})"
        return nickname.strip()
    if isinstance(role, str) and role.strip():
        return role.strip()
    receiver = fields.get("receiver_thread_id") or fields.get("new_thread_id")
    if isinstance(receiver, str) and receiver:
        return f"subagent-{receiver[:8]}"
    return "subagent"


def _collab_agent_records(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for key in ("receiver_agents", "agent_statuses"):
        value = fields.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                records.append(item)
    return records


def _collab_thread_ids(fields: Dict[str, Any]) -> List[str]:
    thread_ids: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in thread_ids:
            thread_ids.append(value)

    add(fields.get("new_thread_id"))
    add(fields.get("receiver_thread_id"))

    receiver_thread_ids = fields.get("receiver_thread_ids")
    if isinstance(receiver_thread_ids, list):
        for item in receiver_thread_ids:
            add(item)

    for record in _collab_agent_records(fields):
        add(record.get("thread_id"))

    statuses = fields.get("statuses")
    if isinstance(statuses, dict):
        for candidate in statuses.keys():
            add(candidate)

    return thread_ids


def _collab_status_for_thread(fields: Dict[str, Any], thread_id: str) -> Any:
    for record in _collab_agent_records(fields):
        if record.get("thread_id") == thread_id and record.get("status") is not None:
            return record.get("status")

    statuses = fields.get("statuses")
    if isinstance(statuses, dict):
        status = statuses.get(thread_id)
        if status is not None:
            return status

    return fields.get("status")


def _subagent_terminal_summary(name: str, status: Any, *, success_text: str, failure_text: str) -> str:
    if isinstance(status, dict):
        errored = status.get("errored")
        if isinstance(errored, str) and errored.strip():
            return f"Failed: {errored.strip()}"
    if status == "shutdown":
        return "subagent shutdown"
    if status == "not_found":
        return "subagent not found"
    return success_text if _agent_status_success(status) else failure_text


def _agent_status_is_terminal(status: Any) -> bool:
    if isinstance(status, dict):
        return "completed" in status or "errored" in status
    return status in {"shutdown", "not_found"}


def _agent_status_success(status: Any) -> bool:
    if isinstance(status, dict):
        return "completed" in status
    return False


def _agent_status_summary(status: Any, default: str) -> str:
    if isinstance(status, dict):
        completed = status.get("completed")
        if isinstance(completed, str) and completed.strip():
            return completed.strip()
        errored = status.get("errored")
        if isinstance(errored, str) and errored.strip():
            return f"Failed: {errored.strip()}"
    if status == "shutdown":
        return "subagent shutdown"
    if status == "not_found":
        return "subagent not found"
    return default


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


def _append_text_parts(parts: List[str], value: Any) -> None:
    if isinstance(value, str):
        text = _normalize_output(value).strip()
        if text:
            parts.append(text)
        return
    if isinstance(value, list):
        for item in value:
            _append_text_parts(parts, item)
        return
    if isinstance(value, dict):
        for key in ("text", "summary", "content", "message"):
            candidate = value.get(key)
            if candidate is None:
                continue
            before = len(parts)
            _append_text_parts(parts, candidate)
            if len(parts) != before:
                return


def _extract_reasoning_text(item: Dict[str, Any], fallback: Optional[str] = None) -> Optional[str]:
    parts: List[str] = []
    for key in ("summary", "summary_text", "summaryText", "text", "raw_content", "rawContent", "content"):
        _append_text_parts(parts, item.get(key))
        if parts:
            break
    if not parts and isinstance(fallback, str):
        _append_text_parts(parts, fallback)
    text = "\n".join(part for part in parts if isinstance(part, str) and part).strip()
    return text or None


def _reasoning_event_id(payload: Dict[str, Any], turn_state: Dict[str, Any]) -> str:
    for key in ("item_id", "itemId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    current = turn_state.get("reasoning_id")
    if isinstance(current, str) and current:
        return current
    return "reasoning"


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


_THOUGHT_PATTERN = re.compile(r"\*\*([^*]+)\*\*")


def _extract_and_scrub_thoughts_stream(delta: str, state: Dict[str, Any]) -> Tuple[str, List[str]]:
    if not isinstance(delta, str) or not delta:
        return delta, []
    buffer = state.get("thought_buffer", "")
    text = buffer + delta
    thoughts: List[str] = []
    scrubbed_parts: List[str] = []
    idx = 0
    state["thought_buffer"] = ""
    while True:
        start = text.find("**", idx)
        if start == -1:
            scrubbed_parts.append(text[idx:])
            break
        scrubbed_parts.append(text[idx:start])
        end = text.find("**", start + 2)
        if end == -1:
            state["thought_buffer"] = text[start:]
            break
        content = text[start + 2:end]
        if content:
            thoughts.append(content)
        idx = end + 2
    scrubbed = "".join(scrubbed_parts)
    if not state["thought_buffer"] and text.endswith("*") and not text.endswith("**"):
        state["thought_buffer"] = "*"
        if scrubbed.endswith("*"):
            scrubbed = scrubbed[:-1]
    return scrubbed, thoughts


def _extract_and_scrub_thoughts(text: str) -> Tuple[str, List[str]]:
    if not text:
        return text, []
    thoughts = _THOUGHT_PATTERN.findall(text)
    scrubbed = _THOUGHT_PATTERN.sub("", text)
    return scrubbed, thoughts


class CodexEventRouter:
    def __init__(self) -> None:
        self._turn_states: Dict[str, Dict[str, Any]] = {}
        self._item_states: Dict[str, Dict[str, Any]] = {}
        self._approval_request_map: Dict[str, str] = {}
        self._subagent_states: Dict[str, Dict[str, Any]] = {}
        self._thread_subagent_ids: Dict[str, str] = {}

    def reset(self) -> None:
        self._turn_states.clear()
        self._item_states.clear()
        self._approval_request_map.clear()
        self._subagent_states.clear()
        self._thread_subagent_ids.clear()

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
                "plan_steps": [],
                "plan_signature": None,
                "plan_explanation": None,
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
        subagent_id = self._subagent_id_for_context(thread_id)
        if subagent_id:
            state["subagent_id"] = subagent_id
        return state

    def _clear_item_state(self, item_id: Optional[str]) -> Dict[str, Any]:
        if not item_id:
            return {}
        return self._item_states.pop(item_id, {})

    def _ensure_subagent_state(
        self,
        subagent_id: str,
        *,
        name: Optional[str] = None,
        intent: Optional[str] = None,
        parent_thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = self._subagent_states.get(subagent_id)
        if state is None:
            state = {
                "id": subagent_id,
                "name": name or "subagent",
                "intent": intent or "",
                "parent_thread_id": parent_thread_id,
                "thread_ids": set(),
                "started": False,
                "ended": False,
                "active": False,
            }
            self._subagent_states[subagent_id] = state
        if isinstance(name, str) and name.strip():
            state["name"] = name.strip()
        if isinstance(intent, str) and intent.strip():
            state["intent"] = intent.strip()
        if isinstance(parent_thread_id, str) and parent_thread_id:
            state["parent_thread_id"] = parent_thread_id
        return state

    def _bind_subagent_thread(self, subagent_id: str, thread_id: Optional[str]) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        state = self._ensure_subagent_state(subagent_id)
        thread_ids = state.setdefault("thread_ids", set())
        if isinstance(thread_ids, set):
            thread_ids.add(thread_id)
        self._thread_subagent_ids[thread_id] = subagent_id

    def _subagent_id_for_context(self, thread_id: Optional[str]) -> Optional[str]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        return self._thread_subagent_ids.get(thread_id)

    def _claim_reasoning_source(self, turn_state: Dict[str, Any], source: str) -> bool:
        current = turn_state.get("reason_source")
        if current not in {None, source}:
            return False
        if current is None:
            turn_state["reason_source"] = source
        return True

    def _prepare_reasoning_state(
        self,
        *,
        source: str,
        payload: Dict[str, Any],
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], str]]:
        turn_state = self._get_turn_state(thread_id, turn_id)
        if not self._claim_reasoning_source(turn_state, source):
            return None
        item_id = _reasoning_event_id(payload, turn_state)
        turn_state["reasoning_id"] = item_id
        turn_state["reasoning_started"] = True
        item_state = self._get_item_state(item_id if item_id != "reasoning" else None, thread_id, turn_id)
        item_state["item_type"] = "reasoning"
        return turn_state, item_state, item_id

    def _should_record_reasoning(self, turn_state: Dict[str, Any], item_id: str) -> bool:
        recorded = turn_state.setdefault("reasoning_transcript_ids", set())
        if not isinstance(recorded, set):
            if isinstance(recorded, (list, tuple)):
                recorded = set(recorded)
            else:
                recorded = set()
            turn_state["reasoning_transcript_ids"] = recorded
        if item_id in recorded:
            return False
        recorded.add(item_id)
        return True

    def _reset_reasoning_stream(self, turn_state: Dict[str, Any]) -> None:
        turn_state["reasoning_started"] = False
        turn_state["reasoning_buffer"] = ""
        turn_state["reasoning_id"] = None
        turn_state["thought_buffer"] = ""

    def _plan_update_result(
        self,
        *,
        label_lower: str,
        payload: Dict[str, Any],
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Dict[str, Any]:
        turn_state = self._get_turn_state(thread_id, turn_id)
        steps = normalize_plan_steps(payload.get("plan"))
        explanation_raw = payload.get("explanation")
        explanation = explanation_raw.strip() if isinstance(explanation_raw, str) and explanation_raw.strip() else None
        signature = plan_signature(steps, explanation)
        if turn_state.get("plan_signature") == signature:
            return {"handled": True, "events": [], "transcript_entries": []}

        turn_state["plan_steps"] = steps
        turn_state["plan_signature"] = signature
        turn_state["plan_explanation"] = explanation

        events: List[Dict[str, Any]] = []
        if steps:
            plan_content = render_plan_markdown(steps, explanation)
            events.append({
                "type": "plan_state",
                "has_plan": False,
                "has_todo": True,
                "plan_exists": False,
                "plan_content": plan_content,
                "plan_steps": steps,
            })
            plan_event: Dict[str, Any] = {
                "type": "plan_update",
                "steps": steps,
            }
            if explanation:
                plan_event["explanation"] = explanation
            events.append(plan_event)
            events.append({"type": "activity", "label": "planning", "active": True})
        else:
            events.append({
                "type": "plan_state",
                "has_plan": False,
                "has_todo": True,
                "plan_exists": False,
                "plan_content": "",
                "plan_steps": [],
            })

        active_plan: Optional[Dict[str, Any]]
        if steps:
            active_plan = {
                "steps": steps,
                "turn_id": turn_id,
                "updated_at": utc_ts(),
            }
            if explanation:
                active_plan["explanation"] = explanation
        else:
            active_plan = None

        return {
            "handled": True,
            "events": events,
            "transcript_entries": [],
            "meta_patch": {"active_plan": active_plan},
        }

    def _decorate_event(self, entry: Dict[str, Any], subagent_id: Optional[str]) -> Dict[str, Any]:
        if subagent_id:
            entry["subagent_id"] = subagent_id
        return entry

    def _decorate_transcript_entry(self, entry: Dict[str, Any], subagent_id: Optional[str]) -> Dict[str, Any]:
        if subagent_id:
            entry["subagent_id"] = subagent_id
        return entry

    def _decorate_routed_result(
        self,
        routed: Dict[str, Any],
        *,
        thread_id: Optional[str],
        item_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        subagent_id = None
        if isinstance(item_state, dict):
            candidate = item_state.get("subagent_id")
            if isinstance(candidate, str) and candidate:
                subagent_id = candidate
        if not subagent_id:
            subagent_id = self._subagent_id_for_context(thread_id)
        if not subagent_id:
            return routed

        event_types = {
            "assistant_delta",
            "assistant_finalize",
            "message",
            "tool_begin",
            "tool_delta",
            "tool_end",
            "tool_interaction",
            "command_result",
            "reasoning_delta",
            "reasoning_finalize",
            "diff",
            "approval",
        }
        transcript_roles = {"assistant", "user", "command", "diff", "reasoning"}

        for event in routed.get("events", []):
            if not isinstance(event, dict) or event.get("subagent_id"):
                continue
            if event.get("type") in event_types:
                event["subagent_id"] = subagent_id

        for entry in routed.get("transcript_entries", []):
            if not isinstance(entry, dict) or entry.get("subagent_id"):
                continue
            if entry.get("role") in transcript_roles:
                entry["subagent_id"] = subagent_id

        descriptors = routed.get("approval_descriptors")
        if isinstance(descriptors, list):
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    continue
                render_event = descriptor.get("render_event")
                if isinstance(render_event, dict) and not render_event.get("subagent_id"):
                    render_event["subagent_id"] = subagent_id
        return routed

    def _route_collab_event(
        self,
        protocol: RuntimeProtocol,
        event_type: str,
        payload: Dict[str, Any],
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        event_spec = protocol.event_spec(event_type)
        if event_spec is None or event_spec.category != "collab":
            return None
        fields = _extract_known_fields(event_spec, payload)
        call_id = str(fields.get(event_spec.call_id_field or "call_id") or "").strip()
        if not call_id:
            return {"handled": True, "events": [], "transcript_entries": []}

        prompt = fields.get(event_spec.prompt_field or "prompt")
        prompt_text = prompt.strip() if isinstance(prompt, str) else ""
        sender_thread_id = fields.get(event_spec.sender_thread_field or "sender_thread_id")
        sender_thread_text = sender_thread_id if isinstance(sender_thread_id, str) and sender_thread_id else thread_id
        collab_thread_ids = _collab_thread_ids(fields)
        subagent_id = None
        for candidate in collab_thread_ids:
            subagent_id = self._subagent_id_for_context(candidate)
            if subagent_id:
                break
        if not subagent_id:
            subagent_id = call_id

        name = _subagent_display_name(fields, subagent_id)
        state = self._ensure_subagent_state(
            subagent_id,
            name=name,
            intent=prompt_text,
            parent_thread_id=sender_thread_text,
        )

        bind_thread_ids: List[str] = []
        for candidate in collab_thread_ids:
            self._bind_subagent_thread(subagent_id, candidate)
            if candidate not in bind_thread_ids:
                bind_thread_ids.append(candidate)

        ts = utc_ts()
        events: List[Dict[str, Any]] = []
        transcript_entries: List[Dict[str, Any]] = []

        def emit_start(target_id: str, target_state: Dict[str, Any]) -> None:
            if target_state.get("started") and not target_state.get("ended"):
                return
            target_state["started"] = True
            target_state["ended"] = False
            target_state["active"] = True
            display_name = str(target_state.get("name") or name or "subagent")
            display_intent = str(target_state.get("intent") or prompt_text or "")
            events.append({
                "type": "subagent_start",
                "id": target_id,
                "name": display_name,
                "intent": display_intent,
                "turn_id": turn_id,
                "timestamp": ts,
            })
            transcript_entries.append({
                "role": "subagent_start",
                "id": target_id,
                "name": display_name,
                "intent": display_intent,
                "turn_id": turn_id,
                "timestamp": ts,
            })

        def emit_end(target_id: str, target_state: Dict[str, Any], success: bool, summary: str) -> None:
            if target_state.get("ended"):
                return
            target_state["active"] = False
            target_state["ended"] = True
            events.append({
                "type": "subagent_end",
                "id": target_id,
                "turn_id": turn_id,
                "success": success,
                "summary": summary,
                "timestamp": ts,
            })
            transcript_entries.append({
                "role": "subagent_end",
                "id": target_id,
                "turn_id": turn_id,
                "success": success,
                "summary": summary,
                "timestamp": ts,
            })

        if event_spec.subject == "agent_spawn" and event_spec.phase == "begin":
            events.append({"type": "activity", "label": "spawning subagent", "active": True})
        elif event_spec.subject == "agent_spawn" and event_spec.phase == "end":
            status = fields.get(event_spec.status_field or "status")
            emit_start(subagent_id, state)
            if _agent_status_is_terminal(status):
                emit_end(
                    subagent_id,
                    state,
                    _agent_status_success(status),
                    _subagent_terminal_summary(
                        str(state.get("name") or name or "subagent"),
                        status,
                        success_text="spawn completed",
                        failure_text="spawn failed",
                    ),
                )
        elif event_spec.subject == "agent_interaction" and event_spec.phase == "begin":
            emit_start(subagent_id, state)
        elif event_spec.subject == "agent_interaction" and event_spec.phase == "end":
            status = fields.get(event_spec.status_field or "status")
            display_name = str(state.get("name") or name or "subagent")
            emit_end(
                subagent_id,
                state,
                _agent_status_success(status) if _agent_status_is_terminal(status) else True,
                _subagent_terminal_summary(
                    display_name,
                    status,
                    success_text=f"{display_name} finished",
                    failure_text=f"{display_name} failed",
                ),
            )
        elif event_spec.subject == "close" and event_spec.phase == "end":
            emit_end(subagent_id, state, True, "subagent closed")
        elif event_spec.subject in {"waiting", "resume", "close"} and event_spec.phase == "begin":
            activity_label = event_spec.subject.replace("_", " ")
            events.append({"type": "activity", "label": f"collab: {activity_label}", "active": True})
        elif event_spec.subject in {"waiting", "resume"} and event_spec.phase == "end":
            ended_any = False
            for candidate in collab_thread_ids or [thread_id]:
                if not isinstance(candidate, str) or not candidate:
                    continue
                target_id = self._subagent_id_for_context(candidate) or subagent_id
                target_name = _subagent_display_name(fields, target_id)
                target_state = self._ensure_subagent_state(
                    target_id,
                    name=target_name,
                    intent=prompt_text,
                    parent_thread_id=sender_thread_text,
                )
                status = _collab_status_for_thread(fields, candidate)
                if status is None:
                    continue
                display_name = str(target_state.get("name") or target_name or "subagent")
                emit_end(
                    target_id,
                    target_state,
                    _agent_status_success(status),
                    _subagent_terminal_summary(
                        display_name,
                        status,
                        success_text=f"{display_name} finished",
                        failure_text=f"{display_name} failed",
                    ),
                )
                ended_any = True
            if not ended_any:
                events.append({"type": "activity", "label": "processing", "active": True})

        return {
            "handled": True,
            "events": events,
            "transcript_entries": transcript_entries,
            "bind_thread_ids": bind_thread_ids,
        }

    def _token_usage_result(
        self,
        *,
        label_lower: str,
        payload: Dict[str, Any],
        turn_id: Optional[str],
    ) -> Dict[str, Any]:
        total = None
        input_tokens = None
        cached_input_tokens = None
        context_window = None

        if isinstance(payload.get("info"), dict):
            info = payload["info"]
            usage = info.get("last_token_usage") or {}
            if isinstance(usage, dict):
                total = usage.get("input_tokens")
                input_tokens = usage.get("input_tokens")
                cached_input_tokens = usage.get("cached_input_tokens")
            context_window = info.get("model_context_window")

        if total is None and isinstance(payload.get("tokenUsage"), dict):
            token_usage = payload["tokenUsage"]
            last_breakdown = token_usage.get("last") or {}
            if isinstance(last_breakdown, dict):
                total = last_breakdown.get("inputTokens") or last_breakdown.get("input_tokens")
                input_tokens = last_breakdown.get("inputTokens") or last_breakdown.get("input_tokens")
                cached_input_tokens = last_breakdown.get("cachedInputTokens") or last_breakdown.get("cached_input_tokens")
            context_window = token_usage.get("modelContextWindow") or token_usage.get("model_context_window")

        if total is None:
            total = payload.get("total") or payload.get("total_tokens") or payload.get("tokenCount")
        if context_window is None:
            context_window = payload.get("model_context_window") or payload.get("modelContextWindow")

        if not isinstance(total, (int, float)):
            return {"handled": True, "events": [], "transcript_entries": []}

        total_int = int(total)
        event: Dict[str, Any] = {"type": "token_count", "total": total_int}
        transcript_entry: Dict[str, Any] = {
            "role": "token_usage",
            "total": total_int,
            "event": label_lower,
            "turn_id": turn_id,
        }

        if isinstance(input_tokens, (int, float)) and isinstance(cached_input_tokens, (int, float)):
            active_context = max(0, int(input_tokens) - int(cached_input_tokens))
            event["active_context"] = active_context
            event["input_tokens"] = int(input_tokens)
            event["cached_input_tokens"] = int(cached_input_tokens)
            transcript_entry["active_context"] = active_context
            transcript_entry["input_tokens"] = int(input_tokens)
            transcript_entry["cached_input_tokens"] = int(cached_input_tokens)

        if isinstance(context_window, (int, float)):
            event["context_window"] = int(context_window)
            transcript_entry["context_window"] = int(context_window)

        return {
            "handled": True,
            "events": [event],
            "transcript_entries": [transcript_entry],
        }

    def _collaboration_mode_result(
        self,
        *,
        label_lower: str,
        payload: Dict[str, Any],
        turn_id: Optional[str],
    ) -> Dict[str, Any]:
        raw_kind = payload.get("collaboration_mode_kind") or payload.get("collaborationModeKind")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            return {"handled": True, "events": [], "transcript_entries": []}
        kind = raw_kind.strip()
        event: Dict[str, Any] = {
            "type": "mode",
            "kind": kind,
        }
        transcript_entry: Dict[str, Any] = {
            "role": "mode",
            "kind": kind,
            "turn_id": turn_id,
            "event": label_lower,
        }
        context_window = payload.get("model_context_window") or payload.get("modelContextWindow")
        if isinstance(context_window, (int, float)):
            event["context_window"] = int(context_window)
            transcript_entry["context_window"] = int(context_window)
        return {
            "handled": True,
            "events": [event],
            "transcript_entries": [transcript_entry],
        }

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
        subagent_id = self._subagent_id_for_context(thread_id)
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
            result["events"].append(self._decorate_event({
                "type": "diff",
                "id": diff_id,
                "text": section_text,
                "path": effective_path,
            }, subagent_id))
            result["transcript_entries"].append(self._decorate_transcript_entry({
                "role": "diff",
                "text": section_text,
                "path": effective_path,
                "item_id": diff_id,
                "turn_id": turn_id,
                "event": event_name,
            }, subagent_id))

    def _tool_request_result(
        self,
        *,
        request_id: str,
        kind: str,
        payload: Dict[str, Any],
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_method: Optional[str] = None,
        request_params: Optional[Dict[str, Any]] = None,
        activity_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        subagent_id = self._subagent_id_for_context(thread_id)
        approval_event = {
            "type": "approval",
            "kind": kind,
            "id": request_id,
            "request_id": request_id,
            "payload": payload,
            "request_method": request_method,
            "request_params": dict(request_params or {}),
            "turn_id": turn_id,
            "created_at": utc_ts(),
        }
        if subagent_id:
            approval_event["subagent_id"] = subagent_id
        return {
            "handled": True,
            "events": [
                approval_event,
                {"type": "activity", "label": activity_label or "approval", "active": True},
            ],
            "transcript_entries": [],
            "approval_descriptors": [{
                "request_id": request_id,
                "kind": kind,
                "request_method": request_method,
                "request_params": dict(request_params or {}),
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
        event_spec = protocol.event_spec(event_type) if event_type else None
        notification_spec = protocol.notification_spec(label_lower)
        request_spec = protocol.server_request_spec(label_lower)

        if event_spec is not None and isinstance(payload, dict):
            collab = self._route_collab_event(protocol, event_type, payload, thread_id, turn_id)
            if collab is not None:
                return collab

        if notification_spec and notification_spec.category == "thread" and notification_spec.subject == "thread" and notification_spec.phase == "started":
            thread_obj = payload.get("thread") if isinstance(payload, dict) and isinstance(payload.get("thread"), dict) else {}
            next_thread_id = thread_obj.get("id") if isinstance(thread_obj.get("id"), str) else None
            return {
                "handled": True,
                "events": [{"type": "activity", "label": "thread started", "active": True}],
                "transcript_entries": [],
                "bind_thread_ids": [next_thread_id] if next_thread_id else [],
            }

        if notification_spec and notification_spec.category == "turn" and notification_spec.subject == "turn" and notification_spec.phase == "started" and isinstance(payload, dict):
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
                "meta_patch": {"active_plan": None},
            }

        if (
            (notification_spec and notification_spec.category == "turn" and notification_spec.subject == "plan" and notification_spec.phase == "updated")
            or (event_spec and event_spec.category == "plan" and event_spec.subject == "update")
        ) and isinstance(payload, dict):
            return self._plan_update_result(
                label_lower=label_lower,
                payload=payload,
                thread_id=thread_id,
                turn_id=turn_id,
            )

        if notification_spec and notification_spec.category == "turn" and notification_spec.subject == "turn" and notification_spec.phase == "completed" and isinstance(payload, dict):
            turn_state = self._get_turn_state(thread_id, turn_id)
            plan_steps = turn_state.get("plan_steps")
            plan_explanation = turn_state.get("plan_explanation")
            turn_status, turn_error = _normalize_turn_status(payload)
            if turn_status == "failed":
                ribbon_status = "error"
            elif turn_status == "interrupted":
                ribbon_status = "warning"
            else:
                ribbon_status = "success"
            self._turn_states.pop(self._turn_key(thread_id, turn_id), None)
            events: List[Dict[str, Any]] = [
                {
                    "type": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "error": turn_error,
                },
                {"type": "activity", "label": "idle", "active": False},
            ]
            transcript_entries: List[Dict[str, Any]] = [{
                "role": "status",
                "status": ribbon_status,
                "turn_status": turn_status,
                "turn_id": turn_id,
                "error": turn_error,
                "event": label_lower,
            }]
            if isinstance(plan_steps, list) and plan_steps:
                plan_content = render_plan_markdown(plan_steps, plan_explanation if isinstance(plan_explanation, str) else None)
                events.append({
                    "type": "plan_state",
                    "has_plan": False,
                    "has_todo": True,
                    "plan_exists": False,
                    "plan_content": plan_content,
                    "plan_steps": plan_steps,
                })
                plan_event: Dict[str, Any] = {
                    "type": "plan",
                    "steps": plan_steps,
                }
                plan_entry: Dict[str, Any] = {
                    "role": "plan",
                    "steps": plan_steps,
                    "turn_id": turn_id,
                    "event": label_lower,
                }
                if isinstance(plan_explanation, str) and plan_explanation:
                    plan_event["explanation"] = plan_explanation
                    plan_entry["explanation"] = plan_explanation
                events.append(plan_event)
                transcript_entries.append(plan_entry)
            return {
                "handled": True,
                "clear_turn_id": True,
                "events": events,
                "transcript_entries": transcript_entries,
                "meta_patch": {"active_plan": None},
            }

        if (
            (notification_spec and notification_spec.category == "turn" and notification_spec.subject == "diff" and notification_spec.phase == "updated")
            or label_lower == "codex/event/turn_diff"
        ) and isinstance(payload, dict):
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

        if (
            (notification_spec and notification_spec.category == "thread" and notification_spec.subject == "tokenusage" and notification_spec.phase == "updated")
            or label_lower == "codex/event/token_count"
        ) and isinstance(payload, dict):
            return self._token_usage_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

        if label_lower == "codex/event/task_started" and isinstance(payload, dict):
            return self._collaboration_mode_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "item" and notification_spec.phase == "started" and isinstance(payload, dict):
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

            if item_type == "reasoning":
                turn_state = self._get_turn_state(thread_id, turn_id)
                current_source = turn_state.get("reason_source")
                if current_source in {None, "item"}:
                    turn_state["reason_source"] = current_source or "item"
                    turn_state["reasoning_id"] = item_id or turn_state.get("reasoning_id")
                    turn_state["reasoning_started"] = False
                    turn_state["reasoning_buffer"] = ""
                    turn_state["thought_buffer"] = ""
                item_state.update({
                    "item_type": item_type,
                })
                return {"handled": True, "events": [], "transcript_entries": []}

            if item_type == "commandexecution":
                command = item.get("command") or item.get("parsedCmd") or item.get("cmd") or item.get("argv") or ""
                cwd = item.get("cwd") or ""
                item_state.update({
                    "item_type": item_type,
                    "command": command,
                    "cwd": cwd,
                    "output_buffer": "",
                })
                return self._decorate_routed_result({
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
                }, thread_id=thread_id, item_state=item_state)

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
                return self._decorate_routed_result({
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
                }, thread_id=thread_id, item_state=item_state)

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
                return self._decorate_routed_result({
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
                }, thread_id=thread_id, item_state=item_state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "agentmessage" and notification_spec.phase == "delta" and isinstance(payload, dict):
            delta = payload.get("delta")
            if isinstance(delta, str):
                item_id = _assistant_id(payload, thread_id, turn_id)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        {"type": "assistant_delta", "id": item_id, "delta": delta},
                        {"type": "activity", "label": "responding", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=thread_id)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "plan" and notification_spec.phase == "delta":
            return {"handled": True, "events": [], "transcript_entries": []}

        if notification_spec and notification_spec.category == "item" and isinstance(payload, dict):
            spec_name = notification_spec.name
            if spec_name in {"item/reasoning/summarytextdelta", "item/reasoning/textdelta"}:
                prepared = self._prepare_reasoning_state(
                    source="item",
                    payload=payload,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                delta = payload.get("delta")
                if isinstance(delta, str):
                    turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}{delta}"
                    scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, turn_state)
                    events: List[Dict[str, Any]] = [{"type": "thought", "text": thought} for thought in thoughts]
                    if scrubbed_delta:
                        events.append({"type": "reasoning_delta", "id": item_id, "delta": scrubbed_delta})
                    if thoughts:
                        events.append({"type": "activity", "label": thoughts[-1], "active": True})
                    elif scrubbed_delta:
                        events.append({"type": "activity", "label": "reasoning", "active": True})
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": events,
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                return {"handled": True, "events": [], "transcript_entries": []}

            if spec_name == "item/reasoning/summarypartadded":
                prepared = self._prepare_reasoning_state(
                    source="item",
                    payload=payload,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}\n"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [{"type": "reasoning_delta", "id": item_id, "delta": "\n"}],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "commandexecution" and notification_spec.phase == "outputdelta" and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            delta = _normalize_output(payload.get("delta"))
            if delta:
                state["output_buffer"] = f"{state.get('output_buffer', '')}{delta}"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [{
                        "type": "tool_delta",
                        "id": item_id or _assistant_id(payload, thread_id, turn_id),
                        "tool": "command",
                        "delta": delta,
                    }],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=state)
            return {"handled": True, "events": [], "transcript_entries": []}

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "filechange" and notification_spec.phase == "outputdelta" and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            delta = _normalize_output(payload.get("delta"))
            if delta:
                state["output_buffer"] = f"{state.get('output_buffer', '')}{delta}"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [{
                        "type": "tool_delta",
                        "id": item_id or _assistant_id(payload, thread_id, turn_id),
                        "tool": "apply_patch",
                        "delta": delta,
                    }],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=state)
            return {"handled": True, "events": [], "transcript_entries": []}

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "commandexecution" and notification_spec.phase == "terminalinteraction" and isinstance(payload, dict):
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else payload.get("item_id")
            return self._decorate_routed_result({
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
            }, thread_id=thread_id)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "item" and notification_spec.phase == "completed" and isinstance(payload, dict):
            item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
            if not isinstance(item, dict):
                return result
            item_type = _item_type(item)
            item_id = item.get("id") if isinstance(item.get("id"), str) else None
            item_state = self._clear_item_state(item_id)

            if extract_item_text:
                entry = extract_item_text(item)
                if entry and entry.get("role") == "assistant":
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [{"type": "assistant_finalize", "id": item_id or _assistant_id(item, thread_id, turn_id), "text": entry["text"]}],
                    "transcript_entries": [{
                            "role": "assistant",
                            "text": entry["text"],
                            "item_id": item_id,
                            "turn_id": turn_id,
                            "event": label_lower,
                        }],
                    }, thread_id=thread_id, item_state=item_state)

            if item_type == "reasoning":
                turn_state = self._get_turn_state(thread_id, turn_id)
                effective_id = item_id or _reasoning_event_id(item, turn_state)
                text = _extract_reasoning_text(item, fallback=turn_state.get("reasoning_buffer"))
                scrubbed_text, thoughts = _extract_and_scrub_thoughts(text) if text else ("", [])
                should_finalize_live = turn_state.get("reason_source") in {None, "item"} and (
                    bool(scrubbed_text) or bool(turn_state.get("reasoning_started"))
                )
                events: List[Dict[str, Any]] = []
                if should_finalize_live:
                    events.extend({"type": "thought", "text": thought} for thought in thoughts)
                    events.append({"type": "reasoning_finalize", "id": effective_id, "text": scrubbed_text})
                transcript_entries: List[Dict[str, Any]] = []
                if scrubbed_text and self._should_record_reasoning(turn_state, effective_id):
                    transcript_entries.append({
                        "role": "reasoning",
                        "text": scrubbed_text,
                        "item_id": effective_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    })
                if turn_state.get("reason_source") != "codex":
                    self._reset_reasoning_stream(turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": events,
                    "transcript_entries": transcript_entries,
                }, thread_id=thread_id, item_state=item_state)

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
                return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)

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
                return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)

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
                return self._decorate_routed_result({
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
                }, thread_id=thread_id, item_state=item_state)

            return {"handled": True, "events": [], "transcript_entries": []}

        if request_spec and request_spec.category == "item" and request_spec.subject == "tool" and request_spec.phase == "call" and isinstance(payload, dict):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            request_id_text = str(request_id or "").strip()
            tool_id = payload.get("callId") or payload.get("call_id") or payload.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = _assistant_id(payload, thread_id, turn_id)
            tool_name = payload.get("tool") or "tool"
            arguments = payload.get("arguments")
            item_state = self._get_item_state(tool_id, thread_id, turn_id)
            item_state.update({
                "item_type": "tool",
                "tool": tool_name,
                "arguments": arguments,
            })
            payload_data = {
                "tool": tool_name,
                "call_id": payload.get("callId") or payload.get("call_id"),
                "arguments": arguments,
            }
            return self._decorate_routed_result(self._tool_request_result(
                request_id=request_id_text or str(tool_id or ""),
                kind="tool_call",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
                request_method=request_spec.name,
                request_params=dict(payload),
                activity_label="request",
            ), thread_id=thread_id, item_state=item_state)

        if request_spec and request_spec.category == "item" and request_spec.subject == "tool" and request_spec.phase == "requestuserinput" and isinstance(payload, dict):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            item_id = payload.get("itemId") or payload.get("item_id")
            request_id_text = str(request_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            payload_data = {
                "questions": payload.get("questions"),
                "message": payload.get("message"),
            }
            return self._decorate_routed_result(self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="request_user_input",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
                request_method=request_spec.name,
                request_params=dict(payload),
                activity_label="request",
            ), thread_id=thread_id, item_state=item_state)

        if request_spec and request_spec.category == "item" and request_spec.subject == "commandexecution" and request_spec.phase == "requestapproval" and isinstance(payload, dict):
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
                "approval_id": payload.get("approvalId"),
                "available_decisions": payload.get("availableDecisions"),
                "command_actions": payload.get("commandActions"),
                "additional_permissions": payload.get("additionalPermissions"),
                "network_approval_context": payload.get("networkApprovalContext"),
                "proposed_execpolicy_amendment": payload.get("proposedExecpolicyAmendment"),
                "proposed_network_policy_amendments": payload.get("proposedNetworkPolicyAmendments"),
            }
            return self._decorate_routed_result(self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="command",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
                request_method=request_spec.name,
                request_params=dict(payload),
            ), thread_id=thread_id, item_state=item_state)

        if request_spec and request_spec.category == "item" and request_spec.subject == "filechange" and request_spec.phase == "requestapproval" and isinstance(payload, dict):
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
                "grant_root": payload.get("grantRoot"),
            }
            return self._decorate_routed_result(self._tool_request_result(
                request_id=request_id_text or str(item_id or ""),
                kind="diff",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
                request_method=request_spec.name,
                request_params=dict(payload),
            ), thread_id=thread_id, item_state=item_state)

        if request_spec and request_spec.name.lower() == "mcpserver/elicitation/request" and isinstance(payload, dict):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id") or payload.get("elicitationId")
            request_id_text = str(request_id or "").strip()
            payload_data = {
                "server_name": payload.get("serverName") or payload.get("server_name"),
                "message": payload.get("message"),
                "mode": payload.get("mode"),
                "url": payload.get("url"),
                "requested_schema": payload.get("requestedSchema"),
                "elicitation_id": payload.get("elicitationId") or payload.get("elicitation_id"),
            }
            return self._decorate_routed_result(self._tool_request_result(
                request_id=request_id_text or str(payload.get("elicitationId") or ""),
                kind="elicitation",
                payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
                thread_id=thread_id,
                turn_id=turn_id,
                request_method=request_spec.name,
                request_params=dict(payload),
                activity_label="request",
            ), thread_id=thread_id)

        if event_spec and event_spec.category == "user" and event_spec.subject == "message" and isinstance(payload, dict):
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

        if event_spec and isinstance(payload, dict):
            if (
                (
                    event_spec.category == "agent"
                    and event_spec.subject in {"reasoning", "reasoning_raw_content"}
                    and event_spec.phase == "delta"
                )
                or (
                    event_spec.category == "reasoning"
                    and event_spec.subject in {"content", "raw_content"}
                    and event_spec.phase == "delta"
                )
            ):
                prepared = self._prepare_reasoning_state(
                    source="codex",
                    payload=payload,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                delta = payload.get("delta")
                if isinstance(delta, str):
                    turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}{delta}"
                    scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, turn_state)
                    events: List[Dict[str, Any]] = [{"type": "thought", "text": thought} for thought in thoughts]
                    if scrubbed_delta:
                        events.append({"type": "reasoning_delta", "id": item_id, "delta": scrubbed_delta})
                    if thoughts:
                        events.append({"type": "activity", "label": thoughts[-1], "active": True})
                    elif scrubbed_delta:
                        events.append({"type": "activity", "label": "reasoning", "active": True})
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": events,
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                return {"handled": True, "events": [], "transcript_entries": []}

            if event_spec.category == "agent" and event_spec.subject == "reasoning_section_break":
                prepared = self._prepare_reasoning_state(
                    source="codex",
                    payload=payload,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}\n\n"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [{"type": "reasoning_delta", "id": item_id, "delta": "\n\n"}],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)

            if event_spec.category == "agent" and event_spec.subject in {"reasoning", "reasoning_raw_content"} and event_spec.phase is None:
                turn_state = self._get_turn_state(thread_id, turn_id)
                if not self._claim_reasoning_source(turn_state, "codex"):
                    return {"handled": True, "events": [], "transcript_entries": []}
                effective_id = _reasoning_event_id(payload, turn_state)
                turn_state["reasoning_id"] = effective_id
                item_state = self._get_item_state(
                    effective_id if effective_id != "reasoning" else None,
                    thread_id,
                    turn_id,
                )
                item_state["item_type"] = "reasoning"
                text = _extract_reasoning_text(payload, fallback=turn_state.get("reasoning_buffer"))
                scrubbed_text, thoughts = _extract_and_scrub_thoughts(text) if text else ("", [])
                should_finalize_live = turn_state.get("reason_source") in {None, "codex"} and (
                    bool(scrubbed_text) or bool(turn_state.get("reasoning_started"))
                )
                events: List[Dict[str, Any]] = []
                if should_finalize_live:
                    events.extend({"type": "thought", "text": thought} for thought in thoughts)
                    events.append({"type": "reasoning_finalize", "id": effective_id, "text": scrubbed_text})
                transcript_entries: List[Dict[str, Any]] = []
                if scrubbed_text and self._should_record_reasoning(turn_state, effective_id):
                    transcript_entries.append({
                        "role": "reasoning",
                        "text": scrubbed_text,
                        "item_id": effective_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    })
                self._reset_reasoning_stream(turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": events,
                    "transcript_entries": transcript_entries,
                }, thread_id=thread_id, item_state=item_state)

        if event_spec and event_spec.category == "agent" and event_spec.subject in {"message", "message_content"} and event_spec.phase == "delta" and isinstance(payload, dict):
            delta = payload.get("delta")
            if isinstance(delta, str):
                item_id = _assistant_id(payload, thread_id, turn_id)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        {"type": "assistant_delta", "id": item_id, "delta": delta},
                        {"type": "activity", "label": "responding", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=thread_id)

        if event_spec and event_spec.category == "agent" and event_spec.subject == "message" and event_spec.phase is None and isinstance(payload, dict):
            text = _direct_event_text(payload)
            if text:
                item_id = _assistant_id(payload, thread_id, turn_id)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [{"type": "assistant_finalize", "id": item_id, "text": text}],
                    "transcript_entries": [{
                        "role": "assistant",
                        "text": text,
                        "item_id": payload.get("item_id") or payload.get("itemId"),
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }, thread_id=thread_id)

        if event_spec and event_spec.phase == "request" and isinstance(payload, dict) and (
            (event_spec.category == "exec" and event_spec.subject == "approval")
            or (event_spec.category == "apply" and event_spec.subject == "patch_approval")
        ):
            # These codex/event wrappers mirror approval context but do not carry the actionable
            # JSON-RPC request id; only item/*/requestApproval should create live approval cards.
            return {"handled": True, "events": [], "transcript_entries": []}

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

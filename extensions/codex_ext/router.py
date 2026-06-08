import difflib
import hashlib
import json
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple, TypeGuard, TypedDict, cast

from ..message_card_contracts import (
    build_assistant_delta_event,
    build_assistant_finalize_event,
    build_message_event,
    build_message_transcript_entry,
    build_reasoning_delta_event,
    build_reasoning_finalize_event,
    build_reasoning_transcript_entry,
    build_thought_event,
)
from .plan_utils import normalize_plan_steps, plan_signature, render_plan_markdown
from .runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol
from ..tool_card_contracts import build_tool_card_request, build_tool_card_response

ObjectDict = Dict[str, object]

AGENT_PTY_ASK_USER_REQUEST_METHOD = "agent-pty/ask-user"
AGENT_PTY_ASK_USER_SERVER = "agent-pty-blocks"
AGENT_PTY_ASK_USER_TOOL = "ask_user"


class NormalizedAskUserRequest(TypedDict):
    question: str
    choices: List[str]
    allow_freeform: bool


def _is_object_dict(value: object) -> TypeGuard[ObjectDict]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[List[object]]:
    return isinstance(value, list)


def _ask_user_object_dict(value: object) -> ObjectDict:
    return _dict_payload(value)


def normalize_ask_user_choices(value: object) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not _is_object_list(value):
        return []
    normalized: List[str] = []
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


def normalize_ask_user_request(question: object, choices: object, allow_freeform: object) -> NormalizedAskUserRequest:
    return {
        "question": str(question or "").strip(),
        "choices": normalize_ask_user_choices(choices),
        "allow_freeform": bool(allow_freeform),
    }


def is_agent_pty_ask_user_tool(server_name: object, tool_name: object) -> bool:
    return (
        str(server_name or "").strip() == AGENT_PTY_ASK_USER_SERVER
        and str(tool_name or "").strip() == AGENT_PTY_ASK_USER_TOOL
    )


def is_agent_pty_ask_user_request(tool_name: object, arguments: object) -> bool:
    if str(tool_name or "").strip() != AGENT_PTY_ASK_USER_TOOL:
        return False
    arguments_map = _ask_user_object_dict(arguments)
    if not arguments_map:
        return False
    normalized = normalize_ask_user_request(
        arguments_map.get("question"),
        arguments_map.get("choices"),
        arguments_map.get("allow_freeform", arguments_map.get("allowFreeform", True)),
    )
    return bool(normalized["question"] and (normalized["choices"] or normalized["allow_freeform"]))


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_type_from_label(label_lower: str) -> Optional[str]:
    if label_lower.startswith("codex/event/"):
        return label_lower.split("codex/event/", 1)[-1]
    return None


def _extract_known_fields(spec: Optional[ProtocolSemanticSpec], payload: ObjectDict) -> ObjectDict:
    if spec is None:
        return {}
    fields: ObjectDict = {}
    for key in spec.properties:
        value = payload.get(key)
        if value is not None:
            fields[key] = value
    return fields


def _dict_payload(value: object) -> ObjectDict:
    if not isinstance(value, Mapping):
        return {}
    result: ObjectDict = {}
    for key, item_value in cast(Iterable[tuple[object, object]], value.items()):
        result[str(key)] = item_value
    return result


def _dict_list(value: object) -> List[ObjectDict]:
    if not _is_object_list(value):
        return []
    return [_dict_payload(item) for item in value if _is_object_dict(item)]


def _string_list(value: object) -> List[str]:
    if not _is_object_list(value):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _ensure_dict_list(container: ObjectDict, key: str) -> List[ObjectDict]:
    value = container.get(key)
    if _is_object_list(value):
        if all(_is_object_dict(item) for item in value):
            return cast(List[ObjectDict], value)
    items: List[ObjectDict] = []
    container[key] = items
    return items


def _string_value(value: object, default: str = "") -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _subagent_display_name(fields: ObjectDict, call_id: str) -> str:
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


def _collab_agent_records(fields: ObjectDict) -> List[ObjectDict]:
    records: List[ObjectDict] = []
    for key in ("receiver_agents", "agent_statuses"):
        value = fields.get(key)
        if not _is_object_list(value):
            continue
        for item in value:
            if _is_object_dict(item):
                records.append(item)
    return records


def _collab_thread_ids(fields: ObjectDict) -> List[str]:
    thread_ids: List[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in thread_ids:
            thread_ids.append(value)

    add(fields.get("new_thread_id"))
    add(fields.get("receiver_thread_id"))

    receiver_thread_ids = fields.get("receiver_thread_ids")
    if _is_object_list(receiver_thread_ids):
        for item in receiver_thread_ids:
            add(item)

    for record in _collab_agent_records(fields):
        add(record.get("thread_id"))

    statuses = fields.get("statuses")
    if _is_object_dict(statuses):
        for candidate in statuses.keys():
            add(candidate)

    return thread_ids


def _collab_status_for_thread(fields: ObjectDict, thread_id: str) -> object:
    for record in _collab_agent_records(fields):
        if record.get("thread_id") == thread_id and record.get("status") is not None:
            return record.get("status")

    statuses = fields.get("statuses")
    if _is_object_dict(statuses):
        status = statuses.get(thread_id)
        if status is not None:
            return status

    return fields.get("status")


def _subagent_terminal_summary(name: str, status: object, *, success_text: str, failure_text: str) -> str:
    if _is_object_dict(status):
        status_map = status
        errored = status_map.get("errored")
        if isinstance(errored, str) and errored.strip():
            return f"Failed: {errored.strip()}"
    if status == "shutdown":
        return "subagent shutdown"
    if status == "not_found":
        return "subagent not found"
    return success_text if _agent_status_success(status) else failure_text


def _agent_status_is_terminal(status: object) -> bool:
    if _is_object_dict(status):
        status_map = status
        return "completed" in status_map or "errored" in status_map
    return status in {"shutdown", "not_found"}


def _agent_status_success(status: object) -> bool:
    if _is_object_dict(status):
        return "completed" in status
    return False


def _direct_event_text(payload: ObjectDict) -> Optional[str]:
    text = payload.get("message")
    if not isinstance(text, str):
        text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    text_elements = payload.get("text_elements") or payload.get("textElements")
    if _is_object_list(text_elements):
        parts = [part for part in text_elements if isinstance(part, str) and part.strip()]
        if parts:
            return "\n".join(parts).strip()
    return None


def _notification_text(payload: ObjectDict) -> Optional[str]:
    error_value = payload.get("error")
    if isinstance(error_value, str) and error_value.strip():
        return error_value.strip()
    if _is_object_dict(error_value):
        for key in ("message", "details", "summary", "error"):
            candidate = error_value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

    warning_value = payload.get("warning")
    if isinstance(warning_value, str) and warning_value.strip():
        return warning_value.strip()

    for key in ("message", "text", "summary", "detail", "details", "reason", "status_text", "statusText"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    return _direct_event_text(payload)


def _notification_severity(
    label_lower: str,
    notification_spec: Optional[ProtocolSemanticSpec],
    event_spec: Optional[ProtocolSemanticSpec],
    payload: ObjectDict,
) -> Optional[str]:
    label_text = label_lower.lower()
    if "interrupt" in label_text:
        return "warning"

    tokens: List[str] = []
    for spec in (notification_spec, event_spec):
        if spec is None:
            continue
        for candidate in (spec.category, spec.subject, spec.phase):
            if isinstance(candidate, str) and candidate.strip():
                tokens.append(candidate.strip().lower())

    for key in ("level", "severity", "status", "type", "kind"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            tokens.append(value.strip().lower())

    error_value = payload.get("error")
    if isinstance(error_value, str) or _is_object_dict(error_value):
        tokens.append("error")
    if _is_object_dict(error_value):
        for key in ("type", "kind", "level", "severity", "status", "error_type", "errorType"):
            value = error_value.get(key)
            if isinstance(value, str) and value.strip():
                tokens.append(value.strip().lower())

    if "error" in label_text or "failed" in label_text or "fatal" in label_text:
        return "error"
    if any(token in {"error", "failed", "failure", "fatal"} for token in tokens):
        return "error"

    if "warning" in label_text or "warn" in label_text:
        return "warning"
    if any(token in {"warning", "warn", "interrupted"} for token in tokens):
        return "warning"

    return None


def _append_text_parts(parts: List[str], value: object) -> None:
    if isinstance(value, str):
        text = _normalize_output(value).strip()
        if text:
            parts.append(text)
        return
    if _is_object_list(value):
        for item in value:
            _append_text_parts(parts, item)
        return
    if _is_object_dict(value):
        for key in ("text", "summary", "content", "message"):
            candidate = value.get(key)
            if candidate is None:
                continue
            before = len(parts)
            _append_text_parts(parts, candidate)
            if len(parts) != before:
                return


def _extract_reasoning_text(item: ObjectDict, fallback: Optional[str] = None) -> Optional[str]:
    parts: List[str] = []
    for key in ("summary", "summary_text", "summaryText", "text", "raw_content", "rawContent", "content"):
        _append_text_parts(parts, item.get(key))
        if parts:
            break
    if not parts and fallback:
        _append_text_parts(parts, fallback)
    text = "\n".join(part for part in parts if part).strip()
    return text or None


def _reasoning_event_id(payload: ObjectDict, turn_state: ObjectDict) -> str:
    for key in ("item_id", "itemId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    current = turn_state.get("reasoning_id")
    if isinstance(current, str) and current:
        return current
    return "reasoning"


def _assistant_id(payload: ObjectDict, thread_id: Optional[str], turn_id: Optional[str]) -> str:
    item = _dict_payload(payload.get("item"))
    item_id = item.get("id")
    if isinstance(item_id, str):
        return item_id
    for key in ("item_id", "itemId", "id", "callId", "call_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if turn_id:
        return f"assistant_{turn_id}"
    if thread_id:
        return f"assistant_{thread_id}"
    return "assistant"


def _payload_string(payload: ObjectDict, *keys: str) -> Optional[str]:
    sources: List[ObjectDict] = [payload]
    item = payload.get("item")
    if _is_object_dict(item):
        sources.append(item)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _payload_thread_id(payload: ObjectDict, fallback: Optional[str]) -> Optional[str]:
    return _payload_string(payload, "thread_id", "threadId") or fallback


def _payload_turn_id(payload: ObjectDict, fallback: Optional[str]) -> Optional[str]:
    return _payload_string(payload, "turn_id", "turnId") or fallback


def _normalize_turn_status(payload: ObjectDict) -> tuple[str, Optional[str]]:
    turn_value = payload.get("turn")
    turn_obj = turn_value if _is_object_dict(turn_value) else {}
    status = turn_obj.get("status")
    if _is_object_dict(status):
        turn_status = str(status.get("type") or status.get("status") or "completed")
    elif isinstance(status, str):
        turn_status = status
    else:
        turn_status = str(payload.get("status") or "completed")
    turn_error = turn_obj.get("error") if turn_obj else payload.get("error")
    if not isinstance(turn_error, str):
        turn_error = None
    return turn_status, turn_error


def _item_type(item: ObjectDict) -> str:
    return str(item.get("type") or "").strip().lower()


def _normalize_output(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _stringify_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_output(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _duration_ms(value: object) -> Optional[int]:
    if _is_object_dict(value):
        secs = value.get("secs")
        nanos = value.get("nanos")
        if isinstance(secs, (int, float)) or isinstance(nanos, (int, float)):
            secs_value = secs if isinstance(secs, (int, float)) else 0
            nanos_value = nanos if isinstance(nanos, (int, float)) else 0
            return int(secs_value) * 1000 + int(nanos_value) // 1_000_000
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _extract_tool_result(value: object, *, status: str, error: object = None) -> Tuple[object, bool]:
    is_error = bool(error) or status in {"failed", "error"}
    if error is not None:
        return {"error": error}, True
    if _is_object_dict(value):
        is_error_value = value.get("isError")
        if isinstance(is_error_value, bool):
            is_error = is_error_value or is_error
        structured = value.get("structuredContent")
        if _is_object_dict(structured) and structured.get("result") is not None:
            return structured.get("result"), is_error
        content = value.get("content")
        if _is_object_list(content):
            for item in content:
                if not _is_object_dict(item):
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    parsed = cast(object, json.loads(text))
                    return parsed, is_error
                except (json.JSONDecodeError, TypeError, ValueError):
                    return _normalize_output(text), is_error
    return value, is_error


def _command_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if _is_object_list(value):
        return " ".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _resolve_view_path(path: str, cwd: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path) or not cwd:
        return path
    return os.path.normpath(os.path.join(cwd, path))


def _build_view_title(path: str, view_range: Optional[List[int]]) -> str:
    short_path = os.path.basename(path) if path else "view"
    if _is_object_list(view_range) and len(view_range) >= 2:
        return f"{short_path}  Lines {view_range[0]}–{view_range[1]}"
    if _is_object_list(view_range) and len(view_range) == 1:
        return f"{short_path}  Line {view_range[0]}+"
    return short_path


def _view_spec_path(spec: ObjectDict) -> str:
    path = spec.get("path")
    return path if isinstance(path, str) else ""


def _view_spec_range(spec: ObjectDict) -> Optional[List[int]]:
    value = spec.get("view_range")
    if not _is_object_list(value):
        return None
    result: List[int] = []
    for item in value:
        if not isinstance(item, (int, float)):
            return None
        result.append(int(item))
    return result


def _view_spec_title(spec: ObjectDict) -> str:
    title = spec.get("title")
    if isinstance(title, str) and title:
        return title
    return _build_view_title(_view_spec_path(spec), _view_spec_range(spec))


_CODEX_VIEW_LINE_RE = re.compile(r"^\s*(\d+):(.*)$")
_CODEX_NL_VIEW_LINE_RE = re.compile(r"^\s*(\d+)\t(.*)$")


def _parse_codex_view_lines(content: str) -> Optional[List[ObjectDict]]:
    if not content:
        return []

    parsed: List[ObjectDict] = []
    for raw_line in content.splitlines():
        match = _CODEX_VIEW_LINE_RE.match(raw_line)
        if match:
            line_content = match.group(2)
            if line_content[:1] in {" ", "\t"}:
                line_content = line_content[1:]
            parsed.append({
                "line_no": int(match.group(1)),
                "content": line_content,
            })
            continue
        nl_match = _CODEX_NL_VIEW_LINE_RE.match(raw_line)
        if nl_match:
            parsed.append({
                "line_no": int(nl_match.group(1)),
                "content": nl_match.group(2),
            })
            continue
        return None
    return parsed


def _build_codex_view_lines(content: str, view_spec: Optional[ObjectDict] = None) -> Optional[List[ObjectDict]]:
    parsed = _parse_codex_view_lines(content)
    if parsed is not None:
        return parsed
    if not content:
        return []
    if not _is_object_dict(view_spec):
        return None

    start_line = 1
    raw_view_range = view_spec.get("view_range")
    if _is_object_list(raw_view_range) and raw_view_range:
        start_value = raw_view_range[0]
        if not isinstance(start_value, (int, float, str)):
            return None
        try:
            start_line = int(start_value)
        except (TypeError, ValueError):
            return None

    return [
        {
            "line_no": start_line + idx,
            "content": raw_line,
        }
        for idx, raw_line in enumerate(content.splitlines())
    ]


def _unwrap_single_shell_command(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return text
    if len(tokens) >= 3 and tokens[0] in {"sh", "/bin/sh", "bash", "/bin/bash"} and tokens[1] in {"-c", "-lc"}:
        return str(tokens[2] or "").strip()
    return text


def _last_non_flag_token(tokens: List[str], start: int = 1) -> Optional[str]:
    candidate: Optional[str] = None
    for token in tokens[start:]:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        candidate = token
    return candidate


def _parse_sed_view_range(range_token: object) -> Optional[List[int]]:
    text = str(range_token or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d+),(\d+)p", text)
    if m:
        return [int(m.group(1)), int(m.group(2))]
    m = re.fullmatch(r"(\d+)p", text)
    if m:
        return [int(m.group(1))]
    return None


def _shell_pipeline_to_view_spec(tokens: List[str], cwd: str = "") -> Optional[ObjectDict]:
    if tokens.count("|") != 1:
        return None
    pipe_index = tokens.index("|")
    if pipe_index <= 0 or pipe_index >= len(tokens) - 1:
        return None

    left = tokens[:pipe_index]
    right = tokens[pipe_index + 1 :]
    if not left or not right:
        return None
    if left[0] != "nl" or "-ba" not in left[1:]:
        return None
    if right[0] != "sed" or len(right) < 3 or right[1] != "-n":
        return None

    path = _last_non_flag_token(left)
    if not path or path.startswith("-"):
        return None
    if len(right) > 3 and any(token != "--" for token in right[3:]):
        return None

    view_range = _parse_sed_view_range(right[2])
    if view_range is None:
        return None

    resolved_path = _resolve_view_path(path, cwd)
    return {
        "path": resolved_path,
        "view_range": view_range,
        "title": _build_view_title(resolved_path, view_range),
    }


def _command_tokens_to_view_spec(tokens: List[str], cwd: str = "") -> Optional[ObjectDict]:
    if not tokens:
        return None
    if any(token in {"&&", "||", ";", ">", "<"} for token in tokens):
        return None
    if "|" in tokens:
        return _shell_pipeline_to_view_spec(tokens, cwd)

    cmd = tokens[0]
    path: Optional[str] = None
    view_range: Optional[List[int]] = None

    if cmd == "sed":
        if len(tokens) < 4 or tokens[1] != "-n":
            return None
        range_token = tokens[2]
        path = tokens[-1] if len(tokens) >= 4 else None
        if not path or path.startswith("-"):
            return None
        view_range = _parse_sed_view_range(range_token)
    elif cmd in {"cat", "less", "more"}:
        path = _last_non_flag_token(tokens)
    elif cmd == "bat":
        path = _last_non_flag_token(tokens)
    elif cmd == "head":
        path = _last_non_flag_token(tokens)
        for idx, token in enumerate(tokens[1:], start=1):
            if token == "-n" and idx + 1 < len(tokens):
                try:
                    view_range = [1, int(tokens[idx + 1])]
                except (TypeError, ValueError):
                    view_range = None
                break
            if token.startswith("-n") and len(token) > 2:
                try:
                    view_range = [1, int(token[2:])]
                except (TypeError, ValueError):
                    view_range = None
                break
    elif cmd == "tail":
        path = _last_non_flag_token(tokens)
    else:
        return None

    if not path or path.startswith("-"):
        return None
    resolved_path = _resolve_view_path(path, cwd)
    return {
        "path": resolved_path,
        "view_range": view_range,
        "title": _build_view_title(resolved_path, view_range),
    }


def _split_shell_tokens_on_and(tokens: List[str]) -> Optional[List[List[str]]]:
    if not tokens:
        return None
    segments: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token == "&&":
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        if token in {"||", ";"}:
            return None
        current.append(token)
    if not current:
        return None
    segments.append(current)
    return segments


def _separator_tokens_to_text(tokens: List[str]) -> Optional[str]:
    if len(tokens) != 3 or tokens[0] != "printf":
        return None
    if tokens[1] not in {"%s\\n", "%s\n"}:
        return None
    divider = str(tokens[2] or "")
    return divider or None


def _shell_command_to_view_sequence(command: object, cwd: str = "") -> Optional[ObjectDict]:
    inner = _unwrap_single_shell_command(_command_text(command))
    if not inner or "\n" in inner:
        return None
    try:
        tokens = shlex.split(inner, posix=True)
    except ValueError:
        return None
    if not tokens or "&&" not in tokens:
        return None
    segments = _split_shell_tokens_on_and(tokens)
    if not segments or len(segments) < 3 or len(segments) % 2 == 0:
        return None

    specs: List[ObjectDict] = []
    divider_text: Optional[str] = None
    for idx, segment in enumerate(segments):
        if idx % 2 == 0:
            spec = _command_tokens_to_view_spec(segment, cwd)
            if spec is None:
                return None
            specs.append(spec)
            continue
        divider = _separator_tokens_to_text(segment)
        if divider is None:
            return None
        if divider_text is None:
            divider_text = divider
        elif divider_text != divider:
            return None

    if len(specs) < 2 or not divider_text:
        return None
    return {
        "separator": divider_text,
        "specs": specs,
    }


def _split_view_output_by_divider(output: str, divider: str, expected_parts: int) -> Optional[List[str]]:
    if not divider or expected_parts <= 0:
        return None
    parts: List[str] = []
    current: List[str] = []
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.splitlines(keepends=True):
        stripped = line[:-1] if line.endswith("\n") else line
        if stripped == divider:
            parts.append("".join(current))
            current = []
            continue
        current.append(line)
    parts.append("".join(current))
    if len(parts) != expected_parts:
        return None
    return parts


def _shell_command_to_view_spec(command: object, cwd: str = "") -> Optional[ObjectDict]:
    inner = _unwrap_single_shell_command(_command_text(command))
    if not inner or any(marker in inner for marker in ("\n", "&&", "||", ";")):
        return None
    try:
        tokens = shlex.split(inner, posix=True)
    except ValueError:
        return None
    return _command_tokens_to_view_spec(tokens, cwd)


def _normalized_new_file_text(value: object) -> str:
    text = str(value or "")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _build_new_file_diff(path: str, content: str) -> str:
    resolved_path = str(path or "").strip() or "new_file"
    diff = "".join(
        difflib.unified_diff(
            [],
            _normalized_new_file_text(content).splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=resolved_path,
        )
    )
    if diff:
        return diff
    return f"--- /dev/null\n+++ {resolved_path}\n"


def _parse_cat_heredoc_line(tokens: List[str]) -> Optional[Tuple[str, str]]:
    if len(tokens) != 4 or tokens[0] != "cat" or tokens[1] != ">" or not tokens[3].startswith("<<"):
        return None
    path_token = str(tokens[2] or "").strip()
    terminator = str(tokens[3][2:] or "").strip()
    if not path_token or path_token.startswith("-") or not terminator:
        return None
    return path_token, terminator


def _parse_mkdir_p_directories(tokens: List[str], cwd: str = "") -> Optional[List[str]]:
    if not tokens or tokens[0] != "mkdir":
        return None
    directories: List[str] = []
    for token in tokens[1:]:
        if token in {"-p", "--parents", "--"}:
            continue
        if token.startswith("-"):
            return None
        resolved = _resolve_view_path(token, cwd)
        if resolved not in directories:
            directories.append(resolved)
    return directories or None


def _parse_new_file_command_preamble(lines: List[str], cwd: str = "") -> Optional[Tuple[int, str, str, List[str]]]:
    created_dirs: List[str] = []
    for idx, raw_line in enumerate(lines):
        if not raw_line.strip():
            continue
        try:
            tokens = shlex.split(raw_line, posix=True)
        except ValueError:
            return None
        if not tokens:
            continue
        mkdir_dirs = _parse_mkdir_p_directories(tokens, cwd)
        if mkdir_dirs is not None:
            for directory in mkdir_dirs:
                if directory not in created_dirs:
                    created_dirs.append(directory)
            continue
        cat_spec = _parse_cat_heredoc_line(tokens)
        if cat_spec is None:
            return None
        path_token, terminator = cat_spec
        return idx, path_token, terminator, created_dirs
    return None


def _new_file_arguments(spec: ObjectDict) -> ObjectDict:
    arguments: ObjectDict = {}
    path = spec.get("path")
    if isinstance(path, str) and path:
        arguments["path"] = path
    directory = spec.get("directory")
    if isinstance(directory, str) and directory:
        arguments["directory"] = directory
        return arguments
    directories = spec.get("directories")
    if _is_object_list(directories):
        normalized = [value for value in directories if isinstance(value, str) and value]
        if len(normalized) == 1:
            arguments["directory"] = normalized[0]
        elif normalized:
            arguments["directories"] = normalized
    return arguments


def _shell_command_to_new_file_spec(command: object, cwd: str = "") -> Optional[ObjectDict]:
    inner = _unwrap_single_shell_command(_command_text(command))
    if not inner or "\n" not in inner:
        return None
    normalized = inner.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if len(lines) < 3:
        return None
    preamble = _parse_new_file_command_preamble(lines, cwd)
    if preamble is None:
        return None
    cat_line_idx, path_token, terminator, created_dirs = preamble
    end_idx: Optional[int] = None
    for idx, line in enumerate(lines[cat_line_idx + 1 :], start=cat_line_idx + 1):
        if line == terminator:
            end_idx = idx
            break
    if end_idx is None or end_idx <= 0:
        return None
    if any(line.strip() for line in lines[end_idx + 1 :]):
        return None
    resolved_path = _resolve_view_path(path_token, cwd)
    content = "\n".join(lines[cat_line_idx + 1 : end_idx])
    spec: ObjectDict = {
        "path": resolved_path,
        "content": content,
        "diff": _build_new_file_diff(resolved_path, content),
        "new_file": True,
    }
    if len(created_dirs) == 1:
        spec["directory"] = created_dirs[0]
    elif created_dirs:
        spec["directories"] = created_dirs
    return spec


def _shell_command_to_search_spec(command: object, cwd: str = "") -> Optional[ObjectDict]:
    inner = _unwrap_single_shell_command(_command_text(command))
    if not inner or "\n" in inner:
        return None
    try:
        tokens = shlex.split(inner, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    if any(token in {"&&", "||", ";", "|", ">", "<"} for token in tokens):
        return None

    cmd = tokens[0]
    if cmd not in {"rg", "grep"}:
        return None

    bool_flags = {
        "-n": "n",
        "--line-number": "n",
        "-i": "i",
        "--ignore-case": "i",
        "-l": "l",
        "--files-with-matches": "l",
        "-S": "S",
        "--smart-case": "S",
        "-u": "u",
        "-uu": "uu",
        "-uuu": "uuu",
        "-r": "recursive",
        "-R": "recursive",
        "--recursive": "recursive",
    }
    value_flags = {
        "-g": "glob",
        "--glob": "glob",
        "--type": "type",
        "-A": "A",
        "-B": "B",
        "-C": "C",
        "-m": "head_limit",
        "--max-count": "head_limit",
    }

    args: ObjectDict = {}
    positional: List[str] = []
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            positional.extend(tokens[idx + 1 :])
            break
        if token in value_flags:
            if idx + 1 >= len(tokens):
                return None
            args[value_flags[token]] = tokens[idx + 1]
            idx += 2
            continue
        if token.startswith("--glob="):
            args["glob"] = token.split("=", 1)[1]
            idx += 1
            continue
        if token.startswith("--type="):
            args["type"] = token.split("=", 1)[1]
            idx += 1
            continue
        if token.startswith("--max-count="):
            args["head_limit"] = token.split("=", 1)[1]
            idx += 1
            continue
        if token in bool_flags:
            args[bool_flags[token]] = True
            idx += 1
            continue
        if token.startswith("-") and token not in {"-"}:
            if cmd == "rg" and token.startswith("-g") and len(token) > 2:
                args["glob"] = token[2:]
                idx += 1
                continue
            if token[:2] in {"-A", "-B", "-C", "-m"} and len(token) > 2:
                args[value_flags[token[:2]]] = token[2:]
                idx += 1
                continue
            if token.startswith("-") and len(token) > 2 and all(f"-{ch}" in bool_flags for ch in token[1:]):
                for ch in token[1:]:
                    args[bool_flags[f"-{ch}"]] = True
                idx += 1
                continue
        positional.append(token)
        idx += 1

    if not positional:
        return None

    pattern = positional[0]
    target_paths = positional[1:]
    resolved_targets = [_resolve_view_path(target_path, cwd) for target_path in target_paths]
    if len(resolved_targets) == 1:
        resolved_path = resolved_targets[0]
    else:
        resolved_path = str(cwd or "")
    if pattern:
        args["pattern"] = pattern
    if resolved_path:
        args["path"] = resolved_path
    if resolved_targets:
        args["targets"] = resolved_targets

    return {
        "mode": cmd,
        "path": resolved_path,
        "pattern": pattern,
        "arguments": args,
        "title": "search",
    }


def _normalize_search_output(output: str, search_spec: Optional[ObjectDict]) -> str:
    text = _normalize_output(output)
    if not text or not _is_object_dict(search_spec):
        return text
    target_path = str(search_spec.get("path") or "")
    if not target_path:
        return text

    had_trailing_newline = text.endswith("\n")
    normalized_lines: List[str] = []
    for raw_line in text.splitlines():
        match = re.match(r"^(\d+)(?::(\d+))?:(.*)$", raw_line)
        if match:
            line_no = match.group(1)
            col_no = match.group(2)
            preview = match.group(3)
            if col_no is not None:
                normalized_lines.append(f"{target_path}:{line_no}:{col_no}:{preview}")
            else:
                normalized_lines.append(f"{target_path}:{line_no}:{preview}")
        else:
            normalized_lines.append(raw_line)
    normalized = "\n".join(normalized_lines)
    if had_trailing_newline:
        normalized += "\n"
    return normalized


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


def _extract_diff_with_path(payload: object) -> Tuple[Optional[str], Optional[str]]:
    if not _is_object_dict(payload):
        return None, None
    path_value = payload.get("path")
    path = path_value if isinstance(path_value, str) else None
    diff = payload.get("diff") or payload.get("patch") or payload.get("unified_diff")
    if isinstance(diff, str) and diff.strip():
        return diff, path or _extract_path_from_diff(diff)

    changes = payload.get("changes")
    if _is_object_list(changes):
        chunks: List[str] = []
        for change in changes:
            if not _is_object_dict(change):
                continue
            text = change.get("diff") or change.get("patch") or change.get("unified_diff")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
                if not path:
                    change_path_value = change.get("path")
                    if isinstance(change_path_value, str):
                        path = change_path_value
        if chunks:
            combined = "\n".join(chunks)
            return combined, path or _extract_path_from_diff(combined)

    if _is_object_dict(changes):
        chunks = []
        for change_path, change in changes.items():
            if not _is_object_dict(change):
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


def _diff_sections_from_changes(changes: object) -> List[Tuple[Optional[str], str]]:
    sections: List[Tuple[Optional[str], str]] = []
    if _is_object_list(changes):
        for change in changes:
            if not _is_object_dict(change):
                continue
            text = change.get("diff") or change.get("patch") or change.get("unified_diff")
            if not isinstance(text, str) or not text.strip():
                continue
            path = change.get("path") or change.get("file_path") or _extract_path_from_diff(text)
            section_path = path if isinstance(path, str) else None
            sections.append((section_path, _normalized_change_diff(section_path, change, text)))
    elif _is_object_dict(changes):
        for change_path, change in changes.items():
            if not _is_object_dict(change):
                continue
            text = change.get("diff") or change.get("patch") or change.get("unified_diff")
            if not isinstance(text, str) or not text.strip():
                continue
            path = change.get("path") or change.get("file_path") or change_path or _extract_path_from_diff(text)
            section_path = path if isinstance(path, str) else None
            sections.append((section_path, _normalized_change_diff(section_path, change, text)))
    return sections


def _change_kind_type(change: ObjectDict) -> str:
    kind = change.get("kind")
    if isinstance(kind, str):
        return kind.strip().lower()
    if _is_object_dict(kind):
        kind_type = kind.get("type")
        if isinstance(kind_type, str):
            return kind_type.strip().lower()
    return ""


def _diff_text_has_file_headers(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.startswith("--- ") and lines[index + 1].startswith("+++ "):
            return True
    return False


def _diff_text_has_hunk_header(text: str) -> bool:
    return any(line.startswith("@@ ") for line in text.splitlines())


def _diff_text_has_git_header(text: str) -> bool:
    return any(line.startswith("diff --git ") for line in text.splitlines())


def _normalized_change_diff(path: Optional[str], change: ObjectDict, text: str) -> str:
    if _change_kind_type(change) != "add":
        return text
    if _diff_text_has_git_header(text) or _diff_text_has_file_headers(text):
        return text
    header_path = _diff_header_path(path or _extract_path_from_diff(text))
    if _diff_text_has_hunk_header(text):
        return f"--- /dev/null\n+++ {_prefix_diff_path('b', header_path)}\n{text}"
    return _build_new_file_diff(header_path, text)


def _diff_header_path(path: Optional[str]) -> str:
    if not isinstance(path, str) or not path.strip():
        return "unknown"
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "unknown"


def _prefix_diff_path(prefix: str, path: str) -> str:
    if path == "/dev/null" or path.startswith(f"{prefix}/"):
        return path
    return f"{prefix}/{path}"


def _diff_with_file_header(path: Optional[str], diff_text: str) -> str:
    text = diff_text.strip()
    if not text:
        return ""
    if any(line.startswith("diff --git ") for line in text.splitlines()):
        return text
    header_path = _diff_header_path(path or _extract_path_from_diff(text))
    old_path = _prefix_diff_path("a", header_path)
    new_path = _prefix_diff_path("b", header_path)
    if any(line.startswith("--- ") or line.startswith("+++ ") for line in text.splitlines()):
        return f"diff --git {old_path} {new_path}\n{text}"
    return f"diff --git {old_path} {new_path}\n--- {old_path}\n+++ {new_path}\n{text}"


def _diff_sections_from_changes_with_headers(changes: object) -> List[Tuple[Optional[str], str]]:
    sections: List[Tuple[Optional[str], str]] = []
    for path, diff_text in _diff_sections_from_changes(changes):
        headered = _diff_with_file_header(path, diff_text)
        if headered:
            sections.append((path or _extract_path_from_diff(headered), headered))
    return sections


def _diff_text_from_changes_with_headers(changes: object) -> Optional[str]:
    chunks = [text for _path, text in _diff_sections_from_changes_with_headers(changes)]
    if not chunks:
        return None
    return "\n".join(chunks)


def _diff_is_new_file(diff_text: Optional[str]) -> bool:
    if not isinstance(diff_text, str) or not diff_text.strip():
        return False
    if "new file mode" in diff_text:
        return True
    from_dev_null = False
    to_real_path = False
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            from_dev_null = line[4:].strip() in {"/dev/null", "a/dev/null"}
        elif line.startswith("+++ "):
            to_path = line[4:].strip()
            if to_path and to_path != "/dev/null":
                to_real_path = True
    return from_dev_null and to_real_path


def _diff_id_token(value: Optional[str], fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    token = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip()).strip("_")
    return token or fallback


def _atomic_filechange_diff_id(
    *,
    thread_id: Optional[str],
    turn_id: Optional[str],
    item_id: Optional[str],
    change_index: int,
    path: Optional[str],
) -> str:
    path_fingerprint = hashlib.sha1((path or "").encode("utf-8")).hexdigest()[:12]
    return ":".join([
        _diff_id_token(thread_id, "unknown-thread"),
        _diff_id_token(turn_id, "unknown-turn"),
        _diff_id_token(item_id, "unknown-item"),
        str(change_index),
        path_fingerprint,
    ])


def _paths_from_changes(changes: object) -> List[str]:
    paths: List[str] = []
    if _is_object_list(changes):
        for change in changes:
            if not _is_object_dict(change):
                continue
            path = change.get("path") or _extract_path_from_diff(str(change.get("diff") or ""))
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    elif _is_object_dict(changes):
        for change_path, change in changes.items():
            candidate = None
            if _is_object_dict(change):
                candidate = change.get("path") or change.get("file_path") or change_path
            else:
                candidate = change_path
            if isinstance(candidate, str) and candidate and candidate not in paths:
                paths.append(candidate)
    return paths


def _result_error_status(status: str, exit_code: Optional[int], error: object = None) -> bool:
    return bool(error) or status in {"failed", "declined", "error"} or exit_code not in (None, 0)


_THOUGHT_PATTERN = re.compile(r"\*\*([^*]+)\*\*")


def _has_visible_reasoning_text(text: str) -> bool:
    return bool(text.strip())


def _extract_and_scrub_thoughts_stream(delta: str, state: ObjectDict) -> Tuple[str, List[str]]:
    if not delta:
        return delta, []
    buffer_value = state.get("thought_buffer")
    buffer = buffer_value if isinstance(buffer_value, str) else ""
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


def _consume_live_reasoning_delta(delta: str, state: ObjectDict) -> Optional[str]:
    if not delta:
        return None
    if state.get("reasoning_live_visible"):
        return delta
    if _has_visible_reasoning_text(delta):
        pending_prefix = state.get("reasoning_pending_prefix", "")
        state["reasoning_pending_prefix"] = ""
        state["reasoning_live_visible"] = True
        return f"{pending_prefix}{delta}"
    pending_prefix = state.get("reasoning_pending_prefix", "")
    state["reasoning_pending_prefix"] = f"{pending_prefix}{delta}"
    return None


class CodexEventRouter:
    def __init__(self) -> None:
        self._turn_states: Dict[str, ObjectDict] = {}
        self._item_states: Dict[str, ObjectDict] = {}
        self._approval_request_map: Dict[str, str] = {}
        self._subagent_states: Dict[str, ObjectDict] = {}
        self._thread_subagent_ids: Dict[str, str] = {}

    def reset(self) -> None:
        self._turn_states.clear()
        self._item_states.clear()
        self._approval_request_map.clear()
        self._subagent_states.clear()
        self._thread_subagent_ids.clear()

    def _turn_key(self, thread_id: Optional[str], turn_id: Optional[str]) -> str:
        return f"{thread_id or 'unknown'}:{turn_id or 'unknown'}"

    def _get_turn_state(self, thread_id: Optional[str], turn_id: Optional[str]) -> ObjectDict:
        key = self._turn_key(thread_id, turn_id)
        state = self._turn_states.get(key)
        if state is not None:
            return state
        new_state: ObjectDict = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "plan_steps": list[object](),
            "plan_signature": None,
            "plan_explanation": None,
        }
        self._turn_states[key] = new_state
        return new_state

    def _get_item_state(self, item_id: Optional[str], thread_id: Optional[str], turn_id: Optional[str]) -> ObjectDict:
        turn_state = self._get_turn_state(thread_id, turn_id)
        if not item_id:
            return turn_state
        state = self._item_states.get(item_id)
        if state is None:
            new_state: ObjectDict = {
                "item_id": item_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_key": self._turn_key(thread_id, turn_id),
                "output_buffer": "",
            }
            self._item_states[item_id] = new_state
            state = new_state
        else:
            state["thread_id"] = thread_id or state.get("thread_id")
            state["turn_id"] = turn_id or state.get("turn_id")
            existing_thread_id = state.get("thread_id")
            next_thread_id = thread_id or (existing_thread_id if isinstance(existing_thread_id, str) else None)
            existing_turn_id = state.get("turn_id")
            next_turn_id = turn_id or (existing_turn_id if isinstance(existing_turn_id, str) else None)
            state["turn_key"] = self._turn_key(next_thread_id, next_turn_id)
        subagent_id = self._subagent_id_for_context(thread_id)
        if subagent_id:
            state["subagent_id"] = subagent_id
        return state

    def _clear_item_state(self, item_id: Optional[str]) -> ObjectDict:
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
    ) -> ObjectDict:
        state = self._subagent_states.get(subagent_id)
        if state is None:
            new_state: ObjectDict = {
                "id": subagent_id,
                "name": name or "subagent",
                "intent": intent or "",
                "parent_thread_id": parent_thread_id,
                "thread_ids": set[str](),
                "started": False,
                "ended": False,
                "active": False,
            }
            self._subagent_states[subagent_id] = new_state
            state = new_state
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
        thread_ids = state.setdefault("thread_ids", set[str]())
        if isinstance(thread_ids, set):
            cast(set[object], thread_ids).add(thread_id)
        self._thread_subagent_ids[thread_id] = subagent_id

    def _subagent_id_for_context(self, thread_id: Optional[str]) -> Optional[str]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        return self._thread_subagent_ids.get(thread_id)

    def _claim_reasoning_source(self, turn_state: ObjectDict, source: str) -> bool:
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
        payload: ObjectDict,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[Tuple[ObjectDict, ObjectDict, str]]:
        turn_state = self._get_turn_state(thread_id, turn_id)
        if not self._claim_reasoning_source(turn_state, source):
            return None
        item_id = _reasoning_event_id(payload, turn_state)
        turn_state["reasoning_id"] = item_id
        turn_state["reasoning_started"] = True
        item_state = self._get_item_state(item_id if item_id != "reasoning" else None, thread_id, turn_id)
        item_state["item_type"] = "reasoning"
        return turn_state, item_state, item_id

    def _should_record_reasoning(self, turn_state: ObjectDict, item_id: str) -> bool:
        recorded = turn_state.setdefault("reasoning_transcript_ids", set[object]())
        if not isinstance(recorded, set):
            if isinstance(recorded, (list, tuple)):
                recorded = set(cast(list[object] | tuple[object, ...], recorded))
            else:
                recorded = set[object]()
            turn_state["reasoning_transcript_ids"] = recorded
        if item_id in recorded:
            return False
        cast(set[object], recorded).add(item_id)
        return True

    def _reset_reasoning_stream(self, turn_state: ObjectDict) -> None:
        turn_state["reasoning_started"] = False
        turn_state["reasoning_buffer"] = ""
        turn_state["reasoning_id"] = None
        turn_state["thought_buffer"] = ""
        turn_state["reasoning_pending_prefix"] = ""
        turn_state["reasoning_live_visible"] = False

    def _plan_update_result(
        self,
        *,
        label_lower: str,
        payload: ObjectDict,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> ObjectDict:
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

        events: List[ObjectDict] = []
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
            plan_event: ObjectDict = {
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

        active_plan: Optional[ObjectDict]
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

    def _decorate_event(self, entry: ObjectDict, subagent_id: Optional[str]) -> ObjectDict:
        if subagent_id:
            entry["subagent_id"] = subagent_id
        return entry

    def _decorate_transcript_entry(self, entry: ObjectDict, subagent_id: Optional[str]) -> ObjectDict:
        if subagent_id:
            entry["subagent_id"] = subagent_id
        return entry

    def _decorate_routed_result(
        self,
        routed: ObjectDict,
        *,
        thread_id: Optional[str],
        item_state: Optional[ObjectDict] = None,
    ) -> ObjectDict:
        subagent_id = None
        if _is_object_dict(item_state):
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
            "shell_begin",
            "shell_delta",
            "shell_end",
            "command_result",
            "reasoning_delta",
            "reasoning_finalize",
            "diff",
            "approval",
            "view",
            "search",
        }
        transcript_roles = {"assistant", "user", "command", "diff", "reasoning", "mcp_tool", "tool", "view", "search", "web_search"}

        for event in _dict_list(routed.get("events")):
            if not _is_object_dict(event) or event.get("subagent_id"):
                continue
            if event.get("type") in event_types:
                event["subagent_id"] = subagent_id

        for entry in _dict_list(routed.get("transcript_entries")):
            if not _is_object_dict(entry) or entry.get("subagent_id"):
                continue
            if entry.get("role") in transcript_roles:
                entry["subagent_id"] = subagent_id

        descriptors = routed.get("approval_descriptors")
        if _is_object_list(descriptors):
            for descriptor in descriptors:
                if not _is_object_dict(descriptor):
                    continue
                render_event = descriptor.get("render_event")
                if _is_object_dict(render_event) and not render_event.get("subagent_id"):
                    render_event["subagent_id"] = subagent_id
        return routed

    def _route_collab_event(
        self,
        protocol: RuntimeProtocol,
        event_type: str,
        payload: ObjectDict,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[ObjectDict]:
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
        events: List[ObjectDict] = []
        transcript_entries: List[ObjectDict] = []

        def emit_start(target_id: str, target_state: ObjectDict) -> None:
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

        def emit_end(target_id: str, target_state: ObjectDict, success: bool, summary: str) -> None:
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

    def _error_result(
        self,
        *,
        label_lower: str,
        payload: ObjectDict,
        turn_id: Optional[str],
    ) -> ObjectDict:
        error_value = payload.get("error")
        if _is_object_dict(error_value):
            error_obj = error_value
        elif isinstance(error_value, str) and error_value.strip():
            error_obj = {"message": error_value.strip()}
        else:
            error_obj = payload

        message = ""
        if _is_object_dict(error_obj):
            raw_message = error_obj.get("message") or payload.get("message")
            if isinstance(raw_message, str):
                message = raw_message.strip()
            elif raw_message is not None:
                message = str(raw_message).strip()
        if not message:
            return {"handled": True, "events": [], "transcript_entries": []}

        error_event: ObjectDict = {
            "type": "error",
            "message": message,
            "source": label_lower,
        }
        error_entry: ObjectDict = {
            "role": "error",
            "message": message,
            "turn_id": turn_id,
            "source": label_lower,
            "event": label_lower,
        }

        details = error_obj.get("details") or error_obj.get("additional_details") or payload.get("additional_details")
        if isinstance(details, str) and details.strip():
            error_event["details"] = details
            error_entry["details"] = details

        stack = error_obj.get("stack")
        if isinstance(stack, str) and stack.strip():
            error_event["stack"] = stack
            error_entry["stack"] = stack

        error_type = error_obj.get("error_type") or error_obj.get("errorType")
        if isinstance(error_type, str) and error_type.strip():
            error_event["error_type"] = error_type
            error_entry["error_type"] = error_type

        provider_call_id = error_obj.get("provider_call_id") or error_obj.get("providerCallId")
        if isinstance(provider_call_id, str) and provider_call_id.strip():
            error_event["provider_call_id"] = provider_call_id
            error_entry["provider_call_id"] = provider_call_id

        code = error_obj.get("code")
        if code is not None:
            error_event["code"] = code
            error_entry["code"] = code

        status_code = error_obj.get("status_code")
        if status_code is None:
            status_code = error_obj.get("statusCode")
        if status_code is None:
            status_code = error_obj.get("httpStatusCode")
        if isinstance(status_code, (int, float)):
            error_event["status_code"] = int(status_code)
            error_entry["status_code"] = int(status_code)

        return {
            "handled": True,
            "events": [
                error_event,
                {"type": "activity", "label": "error", "active": False},
            ],
            "transcript_entries": [error_entry],
        }

    def _warning_result(self, *, message: str) -> ObjectDict:
        text = message.strip()
        if not text:
            return {"handled": True, "events": [], "transcript_entries": []}
        return {
            "handled": True,
            "events": [
                {"type": "warning", "message": text},
                {"type": "activity", "label": "warning", "active": False},
            ],
            "transcript_entries": [],
        }

    def _generic_notification_result(
        self,
        *,
        label_lower: str,
        payload: ObjectDict,
        notification_spec: Optional[ProtocolSemanticSpec],
        event_spec: Optional[ProtocolSemanticSpec],
        turn_id: Optional[str],
    ) -> Optional[ObjectDict]:
        severity = _notification_severity(label_lower, notification_spec, event_spec, payload)
        if severity is None:
            return None

        message = _notification_text(payload)
        if severity == "warning":
            if not message and "interrupt" in label_lower:
                message = "Interrupted"
            if not message:
                return None
            return self._warning_result(message=message)

        if not message:
            return None
        return self._error_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

    def _token_usage_result(
        self,
        *,
        label_lower: str,
        payload: ObjectDict,
        turn_id: Optional[str],
    ) -> ObjectDict:
        total = None
        input_tokens = None
        cached_input_tokens = None
        context_window = None

        info_value = payload.get("info")
        if _is_object_dict(info_value):
            info = _dict_payload(info_value)
            usage: object = info.get("last_token_usage")
            if _is_object_dict(usage):
                total = usage.get("input_tokens")
                input_tokens = usage.get("input_tokens")
                cached_input_tokens = usage.get("cached_input_tokens")
            context_window = info.get("model_context_window")

        token_usage_value = payload.get("tokenUsage")
        if total is None and _is_object_dict(token_usage_value):
            token_usage = _dict_payload(token_usage_value)
            last_breakdown: object = token_usage.get("last")
            if _is_object_dict(last_breakdown):
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
        event: ObjectDict = {"type": "token_count", "total": total_int}
        transcript_entry: ObjectDict = {
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
        payload: ObjectDict,
        turn_id: Optional[str],
    ) -> ObjectDict:
        raw_kind = payload.get("collaboration_mode_kind") or payload.get("collaborationModeKind")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            return {"handled": True, "events": [], "transcript_entries": []}
        kind = raw_kind.strip()
        event: ObjectDict = {
            "type": "mode",
            "kind": kind,
        }
        transcript_entry: ObjectDict = {
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

    def _emit_filechange_diff_entries(
        self,
        result: ObjectDict,
        *,
        changes: object,
        diff_text: Optional[str],
        path: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        item_id: Optional[str],
        event_name: str,
    ) -> None:
        turn_state = self._get_turn_state(thread_id, turn_id)
        subagent_id = self._subagent_id_for_context(thread_id)
        event_entries = _ensure_dict_list(result, "events")
        transcript_entries = _ensure_dict_list(result, "transcript_entries")
        sections = _diff_sections_from_changes_with_headers(changes)
        if not sections and diff_text:
            sections = [(path or _extract_path_from_diff(diff_text), _diff_with_file_header(path, diff_text))]
        emitted_any = False
        for change_index, (change_path, change_diff) in enumerate(sections):
            if not change_diff:
                continue
            effective_path = change_path or path or _extract_path_from_diff(change_diff)
            diff_id = _atomic_filechange_diff_id(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                change_index=change_index,
                path=effective_path,
            )
            event_entries.append(self._decorate_event({
                "type": "diff",
                "id": diff_id,
                "text": change_diff,
                "path": effective_path,
            }, subagent_id))
            transcript_entries.append(self._decorate_transcript_entry({
                "role": "diff",
                "text": change_diff,
                "path": effective_path,
                "item_id": diff_id,
                "turn_id": turn_id,
                "event": event_name,
            }, subagent_id))
            emitted_any = True
        if emitted_any:
            turn_state["filechange_diff_emitted"] = True

    def _tool_request_result(
        self,
        *,
        request_id: str,
        kind: str,
        payload: ObjectDict,
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_method: Optional[str] = None,
        request_params: Optional[ObjectDict] = None,
        activity_label: Optional[str] = None,
    ) -> ObjectDict:
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

    def _ask_user_request_result(
        self,
        *,
        tool_id: str,
        request_id: str,
        arguments: object,
        item_state: ObjectDict,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> ObjectDict:
        normalized_arguments = arguments if _is_object_dict(arguments) else {}
        question = str(normalized_arguments.get("question") or "").strip()
        raw_choices = normalized_arguments.get("choices")
        if _is_object_list(raw_choices):
            choices = raw_choices
        elif isinstance(raw_choices, str):
            try:
                parsed = cast(object, json.loads(raw_choices))
                choices = parsed if _is_object_list(parsed) else []
            except Exception:
                choices = []
        else:
            choices = []
        allow_freeform = normalized_arguments.get(
            "allow_freeform",
            normalized_arguments.get("allowFreeform", True),
        )
        card_id = str(tool_id or request_id or "").strip() or request_id
        item_state["approval_request_id"] = request_id
        item_state["ask_user_descriptor_emitted"] = True
        self._approval_request_map[str(tool_id)] = request_id
        request_params: ObjectDict = {
            "requestId": request_id,
            "question": question,
            "choices": list(choices),
            "allowFreeform": bool(allow_freeform),
        }
        payload_data: ObjectDict = {
            "requestId": request_id,
            "question": question,
            "choices": list(choices),
            "allowFreeform": bool(allow_freeform),
            "message": question,
            "tool_call_id": str(tool_id or ""),
        }
        routed = self._tool_request_result(
            request_id=request_id,
            kind="user_input",
            payload={key: value for key, value in payload_data.items() if value not in (None, "", [], {})},
            thread_id=thread_id,
            turn_id=turn_id,
            request_method=AGENT_PTY_ASK_USER_REQUEST_METHOD,
            request_params=request_params,
            activity_label="request",
        )
        events = routed.get("events")
        if _is_object_list(events):
            for event in events:
                if _is_object_dict(event) and event.get("type") == "approval":
                    event["card_id"] = card_id
        descriptors = routed.get("approval_descriptors")
        if _is_object_list(descriptors):
            for descriptor in descriptors:
                if not _is_object_dict(descriptor):
                    continue
                descriptor["card_id"] = card_id
                render_event_value = descriptor.get("render_event")
                render_event = render_event_value if _is_object_dict(render_event_value) else None
                if _is_object_dict(render_event):
                    render_event["card_id"] = card_id
        return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)

    def route_event(
        self,
        protocol: RuntimeProtocol,
        *,
        label: Optional[str],
        payload: object,
        thread_id: Optional[str],
        turn_id: Optional[str],
        conversation_id: Optional[str] = None,
        extract_item_text: Optional[Callable[[ObjectDict], Optional[Dict[str, str]]]] = None,
    ) -> ObjectDict:
        result: ObjectDict = {
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

        if event_spec is not None and event_type is not None and _is_object_dict(payload):
            collab = self._route_collab_event(protocol, event_type, payload, thread_id, turn_id)
            if collab is not None:
                return collab

        if notification_spec and notification_spec.category == "thread" and notification_spec.subject == "thread" and notification_spec.phase == "started":
            thread_obj = _dict_payload(payload.get("thread")) if _is_object_dict(payload) else {}
            next_thread_id = thread_obj.get("id") if isinstance(thread_obj.get("id"), str) else None
            return {
                "handled": True,
                "events": [{"type": "activity", "label": "thread started", "active": True}],
                "transcript_entries": [],
                "bind_thread_ids": [next_thread_id] if next_thread_id else [],
            }

        if notification_spec and notification_spec.category == "turn" and notification_spec.subject == "turn" and notification_spec.phase == "started" and _is_object_dict(payload):
            next_turn_id = turn_id
            turn_value = payload.get("turn")
            turn_obj = turn_value if _is_object_dict(turn_value) else {}
            turn_id_value = turn_obj.get("id")
            if isinstance(turn_id_value, str) and turn_id_value:
                next_turn_id = turn_id_value
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
        ) and _is_object_dict(payload):
            return self._plan_update_result(
                label_lower=label_lower,
                payload=payload,
                thread_id=thread_id,
                turn_id=turn_id,
            )

        if notification_spec and notification_spec.category == "turn" and notification_spec.subject == "turn" and notification_spec.phase == "completed" and _is_object_dict(payload):
            turn_state = self._get_turn_state(thread_id, turn_id)
            plan_steps = normalize_plan_steps(turn_state.get("plan_steps"))
            plan_explanation = turn_state.get("plan_explanation")
            turn_status, turn_error = _normalize_turn_status(payload)
            if turn_status == "failed":
                ribbon_status = "error"
            elif turn_status == "interrupted":
                ribbon_status = "warning"
            else:
                ribbon_status = "success"
            self._turn_states.pop(self._turn_key(thread_id, turn_id), None)
            events = [
                {
                    "type": "status",
                    "status": ribbon_status,
                    "turn_status": turn_status,
                    "error": turn_error,
                },
                {"type": "activity", "label": "idle", "active": False},
            ]
            if turn_status == "interrupted":
                interrupted_message = turn_error or _notification_text(payload) or "Interrupted"
                events.insert(0, {"type": "warning", "message": interrupted_message})
            transcript_entries = [{
                "role": "status",
                "status": ribbon_status,
                "turn_status": turn_status,
                "turn_id": turn_id,
                "error": turn_error,
                "event": label_lower,
            }]
            if plan_steps:
                plan_content = render_plan_markdown(plan_steps, plan_explanation if isinstance(plan_explanation, str) else None)
                events.append({
                    "type": "plan_state",
                    "has_plan": False,
                    "has_todo": True,
                    "plan_exists": False,
                    "plan_content": plan_content,
                    "plan_steps": plan_steps,
                })
                plan_event: ObjectDict = {
                    "type": "plan",
                    "steps": plan_steps,
                }
                plan_entry: ObjectDict = {
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
        ) and _is_object_dict(payload):
            return {"handled": True, "events": [], "transcript_entries": []}

        if (
            (notification_spec and notification_spec.category == "thread" and notification_spec.subject == "tokenusage" and notification_spec.phase == "updated")
            or label_lower == "codex/event/token_count"
        ) and _is_object_dict(payload):
            return self._token_usage_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

        if (
            (notification_spec and notification_spec.category == "thread" and notification_spec.subject == "realtime" and notification_spec.phase == "error")
            or label_lower in {"thread/realtime/error", "codex/event/error", "codex/event/stream_error"}
        ) and _is_object_dict(payload):
            return self._error_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

        if label_lower == "codex/event/task_started" and _is_object_dict(payload):
            return self._collaboration_mode_result(label_lower=label_lower, payload=payload, turn_id=turn_id)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "item" and notification_spec.phase == "started" and _is_object_dict(payload):
            item_value = payload.get("item")
            item = item_value if _is_object_dict(item_value) else payload
            if not _is_object_dict(item):
                return result
            item_type = _item_type(item)
            effective_thread_id = _payload_thread_id(payload, thread_id)
            effective_turn_id = _payload_turn_id(payload, turn_id)
            item_id_value = item.get("id")
            item_id = item_id_value if isinstance(item_id_value, str) else None
            item_state = self._get_item_state(item_id, effective_thread_id, effective_turn_id)

            if extract_item_text:
                entry = extract_item_text(item)
                if entry and entry.get("role") == "user":
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [build_message_event(
                            role="user",
                            entry_id=item_id,
                            text=entry["text"],
                            turn_id=effective_turn_id,
                        )],
                        "transcript_entries": [build_message_transcript_entry(
                            role="user",
                            entry_id=item_id,
                            item_id=item_id,
                            text=entry["text"],
                            timestamp=utc_ts(),
                            turn_id=effective_turn_id,
                            event=label_lower,
                        )],
                    }, thread_id=effective_thread_id, item_state=item_state)

            if item_type == "reasoning":
                turn_state = self._get_turn_state(effective_thread_id, effective_turn_id)
                current_source = turn_state.get("reason_source")
                if current_source in {None, "item"}:
                    turn_state["reason_source"] = current_source or "item"
                    turn_state["reasoning_id"] = item_id or turn_state.get("reasoning_id")
                    turn_state["reasoning_started"] = False
                    turn_state["reasoning_buffer"] = ""
                    turn_state["thought_buffer"] = ""
                    turn_state["reasoning_pending_prefix"] = ""
                    turn_state["reasoning_live_visible"] = False
                item_state.update({
                    "item_type": item_type,
                })
                return {"handled": True, "events": [], "transcript_entries": []}

            if item_type == "commandexecution":
                command = item.get("command") or item.get("parsedCmd") or item.get("cmd") or item.get("argv") or ""
                cwd_value = item.get("cwd")
                cwd = cwd_value if isinstance(cwd_value, str) else ""
                new_file_spec = _shell_command_to_new_file_spec(command, cwd)
                view_sequence = None if new_file_spec else _shell_command_to_view_sequence(command, cwd)
                view_spec = None if (new_file_spec or view_sequence) else _shell_command_to_view_spec(command, cwd)
                search_spec = None if (new_file_spec or view_sequence or view_spec) else _shell_command_to_search_spec(command, cwd)
                item_state.update({
                    "item_type": item_type,
                    "command": command,
                    "cwd": cwd,
                    "output_buffer": "",
                    "new_file_spec": new_file_spec,
                    "view_sequence": view_sequence,
                    "view_spec": view_spec,
                    "search_spec": search_spec,
                })
                if new_file_spec:
                    path = new_file_spec.get("path") if isinstance(new_file_spec.get("path"), str) else ""
                    arguments = _new_file_arguments(new_file_spec)
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [
                            {
                                "type": "tool_begin",
                                "id": item_id or _assistant_id(item, thread_id, turn_id),
                                "tool": "apply_patch",
                                "arguments": arguments,
                                "path": path,
                                "diff": new_file_spec.get("diff"),
                                "new_file": True,
                            },
                            {"type": "activity", "label": "creating file", "active": True},
                        ],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                if view_sequence:
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [
                            {"type": "activity", "label": "reading files", "active": True},
                        ],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                if view_spec:
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [
                            {"type": "activity", "label": "reading file", "active": True},
                        ],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                if search_spec:
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [
                            {"type": "activity", "label": "searching files", "active": True},
                        ],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        {
                            "type": "shell_begin",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "command": _command_text(command),
                            "cwd": cwd,
                            "activity": "running command",
                        },
                        {"type": "activity", "label": "running command", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)

            if item_type == "filechange":
                changes = item.get("changes")
                diff_text, path = _extract_diff_with_path(item)
                display_diff_text = _diff_text_from_changes_with_headers(changes) or diff_text
                paths = _paths_from_changes(changes)
                new_file = _diff_is_new_file(display_diff_text)
                item_state.update({
                    "item_type": item_type,
                    "changes": changes,
                    "diff": display_diff_text,
                    "path": path,
                    "paths": paths,
                    "new_file": new_file,
                    "patch_updated_seen": False,
                    "output_buffer": "",
                })
                arguments: ObjectDict = {}
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
                            "path": path,
                            "diff": display_diff_text,
                            "new_file": new_file,
                        },
                        {"type": "activity", "label": "preparing diff", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)

            if item_type in {"mcptoolcall", "websearch", "imageview"}:
                tool_name = _string_value(item.get("tool"))
                server_name = _string_value(item.get("server"))
                arguments = _dict_payload(item.get("arguments"))
                if item_type == "websearch":
                    tool_name = "web_search"
                    arguments = {"query": item.get("query")}
                elif item_type == "imageview":
                    tool_name = "view_image"
                    arguments = {"path": item.get("path")}
                request_payload = build_tool_card_request(server_name or "", tool_name or item_type, arguments)
                item_state.update({
                    "item_type": item_type,
                    "tool": tool_name or item_type,
                    "server": server_name or "",
                    "arguments": arguments,
                    "request": request_payload,
                })
                if is_agent_pty_ask_user_tool(server_name, tool_name):
                    request_id = str(conversation_id or "").strip()
                    if request_id and _is_object_dict(arguments):
                        arguments = dict(arguments)
                        arguments["requestId"] = request_id
                        item_state["arguments"] = arguments
                    if request_id:
                        item_state["approval_request_id"] = request_id
                        if item_state.get("ask_user_descriptor_emitted"):
                            return self._decorate_routed_result({
                                "handled": True,
                                "events": [],
                                "transcript_entries": [],
                            }, thread_id=thread_id, item_state=item_state)
                        return self._ask_user_request_result(
                            tool_id=str(item_id or _assistant_id(item, thread_id, turn_id)),
                            request_id=request_id,
                            arguments=arguments,
                            item_state=item_state,
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                if item_type == "websearch":
                    query = item.get("query")
                    if not isinstance(query, str):
                        query = arguments.get("query") if isinstance(arguments.get("query"), str) else ""
                    item_state["query"] = query
                    activity_label = f"web_search: {query}" if query else "web_search"
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [
                            {"type": "activity", "label": activity_label, "active": True},
                        ],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                activity_label = f"calling {tool_name}" if tool_name else "calling tool"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_begin",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": tool_name or item_type,
                            "server": server_name or "",
                            "arguments": arguments,
                            "request": request_payload,
                        },
                        {"type": "activity", "label": activity_label, "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "agentmessage" and notification_spec.phase == "delta" and _is_object_dict(payload):
            delta = payload.get("delta")
            if isinstance(delta, str):
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                item_id = _assistant_id(payload, effective_thread_id, effective_turn_id)
                item_state = self._get_item_state(item_id, effective_thread_id, effective_turn_id)
                item_state["item_type"] = "assistant_message"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        build_assistant_delta_event(
                            entry_id=item_id,
                            delta=delta,
                            turn_id=effective_turn_id,
                        ),
                        {"type": "activity", "label": "responding", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=effective_thread_id, item_state=item_state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "plan" and notification_spec.phase == "delta":
            return {"handled": True, "events": [], "transcript_entries": []}

        if notification_spec and notification_spec.category == "item" and _is_object_dict(payload):
            spec_name = notification_spec.name
            if spec_name in {"item/reasoning/summarytextdelta", "item/reasoning/textdelta"}:
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                prepared = self._prepare_reasoning_state(
                    source="item",
                    payload=payload,
                    thread_id=effective_thread_id,
                    turn_id=effective_turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                delta = payload.get("delta")
                if isinstance(delta, str):
                    turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}{delta}"
                    scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, turn_state)
                    events = [
                        build_thought_event(text=thought, turn_id=effective_turn_id)
                        for thought in thoughts
                    ]
                    live_delta = _consume_live_reasoning_delta(scrubbed_delta, turn_state)
                    if live_delta:
                        events.append(build_reasoning_delta_event(
                            entry_id=item_id,
                            delta=live_delta,
                            turn_id=effective_turn_id,
                        ))
                    if thoughts:
                        events.append({"type": "activity", "label": thoughts[-1], "active": True})
                    elif live_delta:
                        events.append({"type": "activity", "label": "reasoning", "active": True})
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": events,
                        "transcript_entries": [],
                    }, thread_id=effective_thread_id, item_state=item_state)
                return {"handled": True, "events": [], "transcript_entries": []}

            if spec_name == "item/reasoning/summarypartadded":
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                prepared = self._prepare_reasoning_state(
                    source="item",
                    payload=payload,
                    thread_id=effective_thread_id,
                    turn_id=effective_turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}\n"
                live_delta = _consume_live_reasoning_delta("\n", turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [build_reasoning_delta_event(
                        entry_id=item_id,
                        delta=live_delta,
                        turn_id=effective_turn_id,
                    )] if live_delta else [],
                    "transcript_entries": [],
                }, thread_id=effective_thread_id, item_state=item_state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "commandexecution" and notification_spec.phase == "outputdelta" and _is_object_dict(payload):
            item_id = _payload_string(payload, "itemId", "item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            delta = _normalize_output(payload.get("delta"))
            if delta:
                state["output_buffer"] = f"{state.get('output_buffer', '')}{delta}"
                if state.get("view_spec") or state.get("view_sequence") or state.get("search_spec"):
                    return {"handled": True, "events": [], "transcript_entries": []}
                if not state.get("new_file_spec"):
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [{
                            "type": "shell_delta",
                            "id": item_id or _assistant_id(payload, thread_id, turn_id),
                            "delta": delta,
                        }],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=state)
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

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "filechange" and notification_spec.phase == "outputdelta" and _is_object_dict(payload):
            item_id = _payload_string(payload, "itemId", "item_id")
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

        if (
            (
                notification_spec
                and notification_spec.category == "item"
                and notification_spec.subject == "filechange_patchupdated"
            )
            or label_lower == "item/filechange/patchupdated"
        ) and _is_object_dict(payload):
            item_id = _payload_string(payload, "itemId", "item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            changes = payload.get("changes")
            diff_text, path = _extract_diff_with_path({
                "changes": changes,
                "path": payload.get("path") or state.get("path"),
            })
            display_diff_text = _diff_text_from_changes_with_headers(changes) or diff_text
            paths = _paths_from_changes(changes)
            state["changes"] = changes
            state["patch_updated_seen"] = True
            if display_diff_text:
                state["diff"] = display_diff_text
                state["new_file"] = bool(state.get("new_file")) or _diff_is_new_file(display_diff_text)
            if path:
                state["path"] = path
            if paths:
                state["paths"] = paths
            return self._decorate_routed_result({
                "handled": True,
                "events": [{"type": "activity", "label": "preparing diff", "active": True}],
                "transcript_entries": [],
            }, thread_id=thread_id, item_state=state)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "commandexecution" and notification_spec.phase == "terminalinteraction" and _is_object_dict(payload):
            item_id = _payload_string(payload, "itemId", "item_id")
            state = self._get_item_state(item_id, thread_id, turn_id)
            if state.get("view_spec") or state.get("view_sequence") or state.get("search_spec"):
                return {"handled": True, "events": [], "transcript_entries": []}
            return self._decorate_routed_result({
                "handled": True,
                "events": [{
                    "type": "tool_interaction",
                    "id": item_id or _assistant_id(payload, thread_id, turn_id),
                    "tool": "apply_patch" if state.get("new_file_spec") else "command",
                    "payload": {
                        "stdin": payload.get("stdin"),
                        "stdout": payload.get("stdout"),
                        "pid": payload.get("pid"),
                    },
                }],
                "transcript_entries": [],
            }, thread_id=thread_id)

        if notification_spec and notification_spec.category == "item" and notification_spec.subject == "item" and notification_spec.phase == "completed" and _is_object_dict(payload):
            item_value = payload.get("item")
            item = item_value if _is_object_dict(item_value) else payload
            if not _is_object_dict(item):
                return result
            item_type = _item_type(item)
            effective_thread_id = _payload_thread_id(payload, thread_id)
            effective_turn_id = _payload_turn_id(payload, turn_id)
            item_id_value = item.get("id")
            item_id = item_id_value if isinstance(item_id_value, str) else None
            item_state = self._clear_item_state(item_id)

            if extract_item_text:
                entry = extract_item_text(item)
                if entry and entry.get("role") == "assistant":
                    assistant_id = item_id or _assistant_id(item, effective_thread_id, effective_turn_id)
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [build_assistant_finalize_event(
                            entry_id=assistant_id,
                            text=entry["text"],
                            turn_id=effective_turn_id,
                        )],
                        "transcript_entries": [build_message_transcript_entry(
                            role="assistant",
                            entry_id=assistant_id,
                            item_id=item_id,
                            text=entry["text"],
                            timestamp=utc_ts(),
                            turn_id=effective_turn_id,
                            event=label_lower,
                        )],
                    }, thread_id=effective_thread_id, item_state=item_state)

            if item_type == "reasoning":
                turn_state = self._get_turn_state(effective_thread_id, effective_turn_id)
                effective_id = item_id or _reasoning_event_id(item, turn_state)
                reasoning_buffer = turn_state.get("reasoning_buffer")
                text = _extract_reasoning_text(
                    item,
                    fallback=reasoning_buffer if isinstance(reasoning_buffer, str) else None,
                )
                scrubbed_text, thoughts = _extract_and_scrub_thoughts(text) if text else ("", [])
                has_visible_reasoning = _has_visible_reasoning_text(scrubbed_text)
                should_finalize_live = turn_state.get("reason_source") in {None, "item"} and has_visible_reasoning
                reasoning_events: List[ObjectDict] = []
                if thoughts:
                    reasoning_events.extend(
                        build_thought_event(text=thought, turn_id=effective_turn_id)
                        for thought in thoughts
                    )
                if should_finalize_live:
                    reasoning_events.append(build_reasoning_finalize_event(
                        entry_id=effective_id,
                        text=scrubbed_text,
                        turn_id=effective_turn_id,
                    ))
                reasoning_transcript_entries: List[ObjectDict] = []
                if has_visible_reasoning and self._should_record_reasoning(turn_state, effective_id):
                    reasoning_transcript_entries.append(build_reasoning_transcript_entry(
                        entry_id=effective_id,
                        item_id=effective_id,
                        text=scrubbed_text,
                        timestamp=utc_ts(),
                        turn_id=effective_turn_id,
                        event=label_lower,
                    ))
                if turn_state.get("reason_source") != "codex":
                    self._reset_reasoning_stream(turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": reasoning_events,
                    "transcript_entries": reasoning_transcript_entries,
                }, thread_id=effective_thread_id, item_state=item_state)

            if item_type == "commandexecution":
                command = item.get("command") or item.get("parsedCmd") or item_state.get("command") or ""
                display_command = _command_text(command)
                cwd = item.get("cwd") or item_state.get("cwd") or ""
                output = _normalize_output(
                    item.get("aggregatedOutput") or item.get("output") or item.get("stdout") or item_state.get("output_buffer")
                )
                exit_code_value = item.get("exitCode") if item.get("exitCode") is not None else item.get("exit_code")
                exit_code = exit_code_value if isinstance(exit_code_value, int) else None
                duration_ms = item.get("durationMs") if item.get("durationMs") is not None else item.get("duration_ms")
                status = str(item.get("status") or "").strip().lower()
                is_error = _result_error_status(status, exit_code, item.get("error"))
                view_sequence = _dict_payload(item_state.get("view_sequence"))
                view_spec = _dict_payload(item_state.get("view_spec"))
                search_spec = _dict_payload(item_state.get("search_spec"))
                new_file_spec = _dict_payload(item_state.get("new_file_spec"))
                if view_sequence and not is_error:
                    raw_specs = _dict_list(view_sequence.get("specs"))
                    divider = _string_value(view_sequence.get("separator"))
                    if raw_specs and divider:
                        split_output = _split_view_output_by_divider(output, divider, len(raw_specs))
                        if split_output is not None:
                            routed_events: List[ObjectDict] = []
                            view_entries: List[ObjectDict] = []
                            base_id = item_id or _assistant_id(item, thread_id, turn_id)
                            for idx, (raw_spec, segment_output) in enumerate(zip(raw_specs, split_output), start=1):
                                view_lines = _build_codex_view_lines(segment_output, raw_spec)
                                view_id = f"{base_id}:view:{idx}"
                                routed_events.append({
                                    "type": "view",
                                    "id": view_id,
                                    "title": _view_spec_title(raw_spec),
                                    "path": _view_spec_path(raw_spec),
                                    "content": segment_output,
                                    "view_range": _view_spec_range(raw_spec),
                                    **({"lines": view_lines} if view_lines is not None else {}),
                                })
                                view_entries.append({
                                    "role": "view",
                                    "id": view_id,
                                    "title": _view_spec_title(raw_spec),
                                    "path": _view_spec_path(raw_spec),
                                    "content": segment_output,
                                    "view_range": _view_spec_range(raw_spec),
                                    **({"lines": view_lines} if view_lines is not None else {}),
                                    "item_id": item_id,
                                    "turn_id": turn_id,
                                    "event": label_lower,
                                })
                            routed_events.append({"type": "activity", "label": "processing", "active": True})
                            view_routed: ObjectDict = {
                                "handled": True,
                                "events": routed_events,
                                "transcript_entries": view_entries,
                            }
                            approval_request_id = item_state.get("approval_request_id")
                            if approval_request_id:
                                view_routed["clear_live_approval_ids"] = [approval_request_id]
                                self._approval_request_map.pop(str(item_id), None)
                            return self._decorate_routed_result(view_routed, thread_id=thread_id, item_state=item_state)
                if view_spec and not is_error:
                    view_lines = _build_codex_view_lines(output, view_spec)
                    routed = {
                        "handled": True,
                        "events": [
                            {
                                "type": "view",
                                "id": item_id or _assistant_id(item, thread_id, turn_id),
                                "title": _view_spec_title(view_spec),
                                "path": _view_spec_path(view_spec),
                                "content": output,
                                "view_range": _view_spec_range(view_spec),
                                **({"lines": view_lines} if view_lines is not None else {}),
                            },
                            {"type": "activity", "label": "processing", "active": True},
                        ],
                        "transcript_entries": [{
                            "role": "view",
                            "title": _view_spec_title(view_spec),
                            "path": _view_spec_path(view_spec),
                            "content": output,
                            "view_range": _view_spec_range(view_spec),
                            **({"lines": view_lines} if view_lines is not None else {}),
                            "item_id": item_id,
                            "turn_id": turn_id,
                            "event": label_lower,
                        }],
                    }
                    approval_request_id = item_state.get("approval_request_id")
                    if approval_request_id:
                        routed["clear_live_approval_ids"] = [approval_request_id]
                        self._approval_request_map.pop(str(item_id), None)
                    return self._decorate_routed_result(routed, thread_id=effective_thread_id, item_state=item_state)
                if search_spec and not is_error:
                    normalized_search_output = _normalize_search_output(output, search_spec)
                    routed = {
                        "handled": True,
                        "events": [
                            {
                                "type": "search",
                                "id": item_id or _assistant_id(item, thread_id, turn_id),
                                "title": search_spec.get("title") or "search",
                                "mode": search_spec.get("mode") or "search",
                                "path": search_spec.get("path") or "",
                                "pattern": search_spec.get("pattern") or "",
                                "arguments": search_spec.get("arguments") or {},
                                "content": normalized_search_output,
                            },
                            {"type": "activity", "label": "processing", "active": True},
                        ],
                        "transcript_entries": [{
                            "role": "search",
                            "title": search_spec.get("title") or "search",
                            "mode": search_spec.get("mode") or "search",
                            "path": search_spec.get("path") or "",
                            "pattern": search_spec.get("pattern") or "",
                            "arguments": search_spec.get("arguments") or {},
                            "content": normalized_search_output,
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
                if new_file_spec:
                    path_value = new_file_spec.get("path")
                    path = path_value if isinstance(path_value, str) else ""
                    diff_value = new_file_spec.get("diff")
                    diff_text = diff_value if isinstance(diff_value, str) else ""
                    arguments = _new_file_arguments(new_file_spec)
                    result_payload = {
                        "status": status or "completed",
                        "changed_files": 1,
                    }
                    routed = {
                        "handled": True,
                        "events": [
                            {
                                "type": "tool_end",
                                "id": item_id or _assistant_id(item, thread_id, turn_id),
                                "tool": "apply_patch",
                                "arguments": arguments,
                                "result": result_payload,
                                "output": output,
                                "path": path,
                                "diff": diff_text,
                                "duration_ms": duration_ms,
                                "is_error": is_error,
                                "new_file": True,
                            },
                            {"type": "activity", "label": "processing", "active": True},
                        ],
                        "transcript_entries": [{
                            "role": "tool",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "apply_patch",
                            "arguments": arguments,
                            "result": result_payload,
                            "output": output,
                            "path": path,
                            "diff": diff_text,
                            "duration_ms": duration_ms,
                            "status": status or ("error" if is_error else "completed"),
                            "is_error": is_error,
                            "new_file": True,
                            "timestamp": utc_ts(),
                            "item_id": item_id,
                            "turn_id": turn_id,
                            "event": label_lower,
                        }],
                    }
                    self._emit_filechange_diff_entries(
                        routed,
                        changes=None,
                        diff_text=diff_text,
                        path=path,
                        thread_id=effective_thread_id,
                        turn_id=effective_turn_id,
                        item_id=item_id,
                        event_name=label_lower,
                    )
                    approval_request_id = item_state.get("approval_request_id")
                    if approval_request_id:
                        routed["clear_live_approval_ids"] = [approval_request_id]
                        self._approval_request_map.pop(str(item_id), None)
                    return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)
                routed: ObjectDict = {
                    "handled": True,
                    "events": [
                        {
                            "type": "shell_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "command": display_command,
                            "cwd": cwd,
                            "stdout": output,
                            "stderr": "",
                            "exitCode": exit_code if exit_code is not None else (1 if is_error else 0),
                            "duration_ms": duration_ms,
                            "status": status or ("error" if is_error else "completed"),
                            "is_error": is_error,
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
                paths = _paths_from_changes(changes) or _string_list(item_state.get("paths"))
                output = _normalize_output(item_state.get("output_buffer"))
                status = str(item.get("status") or "").strip().lower()
                duration_ms = item.get("durationMs") if item.get("durationMs") is not None else item.get("duration_ms")
                primary_path = paths[0] if paths else item_state.get("path")
                diff_value = item_state.get("diff")
                diff_text = diff_value if isinstance(diff_value, str) else None
                if not diff_text:
                    diff_text = _diff_text_from_changes_with_headers(changes)
                    extracted_path = None
                    if not diff_text:
                        diff_text, extracted_path = _extract_diff_with_path({"changes": changes, "path": primary_path})
                    if extracted_path and not primary_path:
                        primary_path = extracted_path
                new_file = bool(item_state.get("new_file")) or _diff_is_new_file(diff_text)
                is_error = status in {"failed", "declined", "error"}
                arguments = {
                    "paths": paths,
                    "change_count": len(paths),
                } if paths else {}
                result_payload = {
                    "status": status or "completed",
                    "changed_files": len(paths),
                }
                routed = {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": "apply_patch",
                            "arguments": arguments,
                            "result": result_payload,
                            "output": output,
                            "path": primary_path,
                            "diff": diff_text,
                            "duration_ms": duration_ms,
                            "is_error": is_error,
                            "new_file": new_file,
                        },
                        {"type": "activity", "label": "processing", "active": True},
                    ],
                    "transcript_entries": [{
                        "role": "tool",
                        "id": item_id or _assistant_id(item, thread_id, turn_id),
                        "tool": "apply_patch",
                        "arguments": arguments,
                        "result": result_payload,
                        "output": output,
                        "path": primary_path,
                        "diff": diff_text,
                        "duration_ms": duration_ms,
                        "status": status or ("error" if is_error else "completed"),
                        "is_error": is_error,
                        "new_file": new_file,
                        "timestamp": utc_ts(),
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "event": label_lower,
                    }],
                }
                if not is_error:
                    self._emit_filechange_diff_entries(
                        routed,
                        changes=changes,
                        diff_text=diff_text,
                        path=primary_path if isinstance(primary_path, str) else None,
                        thread_id=effective_thread_id,
                        turn_id=effective_turn_id,
                        item_id=item_id,
                        event_name=label_lower,
                    )
                approval_request_id = item_state.get("approval_request_id")
                if approval_request_id:
                    routed["clear_live_approval_ids"] = [approval_request_id]
                    self._approval_request_map.pop(str(item_id), None)
                return self._decorate_routed_result(routed, thread_id=effective_thread_id, item_state=item_state)

            if item_type in {"mcptoolcall", "websearch", "imageview"}:
                tool_name = _string_value(item.get("tool"), _string_value(item_state.get("tool"), item_type))
                server_name = _string_value(item.get("server"), _string_value(item_state.get("server")))
                arguments = _dict_payload(item.get("arguments")) or _dict_payload(item_state.get("arguments"))
                if item_type == "websearch":
                    tool_name = "web_search"
                    arguments = {"query": item.get("query")}
                elif item_type == "imageview":
                    tool_name = "view_image"
                    arguments = {"path": item.get("path")}
                status = str(item.get("status") or "").strip().lower()
                error = item.get("error")
                duration_ms = _duration_ms(item.get("durationMs"))
                if duration_ms is None:
                    duration_ms = _duration_ms(item.get("duration_ms"))
                result_value, is_error = _extract_tool_result(item.get("result"), status=status, error=error)
                live_result = result_value if result_value is not None else None
                if live_result is None and item_type != "websearch":
                    live_result = {"status": status or "completed"}
                if is_agent_pty_ask_user_tool(server_name, tool_name):
                    approval_request_id = str(
                        item_state.get("approval_request_id")
                        or self._approval_request_map.get(str(item_id or ""))
                        or ""
                    ).strip()
                    if approval_request_id:
                        terminal_resolution = dict(live_result) if _is_object_dict(live_result) else {}
                        if is_error:
                            if not terminal_resolution:
                                terminal_resolution = {"status": "error"}
                            terminal_resolution.setdefault("status", "error")
                            if error not in (None, "", {}):
                                terminal_resolution["error"] = _stringify_value(error)
                        routed = {
                            "handled": True,
                            "events": [],
                            "transcript_entries": [],
                        }
                        routed["clear_live_approval_ids"] = [approval_request_id]
                        self._approval_request_map.pop(str(item_id), None)
                        return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)
                request_payload = item_state.get("request")
                if request_payload is None:
                    request_payload = build_tool_card_request(server_name, tool_name, arguments)
                response_payload = build_tool_card_response(server_name, tool_name, live_result)
                if item_type == "websearch":
                    query = item.get("query")
                    if not isinstance(query, str):
                        query = item_state.get("query") if isinstance(item_state.get("query"), str) else ""
                    results_payload: object = result_value
                    if _is_object_dict(result_value) and "results" in result_value:
                        results_payload = result_value.get("results")
                    search_content = ""
                    if results_payload is not None:
                        search_content = _stringify_value(results_payload)
                    elif live_result is not None:
                        search_content = _stringify_value(live_result)
                    elif isinstance(error, str) and error.strip():
                        search_content = error
                    routed = {
                        "handled": True,
                        "events": [
                            {
                                "type": "search",
                                "id": item_id or _assistant_id(item, thread_id, turn_id),
                                "title": "web search",
                                "mode": "web_search",
                                "path": "",
                                "pattern": query or "",
                                "arguments": {"query": query or ""},
                                "content": search_content,
                                "result": live_result,
                                "results": results_payload,
                                "duration_ms": duration_ms,
                                "is_error": is_error,
                            },
                            {"type": "activity", "label": "processing", "active": True},
                        ],
                        "transcript_entries": [{
                            "role": "search",
                            "title": "web search",
                            "mode": "web_search",
                            "path": "",
                            "pattern": query or "",
                            "arguments": {"query": query or ""},
                            "content": search_content,
                            "result": live_result,
                            "results": results_payload,
                            "duration_ms": duration_ms,
                            "is_error": is_error,
                            "timestamp": utc_ts(),
                            "item_id": item_id,
                            "turn_id": turn_id,
                            "event": label_lower,
                        }],
                    }
                    return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)
                routed = {
                    "handled": True,
                    "events": [
                        {
                            "type": "tool_end",
                            "id": item_id or _assistant_id(item, thread_id, turn_id),
                            "tool": tool_name,
                            "server": server_name,
                            "arguments": arguments,
                            "request": request_payload,
                            "result": live_result,
                            "response": response_payload,
                            "duration_ms": duration_ms,
                            "is_error": is_error,
                        },
                        {"type": "activity", "label": "processing", "active": True},
                    ],
                    "transcript_entries": [],
                }
                _ensure_dict_list(routed, "transcript_entries").append({
                    "role": "mcp_tool",
                    "server": server_name,
                    "tool": tool_name,
                    "call_id": item_id,
                    "arguments": arguments,
                    "request": request_payload,
                    "result": live_result,
                    "response": response_payload,
                    "duration_ms": duration_ms,
                    "is_error": is_error,
                    "timestamp": utc_ts(),
                    "item_id": item_id,
                    "turn_id": turn_id,
                    "event": label_lower,
                })
                return self._decorate_routed_result(routed, thread_id=thread_id, item_state=item_state)

            return {"handled": True, "events": [], "transcript_entries": []}

        if request_spec and request_spec.category == "item" and request_spec.subject == "tool" and request_spec.phase == "call" and _is_object_dict(payload):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            request_id_text = str(request_id or "").strip()
            tool_id = payload.get("callId") or payload.get("call_id") or payload.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = _assistant_id(payload, thread_id, turn_id)
            tool_name = _string_value(payload.get("tool"), "tool")
            arguments = _dict_payload(payload.get("arguments"))
            server_name = _string_value(
                payload.get("server"),
                _string_value(
                    payload.get("serverName"),
                    _string_value(payload.get("server_name"), _string_value(payload.get("mcpServer"))),
                ),
            )
            item_state = self._get_item_state(tool_id, thread_id, turn_id)
            item_state.update({
                "item_type": "tool",
                "tool": tool_name,
                "server": server_name,
                "arguments": arguments,
            })
            if is_agent_pty_ask_user_request(tool_name, arguments):
                request_id = str(conversation_id or "").strip()
                if request_id:
                    if _is_object_dict(arguments):
                        arguments = dict(arguments)
                        arguments["requestId"] = request_id
                        item_state["arguments"] = arguments
                    item_state["approval_request_id"] = request_id
                if item_state.get("ask_user_descriptor_emitted"):
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": [],
                        "transcript_entries": [],
                    }, thread_id=thread_id, item_state=item_state)
                if request_id:
                    return self._ask_user_request_result(
                        tool_id=str(tool_id or ""),
                        request_id=request_id,
                        arguments=arguments,
                        item_state=item_state,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [],
                    "transcript_entries": [],
                }, thread_id=thread_id, item_state=item_state)
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

        if request_spec and request_spec.category == "item" and request_spec.subject == "tool" and request_spec.phase == "requestuserinput" and _is_object_dict(payload):
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

        if request_spec and request_spec.category == "item" and request_spec.subject == "commandexecution" and request_spec.phase == "requestapproval" and _is_object_dict(payload):
            request_id = payload.get("_request_id")
            if request_id is None:
                request_id = payload.get("id")
            item_id = payload.get("itemId") or payload.get("item_id")
            request_id_text = str(request_id or "").strip()
            item_state = self._get_item_state(item_id if isinstance(item_id, str) else None, thread_id, turn_id)
            if request_id_text and item_id:
                self._approval_request_map[str(item_id)] = request_id_text
                item_state["approval_request_id"] = request_id_text
            payload_data: ObjectDict = {
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

        if request_spec and request_spec.category == "item" and request_spec.subject == "filechange" and request_spec.phase == "requestapproval" and _is_object_dict(payload):
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
            changes = payload.get("changes") or item_state.get("changes")
            headered_diff = _diff_text_from_changes_with_headers(changes)
            paths = _paths_from_changes(changes)
            if headered_diff:
                diff_text = headered_diff
            payload_data = {
                "diff": diff_text,
                "changes": changes,
                "paths": paths,
                "reason": payload.get("reason"),
                "path": (path or item_state.get("path")) if len(paths) <= 1 else None,
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

        if request_spec and request_spec.name.lower() == "mcpserver/elicitation/request" and _is_object_dict(payload):
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

        if event_spec and event_spec.category == "user" and event_spec.subject == "message" and _is_object_dict(payload):
            text = _direct_event_text(payload)
            if text:
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                item_id = _assistant_id(payload, effective_thread_id, effective_turn_id)
                item_state = self._get_item_state(item_id, effective_thread_id, effective_turn_id)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [build_message_event(
                        role="user",
                        entry_id=item_id,
                        text=text,
                        turn_id=effective_turn_id,
                    )],
                    "transcript_entries": [build_message_transcript_entry(
                        role="user",
                        entry_id=item_id,
                        item_id=_payload_string(payload, "item_id", "itemId"),
                        text=text,
                        timestamp=utc_ts(),
                        turn_id=effective_turn_id,
                        event=label_lower,
                    )],
                }, thread_id=effective_thread_id, item_state=item_state)

        if event_spec and _is_object_dict(payload):
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
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                prepared = self._prepare_reasoning_state(
                    source="codex",
                    payload=payload,
                    thread_id=effective_thread_id,
                    turn_id=effective_turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                delta = payload.get("delta")
                if isinstance(delta, str):
                    turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}{delta}"
                    scrubbed_delta, thoughts = _extract_and_scrub_thoughts_stream(delta, turn_state)
                    events = [
                        build_thought_event(text=thought, turn_id=effective_turn_id)
                        for thought in thoughts
                    ]
                    live_delta = _consume_live_reasoning_delta(scrubbed_delta, turn_state)
                    if live_delta:
                        events.append(build_reasoning_delta_event(
                            entry_id=item_id,
                            delta=live_delta,
                            turn_id=effective_turn_id,
                        ))
                    if thoughts:
                        events.append({"type": "activity", "label": thoughts[-1], "active": True})
                    elif live_delta:
                        events.append({"type": "activity", "label": "reasoning", "active": True})
                    return self._decorate_routed_result({
                        "handled": True,
                        "events": events,
                        "transcript_entries": [],
                    }, thread_id=effective_thread_id, item_state=item_state)
                return {"handled": True, "events": [], "transcript_entries": []}

            if event_spec.category == "agent" and event_spec.subject == "reasoning_section_break":
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                prepared = self._prepare_reasoning_state(
                    source="codex",
                    payload=payload,
                    thread_id=effective_thread_id,
                    turn_id=effective_turn_id,
                )
                if prepared is None:
                    return {"handled": True, "events": [], "transcript_entries": []}
                turn_state, item_state, item_id = prepared
                turn_state["reasoning_buffer"] = f"{turn_state.get('reasoning_buffer', '')}\n\n"
                live_delta = _consume_live_reasoning_delta("\n\n", turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [build_reasoning_delta_event(
                        entry_id=item_id,
                        delta=live_delta,
                        turn_id=effective_turn_id,
                    )] if live_delta else [],
                    "transcript_entries": [],
                }, thread_id=effective_thread_id, item_state=item_state)

            if event_spec.category == "agent" and event_spec.subject in {"reasoning", "reasoning_raw_content"} and event_spec.phase is None:
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                turn_state = self._get_turn_state(effective_thread_id, effective_turn_id)
                if not self._claim_reasoning_source(turn_state, "codex"):
                    return {"handled": True, "events": [], "transcript_entries": []}
                effective_id = _reasoning_event_id(payload, turn_state)
                turn_state["reasoning_id"] = effective_id
                item_state = self._get_item_state(
                    effective_id if effective_id != "reasoning" else None,
                    effective_thread_id,
                    effective_turn_id,
                )
                item_state["item_type"] = "reasoning"
                reasoning_buffer = turn_state.get("reasoning_buffer")
                text = _extract_reasoning_text(
                    payload,
                    fallback=reasoning_buffer if isinstance(reasoning_buffer, str) else None,
                )
                scrubbed_text, thoughts = _extract_and_scrub_thoughts(text) if text else ("", [])
                has_visible_reasoning = _has_visible_reasoning_text(scrubbed_text)
                should_finalize_live = turn_state.get("reason_source") in {None, "codex"} and has_visible_reasoning
                events: List[ObjectDict] = []
                if thoughts:
                    events.extend(
                        build_thought_event(text=thought, turn_id=effective_turn_id)
                        for thought in thoughts
                    )
                if should_finalize_live:
                    events.append(build_reasoning_finalize_event(
                        entry_id=effective_id,
                        text=scrubbed_text,
                        turn_id=effective_turn_id,
                    ))
                transcript_entries: List[ObjectDict] = []
                if has_visible_reasoning and self._should_record_reasoning(turn_state, effective_id):
                    transcript_entries.append(build_reasoning_transcript_entry(
                        entry_id=effective_id,
                        item_id=effective_id,
                        text=scrubbed_text,
                        timestamp=utc_ts(),
                        turn_id=effective_turn_id,
                        event=label_lower,
                    ))
                self._reset_reasoning_stream(turn_state)
                return self._decorate_routed_result({
                    "handled": True,
                    "events": events,
                    "transcript_entries": transcript_entries,
                }, thread_id=effective_thread_id, item_state=item_state)

        if event_spec and event_spec.category == "agent" and event_spec.subject in {"message", "message_content"} and event_spec.phase == "delta" and _is_object_dict(payload):
            delta = payload.get("delta")
            if isinstance(delta, str):
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                item_id = _assistant_id(payload, effective_thread_id, effective_turn_id)
                item_state = self._get_item_state(item_id, effective_thread_id, effective_turn_id)
                item_state["item_type"] = "assistant_message"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [
                        build_assistant_delta_event(
                            entry_id=item_id,
                            delta=delta,
                            turn_id=effective_turn_id,
                        ),
                        {"type": "activity", "label": "responding", "active": True},
                    ],
                    "transcript_entries": [],
                }, thread_id=effective_thread_id, item_state=item_state)

        if event_spec and event_spec.category == "agent" and event_spec.subject == "message" and event_spec.phase is None and _is_object_dict(payload):
            text = _direct_event_text(payload)
            if text:
                effective_thread_id = _payload_thread_id(payload, thread_id)
                effective_turn_id = _payload_turn_id(payload, turn_id)
                item_id = _assistant_id(payload, effective_thread_id, effective_turn_id)
                item_state = self._get_item_state(item_id, effective_thread_id, effective_turn_id)
                item_state["item_type"] = "assistant_message"
                return self._decorate_routed_result({
                    "handled": True,
                    "events": [build_assistant_finalize_event(
                        entry_id=item_id,
                        text=text,
                        turn_id=effective_turn_id,
                    )],
                    "transcript_entries": [build_message_transcript_entry(
                        role="assistant",
                        entry_id=item_id,
                        item_id=_payload_string(payload, "item_id", "itemId"),
                        text=text,
                        timestamp=utc_ts(),
                        turn_id=effective_turn_id,
                        event=label_lower,
                    )],
                }, thread_id=effective_thread_id, item_state=item_state)

        if event_spec and event_spec.phase == "request" and _is_object_dict(payload) and (
            (event_spec.category == "exec" and event_spec.subject == "approval")
            or (event_spec.category == "apply" and event_spec.subject == "patch_approval")
        ):
            # These codex/event wrappers mirror approval context but do not carry the actionable
            # JSON-RPC request id; only item/*/requestApproval should create live approval cards.
            return {"handled": True, "events": [], "transcript_entries": []}

        if _is_object_dict(payload):
            generic_notification = self._generic_notification_result(
                label_lower=label_lower,
                payload=payload,
                notification_spec=notification_spec,
                event_spec=event_spec,
                turn_id=turn_id,
            )
            if generic_notification is not None:
                return generic_notification

        return result


def route_event(
    protocol: RuntimeProtocol,
    *,
    label: Optional[str],
    payload: object,
    thread_id: Optional[str],
    turn_id: Optional[str],
    conversation_id: Optional[str] = None,
    extract_item_text: Optional[Callable[[ObjectDict], Optional[Dict[str, str]]]] = None,
) -> ObjectDict:
    return CodexEventRouter().route_event(
        protocol,
        label=label,
        payload=payload,
        thread_id=thread_id,
        turn_id=turn_id,
        conversation_id=conversation_id,
        extract_item_text=extract_item_text,
    )

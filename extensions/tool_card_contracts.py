from __future__ import annotations

import json
from typing import TypeAlias, cast

ToolCardMap: TypeAlias = dict[str, object]


def _copy_jsonish(value: object) -> object:
    if isinstance(value, dict):
        value_map = cast(dict[object, object], value)
        return {str(key): _copy_jsonish(inner) for key, inner in value_map.items()}
    if isinstance(value, list):
        value_list = cast(list[object], value)
        return [_copy_jsonish(item) for item in value_list]
    return value


def _copy_object_map(value: object) -> ToolCardMap:
    if not isinstance(value, dict):
        return {}
    value_map = cast(dict[object, object], value)
    return {str(key): _copy_jsonish(inner) for key, inner in value_map.items()}


def _try_parse_json_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        parsed = cast(object, json.loads(text))
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def build_tool_card_request(server_name: str, tool_name: str, arguments: object) -> ToolCardMap:
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()
    request_args = _copy_object_map(arguments)

    if server == "te2-mcp" and tool == "te2_console_eval":
        te2_request: ToolCardMap = {}
        if "target_worker_id" in request_args:
            te2_request["target_worker_id"] = _copy_jsonish(request_args.get("target_worker_id"))
        if "code" in request_args:
            te2_request["code"] = _copy_jsonish(request_args.get("code"))
        return te2_request

    if not server and tool == "sql":
        sql_request: ToolCardMap = {}
        if "description" in request_args:
            sql_request["description"] = _copy_jsonish(request_args.get("description"))
        if "query" in request_args:
            sql_request["query"] = _copy_jsonish(request_args.get("query"))
        return sql_request

    if isinstance(arguments, dict):
        return _copy_object_map(cast(object, arguments))
    if isinstance(arguments, str) and arguments.strip():
        return {"input": arguments}
    return {}


def build_tool_card_response(server_name: str, tool_name: str, response: object) -> object:
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()

    if server == "te2-mcp" and tool == "te2_console_eval":
        parsed = _try_parse_json_text(response)
        if isinstance(parsed, dict):
            parsed_map = cast(dict[object, object], parsed)
            normalized: ToolCardMap = {}
            consumed_keys: set[str] = set()
            for source_key, target_key in (
                ("workerId", "workerId"),
                ("worker_id", "workerId"),
                ("reqId", "reqId"),
                ("req_id", "reqId"),
                ("ok", "ok"),
            ):
                if source_key in parsed_map:
                    normalized[target_key] = _copy_jsonish(parsed_map.get(source_key))
                    consumed_keys.add(source_key)
            if "value" in parsed_map:
                normalized["value"] = _copy_jsonish(parsed_map.get("value"))
                consumed_keys.add("value")
            extras = {
                str(key): _copy_jsonish(value)
                for key, value in parsed_map.items()
                if key not in consumed_keys
            }
            if "value" not in normalized and extras:
                normalized["value"] = extras
            return normalized or {"value": _copy_jsonish(parsed_map)}
        if parsed is None:
            return None
        return {"value": _copy_jsonish(parsed)}

    return _copy_jsonish(response)

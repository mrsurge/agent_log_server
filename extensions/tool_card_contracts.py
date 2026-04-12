import json
from typing import Any, Dict


def _copy_jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_jsonish(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_copy_jsonish(item) for item in value]
    return value


def _try_parse_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def build_tool_card_request(server_name: str, tool_name: str, arguments: Any) -> Any:
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()
    request_args = arguments if isinstance(arguments, dict) else {}

    if server == "te2-mcp" and tool == "te2_console_eval":
        te2_request: Dict[str, Any] = {}
        if "target_worker_id" in request_args:
            te2_request["target_worker_id"] = _copy_jsonish(request_args.get("target_worker_id"))
        if "code" in request_args:
            te2_request["code"] = _copy_jsonish(request_args.get("code"))
        return te2_request

    if not server and tool == "sql":
        sql_request: Dict[str, Any] = {}
        if "description" in request_args:
            sql_request["description"] = _copy_jsonish(request_args.get("description"))
        if "query" in request_args:
            sql_request["query"] = _copy_jsonish(request_args.get("query"))
        return sql_request

    if isinstance(arguments, dict):
        return _copy_jsonish(arguments)
    if isinstance(arguments, str) and arguments.strip():
        return {"input": arguments}
    return {}


def build_tool_card_response(server_name: str, tool_name: str, response: Any) -> Any:
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()

    if server == "te2-mcp" and tool == "te2_console_eval":
        parsed = _try_parse_json_text(response)
        if isinstance(parsed, dict):
            normalized: Dict[str, Any] = {}
            consumed_keys = set()
            for source_key, target_key in (
                ("workerId", "workerId"),
                ("worker_id", "workerId"),
                ("reqId", "reqId"),
                ("req_id", "reqId"),
                ("ok", "ok"),
            ):
                if source_key in parsed:
                    normalized[target_key] = _copy_jsonish(parsed.get(source_key))
                    consumed_keys.add(source_key)
            if "value" in parsed:
                normalized["value"] = _copy_jsonish(parsed.get("value"))
                consumed_keys.add("value")
            extras = {
                key: _copy_jsonish(value)
                for key, value in parsed.items()
                if key not in consumed_keys
            }
            if "value" not in normalized and extras:
                normalized["value"] = extras
            return normalized or {"value": _copy_jsonish(parsed)}
        if parsed is None:
            return None
        return {"value": _copy_jsonish(parsed)}

    return _copy_jsonish(response)

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

JsonObject = dict[str, object]


_SCHEMA_DIR: Final[Path] = Path(__file__).with_name("schemas")
_API_SCHEMA_PATH: Final[Path] = _SCHEMA_DIR / "api.schema.json"
_SESSION_EVENTS_SCHEMA_PATH: Final[Path] = _SCHEMA_DIR / "session-events.schema.json"
_GENERATED_RPC_PATH: Final[Path] = (
    Path(__file__).with_name("_vendor") / "copilot" / "generated" / "rpc.py"
)
_SESSION_RPC_REQUEST_RE: Final[re.Pattern[str]] = re.compile(r'request\("((?:session|clientSession)\.[^"]+)"')


def _snake_case(name: str) -> str:
    result: list[str] = []
    for index, char in enumerate(name):
        if char == ".":
            result.append("_")
            continue
        if char.isupper() and index > 0 and name[index - 1] not in {"_", "."}:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _coerce_json_object(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    value_map = cast(dict[object, object], value)
    return {str(key): item for key, item in value_map.items()}


def _load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as handle:
        data = cast(object, json.load(handle))
    return _coerce_json_object(data) or {}


def _collect_session_methods(node: object, methods: set[str]) -> None:
    node_map = _coerce_json_object(node)
    if node_map is not None:
        rpc_method = node_map.get("rpcMethod")
        if isinstance(rpc_method, str) and rpc_method.startswith("session."):
            methods.add(rpc_method)
        for child in node_map.values():
            _collect_session_methods(child, methods)
    elif isinstance(node, list):
        node_list = cast(list[object], node)
        for child in node_list:
            _collect_session_methods(child, methods)


def _resolve_schema_node(
    node: object,
    definitions: JsonObject,
) -> JsonObject:
    node_map = _coerce_json_object(node)
    if node_map is None:
        return {}
    ref = node_map.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        target = definitions.get(ref.rsplit("/", 1)[-1])
        return _coerce_json_object(target) or {}
    return node_map


def _collect_event_fields(schema: JsonObject) -> dict[str, frozenset[str]]:
    definition_map = _coerce_json_object(schema.get("definitions")) or {}
    root = _resolve_schema_node(schema, definition_map)
    variants = root.get("anyOf")
    if not isinstance(variants, list):
        variants = root.get("oneOf")
    if not isinstance(variants, list):
        return {}
    variant_list = cast(list[object], variants)
    fields: dict[str, frozenset[str]] = {}
    for variant in variant_list:
        resolved_variant = _resolve_schema_node(variant, definition_map)
        if not resolved_variant:
            continue
        properties = resolved_variant.get("properties")
        properties_map = _coerce_json_object(properties)
        if properties_map is None:
            continue
        type_prop = properties_map.get("type")
        data_prop = properties_map.get("data")
        resolved_type = _resolve_schema_node(type_prop, definition_map)
        resolved_data = _resolve_schema_node(data_prop, definition_map)
        if not resolved_type or not resolved_data:
            continue
        event_type = resolved_type.get("const")
        data_properties = resolved_data.get("properties")
        data_properties_map = _coerce_json_object(data_properties)
        if not isinstance(event_type, str) or data_properties_map is None:
            continue
        fields[event_type] = frozenset(_snake_case(name) for name in data_properties_map.keys())
    return fields


def _collect_generated_session_methods(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    source = path.read_text(encoding="utf-8")
    return frozenset(match.group(1) for match in _SESSION_RPC_REQUEST_RE.finditer(source))


@dataclass(frozen=True)
class CopilotProtocolRegistry:
    api_schema_path: Path
    session_events_schema_path: Path
    schema_session_methods: frozenset[str]
    generated_session_methods: frozenset[str]
    session_event_fields: dict[str, frozenset[str]]

    def has_schema_session_method(self, method: str) -> bool:
        return method in self.schema_session_methods

    def has_generated_session_method(self, method: str) -> bool:
        return method in self.generated_session_methods

    def event_has_field(self, event_type: str, field_name: str) -> bool:
        return _snake_case(field_name) in self.session_event_fields.get(event_type, frozenset())

    def known_event_fields(self, event_type: str) -> frozenset[str]:
        return self.session_event_fields.get(event_type, frozenset())


@lru_cache(maxsize=1)
def load_copilot_protocol_registry() -> CopilotProtocolRegistry:
    api_schema = _load_json(_API_SCHEMA_PATH)
    session_event_schema = _load_json(_SESSION_EVENTS_SCHEMA_PATH)
    schema_session_methods: set[str] = set()
    session_root = api_schema.get("session")
    _collect_session_methods(session_root, schema_session_methods)
    return CopilotProtocolRegistry(
        api_schema_path=_API_SCHEMA_PATH,
        session_events_schema_path=_SESSION_EVENTS_SCHEMA_PATH,
        schema_session_methods=frozenset(schema_session_methods),
        generated_session_methods=_collect_generated_session_methods(_GENERATED_RPC_PATH),
        session_event_fields=_collect_event_fields(session_event_schema),
    )

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, cast


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


def _load_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        data = cast(object, json.load(handle))
    return data if isinstance(data, dict) else {}


def _collect_session_methods(node: object, methods: set[str]) -> None:
    if isinstance(node, dict):
        rpc_method = node.get("rpcMethod")
        if isinstance(rpc_method, str) and rpc_method.startswith("session."):
            methods.add(rpc_method)
        for child in node.values():
            _collect_session_methods(child, methods)
    elif isinstance(node, list):
        for child in node:
            _collect_session_methods(child, methods)


def _resolve_schema_node(
    node: object,
    definitions: dict[str, object],
) -> dict[str, object]:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        target = definitions.get(ref.rsplit("/", 1)[-1])
        return target if isinstance(target, dict) else {}
    return node


def _collect_event_fields(schema: dict[str, object]) -> dict[str, frozenset[str]]:
    definitions = schema.get("definitions")
    definition_map = definitions if isinstance(definitions, dict) else {}
    root = _resolve_schema_node(schema, definition_map)
    variants = root.get("anyOf")
    if not isinstance(variants, list):
        variants = root.get("oneOf")
    if not isinstance(variants, list):
        return {}
    fields: dict[str, frozenset[str]] = {}
    for variant in variants:
        resolved_variant = _resolve_schema_node(variant, definition_map)
        if not resolved_variant:
            continue
        properties = resolved_variant.get("properties")
        if not isinstance(properties, dict):
            continue
        type_prop = properties.get("type")
        data_prop = properties.get("data")
        resolved_type = _resolve_schema_node(type_prop, definition_map)
        resolved_data = _resolve_schema_node(data_prop, definition_map)
        if not resolved_type or not resolved_data:
            continue
        event_type = resolved_type.get("const")
        data_properties = resolved_data.get("properties")
        if not isinstance(event_type, str) or not isinstance(data_properties, dict):
            continue
        fields[event_type] = frozenset(_snake_case(str(name)) for name in data_properties.keys())
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

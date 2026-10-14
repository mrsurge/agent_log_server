"""SDK-independent tool registry; captured wire schemas are contract fixtures."""
from __future__ import annotations

import inspect
import json
import re
from types import UnionType
from decimal import Decimal, InvalidOperation
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, Union, cast, get_args, get_origin

ObjectMap = dict[str, object]
ToolFunction = Callable[..., Awaitable[object]]
F = TypeVar("F", bound=ToolFunction)


@dataclass(frozen=True)
class RequestContext:
    request_id: str


@dataclass(frozen=True)
class ToolAnnotations:
    readOnlyHint: bool


def object_map(value: object) -> ObjectMap:
    if not isinstance(value, dict):
        raise ValueError("expected object with string keys")
    if any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError("expected object with string keys")
    return cast(ObjectMap, value)


def object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected array")
    return cast(list[object], value)


def _convert(value: object, schema: ObjectMap) -> object:
    alternatives = schema.get("anyOf")
    if alternatives is not None:
        for candidate in object_list(alternatives):
            branch = object_map(candidate)
            if value is None and branch.get("type") == "null":
                return None
            if isinstance(value, str) and branch.get("type") == "string":
                return value
        for candidate in object_list(alternatives):
            try:
                return _convert(value, object_map(candidate))
            except ValueError:
                pass
        raise ValueError("value does not match any permitted type")
    kind = schema.get("type")
    if kind == "null" and value is None:
        return None
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "integer":
        if isinstance(value, (int, bool)):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.0+)?", value.strip()):
            try:
                number = Decimal(value)
                if number.is_finite() and number == number.to_integral_value():
                    return int(number)
            except (ValueError, OverflowError, InvalidOperation):
                pass
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if value == 0 or value == 1:
            return bool(value)
        if isinstance(value, str):
            if value.lower() in {"true", "1", "yes", "y", "on", "t"}:
                return True
            if value.lower() in {"false", "0", "no", "n", "off", "f"}:
                return False
    if kind == "array":
        if isinstance(value, str):
            try:
                value = cast(object, json.loads(value))
            except ValueError:
                pass
        if isinstance(value, list):
            return [_convert(item, object_map(schema["items"])) for item in cast(list[object], value)]
    raise ValueError(f"expected {kind}")


def _shape(annotation: object) -> ObjectMap:
    for kind, name in ((str, "string"), (int, "integer"), (bool, "boolean"), (type(None), "null")):
        if annotation is kind:
            return {"type": name}
    origin = get_origin(annotation)
    args = cast(tuple[object, ...], get_args(annotation))
    if origin is list and len(args) == 1:
        return {"type": "array", "items": _shape(args[0])}
    if origin in (Union, UnionType):
        return {"anyOf": [_shape(arg) for arg in args]}
    raise ValueError(f"unsupported MCP parameter annotation: {annotation}")


def _schema_shape(schema: ObjectMap) -> ObjectMap:
    if "anyOf" in schema:
        return {"anyOf": [_schema_shape(object_map(item)) for item in object_list(schema["anyOf"])]}
    if schema.get("type") == "array":
        return {"type": "array", "items": _schema_shape(object_map(schema["items"]))}
    return {"type": schema["type"]}


class Registry:
    def __init__(self) -> None:
        self.catalog = object_map(cast(object, json.loads(Path(__file__).with_name("mcp_catalog.json").read_text())))
        self.descriptors = {str(item["name"]): item for raw in object_list(self.catalog["tools"]) if (item := object_map(raw))}
        self.tools: dict[str, ToolFunction] = {}
        self.resources: dict[str, Callable[[], Awaitable[str]]] = {}

    def tool(self, *, name: str, description: str, annotations: ToolAnnotations | None = None) -> Callable[[F], F]:
        def register(fn: F) -> F:
            descriptor = self.descriptors[name]
            if name in self.tools or descriptor["description"] != description:
                raise ValueError(f"tool catalog mismatch: {name}")
            schema = object_map(descriptor["inputSchema"])
            properties = object_map(schema["properties"])
            signature = inspect.signature(fn)
            hints = cast(dict[str, object], inspect.get_annotations(fn, eval_str=True))
            params = {key: param for key, param in signature.parameters.items() if key != "ctx"}
            if set(properties) != set(params):
                raise ValueError(f"tool signature changed: {name}")
            required = set(object_list(schema.get("required", [])))
            for key, param in params.items():
                field = object_map(properties[key])
                if _shape(hints[key]) != _schema_shape(field):
                    raise ValueError(f"tool parameter type changed: {name}.{key}")
                default = cast(object, param.default)
                if (default is inspect.Parameter.empty) != (key in required):
                    raise ValueError(f"tool required parameters changed: {name}.{key}")
                if default is not inspect.Parameter.empty and field.get("default") != default:
                    raise ValueError(f"tool default changed: {name}.{key}")
            wire_annotations = descriptor.get("annotations")
            if annotations is not None and object_map(wire_annotations).get("readOnlyHint") != annotations.readOnlyHint:
                raise ValueError(f"tool annotations changed: {name}")
            if annotations is None and wire_annotations is not None:
                raise ValueError(f"missing tool annotations: {name}")
            self.tools[name] = fn
            return fn
        return register

    def resource(self, uri: str, *, name: str, title: str, description: str, mime_type: str) -> Callable[[Callable[[], Awaitable[str]]], Callable[[], Awaitable[str]]]:
        def register(fn: Callable[[], Awaitable[str]]) -> Callable[[], Awaitable[str]]:
            matches = [object_map(item) for item in object_list(self.catalog["resources"]) if object_map(item)["uri"] == uri]
            if len(matches) != 1 or uri in self.resources:
                raise ValueError("resource catalog mismatch")
            descriptor = matches[0]
            for key, value in {"name": name, "title": title, "description": description, "mimeType": mime_type}.items():
                if descriptor[key] != value:
                    raise ValueError(f"resource metadata changed: {key}")
            self.resources[uri] = fn
            return fn
        return register

    def validate_complete(self) -> None:
        if set(self.tools) != set(self.descriptors) or len(self.resources) != len(object_list(self.catalog["resources"])):
            raise ValueError("incomplete MCP catalog registration")

    def arguments(self, name: str, arguments: ObjectMap) -> ObjectMap:
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        descriptor = self.descriptors[name]
        schema = object_map(descriptor["inputSchema"])
        properties = object_map(schema["properties"])
        kwargs: ObjectMap = {}
        for key, raw in properties.items():
            field = object_map(raw)
            if key in arguments:
                value = arguments[key]
                # Preserve SDK JSON-in-string handling only for nonplain-str fields.
                if isinstance(value, str) and field.get("type") != "string":
                    try:
                        parsed = cast(object, json.loads(value))
                        if not isinstance(parsed, (str, int, float)):
                            value = parsed
                    except ValueError:
                        pass
            elif "default" in field:
                value = field["default"]
            else:
                raise ValueError(f"missing required parameter {key}")
            kwargs[key] = _convert(value, field)
        return kwargs

    async def call(self, name: str, arguments: ObjectMap, context: RequestContext) -> ObjectMap:
        try:
            kwargs = self.arguments(name, arguments)
        except ValueError as exc:
            return {"content": [{"type": "text", "text": f"Error executing tool {name}: {exc}"}], "isError": True}
        if name == "ask_user":
            kwargs["ctx"] = context
        try:
            result = await self.tools[name](**kwargs)
            text = result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
            structured = {"result": result} if isinstance(result, str) else object_map(result)
            return {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": False}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error executing tool {name}: {exc}"}], "isError": True}

    async def read(self, uri: str) -> ObjectMap:
        if uri not in self.resources:
            raise ValueError(f"Unknown resource: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": await self.resources[uri]()}]}

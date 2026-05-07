import ast
import asyncio
import contextlib
import json
import os
import platform
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, cast

from agent_log_server.prompt_context import build_effective_prompt_context
from agent_log_server.te2_mcp_config import (
    build_codex_thread_config,
    te2_mcp_integration_enabled,
)
from .mcp_contract import apply_mcp_context as apply_codex_mcp_context
from .dependencies import is_android_termux as _is_android_termux
from .dependencies import recommended_codex_install_command as _recommended_codex_install_command
from .dependencies import recommended_codex_package as _recommended_codex_package

_server_root: Optional[Path] = None
_extensions_dir: Optional[Path] = None
_runtime_lock: Optional[asyncio.Lock] = None
_runtime_protocol: Optional["RuntimeProtocol"] = None
_AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
_AGENT_PTY_BLOCKS_TOOL_TIMEOUT_SEC = 3600

SchemaDict = Dict[str, object]
SchemaRegistry = Dict[str, SchemaDict]


_SEMANTIC_SUFFIXES = {
    "archived",
    "begin",
    "call",
    "changed",
    "closed",
    "completed",
    "delta",
    "end",
    "outputdelta",
    "request",
    "requestapproval",
    "requestuserinput",
    "resolved",
    "started",
    "terminalinteraction",
    "unarchived",
    "updated",
}

_THREAD_BINDING_FIELDS = ("new_thread_id", "receiver_thread_id")
_CLIENT_RESPONSE_METHODS = frozenset({
    "account/login/cancel",
    "account/login/start",
    "account/logout",
    "account/ratelimits/read",
    "account/read",
    "initialize",
    "command/exec",
    "model/list",
    "thread/compact/start",
    "thread/list",
    "thread/resume",
    "thread/start",
    "turn/interrupt",
    "turn/start",
})
_RESPONSE_SCHEMA_OVERRIDES: Dict[str, str] = {
    "account/login/cancel": "CancelLoginAccountResponse",
    "account/login/start": "LoginAccountResponse",
    "account/logout": "LogoutAccountResponse",
    "account/ratelimits/read": "GetAccountRateLimitsResponse",
    "account/read": "GetAccountResponse",
}
_SERVER_REQUEST_RESPONSE_DEFINITIONS: Dict[str, str] = {
    "account/chatgptauthtokens/refresh": "ChatgptAuthTokensRefreshResponse",
    "item/commandexecution/requestapproval": "CommandExecutionRequestApprovalResponse",
    "item/filechange/requestapproval": "FileChangeRequestApprovalResponse",
    "item/tool/call": "DynamicToolCallResponse",
    "item/tool/requestuserinput": "ToolRequestUserInputResponse",
    "mcpserver/elicitation/request": "McpServerElicitationRequestResponse",
}


class _SchemaDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class ProtocolSemanticSpec:
    name: str
    category: str
    subject: str
    phase: Optional[str]
    properties: Tuple[str, ...]
    call_id_field: Optional[str] = None
    prompt_field: Optional[str] = None
    status_field: Optional[str] = None
    sender_thread_field: Optional[str] = None
    thread_binding_fields: Tuple[str, ...] = ()

    def has_property(self, key: str) -> bool:
        return key in self.properties


@dataclass
class RuntimeProtocol:
    version: str
    version_key: str
    cache_dir: Path
    schema_path: Path
    definitions: Dict[str, object]
    request_params: Dict[str, Dict[str, object]]
    responses: Dict[str, Dict[str, object]]
    server_requests: Dict[str, Dict[str, object]]
    server_request_responses: Dict[str, Dict[str, object]]
    notifications: Dict[str, Dict[str, object]]
    events: Dict[str, Dict[str, object]]
    server_request_semantics: Dict[str, ProtocolSemanticSpec]
    notification_semantics: Dict[str, ProtocolSemanticSpec]
    event_semantics: Dict[str, ProtocolSemanticSpec]
    settings_cache: Dict[str, Dict[str, object]] = field(default_factory=dict)

    def request_schema(self, method: str) -> Optional[Dict[str, object]]:
        return self.request_params.get(method.lower())

    def response_schema(self, method: str) -> Optional[Dict[str, object]]:
        normalized = method.lower()
        schema = self.responses.get(normalized)
        if isinstance(schema, dict):
            return schema
        schema = _resolve_response_schema_from_definitions(method, self.definitions)
        if isinstance(schema, dict):
            self.responses[normalized] = schema
            return schema
        with contextlib.suppress(Exception):
            _refresh_runtime_protocol_from_disk(self)
            schema = self.responses.get(normalized)
            if isinstance(schema, dict):
                return schema
            schema = _resolve_response_schema_from_definitions(method, self.definitions)
            if isinstance(schema, dict):
                self.responses[normalized] = schema
                return schema
        return None

    def notification_schema(self, method: str) -> Optional[Dict[str, object]]:
        return self.notifications.get(method.lower())

    def server_request_schema(self, method: str) -> Optional[Dict[str, object]]:
        return self.server_requests.get(method.lower())

    def server_request_response_schema(self, method: str) -> Optional[Dict[str, object]]:
        return self.server_request_responses.get(method.lower())

    def server_request_spec(self, method: str) -> Optional[ProtocolSemanticSpec]:
        return self.server_request_semantics.get(method.lower())

    def has_server_request(self, method: str) -> bool:
        return method.lower() in self.server_requests

    def notification_spec(self, method: str) -> Optional[ProtocolSemanticSpec]:
        return self.notification_semantics.get(method.lower())

    def has_notification(self, method: str) -> bool:
        return method.lower() in self.notifications

    def has_event_type(self, event_type: str) -> bool:
        return event_type in self.events

    def event_schema(self, event_type: str) -> Optional[SchemaDict]:
        return self.events.get(event_type)

    def event_spec(self, event_type: str) -> Optional[ProtocolSemanticSpec]:
        return self.event_semantics.get(event_type)


def configure_runtime_protocol(
    server_root: Optional[Path],
    extensions_dir: Optional[Path],
) -> None:
    global _server_root, _extensions_dir, _runtime_protocol
    _server_root = server_root
    _extensions_dir = extensions_dir
    _runtime_protocol = None


def peek_runtime_protocol() -> Optional["RuntimeProtocol"]:
    return _runtime_protocol


def _get_runtime_lock() -> asyncio.Lock:
    global _runtime_lock
    if _runtime_lock is None:
        _runtime_lock = asyncio.Lock()
    return _runtime_lock


def _cache_root() -> Path:
    return Path.home() / ".cache" / "app_server" / "codex_app_server_schema"


def _schema_bundle_path(cache_dir: Path) -> Path:
    return cache_dir / "codex_app_server_protocol.v2.schemas.json"


def _legacy_schema_bundle_path(cache_dir: Path) -> Path:
    return cache_dir / "codex_app_server_protocol.schemas.json"


def _version_key(raw_version: str) -> str:
    match = re.search(r"(\d+\.\d+\.\d+)", raw_version)
    token = match.group(1) if match else raw_version.strip()
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", token).strip("_")
    return token or "unknown"


def _normalize_identifier(value: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "-", value.strip())
    text = text.replace("_", "-").replace(" ", "-")
    text = re.sub(r"-+", "-", text)
    return text.lower()


def _display_label(value: str) -> str:
    overrides = {
        "on-failure": "On Failure",
        "on-request": "On Request",
        "read-only": "Read Only",
        "workspace-write": "Workspace Write",
        "danger-full-access": "Danger Full Access",
        "xhigh": "Extra High",
    }
    if value in overrides:
        return overrides[value]
    return value.replace("-", " ").replace("_", " ").title()


def _schema_options(values: List[str]) -> List[Dict[str, str]]:
    return [{"value": value, "label": _display_label(value)} for value in values]


def _expand_path(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return os.path.expanduser(text) if text.startswith("~") else text


def _codex_binary_path() -> str:
    return shutil.which("codex") or "<not found>"


def _generated_schema_files(root: Path, limit: int = 24) -> List[str]:
    if not root.exists():
        return []
    files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    if len(files) <= limit:
        return files
    overflow = len(files) - limit
    return files[:limit] + [f"... (+{overflow} more)"]


def _codex_runtime_context() -> str:
    prefix = os.environ.get("PREFIX", "")
    parts = [f"platform={platform.platform()}"]
    if prefix:
        parts.append(f"PREFIX={prefix}")
    return "; ".join(parts)


def _format_codex_unavailable_message(error: str) -> str:
    return "\n".join(
        (
            f"codex CLI unavailable: {error}",
            f"codex binary: {_codex_binary_path()}",
            _codex_runtime_context(),
            f"Repair command: {_recommended_codex_install_command()}",
        )
    )


def _format_schema_generation_failure(
    *,
    version_raw: str,
    version_key: str,
    temp_dir: Path,
    expected_schema: Path,
    command_error: Optional[str] = None,
) -> str:
    legacy_schema = _legacy_schema_bundle_path(temp_dir)
    generated_files = _generated_schema_files(temp_dir)
    lines = [
        f"schema bundle missing expected file: {expected_schema}",
        f"codex binary: {_codex_binary_path()}",
        f"codex --version: {version_raw}",
        _codex_runtime_context(),
    ]
    if version_key == "0.0.0":
        lines.append(
            "Detected Codex version key 0.0.0, which usually indicates an incompatible or placeholder CLI build."
        )
    if command_error:
        lines.append(f"schema generation command error: {command_error}")
    if legacy_schema.exists() and not expected_schema.exists():
        lines.append(
            f"Found legacy schema bundle {legacy_schema.name} but not {expected_schema.name}; "
            "the installed Codex CLI does not match the v2 schema layout expected by this extension."
        )
    if generated_files:
        lines.append("Generated files:")
        lines.extend(f"  - {entry}" for entry in generated_files)
    else:
        lines.append("Generated files: none")
    lines.append(f"Repair command: {_recommended_codex_install_command()}")
    return "\n".join(lines)


async def _run_process(*args: str, timeout: float) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"command not found: {args[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise RuntimeError(f"command timed out: {' '.join(args)}")
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{' '.join(args)} failed: {message or f'exit {proc.returncode}'}")
    return stdout.decode("utf-8", errors="replace").strip()


def _cleanup_old_cache_dirs(root: Path, keep_name: str) -> None:
    for child in root.iterdir():
        if not child.is_dir() or child.name == keep_name or child.name.startswith(".tmp-"):
            continue
        shutil.rmtree(child, ignore_errors=True)


async def _ensure_schema_bundle() -> tuple[str, str, Path]:
    try:
        version_raw = await _run_process("codex", "--version", timeout=15.0)
    except RuntimeError as exc:
        raise RuntimeError(_format_codex_unavailable_message(str(exc))) from exc
    version_key = _version_key(version_raw)
    cache_root = _cache_root()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_root / version_key
    schema_path = _schema_bundle_path(cache_dir)
    if schema_path.exists():
        _cleanup_old_cache_dirs(cache_root, keep_name=version_key)
        return version_raw, version_key, schema_path

    temp_dir = cache_root / f".tmp-{version_key}-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            await _run_process(
                "codex",
                "app-server",
                "generate-json-schema",
                "--out",
                str(temp_dir),
                timeout=60.0,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                _format_schema_generation_failure(
                    version_raw=version_raw,
                    version_key=version_key,
                    temp_dir=temp_dir,
                    expected_schema=_schema_bundle_path(temp_dir),
                    command_error=str(exc),
                )
            ) from exc
        generated_schema = _schema_bundle_path(temp_dir)
        if not generated_schema.exists():
            raise RuntimeError(
                _format_schema_generation_failure(
                    version_raw=version_raw,
                    version_key=version_key,
                    temp_dir=temp_dir,
                    expected_schema=generated_schema,
                )
            )
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        temp_dir.rename(cache_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    _cleanup_old_cache_dirs(cache_root, keep_name=version_key)
    return version_raw, version_key, schema_path


def _resolve_schema(spec: object, definitions: SchemaDict) -> SchemaDict:
    if not isinstance(spec, dict):
        return {}
    if "$ref" in spec:
        ref = spec["$ref"]
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            return _resolve_schema(definitions.get(ref.rsplit("/", 1)[-1], {}), definitions)
        return {}
    if isinstance(spec.get("allOf"), list):
        merged: SchemaDict = {k: v for k, v in spec.items() if k != "allOf"}
        merged_props: SchemaDict = {}
        merged_required: List[str] = []
        for part in spec["allOf"]:
            resolved = _resolve_schema(part, definitions)
            resolved_properties = _dict_value(resolved.get("properties"))
            if resolved_properties:
                merged_props.update(resolved_properties)
            resolved_required = resolved.get("required")
            if isinstance(resolved_required, list):
                merged_required.extend(str(item) for item in resolved_required)
            for key, value in resolved.items():
                if key in {"properties", "required"}:
                    continue
                merged.setdefault(key, value)
        merged_properties = _dict_value(merged.get("properties"))
        if merged_properties:
            merged_props.update(merged_properties)
        if merged_props:
            merged["properties"] = merged_props
        merged_required_value = merged.get("required")
        existing_required = [str(item) for item in merged_required_value] if isinstance(merged_required_value, list) else []
        if merged_required or existing_required:
            merged["required"] = list(dict.fromkeys(merged_required + existing_required))
        return merged
    return spec


def _schema_string_enums(spec: object, definitions: SchemaDict) -> List[str]:
    values: List[str] = []

    def collect(node: object) -> None:
        resolved = _resolve_schema(node, definitions)
        if not isinstance(resolved, dict):
            return
        enum_values = resolved.get("enum")
        if isinstance(enum_values, list):
            for item in enum_values:
                if isinstance(item, str) and item not in values:
                    values.append(item)
        for key in ("anyOf", "oneOf", "allOf"):
            variants = resolved.get(key)
            if isinstance(variants, list):
                for variant in variants:
                    collect(variant)

    collect(spec)
    return values


def _schema_tagged_union_variants(spec: object, definitions: SchemaDict) -> Dict[str, SchemaDict]:
    variants: Dict[str, SchemaDict] = {}

    def collect(node: object) -> None:
        resolved = _resolve_schema(node, definitions)
        if not isinstance(resolved, dict):
            return
        props = resolved.get("properties")
        if isinstance(props, dict):
            type_prop = _resolve_schema(props.get("type"), definitions)
            enum_values = type_prop.get("enum") if isinstance(type_prop, dict) else None
            if isinstance(enum_values, list) and len(enum_values) == 1 and isinstance(enum_values[0], str):
                variants[enum_values[0]] = resolved
        for key in ("anyOf", "oneOf", "allOf"):
            items = resolved.get(key)
            if isinstance(items, list):
                for item in items:
                    collect(item)

    collect(spec)
    return variants


def _semantic_tokens(name: str, separator: str) -> List[str]:
    tokens: List[str] = []
    for part in name.split(separator):
        normalized = _normalize_identifier(part)
        collapsed = re.sub(r"[^a-z0-9]+", "", normalized)
        if collapsed:
            tokens.append(collapsed)
    return tokens


def _build_semantic_spec(name: str, schema: SchemaDict, *, separator: str) -> ProtocolSemanticSpec:
    props = _schema_properties(schema)
    properties = tuple(key for key in props if key not in {"type", "method"})
    property_set = set(properties)

    tokens = _semantic_tokens(name, separator)
    category = tokens[0] if tokens else ""
    phase = tokens[-1] if tokens and tokens[-1] in _SEMANTIC_SUFFIXES else None
    if phase:
        subject_tokens = tokens[1:-1]
    else:
        subject_tokens = tokens[1:]
    subject = "_".join(subject_tokens) if subject_tokens else category

    thread_binding_fields = tuple(field for field in _THREAD_BINDING_FIELDS if field in property_set)
    call_id_field = "call_id" if "call_id" in property_set else ("id" if "id" in property_set else None)
    prompt_field = "prompt" if "prompt" in property_set else None
    status_field = "status" if "status" in property_set else None
    sender_thread_field = "sender_thread_id" if "sender_thread_id" in property_set else None

    return ProtocolSemanticSpec(
        name=name,
        category=category,
        subject=subject,
        phase=phase,
        properties=properties,
        call_id_field=call_id_field,
        prompt_field=prompt_field,
        status_field=status_field,
        sender_thread_field=sender_thread_field,
        thread_binding_fields=thread_binding_fields,
    )


def _build_notification_semantics(notifications: SchemaRegistry) -> Dict[str, ProtocolSemanticSpec]:
    return {
        name.lower(): _build_semantic_spec(name.lower(), schema, separator="/")
        for name, schema in notifications.items()
    }


def _build_server_request_semantics(server_requests: SchemaRegistry) -> Dict[str, ProtocolSemanticSpec]:
    return {
        name.lower(): _build_semantic_spec(name.lower(), schema, separator="/")
        for name, schema in server_requests.items()
    }


def _build_event_semantics(events: SchemaRegistry) -> Dict[str, ProtocolSemanticSpec]:
    return {
        name: _build_semantic_spec(name, schema, separator="_")
        for name, schema in events.items()
    }


def _build_request_registry(definitions: SchemaDict) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    union = _dict_value(definitions.get("ClientRequest"))
    union_variants = union.get("oneOf")
    if not isinstance(union_variants, list):
        return registry
    for variant in union_variants:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        method_prop = props.get("method")
        params_prop = props.get("params")
        method_values = method_prop.get("enum") if isinstance(method_prop, dict) else None
        if not isinstance(method_values, list) or not method_values:
            continue
        method = method_values[0]
        if isinstance(method, str):
            registry[method.lower()] = _resolve_schema(params_prop, definitions) if isinstance(params_prop, dict) else {}
    return registry


def _request_params_sidecar_path(cache_dir: Path, method: str) -> Path:
    parts = [_pascalize_identifier(part) for part in method.lower().split("/") if part]
    return cache_dir / "v2" / f'{"".join(parts)}Params.json'


def _load_request_sidecar_definitions(cache_dir: Path) -> SchemaDict:
    definitions: SchemaDict = {}
    sidecar_dir = cache_dir / "v2"
    if not sidecar_dir.exists():
        return definitions
    for path in sidecar_dir.glob("*Params.json"):
        try:
            schema = _load_json_object(path)
        except Exception:
            continue
        sidecar_definitions = schema.get("definitions")
        if isinstance(sidecar_definitions, dict):
            definitions.update(sidecar_definitions)
    return definitions


def _load_request_sidecar_registry(
    cache_dir: Path,
    definitions: SchemaDict,
    methods: Iterable[str],
) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    for method in methods:
        if not isinstance(method, str):
            continue
        path = _request_params_sidecar_path(cache_dir, method)
        if not path.exists():
            continue
        try:
            schema = _load_json_object(path)
        except Exception:
            continue
        registry[method.lower()] = _resolve_schema(schema, definitions)
    return registry


def _pascalize_identifier(value: str) -> str:
    expanded = re.sub(r"(?<!^)(?=[A-Z])", " ", value)
    tokens = re.split(r"[^A-Za-z0-9]+", expanded)
    return "".join(token[:1].upper() + token[1:] for token in tokens if token)


def _load_json_object(path: Path) -> SchemaDict:
    return _dict_value(cast(object, json.loads(path.read_text(encoding="utf-8"))))


def _response_definition_name(method: str) -> Optional[str]:
    normalized = method.lower().strip()
    override = _RESPONSE_SCHEMA_OVERRIDES.get(normalized)
    if isinstance(override, str) and override:
        return override
    parts = [_pascalize_identifier(part) for part in normalized.split("/") if part]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return f'{"".join(parts)}Response'


def _method_lookup_tokens(value: str) -> List[str]:
    expanded = re.sub(r"(?<!^)(?=[A-Z])", " ", str(value or "").strip())
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", expanded) if token]


def _infer_response_definition_name(
    method: str,
    definitions: SchemaDict,
) -> Optional[str]:
    normalized = method.lower().strip()
    if not normalized:
        return None

    parts = [part for part in normalized.split("/") if part]
    if parts and parts[-1] == "read":
        read_candidate = f'Get{"".join(_pascalize_identifier(part) for part in parts[:-1])}Response'
        schema = definitions.get(read_candidate)
        if isinstance(schema, dict):
            return read_candidate

    required_tokens = [token for token in _method_lookup_tokens(method) if token not in {"read"}]
    if not required_tokens:
        return None

    matches: List[Tuple[Tuple[int, int, int, str], str]] = []
    for definition_name, schema in definitions.items():
        if not isinstance(definition_name, str) or not definition_name.endswith("Response"):
            continue
        if not isinstance(schema, dict):
            continue
        definition_tokens = _method_lookup_tokens(definition_name[:-8])
        if not all(token in definition_tokens for token in required_tokens):
            continue
        score = (
            len(definition_tokens) - len(required_tokens),
            0 if definition_tokens[:1] == ["get"] else 1,
            len(definition_name),
            definition_name,
        )
        matches.append((score, definition_name))

    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    if len(matches) == 1 or matches[0][0] != matches[1][0]:
        return matches[0][1]
    return None


def _resolve_response_schema_from_definitions(
    method: str,
    definitions: SchemaDict,
) -> Optional[SchemaDict]:
    raw_method = str(method or "").strip()
    normalized = raw_method.lower()
    candidates: List[str] = []
    direct_name = _response_definition_name(normalized)
    if isinstance(direct_name, str) and direct_name:
        candidates.append(direct_name)
    inferred_name = _infer_response_definition_name(raw_method, definitions)
    if isinstance(inferred_name, str) and inferred_name and inferred_name not in candidates:
        candidates.append(inferred_name)
    for definition_name in candidates:
        schema = definitions.get(definition_name)
        if isinstance(schema, dict):
            return _resolve_schema(schema, definitions)
    return None


def _build_response_registry(
    definitions: SchemaDict,
    methods: Iterable[str],
) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    missing: List[str] = []
    normalized_methods = sorted({
        method.lower()
        for method in methods
        if isinstance(method, str) and method.lower() in _CLIENT_RESPONSE_METHODS
    })
    for method in normalized_methods:
        definition_name = _response_definition_name(method)
        if not definition_name:
            missing.append(f"{method} (unresolved)")
            continue
        schema = definitions.get(definition_name)
        if not isinstance(schema, dict):
            missing.append(f"{method} ({definition_name})")
            continue
        registry[method] = _resolve_schema(schema, definitions)
    if missing:
        raise RuntimeError(
            "runtime schema missing response definitions for basic methods: "
            + ", ".join(missing)
        )
    return registry


def _build_runtime_protocol_from_schema(
    version: str,
    version_key: str,
    schema_path: Path,
) -> RuntimeProtocol:
    schema = _load_json_object(schema_path)
    server_request_path = schema_path.parent / "ServerRequest.json"
    server_request_schema = (
        _load_json_object(server_request_path)
        if server_request_path.exists()
        else {"oneOf": [], "definitions": schema.get("definitions") if isinstance(schema.get("definitions"), dict) else {}}
    )
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict):
        raise RuntimeError(f"runtime schema missing definitions: {schema_path}")
    merged_definitions: SchemaDict = {}
    legacy_schema_path = _legacy_schema_bundle_path(schema_path.parent)
    if legacy_schema_path.exists():
        legacy_schema = _load_json_object(legacy_schema_path)
        legacy_definitions = legacy_schema.get("definitions")
        if isinstance(legacy_definitions, dict):
            merged_definitions.update(legacy_definitions)
    merged_definitions.update(definitions)
    merged_definitions.update(_load_request_sidecar_definitions(schema_path.parent))
    request_params = _build_request_registry(merged_definitions)
    request_params.update(
        _load_request_sidecar_registry(schema_path.parent, merged_definitions, request_params.keys())
    )
    responses = _build_response_registry(merged_definitions, request_params.keys())
    server_requests = _build_server_request_registry(server_request_schema)
    server_request_responses = _build_server_request_response_registry(
        merged_definitions,
        server_requests.keys(),
    )
    notifications = _build_notification_registry(merged_definitions)
    events = _build_event_registry(merged_definitions)
    return RuntimeProtocol(
        version=version,
        version_key=version_key,
        cache_dir=schema_path.parent,
        schema_path=schema_path,
        definitions=merged_definitions,
        request_params=request_params,
        responses=responses,
        server_requests=server_requests,
        server_request_responses=server_request_responses,
        notifications=notifications,
        events=events,
        server_request_semantics=_build_server_request_semantics(server_requests),
        notification_semantics=_build_notification_semantics(notifications),
        event_semantics=_build_event_semantics(events),
    )


def _refresh_runtime_protocol_from_disk(protocol: RuntimeProtocol) -> None:
    refreshed = _build_runtime_protocol_from_schema(
        protocol.version,
        protocol.version_key,
        protocol.schema_path,
    )
    protocol.version = refreshed.version
    protocol.version_key = refreshed.version_key
    protocol.cache_dir = refreshed.cache_dir
    protocol.schema_path = refreshed.schema_path
    protocol.definitions = refreshed.definitions
    protocol.request_params = refreshed.request_params
    protocol.responses = refreshed.responses
    protocol.server_requests = refreshed.server_requests
    protocol.server_request_responses = refreshed.server_request_responses
    protocol.notifications = refreshed.notifications
    protocol.events = refreshed.events
    protocol.server_request_semantics = refreshed.server_request_semantics
    protocol.notification_semantics = refreshed.notification_semantics
    protocol.event_semantics = refreshed.event_semantics
    protocol.settings_cache.clear()


def _build_server_request_response_registry(
    definitions: SchemaDict,
    methods: Iterable[str],
) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    missing: List[str] = []
    normalized_methods = sorted({
        method.lower()
        for method in methods
        if isinstance(method, str) and method.lower() in _SERVER_REQUEST_RESPONSE_DEFINITIONS
    })
    for method in normalized_methods:
        definition_name = _SERVER_REQUEST_RESPONSE_DEFINITIONS.get(method)
        if not isinstance(definition_name, str) or not definition_name:
            missing.append(f"{method} (unresolved)")
            continue
        schema = definitions.get(definition_name)
        if not isinstance(schema, dict):
            missing.append(f"{method} ({definition_name})")
            continue
        registry[method] = _resolve_schema(schema, definitions)
    if missing:
        raise RuntimeError(
            "runtime schema missing server-request response definitions: "
            + ", ".join(missing)
        )
    return registry


def _build_server_request_registry(schema: SchemaDict) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    definitions: SchemaDict = _dict_value(schema.get("definitions"))
    union_variants = schema.get("oneOf")
    if not isinstance(union_variants, list):
        return registry
    for variant in union_variants:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        method_prop = props.get("method")
        params_prop = props.get("params")
        method_values = method_prop.get("enum") if isinstance(method_prop, dict) else None
        if not isinstance(method_values, list) or not method_values:
            continue
        method = method_values[0]
        if isinstance(method, str):
            registry[method.lower()] = _resolve_schema(params_prop, definitions) if isinstance(params_prop, dict) else {}
    return registry


def _build_notification_registry(definitions: SchemaDict) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    union = _dict_value(definitions.get("ServerNotification"))
    union_variants = union.get("oneOf")
    if not isinstance(union_variants, list):
        return registry
    for variant in union_variants:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        method_prop = props.get("method")
        params_prop = props.get("params")
        method_values = method_prop.get("enum") if isinstance(method_prop, dict) else None
        if not isinstance(method_values, list) or not method_values:
            continue
        method = method_values[0]
        if isinstance(method, str):
            registry[method.lower()] = _resolve_schema(params_prop, definitions) if isinstance(params_prop, dict) else {}
    return registry


def _build_event_registry(definitions: SchemaDict) -> SchemaRegistry:
    registry: SchemaRegistry = {}
    union = _dict_value(definitions.get("EventMsg"))
    union_variants = union.get("oneOf")
    if not isinstance(union_variants, list):
        return registry
    for variant in union_variants:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        type_prop = props.get("type")
        type_values = type_prop.get("enum") if isinstance(type_prop, dict) else None
        if not isinstance(type_values, list) or not type_values:
            continue
        event_type = type_values[0]
        if isinstance(event_type, str):
            registry[event_type] = variant
    return registry


def _decode_schema_value(
    value: object,
    schema: SchemaDict,
    definitions: SchemaDict,
    path: str,
) -> object:
    resolved = _resolve_schema(schema, definitions)
    if not isinstance(resolved, dict):
        return value

    for key in ("anyOf", "oneOf"):
        variants = resolved.get(key)
        if isinstance(variants, list) and variants:
            for variant in variants:
                try:
                    return _decode_schema_value(value, variant, definitions, path)
                except _SchemaDecodeError:
                    continue
            raise _SchemaDecodeError(f"{path}: value did not match any allowed schema variant")

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list):
        if value in enum_values:
            return value
        raise _SchemaDecodeError(f"{path}: expected one of {enum_values!r}, got {value!r}")

    type_decl = resolved.get("type")
    if isinstance(type_decl, list):
        if value is None and "null" in type_decl:
            return None
        for candidate_type in type_decl:
            if candidate_type == "null":
                continue
            candidate_schema = dict(resolved)
            candidate_schema["type"] = candidate_type
            try:
                return _decode_schema_value(value, candidate_schema, definitions, path)
            except _SchemaDecodeError:
                continue
        raise _SchemaDecodeError(
            f"{path}: expected one of {type_decl!r}, got {type(value).__name__}"
        )

    if value is None:
        if type_decl == "null":
            return None
        raise _SchemaDecodeError(f"{path}: expected {type_decl or 'value'}, got null")

    props_dict = _dict_value(resolved.get("properties"))
    props = props_dict or None
    additional = resolved.get("additionalProperties", True)
    if type_decl == "object" or props is not None or additional is not True:
        if not isinstance(value, dict):
            raise _SchemaDecodeError(f"{path}: expected object, got {type(value).__name__}")
        required_value = resolved.get("required")
        required_fields = required_value if isinstance(required_value, list) else []
        for key in required_fields:
            if key not in value:
                raise _SchemaDecodeError(f"{path}.{key}: missing required property")
        output: SchemaDict = {}
        for key, item in value.items():
            next_path = f"{path}.{key}"
            prop_schema = props.get(key) if props is not None else None
            if isinstance(prop_schema, dict):
                output[key] = _decode_schema_value(item, prop_schema, definitions, next_path)
            elif additional is False:
                raise _SchemaDecodeError(f"{next_path}: unexpected property")
            elif isinstance(additional, dict):
                output[key] = _decode_schema_value(item, additional, definitions, next_path)
            else:
                output[key] = item
        return output

    items_schema = resolved.get("items")
    if type_decl == "array" or isinstance(items_schema, dict):
        if not isinstance(value, list):
            raise _SchemaDecodeError(f"{path}: expected array, got {type(value).__name__}")
        if isinstance(items_schema, dict):
            return [
                _decode_schema_value(item, items_schema, definitions, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return list(value)

    if type_decl == "string":
        if not isinstance(value, str):
            raise _SchemaDecodeError(f"{path}: expected string, got {type(value).__name__}")
        return value
    if type_decl == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _SchemaDecodeError(f"{path}: expected integer, got {type(value).__name__}")
        return value
    if type_decl == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _SchemaDecodeError(f"{path}: expected number, got {type(value).__name__}")
        return value
    if type_decl == "boolean":
        if not isinstance(value, bool):
            raise _SchemaDecodeError(f"{path}: expected boolean, got {type(value).__name__}")
        return value
    if type_decl == "null":
        raise _SchemaDecodeError(f"{path}: expected null, got {type(value).__name__}")

    return value


def decode_response_result(
    protocol: RuntimeProtocol,
    method: str,
    result: object,
) -> object:
    schema = protocol.response_schema(method)
    if not isinstance(schema, dict):
        raise RuntimeError(f"runtime protocol missing response schema for {method}")
    try:
        return _decode_schema_value(
            result,
            schema,
            protocol.definitions,
            path=f"{method}.result",
        )
    except _SchemaDecodeError as exc:
        raise RuntimeError(f"invalid {method} response: {exc}") from exc


def encode_server_request_result(
    protocol: RuntimeProtocol,
    method: str,
    result: object,
) -> object:
    schema = protocol.server_request_response_schema(method)
    if not isinstance(schema, dict):
        raise RuntimeError(f"runtime protocol missing server-request response schema for {method}")
    try:
        return _decode_schema_value(
            result,
            schema,
            protocol.definitions,
            path=f"{method}.result",
        )
    except _SchemaDecodeError as exc:
        raise RuntimeError(f"invalid {method} response: {exc}") from exc


def _match_allowed_string(value: str, allowed: List[str]) -> Optional[str]:
    normalized_value = _normalize_identifier(value)
    if value == "unlessTrusted":
        normalized_value = _normalize_identifier("untrusted")
    for item in allowed:
        if normalized_value == _normalize_identifier(item):
            return item
    return None


def _coerce_schema_value(
    value: object,
    schema: SchemaDict,
    definitions: SchemaDict,
    settings: SchemaDict,
) -> object:
    if value is None or value == "":
        return None

    enum_values = _schema_string_enums(schema, definitions)
    if enum_values:
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            matched = _match_allowed_string(value["type"], enum_values)
            if matched is not None:
                return matched
        if isinstance(value, str):
            matched = _match_allowed_string(value, enum_values)
            if matched is not None:
                return matched

    tagged_variants = _schema_tagged_union_variants(schema, definitions)
    if tagged_variants:
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            matched_tag = None
            for tag in tagged_variants:
                if _normalize_identifier(value["type"]) == _normalize_identifier(tag):
                    matched_tag = tag
                    break
            if matched_tag:
                out = dict(value)
                out["type"] = matched_tag
                if matched_tag == "workspaceWrite" and "writableRoots" not in out:
                    cwd = _expand_path(settings.get("cwd"))
                    if cwd:
                        out["writableRoots"] = [cwd]
                return out
        if isinstance(value, str):
            for tag, variant in tagged_variants.items():
                if _normalize_identifier(value) != _normalize_identifier(tag):
                    continue
                out: SchemaDict = {"type": tag}
                props = _schema_properties(variant)
                if tag == "workspaceWrite" and "writableRoots" in props:
                    cwd = _expand_path(settings.get("cwd"))
                    if cwd:
                        out["writableRoots"] = [cwd]
                return out

    resolved = _resolve_schema(schema, definitions)
    type_decl = resolved.get("type")
    if isinstance(type_decl, list) and "string" in type_decl and isinstance(value, str):
        text = value.strip()
        return text or None
    if type_decl == "string" and isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    return None


def _first_setting(settings: SchemaDict, keys: List[str]) -> object:
    for key in keys:
        value = settings.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def _apply_setting_binding(
    params: SchemaDict,
    props: SchemaDict,
    definitions: SchemaDict,
    settings: SchemaDict,
    source_keys: List[str],
    target_candidates: List[str],
) -> None:
    value = _first_setting(settings, source_keys)
    if value is None or value == "":
        return
    for target in target_candidates:
        schema = props.get(target)
        if not isinstance(schema, dict):
            continue
        coerced = _coerce_schema_value(value, schema, definitions, settings)
        if coerced is not None:
            params[target] = coerced
        return


def _resolve_object_schema(spec: object, definitions: SchemaDict) -> SchemaDict:
    resolved = _resolve_schema(spec, definitions)
    if isinstance(resolved.get("properties"), dict) or resolved.get("type") == "object":
        return resolved
    for key in ("anyOf", "oneOf", "allOf"):
        variants = resolved.get(key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            candidate = _resolve_schema(variant, definitions)
            if isinstance(candidate.get("properties"), dict) or candidate.get("type") == "object":
                return candidate
    return resolved


def _dict_value(value: object) -> Dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _schema_properties(spec: object) -> Dict[str, object]:
    if isinstance(spec, dict):
        return _dict_value(spec.get("properties"))
    return {}


def _schema_path(spec: object, definitions: SchemaDict, path: Iterable[str]) -> SchemaDict:
    current: object = spec
    for segment in path:
        resolved = _resolve_schema(current, definitions)
        if not isinstance(resolved, dict):
            return {}
        if segment == "items":
            current = resolved.get("items")
            continue
        current = _schema_properties(resolved).get(segment)
    resolved = _resolve_schema(current, definitions)
    return resolved if isinstance(resolved, dict) else {}


def build_thread_list_params(protocol: RuntimeProtocol, limit: int = 200) -> SchemaDict:
    schema = protocol.request_schema("thread/list")
    if not isinstance(schema, dict):
        raise RuntimeError("runtime protocol missing request schema for thread/list")
    props = _schema_properties(schema)
    params: SchemaDict = {}
    if "limit" in props:
        params["limit"] = limit
    return params


def normalize_thread_list_timestamp(protocol: RuntimeProtocol, field_name: str, value: object) -> Optional[str]:
    schema = protocol.response_schema("thread/list")
    field_schema = _schema_path(schema or {}, protocol.definitions, ("data", "items", field_name))
    field_type = field_schema.get("type")
    field_types = field_type if isinstance(field_type, list) else [field_type]
    description = field_schema.get("description")
    is_unix_timestamp = (
        "integer" in field_types
        and field_schema.get("format") in {"int64", "uint64", "int32", "uint32"}
        and isinstance(description, str)
        and "unix timestamp" in description.lower()
    )
    if is_unix_timestamp and isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    if "string" in field_types and isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_collaboration_mode_setting(
    props: SchemaDict,
    definitions: SchemaDict,
    settings: SchemaDict,
) -> Optional[SchemaDict]:
    schema = props.get("collaborationMode")
    if not isinstance(schema, dict) and isinstance(definitions.get("CollaborationMode"), dict):
        schema = {"$ref": "#/definitions/CollaborationMode"}
    if not isinstance(schema, dict):
        return None

    mode_value = _first_setting(settings, ["mode"])
    if not isinstance(mode_value, str) or not mode_value.strip():
        return None

    resolved = _resolve_schema(schema, definitions)
    collab_props = _schema_properties(resolved)
    mode_schema = collab_props.get("mode")
    settings_schema = collab_props.get("settings")
    if not isinstance(mode_schema, dict) or not isinstance(settings_schema, dict):
        return None

    coerced_mode = _coerce_schema_value(mode_value, mode_schema, definitions, settings)
    if not isinstance(coerced_mode, str) or not coerced_mode:
        return None

    nested_settings_schema = _resolve_schema(settings_schema, definitions)
    nested_props = _schema_properties(nested_settings_schema)

    model_value = _first_setting(settings, ["model"])
    if not isinstance(model_value, str) or not model_value.strip():
        return None

    nested_settings: SchemaDict = {}
    model_schema = nested_props.get("model")
    if isinstance(model_schema, dict):
        coerced_model = _coerce_schema_value(model_value, model_schema, definitions, settings)
        if not isinstance(coerced_model, str) or not coerced_model:
            return None
        nested_settings["model"] = coerced_model
    else:
        nested_settings["model"] = model_value.strip()

    reasoning_value = _first_setting(settings, ["reasoning_effort", "effort"])
    reasoning_schema = nested_props.get("reasoning_effort")
    if reasoning_schema is not None and reasoning_value not in (None, "") and isinstance(reasoning_schema, dict):
        coerced_reasoning = _coerce_schema_value(reasoning_value, reasoning_schema, definitions, settings)
        if coerced_reasoning is not None:
            nested_settings["reasoning_effort"] = coerced_reasoning

    developer_instructions = _first_setting(settings, ["developer_instructions"])
    developer_schema = nested_props.get("developer_instructions")
    if developer_schema is not None and developer_instructions not in (None, "") and isinstance(developer_schema, dict):
        coerced_developer = _coerce_schema_value(developer_instructions, developer_schema, definitions, settings)
        if coerced_developer is not None:
            nested_settings["developer_instructions"] = coerced_developer

    return {
        "mode": coerced_mode,
        "settings": nested_settings,
    }


def build_initialize_params(protocol: RuntimeProtocol) -> SchemaDict:
    schema = protocol.request_schema("initialize")
    if not isinstance(schema, dict):
        raise RuntimeError("runtime protocol missing request schema for initialize")
    props = _schema_properties(schema)

    client_info_schema = _resolve_object_schema(props.get("clientInfo"), protocol.definitions)
    client_info_props = _schema_properties(client_info_schema)
    if not client_info_props:
        raise RuntimeError("runtime protocol missing initialize.clientInfo schema")

    client_info: SchemaDict = {}
    for key, value in (
        ("name", "agent_log_server"),
        ("title", "Agent Log Server"),
        ("version", "0.1.0"),
    ):
        field_schema = client_info_props.get(key)
        if not isinstance(field_schema, dict):
            continue
        coerced = _coerce_schema_value(value, field_schema, protocol.definitions, {})
        if coerced is not None:
            client_info[key] = coerced
    if not client_info.get("name") or not client_info.get("version"):
        raise RuntimeError("runtime protocol requires initialize.clientInfo.name and version")

    params: SchemaDict = {"clientInfo": client_info}

    capabilities_schema = _resolve_object_schema(props.get("capabilities"), protocol.definitions)
    capability_props = _schema_properties(capabilities_schema)
    experimental_schema = capability_props.get("experimentalApi")
    if isinstance(experimental_schema, dict):
        experimental_api = _coerce_schema_value(True, experimental_schema, protocol.definitions, {})
        if isinstance(experimental_api, bool):
            params["capabilities"] = {"experimentalApi": experimental_api}

    return params


def _agent_pty_mcp_server_script_path() -> Path:
    if isinstance(_server_root, Path):
        package_candidate = _server_root / "mcp_agent_pty_server.py"
        if package_candidate.exists():
            return package_candidate
        repo_candidate = _server_root.parent / "mcp_agent_pty_server.py"
        if repo_candidate.exists():
            return repo_candidate
    return Path(os.path.abspath(__file__)).parents[2] / "mcp_agent_pty_server.py"


@lru_cache(maxsize=1)
def _agent_pty_blocks_tool_names() -> Tuple[str, ...]:
    script_path = _agent_pty_mcp_server_script_path()
    try:
        tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    except OSError as exc:
        raise RuntimeError(f"failed to read {script_path} for agent-pty-blocks tool list") from exc

    names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if not isinstance(call, ast.Call):
                continue
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "tool":
                continue
            tool_name = node.name
            for keyword in call.keywords:
                value = keyword.value
                if (
                    keyword.arg == "name"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.strip()
                ):
                    tool_name = value.value.strip()
                    break
            names.append(tool_name)
            break

    if not names:
        raise RuntimeError(f"no agent-pty-blocks MCP tools found in {script_path}")
    return tuple(dict.fromkeys(names))


def _build_agent_pty_blocks_mcp_server(
    *,
    cwd: object,
    existing_server: object = None,
    conversation_id: object = None,
) -> Optional[SchemaDict]:
    launch_cwd = _expand_path(cwd)
    if not launch_cwd:
        return None
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return None

    command = sys.executable.strip() if isinstance(sys.executable, str) and sys.executable.strip() else "python3"
    merged: SchemaDict = dict(existing_server) if isinstance(existing_server, dict) else {}
    env = _dict_value(merged.get("env"))
    existing_timeout = merged.get("tool_timeout_sec")
    tool_timeout_sec = (
        existing_timeout
        if isinstance(existing_timeout, int)
        and not isinstance(existing_timeout, bool)
        and existing_timeout > _AGENT_PTY_BLOCKS_TOOL_TIMEOUT_SEC
        else _AGENT_PTY_BLOCKS_TOOL_TIMEOUT_SEC
    )
    env["PWD"] = launch_cwd
    env["CONVERSATION_ID"] = conversation_id.strip()

    merged_without_transport_keys: SchemaDict = {
        key: value
        for key, value in merged.items()
        if key not in {"args", "command", "cwd", "disabled_tools", "env", "http_headers", "tools", "type", "url"}
    }
    server: SchemaDict = {
        **merged_without_transport_keys,
        "command": command,
        "args": [str(_agent_pty_mcp_server_script_path())],
        "cwd": launch_cwd,
        "env": env,
        "tool_timeout_sec": tool_timeout_sec,
        "enabled_tools": list(_agent_pty_blocks_tool_names()),
    }
    return server


def _build_codex_ext_thread_config(
    existing_config: object,
    *,
    te2_enabled: bool,
    base_url: Optional[str],
    cwd: object,
    force_te2_mcp_entry: bool = False,
    enable_high_context_400k: bool = False,
    conversation_id: object = None,
) -> Optional[SchemaDict]:
    merged = build_codex_thread_config(
        existing_config,
        te2_enabled=te2_enabled,
        base_url=base_url,
        force_te2_mcp_entry=force_te2_mcp_entry,
        enable_high_context_400k=enable_high_context_400k,
    )
    config: SchemaDict = dict(merged) if isinstance(merged, dict) else {}

    existing_mcp = config.get("mcp_servers")
    if existing_mcp in (None, ""):
        mcp_servers: SchemaDict = {}
    elif isinstance(existing_mcp, dict):
        mcp_servers = dict(existing_mcp)
    else:
        raise ValueError("Codex config.mcp_servers must be a JSON object")

    agent_pty_server = _build_agent_pty_blocks_mcp_server(
        cwd=cwd,
        existing_server=mcp_servers.get(_AGENT_PTY_BLOCKS_MCP_SERVER_NAME),
        conversation_id=conversation_id,
    )
    if agent_pty_server is not None:
        mcp_servers[_AGENT_PTY_BLOCKS_MCP_SERVER_NAME] = agent_pty_server

    if mcp_servers or force_te2_mcp_entry:
        config["mcp_servers"] = mcp_servers
    else:
        config.pop("mcp_servers", None)

    return config or None


def build_request_params(
    protocol: RuntimeProtocol,
    method: str,
    settings: SchemaDict,
    *,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    text: Optional[str] = None,
    force_te2_config: bool = False,
) -> SchemaDict:
    schema = protocol.request_schema(method)
    if not isinstance(schema, dict):
        raise RuntimeError(f"runtime protocol missing request schema for {method}")
    props = _schema_properties(schema)
    params: SchemaDict = {}

    if "threadId" in props:
        if not thread_id:
            raise RuntimeError(f"{method} requires thread_id")
        params["threadId"] = thread_id
    if "turnId" in props:
        if not turn_id:
            raise RuntimeError(f"{method} requires turn_id")
        params["turnId"] = turn_id
    if "input" in props:
        if text is None:
            raise RuntimeError(f"{method} requires text input")
        params["input"] = [{"type": "text", "text": text}]

    normalized_settings = dict(settings)
    high_context_400k = normalized_settings.get("high_context_400k") in (True, "true")
    cwd = _expand_path(normalized_settings.get("cwd"))
    if cwd:
        normalized_settings["cwd"] = cwd
    te2_base_url = normalized_settings.get("te2_base_url")
    base_url = te2_base_url if isinstance(te2_base_url, str) else None
    cwd_setting = normalized_settings.get("cwd")
    config_cwd = cwd_setting if isinstance(cwd_setting, str) else None
    conversation_value = normalized_settings.get("conversation_id")
    conversation_id = conversation_value if isinstance(conversation_value, str) else None

    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["cwd"], ["cwd"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["model"], ["model"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["approvalPolicy"], ["approvalPolicy"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["sandboxPolicy", "sandbox"], ["sandboxPolicy", "sandbox"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["reasoning_effort", "effort"], ["reasoningEffort", "effort"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["summary"], ["summary"])
    collaboration_mode = _build_collaboration_mode_setting(props, protocol.definitions, normalized_settings)
    if collaboration_mode is not None:
        params["collaborationMode"] = collaboration_mode

    if "config" in props or method == "turn/start":
        if isinstance(normalized_settings.get("mcp_context"), dict):
            config = apply_codex_mcp_context(
                normalized_settings.get("config"),
                normalized_settings,
                force_te2_mcp_entry=force_te2_config,
                enable_high_context_400k=high_context_400k,
            )
        else:
            config = _build_codex_ext_thread_config(
                normalized_settings.get("config"),
                te2_enabled=te2_mcp_integration_enabled(normalized_settings),
                base_url=base_url,
                cwd=config_cwd,
                force_te2_mcp_entry=force_te2_config,
                enable_high_context_400k=high_context_400k,
                conversation_id=conversation_id,
            )
        if config:
            params["config"] = config
    prompt_context = build_effective_prompt_context(
        normalized_settings.get("developer_instructions"),
        te2_enabled=te2_mcp_integration_enabled(normalized_settings),
        cwd=config_cwd,
    )
    if prompt_context:
        if "developerInstructions" in props or method == "turn/start":
            params["developerInstructions"] = prompt_context
        elif "baseInstructions" in props:
            params["baseInstructions"] = prompt_context

    return params


def build_thread_runtime_signature_payload(
    protocol: RuntimeProtocol,
    settings: SchemaDict,
) -> SchemaDict:
    return build_request_params(
        protocol,
        "thread/start",
        settings,
        force_te2_config=True,
    )


def build_settings_schema(protocol: RuntimeProtocol, extension_id: str) -> SchemaDict:
    cached = protocol.settings_cache.get(extension_id)
    if cached is not None:
        return cached

    thread_start = protocol.request_schema("thread/start") or {}
    turn_start = protocol.request_schema("turn/start") or {}
    thread_props = _schema_properties(thread_start)
    turn_props = _schema_properties(turn_start)

    approval_values = _schema_string_enums(thread_props.get("approvalPolicy", {}), protocol.definitions)
    sandbox_values = _schema_string_enums(thread_props.get("sandbox", {}), protocol.definitions)
    effort_values = _schema_string_enums(turn_props.get("effort", {}), protocol.definitions)
    summary_values = _schema_string_enums(turn_props.get("summary", {}), protocol.definitions)
    collaboration_ref = turn_props.get("collaborationMode")
    if not isinstance(collaboration_ref, dict) and isinstance(protocol.definitions.get("CollaborationMode"), dict):
        collaboration_ref = {"$ref": "#/definitions/CollaborationMode"}
    collaboration_schema = _resolve_schema(collaboration_ref or {}, protocol.definitions)
    collaboration_props = _schema_properties(collaboration_schema)
    mode_values = _schema_string_enums(collaboration_props.get("mode", {}), protocol.definitions)
    mode_options = _schema_options(mode_values)

    schema = {
        "version": "1",
        "description": "Runtime-generated settings schema for the Codex app-server extension",
        "generated_from": str(protocol.schema_path),
        "codex_version": protocol.version,
        "fields": [
            {
                "id": "cwd",
                "type": "path",
                "label": "Working Directory",
                "placeholder": "~/project",
                "default": "~",
                "required": True,
                "browse": True,
            },
            {
                "id": "session",
                "type": "session_picker",
                "label": "Session",
                "placeholder": "(new session) or type/paste session ID",
                "source": f"/api/extensions/{extension_id}/sessions",
                "resume_endpoint": f"/api/extensions/{extension_id}/sessions/resume",
                "picker_sort": {
                    "param": "sort",
                    "default": "updated_at",
                    "options": [
                        {"value": "updated_at", "label": "MRU"},
                        {"value": "created_at", "label": "Created"},
                    ],
                },
            },
            {
                "id": "model",
                "type": "select",
                "label": "Model",
                "options": [],
                "dynamic_source": f"/api/extensions/{extension_id}/models",
                "placeholder": "Use server default",
                "default": "",
            },
            {
                "id": "high_context_400k",
                "type": "checkbox",
                "label": "400k Context",
                "default": False,
                "model_gate": {
                    "family": "gpt",
                    "min_major": 5,
                    "min_minor": 4,
                    "label": "GPT-5.4 or newer",
                },
            },
            {
                "id": "reasoning_effort",
                "type": "select",
                "label": "Reasoning Effort",
                "options": [],
                "dynamic_source": f"/api/extensions/{extension_id}/models",
                "dynamic_options_from": {
                    "source_field": "model",
                    "match_path": "id",
                    "options_path": [
                        "supportedReasoningEfforts",
                        "supported_reasoning_efforts",
                        "raw.supportedReasoningEfforts",
                        "raw.supported_reasoning_efforts",
                    ],
                    "option_value_path": "reasoningEffort",
                    "option_label_path": "reasoningEffort",
                    "default_path": [
                        "defaultReasoningEffort",
                        "default_reasoning_effort",
                        "raw.defaultReasoningEffort",
                        "raw.default_reasoning_effort",
                    ],
                    "missing_source_placeholder": "Select model first",
                    "empty_placeholder": "Not supported by selected model",
                },
                "placeholder": "Select model first",
                "default": "",
                "value_keys": ["effort"],
            },
            {
                "id": "summary",
                "type": "select",
                "label": "Reasoning Summary",
                "options": _schema_options(summary_values),
                "placeholder": "Use model default",
                "default": "",
            },
            {
                "id": "approvalPolicy",
                "type": "select",
                "label": "Approval Policy",
                "options": _schema_options(approval_values),
                "placeholder": "Use server default",
                "default": "",
                "runtime_option": {
                    "kind": "approval",
                    "footer": True,
                    "footer_label": "Approval",
                },
            },
            {
                "id": "sandboxPolicy",
                "type": "select",
                "label": "Sandbox Policy",
                "options": _schema_options(sandbox_values),
                "placeholder": "Use server default",
                "default": "",
                "runtime_option": {
                    "kind": "sandbox",
                },
            },
            {
                "id": "mode",
                "type": "select",
                "label": "Mode",
                "options": mode_options,
                "placeholder": "Use runtime default",
                "default": "",
                "runtime_option": {
                    "kind": "mode",
                    "footer": True,
                    "footer_label": "Mode",
                    "accents": {
                        "plan": "ok",
                        "default": "warn",
                    },
                },
            },
            {
                "id": "developer_instructions",
                "type": "textarea",
                "label": "Developer Instructions",
                "placeholder": "Additional runtime instructions appended to the Codex thread configuration",
                "rows": 6,
                "default": "",
            },
        ],
    }
    protocol.settings_cache[extension_id] = schema
    return schema


async def get_runtime_protocol() -> RuntimeProtocol:
    global _runtime_protocol
    if _runtime_protocol is not None:
        return _runtime_protocol

    async with _get_runtime_lock():
        if _runtime_protocol is not None:
            return _runtime_protocol
        version, version_key, schema_path = await _ensure_schema_bundle()
        _runtime_protocol = _build_runtime_protocol_from_schema(version, version_key, schema_path)
        return _runtime_protocol
